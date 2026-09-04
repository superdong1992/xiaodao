"""Deterministic, non-overlapping timing attribution for one Case Journey."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any


_WAIT_STATUSES = frozenset({"WAITING_INPUT", "WAITING_ATTACHMENT", "INTERRUPTED"})
_WAIT_END_EVENTS = frozenset({"case.supplement.applied", "case.resumed", "case.cancelled"})


def _timestamp_ms(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000


@dataclass(frozen=True, slots=True)
class TimingSpan:
    label: str
    category: str
    start_ms: float
    end_ms: float
    priority: int
    line_number: int
    job_id: str | None = None
    job_type: str | None = None
    status: str = "COMPLETE"
    detail: str | None = None
    backend_phase: str | None = None
    backend_invocation_id: str | None = None

    @property
    def duration_ms(self) -> float:
        return max(0.0, self.end_ms - self.start_ms)


@dataclass(frozen=True, slots=True)
class TimingCause:
    label: str
    category: str
    duration_ms: float
    share: float
    source_lines: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class JobTiming:
    job_id: str
    job_type: str
    queue_ms: float | None
    runtime_ms: float | None
    delivery_ms: float | None
    spans: tuple[TimingSpan, ...]
    telemetry: tuple[tuple[int, dict[str, Any]], ...]


@dataclass(frozen=True, slots=True)
class TimingReport:
    elapsed_ms: float
    system_processing_ms: float
    user_wait_ms: float
    unclassified_ms: float
    causes: tuple[TimingCause, ...]
    jobs: tuple[JobTiming, ...]
    notes: tuple[str, ...]


def _line_time(line: Any) -> float:
    return _timestamp_ms(line.event.timestamp)


def _span_from_end(
    *,
    line: Any,
    label: str,
    category: str,
    priority: int,
    start_line: Any | None,
    status: str,
    detail: str | None = None,
) -> TimingSpan:
    end = _line_time(line)
    duration = line.event.duration_ms
    end_phase = line.event.data.get("backend_phase")
    start_phase = (
        start_line.event.data.get("backend_phase")
        if start_line is not None
        else None
    )
    end_invocation_id = line.event.data.get("backend_invocation_id")
    start_invocation_id = (
        start_line.event.data.get("backend_invocation_id")
        if start_line is not None
        else None
    )
    start = (
        end - float(duration)
        if duration is not None
        else (_line_time(start_line) if start_line is not None else end)
    )
    return TimingSpan(
        label=label,
        category=category,
        start_ms=start,
        end_ms=end,
        priority=priority,
        line_number=(
            start_line.line_number if start_line is not None else line.line_number
        ),
        job_id=line.event.job_id,
        job_type=line.event.job_type,
        status=status,
        detail=detail,
        backend_phase=(
            end_phase
            if isinstance(end_phase, str)
            else start_phase
            if isinstance(start_phase, str)
            else None
        ),
        backend_invocation_id=(
            end_invocation_id
            if isinstance(end_invocation_id, str)
            else start_invocation_id
            if isinstance(start_invocation_id, str)
            else None
        ),
    )


def _job_label(job_type: str | None, suffix: str) -> str:
    return f"{job_type or 'UNKNOWN'} / {suffix}"


def _stage_label(job_type: str | None, stage: str) -> str:
    titles = {
        "BACKEND_START": "Agent 启动",
        "BACKEND_EXECUTE": "Agent 后端执行",
        "TOOL_EXECUTE": "工具执行",
        "CONTEXT_BUILD": "上下文构建",
        "WORKSPACE_PREPARE": "工作区准备",
        "OUTCOME_VALIDATE": "结果校验",
        "ASSET_RESOLUTION": "资产解析",
        "RESOURCE_STAGE": "资源暂存",
        "EXECUTION_RECORD": "执行记录",
    }
    return _job_label(job_type, titles.get(stage, stage))


def _clip(span: TimingSpan, start: float, end: float) -> TimingSpan | None:
    clipped_start = max(start, span.start_ms)
    clipped_end = min(end, span.end_ms)
    if clipped_end <= clipped_start:
        return None
    return TimingSpan(
        label=span.label,
        category=span.category,
        start_ms=clipped_start,
        end_ms=clipped_end,
        priority=span.priority,
        line_number=span.line_number,
        job_id=span.job_id,
        job_type=span.job_type,
        status=span.status,
        detail=span.detail,
        backend_phase=span.backend_phase,
        backend_invocation_id=span.backend_invocation_id,
    )


def _marker_span(
    start_line: Any | None,
    end_line: Any | None,
    *,
    as_of: float,
    label: str,
    category: str,
    priority: int,
    job_id: str,
    job_type: str,
) -> TimingSpan | None:
    if start_line is None:
        return None
    start = _line_time(start_line)
    end = as_of if end_line is None else _line_time(end_line)
    if end < start:
        end = start
    return TimingSpan(
        label=label,
        category=category,
        start_ms=start,
        end_ms=end,
        priority=priority,
        line_number=start_line.line_number,
        job_id=job_id,
        job_type=job_type,
        status="IN_PROGRESS" if end_line is None else "COMPLETE",
    )


def _collect_wait_spans(lines: tuple[Any, ...], as_of: float) -> list[TimingSpan]:
    result: list[TimingSpan] = []
    active: tuple[str, Any] | None = None
    for line in lines:
        event = line.event
        if active is not None and event.event in _WAIT_END_EVENTS:
            status, started = active
            result.append(
                TimingSpan(
                    label=f"用户等待（{status}）",
                    category="user_wait",
                    start_ms=_line_time(started),
                    end_ms=_line_time(line),
                    priority=100,
                    line_number=started.line_number,
                    status="COMPLETE",
                )
            )
            active = None
        if event.event != "case.status.changed":
            continue
        to_status = event.data.get("to_status")
        if active is not None and to_status != active[0]:
            status, started = active
            result.append(
                TimingSpan(
                    label=f"用户等待（{status}）",
                    category="user_wait",
                    start_ms=_line_time(started),
                    end_ms=_line_time(line),
                    priority=100,
                    line_number=started.line_number,
                )
            )
            active = None
        if isinstance(to_status, str) and to_status in _WAIT_STATUSES:
            active = (to_status, line)
    if active is not None:
        status, started = active
        result.append(
            TimingSpan(
                label=f"用户等待（{status}）",
                category="user_wait",
                start_ms=_line_time(started),
                end_ms=as_of,
                priority=100,
                line_number=started.line_number,
                status="IN_PROGRESS",
            )
        )
    return result


def _collect_stage_spans(lines: tuple[Any, ...], as_of: float) -> list[TimingSpan]:
    starts: dict[tuple[str | None, str], list[Any]] = defaultdict(list)
    result: list[TimingSpan] = []
    for line in lines:
        event = line.event
        if event.event not in {
            "job.stage.started",
            "job.stage.completed",
            "job.stage.failed",
        }:
            continue
        stage = event.data.get("stage")
        if not isinstance(stage, str) or not stage:
            continue
        key = (event.job_id, stage)
        if event.event == "job.stage.started":
            starts[key].append(line)
            continue
        started = starts[key].pop() if starts[key] else None
        result.append(
            _span_from_end(
                line=line,
                label=_stage_label(event.job_type, stage),
                category="stage",
                priority=40,
                start_line=started,
                status="FAILED" if event.event == "job.stage.failed" else "COMPLETE",
                detail=stage,
            )
        )
    for (_job_id, stage), pending in starts.items():
        for started in pending:
            result.append(
                TimingSpan(
                    label=_stage_label(started.event.job_type, stage),
                    category="stage",
                    start_ms=_line_time(started),
                    end_ms=as_of,
                    priority=40,
                    line_number=started.line_number,
                    job_id=started.event.job_id,
                    job_type=started.event.job_type,
                    status="IN_PROGRESS",
                    detail=stage,
                    backend_phase=(
                        started.event.data.get("backend_phase")
                        if isinstance(started.event.data.get("backend_phase"), str)
                        else None
                    ),
                    backend_invocation_id=(
                        started.event.data.get("backend_invocation_id")
                        if isinstance(
                            started.event.data.get("backend_invocation_id"), str
                        )
                        else None
                    ),
                )
            )

    # More deeply nested stages win each timeline slice over their parents.
    adjusted: list[TimingSpan] = []
    for span in result:
        depth = sum(
            1
            for other in result
            if other is not span
            and other.job_id == span.job_id
            and other.start_ms <= span.start_ms
            and other.end_ms >= span.end_ms
            and (other.start_ms < span.start_ms or other.end_ms > span.end_ms)
        )
        adjusted.append(
            TimingSpan(
                label=span.label,
                category=span.category,
                start_ms=span.start_ms,
                end_ms=span.end_ms,
                priority=span.priority + depth,
                line_number=span.line_number,
                job_id=span.job_id,
                job_type=span.job_type,
                status=span.status,
                detail=span.detail,
                backend_phase=span.backend_phase,
                backend_invocation_id=span.backend_invocation_id,
            )
        )
    return adjusted


def _collect_logparse_spans(lines: tuple[Any, ...]) -> list[TimingSpan]:
    starts: dict[tuple[str | None, str], list[Any]] = defaultdict(list)
    result: list[TimingSpan] = []
    for line in lines:
        event = line.event
        operation = event.data.get("operation")
        if not isinstance(operation, str):
            continue
        if event.event == "job.logparse.operation.started":
            starts[(event.job_id, operation)].append(line)
            continue
        if event.event in {
            "job.logparse.operation.completed",
            "job.logparse.operation.failed",
        }:
            key = (event.job_id, operation)
            started = starts[key].pop() if starts[key] else None
            result.append(
                _span_from_end(
                    line=line,
                    label=_job_label(event.job_type, f"Logparse {operation}"),
                    category="logparse_operation",
                    priority=60,
                    start_line=started,
                    status=("FAILED" if event.event.endswith("failed") else "COMPLETE"),
                    detail=operation,
                )
            )
            continue
        if event.event not in {
            "job.logparse.phase.completed",
            "job.logparse.phase.failed",
        }:
            continue
        phase = event.data.get("phase")
        ordinal = event.data.get("ordinal")
        if not isinstance(phase, str):
            continue
        result.append(
            _span_from_end(
                line=line,
                label=_job_label(
                    event.job_type,
                    f"Logparse {operation} / {phase}#{ordinal}",
                ),
                category="logparse_phase",
                priority=70,
                start_line=None,
                status="FAILED" if event.event.endswith("failed") else "COMPLETE",
                detail=f"{operation}:{phase}:{ordinal}",
            )
        )
    return result


def _collect_job_spans(
    lines: tuple[Any, ...],
    as_of: float,
) -> tuple[list[TimingSpan], dict[str, dict[str, Any]]]:
    state: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "job_type": "UNKNOWN",
            "pending": None,
            "queued": None,
            "claimed": None,
            "produced": None,
            "applied": None,
            "failure": None,
            "telemetry": [],
        }
    )
    for line in lines:
        event = line.event
        if event.job_id is None:
            continue
        item = state[event.job_id]
        if event.job_type is not None:
            item["job_type"] = event.job_type
        markers = {
            "job.pending_persisted": "pending",
            "job.queued": "queued",
            "job.claimed": "claimed",
            "job.outcome.produced": "produced",
            "job.outcome.applied": "applied",
            "job.outcome.rejected": "applied",
            "job.outcome.stale": "applied",
            "job.execution.failure_applied": "failure",
        }
        marker = markers.get(event.event)
        if marker is not None and item[marker] is None:
            item[marker] = line
        if event.event == "job.backend.telemetry":
            item["telemetry"].append((line.line_number, dict(event.data)))

    spans: list[TimingSpan] = []
    for job_id, item in state.items():
        job_type = item["job_type"]
        queue_start = item["pending"] or item["queued"]
        queue = _marker_span(
            queue_start,
            item["claimed"],
            as_of=as_of,
            label=_job_label(job_type, "排队"),
            category="queue",
            priority=20,
            job_id=job_id,
            job_type=job_type,
        )
        runtime_end = item["produced"] or item["failure"]
        runtime = _marker_span(
            item["claimed"],
            runtime_end,
            as_of=as_of,
            label=_job_label(job_type, "Job 执行未分类"),
            category="runtime",
            priority=10,
            job_id=job_id,
            job_type=job_type,
        )
        delivery = _marker_span(
            item["produced"],
            item["applied"],
            as_of=as_of,
            label=_job_label(job_type, "Outcome 投递"),
            category="delivery",
            priority=20,
            job_id=job_id,
            job_type=job_type,
        )
        spans.extend(span for span in (queue, runtime, delivery) if span is not None)
    return spans, state


def _allocated_causes(
    spans: list[TimingSpan],
    *,
    case_start: float,
    case_end: float,
) -> tuple[tuple[TimingCause, ...], float, float, float]:
    clipped = [candidate for span in spans if (candidate := _clip(span, case_start, case_end))]
    boundaries = sorted({case_start, case_end, *(value for span in clipped for value in (span.start_ms, span.end_ms))})
    allocated: dict[tuple[str, str], float] = defaultdict(float)
    sources: dict[tuple[str, str], set[int]] = defaultdict(set)
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        if end <= start:
            continue
        active = [span for span in clipped if span.start_ms < end and span.end_ms > start]
        if not active:
            key = ("未归类时间", "unclassified")
            allocated[key] += end - start
            continue
        priority = max(span.priority for span in active)
        winners = [span for span in active if span.priority == priority]
        labels = sorted({span.label for span in winners})
        if len(labels) == 1:
            key = (labels[0], winners[0].category)
        else:
            key = ("并发处理（" + " / ".join(labels) + "）", "concurrent")
        allocated[key] += end - start
        sources[key].update(span.line_number for span in winners)
    elapsed = max(0.0, case_end - case_start)
    causes = tuple(
        TimingCause(
            label=label,
            category=category,
            duration_ms=duration,
            share=0.0 if elapsed == 0 else duration / elapsed,
            source_lines=tuple(sorted(sources[(label, category)])),
        )
        for (label, category), duration in sorted(
            allocated.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        )
        if duration > 0
    )
    user_wait = sum(item.duration_ms for item in causes if item.category == "user_wait")
    unclassified = sum(
        item.duration_ms for item in causes if item.category == "unclassified"
    )
    system = max(0.0, elapsed - user_wait - unclassified)
    return causes, system, user_wait, unclassified


def analyze_timing(lines: tuple[Any, ...]) -> TimingReport:
    """Build one wall-clock allocation while retaining nested evidence."""

    case_start = _line_time(lines[0])
    case_end = _line_time(lines[-1])
    as_of = case_end
    waits = _collect_wait_spans(lines, as_of)
    stages = _collect_stage_spans(lines, as_of)
    logparse = _collect_logparse_spans(lines)
    job_spans, job_state = _collect_job_spans(lines, as_of)
    all_spans = waits + stages + logparse + job_spans
    causes, system, user_wait, unclassified = _allocated_causes(
        all_spans,
        case_start=case_start,
        case_end=case_end,
    )

    jobs: list[JobTiming] = []
    for job_id, item in sorted(job_state.items()):
        owned = tuple(
            sorted(
                (span for span in all_spans if span.job_id == job_id),
                key=lambda span: (span.start_ms, span.end_ms, span.label),
            )
        )

        def duration(category: str) -> float | None:
            values = [span.duration_ms for span in owned if span.category == category]
            return sum(values) if values else None

        jobs.append(
            JobTiming(
                job_id=job_id,
                job_type=item["job_type"],
                queue_ms=duration("queue"),
                runtime_ms=duration("runtime"),
                delivery_ms=duration("delivery"),
                spans=owned,
                telemetry=tuple(item["telemetry"]),
            )
        )

    notes: list[str] = []
    if unclassified > 0:
        notes.append("未归类时间表示 Journey 没有足够的成对事件，未强行归入某个阶段。")
    if any(span.status != "COMPLETE" for span in all_spans):
        notes.append("进行中或失败区间按当前快照截止时间计算。")
    notes.append("thinking、text 与工具观察窗口是嵌套证据，不重复计入 Case 排名。")
    return TimingReport(
        elapsed_ms=max(0.0, case_end - case_start),
        system_processing_ms=system,
        user_wait_ms=user_wait,
        unclassified_ms=unclassified,
        causes=causes,
        jobs=tuple(jobs),
        notes=tuple(notes),
    )


__all__ = [
    "JobTiming",
    "TimingCause",
    "TimingReport",
    "TimingSpan",
    "analyze_timing",
]
