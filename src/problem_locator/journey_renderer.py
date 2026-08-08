"""Strict Journey JSONL loading and deterministic human-readable projections."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from problem_locator.contracts.models import NonEmptyText, OpaqueId, UtcTimestamp
from problem_locator.journey import JourneyEvent


_KNOWN_EVENTS = frozenset(
    {
        "case.created",
        "case.supplement.applied",
        "case.resumed",
        "case.cancelled",
        "case.status.changed",
        "attachment.prepared",
        "attachment.uploaded",
        "job.pending_persisted",
        "job.queued",
        "job.queue.duplicate",
        "job.queue.failed",
        "job.claimed",
        "job.claim.failed",
        "job.cancel.signalled",
        "job.cancel.signal_failed",
        "job.stage.started",
        "job.stage.completed",
        "job.stage.failed",
        "job.outcome.produced",
        "job.outcome.applied",
        "job.outcome.rejected",
        "job.outcome.stale",
        "job.execution.failure_applied",
    }
)
_TERMINAL_STATUSES = frozenset(
    {"RESOLVED", "UNRESOLVED", "FAILED", "CANCELLED"}
)
_EVENT_TITLES = {
    "case.created": "创建 Case",
    "case.supplement.applied": "应用补充信息",
    "case.resumed": "恢复 Case",
    "case.cancelled": "取消 Case",
    "case.status.changed": "Case 状态变化",
    "attachment.prepared": "准备附件",
    "attachment.uploaded": "附件上传完成",
    "job.pending_persisted": "Job 已持久化",
    "job.queued": "Job 已入队",
    "job.queue.duplicate": "Job 重复入队信号",
    "job.queue.failed": "Job 入队信号失败",
    "job.claimed": "Job 已认领",
    "job.claim.failed": "Job 认领失败",
    "job.cancel.signalled": "Job 已收到取消信号",
    "job.cancel.signal_failed": "Job 取消信号失败",
    "job.stage.started": "执行阶段开始",
    "job.stage.completed": "执行阶段完成",
    "job.stage.failed": "执行阶段失败",
    "job.outcome.produced": "Outcome 已生成",
    "job.outcome.applied": "Outcome 已应用",
    "job.outcome.rejected": "Outcome 被拒绝",
    "job.outcome.stale": "Outcome 已过期",
    "job.execution.failure_applied": "执行基础设施失败已应用",
}


class JourneySourceError(ValueError):
    """The complete Journey source cannot be trusted or parsed."""


class JourneyCaseNotFound(ValueError):
    """No valid event belongs to the requested Case."""


class JourneyOutputError(OSError):
    """A rendered view could not be published."""


@dataclass(frozen=True, slots=True)
class JourneyLine:
    line_number: int
    event: JourneyEvent


class RenderJourneyReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    case_id: OpaqueId
    case_status: NonEmptyText
    terminal: bool
    as_of: UtcTimestamp
    events_rendered: int
    last_sequence: int
    unknown_event_count: int
    detailed_log: NonEmptyText
    brief_log: NonEmptyText


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def load_journey(path: Path) -> tuple[JourneyLine, ...]:
    """Read and validate the entire source before any Case filtering."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise JourneySourceError(f"journey.jsonl could not be read: {exc}") from exc
    if not raw:
        raise JourneySourceError("journey.jsonl is empty")

    raw_lines = raw.splitlines(keepends=True)
    loaded: list[JourneyLine] = []
    expected_sequence = 1
    for line_number, raw_line in enumerate(raw_lines, start=1):
        if not raw_line.endswith(b"\n"):
            raise JourneySourceError(
                f"journey.jsonl:{line_number}: truncated line without LF"
            )
        body = raw_line[:-1]
        if body.endswith(b"\r"):
            raise JourneySourceError(
                f"journey.jsonl:{line_number}: CRLF is not supported"
            )
        if not body:
            raise JourneySourceError(f"journey.jsonl:{line_number}: blank line")
        try:
            text = body.decode("utf-8")
            payload = json.loads(
                text,
                parse_constant=_reject_constant,
                object_pairs_hook=_unique_object,
            )
            event = JourneyEvent.model_validate(payload, strict=True)
        except (UnicodeDecodeError, ValueError, TypeError, ValidationError) as exc:
            raise JourneySourceError(
                f"journey.jsonl:{line_number}: invalid Journey event: {exc}"
            ) from exc
        if event.sequence != expected_sequence:
            raise JourneySourceError(
                f"journey.jsonl:{line_number}: expected sequence "
                f"{expected_sequence}, got {event.sequence}"
            )
        loaded.append(JourneyLine(line_number=line_number, event=event))
        expected_sequence += 1
    return tuple(loaded)


def _nested(mapping: Any, *keys: str) -> Any:
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _case_status(lines: tuple[JourneyLine, ...]) -> str:
    for line in reversed(lines):
        data = line.event.data
        candidates = (
            data.get("to_status"),
            _nested(data, "case_view", "status"),
            _nested(data, "to_case", "status"),
            _nested(data, "case", "status"),
            data.get("status"),
        )
        for candidate in candidates:
            if isinstance(candidate, str) and candidate:
                return candidate
    return "UNKNOWN"


def _problem_statement(lines: tuple[JourneyLine, ...]) -> str:
    for line in lines:
        if line.event.event != "case.created":
            continue
        spec = line.event.data.get("problem_spec")
        if isinstance(spec, dict):
            statement = spec.get("statement")
            if isinstance(statement, str) and statement:
                return statement
        case_spec = _nested(line.event.data, "case", "diagnosis_state", "problem_spec")
        if isinstance(case_spec, dict):
            statement = case_spec.get("statement")
            if isinstance(statement, str) and statement:
                return statement
    return "未在 Journey 中记录问题描述"


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _duration_text(lines: tuple[JourneyLine, ...]) -> str:
    elapsed = max(
        0.0,
        (
            _parse_timestamp(lines[-1].event.timestamp)
            - _parse_timestamp(lines[0].event.timestamp)
        ).total_seconds(),
    )
    total = int(round(elapsed))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return "".join(parts)


def _pretty_data(data: dict[str, Any]) -> list[str]:
    rendered = json.dumps(
        data,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )
    return [f"    {line}" for line in rendered.splitlines()]


def render_detailed(
    case_id: str,
    lines: tuple[JourneyLine, ...],
) -> str:
    status = _case_status(lines)
    terminal = status in _TERMINAL_STATUSES
    unknown_count = sum(line.event.event not in _KNOWN_EVENTS for line in lines)
    result = [
        "Problem Locator 服务端端到端详细日志",
        f"Case ID: {case_id}",
        f"状态: {status}",
        f"性质: {'终态记录' if terminal else '当前快照（非最终结论）'}",
        f"问题: {_problem_statement(lines)}",
        f"截至: {lines[-1].event.timestamp} / sequence {lines[-1].event.sequence}",
        f"事件数: {len(lines)}",
    ]
    if unknown_count:
        result.append(f"完整性提示: 存在 {unknown_count} 个未识别事件，已按通用格式保留")
    result.append("")

    for line in lines:
        event = line.event
        title = _EVENT_TITLES.get(event.event, "未知事件")
        result.append(
            f"[{event.sequence:06d}][{event.timestamp}] {title} [{event.event}]"
        )
        result.append(f"  来源: journey.jsonl:{line.line_number}")
        result.append(f"  级别: {event.level}")
        if event.correlation_id is not None:
            result.append(f"  correlation_id: {event.correlation_id}")
        if event.request_id is not None:
            result.append(f"  request_id: {event.request_id}")
        if event.job_id is not None:
            result.append(f"  job_id: {event.job_id}")
        if event.job_type is not None:
            result.append(f"  job_type: {event.job_type}")
        if event.outcome_id is not None:
            result.append(f"  outcome_id: {event.outcome_id}")
        if event.duration_ms is not None:
            result.append(f"  耗时: {event.duration_ms:.3f} ms")
        result.append("  语义数据:")
        result.extend(_pretty_data(event.data))
        result.append("")
    return "\n".join(result).rstrip() + "\n"


def _skill_text(data: dict[str, Any]) -> str | None:
    skill_ref = _nested(data, "outcome", "payload", "skill_ref")
    if not isinstance(skill_ref, dict):
        skill_ref = _nested(data, "case_view", "selected_skill_ref")
    if not isinstance(skill_ref, dict):
        return None
    identifier = skill_ref.get("id")
    version = skill_ref.get("version")
    if isinstance(identifier, str) and isinstance(version, str):
        return f"{identifier}@{version}"
    return None


def _candidate_statement(data: dict[str, Any]) -> str | None:
    candidates = (
        _nested(data, "case_view", "final_result", "statement"),
        _nested(data, "outcome", "payload", "candidate_conclusion_draft", "statement"),
        _nested(data, "to_case", "final_result", "statement"),
        _nested(data, "final_result", "statement"),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _brief_milestone(line: JourneyLine) -> str | None:
    event = line.event
    data = event.data
    if event.event == "case.created":
        return "创建 Case，并持久化初始 ROUTE Job"
    if event.event == "attachment.prepared":
        attachment = data.get("attachment")
        name = attachment.get("name") if isinstance(attachment, dict) else None
        return f"准备附件{f' {name}' if isinstance(name, str) else ''}"
    if event.event == "attachment.uploaded":
        return f"附件上传完成，SHA-256={data.get('actual_sha256', 'UNKNOWN')}"
    if event.event == "case.supplement.applied":
        command = data.get("command")
        inputs = command.get("inputs") if isinstance(command, dict) else None
        attachments = command.get("attachment_ids") if isinstance(command, dict) else None
        return (
            "应用补充信息"
            f"（输入 {len(inputs) if isinstance(inputs, dict) else 0} 项，"
            f"附件 {len(attachments) if isinstance(attachments, list) else 0} 个）"
        )
    if event.event == "case.resumed":
        return "恢复 Case 并继续既有或替代 Job"
    if event.event == "case.cancelled":
        return "用户取消 Case"
    if event.event == "job.outcome.applied":
        result_type = _nested(data, "outcome", "result_type") or "UNKNOWN"
        if event.job_type == "ROUTE":
            skill = _skill_text(data) or "未匹配 Skill"
            confidence = _nested(data, "outcome", "payload", "confidence")
            suffix = f"，confidence={confidence}" if confidence is not None else ""
            return f"ROUTE Outcome 已应用：{skill}{suffix}"
        if event.job_type == "DIAGNOSE":
            statement = _candidate_statement(data)
            suffix = f"，候选结论：{statement}" if statement else ""
            return f"DIAGNOSE Outcome 已应用：{result_type}{suffix}"
        if event.job_type == "REVIEW":
            verdict = _nested(data, "outcome", "payload", "verdict") or "UNKNOWN"
            recommendation = _nested(data, "outcome", "payload", "recommendation")
            suffix = f"，{recommendation}" if isinstance(recommendation, str) else ""
            return f"REVIEW Outcome 已应用：{verdict}{suffix}"
        return f"Outcome 已应用：{result_type}"
    if event.event == "job.outcome.rejected":
        return f"Outcome 被拒绝：{data.get('rejection_code', 'UNKNOWN')}"
    if event.event == "job.outcome.stale":
        return "Outcome 已过期，未作为当前诊断事实应用"
    if event.event == "job.claim.failed":
        return f"Job 认领失败：{data.get('code', 'UNKNOWN')}"
    if event.event == "job.execution.failure_applied":
        failure = data.get("failure")
        code = failure.get("code") if isinstance(failure, dict) else "UNKNOWN"
        return f"执行基础设施失败已应用：{code}"
    if event.event == "job.stage.failed":
        return (
            f"执行阶段失败：{data.get('stage', 'UNKNOWN')} / "
            f"{data.get('code', 'UNKNOWN')}"
        )
    if event.event == "case.status.changed":
        return f"Case 状态：{data.get('from_status')} → {data.get('to_status')}"
    return None


def _latest_failure(lines: tuple[JourneyLine, ...]) -> str:
    for line in reversed(lines):
        data = line.event.data
        if line.event.event == "job.stage.failed":
            return (
                f"阶段 {data.get('stage', 'UNKNOWN')}，"
                f"错误 {data.get('code', 'UNKNOWN')}，"
                f"retryable={str(data.get('retryable', False)).lower()}，"
                f"原始位置 journey.jsonl:{line.line_number}"
            )
        if line.event.event == "job.execution.failure_applied":
            failure = data.get("failure")
            if isinstance(failure, dict):
                return (
                    f"阶段 {failure.get('stage', 'UNKNOWN')}，"
                    f"错误 {failure.get('code', 'UNKNOWN')}，"
                    f"retryable={str(failure.get('retryable', False)).lower()}，"
                    f"原始位置 journey.jsonl:{line.line_number}"
                )
    return "无"


def _pending_text(lines: tuple[JourneyLine, ...]) -> str:
    for line in reversed(lines):
        data = line.event.data
        requirements = data.get("pending_requirements")
        if not isinstance(requirements, list):
            requirements = _nested(data, "case_view", "pending_requirements")
        if isinstance(requirements, list):
            open_items = [
                item
                for item in requirements
                if isinstance(item, dict) and item.get("status") == "OPEN"
            ]
            if open_items:
                names = [
                    str(item.get("name", item.get("requirement_id")))
                    for item in open_items
                ]
                return "、".join(names)
    return "无"


def render_brief(case_id: str, lines: tuple[JourneyLine, ...]) -> str:
    status = _case_status(lines)
    terminal = status in _TERMINAL_STATUSES
    unknown_count = sum(line.event.event not in _KNOWN_EVENTS for line in lines)
    milestones = [
        milestone
        for line in lines
        if (milestone := _brief_milestone(line)) is not None
    ]
    conclusion = next(
        (
            statement
            for line in reversed(lines)
            if (statement := _candidate_statement(line.event.data)) is not None
        ),
        None,
    )
    result = [
        "Problem Locator 服务端端到端简略日志",
        f"Case ID: {case_id}",
        f"状态: {status}",
        f"性质: {'终态记录' if terminal else '当前快照（非最终结论）'}",
        f"问题: {_problem_statement(lines)}",
        f"耗时: {_duration_text(lines)}",
        f"截至: {lines[-1].event.timestamp} / sequence {lines[-1].event.sequence}",
        "",
        "定位路径:",
    ]
    if milestones:
        result.extend(f"{index}. {item}" for index, item in enumerate(milestones, start=1))
    else:
        result.append("1. 尚无可归纳的业务里程碑")
    result.extend(
        [
            "",
            f"{'最终结论' if terminal and status == 'RESOLVED' else '当前发现'}: "
            f"{conclusion or '尚未形成可记录的结论'}",
            f"待补充/阻塞: {_pending_text(lines)}",
            f"失败信息: {_latest_failure(lines)}",
        ]
    )
    if unknown_count:
        result.append(
            f"完整性提示: 存在 {unknown_count} 个未识别事件，请查看 detailed.log"
        )
    if not terminal:
        result.append("提示: 该 Case 尚未结束，本文件不能作为最终根因结论。")
    return "\n".join(result).rstrip() + "\n"


def _atomic_replace(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def render_journey(log_dir: Path, case_id: str) -> RenderJourneyReceipt:
    """Validate one log snapshot and publish both fixed Case projections."""

    try:
        case_id = TypeAdapter(OpaqueId).validate_python(case_id, strict=True)
    except (TypeError, ValueError, ValidationError) as exc:
        raise JourneyCaseNotFound("case_id must be a canonical lowercase UUID") from exc
    source = log_dir / "journey.jsonl"
    all_lines = load_journey(source)
    lines = tuple(line for line in all_lines if line.event.case_id == case_id)
    if not lines:
        raise JourneyCaseNotFound(f"Case {case_id} is not present in journey.jsonl")

    detailed = render_detailed(case_id, lines).encode("utf-8")
    brief = render_brief(case_id, lines).encode("utf-8")
    output_dir = log_dir / "cases" / case_id
    detailed_path = output_dir / "detailed.log"
    brief_path = output_dir / "brief.log"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        _atomic_replace(detailed_path, detailed)
        _atomic_replace(brief_path, brief)
    except OSError as exc:
        raise JourneyOutputError(f"rendered logs could not be published: {exc}") from exc

    status = _case_status(lines)
    return RenderJourneyReceipt(
        case_id=case_id,
        case_status=status,
        terminal=status in _TERMINAL_STATUSES,
        as_of=lines[-1].event.timestamp,
        events_rendered=len(lines),
        last_sequence=lines[-1].event.sequence,
        unknown_event_count=sum(
            line.event.event not in _KNOWN_EVENTS for line in lines
        ),
        detailed_log=str(detailed_path.resolve(strict=False)),
        brief_log=str(brief_path.resolve(strict=False)),
    )


__all__ = [
    "JourneyCaseNotFound",
    "JourneyLine",
    "JourneyOutputError",
    "JourneySourceError",
    "RenderJourneyReceipt",
    "load_journey",
    "render_brief",
    "render_detailed",
    "render_journey",
]
