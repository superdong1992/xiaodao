from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from problem_locator.contracts import canonical_json_bytes
from problem_locator.domain.methods_state_v2 import (
    accept_specialist_evaluation_v2,
    record_protocol_error_v2,
    start_method_state_v2,
)
from problem_locator.runtime import methods_evidence_v2
from problem_locator.runtime.methods_evaluation_v2 import evaluate_method_role_v2
from problem_locator.runtime.methods_evidence_v2 import (
    build_method_evaluation_plan_v2,
    scan_method_evidence_v2,
)
from problem_locator.runtime.methods_grounding import FrozenTargetLogV1
from problem_locator.runtime.methods_records_v2 import (
    METHODS_STATE_V2_FILENAME,
    publish_method_evaluation_plan_v2,
    publish_method_evidence_graph_v2,
    publish_method_rejected_attempt_v2,
    publish_method_state_v2,
)
from problem_locator.runtime.methods_replay_v2 import (
    MethodValidationReplayErrorCodeV2,
    MethodValidationReplayErrorV2,
    replay_method_rejected_attempt_v2,
)
from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.execution_records import FileExecutionRecordStore
from tests.deterministic.unit.runtime.methods_v2_test_support import (
    load_test_methods_skill,
)


SOURCE_JOB_ID = "00000000-0000-0000-0000-000000000071"
EVALUATION_ID = "00000000-0000-0000-0000-000000000072"
CASE_ID = "00000000-0000-0000-0000-000000000073"
REVIEW_JOB_ID = "00000000-0000-0000-0000-000000000074"


def _production_values(tmp_path: Path):
    skill = load_test_methods_skill(
        tmp_path / "skill-input",
        name="replay-test",
        methods=(("replay-method", "REPLAY_MARKER"),),
    )
    content = b"REPLAY_MARKER request_id=req-1\n"
    target = FrozenTargetLogV1(
        source_id="server",
        relative_path="logs/server.log",
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    graph = scan_method_evidence_v2(skill=skill, target_logs=(target,))
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)
    state = start_method_state_v2(
        case_id=CASE_ID,
        source_job_id=SOURCE_JOB_ID,
        evaluation_id=EVALUATION_ID,
        plan=plan,
    )
    return graph, plan, state


def _valid_response(plan):
    return [
        {
            "evaluation_ref": item.evaluation_ref,
            "verdict": "CONFIRMED",
            "reason": "The frozen event supports this method.",
        }
        for item in plan.evaluations
    ]


def _store(tmp_path: Path) -> FileExecutionRecordStore:
    tmp_path.mkdir()
    return FileExecutionRecordStore(tmp_path, StorageCoordinationLock())


def _publish_source_closure(records, *, graph, plan) -> None:
    publish_method_evidence_graph_v2(
        records,
        job_id=SOURCE_JOB_ID,
        graph=graph,
    )
    publish_method_evaluation_plan_v2(
        records,
        job_id=SOURCE_JOB_ID,
        plan=plan,
    )


def test_real_store_replays_specialist_primary_rejection_without_rescanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph, plan, state = _production_values(tmp_path)
    state = record_protocol_error_v2(
        state=state,
        role="SPECIALIST",
        reason="invalid JSON",
    )
    raw_bytes = b"not-json\n"
    records = _store(tmp_path / "records")
    _publish_source_closure(records, graph=graph, plan=plan)
    publish_method_state_v2(records, job_id=SOURCE_JOB_ID, state=state)
    publish_method_rejected_attempt_v2(
        records,
        job_id=SOURCE_JOB_ID,
        role="SPECIALIST",
        attempt="PRIMARY",
        raw_bytes=raw_bytes,
    )

    def unexpected_scan(*_args, **_kwargs):
        raise AssertionError("validation-only replay must not scan logs")

    monkeypatch.setattr(
        methods_evidence_v2,
        "scan_method_evidence_v2",
        unexpected_scan,
    )
    receipt = replay_method_rejected_attempt_v2(
        records,
        job_id=SOURCE_JOB_ID,
        role="SPECIALIST",
        attempt="PRIMARY",
    )

    assert receipt.status == "REJECTION_REPRODUCED"
    assert receipt.job_id == receipt.source_job_id == SOURCE_JOB_ID
    assert receipt.graph_ref == graph.graph_ref
    assert receipt.plan_ref == plan.plan_ref
    assert receipt.state_ref == state.state_ref
    assert receipt.raw_response_sha256 == hashlib.sha256(raw_bytes).hexdigest()
    assert receipt.raw_response_size == len(raw_bytes)
    assert receipt.rejection_reason == (
        "SPECIALIST model evaluation response is not valid UTF-8 JSON"
    )


def test_real_store_replays_reviewer_repair_from_source_graph_and_review_state(
    tmp_path: Path,
) -> None:
    graph, plan, state = _production_values(tmp_path)
    specialist = evaluate_method_role_v2(
        role="SPECIALIST",
        plan=plan,
        response=_valid_response(plan),
        attempt="PRIMARY",
    )
    state = accept_specialist_evaluation_v2(
        state=state,
        evaluation=specialist,
    )
    state = record_protocol_error_v2(
        state=state,
        role="REVIEWER",
        reason="missing evaluation",
    )
    state = record_protocol_error_v2(
        state=state,
        role="REVIEWER",
        reason="repair still missing evaluation",
    )
    raw_bytes = canonical_json_bytes([])
    records = _store(tmp_path / "records")
    _publish_source_closure(records, graph=graph, plan=plan)
    publish_method_state_v2(records, job_id=REVIEW_JOB_ID, state=state)
    publish_method_rejected_attempt_v2(
        records,
        job_id=REVIEW_JOB_ID,
        role="REVIEWER",
        attempt="REPAIR",
        raw_bytes=raw_bytes,
    )

    receipt = replay_method_rejected_attempt_v2(
        records,
        job_id=REVIEW_JOB_ID,
        role="REVIEWER",
        attempt="REPAIR",
    )

    assert receipt.job_id == REVIEW_JOB_ID
    assert receipt.source_job_id == SOURCE_JOB_ID
    assert receipt.role == "REVIEWER"
    assert receipt.attempt == "REPAIR"
    assert receipt.rejection_reason == (
        "model evaluation response must exactly cover every planned evaluation"
    )


def test_replay_reports_when_current_parser_accepts_a_stored_rejection(
    tmp_path: Path,
) -> None:
    graph, plan, state = _production_values(tmp_path)
    state = record_protocol_error_v2(
        state=state,
        role="SPECIALIST",
        reason="recorded protocol failure",
    )
    records = _store(tmp_path / "records")
    _publish_source_closure(records, graph=graph, plan=plan)
    publish_method_state_v2(records, job_id=SOURCE_JOB_ID, state=state)
    publish_method_rejected_attempt_v2(
        records,
        job_id=SOURCE_JOB_ID,
        role="SPECIALIST",
        attempt="PRIMARY",
        raw_bytes=canonical_json_bytes(_valid_response(plan)),
    )

    with pytest.raises(MethodValidationReplayErrorV2) as caught:
        replay_method_rejected_attempt_v2(
            records,
            job_id=SOURCE_JOB_ID,
            role="SPECIALIST",
            attempt="PRIMARY",
        )

    assert (
        caught.value.code
        is MethodValidationReplayErrorCodeV2.REJECTION_NOT_REPRODUCED
    )


def test_replay_missing_attempt_has_a_typed_error(tmp_path: Path) -> None:
    graph, plan, state = _production_values(tmp_path)
    state = record_protocol_error_v2(
        state=state,
        role="SPECIALIST",
        reason="recorded protocol failure",
    )
    records = _store(tmp_path / "records")
    _publish_source_closure(records, graph=graph, plan=plan)
    publish_method_state_v2(records, job_id=SOURCE_JOB_ID, state=state)

    with pytest.raises(MethodValidationReplayErrorV2) as caught:
        replay_method_rejected_attempt_v2(
            records,
            job_id=SOURCE_JOB_ID,
            role="SPECIALIST",
            attempt="PRIMARY",
        )

    assert (
        caught.value.code
        is MethodValidationReplayErrorCodeV2.REJECTED_ATTEMPT_NOT_FOUND
    )


def test_one_field_state_mutation_is_reported_as_an_invalid_core_record(
    tmp_path: Path,
) -> None:
    graph, plan, state = _production_values(tmp_path)
    state = record_protocol_error_v2(
        state=state,
        role="SPECIALIST",
        reason="recorded protocol failure",
    )
    records = _store(tmp_path / "records")
    _publish_source_closure(records, graph=graph, plan=plan)
    state_value = state.model_dump(mode="json")
    state_value["plan_ref"] = "plan-" + "5" * 64
    records.publish_audit_bytes(
        SOURCE_JOB_ID,
        METHODS_STATE_V2_FILENAME,
        canonical_json_bytes(state_value),
    )

    with pytest.raises(MethodValidationReplayErrorV2) as caught:
        replay_method_rejected_attempt_v2(
            records,
            job_id=SOURCE_JOB_ID,
            role="SPECIALIST",
            attempt="PRIMARY",
        )

    assert caught.value.code is MethodValidationReplayErrorCodeV2.CORE_RECORD_INVALID
