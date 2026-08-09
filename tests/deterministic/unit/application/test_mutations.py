from __future__ import annotations

import json
from pathlib import Path

from problem_locator.application.mutations import (
    apply_transition_plan_to_case,
    build_state_mutation,
)
from problem_locator.contracts import (
    CaseStatus,
    DiagnosisStateDelta,
    JobStatus,
    OutcomeDisposition,
    StateFile,
    TransitionPlan,
)


ROOT = Path(__file__).resolve().parents[4]


def _state() -> StateFile:
    return StateFile.model_validate(
        json.loads(
            (ROOT / "tests/fixtures/contracts/positive/state.json").read_text(
                encoding="utf-8"
            )
        )
    )


def _empty_delta() -> DiagnosisStateDelta:
    return DiagnosisStateDelta(
        problem_spec_patch=None,
        add_user_facts=[],
        proposed_facts=[],
        add_active_hypotheses=[],
        update_hypotheses=[],
        reject_hypotheses=[],
        add_open_questions=[],
        resolve_questions=[],
        add_pending_requirements=[],
        fulfill_requirements=[],
        add_evidence_bindings=[],
    )


def test_mutation_builder_populates_every_collection_independently() -> None:
    first = build_state_mutation()
    second = build_state_mutation()

    assert first.model_dump(mode="json") == {
        "upsert_case": None,
        "upsert_runtime_epoch_records": [],
        "upsert_recovery_processing_records": [],
        "insert_jobs": [],
        "job_lifecycle_updates": [],
        "insert_outcomes": [],
        "insert_outcome_processing_records": [],
        "insert_execution_failure_records": [],
        "upsert_attachments": [],
        "insert_evidence": [],
        "insert_artifacts": [],
        "insert_idempotency_records": [],
    }
    assert first is not second
    assert first.insert_jobs is not second.insert_jobs


def test_case_plan_clears_active_job_and_bumps_only_case_revision() -> None:
    aggregate = next(iter(_state().cases.values()))
    current = aggregate.case
    active = aggregate.jobs[current.active_job_id]
    plan = TransitionPlan(
        accepted_state_delta=_empty_delta(),
        target_case_status=CaseStatus.WAITING_INPUT,
        job_updates=[
            {
                "job_id": active.job_id,
                "expected_status": JobStatus.PENDING,
                "target_status": JobStatus.SUCCEEDED,
                "started_at": None,
                "finished_at": "2026-07-31T00:02:00.000Z",
                "runtime_epoch": None,
            }
        ],
        outcome_disposition=OutcomeDisposition.APPLIED,
        accepted_evidence_proposal_keys=[],
        accepted_artifact_proposal_keys=[],
        accepted_candidate_proposal_key=None,
        selected_skill_update=None,
        case_failure_update=None,
        candidate_mutation=None,
        next_job_spec=None,
        final_result_target=None,
        clear_active_job=True,
        reason="Wait for validated user input.",
    )

    result = apply_transition_plan_to_case(
        current,
        plan,
        current.diagnosis_state,
        created_job=None,
        processed_at="2026-07-31T00:02:00.000Z",
    )

    assert result.status is CaseStatus.WAITING_INPUT
    assert result.active_job_id is None
    assert result.case_revision == current.case_revision + 1
    assert result.diagnosis_state.revision == current.diagnosis_state.revision
    assert result.updated_at == "2026-07-31T00:02:00.000Z"
