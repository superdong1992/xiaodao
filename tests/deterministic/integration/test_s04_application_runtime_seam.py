from __future__ import annotations

import hashlib
import json
from pathlib import Path

from problem_locator.application.outcome_submission import OutcomeSubmissionService
from problem_locator.application.preparation import runtime_bindings_from_job
from problem_locator.contracts import (
    ContextSectionKind,
    Job,
    JobOutcome,
    JobStatus,
    OutcomeDisposition,
    ResourceKind,
    StateFile,
    WorkspaceInputManifest,
    canonical_json_bytes,
)
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.runtime.context_builder import ContextBuilder, ContextMaterials
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeClock,
    InMemoryExecutionRecordStore,
    InMemoryPublicationCommitGuard,
    InMemoryResourceStore,
    InMemoryStateChangeNotifier,
    InMemoryStateRepository,
    RecordingDispatcher,
)
from tests.v2_helpers import resolved_logparse_plan


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/contracts/positive"
CASE_ID = "00000000-0000-0000-0000-000000000001"
JOB_ID = "00000000-0000-0000-0000-000000000010"
RUNTIME_EPOCH = "00000000-0000-0000-0000-000000000090"
FIXED_TIME = "2026-07-31T08:04:00.000Z"


def _running_route_state() -> StateFile:
    payload = json.loads((FIXTURES / "state.json").read_text())
    aggregate = payload["cases"][CASE_ID]
    aggregate["case"].update(
        case_revision=2,
        updated_at="2026-07-31T00:01:00.000Z",
    )
    aggregate["jobs"][JOB_ID].update(
        status=JobStatus.RUNNING,
        started_at="2026-07-31T00:01:00.000Z",
        runtime_epoch=RUNTIME_EPOCH,
    )
    return StateFile.model_validate(payload)


def _apply_route_outcome():
    outcome = JobOutcome.model_validate_json(
        (FIXTURES / "job-outcome-route.json").read_text()
    )
    diagnose_template = Job.model_validate_json(
        (FIXTURES / "job-diagnose.json").read_text()
    )
    diagnose_bindings = runtime_bindings_from_job(diagnose_template)
    skill_ref = outcome.payload.skill_ref
    assert skill_ref is not None
    catalog = FakeAssetCatalog(
        diagnose={
            (skill_ref.id, skill_ref.version, skill_ref.content_hash): diagnose_bindings
        }
    )
    guard = InMemoryPublicationCommitGuard()
    records = InMemoryExecutionRecordStore()
    outcome_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    repository = InMemoryStateRepository(_running_route_state())
    service = OutcomeSubmissionService(
        repository,
        InMemoryResourceStore(publication_guard=guard),
        guard,
        records,
        DomainCoordinator(),
        PureContextSnapshotProjector(),
        catalog,
        RecordingDispatcher(),
        InMemoryStateChangeNotifier(),
        FakeClock(FIXED_TIME),
        DeterministicIdGenerator(seed="s08-s04-route"),
    )
    receipt = service.submit_outcome(outcome, outcome_ref)
    assert receipt.disposition is OutcomeDisposition.APPLIED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert aggregate.case.active_job_id is not None
    return aggregate.jobs[aggregate.case.active_job_id], outcome


def _manifest(job: Job, outcome: JobOutcome) -> WorkspaceInputManifest:
    encoded = canonical_json_bytes(outcome)
    return WorkspaceInputManifest.model_validate(
        {
            "schema_version": 2,
            "job_id": job.job_id,
            "case_id": job.case_id,
            "job_type": job.job_type.value,
            "logparse_tool_ref": (
                None
                if job.logparse_tool_ref is None
                else job.logparse_tool_ref.model_dump(mode="json")
            ),
            "logparse_product": job.logparse_product,
            "entries": [
                {
                    "input_kind": "PREVIOUS_OUTCOME",
                    "resource_id": outcome.outcome_id,
                    "relative_path": (
                        f"inputs/outcomes/{outcome.outcome_id}/job_outcome.json"
                    ),
                    "resource_kind": ResourceKind.FILE.value,
                    "size": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "source_job_id": outcome.job_id,
                    "result_type": outcome.result_type.value,
                }
            ],
            "resolved_logparse_plan": (
                None
                if (
                    job.logparse_tool_ref is None
                    or not (job.attachment_refs or job.artifact_refs)
                )
                else resolved_logparse_plan(
                    job,
                    problem_time="2026-07-31T00:00:00.000Z",
                    anchors=[
                        {
                            "label": "request",
                            "module": "payment",
                            "slot": "caller",
                            "process_name": "payment-service",
                            "pid": None,
                        }
                    ],
                ).model_dump(mode="json")
            ),
            "review_subject": None,
        }
    )


def test_application_frozen_previous_outcome_is_visible_to_runtime_context() -> None:
    job, previous_outcome = _apply_route_outcome()
    assert job.previous_outcome_refs == [previous_outcome.outcome_id]
    materials = ContextMaterials(
        profile="Specialist profile\n",
        skill="RPC diagnosis skill\n",
        tool_bundle="{}\n",
        output_contract="Frozen diagnosis outcome contract\n",
        manifest=_manifest(job, previous_outcome),
        previous_outcomes=(previous_outcome,),
    )

    context = ContextBuilder().build(job, materials)

    previous_sections = [
        section
        for section in context.sections
        if section.kind is ContextSectionKind.PREVIOUS_OUTCOME
    ]
    assert len(previous_sections) == 1
    assert previous_sections[0].source_refs == [previous_outcome.outcome_id]
    assert canonical_json_bytes(previous_outcome).decode("utf-8") in context.body
    manifest_section = next(
        section
        for section in context.sections
        if section.kind is ContextSectionKind.RESOURCE_MANIFEST
    )
    assert manifest_section.required is True
