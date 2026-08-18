from __future__ import annotations

from pathlib import Path

import pytest

from problem_locator.contracts.serialization import canonical_json_bytes
from problem_locator.journey import JourneyEvent
from problem_locator.journey_timing import analyze_timing
from problem_locator.journey_renderer import (
    JourneyCaseNotFound,
    JourneySourceError,
    load_journey,
    render_journey,
)


CASE_ID = "00000000-0000-4000-8000-000000000101"
OTHER_CASE_ID = "00000000-0000-4000-8000-000000000102"
JOB_ID = "00000000-0000-4000-8000-000000000201"
OUTCOME_ID = "00000000-0000-4000-8000-000000000301"


def _event(
    sequence: int,
    event: str,
    *,
    case_id: str | None = CASE_ID,
    job_id: str | None = None,
    job_type: str | None = None,
    outcome_id: str | None = None,
    data: dict | None = None,
    timestamp: str | None = None,
    duration_ms: float | None = None,
) -> JourneyEvent:
    return JourneyEvent.model_validate(
        {
            "schema_version": 1,
            "sequence": sequence,
            "timestamp": timestamp or f"2026-08-05T08:00:{sequence:02d}.000Z",
            "level": "INFO",
            "event": event,
            "correlation_id": "correlation-1",
            "request_id": "request-1",
            "case_id": case_id,
            "job_id": job_id,
            "job_type": job_type,
            "outcome_id": outcome_id,
            "duration_ms": duration_ms,
            "data": {} if data is None else data,
        },
        strict=True,
    )


def _write(log_dir: Path, events: list[JourneyEvent]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "journey.jsonl").write_bytes(
        b"".join(canonical_json_bytes(event) for event in events)
    )


def test_render_journey_generates_detailed_and_nonterminal_brief_snapshots(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    _write(
        log_dir,
        [
            _event(
                1,
                "case.created",
                data={"problem_spec": {"statement": "库存 RPC 超时"}},
            ),
            _event(2, "case.created", case_id=OTHER_CASE_ID),
            _event(
                3,
                "job.outcome.applied",
                job_id=JOB_ID,
                job_type="DIAGNOSE",
                outcome_id=OUTCOME_ID,
                data={
                    "outcome": {
                        "result_type": "NEED_INPUT",
                        "payload": {
                            "candidate_conclusion_draft": None,
                        },
                    },
                    "case_view": {
                        "status": "WAITING_INPUT",
                        "pending_requirements": [
                            {"name": "order_id", "status": "OPEN"}
                        ],
                    },
                },
            ),
            _event(
                4,
                "case.status.changed",
                job_id=JOB_ID,
                job_type="DIAGNOSE",
                outcome_id=OUTCOME_ID,
                data={
                    "from_status": "RUNNING",
                    "to_status": "WAITING_INPUT",
                    "pending_requirements": [
                        {"name": "order_id", "status": "OPEN"}
                    ],
                },
            ),
        ],
    )

    receipt = render_journey(log_dir, CASE_ID)
    detailed = Path(receipt.detailed_log).read_text(encoding="utf-8")
    brief = Path(receipt.brief_log).read_text(encoding="utf-8")

    assert receipt.case_status == "WAITING_INPUT"
    assert not receipt.terminal
    assert receipt.events_rendered == 3
    assert "库存 RPC 超时" in detailed
    assert f"job_id: {JOB_ID}" in detailed
    assert "journey.jsonl:3" in detailed
    assert "当前快照（非最终结论）" in brief
    assert "order_id" in brief
    assert "不能作为最终根因结论" in brief
    assert OTHER_CASE_ID not in detailed
    assert detailed.encode("utf-8").endswith(b"\n")
    assert b"\r\n" not in detailed.encode("utf-8")

    first_detailed = Path(receipt.detailed_log).read_bytes()
    first_brief = Path(receipt.brief_log).read_bytes()
    repeated = render_journey(log_dir, CASE_ID)
    assert Path(repeated.detailed_log).read_bytes() == first_detailed
    assert Path(repeated.brief_log).read_bytes() == first_brief


def test_unknown_event_is_visible_without_becoming_a_business_milestone(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    _write(
        log_dir,
        [
            _event(1, "case.created", data={"problem_spec": {"statement": "问题"}}),
            _event(2, "future.semantic.event", data={"future": True}),
        ],
    )

    receipt = render_journey(log_dir, CASE_ID)
    detailed = Path(receipt.detailed_log).read_text(encoding="utf-8")
    brief = Path(receipt.brief_log).read_text(encoding="utf-8")

    assert receipt.unknown_event_count == 1
    assert "未知事件 [future.semantic.event]" in detailed
    assert "存在 1 个未识别事件" in brief
    assert "future.semantic.event" not in brief


@pytest.mark.parametrize(
    "invalid",
    [
        b'{"broken":}\n',
        b'{"schema_version":1}\n',
        canonical_json_bytes(_event(2, "case.created")),
        canonical_json_bytes(_event(1, "case.created"))[:-1],
        canonical_json_bytes(_event(1, "case.created")).replace(b"\n", b"\r\n"),
    ],
)
def test_invalid_source_fails_before_existing_outputs_are_replaced(
    tmp_path: Path,
    invalid: bytes,
) -> None:
    log_dir = tmp_path / "logs"
    output_dir = log_dir / "cases" / CASE_ID
    output_dir.mkdir(parents=True)
    detailed = output_dir / "detailed.log"
    brief = output_dir / "brief.log"
    detailed.write_bytes(b"old detailed\n")
    brief.write_bytes(b"old brief\n")
    (log_dir / "journey.jsonl").write_bytes(invalid)

    with pytest.raises(JourneySourceError, match="journey.jsonl:1"):
        render_journey(log_dir, CASE_ID)

    assert detailed.read_bytes() == b"old detailed\n"
    assert brief.read_bytes() == b"old brief\n"


def test_missing_case_is_distinct_from_invalid_source(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _write(log_dir, [_event(1, "case.created", case_id=OTHER_CASE_ID)])

    with pytest.raises(JourneyCaseNotFound, match=CASE_ID):
        render_journey(log_dir, CASE_ID)


def test_loader_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    source = tmp_path / "journey.jsonl"
    payload = canonical_json_bytes(_event(1, "case.created")).replace(
        b'"schema_version":1',
        b'"schema_version":2',
    )
    source.write_bytes(payload)

    with pytest.raises(JourneySourceError, match="schema_version"):
        load_journey(source)


def test_timing_attribution_ranks_exclusive_critical_path_and_agent_detail(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    base = "2026-08-05T08:00:"
    telemetry = {
        "diagnosis_mode": "SPECIALIZED",
        "backend_status": "SUCCESS",
        "stream_status": "COMPLETE",
        "stream_reason": None,
        "content_included": False,
        "prompt_bytes": 120,
        "prompt_write_status": "COMPLETE",
        "prompt_write_ms": 5.0,
        "cli_duration_ms": 6500.0,
        "model_api_duration_ms": 5000.0,
        "turn_count": 3,
        "usage_counts": {"input": 100, "output": 20},
        "block_observations": {
            "thinking": {
                "block_count": 2,
                "utf8_bytes": 40,
                "observed_window_ms": 3000.0,
            },
            "text": {
                "block_count": 1,
                "utf8_bytes": 10,
                "observed_window_ms": 0.0,
            },
        },
        "tool_observed_union_ms": 2100.0,
        "tool_observations": [
            {
                "name": "Bash",
                "call_count": 2,
                "completed_count": 2,
                "observed_duration_ms": 2100.0,
                "max_call_ms": 1600.0,
            }
        ],
    }
    _write(
        log_dir,
        [
            _event(1, "case.created", timestamp=base + "00.000Z"),
            _event(2, "job.pending_persisted", job_id=JOB_ID, job_type="DIAGNOSE", timestamp=base + "01.000Z"),
            _event(3, "job.claimed", job_id=JOB_ID, job_type="DIAGNOSE", timestamp=base + "02.000Z"),
            _event(4, "job.stage.started", job_id=JOB_ID, job_type="DIAGNOSE", data={"stage": "TOOL_EXECUTE"}, timestamp=base + "02.000Z"),
            _event(5, "job.stage.started", job_id=JOB_ID, job_type="DIAGNOSE", data={"stage": "BACKEND_EXECUTE"}, timestamp=base + "03.000Z"),
            _event(6, "job.logparse.operation.started", job_id=JOB_ID, job_type="DIAGNOSE", data={"operation": "parse-targets"}, timestamp=base + "04.000Z"),
            _event(7, "job.logparse.phase.completed", job_id=JOB_ID, job_type="DIAGNOSE", data={"operation": "parse-targets", "phase": "PARSE", "ordinal": 1}, timestamp=base + "06.000Z", duration_ms=2000.0),
            _event(8, "job.logparse.operation.completed", job_id=JOB_ID, job_type="DIAGNOSE", data={"operation": "parse-targets"}, timestamp=base + "07.000Z", duration_ms=3000.0),
            _event(9, "job.stage.completed", job_id=JOB_ID, job_type="DIAGNOSE", data={"stage": "BACKEND_EXECUTE"}, timestamp=base + "10.000Z", duration_ms=7000.0),
            _event(10, "job.stage.completed", job_id=JOB_ID, job_type="DIAGNOSE", data={"stage": "TOOL_EXECUTE"}, timestamp=base + "10.000Z", duration_ms=8000.0),
            _event(11, "job.backend.telemetry", job_id=JOB_ID, job_type="DIAGNOSE", data=telemetry, timestamp=base + "10.100Z"),
            _event(12, "job.outcome.produced", job_id=JOB_ID, job_type="DIAGNOSE", timestamp=base + "11.000Z"),
            _event(13, "job.outcome.applied", job_id=JOB_ID, job_type="DIAGNOSE", timestamp=base + "12.000Z", data={"case_view": {"status": "RESOLVED"}}),
        ],
    )

    receipt = render_journey(log_dir, CASE_ID)
    brief = Path(receipt.brief_log).read_text(encoding="utf-8")
    detailed = Path(receipt.detailed_log).read_text(encoding="utf-8")

    assert "主要耗时来源（按 Case 关键路径排名，非异常判定）" in brief
    assert "DIAGNOSE / Agent 后端执行: 4.000 s (33.3%)" in brief
    assert "系统处理: 11.000 s (91.7%)" in brief
    assert "未归类: 1.000 s (8.3%)" in brief
    assert "Agent 细分: COMPLETE（1/1 次调用）" in brief
    assert "服务端 Backend: 7.000 s；CLI 报告总耗时: 6.500 s" in detailed
    assert "服务端包装开销: 500.000 ms" in detailed
    assert "CLI 非 API 时间: 1.500 s" in detailed
    assert "Logparse parse-targets / PARSE#1: 2.000 s" in detailed
    assert "工具观察（嵌套、不可与模型时间直接相加）" in detailed
    assert "内容记录: false" in detailed

    case_lines = tuple(
        line
        for line in load_journey(log_dir / "journey.jsonl")
        if line.event.case_id == CASE_ID
    )
    ranked = sum(cause.duration_ms for cause in analyze_timing(case_lines).causes)
    assert ranked == 12000.0


def test_unsupported_agent_output_keeps_base_timing_and_reason(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _write(
        log_dir,
        [
            _event(1, "case.created"),
            _event(2, "job.claimed", job_id=JOB_ID, job_type="DIAGNOSE"),
            _event(3, "job.stage.completed", job_id=JOB_ID, job_type="DIAGNOSE", data={"stage": "BACKEND_EXECUTE"}, duration_ms=900.0),
            _event(
                4,
                "job.backend.telemetry",
                job_id=JOB_ID,
                job_type="DIAGNOSE",
                data={
                    "diagnosis_mode": "GENERIC",
                    "stream_status": "UNAVAILABLE",
                    "stream_reason": "UNSUPPORTED_STREAM_JSON",
                    "content_included": False,
                },
            ),
        ],
    )

    receipt = render_journey(log_dir, CASE_ID)
    brief = Path(receipt.brief_log).read_text(encoding="utf-8")
    detailed = Path(receipt.detailed_log).read_text(encoding="utf-8")

    assert "Agent 后端执行" in brief
    assert "UNSUPPORTED_STREAM_JSON" in brief
    assert "UNAVAILABLE / UNSUPPORTED_STREAM_JSON，模式=GENERIC" in detailed
