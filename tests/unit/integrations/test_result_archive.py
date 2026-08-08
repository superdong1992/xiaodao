from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import replace

import pytest

from problem_locator.contracts import (
    EvidenceBinding,
    UserResultPayloadV2,
    parse_canonical_json_bytes,
)
from problem_locator.integrations.result_archive import (
    ResultArchiveLog,
    build_result_archive,
    render_result_text,
    validate_result_archive_bytes,
)
from problem_locator.runtime.authoritative_targets import AuthoritativeTargetLog
from problem_locator.runtime.result_types import CapturedTargetLog
from problem_locator.runtime.user_results import _result_archive_logs


EVIDENCE_ID = "00000000-0000-0000-0000-000000000041"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000060"
PROBLEM_TIME = "2026-07-31T00:00:00.000Z"


def _binding() -> EvidenceBinding:
    return EvidenceBinding(
        existing_evidence_id=EVIDENCE_ID,
        evidence_proposal_key=None,
    )


def _target(
    ordinal: int,
    *,
    label: str,
    module: str,
    process: str,
    path: str,
    cpu_id: str | None = None,
) -> AuthoritativeTargetLog:
    cpu = "" if cpu_id is None else f"__cpu_{cpu_id}"
    return AuthoritativeTargetLog(
        ordinal=ordinal,
        label=label,
        requested_module=module,
        requested_slot="request",
        requested_process_name=process,
        requested_pid=None,
        module_key=module,
        module_name=module,
        slot="request",
        process_name=process,
        pid=None,
        match_status="exact",
        board_cycle=None,
        cpu_id=cpu_id,
        cpu_cycle=None,
        caveats=(),
        source_kind="INPUT_ARTIFACT",
        source_ref=ARTIFACT_ID,
        source_root=f"inputs/artifacts/{ARTIFACT_ID}/tree",
        log_path=path,
        archive_name=(
            f"{label}__{module}__slot_request{cpu}__{process}.log"
        ),
    )


def _logs() -> tuple[ResultArchiveLog, ResultArchiveLog]:
    first = ResultArchiveLog(
        target=_target(
            1,
            label="caller",
            module="payment",
            process="payment-service",
            path="task/logs/caller.log",
        ),
        content=b"[0001] caller timeout\r\n[0002] retry\n",
        evidence_bindings=(_binding(),),
    )
    # The second plan anchor is intentionally not used by any Candidate
    # citation.  It must still be delivered because plan order owns scope.
    second = ResultArchiveLog(
        target=_target(
            2,
            label="server",
            module="inventory",
            process="inventory-service",
            path="task/logs/server.log",
            cpu_id="1",
        ),
        content=b"[0001] server takeover\n",
        evidence_bindings=(),
    )
    return first, second


def _report(
    first_log: ResultArchiveLog | None,
    *,
    raw_digest: str | None = None,
    excerpt: str | None = None,
) -> UserResultPayloadV2:
    binding = _binding().model_dump(mode="json")
    if first_log is None:
        citation = {
            "evidence_binding": binding,
            "archive_name": None,
            "line_start": None,
            "line_end": None,
            "raw_bytes_sha256": None,
            "excerpt": None,
        }
        problem_time = None
        observations: list[dict[str, object]] = []
    else:
        raw_line = first_log.content.splitlines(keepends=True)[0]
        citation = {
            "evidence_binding": binding,
            "archive_name": first_log.target.archive_name,
            "line_start": 1,
            "line_end": 1,
            "raw_bytes_sha256": raw_digest
            or hashlib.sha256(raw_line).hexdigest(),
            "excerpt": excerpt
            if excerpt is not None
            else raw_line.rstrip(b"\r\n").decode("utf-8"),
        }
        problem_time = PROBLEM_TIME
        observations = [
            {
                "rule_id": "timeout_window",
                "event_time": PROBLEM_TIME,
                "offset_ms": 0,
            }
        ]
    return UserResultPayloadV2.model_validate(
        {
            "schema_version": 2,
            "format_id": "problem-locator-diagnosis-v2",
            "status": "COMPLETED",
            "source_job_type": "DIAGNOSE",
            "problem_statement": "支付调用库存服务超时。",
            "root_cause": "库存服务接管期间请求超过截止时间。",
            "findings": [
                {
                    "statement": "调用方记录到明确超时事件。",
                    "confidence": 0.95,
                    "evidence_bindings": [binding],
                    "citations": [citation],
                }
            ],
            "supporting_evidence_bindings": [binding],
            "completion_criteria_mapping": [
                {
                    "criterion_index": 0,
                    "criterion": "确认超时请求。",
                    "satisfied": True,
                    "evidence_bindings": [binding],
                    "explanation": "目标日志包含超时原文。",
                }
            ],
            "verification_rules": [
                {
                    "rule_id": "timeout_present",
                    "rule_kind": "EVENT_PRESENT",
                    "status": "VERIFIED_PASS",
                    "explanation": "服务端逐行扫描命中一次。",
                    "evidence_bindings": [binding],
                    "citations": [citation],
                    "observed_times": [PROBLEM_TIME] if problem_time else [],
                    "issues": [],
                }
            ],
            "time_relevance": {
                "assessment": "RELEVANT" if problem_time else "UNKNOWN",
                "problem_time": problem_time,
                "derived_anchor_time": problem_time,
                "observations": observations,
                "explanation": "事件与问题时间重合。" if problem_time else "没有日志时间。",
                "citations": [citation],
            },
            "evidence_gaps": ["未取得下游线程栈。"],
            "limitations": ["结论仅覆盖已声明完成条件。"],
            "recommendations": ["检查接管窗口并增加超时保护。"],
        }
    )


def test_result_archive_v2_is_deterministic_and_uses_plan_order() -> None:
    logs = _logs()
    report = _report(logs[0])

    first = build_result_archive(
        report,
        problem_time=PROBLEM_TIME,
        target_logs=logs,
    )
    second = build_result_archive(
        report,
        problem_time=PROBLEM_TIME,
        target_logs=logs,
    )

    assert first == second
    assert validate_result_archive_bytes(
        first,
        report=report,
        problem_time=PROBLEM_TIME,
        target_logs=logs,
    ) == render_result_text(report, target_logs=logs)
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == [
            "result.txt",
            "archive-manifest.json",
            "caller__payment__slot_request__payment-service.log",
            "server__inventory__slot_request__cpu_1__inventory-service.log",
        ]
        manifest_bytes = archive.read("archive-manifest.json")
        manifest = parse_canonical_json_bytes(manifest_bytes)
        assert manifest["schema_version"] == 2
        assert manifest["format_id"] == "problem-locator-result-archive-v2"
        assert manifest["target_log_count"] == 2
        assert [item["ordinal"] for item in manifest["target_logs"]] == [1, 2]
        assert manifest["target_logs"][0]["evidence_bindings"] == [
            _binding().model_dump(mode="json")
        ]
        assert manifest["target_logs"][1]["evidence_bindings"] == []
        assert "bytes" not in manifest["target_logs"][0]
        assert manifest["target_logs"][0]["size"] == len(logs[0].content)
        assert "diagnosis_result_sha256" in manifest
        assert "result_txt_sha256" in manifest
        assert "user_result_sha256" not in manifest
        assert "result_txt" not in manifest


def test_archive_manifest_annotations_exclude_unused_evidence() -> None:
    logs = _logs()
    used = _binding()
    unused = EvidenceBinding(
        existing_evidence_id="00000000-0000-0000-0000-000000000099",
        evidence_proposal_key=None,
    )
    captured = (
        CapturedTargetLog(
            target=logs[0].target,
            content=logs[0].content,
            evidence_bindings=(used, unused),
        ),
        CapturedTargetLog(
            target=logs[1].target,
            content=logs[1].content,
            evidence_bindings=(unused,),
        ),
    )

    annotated = _result_archive_logs(captured, _report(logs[0]))

    assert annotated[0].evidence_bindings == (used,)
    assert annotated[1].evidence_bindings == ()


def test_archive_preserves_safe_unicode_semantic_filename() -> None:
    logs = _logs()
    first = replace(
        logs[0],
        target=replace(
            logs[0].target,
            module_name="支付模块",
            archive_name="caller__支付模块__slot_request__payment-service.log",
        ),
    )
    unicode_logs = (first, logs[1])
    report = _report(first)

    data = build_result_archive(
        report,
        problem_time=PROBLEM_TIME,
        target_logs=unicode_logs,
    )

    assert validate_result_archive_bytes(
        data,
        report=report,
        problem_time=PROBLEM_TIME,
        target_logs=unicode_logs,
    )
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist()[2] == (
            "caller__支付模块__slot_request__payment-service.log"
        )


def test_result_text_uses_the_locked_nine_chinese_sections() -> None:
    logs = _logs()
    text = render_result_text(_report(logs[0]), target_logs=logs)
    headers = [
        "1. 定位结论",
        "2. 问题描述",
        "3. 关键分析依据",
        "4. 完成条件核对",
        "5. 服务端验证与日志原文",
        "6. 时间相关性说明",
        "7. 证据缺口与限制",
        "8. 处置建议",
        "9. 目标日志清单",
    ]
    assert [text.index(header) for header in headers] == sorted(
        text.index(header) for header in headers
    )
    assert "库存服务接管期间请求超过截止时间" in text
    assert "[0001] caller timeout" in text
    assert logs[1].target.archive_name in text
    assert "Evidence=无" in text
    assert text.endswith("\n") and "\r" not in text


def test_empty_target_set_builds_a_canonical_two_entry_archive() -> None:
    report = _report(None)
    data = build_result_archive(report, problem_time=None, target_logs=())

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["result.txt", "archive-manifest.json"]
        manifest = parse_canonical_json_bytes(archive.read("archive-manifest.json"))
        assert manifest["problem_time"] is None
        assert manifest["target_log_count"] == 0
        assert manifest["target_logs"] == []
    assert validate_result_archive_bytes(
        data,
        report=report,
        problem_time=None,
        target_logs=(),
    ).startswith("1. 定位结论\n")


@pytest.mark.parametrize(
    ("raw_digest", "excerpt", "message"),
    [
        ("0" * 64, None, "raw hash differs"),
        (None, "改写后的文本", "not verbatim"),
    ],
)
def test_citation_must_bind_verbatim_raw_line_bytes(
    raw_digest: str | None,
    excerpt: str | None,
    message: str,
) -> None:
    logs = _logs()
    report = _report(logs[0], raw_digest=raw_digest, excerpt=excerpt)
    with pytest.raises(ValueError, match=message):
        build_result_archive(
            report,
            problem_time=PROBLEM_TIME,
            target_logs=logs,
        )


def test_archive_rejects_forged_generic_or_out_of_order_target_names() -> None:
    logs = _logs()
    report = _report(logs[0])
    forged = (
        replace(logs[0], target=replace(logs[0].target, archive_name="target-log-001.log")),
        logs[1],
    )
    with pytest.raises(ValueError, match="plan order"):
        build_result_archive(
            report,
            problem_time=PROBLEM_TIME,
            target_logs=forged,
        )
    with pytest.raises(ValueError, match="plan order"):
        build_result_archive(
            report,
            problem_time=PROBLEM_TIME,
            target_logs=tuple(reversed(logs)),
        )


def test_archive_rejects_a_forged_logparse_source_root() -> None:
    logs = _logs()
    report = _report(logs[0])
    forged = (
        replace(
            logs[0],
            target=replace(
                logs[0].target,
                source_root="inputs/attachments/upload",
            ),
        ),
        logs[1],
    )
    with pytest.raises(ValueError, match="plan order"):
        build_result_archive(
            report,
            problem_time=PROBLEM_TIME,
            target_logs=forged,
        )


def test_validator_rejects_any_target_byte_drift() -> None:
    logs = _logs()
    report = _report(logs[0])
    data = build_result_archive(
        report,
        problem_time=PROBLEM_TIME,
        target_logs=logs,
    )
    drifted = (logs[0], replace(logs[1], content=logs[1].content + b"drift\n"))
    with pytest.raises(ValueError, match="entry bytes|canonical v2"):
        validate_result_archive_bytes(
            data,
            report=report,
            problem_time=PROBLEM_TIME,
            target_logs=drifted,
        )


def test_inconclusive_result_never_builds_result_zip() -> None:
    logs = _logs()
    value = _report(logs[0]).model_dump(mode="json")
    value.update(
        status="INCONCLUSIVE",
        root_cause=None,
        findings=[],
        evidence_gaps=["根因证据不足。"],
    )
    report = UserResultPayloadV2.model_validate(value)
    with pytest.raises(ValueError, match="only a COMPLETED"):
        build_result_archive(
            report,
            problem_time=PROBLEM_TIME,
            target_logs=logs,
        )
