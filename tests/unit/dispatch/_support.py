from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from problem_locator.contracts import (
    APPLICATION_ERROR_RETRYABLE_CODES,
    ApplicationError,
    ApplicationPortError,
    CaseStatus,
    CaseView,
    ExecutionFileRef,
    ErrorCode,
    Job,
    JobOutcome,
    JobStatus,
    JobSummary,
    RuntimeExecutionReceipt,
    StateFile,
    canonical_json_bytes,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "contracts" / "positive"
DISPATCH_FIXTURES = (
    REPOSITORY_ROOT / "tests" / "fixtures" / "components" / "dispatch-scheduler"
)
CURRENT_EPOCH = "00000000-0000-0000-0000-000000000091"
OLD_EPOCH = "00000000-0000-0000-0000-000000000090"
STARTED_AT = "2026-07-31T00:10:00.000Z"
FINISHED_AT = "2026-07-31T00:11:00.000Z"
_UNSET = object()


def application_port_error(code: ErrorCode) -> ApplicationPortError:
    return ApplicationPortError(
        ApplicationError(
            code=code,
            message=f"Modeled JobControlPort failure: {code.value}.",
            details=[],
            retryable=code in APPLICATION_ERROR_RETRYABLE_CODES,
        )
    )


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_job(job_type: str) -> Job:
    return Job.model_validate(load_json(CONTRACT_FIXTURES / f"job-{job_type.lower()}.json"))


def load_outcome(job_type: str) -> JobOutcome:
    fixture_name = "diagnosis" if job_type.lower() == "diagnose" else job_type.lower()
    return JobOutcome.model_validate(
        load_json(CONTRACT_FIXTURES / f"job-outcome-{fixture_name}.json")
    )


def load_state() -> StateFile:
    return StateFile.model_validate(load_json(CONTRACT_FIXTURES / "state.json"))


def load_dispatch_fixture(name: str) -> dict[str, Any]:
    return load_json(DISPATCH_FIXTURES / name)


def clone_job(
    job: Job,
    *,
    job_id: str | None = None,
    case_id: str | None = None,
    status: JobStatus | None = None,
    runtime_epoch: str | None = None,
    replacement_for_job_id: str | None | object = _UNSET,
) -> Job:
    payload = job.model_dump(mode="json")
    payload["job_id"] = job_id or job.job_id
    payload["case_id"] = case_id or job.case_id
    if replacement_for_job_id is not _UNSET:
        payload["replacement_for_job_id"] = replacement_for_job_id
    target_status = status or job.status
    payload["status"] = target_status.value
    if target_status is JobStatus.PENDING:
        payload.update(started_at=None, finished_at=None, runtime_epoch=None)
    elif target_status is JobStatus.RUNNING:
        payload.update(
            started_at=STARTED_AT,
            finished_at=None,
            runtime_epoch=runtime_epoch or CURRENT_EPOCH,
        )
    else:
        payload.update(
            started_at=payload.get("started_at") or STARTED_AT,
            finished_at=FINISHED_AT,
            runtime_epoch=runtime_epoch or OLD_EPOCH,
        )
    return Job.model_validate(payload)


def clone_outcome(
    outcome: JobOutcome,
    job: Job,
    *,
    outcome_id: str | None = None,
) -> JobOutcome:
    payload = outcome.model_dump(mode="json")
    payload.update(
        outcome_id=outcome_id or outcome.outcome_id,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type.value,
        base_state_revision=job.base_state_revision,
    )
    for proposal in payload["proposed_artifacts"]:
        proposal["staged_resource_ref"]["owner_job_id"] = job.job_id
    for proposal in payload["proposed_evidence"]:
        staged = proposal.get("staged_resource_ref")
        if staged is not None:
            staged["owner_job_id"] = job.job_id
    audit = payload.get("decision_audit")
    if audit is not None:
        assert job.skill_ref is not None
        audit.update(
            job_id=job.job_id,
            case_id=job.case_id,
            job_type=job.job_type.value,
            skill_ref=job.skill_ref.model_dump(mode="json"),
        )
    if payload["payload"] is not None and job.job_type.value == "REVIEW":
        target = job.review_target
        assert target is not None
        payload["payload"].update(
            candidate_conclusion_id=target.candidate_conclusion_id,
            candidate_revision=target.candidate_revision,
            candidate_content_hash=target.candidate_content_hash,
            reviewed_state_revision=job.base_state_revision,
            reviewed_evidence_refs=job.evidence_refs,
        )
        assert audit is not None
        audit["candidate_target"] = target.model_dump(mode="json")
    return JobOutcome.model_validate(payload)


def runtime_receipt(outcome: JobOutcome) -> RuntimeExecutionReceipt:
    payload = canonical_json_bytes(outcome)
    file_ref = ExecutionFileRef(
        relative_key=f"jobs/{outcome.job_id}/job_outcome.json",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    return RuntimeExecutionReceipt(job_outcome=outcome, outcome_file_ref=file_ref)


def case_view_for_job(job: Job) -> CaseView:
    if job.status in {JobStatus.PENDING, JobStatus.RUNNING}:
        status = (
            CaseStatus.REVIEWING
            if job.job_type.value == "REVIEW"
            else CaseStatus.RUNNING
        )
        active_job = JobSummary(
            job_id=job.job_id,
            job_type=job.job_type,
            status=job.status,
            goal=job.goal,
            base_state_revision=job.base_state_revision,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )
    else:
        status = {
            JobStatus.CANCELLED: CaseStatus.CANCELLED,
            JobStatus.INTERRUPTED: CaseStatus.INTERRUPTED,
            JobStatus.FAILED: CaseStatus.FAILED,
        }.get(job.status, CaseStatus.WAITING_INPUT)
        active_job = None
    return CaseView(
        case_id=job.case_id,
        status=status,
        case_revision=1,
        diagnosis_state_revision=job.base_state_revision,
        problem_spec=job.context_snapshot.problem_spec,
        user_facts=job.context_snapshot.user_facts,
        confirmed_facts=job.context_snapshot.confirmed_facts,
        open_questions=job.context_snapshot.open_questions,
        pending_requirements=job.context_snapshot.pending_requirements,
        active_job=active_job,
        selected_skill_ref=job.skill_ref,
        final_result=None,
        failure=None,
        artifacts=[],
        created_at=job.created_at,
        updated_at=STARTED_AT,
    )
