"""Server-owned deterministic Result Archive v3 builder and verifier."""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass
from typing import Iterable

from problem_locator.contracts import (
    EvidenceBinding,
    UserResultPayloadV3,
    canonical_json_bytes,
)
from problem_locator.integrations.logparse.paths import validate_relative_path
from problem_locator.runtime.authoritative_targets import (
    AuthoritativeTargetLog,
    semantic_archive_name,
)


_FORMAT_ID = "problem-locator-result-archive-v3"
_MAX_RESULT_TEXT_BYTES = 1_048_576
_MAX_TARGET_LOGS = 32
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_ZIP_EXTERNAL_ATTR = 0o100644 << 16


@dataclass(frozen=True, slots=True)
class ResultArchiveLog:
    """Exact raw bytes for one server-authoritative target in plan order."""

    target: AuthoritativeTargetLog
    content: bytes
    evidence_bindings: tuple[EvidenceBinding, ...] = ()


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = _ZIP_EXTERNAL_ATTR
    return info


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("result text contains an invalid value")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _enum_text(value: object) -> str:
    raw = getattr(value, "value", value)
    return _text(raw)


def _binding_text(binding: object) -> str:
    existing = getattr(binding, "existing_evidence_id", None)
    proposal = getattr(binding, "evidence_proposal_key", None)
    if existing is not None:
        return f"Evidence {existing}"
    if proposal is not None:
        return f"Evidence proposal {proposal}"
    raise ValueError("result contains an invalid Evidence binding")


def _citation_text(citation: object) -> str:
    parts = [_binding_text(getattr(citation, "evidence_binding"))]
    archive_name = getattr(citation, "archive_name", None)
    line_start = getattr(citation, "line_start", None)
    line_end = getattr(citation, "line_end", None)
    raw_digest = getattr(citation, "raw_bytes_sha256", None)
    excerpt = getattr(citation, "excerpt", None)
    if archive_name is not None:
        location = _text(archive_name)
        if line_start is not None:
            location += f":{line_start}-{line_end}"
        parts.append(location)
    if raw_digest is not None:
        parts.append(f"raw-sha256={raw_digest}")
    if excerpt is not None:
        parts.append(f"原文={_text(excerpt)}")
    return "；".join(parts)


def _numbered(items: Iterable[str], *, empty: str = "无。") -> list[str]:
    values = list(items)
    if not values:
        return [empty]
    return [f"{index}. {value}" for index, value in enumerate(values, start=1)]


def _target_log_rows(target_logs: tuple[ResultArchiveLog, ...]) -> list[str]:
    if not target_logs:
        return ["无（本次诊断未使用目标日志）。"]
    rows: list[str] = []
    for item in target_logs:
        target = item.target
        assert target.archive_name is not None
        digest = hashlib.sha256(item.content).hexdigest()
        cpu = "" if target.cpu_id is None else f"，CPU={target.cpu_id}"
        pid = "" if target.pid is None else f"，PID={target.pid}"
        caveats = "" if not target.caveats else f"，说明={'；'.join(target.caveats)}"
        bindings = (
            "无"
            if not item.evidence_bindings
            else "；".join(_binding_text(binding) for binding in item.evidence_bindings)
        )
        rows.append(
            f"{target.ordinal}. {target.archive_name}（label={target.label}，"
            f"module={target.module_name}，slot={target.slot}{cpu}，"
            f"process={target.process_name}{pid}，match={target.match_status}，"
            f"bytes={len(item.content)}，sha256={digest}，Evidence={bindings}{caveats}）"
        )
    return rows


def _validate_citation_log_bindings(
    report: UserResultPayloadV3,
    target_logs: tuple[ResultArchiveLog, ...],
) -> None:
    logs_by_name = {item.target.archive_name: item.content for item in target_logs}
    citations: list[object] = []
    citations.extend(
        citation for finding in report.findings for citation in finding.citations
    )
    citations.extend(
        citation
        for factor in (
            report.causal_factors
            + report.candidate_factors
            + report.excluded_factors
        )
        for citation in factor.citations
    )
    citations.extend(
        citation
        for rule in report.verification_rules
        for citation in rule.citations
    )
    citations.extend(report.time_relevance.citations)
    for citation in citations:
        name = citation.archive_name
        digest = citation.raw_bytes_sha256
        if name is None:
            if digest is not None:
                raise ValueError("a result citation hash has no target archive name")
            continue
        content = logs_by_name.get(name)
        if content is None:
            raise ValueError("a result citation names a non-authoritative target log")
        if citation.line_start is None or citation.line_end is None:
            raise ValueError("a target-log citation requires one bounded line range")
        physical = content.splitlines(keepends=True)
        if citation.line_end > len(physical):
            raise ValueError("a result citation line range exceeds its target log")
        raw_range = b"".join(
            physical[citation.line_start - 1 : citation.line_end]
        )
        if digest != hashlib.sha256(raw_range).hexdigest():
            raise ValueError("a result citation raw hash differs from cited line bytes")
        try:
            expected_excerpt = raw_range.rstrip(b"\r\n").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("a cited target log range is not UTF-8") from exc
        if citation.excerpt != expected_excerpt:
            raise ValueError("a result citation excerpt is not verbatim target log text")


def render_result_text(
    report: UserResultPayloadV3,
    *,
    target_logs: tuple[ResultArchiveLog, ...],
) -> str:
    """Render the fixed nine-section Chinese ``result.txt`` contract."""

    if not isinstance(report, UserResultPayloadV3):
        raise TypeError("report must be a UserResultPayloadV3")
    if report.status not in {"COMPLETED", "PARTIAL"}:
        raise ValueError("only a completed or partial user result may produce result.zip")
    _validate_citation_log_bindings(report, target_logs)

    supporting = "；".join(
        _binding_text(binding) for binding in report.supporting_evidence_bindings
    )
    findings: list[str] = []
    for finding in report.findings:
        citations = "；".join(_citation_text(item) for item in finding.citations)
        findings.append(
            f"{_text(finding.statement)}（置信度={finding.confidence:.3f}；"
            f"证据={citations or '无'}）"
        )
    factor_lines = [
        *(
            f"已确认[{_text(item.factor_id)}][{_enum_text(item.role)}] "
            f"{_text(item.statement)}"
            for item in report.causal_factors
        ),
        *(
            f"候选[{_text(item.factor_id)}][{_enum_text(item.role)}] "
            f"{_text(item.statement)}"
            for item in report.candidate_factors
        ),
        *(
            f"已排除[{_text(item.factor_id)}][{_enum_text(item.role)}] "
            f"{_text(item.statement)}"
            for item in report.excluded_factors
        ),
    ]
    criteria = [
        f"[{_enum_text(item.status)}] {_text(item.criterion)}；"
        f"说明={_text(item.explanation)}；证据="
        + "；".join(_binding_text(binding) for binding in item.evidence_bindings)
        for item in report.completion_criteria_mapping
    ]
    rules: list[str] = []
    for rule in report.verification_rules:
        details = [
            f"{_text(rule.rule_id)} / {_text(rule.rule_kind)}："
            f"{_enum_text(rule.status)}；说明={_text(rule.explanation)}"
        ]
        if rule.observed_times:
            details.append("观测时间=" + "、".join(rule.observed_times))
        if rule.event_observations:
            details.append(
                "事件计数="
                + "、".join(
                    f"{item.event_id}:{item.observed_count}"
                    + ("(下界)" if item.count_is_lower_bound else "")
                    for item in rule.event_observations
                )
            )
        if rule.derived_values:
            details.append(
                "派生值="
                + "、".join(
                    f"{item.name}={item.value} {item.unit or ''}"
                    f"[{item.lower_bound},{item.upper_bound}]"
                    for item in rule.derived_values
                )
            )
        if rule.citations:
            details.append(
                "日志原文=" + "；".join(_citation_text(item) for item in rule.citations)
            )
        if rule.issues:
            details.append("问题=" + "；".join(_text(item) for item in rule.issues))
        rules.append("；".join(details))

    time = report.time_relevance
    time_lines = [
        f"判断：{_text(time.assessment)}",
        f"问题时间：{time.problem_time or '无'}",
        f"派生锚点时间：{time.derived_anchor_time or '无'}",
    ]
    time_lines.extend(
        f"观测：{_text(item.rule_id)} @ {item.event_time}，相对问题时间偏移 "
        f"{item.offset_ms} ms"
        for item in time.observations
    )
    if not time.observations:
        time_lines.append("观测：无。")
    time_lines.append(f"说明：{_text(time.explanation)}")
    if time.citations:
        time_lines.append(
            "证据：" + "；".join(_citation_text(item) for item in time.citations)
        )

    lines = [
        "1. 定位结论",
        _text(report.root_cause or "已形成部分定位，但尚无可发布的完整根因。"),
        f"结果完整度：{_text(report.status)}",
        f"支持证据：{supporting}",
        "",
        "2. 问题描述",
        _text(report.problem_statement),
        f"来源任务：{_enum_text(report.source_job_type)}",
        "",
        "3. 关键分析依据",
        *_numbered(findings),
        "因素状态：",
        *_numbered(factor_lines),
        "",
        "4. 完成条件核对",
        *_numbered(criteria),
        "",
        "5. 服务端验证与日志原文",
        *_numbered(rules),
        "",
        "6. 时间相关性说明",
        *time_lines,
        "",
        "7. 证据缺口与限制",
        "证据缺口：",
        *_numbered((_text(item) for item in report.evidence_gaps)),
        "限制：",
        *_numbered((_text(item) for item in report.limitations)),
        "",
        "8. 处置建议",
        *_numbered((_text(item) for item in report.recommendations)),
        "安全说明：",
        *_numbered((_text(item) for item in report.safety_notes)),
        "",
        "9. 目标日志清单",
        *_target_log_rows(target_logs),
    ]
    result = "\n".join(lines) + "\n"
    if len(result.encode("utf-8")) > _MAX_RESULT_TEXT_BYTES:
        raise ValueError("rendered result.txt is too large")
    return result


def _validated_logs(
    target_logs: tuple[ResultArchiveLog, ...],
) -> tuple[ResultArchiveLog, ...]:
    if not isinstance(target_logs, tuple) or len(target_logs) > _MAX_TARGET_LOGS:
        raise ValueError("target logs must be one bounded immutable tuple")
    names: set[str] = {"result.txt", "archive-manifest.json"}
    paths: set[str] = set()
    source: tuple[str, str, str] | None = None
    for expected_ordinal, item in enumerate(target_logs, start=1):
        if not isinstance(item, ResultArchiveLog) or not isinstance(item.content, bytes):
            raise TypeError("each target log must bind authoritative metadata to bytes")
        if not isinstance(item.evidence_bindings, tuple) or any(
            not isinstance(binding, EvidenceBinding)
            for binding in item.evidence_bindings
        ):
            raise TypeError("target evidence bindings must be an immutable EvidenceBinding tuple")
        binding_keys = [
            binding.existing_evidence_id
            or f"proposal:{binding.evidence_proposal_key}"
            for binding in item.evidence_bindings
        ]
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("target evidence bindings contain duplicates")
        target = item.target
        expected_source_root = (
            f"inputs/artifacts/{target.source_ref}/tree"
            if target.source_kind == "INPUT_ARTIFACT"
            else f"output/proposals/{target.source_ref}/tree"
        )
        if (
            not isinstance(target, AuthoritativeTargetLog)
            or not target.deliverable
            or target.ordinal != expected_ordinal
            or target.log_path is None
            or target.archive_name is None
            or target.archive_name != semantic_archive_name(target)
            or target.source_kind not in {"INPUT_ARTIFACT", "OUTPUT_PROPOSAL"}
            or not target.source_ref
            or "/" in target.source_ref
            or "\\" in target.source_ref
            or target.source_root != expected_source_root
            or validate_relative_path(target.source_root).as_posix()
            != target.source_root
            or validate_relative_path(target.log_path).as_posix() != target.log_path
            or "/" in target.archive_name
            or "\\" in target.archive_name
            or not target.archive_name.endswith(".log")
        ):
            raise ValueError("target logs do not exactly follow deliverable plan order")
        name_key = target.archive_name.casefold()
        path = target.workspace_relative_path
        assert path is not None
        path_key = path.casefold()
        if name_key in names:
            raise ValueError("target archive names collide case-insensitively")
        if path_key in paths:
            raise ValueError("one target source path is repeated")
        names.add(name_key)
        paths.add(path_key)
        item_source = (target.source_kind, target.source_ref, target.source_root)
        if source is None:
            source = item_source
        elif source != item_source:
            raise ValueError("target logs do not come from one authoritative LOGPARSE_RUN")
    return target_logs


def _archive_manifest(
    report: UserResultPayloadV3,
    *,
    problem_time: str | None,
    result_bytes: bytes,
    target_logs: tuple[ResultArchiveLog, ...],
) -> bytes:
    logs = []
    for item in target_logs:
        target = item.target
        assert target.archive_name is not None
        logs.append(
            {
                "ordinal": target.ordinal,
                "archive_name": target.archive_name,
                "label": target.label,
                "requested_module": target.requested_module,
                "module_key": target.module_key,
                "module_name": target.module_name,
                "slot": target.slot,
                "cpu_id": target.cpu_id,
                "process_name": target.process_name,
                "pid": target.pid,
                "match_status": target.match_status,
                "caveats": list(target.caveats),
                "evidence_bindings": [
                    binding.model_dump(mode="json")
                    for binding in item.evidence_bindings
                ],
                "size": len(item.content),
                "sha256": hashlib.sha256(item.content).hexdigest(),
            }
        )
    return canonical_json_bytes(
        {
            "schema_version": 3,
            "format_id": _FORMAT_ID,
            "problem_time": problem_time,
            "diagnosis_result_sha256": hashlib.sha256(
                canonical_json_bytes(report)
            ).hexdigest(),
            "result_txt_sha256": hashlib.sha256(result_bytes).hexdigest(),
            "target_log_count": len(logs),
            "target_logs": logs,
        }
    )


def build_result_archive(
    report: UserResultPayloadV3,
    *,
    problem_time: str | None,
    target_logs: tuple[ResultArchiveLog, ...],
) -> bytes:
    """Build canonical v3 bytes in result/manifest/plan-log order."""

    logs = _validated_logs(target_logs)
    if logs and not problem_time:
        raise ValueError("Logparse-backed result.zip requires problem_time")
    result_bytes = render_result_text(report, target_logs=logs).encode("utf-8")
    manifest_bytes = _archive_manifest(
        report,
        problem_time=problem_time,
        result_bytes=result_bytes,
        target_logs=logs,
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(
        stream,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=False,
        strict_timestamps=True,
    ) as archive:
        archive.writestr(_zip_info("result.txt"), result_bytes)
        archive.writestr(_zip_info("archive-manifest.json"), manifest_bytes)
        for item in logs:
            assert item.target.archive_name is not None
            archive.writestr(_zip_info(item.target.archive_name), item.content)
    return stream.getvalue()


def validate_result_archive_bytes(
    archive_bytes: bytes,
    *,
    report: UserResultPayloadV3,
    problem_time: str | None,
    target_logs: tuple[ResultArchiveLog, ...],
) -> str:
    """Recompute and validate every v3 entry, byte, order, and ZIP attribute."""

    if not isinstance(archive_bytes, bytes):
        raise TypeError("archive_bytes must be bytes")
    logs = _validated_logs(target_logs)
    expected_result = render_result_text(report, target_logs=logs).encode("utf-8")
    expected_manifest = _archive_manifest(
        report,
        problem_time=problem_time,
        result_bytes=expected_result,
        target_logs=logs,
    )
    expected_names = [
        "result.txt",
        "archive-manifest.json",
        *(item.target.archive_name for item in logs),
    ]
    expected_payloads = [
        expected_result,
        expected_manifest,
        *(item.content for item in logs),
    ]
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if (
                names != expected_names
                or len(names) != len(set(name.casefold() for name in names))
                or archive.comment != b""
            ):
                raise ValueError("result archive entries or order are invalid")
            for info, expected in zip(infos, expected_payloads, strict=True):
                expected_flag_bits = 0 if info.filename.isascii() else 0x800
                if (
                    info.is_dir()
                    or "/" in info.filename
                    or "\\" in info.filename
                    or info.date_time != _ZIP_TIMESTAMP
                    or info.compress_type != zipfile.ZIP_DEFLATED
                    or info.flag_bits != expected_flag_bits
                    or info.create_system != 3
                    or info.external_attr != _ZIP_EXTERNAL_ATTR
                    or info.extra != b""
                    or info.comment != b""
                    or info.file_size != len(expected)
                    or archive.read(info) != expected
                ):
                    raise ValueError("result archive metadata or entry bytes are invalid")
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as exc:
        raise ValueError("result archive is not a valid canonical ZIP") from exc
    expected_archive = build_result_archive(
        report,
        problem_time=problem_time,
        target_logs=logs,
    )
    if archive_bytes != expected_archive:
        raise ValueError("result archive bytes are not the canonical v2 encoding")
    return expected_result.decode("utf-8")


__all__ = [
    "ResultArchiveLog",
    "build_result_archive",
    "render_result_text",
    "validate_result_archive_bytes",
]
