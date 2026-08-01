"""Persist validated Agent proposal bytes and build the canonical JobOutcome."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from problem_locator.contracts import (
    AgentArtifactProposalDraft,
    AgentEvidenceProposalDraft,
    ApplicationErrorDetail,
    ApplicationPortError,
    ArtifactKind,
    ArtifactProposal,
    ErrorCode,
    EvidenceProposal,
    ExecutionStage,
    Job,
    JobOutcome,
    LogparseParseClaim,
    LogparseParseParameters,
    LogparseRunMetadata,
    ResourceKind,
    ResourceStore,
    StagedResourceRef,
    WorkspaceInputManifest,
    canonical_json_bytes,
    validate_logparse_claim_for_job,
    validate_outcome_for_job,
    validate_user_result_for_outcome,
)

from .failures import RuntimeExecutionError, runtime_failure
from .output_reader import (
    ValidatedAgentOutput,
    ValidatedProposalResource,
    _InvalidOutput,
)


_LOGPARSE_CONTENT_TYPE = "application/vnd.problem-locator.logparse-run+directory"


class _InvalidLogparseRun(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StagedOutcome:
    """A canonical Outcome and the still-private resources referenced by it."""

    outcome: JobOutcome
    staged_refs: tuple[StagedResourceRef, ...]


def _resource_stage_failure(
    *,
    retryable: bool,
    details: tuple[ApplicationErrorDetail, ...] | list[ApplicationErrorDetail] = (),
) -> RuntimeExecutionError:
    return runtime_failure(
        stage=ExecutionStage.RESOURCE_STAGE,
        code=ErrorCode.RESOURCE_STAGE_FAILED,
        message="A proposal resource could not be staged.",
        retryable=retryable,
        details=details,
    )


def _stage_port_failure(error: ApplicationPortError) -> RuntimeExecutionError:
    return _resource_stage_failure(
        retryable=error.error.code is ErrorCode.RESOURCE_STAGE_FAILED,
        details=error.error.details,
    )


def _audit_port_failure(error: ApplicationPortError) -> RuntimeExecutionError:
    if error.error.code is ErrorCode.RESOURCE_HASH_MISMATCH:
        return runtime_failure(
            stage=ExecutionStage.RESOURCE_STAGE,
            code=ErrorCode.RESOURCE_HASH_MISMATCH,
            message="A staged proposal resource failed its content audit.",
            details=error.error.details,
        )
    return _resource_stage_failure(
        retryable=True,
        details=error.error.details,
    )


def _assert_receipt(
    staged: object,
    *,
    job: Job,
    resource: ValidatedProposalResource,
) -> StagedResourceRef:
    if not isinstance(staged, StagedResourceRef):
        raise _resource_stage_failure(retryable=False)
    if (
        staged.owner_job_id != job.job_id
        or staged.proposal_key != resource.proposal_key
        or staged.resource_kind is not resource.resource_kind
        or staged.size != resource.size
        or staged.sha256 != resource.sha256
        or staged.tree_manifest != resource.tree_manifest
    ):
        raise _resource_stage_failure(retryable=False)
    return staged


def _stage_one(
    resource_store: ResourceStore,
    job: Job,
    resource: ValidatedProposalResource,
) -> StagedResourceRef:
    value: object | None = None
    try:
        if resource.resource_kind is ResourceKind.FILE:
            with resource.open_verified_file() as stream:
                value = resource_store.stage_file(
                    job.job_id,
                    resource.proposal_key,
                    stream,
                    expected_size=resource.size,
                    expected_sha256=resource.sha256,
                )
        else:
            resource.verify_unchanged()
            value = resource_store.stage_tree(
                job.job_id,
                resource.proposal_key,
                resource.path,
                expected_manifest_hash=resource.sha256,
            )
            resource.verify_unchanged()
    except _InvalidOutput:
        if (
            isinstance(value, StagedResourceRef)
            and value.owner_job_id == job.job_id
            and value.proposal_key == resource.proposal_key
        ):
            discard_staged(resource_store, [value])
        raise _resource_stage_failure(retryable=False) from None
    except ApplicationPortError as exc:
        raise _stage_port_failure(exc) from None
    except Exception as exc:
        raise _resource_stage_failure(retryable=True) from exc

    try:
        staged = _assert_receipt(value, job=job, resource=resource)
    except RuntimeExecutionError:
        if (
            isinstance(value, StagedResourceRef)
            and value.owner_job_id == job.job_id
            and value.proposal_key == resource.proposal_key
        ):
            discard_staged(resource_store, [value])
        raise
    try:
        audit_result = resource_store.validate_staged(staged)
        if audit_result is not None:
            raise TypeError("validate_staged must return None")
    except ApplicationPortError as exc:
        discard_staged(resource_store, [staged])
        raise _audit_port_failure(exc) from None
    except Exception as exc:
        discard_staged(resource_store, [staged])
        raise _resource_stage_failure(retryable=True) from exc
    return staged


def _parse_manifest_path(
    staged: StagedResourceRef,
) -> str:
    if staged.resource_kind is not ResourceKind.DIRECTORY:
        raise _InvalidLogparseRun
    manifest = staged.tree_manifest
    if manifest is None or not manifest.entries:
        raise _InvalidLogparseRun
    paths = [PurePosixPath(entry.path) for entry in manifest.entries]
    direct_tasks = {path.parts[0] for path in paths if len(path.parts) >= 2}
    if len(direct_tasks) != 1 or any(len(path.parts) < 2 for path in paths):
        raise _InvalidLogparseRun
    task_name = next(iter(direct_tasks))
    relative_path = f"{task_name}/parse_manifest.json"
    if relative_path not in {entry.path for entry in manifest.entries}:
        raise _InvalidLogparseRun
    return relative_path


def _artifact_metadata(
    draft: AgentArtifactProposalDraft,
    staged: StagedResourceRef,
    job: Job,
    claim: LogparseParseClaim | None,
) -> object:
    if draft.artifact_kind is not ArtifactKind.LOGPARSE_RUN:
        return draft.metadata
    if (
        claim is None
        or job.logparse_tool_ref is None
        or job.logparse_product is None
        or draft.content_type != _LOGPARSE_CONTENT_TYPE
    ):
        raise _InvalidLogparseRun
    return LogparseRunMetadata(
        tree_manifest_sha256=staged.sha256,
        logparse_version_ref=job.logparse_tool_ref,
        parse_manifest_relative_path=_parse_manifest_path(staged),
        source_attachment_id=claim.attachment_id,
        source_attachment_sha256=claim.attachment_sha256,
        parse_parameters=LogparseParseParameters(product=job.logparse_product),
    )


def _normalize_evidence(
    draft: AgentEvidenceProposalDraft,
    staged: StagedResourceRef | None,
) -> EvidenceProposal:
    return EvidenceProposal(
        proposal_key=draft.proposal_key,
        source_type=draft.source_type,
        source_binding=draft.source_binding,
        locator=draft.locator,
        summary=draft.summary,
        content_hash=None if staged is None else staged.sha256,
        staged_resource_ref=staged,
    )


def _normalize_artifact(
    draft: AgentArtifactProposalDraft,
    staged: StagedResourceRef,
    job: Job,
    claim: LogparseParseClaim | None,
) -> ArtifactProposal:
    return ArtifactProposal(
        proposal_key=draft.proposal_key,
        artifact_kind=draft.artifact_kind,
        name=draft.name,
        content_type=draft.content_type,
        resource_kind=staged.resource_kind,
        size=staged.size,
        sha256=staged.sha256,
        staged_resource_ref=staged,
        metadata=_artifact_metadata(draft, staged, job, claim),
    )


def discard_staged(
    resource_store: ResourceStore,
    staged_refs: tuple[StagedResourceRef, ...] | list[StagedResourceRef],
) -> None:
    """Best-effort cleanup that never hides the execution's primary result."""

    for staged in reversed(staged_refs):
        try:
            resource_store.discard(staged)
        except Exception:
            pass


def stage_validated_output(
    *,
    job: Job,
    workspace_manifest: WorkspaceInputManifest,
    validated: ValidatedAgentOutput,
    resource_store: ResourceStore,
    claim: LogparseParseClaim | None,
    parse_request_bytes: bytes | None,
) -> StagedOutcome:
    """Stage every resource, audit it, then build and revalidate one Outcome."""

    try:
        validate_logparse_claim_for_job(
            claim,
            job,
            workspace_manifest,
            parse_request_bytes,
            validated.outcome,
        )
    except (TypeError, ValueError):
        raise runtime_failure(
            stage=ExecutionStage.TOOL_EXECUTE,
            code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
            message="Logparse execution evidence is invalid.",
        ) from None

    resources = {
        resource.proposal_key: resource
        for resource in validated.proposal_resources
    }
    staged_refs: list[StagedResourceRef] = []
    staged_by_key: dict[str, StagedResourceRef] = {}
    try:
        for draft in (
            *validated.outcome.proposed_evidence_drafts,
            *validated.outcome.proposed_artifact_drafts,
        ):
            resource = resources.get(draft.proposal_key)
            if resource is None:
                if (
                    isinstance(draft, AgentArtifactProposalDraft)
                    or draft.workspace_relative_path is not None
                ):
                    raise ValueError("Artifact proposal content is missing")
                continue
            staged = _stage_one(resource_store, job, resource)
            staged_refs.append(staged)
            staged_by_key[draft.proposal_key] = staged

        proposed_evidence = [
            _normalize_evidence(draft, staged_by_key.get(draft.proposal_key))
            for draft in validated.outcome.proposed_evidence_drafts
        ]
        proposed_artifacts = []
        for draft in validated.outcome.proposed_artifact_drafts:
            resource = resources[draft.proposal_key]
            proposed_artifacts.append(
                _normalize_artifact(
                    draft,
                    staged_by_key[draft.proposal_key],
                    job,
                    claim,
                )
            )

        agent = validated.outcome
        outcome = JobOutcome(
            outcome_id=agent.outcome_id,
            job_id=agent.job_id,
            case_id=agent.case_id,
            job_type=agent.job_type,
            base_state_revision=agent.base_state_revision,
            result_type=agent.result_type,
            payload=agent.payload,
            consumed_evidence_refs=agent.consumed_evidence_refs,
            proposed_evidence=proposed_evidence,
            proposed_artifacts=proposed_artifacts,
            error=agent.error,
            produced_at=agent.produced_at,
        )
        validate_outcome_for_job(job, outcome, workspace_manifest)
        validate_logparse_claim_for_job(
            claim,
            job,
            workspace_manifest,
            parse_request_bytes,
            outcome,
        )
        if validated.user_result is not None:
            validate_user_result_for_outcome(
                job,
                outcome,
                canonical_json_bytes(validated.user_result),
            )
    except RuntimeExecutionError:
        discard_staged(resource_store, staged_refs)
        raise
    except _InvalidLogparseRun:
        discard_staged(resource_store, staged_refs)
        raise runtime_failure(
            stage=ExecutionStage.TOOL_EXECUTE,
            code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
            message="Logparse output directory is invalid.",
        ) from None
    except (KeyError, TypeError, ValueError):
        discard_staged(resource_store, staged_refs)
        raise runtime_failure(
            stage=ExecutionStage.OUTCOME_VALIDATE,
            code=ErrorCode.OUTCOME_INVALID,
            message="Agent outcome normalization failed.",
        ) from None
    except Exception as exc:
        discard_staged(resource_store, staged_refs)
        raise _resource_stage_failure(retryable=True) from exc

    return StagedOutcome(outcome=outcome, staged_refs=tuple(staged_refs))


__all__ = ["StagedOutcome", "discard_staged", "stage_validated_output"]
