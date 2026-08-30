from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from problem_locator.contracts import Job, canonical_json_bytes
from problem_locator.domain.methods_state_v2 import (
    accept_specialist_evaluation_v2,
    record_protocol_error_v2,
    start_method_state_v2,
)
from problem_locator.runtime import methods_evidence_v2
from problem_locator.runtime.methods_evaluation_v2 import evaluate_method_role_v2
from problem_locator.runtime.methods_records_v2 import (
    METHODS_STATE_V2_FILENAME,
    publish_method_evaluation_plan_v2,
    publish_method_evidence_graph_v2,
    publish_method_rejected_attempt_v2,
    publish_method_state_v2,
    read_method_state_v2,
)
from problem_locator.runtime.methods_replay_v2 import (
    MethodValidationReplayErrorCodeV2,
    MethodValidationReplayErrorV2,
    replay_method_rejected_attempt_v2,
)
from problem_locator.storage.coordination import (
    InProcessPublicationCommitGuard,
    StorageCoordinationLock,
)
from problem_locator.storage.execution_records import FileExecutionRecordStore
from tests.deterministic.contracts.fakes import DeterministicIdGenerator
from tests.deterministic.unit.runtime.test_methods_workspace_context_v2 import _jobs


def _production_values(tmp_path: Path):
    _, _, specialist, reviewer, _, graph, plan, _ = _jobs(tmp_path)
    evaluation_id = DeterministicIdGenerator().derive(
        "methods_evaluation",
        [specialist.case_id, specialist.job_id, plan.plan_ref],
    )
    reviewer_value = reviewer.model_dump(mode="json")
    reviewer_value["methods_review_target"]["evaluation_id"] = evaluation_id
    reviewer = Job.model_validate(reviewer_value)
    state = start_method_state_v2(
        case_id=specialist.case_id,
        source_job_id=specialist.job_id,
        evaluation_id=evaluation_id,
        plan=plan,
    )
    return specialist, reviewer, graph, plan, state


def _valid_response(plan):
    return [
        {
            "evaluation_ref": item.evaluation_ref,
            "verdict": "CONFIRMED",
            "supporting_event_refs": list(item.evidence_event_refs),
            "reason": "The frozen event supports this method.",
        }
        for item in plan.evaluations
    ]


def _store(
    tmp_path: Path,
) -> tuple[FileExecutionRecordStore, InProcessPublicationCommitGuard]:
    tmp_path.mkdir()
    coordination = StorageCoordinationLock()
    return (
        FileExecutionRecordStore(tmp_path, coordination),
        InProcessPublicationCommitGuard(coordination),
    )


def _publish_jobs(
    records: FileExecutionRecordStore,
    guard: InProcessPublicationCommitGuard,
    *jobs: Job,
) -> None:
    with guard.acquire():
        for job in jobs:
            records.publish_job(job)


def _publish_source_closure(records, *, source_job_id: str, graph, plan) -> None:
    publish_method_evidence_graph_v2(
        records,
        job_id=source_job_id,
        graph=graph,
    )
    publish_method_evaluation_plan_v2(
        records,
        job_id=source_job_id,
        plan=plan,
    )


def test_real_store_replays_specialist_primary_rejection_without_rescanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specialist, _, graph, plan, initial_state = _production_values(tmp_path)
    expected_state = record_protocol_error_v2(
        state=initial_state,
        role="SPECIALIST",
        reason="invalid JSON",
    )
    raw_bytes = b"not-json\n"
    records, guard = _store(tmp_path / "records")
    _publish_jobs(records, guard, specialist)
    _publish_source_closure(
        records,
        source_job_id=specialist.job_id,
        graph=graph,
        plan=plan,
    )
    publish_method_rejected_attempt_v2(
        records,
        job_id=specialist.job_id,
        role="SPECIALIST",
        attempt="PRIMARY",
        raw_bytes=raw_bytes,
    )

    assert read_method_state_v2(records, job_id=specialist.job_id) is None

    def unexpected_scan(*_args, **_kwargs):
        raise AssertionError("validation-only replay must not scan logs")

    monkeypatch.setattr(
        methods_evidence_v2,
        "scan_method_evidence_v2",
        unexpected_scan,
    )
    receipt = replay_method_rejected_attempt_v2(
        records,
        job_id=specialist.job_id,
        role="SPECIALIST",
        attempt="PRIMARY",
    )

    assert receipt.status == "REJECTION_REPRODUCED"
    assert receipt.job_id == receipt.source_job_id == specialist.job_id
    assert receipt.graph_ref == graph.graph_ref
    assert receipt.plan_ref == plan.plan_ref
    assert receipt.state_ref == expected_state.state_ref
    assert receipt.raw_response_sha256 == hashlib.sha256(raw_bytes).hexdigest()
    assert receipt.raw_response_size == len(raw_bytes)
    assert receipt.rejection_reason == (
        "SPECIALIST model evaluation response is not valid UTF-8 JSON"
    )
    assert read_method_state_v2(records, job_id=specialist.job_id) is None


def test_real_store_replays_reviewer_repair_from_legal_rejection_sequence(
    tmp_path: Path,
) -> None:
    specialist_job, reviewer_job, graph, plan, state = _production_values(tmp_path)
    specialist = evaluate_method_role_v2(
        role="SPECIALIST",
        plan=plan,
        response=_valid_response(plan),
        attempt="PRIMARY",
    )
    source_state = accept_specialist_evaluation_v2(
        state=state,
        evaluation=specialist,
    )
    expected_state = record_protocol_error_v2(
        state=source_state,
        role="REVIEWER",
        reason="missing evaluation",
    )
    expected_state = record_protocol_error_v2(
        state=expected_state,
        role="REVIEWER",
        reason="repair still missing evaluation",
    )
    primary_bytes = canonical_json_bytes([])
    repair_bytes = b"not-json\n"
    records, guard = _store(tmp_path / "records")
    _publish_jobs(records, guard, specialist_job, reviewer_job)
    _publish_source_closure(
        records,
        source_job_id=specialist_job.job_id,
        graph=graph,
        plan=plan,
    )
    publish_method_state_v2(
        records,
        job_id=specialist_job.job_id,
        state=source_state,
    )
    publish_method_rejected_attempt_v2(
        records,
        job_id=reviewer_job.job_id,
        role="REVIEWER",
        attempt="PRIMARY",
        raw_bytes=primary_bytes,
    )
    publish_method_rejected_attempt_v2(
        records,
        job_id=reviewer_job.job_id,
        role="REVIEWER",
        attempt="REPAIR",
        raw_bytes=repair_bytes,
    )

    assert read_method_state_v2(records, job_id=reviewer_job.job_id) is None

    receipt = replay_method_rejected_attempt_v2(
        records,
        job_id=reviewer_job.job_id,
        role="REVIEWER",
        attempt="REPAIR",
    )

    assert receipt.job_id == reviewer_job.job_id
    assert receipt.source_job_id == specialist_job.job_id
    assert receipt.role == "REVIEWER"
    assert receipt.attempt == "REPAIR"
    assert receipt.state_ref == expected_state.state_ref
    assert receipt.raw_response_sha256 == hashlib.sha256(repair_bytes).hexdigest()
    assert receipt.rejection_reason == (
        "REVIEWER model evaluation response is not valid UTF-8 JSON"
    )
    assert read_method_state_v2(records, job_id=reviewer_job.job_id) is None
    assert (
        read_method_state_v2(records, job_id=specialist_job.job_id)
        == source_state
    )


def test_replay_reports_when_current_parser_accepts_a_stored_rejection(
    tmp_path: Path,
) -> None:
    specialist, _, graph, plan, state = _production_values(tmp_path)
    state = record_protocol_error_v2(
        state=state,
        role="SPECIALIST",
        reason="recorded protocol failure",
    )
    records, guard = _store(tmp_path / "records")
    _publish_jobs(records, guard, specialist)
    _publish_source_closure(
        records,
        source_job_id=specialist.job_id,
        graph=graph,
        plan=plan,
    )
    publish_method_state_v2(records, job_id=specialist.job_id, state=state)
    publish_method_rejected_attempt_v2(
        records,
        job_id=specialist.job_id,
        role="SPECIALIST",
        attempt="PRIMARY",
        raw_bytes=canonical_json_bytes(_valid_response(plan)),
    )

    with pytest.raises(MethodValidationReplayErrorV2) as caught:
        replay_method_rejected_attempt_v2(
            records,
            job_id=specialist.job_id,
            role="SPECIALIST",
            attempt="PRIMARY",
        )

    assert (
        caught.value.code
        is MethodValidationReplayErrorCodeV2.REJECTION_NOT_REPRODUCED
    )


def test_replay_missing_attempt_has_a_typed_error(tmp_path: Path) -> None:
    specialist, _, graph, plan, _ = _production_values(tmp_path)
    records, guard = _store(tmp_path / "records")
    _publish_jobs(records, guard, specialist)
    _publish_source_closure(
        records,
        source_job_id=specialist.job_id,
        graph=graph,
        plan=plan,
    )

    with pytest.raises(MethodValidationReplayErrorV2) as caught:
        replay_method_rejected_attempt_v2(
            records,
            job_id=specialist.job_id,
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
    specialist, _, graph, plan, state = _production_values(tmp_path)
    state = record_protocol_error_v2(
        state=state,
        role="SPECIALIST",
        reason="recorded protocol failure",
    )
    records, guard = _store(tmp_path / "records")
    _publish_jobs(records, guard, specialist)
    _publish_source_closure(
        records,
        source_job_id=specialist.job_id,
        graph=graph,
        plan=plan,
    )
    state_value = state.model_dump(mode="json")
    state_value["plan_ref"] = "plan-" + "5" * 64
    records.publish_audit_bytes(
        specialist.job_id,
        METHODS_STATE_V2_FILENAME,
        canonical_json_bytes(state_value),
    )

    with pytest.raises(MethodValidationReplayErrorV2) as caught:
        replay_method_rejected_attempt_v2(
            records,
            job_id=specialist.job_id,
            role="SPECIALIST",
            attempt="PRIMARY",
        )

    assert caught.value.code is MethodValidationReplayErrorCodeV2.CORE_RECORD_INVALID
