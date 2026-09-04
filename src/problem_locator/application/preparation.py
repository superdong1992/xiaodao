"""Deterministic DTO preparation for S03 command handling.

This module contains the parts of the command pipeline that do not touch a
Port.  Keeping allocation outside these helpers lets the application service
preallocate IDs and timestamps once and reuse them across conditional-commit
retries, as required by the S03 consistency contract.
"""

from __future__ import annotations

from collections.abc import Sequence

from problem_locator.contracts import (
    Attachment,
    AttachmentStatus,
    ContinuationResourceView,
    CreateCase,
    CreateCaseTriggerPayload,
    DiagnosisItem,
    DiagnosisItemStatus,
    DiagnosisProvenance,
    DiagnosisProvenanceType,
    Job,
    JobStatus,
    JobType,
    JobLifecycleUpdate,
    ProblemSpec,
    PrepareAttachment,
    ResourceKind,
    ResourceRef,
    RuntimeBindings,
    TriggerType,
    UserFactInput,
    ValidatedTrigger,
    VersionedRef,
)


def empty_continuation_resources() -> ContinuationResourceView:
    """Return the sole valid continuation resource set for CREATE_CASE."""

    return ContinuationResourceView(
        evidence_refs=[],
        attachment_refs=[],
        artifact_refs=[],
        previous_outcome_refs=[],
    )


def problem_spec_at_revision_one(command: CreateCase) -> ProblemSpec:
    """Promote the validated input DTO without normalizing user text."""

    return ProblemSpec(
        **command.problem_spec.model_dump(mode="python"),
        revision=1,
    )


def make_user_fact(
    value: UserFactInput,
    *,
    item_id: str,
    trigger_id: str,
    created_revision: int,
) -> DiagnosisItem:
    """Build one user-input DiagnosisItem with frozen provenance."""

    return DiagnosisItem(
        item_id=item_id,
        statement=value.value,
        status=DiagnosisItemStatus.ACTIVE,
        provenance=DiagnosisProvenance(
            source_type=DiagnosisProvenanceType.USER_INPUT,
            source_ref=trigger_id,
            input_name=value.name,
        ),
        evidence_refs=[],
        created_revision=created_revision,
        supersedes=[],
    )


def build_create_case_trigger(
    command: CreateCase,
    *,
    case_id: str,
    trigger_id: str,
    user_fact_ids: Sequence[str],
    route_bindings: RuntimeBindings,
    occurred_at: str,
) -> ValidatedTrigger:
    """Build CREATE_CASE from IDs/time allocated once by the service."""

    if len(user_fact_ids) != len(command.initial_user_facts):
        raise ValueError("user_fact_ids must match initial_user_facts one-for-one")
    facts = [
        make_user_fact(
            value,
            item_id=item_id,
            trigger_id=trigger_id,
            created_revision=1,
        )
        for value, item_id in zip(
            command.initial_user_facts,
            user_fact_ids,
            strict=True,
        )
    ]
    return ValidatedTrigger(
        trigger_id=trigger_id,
        trigger_type=TriggerType.CREATE_CASE,
        case_id=case_id,
        expected_case_revision=0,
        idempotency_key=command.idempotency_key,
        payload=CreateCaseTriggerPayload(
            raw_problem_text=command.raw_problem_text,
            problem_spec=problem_spec_at_revision_one(command),
            initial_user_facts=facts,
        ),
        continuation_resources=empty_continuation_resources(),
        runtime_bindings_by_job_type={JobType.ROUTE: route_bindings},
        occurred_at=occurred_at,
    )


def build_uploading_attachment(
    command: PrepareAttachment,
    *,
    attachment_id: str,
    occurred_at: str,
) -> Attachment:
    """Create the immutable UPLOADING-side metadata for PrepareAttachment."""

    return Attachment(
        attachment_id=attachment_id,
        case_id=command.case_id,
        status=AttachmentStatus.UPLOADING,
        name=command.name,
        content_type=command.content_type,
        declared_size=command.declared_size,
        declared_sha256=command.declared_sha256,
        size=None,
        sha256=None,
        storage_key=None,
        created_at=occurred_at,
        updated_at=occurred_at,
    )


def finalize_attachment(
    attachment: Attachment,
    resource_ref: ResourceRef,
    *,
    occurred_at: str,
) -> Attachment:
    """Return the READY metadata after a successful immutable publication."""

    if attachment.status is not AttachmentStatus.UPLOADING:
        raise ValueError("only an UPLOADING Attachment can be finalized")
    if resource_ref.resource_kind is not ResourceKind.FILE:
        raise ValueError("Attachment publication must resolve a FILE resource")
    payload = attachment.model_dump(mode="python")
    payload.update(
        status=AttachmentStatus.READY,
        size=resource_ref.size,
        sha256=resource_ref.sha256,
        storage_key=resource_ref.storage_key,
        updated_at=occurred_at,
    )
    return Attachment.model_validate(payload)


def fixed_asset_refs(job: Job) -> list[VersionedRef]:
    """Return every versioned runtime asset in stable Job-field order."""

    refs = [
        job.agent_profile_ref,
        *job.available_skill_refs,
        *([] if job.skill_ref is None else [job.skill_ref]),
        job.tool_bundle_ref,
        job.context_policy_ref,
        job.output_contract_ref,
        *([] if job.logparse_tool_ref is None else [job.logparse_tool_ref]),
    ]
    result: list[VersionedRef] = []
    seen: set[tuple[str, str, str]] = set()
    for ref in refs:
        key = (ref.id, ref.version, ref.content_hash)
        if key not in seen:
            seen.add(key)
            result.append(ref)
    return result


def runtime_bindings_from_job(job: Job) -> RuntimeBindings:
    """Mechanically recover the complete frozen bindings from job.json."""

    return RuntimeBindings(
        diagnosis_mode=job.diagnosis_mode,
        review_policy=job.review_policy,
        generic_skill_name=job.generic_skill_name,
        agent_profile_ref=job.agent_profile_ref,
        available_skill_refs=list(job.available_skill_refs),
        skill_ref=job.skill_ref,
        tool_bundle_ref=job.tool_bundle_ref,
        context_policy_ref=job.context_policy_ref,
        output_contract_ref=job.output_contract_ref,
        logparse_tool_ref=job.logparse_tool_ref,
        logparse_product=job.logparse_product,
        resource_limits=job.resource_limits,
    )


def claim_lifecycle_update(
    job: Job,
    *,
    runtime_epoch: str,
    started_at: str,
) -> JobLifecycleUpdate:
    """Build the PENDING→RUNNING update committed after asset availability."""

    if job.status is not JobStatus.PENDING:
        raise ValueError("only a PENDING Job can be claimed")
    return JobLifecycleUpdate(
        job_id=job.job_id,
        expected_status=JobStatus.PENDING,
        target_status=JobStatus.RUNNING,
        started_at=started_at,
        finished_at=None,
        runtime_epoch=runtime_epoch,
    )


__all__ = [
    "build_create_case_trigger",
    "build_uploading_attachment",
    "claim_lifecycle_update",
    "empty_continuation_resources",
    "finalize_attachment",
    "fixed_asset_refs",
    "make_user_fact",
    "problem_spec_at_revision_one",
    "runtime_bindings_from_job",
]
