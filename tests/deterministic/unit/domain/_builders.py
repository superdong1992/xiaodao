from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from problem_locator.contracts import (
    ArtifactKind,
    ArtifactProposal,
    Case,
    CaseFailure,
    CaseSnapshot,
    CaseStatus,
    ContinuationResourceView,
    DiagnosisState,
    Job,
    JobOutcome,
    JobStatus,
    JobType,
    ResourceKind,
    RuntimeBindings,
    StagedResourceRef,
    UserResultMetadata,
    ValidatedTrigger,
    VersionedRef,
)
from problem_locator.contracts.serialization import parse_canonical_json_bytes


ROOT = Path(__file__).resolve().parents[4]
CONTRACT_FIXTURES = ROOT / "tests" / "fixtures" / "contracts" / "positive"

CASE_ID = "00000000-0000-0000-0000-000000000001"
TRIGGER_ID = "00000000-0000-0000-0000-000000000090"
RUNTIME_EPOCH = "00000000-0000-0000-0000-000000000091"
CURRENT_EPOCH = "00000000-0000-0000-0000-000000000092"
FIXED_TIME = "2026-07-31T00:03:00.000Z"


ModelT = TypeVar("ModelT", bound=BaseModel)


def rebuild(model: ModelT, **changes: object) -> ModelT:
    value = model.model_dump(mode="python")
    value.update(changes)
    return type(model).model_validate(value)


def fixture(model_type: type[ModelT], name: str) -> ModelT:
    return parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / name).read_bytes(),
        model_type=model_type,
    )


def route_job() -> Job:
    return fixture(Job, "job-route.json")


def diagnose_job() -> Job:
    return fixture(Job, "job-diagnose.json")


def review_job() -> Job:
    return fixture(Job, "job-review.json")


def route_outcome() -> JobOutcome:
    return fixture(JobOutcome, "job-outcome-route.json")


def diagnosis_outcome() -> JobOutcome:
    return fixture(JobOutcome, "job-outcome-diagnosis.json")


def review_outcome() -> JobOutcome:
    return fixture(JobOutcome, "job-outcome-review.json")


def unresolved_user_result_proposal(job: Job) -> ArtifactProposal:
    """Build the server-final USER_RESULT required by an unresolved branch."""

    staged = StagedResourceRef(
        staging_id="00000000-0000-0000-0000-000000000069",
        owner_job_id=job.job_id,
        proposal_key="user_result",
        resource_kind=ResourceKind.FILE,
        size=321,
        sha256="9" * 64,
        tree_manifest=None,
    )
    return ArtifactProposal(
        proposal_key=staged.proposal_key,
        artifact_kind=ArtifactKind.USER_RESULT,
        name="diagnosis-result.json",
        content_type="application/json",
        resource_kind=staged.resource_kind,
        size=staged.size,
        sha256=staged.sha256,
        staged_resource_ref=staged,
        metadata=UserResultMetadata(
            schema_version=3,
            format_id="problem-locator-diagnosis-v3",
            description="Canonical unresolved diagnosis result.",
        ),
    )


def failure_outcome() -> JobOutcome:
    return fixture(JobOutcome, "job-outcome-failure.json")


def state_from_job(job: Job) -> DiagnosisState:
    snapshot = job.context_snapshot
    return DiagnosisState(
        revision=snapshot.diagnosis_state_revision,
        problem_spec=snapshot.problem_spec,
        user_facts=snapshot.user_facts,
        confirmed_facts=snapshot.confirmed_facts,
        active_hypotheses=snapshot.active_hypotheses,
        rejected_hypotheses=snapshot.rejected_hypotheses,
        open_questions=snapshot.open_questions,
        pending_requirements=snapshot.pending_requirements,
        evidence_refs=snapshot.evidence_refs,
        candidate_conclusion=snapshot.candidate_conclusion,
    )


def running(job: Job) -> Job:
    return rebuild(
        job,
        status=JobStatus.RUNNING,
        started_at="2026-07-31T00:00:10.000Z",
        finished_at=None,
        runtime_epoch=RUNTIME_EPOCH,
    )


def interrupted(job: Job) -> Job:
    started = running(job)
    return rebuild(
        started,
        status=JobStatus.INTERRUPTED,
        finished_at="2026-07-31T00:00:20.000Z",
    )


def runtime_bindings(job: Job) -> RuntimeBindings:
    return RuntimeBindings(
        diagnosis_mode=job.diagnosis_mode,
        generic_skill_name=job.generic_skill_name,
        agent_profile_ref=job.agent_profile_ref,
        available_skill_refs=job.available_skill_refs,
        skill_ref=job.skill_ref,
        tool_bundle_ref=job.tool_bundle_ref,
        context_policy_ref=job.context_policy_ref,
        output_contract_ref=job.output_contract_ref,
        logparse_tool_ref=job.logparse_tool_ref,
        logparse_product=job.logparse_product,
        resource_limits=job.resource_limits,
    )


def snapshot_with_active(
    job: Job,
    *,
    status: CaseStatus | None = None,
    job_status: JobStatus = JobStatus.RUNNING,
    state: DiagnosisState | None = None,
    case_revision: int = 7,
) -> CaseSnapshot:
    if job_status is JobStatus.RUNNING:
        active = running(job)
    elif job_status is JobStatus.PENDING:
        active = rebuild(
            job,
            status=JobStatus.PENDING,
            started_at=None,
            finished_at=None,
            runtime_epoch=None,
        )
    else:
        raise ValueError("active jobs must be PENDING or RUNNING")
    target_status = status or (
        CaseStatus.REVIEWING if job.job_type is JobType.REVIEW else CaseStatus.RUNNING
    )
    diagnosis_state = state or state_from_job(job)
    case = Case(
        case_id=job.case_id,
        status=target_status,
        case_revision=case_revision,
        raw_problem_text=(
            job.generic_problem_text or job.context_snapshot.problem_spec.statement
        ),
        diagnosis_state=diagnosis_state,
        active_job_id=active.job_id,
        selected_skill_ref=(
            None if job.job_type is JobType.ROUTE else job.skill_ref
        ),
        final_result=None,
        failure=None,
        created_at="2026-07-31T00:00:00.000Z",
        updated_at=FIXED_TIME,
    )
    return CaseSnapshot(
        case=case,
        active_job=active,
        resume_source_job=None,
        replacement_job_ids_by_source={},
    )


def waiting_snapshot(
    state: DiagnosisState,
    status: CaseStatus,
    *,
    selected_skill_ref: VersionedRef | None = None,
    case_revision: int = 7,
) -> CaseSnapshot:
    case = Case(
        case_id=CASE_ID,
        status=status,
        case_revision=case_revision,
        raw_problem_text=state.problem_spec.statement,
        diagnosis_state=state,
        active_job_id=None,
        selected_skill_ref=selected_skill_ref or diagnose_job().skill_ref,
        final_result=None,
        failure=None,
        created_at="2026-07-31T00:00:00.000Z",
        updated_at=FIXED_TIME,
    )
    return CaseSnapshot(
        case=case,
        active_job=None,
        resume_source_job=None,
        replacement_job_ids_by_source={},
    )


def interrupted_snapshot(
    source: Job,
    *,
    replacements: dict[str, str] | None = None,
    case_revision: int = 7,
) -> CaseSnapshot:
    source = interrupted(source)
    state = state_from_job(source)
    case = Case(
        case_id=source.case_id,
        status=CaseStatus.INTERRUPTED,
        case_revision=case_revision,
        raw_problem_text=(
            source.generic_problem_text or source.context_snapshot.problem_spec.statement
        ),
        diagnosis_state=state,
        active_job_id=None,
        selected_skill_ref=(
            None if source.job_type is JobType.ROUTE else source.skill_ref
        ),
        final_result=None,
        failure=None,
        created_at="2026-07-31T00:00:00.000Z",
        updated_at=FIXED_TIME,
    )
    return CaseSnapshot(
        case=case,
        active_job=None,
        resume_source_job=source,
        replacement_job_ids_by_source=replacements or {},
    )


def failed_case_snapshot(state: DiagnosisState) -> CaseSnapshot:
    case = Case(
        case_id=CASE_ID,
        status=CaseStatus.FAILED,
        case_revision=7,
        raw_problem_text=state.problem_spec.statement,
        diagnosis_state=state,
        active_job_id=None,
        selected_skill_ref=None,
        final_result=None,
        failure=CaseFailure(
            code="NO_CAPABILITY",
            message="No fixed skill matches.",
            source_job_id=None,
            source_outcome_id=None,
            occurred_at=FIXED_TIME,
        ),
        created_at="2026-07-31T00:00:00.000Z",
        updated_at=FIXED_TIME,
    )
    return CaseSnapshot(
        case=case,
        active_job=None,
        resume_source_job=None,
        replacement_job_ids_by_source={},
    )


def continuation(
    *,
    incoming_outcome_id: str | None = None,
    job: Job | None = None,
) -> ContinuationResourceView:
    source = job
    previous = [] if source is None else list(source.previous_outcome_refs)
    if incoming_outcome_id is not None:
        previous = [incoming_outcome_id, *previous]
    return ContinuationResourceView(
        evidence_refs=[] if source is None else source.evidence_refs,
        attachment_refs=[] if source is None else source.attachment_refs,
        artifact_refs=[] if source is None else source.artifact_refs,
        previous_outcome_refs=list(dict.fromkeys(previous)),
    )


def trigger(
    snapshot: CaseSnapshot,
    *,
    trigger_type: object,
    payload: object,
    bindings: dict[JobType, RuntimeBindings] | None = None,
    continuation_resources: ContinuationResourceView | None = None,
    occurred_at: str = FIXED_TIME,
    trigger_id: str = TRIGGER_ID,
) -> ValidatedTrigger:
    return ValidatedTrigger(
        trigger_id=trigger_id,
        trigger_type=trigger_type,
        case_id=snapshot.case.case_id,
        expected_case_revision=(
            0 if str(trigger_type) == "CREATE_CASE" else snapshot.case.case_revision
        ),
        idempotency_key=f"domain-{str(trigger_type).lower()}",
        payload=payload,
        continuation_resources=continuation_resources
        or ContinuationResourceView(
            evidence_refs=[],
            attachment_refs=[],
            artifact_refs=[],
            previous_outcome_refs=[],
        ),
        runtime_bindings_by_job_type=bindings or {},
        occurred_at=occurred_at,
    )
