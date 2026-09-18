from __future__ import annotations

import hashlib

from problem_locator.contracts import (
    Artifact, ArtifactKind, Case, CaseAggregate, CaseStatus, DiagnosisState,
    GenericDiagnosisOutcomeV2, GenericReportMetadataV1, GenericResultStatus,
    GenericResultV2Draft, Job, JobOutcome, JobStatus, OutcomeDisposition,
    OutcomeProcessingRecord, OutcomeResultType, ResourceKind, ReviewPolicy,
    SPECIALIZED_DIRECT_AGENT_PROFILE_ID, SPECIALIZED_DIRECT_OUTPUT_CONTRACT_ID,
    canonical_json_bytes, finalize_generic_result_v2,
)

from ._support import FIXTURE_ROOT, load_json


REPORT = "# Skill 原始结论\r\n\r\n根因是服务端处理超时。\n\n```text\n不要求框架证据字段\n```\n"
TIME = "2026-07-31T00:03:00.000Z"
ARTIFACT_ID = "00000000-0000-4000-8000-000000000801"
OUTCOME_ID = "00000000-0000-4000-8000-000000000802"


def direct_job() -> Job:
    values = load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    values.update(
        review_policy=ReviewPolicy.NONE,
        evidence_refs=[], attachment_refs=[], artifact_refs=[], previous_outcome_refs=[],
        status=JobStatus.SUCCEEDED, started_at="2026-07-31T00:01:01.000Z",
        finished_at=TIME, runtime_epoch="00000000-0000-4000-8000-000000000803",
    )
    values["context_snapshot"]["evidence_refs"] = []
    values["skill_ref"]["id"] = "diagnosis-skill/rpc-timeout"
    values["agent_profile_ref"]["id"] = SPECIALIZED_DIRECT_AGENT_PROFILE_ID
    values["output_contract_ref"]["id"] = SPECIALIZED_DIRECT_OUTPUT_CONTRACT_ID
    return Job.model_validate(values)


def direct_outcome(job: Job, status: GenericResultStatus = GenericResultStatus.RESOLVED) -> JobOutcome:
    report_bytes = REPORT.encode("utf-8")
    return JobOutcome(
        outcome_id=OUTCOME_ID, job_id=job.job_id, case_id=job.case_id,
        job_type=job.job_type, base_state_revision=job.base_state_revision,
        result_type=OutcomeResultType.COMPLETED,
        payload=GenericDiagnosisOutcomeV2(
            format_version=2, status=status, report_markdown=REPORT,
            report_utf8_size=len(report_bytes), report_sha256=hashlib.sha256(report_bytes).hexdigest(),
            skill_name="rpc-timeout",
        ),
        consumed_evidence_refs=[], proposed_evidence=[], proposed_artifacts=[],
        error=None, produced_at=TIME, decision_audit=None,
    )


def direct_aggregate(status: GenericResultStatus = GenericResultStatus.RESOLVED) -> CaseAggregate:
    job = direct_job()
    outcome = direct_outcome(job, status)
    payload = outcome.payload
    draft = GenericResultV2Draft(
        **payload.model_dump(mode="python"), source_job_id=job.job_id,
        source_outcome_id=outcome.outcome_id, occurred_at=TIME,
    )
    result = finalize_generic_result_v2(draft, ARTIFACT_ID)
    state_values = job.context_snapshot.model_dump(mode="python")
    state_values["revision"] = state_values.pop("diagnosis_state_revision")
    state = DiagnosisState.model_validate(state_values)
    case = Case(
        case_id=job.case_id, status=CaseStatus(status.value), case_revision=3,
        raw_problem_text=state.problem_spec.statement, diagnosis_state=state,
        active_job_id=None, selected_skill_ref=job.skill_ref, final_result=None,
        generic_result_v2=result, failure=None, created_at=job.created_at, updated_at=TIME,
    )
    artifact = Artifact(
        artifact_id=ARTIFACT_ID, case_id=job.case_id, kind=ArtifactKind.GENERIC_REPORT,
        name="skill-diagnosis-report.md", content_type="text/markdown", resource_kind=ResourceKind.FILE,
        size=result.report_utf8_size, sha256=result.report_sha256,
        storage_key=f"cases/{job.case_id}/artifacts/{ARTIFACT_ID}",
        metadata=GenericReportMetadataV1(
            schema_version=1, format_id="problem-locator-generic-report-v1",
            description="Skill 原始 Markdown 报告。", generic_result_format_version=2,
            status=status, source_job_id=job.job_id, source_outcome_id=outcome.outcome_id,
            report_utf8_size=result.report_utf8_size, report_sha256=result.report_sha256,
        ),
        created_by_job_id=job.job_id, created_at=TIME,
    )
    outcome_bytes = canonical_json_bytes(outcome)
    digest = hashlib.sha256(outcome_bytes).hexdigest()
    processing = OutcomeProcessingRecord(
        outcome_id=outcome.outcome_id, job_id=job.job_id, outcome_hash=digest,
        outcome_file_ref={"relative_key": f"jobs/{job.job_id}/job_outcome.json",
            "size": len(outcome_bytes), "sha256": digest},
        disposition=OutcomeDisposition.APPLIED, processed_at=TIME, error_code=None,
        accepted_evidence_ids=[], accepted_artifact_ids=[], generated_artifact_ids=[ARTIFACT_ID],
        created_job_id=None, reason="直接交付 Skill 的报告。",
    )
    return CaseAggregate(
        case=case, jobs={job.job_id: job}, outcomes={outcome.outcome_id: outcome},
        outcome_processing_records={outcome.outcome_id: processing},
        execution_failure_records={}, attachments={}, evidence={}, artifacts={ARTIFACT_ID: artifact},
    )
