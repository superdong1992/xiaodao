from __future__ import annotations

from pathlib import Path

import pytest

from problem_locator.application.projection import (
    build_case_snapshot,
    continuation_for_outcome,
    continuation_for_resume,
    continuation_for_supplement,
    is_artifact_downloadable,
    project_artifact_summaries,
    project_artifact_summary,
    project_case_view,
)
from problem_locator.contracts import (
    Artifact,
    ArtifactKind,
    Attachment,
    AttachmentStatus,
    CandidateConclusion,
    Case,
    CaseAggregate,
    CaseStatus,
    DiagnosisState,
    Evidence,
    Job,
    JobOutcome,
    JobStatus,
    OutcomeDisposition,
    OutcomeProcessingRecord,
    OutcomeResultType,
    PendingRequirement,
    ResourceKind,
    StateFile,
)


CONTRACT_FIXTURES = (
    Path(__file__).parents[3] / "fixtures" / "contracts" / "positive"
)
NOW = "2026-07-31T00:00:00.000Z"
LATER = "2026-07-31T00:10:00.000Z"
CASE_ID = "00000000-0000-0000-0000-000000000001"
SOURCE_JOB_ID = "00000000-0000-0000-0000-000000000011"
SECOND_JOB_ID = "00000000-0000-0000-0000-000000000012"
REPLACEMENT_JOB_ID = "00000000-0000-0000-0000-000000000013"
RUNTIME_EPOCH = "00000000-0000-0000-0000-000000000099"


def _uuid(number: int) -> str:
    return f"00000000-0000-0000-0000-{number:012d}"


def _model(filename: str, model_type: type):
    return model_type.model_validate_json(
        (CONTRACT_FIXTURES / filename).read_text(encoding="utf-8")
    )


def _base_state() -> StateFile:
    return _model("state.json", StateFile)


def _base_case() -> Case:
    return _base_state().cases[CASE_ID].case


def _diagnose_job(
    job_id: str = SOURCE_JOB_ID,
    *,
    status: JobStatus = JobStatus.PENDING,
    evidence_refs: list[str] | None = None,
    attachment_refs: list[str] | None = None,
    artifact_refs: list[str] | None = None,
    previous_outcome_refs: list[str] | None = None,
    replacement_for_job_id: str | None = None,
    created_at: str = NOW,
    finished_at: str | None = None,
) -> Job:
    payload = _model("job-diagnose.json", Job).model_dump(mode="python")
    refs = list(evidence_refs or [])
    payload.update(
        {
            "job_id": job_id,
            "case_id": CASE_ID,
            "status": status,
            "evidence_refs": refs,
            "attachment_refs": list(attachment_refs or []),
            "artifact_refs": list(artifact_refs or []),
            "previous_outcome_refs": list(previous_outcome_refs or []),
            "replacement_for_job_id": replacement_for_job_id,
            "created_at": created_at,
            "started_at": NOW if status is not JobStatus.PENDING else None,
            "finished_at": finished_at,
            "runtime_epoch": RUNTIME_EPOCH if status is JobStatus.RUNNING else None,
        }
    )
    payload["context_snapshot"]["evidence_refs"] = refs
    if status in {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    }:
        payload["finished_at"] = finished_at or LATER
        payload["runtime_epoch"] = RUNTIME_EPOCH
    return Job.model_validate(payload)


def _case(
    *,
    status: CaseStatus,
    diagnosis: DiagnosisState | None = None,
    active_job_id: str | None = None,
    final_result: CandidateConclusion | None = None,
) -> Case:
    base = _base_case()
    return Case(
        case_id=base.case_id,
        status=status,
        case_revision=base.case_revision,
        raw_problem_text=base.raw_problem_text,
        diagnosis_state=diagnosis or base.diagnosis_state,
        active_job_id=active_job_id,
        selected_skill_ref=base.selected_skill_ref,
        final_result=final_result,
        failure=None,
        created_at=base.created_at,
        updated_at=LATER,
    )


def _aggregate(
    case: Case,
    *,
    jobs: dict[str, Job] | None = None,
    outcomes: dict[str, JobOutcome] | None = None,
    records: dict[str, OutcomeProcessingRecord] | None = None,
    attachments: dict[str, Attachment] | None = None,
    evidence: dict[str, Evidence] | None = None,
    artifacts: dict[str, Artifact] | None = None,
) -> CaseAggregate:
    # The helpers under test validate an application projection from the
    # supplied public snapshot.  model_construct lets negative tests express
    # corrupted cross-object graphs that StateFile's own validator rejects.
    return CaseAggregate.model_construct(
        case=case,
        jobs=jobs or {},
        outcomes=outcomes or {},
        outcome_processing_records=records or {},
        execution_failure_records={},
        attachments=attachments or {},
        evidence=evidence or {},
        artifacts=artifacts or {},
    )


def _state(aggregate: CaseAggregate) -> StateFile:
    base = _base_state()
    return StateFile.model_construct(
        schema_version=base.schema_version,
        contract_revision=base.contract_revision,
        generation=base.generation,
        installation_id=base.installation_id,
        created_at=base.created_at,
        updated_at=base.updated_at,
        runtime_epochs=[],
        cases={CASE_ID: aggregate},
        idempotency_records={},
    )


def _attachment(number: int, fill: str) -> Attachment:
    attachment_id = _uuid(number)
    return Attachment(
        attachment_id=attachment_id,
        case_id=CASE_ID,
        status=AttachmentStatus.READY,
        name=f"attachment-{number}.log",
        content_type="text/plain",
        declared_size=16,
        declared_sha256=fill * 64,
        size=16,
        sha256=fill * 64,
        storage_key=f"resources/cases/{CASE_ID}/attachments/{attachment_id}/payload",
        created_at=NOW,
        updated_at=LATER,
    )


def _diagnostic_artifact(number: int, creator: str, fill: str) -> Artifact:
    artifact_id = _uuid(number)
    return Artifact(
        artifact_id=artifact_id,
        case_id=CASE_ID,
        kind=ArtifactKind.DIAGNOSTIC_EXPORT,
        name=f"export-{number}.json",
        content_type="application/json",
        resource_kind=ResourceKind.FILE,
        size=32,
        sha256=fill * 64,
        storage_key=f"resources/cases/{CASE_ID}/artifacts/{artifact_id}/payload",
        metadata={
            "schema_version": 1,
            "format_id": "diagnostic-export-v1",
            "description": "A diagnostic export.",
        },
        created_by_job_id=creator,
        created_at=NOW,
    )


def _logparse_artifact(
    number: int,
    creator: Job,
    source_attachment: Attachment,
    fill: str,
) -> Artifact:
    artifact_id = _uuid(number)
    return Artifact(
        artifact_id=artifact_id,
        case_id=CASE_ID,
        kind=ArtifactKind.LOGPARSE_RUN,
        name="parsed-logs",
        content_type="application/vnd.problem-locator.logparse-run+directory",
        resource_kind=ResourceKind.DIRECTORY,
        size=64,
        sha256=fill * 64,
        storage_key=f"resources/cases/{CASE_ID}/artifacts/{artifact_id}/tree",
        metadata={
            "tree_manifest_sha256": fill * 64,
            "logparse_version_ref": creator.logparse_tool_ref,
            "parse_manifest_relative_path": "parse_manifest.json",
            "source_attachment_id": source_attachment.attachment_id,
            "source_attachment_sha256": source_attachment.sha256,
            "parse_parameters": {"product": creator.logparse_product},
        },
        created_by_job_id=creator.job_id,
        created_at=NOW,
    )


def _evidence(number: int, *, source_type: str, source_ref: str) -> Evidence:
    locator = {
        "ATTACHMENT": {
            "kind": "ATTACHMENT",
            "byte_start": None,
            "byte_end_exclusive": None,
        },
        "LOGPARSE": {
            "kind": "LOGPARSE",
            "relative_path": "targets/timeout.log",
            "start_line": 1,
            "end_line": 1,
            "start_time": None,
            "end_time": None,
        },
        "TOOL_OUTPUT": {
            "kind": "TOOL_OUTPUT",
            "relative_path": "summary.json",
            "json_pointer": "/cause",
        },
        "PREVIOUS_OUTCOME": {
            "kind": "PREVIOUS_OUTCOME",
            "json_pointer": "/payload",
        },
    }[source_type]
    return Evidence(
        evidence_id=_uuid(number),
        case_id=CASE_ID,
        source_type=source_type,
        source_ref=source_ref,
        locator=locator,
        summary=f"Evidence {number}.",
        collected_at=NOW,
        content_hash=None,
        resource_ref=None,
    )


def _outcome(number: int, job: Job, result_type: OutcomeResultType) -> JobOutcome:
    payload = _model("job-outcome-diagnosis.json", JobOutcome).model_dump(
        mode="python"
    )
    payload.update(
        outcome_id=_uuid(number),
        job_id=job.job_id,
        case_id=CASE_ID,
        job_type=job.job_type,
        base_state_revision=job.base_state_revision,
        result_type=result_type,
    )
    if result_type is OutcomeResultType.NEED_INPUT:
        payload["decision_audit"] = None
        payload["payload"]["requested_input"] = [_uuid(901)]
        payload["payload"]["requested_attachments"] = []
        payload["payload"]["candidate_conclusion_draft"] = None
        payload["proposed_artifacts"] = []
    elif result_type is OutcomeResultType.NEED_ATTACHMENT:
        payload["decision_audit"] = None
        payload["payload"]["requested_input"] = []
        payload["payload"]["requested_attachments"] = [_uuid(902)]
        payload["payload"]["candidate_conclusion_draft"] = None
        payload["proposed_artifacts"] = []
    else:
        payload["payload"]["requested_input"] = []
        payload["payload"]["requested_attachments"] = []
    return JobOutcome.model_validate(payload)


def _applied_record(outcome: JobOutcome) -> OutcomeProcessingRecord:
    digest = f"{int(outcome.outcome_id[-1], 16):x}" * 64
    digest = digest[:64]
    return OutcomeProcessingRecord(
        outcome_id=outcome.outcome_id,
        job_id=outcome.job_id,
        outcome_hash=digest,
        outcome_file_ref={
            "relative_key": f"jobs/{outcome.job_id}/job_outcome.json",
            "size": 1,
            "sha256": digest,
        },
        disposition=OutcomeDisposition.APPLIED,
        processed_at=LATER,
        error_code=None,
        accepted_evidence_ids=[],
        accepted_artifact_ids=[],
        created_job_id=None,
        reason="Applied for projection testing.",
    )


def _diagnosis_with(
    *,
    evidence_refs: list[str] | None = None,
    requirements: list[PendingRequirement] | None = None,
) -> DiagnosisState:
    payload = _base_case().diagnosis_state.model_dump(mode="python")
    payload["evidence_refs"] = list(evidence_refs or [])
    payload["pending_requirements"] = list(requirements or [])
    return DiagnosisState.model_validate(payload)


def test_artifact_downloadability_and_summary_hide_internal_storage() -> None:
    case = _case(status=CaseStatus.WAITING_INPUT)
    producer = _diagnose_job()
    diagnostic = _diagnostic_artifact(60, producer.job_id, "6")
    source = _attachment(50, "5")
    logparse = _logparse_artifact(61, producer, source, "7")

    diagnostic_summary = project_artifact_summary(case, diagnostic)
    logparse_summary = project_artifact_summary(case, logparse)

    assert diagnostic_summary.downloadable is True
    assert logparse_summary.downloadable is False
    assert "storage_key" not in diagnostic_summary.model_dump(mode="json")
    assert project_artifact_summaries(case, [logparse, diagnostic]) == [
        diagnostic_summary
    ]
    assert project_artifact_summaries(
        case, [logparse, diagnostic], include_internal=True
    ) == [diagnostic_summary, logparse_summary]


def test_user_result_is_downloadable_only_for_the_accepted_candidate_job() -> None:
    review_job = _model("job-review.json", Job)
    candidate_payload = review_job.context_snapshot.candidate_conclusion.model_dump(
        mode="python"
    )
    candidate_payload["status"] = "ACCEPTED"
    candidate = CandidateConclusion.model_validate(candidate_payload)
    diagnosis_payload = review_job.context_snapshot.model_dump(mode="python")
    diagnosis_payload["revision"] = diagnosis_payload.pop("diagnosis_state_revision")
    diagnosis_payload["candidate_conclusion"] = candidate
    diagnosis = DiagnosisState.model_validate(diagnosis_payload)
    resolved = _case(
        status=CaseStatus.RESOLVED,
        diagnosis=diagnosis,
        final_result=candidate,
    )
    user_result = Artifact(
        artifact_id=_uuid(70),
        case_id=CASE_ID,
        kind=ArtifactKind.USER_RESULT,
        name="diagnosis-result.json",
        content_type="application/json",
        resource_kind=ResourceKind.FILE,
        size=128,
        sha256="a" * 64,
        storage_key=f"resources/cases/{CASE_ID}/artifacts/{_uuid(70)}/payload",
        metadata={
            "schema_version": 3,
            "format_id": "problem-locator-diagnosis-v3",
            "description": "Accepted result.",
        },
        created_by_job_id=candidate.proposed_by_job_id,
        created_at=NOW,
    )

    assert is_artifact_downloadable(resolved, user_result) is True
    assert (
        is_artifact_downloadable(
            resolved,
            user_result.model_copy(update={"created_by_job_id": SECOND_JOB_ID}),
        )
        is False
    )
    assert is_artifact_downloadable(_case(status=CaseStatus.WAITING_INPUT), user_result) is False


def test_case_view_uses_one_snapshot_and_exposes_only_downloadable_artifacts() -> None:
    base = _base_state()
    aggregate = base.cases[CASE_ID]
    active = aggregate.jobs[aggregate.case.active_job_id]
    diagnostic = _diagnostic_artifact(60, active.job_id, "6")
    internal_source = _attachment(50, "5")
    internal_producer = _diagnose_job(
        attachment_refs=[internal_source.attachment_id]
    )
    internal = _logparse_artifact(61, internal_producer, internal_source, "7")
    projected = _state(
        _aggregate(
            aggregate.case,
            jobs={active.job_id: active, internal_producer.job_id: internal_producer},
            attachments={internal_source.attachment_id: internal_source},
            artifacts={diagnostic.artifact_id: diagnostic, internal.artifact_id: internal},
        )
    )

    view = project_case_view(projected, CASE_ID)

    assert view.case_revision == aggregate.case.case_revision
    assert view.diagnosis_state_revision == aggregate.case.diagnosis_state.revision
    assert view.active_job is not None
    assert view.active_job.job_id == aggregate.case.active_job_id
    assert [artifact.artifact_id for artifact in view.artifacts] == [
        diagnostic.artifact_id
    ]


def test_snapshot_indexes_replacements_and_selects_latest_unreplaced_interrupt() -> None:
    old = _diagnose_job(
        SOURCE_JOB_ID,
        status=JobStatus.INTERRUPTED,
        created_at="2026-07-31T00:01:00.000Z",
        finished_at="2026-07-31T00:02:00.000Z",
    )
    latest = _diagnose_job(
        SECOND_JOB_ID,
        status=JobStatus.INTERRUPTED,
        created_at="2026-07-31T00:03:00.000Z",
        finished_at="2026-07-31T00:04:00.000Z",
    )
    replacement = _diagnose_job(
        REPLACEMENT_JOB_ID,
        status=JobStatus.SUCCEEDED,
        replacement_for_job_id=old.job_id,
        created_at="2026-07-31T00:05:00.000Z",
        finished_at="2026-07-31T00:06:00.000Z",
    )
    state = _state(
        _aggregate(
            _case(status=CaseStatus.INTERRUPTED),
            jobs={job.job_id: job for job in (old, latest, replacement)},
        )
    )

    snapshot = build_case_snapshot(state, CASE_ID)

    assert snapshot.active_job is None
    assert snapshot.resume_source_job == latest
    assert snapshot.replacement_job_ids_by_source == {
        old.job_id: replacement.job_id
    }


def test_outcome_continuation_builds_stable_validated_dependency_closure() -> None:
    log_source = _attachment(50, "1")
    fulfilled = _attachment(51, "2")
    direct_source = _attachment(52, "3")
    fixed_export = _diagnostic_artifact(60, SOURCE_JOB_ID, "4")
    logparse = _logparse_artifact(61, _diagnose_job(attachment_refs=[log_source.attachment_id]), log_source, "5")

    source_prior = _outcome(21, _diagnose_job(), OutcomeResultType.COMPLETED)
    evidence_prior = _outcome(22, _diagnose_job(), OutcomeResultType.COMPLETED)
    evidence = {
        item.evidence_id: item
        for item in (
            _evidence(40, source_type="ATTACHMENT", source_ref=direct_source.attachment_id),
            _evidence(41, source_type="LOGPARSE", source_ref=logparse.artifact_id),
            _evidence(42, source_type="TOOL_OUTPUT", source_ref=fixed_export.artifact_id),
            _evidence(43, source_type="PREVIOUS_OUTCOME", source_ref=evidence_prior.outcome_id),
        )
    }
    fulfilled_requirement = PendingRequirement(
        requirement_id=_uuid(90),
        kind="ATTACHMENT",
        name="log_archive",
        prompt="Provide the log archive.",
        required=True,
        constraints={
            "allowed_content_types": ["text/plain"],
            "min_count": 1,
            "max_count": 1,
        },
        status="FULFILLED",
        requested_by_job_id=SOURCE_JOB_ID,
        fulfilled_by_refs=[fulfilled.attachment_id],
    )
    diagnosis = _diagnosis_with(
        evidence_refs=list(evidence), requirements=[fulfilled_requirement]
    )
    source = _diagnose_job(
        evidence_refs=[_uuid(40), _uuid(41)],
        attachment_refs=[log_source.attachment_id],
        artifact_refs=[fixed_export.artifact_id],
        previous_outcome_refs=[source_prior.outcome_id],
    )
    incoming = _outcome(23, source, OutcomeResultType.COMPLETED)
    outcomes = {
        source_prior.outcome_id: source_prior,
        evidence_prior.outcome_id: evidence_prior,
    }
    records = {
        outcome_id: _applied_record(outcome)
        for outcome_id, outcome in outcomes.items()
    }
    state = _state(
        _aggregate(
            _case(status=CaseStatus.RUNNING, diagnosis=diagnosis, active_job_id=source.job_id),
            jobs={source.job_id: source},
            outcomes=outcomes,
            records=records,
            attachments={
                item.attachment_id: item
                for item in (log_source, fulfilled, direct_source)
            },
            evidence=evidence,
            artifacts={
                fixed_export.artifact_id: fixed_export,
                logparse.artifact_id: logparse,
            },
        )
    )

    continuation = continuation_for_outcome(state, incoming)

    assert continuation.evidence_refs == list(evidence)
    assert continuation.attachment_refs == [
        log_source.attachment_id,
        fulfilled.attachment_id,
        direct_source.attachment_id,
    ]
    assert continuation.artifact_refs == [
        fixed_export.artifact_id,
        logparse.artifact_id,
    ]
    assert continuation.previous_outcome_refs == [
        incoming.outcome_id,
        source_prior.outcome_id,
        evidence_prior.outcome_id,
    ]


def test_outcome_continuation_rejects_missing_or_wrong_evidence_dependency() -> None:
    source = _diagnose_job(evidence_refs=[_uuid(40)])
    evidence = _evidence(40, source_type="ATTACHMENT", source_ref=_uuid(50))
    diagnosis = _diagnosis_with(evidence_refs=[evidence.evidence_id])
    incoming = _outcome(23, source, OutcomeResultType.COMPLETED)
    state = _state(
        _aggregate(
            _case(status=CaseStatus.RUNNING, diagnosis=diagnosis, active_job_id=source.job_id),
            jobs={source.job_id: source},
            evidence={evidence.evidence_id: evidence},
        )
    )

    with pytest.raises(ValueError, match="missing Attachment"):
        continuation_for_outcome(state, incoming)


def test_supplement_continuation_starts_with_unique_applied_waiting_outcome() -> None:
    source = _diagnose_job(status=JobStatus.SUCCEEDED)
    prior = _outcome(21, source, OutcomeResultType.COMPLETED)
    waiting = _outcome(22, source, OutcomeResultType.NEED_INPUT)
    source = _diagnose_job(
        status=JobStatus.SUCCEEDED,
        previous_outcome_refs=[prior.outcome_id],
    )
    requirement = PendingRequirement(
        requirement_id=_uuid(90),
        kind="INPUT",
        name="order_id",
        prompt="Provide the order ID.",
        required=True,
        constraints={
            "value_type": "STRING",
            "min_utf8_bytes": 1,
            "max_utf8_bytes": 64,
            "pattern": None,
            "allowed_values": [],
        },
        status="OPEN",
        requested_by_job_id=source.job_id,
        fulfilled_by_refs=[],
    )
    outcomes = {prior.outcome_id: prior, waiting.outcome_id: waiting}
    state = _state(
        _aggregate(
            _case(
                status=CaseStatus.WAITING_INPUT,
                diagnosis=_diagnosis_with(requirements=[requirement]),
            ),
            jobs={source.job_id: source},
            outcomes=outcomes,
            records={key: _applied_record(value) for key, value in outcomes.items()},
        )
    )

    continuation = continuation_for_supplement(
        state,
        CASE_ID,
        ready_attachment_ids=[],
    )

    assert continuation.previous_outcome_refs == [
        waiting.outcome_id,
        prior.outcome_id,
    ]

    # A first supplement may satisfy the remaining INPUT requirements and
    # leave the Case waiting for an ATTACHMENT without creating a new Outcome.
    # The original NEED_INPUT Outcome remains the unique waiting anchor.
    attachment_requirement = PendingRequirement(
        requirement_id=_uuid(91),
        kind="ATTACHMENT",
        name="server_log",
        prompt="Provide the server log.",
        required=True,
        constraints={
            "allowed_content_types": ["text/plain"],
            "min_count": 1,
            "max_count": 1,
        },
        status="OPEN",
        requested_by_job_id=source.job_id,
        fulfilled_by_refs=[],
    )
    continued_state = _state(
        _aggregate(
            _case(
                status=CaseStatus.WAITING_ATTACHMENT,
                diagnosis=_diagnosis_with(requirements=[attachment_requirement]),
            ),
            jobs={source.job_id: source},
            outcomes=outcomes,
            records={key: _applied_record(value) for key, value in outcomes.items()},
        )
    )

    assert continuation_for_supplement(
        continued_state,
        CASE_ID,
        ready_attachment_ids=[],
    ).previous_outcome_refs == [waiting.outcome_id, prior.outcome_id]


def test_supplement_continuation_inserts_current_ready_attachments_in_requirement_order() -> None:
    source_attachment = _attachment(59, "1")
    prior_requirement_attachment = _attachment(58, "2")
    current_first = _attachment(57, "3")
    current_second = _attachment(55, "4")
    later_requirement_attachment = _attachment(56, "5")
    evidence_attachment = _attachment(54, "6")
    evidence = _evidence(
        40,
        source_type="ATTACHMENT",
        source_ref=evidence_attachment.attachment_id,
    )
    source = _diagnose_job(status=JobStatus.SUCCEEDED)
    prior = _outcome(21, source, OutcomeResultType.COMPLETED)
    waiting = _outcome(22, source, OutcomeResultType.NEED_ATTACHMENT)
    source = _diagnose_job(
        status=JobStatus.SUCCEEDED,
        attachment_refs=[source_attachment.attachment_id],
        previous_outcome_refs=[prior.outcome_id],
    )
    requirements = [
        PendingRequirement(
            requirement_id=_uuid(900),
            kind="ATTACHMENT",
            name="earlier_log",
            prompt="Previously supplied log.",
            required=True,
            constraints={
                "allowed_content_types": ["text/plain"],
                "min_count": 1,
                "max_count": 1,
            },
            status="FULFILLED",
            requested_by_job_id=source.job_id,
            fulfilled_by_refs=[prior_requirement_attachment.attachment_id],
        ),
        PendingRequirement(
            requirement_id=_uuid(902),
            kind="ATTACHMENT",
            name="current_logs",
            prompt="Supply the current logs.",
            required=True,
            constraints={
                "allowed_content_types": ["text/plain"],
                "min_count": 1,
                "max_count": 4,
            },
            status="OPEN",
            requested_by_job_id=source.job_id,
            fulfilled_by_refs=[],
        ),
        PendingRequirement(
            requirement_id=_uuid(901),
            kind="ATTACHMENT",
            name="later_log",
            prompt="Previously supplied later log.",
            required=True,
            constraints={
                "allowed_content_types": ["text/plain"],
                "min_count": 1,
                "max_count": 1,
            },
            status="FULFILLED",
            requested_by_job_id=source.job_id,
            fulfilled_by_refs=[later_requirement_attachment.attachment_id],
        ),
    ]
    attachments = {
        attachment.attachment_id: attachment
        for attachment in (
            source_attachment,
            prior_requirement_attachment,
            current_first,
            current_second,
            later_requirement_attachment,
            evidence_attachment,
        )
    }
    outcomes = {prior.outcome_id: prior, waiting.outcome_id: waiting}
    aggregate = _aggregate(
        _case(
            status=CaseStatus.WAITING_ATTACHMENT,
            diagnosis=_diagnosis_with(
                evidence_refs=[evidence.evidence_id],
                requirements=requirements,
            ),
        ),
        jobs={source.job_id: source},
        outcomes=outcomes,
        records={key: _applied_record(value) for key, value in outcomes.items()},
        attachments=attachments,
        evidence={evidence.evidence_id: evidence},
    )
    state = _state(aggregate)

    continuation = continuation_for_supplement(
        state,
        CASE_ID,
        ready_attachment_ids=[
            current_first.attachment_id,
            source_attachment.attachment_id,
            current_second.attachment_id,
            current_first.attachment_id,
        ],
    )

    assert continuation.attachment_refs == [
        source_attachment.attachment_id,
        prior_requirement_attachment.attachment_id,
        current_first.attachment_id,
        current_second.attachment_id,
        later_requirement_attachment.attachment_id,
        evidence_attachment.attachment_id,
    ]
    assert continuation.previous_outcome_refs == [
        waiting.outcome_id,
        prior.outcome_id,
    ]

    for invalid_attachments, message in (
        (
            {
                key: value
                for key, value in attachments.items()
                if key != current_first.attachment_id
            },
            "missing Attachment",
        ),
        (
            {
                **attachments,
                current_first.attachment_id: current_first.model_copy(
                    update={
                        "status": AttachmentStatus.UPLOADING,
                        "size": None,
                        "sha256": None,
                        "storage_key": None,
                    }
                ),
            },
            "not a complete READY resource",
        ),
    ):
        invalid_state = _state(
            aggregate.model_copy(update={"attachments": invalid_attachments})
        )
        with pytest.raises(ValueError, match=message):
            continuation_for_supplement(
                invalid_state,
                CASE_ID,
                ready_attachment_ids=[current_first.attachment_id],
            )


def test_supplement_continuation_rejects_requirements_from_different_jobs() -> None:
    first = _diagnose_job(status=JobStatus.SUCCEEDED)
    second = _diagnose_job(SECOND_JOB_ID, status=JobStatus.SUCCEEDED)
    requirements = []
    for number, name, job_id in (
        (90, "order_id", first.job_id),
        (91, "trace_id", second.job_id),
    ):
        requirements.append(
            PendingRequirement(
                requirement_id=_uuid(number),
                kind="INPUT",
                name=name,
                prompt=f"Provide {name}.",
                required=True,
                constraints={
                    "value_type": "STRING",
                    "min_utf8_bytes": 1,
                    "max_utf8_bytes": 64,
                    "pattern": None,
                    "allowed_values": [],
                },
                status="OPEN",
                requested_by_job_id=job_id,
                fulfilled_by_refs=[],
            )
        )
    state = _state(
        _aggregate(
            _case(
                status=CaseStatus.WAITING_INPUT,
                diagnosis=_diagnosis_with(requirements=requirements),
            ),
            jobs={first.job_id: first, second.job_id: second},
        )
    )

    with pytest.raises(ValueError, match="share one waiting source Job"):
        continuation_for_supplement(
            state,
            CASE_ID,
            ready_attachment_ids=[],
        )


def test_resume_continuation_is_an_exact_copy_and_validates_its_closure() -> None:
    attachment = _attachment(50, "1")
    evidence = _evidence(40, source_type="ATTACHMENT", source_ref=attachment.attachment_id)
    prior_source = _diagnose_job()
    prior = _outcome(21, prior_source, OutcomeResultType.COMPLETED)
    interrupted = _diagnose_job(
        status=JobStatus.INTERRUPTED,
        evidence_refs=[evidence.evidence_id],
        attachment_refs=[attachment.attachment_id],
        previous_outcome_refs=[prior.outcome_id],
    )
    diagnosis = _diagnosis_with(evidence_refs=[evidence.evidence_id])
    state = _state(
        _aggregate(
            _case(status=CaseStatus.INTERRUPTED, diagnosis=diagnosis),
            jobs={interrupted.job_id: interrupted},
            outcomes={prior.outcome_id: prior},
            records={prior.outcome_id: _applied_record(prior)},
            attachments={attachment.attachment_id: attachment},
            evidence={evidence.evidence_id: evidence},
        )
    )

    continuation = continuation_for_resume(state, CASE_ID)

    assert continuation.evidence_refs == interrupted.evidence_refs
    assert continuation.attachment_refs == interrupted.attachment_refs
    assert continuation.artifact_refs == interrupted.artifact_refs
    assert continuation.previous_outcome_refs == interrupted.previous_outcome_refs

    broken = interrupted.model_copy(update={"attachment_refs": []})
    broken_state = _state(
        _aggregate(
            _case(status=CaseStatus.INTERRUPTED, diagnosis=diagnosis),
            jobs={broken.job_id: broken},
            outcomes={prior.outcome_id: prior},
            records={prior.outcome_id: _applied_record(prior)},
            attachments={attachment.attachment_id: attachment},
            evidence={evidence.evidence_id: evidence},
        )
    )
    with pytest.raises(ValueError, match="omits an Evidence Attachment"):
        continuation_for_resume(broken_state, CASE_ID)
