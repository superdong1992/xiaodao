"""Pure, snapshot-consistent application projections.

This module deliberately consumes only the frozen S00 public contract.  It
does not read a repository or a resource store: callers pass one ``StateFile``
snapshot and every object used by a projection is resolved from that snapshot.
The ``ValueError`` exceptions are internal validation signals; command/query
services map them to the public typed error contract at their boundary.
"""

from __future__ import annotations

from collections.abc import Iterable

from problem_locator.contracts import (
    Artifact,
    ArtifactKind,
    ArtifactSummary,
    Attachment,
    AttachmentStatus,
    Case,
    CaseAggregate,
    CaseSnapshot,
    CaseStatus,
    CaseView,
    ContinuationResourceView,
    DiagnosisProvenanceType,
    Evidence,
    EvidenceSourceType,
    Job,
    JobOutcome,
    JobStatus,
    JobSummary,
    LogparseRunMetadata,
    OutcomeDisposition,
    OutcomeResultType,
    RequirementKind,
    RequirementStatus,
    StateFile,
)


def is_artifact_downloadable(case: Case, artifact: Artifact) -> bool:
    """Return the S00/S03 download decision for one formal Artifact."""

    if artifact.case_id != case.case_id:
        raise ValueError("Artifact belongs to a different Case")
    if artifact.kind is ArtifactKind.DIAGNOSTIC_EXPORT:
        return True
    if artifact.kind is ArtifactKind.LOGPARSE_RUN:
        return False
    if artifact.kind is not ArtifactKind.USER_RESULT:
        raise ValueError("Artifact has an unsupported kind")
    return (
        case.status is CaseStatus.RESOLVED
        and case.final_result is not None
        and artifact.created_by_job_id == case.final_result.proposed_by_job_id
    )


def project_artifact_summary(case: Case, artifact: Artifact) -> ArtifactSummary:
    """Project a formal Artifact without exposing its storage key or metadata."""

    return ArtifactSummary(
        artifact_id=artifact.artifact_id,
        kind=artifact.kind,
        name=artifact.name,
        content_type=artifact.content_type,
        resource_kind=artifact.resource_kind,
        size=artifact.size,
        sha256=artifact.sha256,
        created_by_job_id=artifact.created_by_job_id,
        created_at=artifact.created_at,
        downloadable=is_artifact_downloadable(case, artifact),
    )


def project_artifact_summaries(
    case: Case,
    artifacts: Iterable[Artifact],
    *,
    include_internal: bool = False,
) -> list[ArtifactSummary]:
    """Project Artifacts in a stable order, optionally retaining internal ones."""

    summaries = [project_artifact_summary(case, artifact) for artifact in artifacts]
    summaries.sort(key=lambda item: (item.created_at, item.artifact_id))
    if include_internal:
        return summaries
    return [summary for summary in summaries if summary.downloadable]


def project_case_view(state: StateFile, case_id: str) -> CaseView:
    """Build the public current Case projection from exactly one StateFile."""

    aggregate = _case_aggregate(state, case_id)
    return project_case_components(
        aggregate.case,
        _active_job(aggregate),
        aggregate.artifacts.values(),
    )


def project_case_components(
    case: Case,
    active_job: Job | None,
    artifacts: Iterable[Artifact],
) -> CaseView:
    """Project a fully materialized target Case without another state read."""

    if (active_job is None) != (case.active_job_id is None):
        raise ValueError("Case and active Job projection inputs disagree")
    if active_job is not None and (
        active_job.case_id != case.case_id
        or active_job.job_id != case.active_job_id
    ):
        raise ValueError("active Job projection input does not match the Case")
    return CaseView(
        case_id=case.case_id,
        status=case.status,
        case_revision=case.case_revision,
        diagnosis_state_revision=case.diagnosis_state.revision,
        problem_spec=case.diagnosis_state.problem_spec,
        user_facts=list(case.diagnosis_state.user_facts),
        confirmed_facts=list(case.diagnosis_state.confirmed_facts),
        open_questions=list(case.diagnosis_state.open_questions),
        pending_requirements=list(case.diagnosis_state.pending_requirements),
        active_job=None if active_job is None else _job_summary(active_job),
        selected_skill_ref=case.selected_skill_ref,
        final_result=case.final_result,
        failure=case.failure,
        artifacts=project_artifact_summaries(case, artifacts),
        created_at=case.created_at,
        updated_at=case.updated_at,
    )


def build_case_snapshot(state: StateFile, case_id: str) -> CaseSnapshot:
    """Build Coordinator input, including the deterministic Resume source/index."""

    aggregate = _case_aggregate(state, case_id)
    replacements: dict[str, str] = {}
    for replacement in aggregate.jobs.values():
        source_id = replacement.replacement_for_job_id
        if source_id is None:
            continue
        source = aggregate.jobs.get(source_id)
        if source is None or source.status is not JobStatus.INTERRUPTED:
            raise ValueError("replacement_for_job_id does not resolve an INTERRUPTED Job")
        existing = replacements.get(source_id)
        if existing is not None and existing != replacement.job_id:
            raise ValueError("an INTERRUPTED Job has more than one replacement")
        replacements[source_id] = replacement.job_id

    resume_source: Job | None = None
    if aggregate.case.status is CaseStatus.INTERRUPTED:
        candidates = [
            job
            for job in aggregate.jobs.values()
            if job.status is JobStatus.INTERRUPTED and job.job_id not in replacements
        ]
        if candidates:
            # Terminal Jobs always have finished_at.  The extra keys make the
            # choice deterministic even for a corrupt/imported timestamp tie.
            resume_source = max(
                candidates,
                key=lambda job: (job.finished_at or job.created_at, job.created_at, job.job_id),
            )

    return CaseSnapshot(
        case=aggregate.case,
        active_job=_active_job(aggregate),
        resume_source_job=resume_source,
        replacement_job_ids_by_source={
            source_id: replacements[source_id] for source_id in sorted(replacements)
        },
    )


def empty_continuation_resources() -> ContinuationResourceView:
    """Return the fixed empty continuation used by CREATE/control triggers."""

    return ContinuationResourceView(
        evidence_refs=[],
        attachment_refs=[],
        artifact_refs=[],
        previous_outcome_refs=[],
    )


def continuation_for_outcome(
    state: StateFile,
    incoming_outcome: JobOutcome,
) -> ContinuationResourceView:
    """Build an Outcome continuation with the incoming Outcome first.

    The incoming Outcome is the sole object allowed not to exist in ``state``;
    all other resources and dependency edges are resolved from that snapshot.
    """

    aggregate = _case_aggregate(state, incoming_outcome.case_id)
    source_job = _source_job(aggregate, incoming_outcome.job_id)
    if source_job.job_type is not incoming_outcome.job_type:
        raise ValueError("incoming Outcome job_type does not match its source Job")
    if source_job.base_state_revision != incoming_outcome.base_state_revision:
        raise ValueError("incoming Outcome base revision does not match its source Job")
    existing = aggregate.outcomes.get(incoming_outcome.outcome_id)
    if existing is not None and existing != incoming_outcome:
        raise ValueError("incoming Outcome ID is already bound to different content")

    return _continuation_closure(
        aggregate,
        source_job,
        leading_previous_outcome_ids=[incoming_outcome.outcome_id],
        allowed_unsaved_outcome_id=incoming_outcome.outcome_id,
    )


def continuation_for_supplement(
    state: StateFile,
    case_id: str,
) -> ContinuationResourceView:
    """Build the continuation anchored by the current waiting Outcome."""

    aggregate = _case_aggregate(state, case_id)
    case = aggregate.case
    if case.status not in {
        CaseStatus.WAITING_INPUT,
        CaseStatus.WAITING_ATTACHMENT,
    }:
        raise ValueError("SubmitSupplement continuation requires a waiting Case")

    open_requirements = [
        requirement
        for requirement in case.diagnosis_state.pending_requirements
        if requirement.status is RequirementStatus.OPEN
    ]
    if not open_requirements:
        raise ValueError("waiting Case has no OPEN requirement")
    source_ids = {requirement.requested_by_job_id for requirement in open_requirements}
    if len(source_ids) != 1:
        raise ValueError("OPEN requirements do not share one waiting source Job")
    source_job = _source_job(aggregate, next(iter(source_ids)))

    waiting_outcome_ids = [
        outcome.outcome_id
        for outcome in aggregate.outcomes.values()
        if outcome.job_id == source_job.job_id
        and outcome.result_type
        in {OutcomeResultType.NEED_INPUT, OutcomeResultType.NEED_ATTACHMENT}
        and _outcome_is_applied(aggregate, outcome.outcome_id)
    ]
    if len(waiting_outcome_ids) != 1:
        raise ValueError("waiting source Job must have exactly one APPLIED waiting Outcome")

    return _continuation_closure(
        aggregate,
        source_job,
        leading_previous_outcome_ids=waiting_outcome_ids,
    )


def continuation_for_resume(
    state: StateFile,
    case_id: str,
) -> ContinuationResourceView:
    """Validate and copy the selected interrupted Job's four arrays exactly."""

    aggregate = _case_aggregate(state, case_id)
    source_job = build_case_snapshot(state, case_id).resume_source_job
    if source_job is None:
        raise ValueError("INTERRUPTED Case has no resumable source Job")

    view = ContinuationResourceView(
        evidence_refs=list(source_job.evidence_refs),
        attachment_refs=list(source_job.attachment_refs),
        artifact_refs=list(source_job.artifact_refs),
        previous_outcome_refs=list(source_job.previous_outcome_refs),
    )
    _validate_exact_job_continuation(aggregate, source_job, view)
    return view


def _case_aggregate(state: StateFile, case_id: str) -> CaseAggregate:
    aggregate = state.cases.get(case_id)
    if aggregate is None:
        raise ValueError("Case does not exist in the supplied StateFile")
    if aggregate.case.case_id != case_id:
        raise ValueError("StateFile Case key does not match the aggregate")
    return aggregate


def _active_job(aggregate: CaseAggregate) -> Job | None:
    active_job_id = aggregate.case.active_job_id
    if active_job_id is None:
        return None
    active_job = aggregate.jobs.get(active_job_id)
    if active_job is None or active_job.case_id != aggregate.case.case_id:
        raise ValueError("Case.active_job_id does not resolve in the same snapshot")
    if active_job.status not in {JobStatus.PENDING, JobStatus.RUNNING}:
        raise ValueError("Case.active_job_id does not resolve a live Job")
    return active_job


def _job_summary(job: Job) -> JobSummary:
    return JobSummary(
        job_id=job.job_id,
        job_type=job.job_type,
        status=job.status,
        goal=job.goal,
        base_state_revision=job.base_state_revision,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _source_job(aggregate: CaseAggregate, job_id: str) -> Job:
    job = aggregate.jobs.get(job_id)
    if job is None:
        raise ValueError("continuation source Job does not exist")
    if job.case_id != aggregate.case.case_id:
        raise ValueError("continuation source Job belongs to a different Case")
    return job


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _continuation_closure(
    aggregate: CaseAggregate,
    source_job: Job,
    *,
    leading_previous_outcome_ids: list[str],
    allowed_unsaved_outcome_id: str | None = None,
) -> ContinuationResourceView:
    evidence_ids = list(aggregate.case.diagnosis_state.evidence_refs)
    attachment_ids = list(source_job.attachment_refs)
    artifact_ids = list(source_job.artifact_refs)
    previous_outcome_ids: list[str] = []
    for outcome_id in leading_previous_outcome_ids:
        _append_unique(previous_outcome_ids, outcome_id)
    for outcome_id in source_job.previous_outcome_refs:
        _append_unique(previous_outcome_ids, outcome_id)

    diagnosis_evidence = set(evidence_ids)
    if any(evidence_id not in diagnosis_evidence for evidence_id in source_job.evidence_refs):
        raise ValueError("source Job Evidence is absent from the current DiagnosisState")

    for attachment_id in source_job.attachment_refs:
        _ready_attachment(aggregate, attachment_id)
    for artifact_id in source_job.artifact_refs:
        _formal_artifact(aggregate, artifact_id)
    for outcome_id in source_job.previous_outcome_refs:
        _applied_outcome(aggregate, outcome_id)

    for requirement in aggregate.case.diagnosis_state.pending_requirements:
        if (
            requirement.kind is RequirementKind.ATTACHMENT
            and requirement.status is RequirementStatus.FULFILLED
        ):
            for attachment_id in requirement.fulfilled_by_refs:
                _ready_attachment(aggregate, attachment_id)
                _append_unique(attachment_ids, attachment_id)

    for evidence_id in evidence_ids:
        evidence = _formal_evidence(aggregate, evidence_id)
        _append_evidence_dependencies(
            aggregate,
            evidence,
            attachment_ids=attachment_ids,
            artifact_ids=artifact_ids,
            previous_outcome_ids=previous_outcome_ids,
        )

    for outcome_id in previous_outcome_ids:
        if outcome_id == allowed_unsaved_outcome_id and outcome_id not in aggregate.outcomes:
            continue
        _applied_outcome(aggregate, outcome_id)

    return ContinuationResourceView(
        evidence_refs=evidence_ids,
        attachment_refs=attachment_ids,
        artifact_refs=artifact_ids,
        previous_outcome_refs=previous_outcome_ids,
    )


def _validate_exact_job_continuation(
    aggregate: CaseAggregate,
    source_job: Job,
    view: ContinuationResourceView,
) -> None:
    evidence_set = set(view.evidence_refs)
    attachment_set = set(view.attachment_refs)
    artifact_set = set(view.artifact_refs)
    outcome_set = set(view.previous_outcome_refs)

    for attachment_id in view.attachment_refs:
        _ready_attachment(aggregate, attachment_id)
    for artifact_id in view.artifact_refs:
        _formal_artifact(aggregate, artifact_id)
    for outcome_id in view.previous_outcome_refs:
        _applied_outcome(aggregate, outcome_id)
    for evidence_id in view.evidence_refs:
        evidence = _formal_evidence(aggregate, evidence_id)
        if evidence.source_type is EvidenceSourceType.ATTACHMENT:
            _ready_attachment(aggregate, evidence.source_ref)
            if evidence.source_ref not in attachment_set:
                raise ValueError("Resume source omits an Evidence Attachment dependency")
        elif evidence.source_type is EvidenceSourceType.LOGPARSE:
            artifact = _formal_artifact(
                aggregate,
                evidence.source_ref,
                expected_kind=ArtifactKind.LOGPARSE_RUN,
            )
            if artifact.artifact_id not in artifact_set:
                raise ValueError("Resume source omits a LOGPARSE_RUN dependency")
            source_attachment_id = _logparse_source_attachment(aggregate, artifact)
            if source_attachment_id not in attachment_set:
                raise ValueError("Resume source omits the LOGPARSE_RUN source Attachment")
        elif evidence.source_type is EvidenceSourceType.TOOL_OUTPUT:
            artifact = _formal_artifact(
                aggregate,
                evidence.source_ref,
                expected_kind=ArtifactKind.DIAGNOSTIC_EXPORT,
            )
            if artifact.artifact_id not in artifact_set:
                raise ValueError("Resume source omits a TOOL_OUTPUT Artifact dependency")
        elif evidence.source_type is EvidenceSourceType.PREVIOUS_OUTCOME:
            _applied_outcome(aggregate, evidence.source_ref)
            if evidence.source_ref not in outcome_set:
                raise ValueError("Resume source omits a PREVIOUS_OUTCOME dependency")
        elif evidence.source_type is EvidenceSourceType.USER_FACT:
            _user_fact_source(aggregate, evidence)
        else:
            raise ValueError("Evidence has an unsupported source type")

    if evidence_set != set(source_job.evidence_refs):
        raise ValueError("Resume continuation changed source Job Evidence")


def _formal_evidence(aggregate: CaseAggregate, evidence_id: str) -> Evidence:
    evidence = aggregate.evidence.get(evidence_id)
    if evidence is None:
        raise ValueError("continuation references missing Evidence")
    if evidence.case_id != aggregate.case.case_id:
        raise ValueError("Evidence belongs to a different Case")
    if evidence.resource_ref is not None:
        if evidence.resource_ref.size < 0 or len(evidence.resource_ref.sha256) != 64:
            raise ValueError("Evidence ResourceRef is invalid")
        if (
            evidence.content_hash is not None
            and evidence.content_hash != evidence.resource_ref.sha256
        ):
            raise ValueError("Evidence content hash does not match its ResourceRef")
    return evidence


def _ready_attachment(aggregate: CaseAggregate, attachment_id: str) -> Attachment:
    attachment = aggregate.attachments.get(attachment_id)
    if attachment is None:
        raise ValueError("continuation references missing Attachment")
    if attachment.case_id != aggregate.case.case_id:
        raise ValueError("Attachment belongs to a different Case")
    if (
        attachment.status is not AttachmentStatus.READY
        or attachment.storage_key is None
        or attachment.size is None
        or attachment.sha256 is None
    ):
        raise ValueError("continuation Attachment is not a complete READY resource")
    return attachment


def _formal_artifact(
    aggregate: CaseAggregate,
    artifact_id: str,
    *,
    expected_kind: ArtifactKind | None = None,
) -> Artifact:
    artifact = aggregate.artifacts.get(artifact_id)
    if artifact is None:
        raise ValueError("continuation references missing Artifact")
    if artifact.case_id != aggregate.case.case_id:
        raise ValueError("Artifact belongs to a different Case")
    if expected_kind is not None and artifact.kind is not expected_kind:
        raise ValueError("Artifact kind does not match its Evidence source")
    if artifact.size < 0 or len(artifact.sha256) != 64:
        raise ValueError("Artifact resource metadata is invalid")
    if artifact.kind is ArtifactKind.LOGPARSE_RUN:
        _logparse_source_attachment(aggregate, artifact)
    return artifact


def _logparse_source_attachment(aggregate: CaseAggregate, artifact: Artifact) -> str:
    if artifact.kind is not ArtifactKind.LOGPARSE_RUN or not isinstance(
        artifact.metadata, LogparseRunMetadata
    ):
        raise ValueError("LOGPARSE Evidence source is not a LOGPARSE_RUN Artifact")
    metadata = artifact.metadata
    attachment = _ready_attachment(aggregate, metadata.source_attachment_id)
    producer = aggregate.jobs.get(artifact.created_by_job_id)
    if producer is None or producer.case_id != aggregate.case.case_id:
        raise ValueError("LOGPARSE_RUN producing Job does not resolve")
    if (
        metadata.source_attachment_id not in producer.attachment_refs
        or attachment.sha256 != metadata.source_attachment_sha256
        or producer.logparse_tool_ref != metadata.logparse_version_ref
        or producer.logparse_product != metadata.parse_parameters.product
        or metadata.tree_manifest_sha256 != artifact.sha256
    ):
        raise ValueError("LOGPARSE_RUN reverse metadata is inconsistent")
    return attachment.attachment_id


def _applied_outcome(aggregate: CaseAggregate, outcome_id: str) -> JobOutcome:
    outcome = aggregate.outcomes.get(outcome_id)
    if outcome is None:
        raise ValueError("continuation references missing previous Outcome")
    if outcome.case_id != aggregate.case.case_id:
        raise ValueError("previous Outcome belongs to a different Case")
    if not _outcome_is_applied(aggregate, outcome_id):
        raise ValueError("previous Outcome is not a formal APPLIED Outcome")
    return outcome


def _outcome_is_applied(aggregate: CaseAggregate, outcome_id: str) -> bool:
    record = aggregate.outcome_processing_records.get(outcome_id)
    return (
        record is not None
        and record.outcome_id == outcome_id
        and record.disposition is OutcomeDisposition.APPLIED
        and outcome_id in aggregate.outcomes
        and record.job_id == aggregate.outcomes[outcome_id].job_id
    )


def _user_fact_source(aggregate: CaseAggregate, evidence: Evidence) -> None:
    source = next(
        (
            item
            for item in aggregate.case.diagnosis_state.user_facts
            if item.item_id == evidence.source_ref
        ),
        None,
    )
    if (
        source is None
        or source.provenance.source_type is not DiagnosisProvenanceType.USER_INPUT
        or getattr(evidence.locator, "input_name", None) != source.provenance.input_name
    ):
        raise ValueError("USER_FACT Evidence does not resolve its provenance")


def _append_evidence_dependencies(
    aggregate: CaseAggregate,
    evidence: Evidence,
    *,
    attachment_ids: list[str],
    artifact_ids: list[str],
    previous_outcome_ids: list[str],
) -> None:
    if evidence.source_type is EvidenceSourceType.USER_FACT:
        _user_fact_source(aggregate, evidence)
    elif evidence.source_type is EvidenceSourceType.ATTACHMENT:
        attachment = _ready_attachment(aggregate, evidence.source_ref)
        _append_unique(attachment_ids, attachment.attachment_id)
    elif evidence.source_type is EvidenceSourceType.LOGPARSE:
        artifact = _formal_artifact(
            aggregate,
            evidence.source_ref,
            expected_kind=ArtifactKind.LOGPARSE_RUN,
        )
        _append_unique(artifact_ids, artifact.artifact_id)
        _append_unique(attachment_ids, _logparse_source_attachment(aggregate, artifact))
    elif evidence.source_type is EvidenceSourceType.TOOL_OUTPUT:
        artifact = _formal_artifact(
            aggregate,
            evidence.source_ref,
            expected_kind=ArtifactKind.DIAGNOSTIC_EXPORT,
        )
        _append_unique(artifact_ids, artifact.artifact_id)
    elif evidence.source_type is EvidenceSourceType.PREVIOUS_OUTCOME:
        outcome = _applied_outcome(aggregate, evidence.source_ref)
        _append_unique(previous_outcome_ids, outcome.outcome_id)
    else:
        raise ValueError("Evidence has an unsupported source type")


__all__ = [
    "build_case_snapshot",
    "continuation_for_outcome",
    "continuation_for_resume",
    "continuation_for_supplement",
    "empty_continuation_resources",
    "is_artifact_downloadable",
    "project_artifact_summaries",
    "project_artifact_summary",
    "project_case_components",
    "project_case_view",
]
