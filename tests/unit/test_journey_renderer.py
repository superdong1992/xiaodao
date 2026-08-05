from __future__ import annotations

from pathlib import Path

import pytest

from problem_locator.contracts.serialization import canonical_json_bytes
from problem_locator.journey import JourneyEvent
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
) -> JourneyEvent:
    return JourneyEvent.model_validate(
        {
            "schema_version": 1,
            "sequence": sequence,
            "timestamp": f"2026-08-05T08:00:{sequence:02d}.000Z",
            "level": "INFO",
            "event": event,
            "correlation_id": "correlation-1",
            "request_id": "request-1",
            "case_id": case_id,
            "job_id": job_id,
            "job_type": job_type,
            "outcome_id": outcome_id,
            "duration_ms": None,
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
