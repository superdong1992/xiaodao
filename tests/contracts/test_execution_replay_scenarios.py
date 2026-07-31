from __future__ import annotations

import hashlib
import json

import pytest

from problem_locator.contracts.enums import ErrorCode
from problem_locator.contracts.models import Job, JobOutcome, RuntimeExecutionReceipt
from problem_locator.contracts.serialization import canonical_json_bytes

from tests.contracts._support import FIXTURE_ROOT
from tests.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeClock,
    InMemoryExecutionRecordStore,
    InMemoryPublicationCommitGuard,
    InMemoryResourceStore,
    ScriptedRuntime,
)
from tests.contracts.scenario_fakes import (
    FIXED_OCCURRED_AT,
    ExecutionReplayScenario,
    ScenarioError,
    assets_for_bindings,
    bindings_from_job,
    claim_asset_error,
    job_asset_refs,
    runtime_bindings,
)


def _fixture_model(relative_path: str, model_type):
    value = json.loads((FIXTURE_ROOT / relative_path).read_text(encoding="utf-8"))
    return model_type.model_validate(value)


def test_restart_replays_durable_outcome_without_runtime_and_reuses_all_frozen_facts() -> None:
    source_job = _fixture_model("positive/job-diagnose.json", Job)
    outcome = _fixture_model("positive/job-outcome-diagnosis.json", JobOutcome)
    user_result_bytes = (FIXTURE_ROOT / "positive/user-result.json").read_bytes()
    assert len(user_result_bytes) == outcome.proposed_artifacts[0].size

    records = InMemoryExecutionRecordStore()
    outcome_bytes = canonical_json_bytes(outcome)
    outcome_ref = records.publish_outcome_bytes(source_job.job_id, outcome_bytes)
    runtime_receipt = RuntimeExecutionReceipt(
        job_outcome=outcome,
        outcome_file_ref=outcome_ref,
    )
    runtime = ScriptedRuntime([runtime_receipt])
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    bindings_a = runtime_bindings("2.0.0", "b")
    assert bindings_a.skill_ref is not None
    catalog_a = FakeAssetCatalog(
        assets=assets_for_bindings(bindings_a),
        diagnose={
            (
                bindings_a.skill_ref.id,
                bindings_a.skill_ref.version,
                bindings_a.skill_ref.content_hash,
            ): bindings_a
        },
    )
    first = ExecutionReplayScenario(
        source_job=source_job,
        next_job_template=source_job,
        outcome=outcome,
        user_result_bytes=user_result_bytes,
        execution_records=records,
        resources=resources,
        publication_guard=guard,
        ids=DeterministicIdGenerator(seed="first-process"),
        clock=FakeClock(FIXED_OCCURRED_AT),
        catalog=catalog_a,
        runtime=runtime,
    )

    with pytest.raises(ScenarioError) as failed_commit:
        first.deliver_then_fail_state_commit()
    assert failed_commit.value.error.code is ErrorCode.STATE_WRITE_FAILED
    assert failed_commit.value.error.retryable is True
    original = first.last_observation
    assert original is not None
    assert original.capacity_new_bytes == len(user_result_bytes)
    assert original.resource_bytes == user_result_bytes
    assert len(runtime.calls) == 1
    assert len(records.publish_outcome_calls) == 1
    assert len(records.publish_job_calls) == 1
    assert len(resources.stage_file_calls) == 1

    # Simulate process restart and Catalog A -> B.  The replay process gets a
    # fresh deterministic ID generator and a later clock, but no Runtime.
    bindings_b = runtime_bindings("3.0.0", "c")
    assert bindings_b.skill_ref is not None
    catalog_b = FakeAssetCatalog(
        assets=assets_for_bindings(bindings_b),
        diagnose={
            (
                bindings_b.skill_ref.id,
                bindings_b.skill_ref.version,
                bindings_b.skill_ref.content_hash,
            ): bindings_b
        },
    )
    replay = ExecutionReplayScenario(
        source_job=source_job,
        next_job_template=source_job,
        outcome=outcome,
        user_result_bytes=user_result_bytes,
        execution_records=records,
        resources=resources,
        publication_guard=guard,
        ids=DeterministicIdGenerator(seed="second-process"),
        clock=FakeClock("2030-01-01T00:00:00.000Z"),
        catalog=catalog_b,
        runtime=None,
    ).replay_after_restart()

    assert replay.artifact_id == original.artifact_id
    assert replay.candidate_id == original.candidate_id
    assert replay.next_job_id == original.next_job_id
    assert replay.occurred_at == original.occurred_at == FIXED_OCCURRED_AT
    assert replay.next_job_bytes == original.next_job_bytes
    assert replay.next_job == original.next_job
    persisted_job = records.read_published_job(replay.next_job_id)
    assert persisted_job is not None
    assert persisted_job.job_file_ref.size == len(original.next_job_bytes)
    assert persisted_job.job_file_ref.sha256 == hashlib.sha256(
        original.next_job_bytes
    ).hexdigest()
    assert bindings_from_job(replay.next_job) == bindings_a
    assert bindings_from_job(replay.next_job) != bindings_b
    assert replay.resource_ref == original.resource_ref
    assert replay.resource_bytes == original.resource_bytes == user_result_bytes
    assert replay.capacity_new_bytes == 0
    replayed_outcome = records.read_published_outcome(source_job.job_id)
    assert replayed_outcome is not None
    assert replayed_outcome.job_outcome == outcome
    assert replayed_outcome.outcome_file_ref == outcome_ref
    assert canonical_json_bytes(replayed_outcome.job_outcome) == outcome_bytes
    assert catalog_b.diagnose_calls == []
    assert len(runtime.calls) == 1
    assert len(records.publish_outcome_calls) == 1
    assert len(records.publish_job_calls) == 1
    assert len(resources.stage_file_calls) == 1

    # A later Claim must check the exact old refs.  Catalog B cannot silently
    # substitute its newer assets for the complete prepublished job.json.
    claim_error = claim_asset_error(replay.next_job, catalog_b)
    assert claim_error is not None
    assert claim_error.code is ErrorCode.ASSET_VERSION_UNAVAILABLE
    assert claim_error.retryable is False
    assert {detail.resource_ref.version for detail in claim_error.details} == {"2.0.0"}
    assert catalog_b.check_calls == [tuple(job_asset_refs(replay.next_job))]
