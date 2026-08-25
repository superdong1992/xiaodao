"""Isolated, offline V2 Job replay orchestration.

Replay deliberately has no scheduler, recovery, retention, authentication, or
administrator concept.  It takes a stable copy of one source Job's immutable
input closure, runs the normal claim/runtime boundary in a new installation,
and records enough hashes to compare repeated executions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from problem_locator.application.preparation import fixed_asset_refs
from problem_locator.contracts import (
    CONTRACT_REVISION,
    ERROR_SPECS,
    SCHEMA_VERSION,
    ApplicationError,
    ApplicationPortError,
    AttachmentStatus,
    CandidateStatus,
    Case,
    CaseAggregate,
    CaseStatus,
    DiagnosisState,
    ErrorCode,
    EvidenceSourceType,
    Job,
    JobOutcome,
    JobStatus,
    JobType,
    OutcomeDisposition,
    OutcomeReceipt,
    ResourceKind,
    ResourceRef,
    StateFile,
    VersionedRef,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.diagnostics import bind_diagnostics, configure_diagnostics, log_event
from problem_locator.dispatch.cancellation import CancellationController
from problem_locator.entrypoints.settings import Settings
from problem_locator.integrations.logparse import build_logparse_runtime
from problem_locator.journey import configure_journey, record_journey_event
from problem_locator.runtime.catalog import VersionedAssetCatalog
from problem_locator.storage.atomic import (
    finalize_read_only_file,
    finalize_read_only_tree,
    read_stable_file_bytes,
    require_ordinary_file,
    require_real_directory,
)
from problem_locator.storage.layout import StorageLayout, UnsupportedDataFormatError
from problem_locator.storage.paths import resource_path
from problem_locator.storage.platform import FileInstanceLock, PlatformFileSync
from problem_locator.storage.resource_files import validate_formal_resource


class ReplayMode(StrEnum):
    DIAGNOSE_ONLY = "diagnose-only"
    REVIEW_ONLY = "review-only"
    THROUGH_REVIEW = "through-review"


@dataclass(frozen=True, slots=True)
class ReplayRequest:
    source_data_root: Path
    job_id: str
    mode: ReplayMode
    output_dir: Path


class ReplayStageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    status: Literal["COMPLETED", "FAILED"]
    input_sha256: str | None
    output_sha256: str | None


class ReplayAssetDiff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    binding: str
    source_ref: VersionedRef | None
    replay_ref: VersionedRef | None
    changed: bool


class ReplayManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    state_schema_version: Literal[7]
    contract_revision: str
    replay_id: str
    mode: ReplayMode
    source_data_root: str
    source_state_generation: int
    source_state_sha256: str
    source_installation_id: str
    source_case_id: str
    source_job_id: str
    source_job_sha256: str
    source_outcome_id: str | None
    source_outcome_sha256: str | None
    output_data_root: str
    replay_installation_id: str
    projected_job_sha256: str
    projected_state_sha256: str
    source_fixed_asset_refs: list[VersionedRef]
    replay_fixed_asset_refs: list[VersionedRef]
    asset_diff: list[ReplayAssetDiff]
    created_at: str


class ReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    replay_id: str
    mode: ReplayMode
    success: bool
    stop_reason: str
    source_case_id: str | None
    source_job_id: str
    replay_case_id: str | None
    diagnosis_job_id: str | None
    diagnosis_outcome_id: str | None
    review_job_id: str | None
    review_outcome_id: str | None
    final_case_status: str | None
    stages: list[ReplayStageRecord]
    error: ApplicationError | None
    completed_at: str


class ReplayError(RuntimeError):
    """Safe typed failure returned through the existing CLI error envelope."""

    def __init__(
        self,
        error: ApplicationError,
        *,
        stop_reason: str,
        result: ReplayResult | None = None,
    ) -> None:
        super().__init__(error.code.value)
        self.error = error
        self.stop_reason = stop_reason
        self.result = result


@dataclass(slots=True)
class _ReplayProgress:
    replay_id: str
    request: ReplayRequest
    source_case_id: str | None = None
    diagnosis_job_id: str | None = None
    diagnosis_outcome_id: str | None = None
    review_job_id: str | None = None
    review_outcome_id: str | None = None
    final_case_status: str | None = None
    stages: list[ReplayStageRecord] | None = None
    output_created: bool = False

    def __post_init__(self) -> None:
        if self.stages is None:
            self.stages = []

    def stage(
        self,
        name: str,
        *,
        input_bytes: bytes | None = None,
        output_bytes: bytes | None = None,
    ) -> None:
        assert self.stages is not None
        self.stages.append(
            ReplayStageRecord(
                name=name,
                status="COMPLETED",
                input_sha256=_sha256(input_bytes),
                output_sha256=_sha256(output_bytes),
            )
        )
        record_journey_event(
            "replay.stage.completed",
            case_id=self.source_case_id,
            job_id=self.request.job_id,
            data={
                "replay_id": self.replay_id,
                "stage": name,
                "input_sha256": _sha256(input_bytes),
                "output_sha256": _sha256(output_bytes),
            },
        )

    def failed_stage(self, name: str) -> None:
        assert self.stages is not None
        self.stages.append(
            ReplayStageRecord(
                name=name,
                status="FAILED",
                input_sha256=None,
                output_sha256=None,
            )
        )

    def result(
        self,
        *,
        success: bool,
        stop_reason: str,
        error: ApplicationError | None,
    ) -> ReplayResult:
        assert self.stages is not None
        return ReplayResult(
            schema_version=1,
            replay_id=self.replay_id,
            mode=self.request.mode,
            success=success,
            stop_reason=stop_reason,
            source_case_id=self.source_case_id,
            source_job_id=self.request.job_id,
            replay_case_id=self.source_case_id,
            diagnosis_job_id=self.diagnosis_job_id,
            diagnosis_outcome_id=self.diagnosis_outcome_id,
            review_job_id=self.review_job_id,
            review_outcome_id=self.review_outcome_id,
            final_case_status=self.final_case_status,
            stages=list(self.stages),
            error=error,
            completed_at=_utc_now(),
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sha256(value: bytes | None) -> str | None:
    return None if value is None else hashlib.sha256(value).hexdigest()


def _error(code: ErrorCode, message: str) -> ApplicationError:
    return ApplicationError(
        code=code,
        message=message,
        details=[],
        retryable=ERROR_SPECS[code].application_retryable,
    )


def _fail(code: ErrorCode, message: str, stop_reason: str) -> ReplayError:
    return ReplayError(_error(code, message), stop_reason=stop_reason)


def _initialize_replay_data_root(output_data_root: Path) -> StorageLayout:
    """Create and mark an empty replay DATA_ROOT before copying business bytes."""

    layout = StorageLayout.at(output_data_root)
    sync = PlatformFileSync()
    layout.initialize_v2_data_root(sync)
    return layout


def _write_new(path: Path, payload: bytes, *, read_only: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    created: os.stat_result | None = None
    try:
        created = os.fstat(descriptor)
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("replay artifact write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if read_only:
            finalize_read_only_file(path, PlatformFileSync())
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            current = require_ordinary_file(path)
            if created is not None and (current.st_dev, current.st_ino) == (
                created.st_dev,
                created.st_ino,
            ):
                if not current.st_mode & stat.S_IWUSR:
                    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
                    current = require_ordinary_file(path)
                if (current.st_dev, current.st_ino) == (
                    created.st_dev,
                    created.st_ino,
                ):
                    path.unlink()
        except (FileNotFoundError, OSError, ValueError):
            pass
        raise


def _same_file(left: Path, right: Path) -> bool:
    left_metadata = require_ordinary_file(left)
    right_metadata = require_ordinary_file(right)
    return (left_metadata.st_dev, left_metadata.st_ino) == (
        right_metadata.st_dev,
        right_metadata.st_ino,
    )


def _remove_owned_publication(path: Path, identity: tuple[int, int]) -> None:
    """Best-effort rollback without unlinking a concurrently replaced path."""

    try:
        metadata = require_ordinary_file(path)
        if (metadata.st_dev, metadata.st_ino) != identity:
            return
        if not metadata.st_mode & stat.S_IWUSR:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            metadata = require_ordinary_file(path)
            if (metadata.st_dev, metadata.st_ino) != identity:
                return
        path.unlink()
    except (FileNotFoundError, OSError, ValueError):
        # Publication failure remains authoritative.  A rollback failure may
        # leave a complete (never partial) file for post-mortem inspection.
        pass


def _publish_new(path: Path, payload: bytes, *, read_only: bool = False) -> None:
    """Atomically publish one new replay document without replacing a peer.

    Bytes are first written and fsynced through an exclusive temporary file in
    the destination directory.  A hard-link publication is the cross-platform
    atomic no-replace gate; the temporary name is then removed and the parent
    directory is synced before success is reported.
    """

    if not isinstance(payload, bytes):
        raise TypeError("replay publication payload must be immutable bytes")
    path = Path(path)
    require_real_directory(path.parent)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    linked = False
    temporary_present = False
    temporary_identity: tuple[int, int] | None = None
    sync = PlatformFileSync()
    try:
        _write_new(temporary, payload)
        temporary_present = True
        metadata = require_ordinary_file(temporary)
        temporary_identity = (metadata.st_dev, metadata.st_ino)
        os.link(temporary, path, follow_symlinks=False)
        linked = True
        if not _same_file(path, temporary):
            raise OSError("replay publication hard link changed identity")
        temporary.unlink()
        temporary_present = False
        if read_only:
            finalize_read_only_file(path, sync)
        sync.sync_directory(path.parent)
    except BaseException:
        if linked and temporary_identity is not None:
            _remove_owned_publication(path, temporary_identity)
            try:
                sync.sync_directory(path.parent)
            except (OSError, ValueError):
                pass
        raise
    finally:
        if temporary_present:
            try:
                temporary.unlink()
            except (FileNotFoundError, OSError):
                pass


def _paths_overlap(left: Path, right: Path) -> bool:
    left_resolved = left.resolve(strict=False)
    right_resolved = right.resolve(strict=False)
    return (
        left_resolved == right_resolved
        or left_resolved in right_resolved.parents
        or right_resolved in left_resolved.parents
    )


def validate_replay_paths(request: ReplayRequest, settings: Settings) -> None:
    """Validate the non-overlap boundary before creating the replay root."""

    for label, path in (
        ("source-data-root", request.source_data_root),
        ("output-dir", request.output_dir),
        ("skill-dir", settings.skill_dir),
        ("logparse-repo", settings.logparse_repo),
        ("logparse-config", settings.logparse_config_path),
    ):
        if not Path(path).is_absolute():
            raise _fail(
                ErrorCode.CONFIG_INVALID,
                f"{label} must be an absolute path.",
                "PATH_INVALID",
            )
    if request.output_dir.exists() or request.output_dir.is_symlink():
        raise _fail(
            ErrorCode.CONFIG_INVALID,
            "Replay output directory must not already exist.",
            "OUTPUT_ALREADY_EXISTS",
        )
    try:
        require_real_directory(request.output_dir.parent)
    except (OSError, ValueError) as exc:
        raise _fail(
            ErrorCode.CONFIG_INVALID,
            "Replay output parent must be an existing real directory.",
            "OUTPUT_PARENT_INVALID",
        ) from exc

    protected = [
        request.source_data_root,
        settings.skill_dir,
        settings.logparse_repo,
        settings.logparse_config_path,
    ]
    if settings.dfx_log_dir is not None:
        protected.append(settings.dfx_log_dir)
    if any(_paths_overlap(request.output_dir, path) for path in protected):
        raise _fail(
            ErrorCode.CONFIG_INVALID,
            "Replay output must not overlap source, Skill, Logparse, or DFX paths.",
            "PATH_OVERLAP",
        )


def _decode_source_state(raw_bytes: bytes) -> StateFile:
    try:
        envelope = json.loads(raw_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _fail(
            ErrorCode.STATE_CORRUPT,
            "Source state.json is invalid.",
            "SOURCE_STATE_CORRUPT",
        ) from exc
    if not isinstance(envelope, dict):
        raise _fail(
            ErrorCode.STATE_CORRUPT,
            "Source state.json must contain an object.",
            "SOURCE_STATE_CORRUPT",
        )
    if (
        envelope.get("schema_version") != SCHEMA_VERSION
        or envelope.get("contract_revision") != CONTRACT_REVISION
    ):
        raise _fail(
            ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            "Replay accepts only the current State V7 contract.",
            "SOURCE_STATE_SCHEMA_UNSUPPORTED",
        )
    try:
        return parse_canonical_json_bytes(raw_bytes, StateFile)
    except (TypeError, ValueError, ValidationError) as exc:
        raise _fail(
            ErrorCode.STATE_CORRUPT,
            "Source State V7 violates its canonical contract.",
            "SOURCE_STATE_CORRUPT",
        ) from exc


def _find_job(state: StateFile, job_id: str) -> tuple[CaseAggregate, Job]:
    matches = [
        (aggregate, aggregate.jobs[job_id])
        for aggregate in state.cases.values()
        if job_id in aggregate.jobs
    ]
    if not matches:
        raise _fail(
            ErrorCode.JOB_NOT_FOUND,
            "The source Job does not exist.",
            "SOURCE_JOB_NOT_FOUND",
        )
    if len(matches) != 1:
        raise _fail(
            ErrorCode.STATE_CORRUPT,
            "The source Job ID is not globally unique.",
            "SOURCE_STATE_CORRUPT",
        )
    return matches[0]


def _validate_mode_job(mode: ReplayMode, job: Job) -> None:
    expected = JobType.REVIEW if mode is ReplayMode.REVIEW_ONLY else JobType.DIAGNOSE
    if job.job_type is not expected:
        raise _fail(
            ErrorCode.VALIDATION_ERROR,
            f"{mode.value} requires a {expected.value} source Job.",
            "MODE_JOB_TYPE_MISMATCH",
        )


def _current_bindings(catalog: VersionedAssetCatalog, job: Job) -> Any:
    if job.skill_ref is None:
        raise _fail(
            ErrorCode.ASSET_VERSION_UNAVAILABLE,
            "The replay source Job has no diagnosis Skill.",
            "SOURCE_SKILL_MISSING",
        )
    current = [
        ref
        for ref in catalog.route_bindings().available_skill_refs
        if ref.id == job.skill_ref.id
    ]
    if len(current) != 1:
        raise _fail(
            ErrorCode.ASSET_VERSION_UNAVAILABLE,
            "The current Skill set does not contain one unambiguous matching Skill ID.",
            "CURRENT_SKILL_UNAVAILABLE",
        )
    if job.job_type is JobType.DIAGNOSE:
        return catalog.diagnose_bindings(current[0])
    return catalog.review_bindings(current[0])


def _rebind_pending_job(job: Job, bindings: Any) -> Job:
    payload = job.model_dump(mode="python")
    payload.update(
        status=JobStatus.PENDING,
        started_at=None,
        finished_at=None,
        runtime_epoch=None,
        diagnosis_mode=bindings.diagnosis_mode,
        generic_skill_name=bindings.generic_skill_name,
        agent_profile_ref=bindings.agent_profile_ref,
        available_skill_refs=bindings.available_skill_refs,
        skill_ref=bindings.skill_ref,
        tool_bundle_ref=bindings.tool_bundle_ref,
        context_policy_ref=bindings.context_policy_ref,
        output_contract_ref=bindings.output_contract_ref,
        logparse_tool_ref=bindings.logparse_tool_ref,
        logparse_product=bindings.logparse_product,
        resource_limits=bindings.resource_limits,
    )
    try:
        return Job.model_validate(payload)
    except (TypeError, ValueError, ValidationError) as exc:
        raise _fail(
            ErrorCode.STATE_CORRUPT,
            "The source Job cannot be projected onto current runtime bindings.",
            "JOB_REBIND_FAILED",
        ) from exc


def _asset_bindings(job: Job) -> dict[str, VersionedRef]:
    result = {
        "agent_profile": job.agent_profile_ref,
        "tool_bundle": job.tool_bundle_ref,
        "context_policy": job.context_policy_ref,
        "output_contract": job.output_contract_ref,
    }
    if job.skill_ref is not None:
        result["skill"] = job.skill_ref
    if job.logparse_tool_ref is not None:
        result["logparse_tool"] = job.logparse_tool_ref
    for ref in job.available_skill_refs:
        result[f"available_skill:{ref.id}"] = ref
    return result


def _asset_diff(source_job: Job, replay_job: Job) -> list[ReplayAssetDiff]:
    source = _asset_bindings(source_job)
    replay = _asset_bindings(replay_job)
    return [
        ReplayAssetDiff(
            binding=binding,
            source_ref=source.get(binding),
            replay_ref=replay.get(binding),
            changed=source.get(binding) != replay.get(binding),
        )
        for binding in sorted(set(source) | set(replay))
    ]


def _snapshot_state(job: Job) -> DiagnosisState:
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


def _project_case(source: Case, job: Job) -> Case:
    status = CaseStatus.REVIEWING if job.job_type is JobType.REVIEW else CaseStatus.RUNNING
    payload = source.model_dump(mode="python")
    payload.update(
        status=status,
        diagnosis_state=_snapshot_state(job),
        active_job_id=job.job_id,
        selected_skill_ref=job.skill_ref,
        final_result=None,
        unresolved_result=None,
        failure=None,
        updated_at=_utc_now(),
    )
    return Case.model_validate(payload)


def _add_snapshot_dependencies(job: Job, jobs: set[str], evidence: set[str]) -> None:
    snapshot = job.context_snapshot
    evidence.update(snapshot.evidence_refs)
    jobs.update(item.requested_by_job_id for item in snapshot.pending_requirements)
    if snapshot.candidate_conclusion is not None:
        candidate = snapshot.candidate_conclusion
        jobs.add(candidate.proposed_by_job_id)
        evidence.update(candidate.supporting_evidence_refs)
        for mapping in candidate.completion_criteria_mapping:
            evidence.update(mapping.evidence_refs)


def _project_closure(
    source: CaseAggregate,
    target: Job,
) -> tuple[
    dict[str, Job],
    dict[str, JobOutcome],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    jobs = {target.job_id}
    outcomes: set[str] = set()
    evidence: set[str] = set()
    attachments: set[str] = set()
    artifacts: set[str] = set()
    processing: set[str] = set()

    changed = True
    while changed:
        before = tuple(map(len, (jobs, outcomes, evidence, attachments, artifacts, processing)))
        for job_id in tuple(jobs):
            job = source.jobs.get(job_id)
            if job is None:
                raise _fail(
                    ErrorCode.STATE_CORRUPT,
                    "The source replay closure contains a missing Job.",
                    "SOURCE_CLOSURE_INVALID",
                )
            evidence.update(job.evidence_refs)
            attachments.update(job.attachment_refs)
            artifacts.update(job.artifact_refs)
            outcomes.update(job.previous_outcome_refs)
            if job.replacement_for_job_id is not None:
                jobs.add(job.replacement_for_job_id)
            _add_snapshot_dependencies(job, jobs, evidence)

        for evidence_id in tuple(evidence):
            item = source.evidence.get(evidence_id)
            if item is None:
                raise _fail(
                    ErrorCode.STATE_CORRUPT,
                    "The source replay closure contains missing Evidence.",
                    "SOURCE_CLOSURE_INVALID",
                )
            if item.source_type is EvidenceSourceType.ATTACHMENT:
                attachments.add(item.source_ref)
            elif item.source_type in {
                EvidenceSourceType.LOGPARSE,
                EvidenceSourceType.TOOL_OUTPUT,
            }:
                artifacts.add(item.source_ref)
            elif item.source_type is EvidenceSourceType.PREVIOUS_OUTCOME:
                outcomes.add(item.source_ref)

        for artifact_id in tuple(artifacts):
            item = source.artifacts.get(artifact_id)
            if item is None:
                raise _fail(
                    ErrorCode.STATE_CORRUPT,
                    "The source replay closure contains a missing Artifact.",
                    "SOURCE_CLOSURE_INVALID",
                )
            jobs.add(item.created_by_job_id)
            metadata = item.metadata
            if hasattr(metadata, "source_attachment_id"):
                attachments.add(metadata.source_attachment_id)
            if hasattr(metadata, "source_job_id"):
                jobs.add(metadata.source_job_id)
            if hasattr(metadata, "source_outcome_id"):
                outcomes.add(metadata.source_outcome_id)

        for outcome_id in tuple(outcomes):
            item = source.outcomes.get(outcome_id)
            if item is None:
                raise _fail(
                    ErrorCode.STATE_CORRUPT,
                    "The source replay closure contains a missing Outcome.",
                    "SOURCE_CLOSURE_INVALID",
                )
            jobs.add(item.job_id)
            processing.add(outcome_id)

        for outcome_id in tuple(processing):
            item = source.outcome_processing_records.get(outcome_id)
            if item is None:
                raise _fail(
                    ErrorCode.STATE_CORRUPT,
                    "The source replay closure contains missing Outcome processing.",
                    "SOURCE_CLOSURE_INVALID",
                )
            jobs.add(item.job_id)
            evidence.update(item.accepted_evidence_ids)
            artifacts.update(item.accepted_artifact_ids)
            artifacts.update(item.generated_artifact_ids)
            if item.created_job_id is not None:
                jobs.add(item.created_job_id)

        after = tuple(map(len, (jobs, outcomes, evidence, attachments, artifacts, processing)))
        changed = after != before

    # The replay target's prior execution is intentionally outside the closure.
    if any(source.outcomes[item].job_id == target.job_id for item in outcomes):
        raise _fail(
            ErrorCode.STATE_CORRUPT,
            "The target Job's old Outcome is reachable from its own input closure.",
            "TARGET_OUTCOME_IN_INPUT_CLOSURE",
        )

    return (
        {key: source.jobs[key] for key in sorted(jobs)},
        {key: source.outcomes[key] for key in sorted(outcomes)},
        {key: source.outcome_processing_records[key] for key in sorted(processing)},
        {key: source.attachments[key] for key in sorted(attachments)},
        {key: source.evidence[key] for key in sorted(evidence)},
        {key: source.artifacts[key] for key in sorted(artifacts)},
    )


def _resource_refs(aggregate: CaseAggregate) -> Iterable[ResourceRef]:
    for item in aggregate.attachments.values():
        if item.status is AttachmentStatus.READY:
            assert item.storage_key is not None and item.size is not None and item.sha256 is not None
            yield ResourceRef(
                resource_kind=ResourceKind.FILE,
                storage_key=item.storage_key,
                size=item.size,
                sha256=item.sha256,
            )
    for item in aggregate.evidence.values():
        if item.resource_ref is not None:
            yield item.resource_ref
    for item in aggregate.artifacts.values():
        yield ResourceRef(
            resource_kind=item.resource_kind,
            storage_key=item.storage_key,
            size=item.size,
            sha256=item.sha256,
        )


def _copy_resource(source_root: Path, output_root: Path, reference: ResourceRef) -> None:
    source = validate_formal_resource(source_root, reference, require_read_only=True)
    destination = resource_path(output_root, reference)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sync = PlatformFileSync()
    if reference.resource_kind is ResourceKind.FILE:
        shutil.copyfile(source, destination, follow_symlinks=False)
        finalize_read_only_file(destination, sync)
    else:
        shutil.copytree(source, destination, symlinks=False)
        finalize_read_only_tree(destination, sync)
    validate_formal_resource(output_root, reference, require_read_only=True)


def _pending(job: Job) -> Job:
    payload = job.model_dump(mode="python")
    payload.update(status=JobStatus.PENDING, started_at=None, finished_at=None, runtime_epoch=None)
    return Job.model_validate(payload)


def _read_source_execution_records(
    source_root: Path,
    jobs: dict[str, Job],
    outcomes: dict[str, JobOutcome],
    target_job_id: str,
    source_target_job: Job,
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    outcome_by_job: dict[str, JobOutcome] = {}
    for outcome in outcomes.values():
        if outcome.job_id in outcome_by_job:
            raise _fail(
                ErrorCode.STATE_CORRUPT,
                "The source replay closure contains multiple Outcomes for one Job.",
                "SOURCE_CLOSURE_INVALID",
            )
        outcome_by_job[outcome.job_id] = outcome

    job_bytes: dict[str, bytes] = {}
    outcome_bytes: dict[str, bytes] = {}
    for job in jobs.values():
        source = source_root / "jobs" / job.job_id / "job.json"
        try:
            raw = read_stable_file_bytes(source)
            published = parse_canonical_json_bytes(raw, Job)
        except (OSError, TypeError, ValueError, ValidationError) as exc:
            raise _fail(
                ErrorCode.STATE_CORRUPT,
                "A source Job execution record is invalid.",
                "SOURCE_EXECUTION_RECORD_INVALID",
            ) from exc
        expected_source_job = (
            source_target_job if job.job_id == target_job_id else job
        )
        if published != _pending(expected_source_job):
            raise _fail(
                ErrorCode.STATE_CORRUPT,
                "A source Job does not match its immutable job.json.",
                "SOURCE_EXECUTION_RECORD_INVALID",
            )
        job_bytes[job.job_id] = raw

        outcome = outcome_by_job.get(job.job_id)
        if outcome is None:
            continue
        source_outcome = source_root / "jobs" / job.job_id / "job_outcome.json"
        try:
            raw_outcome = read_stable_file_bytes(source_outcome)
            published_outcome = parse_canonical_json_bytes(raw_outcome, JobOutcome)
        except (OSError, TypeError, ValueError, ValidationError) as exc:
            raise _fail(
                ErrorCode.STATE_CORRUPT,
                "A source Outcome execution record is invalid.",
                "SOURCE_EXECUTION_RECORD_INVALID",
            ) from exc
        if published_outcome != outcome:
            raise _fail(
                ErrorCode.STATE_CORRUPT,
                "A source Outcome does not match its immutable job_outcome.json.",
                "SOURCE_EXECUTION_RECORD_INVALID",
            )
        outcome_bytes[job.job_id] = raw_outcome
    return job_bytes, outcome_bytes


def _copy_execution_records(
    output_root: Path,
    aggregate: CaseAggregate,
    target_job: Job,
    source_job_bytes: dict[str, bytes],
    source_outcome_bytes: dict[str, bytes],
) -> None:
    for job in aggregate.jobs.values():
        payload = (
            canonical_json_bytes(target_job)
            if job.job_id == target_job.job_id
            else source_job_bytes[job.job_id]
        )
        _write_new(
            output_root / "jobs" / job.job_id / "job.json",
            payload,
            read_only=True,
        )
        outcome_payload = source_outcome_bytes.get(job.job_id)
        if outcome_payload is not None:
            _write_new(
                output_root / "jobs" / job.job_id / "job_outcome.json",
                outcome_payload,
                read_only=True,
            )


def _source_target_outcome(
    source_root: Path,
    aggregate: CaseAggregate,
    target_job: Job,
) -> tuple[str | None, str | None]:
    outcomes = [
        item for item in aggregate.outcomes.values() if item.job_id == target_job.job_id
    ]
    if len(outcomes) > 1:
        raise _fail(
            ErrorCode.STATE_CORRUPT,
            "The source target Job has multiple Outcomes.",
            "SOURCE_EXECUTION_RECORD_INVALID",
        )
    expected = outcomes[0] if outcomes else None
    outcome_path = source_root / "jobs" / target_job.job_id / "job_outcome.json"
    try:
        require_ordinary_file(outcome_path)
    except FileNotFoundError:
        if expected is None:
            return None, None
        raise _fail(
            ErrorCode.STATE_CORRUPT,
            "The source target Outcome execution record is missing.",
            "SOURCE_EXECUTION_RECORD_INVALID",
        ) from None
    except (OSError, ValueError) as exc:
        raise _fail(
            ErrorCode.STATE_CORRUPT,
            "The source target Outcome execution record is invalid.",
            "SOURCE_EXECUTION_RECORD_INVALID",
        ) from exc
    try:
        raw = read_stable_file_bytes(outcome_path)
        published = parse_canonical_json_bytes(raw, JobOutcome)
    except (OSError, TypeError, ValueError, ValidationError) as exc:
        raise _fail(
            ErrorCode.STATE_CORRUPT,
            "The source target Outcome execution record is invalid.",
            "SOURCE_EXECUTION_RECORD_INVALID",
        ) from exc
    if expected is not None and published != expected:
        raise _fail(
            ErrorCode.STATE_CORRUPT,
            "The source target Outcome does not match State V7.",
            "SOURCE_EXECUTION_RECORD_INVALID",
        )
    if (
        published.job_id != target_job.job_id
        or published.case_id != target_job.case_id
        or published.job_type is not target_job.job_type
        or published.base_state_revision != target_job.base_state_revision
    ):
        raise _fail(
            ErrorCode.STATE_CORRUPT,
            "The source target Outcome does not bind to its Job.",
            "SOURCE_EXECUTION_RECORD_INVALID",
        )
    return published.outcome_id, hashlib.sha256(raw).hexdigest()


def _build_projected_state(
    source_aggregate: CaseAggregate,
    source_job: Job,
    rebound_job: Job,
    replay_installation_id: str,
) -> StateFile:
    jobs, outcomes, processing, attachments, evidence, artifacts = _project_closure(
        source_aggregate, source_job
    )
    jobs[rebound_job.job_id] = rebound_job
    projected_case = _project_case(source_aggregate.case, rebound_job)
    aggregate = CaseAggregate(
        case=projected_case,
        jobs=jobs,
        outcomes=outcomes,
        outcome_processing_records=processing,
        execution_failure_records={},
        attachments=attachments,
        evidence=evidence,
        artifacts=artifacts,
    )
    now = _utc_now()
    return StateFile(
        schema_version=SCHEMA_VERSION,
        contract_revision=CONTRACT_REVISION,
        generation=1,
        installation_id=replay_installation_id,
        created_at=now,
        updated_at=now,
        runtime_epochs=[],
        recovery_processing_records={},
        cases={projected_case.case_id: aggregate},
        idempotency_records={},
    )


def _prepare_projection(
    progress: _ReplayProgress,
    request: ReplayRequest,
    settings: Settings,
    output_data_root: Path,
) -> tuple[StateFile, Job, ReplayManifest]:
    source_layout = StorageLayout.at(request.source_data_root)
    try:
        source_layout.validate_v2_data_format()
    except UnsupportedDataFormatError as exc:
        raise _fail(
            ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            "The source DATA_ROOT data format is unsupported.",
            "SOURCE_STATE_SCHEMA_UNSUPPORTED",
        ) from exc
    except (OSError, ValueError) as exc:
        raise _fail(
            ErrorCode.STATE_CORRUPT,
            "The source DATA_ROOT data-format marker is invalid.",
            "SOURCE_LAYOUT_INVALID",
        ) from exc
    try:
        for directory in (
            source_layout.data_root,
            source_layout.resources,
            source_layout.cases_resources,
            source_layout.jobs,
            source_layout.temporary,
        ):
            require_real_directory(directory)
        require_ordinary_file(source_layout.state)
        lock_metadata = require_ordinary_file(source_layout.instance_lock)
        if os.name == "nt" and lock_metadata.st_size < 1:
            raise ValueError("Windows source lock file has no lock byte")
    except (OSError, ValueError) as exc:
        raise _fail(
            ErrorCode.STATE_CORRUPT,
            "The source installation layout is invalid.",
            "SOURCE_LAYOUT_INVALID",
        ) from exc

    source_lock = FileInstanceLock(source_layout.instance_lock)
    try:
        source_lock.acquire()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _fail(
            ErrorCode.INSTANCE_LOCKED,
            "The source installation is already open.",
            "SOURCE_INSTANCE_LOCKED",
        ) from exc

    try:
        raw_state = read_stable_file_bytes(source_layout.state)
        source_state = _decode_source_state(raw_state)
        source_aggregate, source_job = _find_job(source_state, request.job_id)
        _validate_mode_job(request.mode, source_job)
        source_outcome_id, source_outcome_sha256 = _source_target_outcome(
            request.source_data_root,
            source_aggregate,
            source_job,
        )
        progress.source_case_id = source_job.case_id
        progress.stage(
            "source-v2-validated",
            input_bytes=raw_state,
            output_bytes=canonical_json_bytes(source_job),
        )

        try:
            logparse_asset, broker_factory = build_logparse_runtime(
                settings.logparse_repo,
                settings.logparse_config_path,
                settings.logparse_python,
            )
            catalog = VersionedAssetCatalog(
                skill_dir=settings.skill_dir,
                logparse_tool=logparse_asset,
                logparse_broker_factory=broker_factory,
                generic_skill_name=settings.generic_skill_name,
            )
            bindings = _current_bindings(catalog, source_job)
        except ReplayError:
            raise
        except ApplicationPortError as exc:
            raise ReplayError(
                exc.error,
                stop_reason="CURRENT_ASSET_BINDINGS_UNAVAILABLE",
            ) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise _fail(
                ErrorCode.CONFIG_INVALID,
                "Replay runtime assets are invalid.",
                "CURRENT_ASSET_BINDINGS_UNAVAILABLE",
            ) from exc

        rebound_job = _rebind_pending_job(source_job, bindings)
        replay_installation_id = str(uuid.uuid4())
        projected_state = _build_projected_state(
            source_aggregate,
            source_job,
            rebound_job,
            replay_installation_id,
        )
        projected_bytes = canonical_json_bytes(projected_state)
        projected_aggregate = projected_state.cases[source_job.case_id]
        source_job_bytes, source_outcome_bytes = _read_source_execution_records(
            request.source_data_root,
            projected_aggregate.jobs,
            projected_aggregate.outcomes,
            rebound_job.job_id,
            source_job,
        )
        seen_resources: set[str] = set()
        resources: list[ResourceRef] = []
        for reference in _resource_refs(projected_aggregate):
            if reference.storage_key in seen_resources:
                continue
            seen_resources.add(reference.storage_key)
            validate_formal_resource(
                request.source_data_root,
                reference,
                require_read_only=True,
            )
            resources.append(reference)

        # The source State/Job/Outcome/resource closure is now fully validated.
        # Only this boundary is allowed to create the isolated output root.
        try:
            request.output_dir.mkdir(mode=0o700)
        except OSError as exc:
            raise _fail(
                ErrorCode.CONFIG_INVALID,
                "Replay output directory could not be created exclusively.",
                "OUTPUT_CREATE_FAILED",
            ) from exc
        progress.output_created = True
        configure_diagnostics(
            settings.dfx_log_level,
            log_file=request.output_dir / "debug.jsonl",
        )
        configure_journey(log_file=request.output_dir / "journey.jsonl")

        layout = _initialize_replay_data_root(output_data_root)
        for reference in resources:
            _copy_resource(request.source_data_root, output_data_root, reference)
        _copy_execution_records(
            output_data_root,
            projected_aggregate,
            rebound_job,
            source_job_bytes,
            source_outcome_bytes,
        )
        _write_new(layout.state, projected_bytes)
        progress.stage(
            "closure-projected",
            input_bytes=canonical_json_bytes(source_aggregate),
            output_bytes=projected_bytes,
        )

        manifest = ReplayManifest(
            schema_version=1,
            state_schema_version=SCHEMA_VERSION,
            contract_revision=CONTRACT_REVISION,
            replay_id=progress.replay_id,
            mode=request.mode,
            source_data_root=str(request.source_data_root.resolve(strict=True)),
            source_state_generation=source_state.generation,
            source_state_sha256=hashlib.sha256(raw_state).hexdigest(),
            source_installation_id=source_state.installation_id,
            source_case_id=source_job.case_id,
            source_job_id=source_job.job_id,
            source_job_sha256=hashlib.sha256(canonical_json_bytes(source_job)).hexdigest(),
            source_outcome_id=source_outcome_id,
            source_outcome_sha256=source_outcome_sha256,
            output_data_root=str(output_data_root.resolve(strict=True)),
            replay_installation_id=replay_installation_id,
            projected_job_sha256=hashlib.sha256(canonical_json_bytes(rebound_job)).hexdigest(),
            projected_state_sha256=hashlib.sha256(projected_bytes).hexdigest(),
            source_fixed_asset_refs=fixed_asset_refs(source_job),
            replay_fixed_asset_refs=fixed_asset_refs(rebound_job),
            asset_diff=_asset_diff(source_job, rebound_job),
            created_at=_utc_now(),
        )
        return projected_state, rebound_job, manifest
    except (OSError, ValueError) as exc:
        if progress.output_created:
            raise _fail(
                ErrorCode.RESOURCE_PUBLISH_FAILED,
                "The isolated replay projection could not be written safely.",
                "REPLAY_OUTPUT_WRITE_FAILED",
            ) from exc
        raise _fail(
            ErrorCode.STATE_CORRUPT,
            "The source replay closure is invalid.",
            "SOURCE_CLOSURE_INVALID",
        ) from exc
    finally:
        source_lock.release()


def _execute_one(composition: Any, job_id: str, runtime_epoch: str) -> Any:
    claim = composition.application.claim_job(job_id, runtime_epoch)
    if not claim.claimed or claim.job is None:
        raise _fail(
            ErrorCode.CLAIM_REJECTED,
            "The projected replay Job could not be claimed.",
            "REPLAY_CLAIM_REJECTED",
        )
    cancellation = CancellationController()
    try:
        with bind_diagnostics(
            case_id=claim.job.case_id,
            job_id=claim.job.job_id,
            job_type=claim.job.job_type.value,
        ):
            return composition.runtime.execute(claim.job, cancellation)
    finally:
        cancellation.retire()


def _find_unique_review(composition: Any, case_id: str) -> Job | None:
    aggregate = composition.repository.read_case(case_id)
    matches = [
        job
        for job in aggregate.jobs.values()
        if job.job_type is JobType.REVIEW and job.status is JobStatus.PENDING
    ]
    candidate = aggregate.case.diagnosis_state.candidate_conclusion
    claims_review = (
        aggregate.case.status is CaseStatus.REVIEWING
        or (
            candidate is not None
            and candidate.status is CandidateStatus.REVIEWING
        )
    )
    if not claims_review:
        if matches:
            raise _fail(
                ErrorCode.OUTCOME_INVALID,
                "A REVIEW Job exists without a REVIEWING Case candidate.",
                "INVALID_REVIEW_JOB_CREATED",
            )
        return None
    if (
        len(matches) != 1
        or aggregate.case.active_job_id != matches[0].job_id
        or aggregate.case.status is not CaseStatus.REVIEWING
    ):
        raise _fail(
            ErrorCode.OUTCOME_INVALID,
            "Through-review replay did not produce one active REVIEW Job.",
            "UNIQUE_REVIEW_NOT_CREATED",
        )
    return matches[0]


_VALID_NO_REVIEW_STATUSES = frozenset(
    {
        CaseStatus.WAITING_INPUT,
        CaseStatus.WAITING_ATTACHMENT,
        CaseStatus.UNRESOLVED,
    }
)
_VALID_REVIEW_COMPLETION_STATUSES = frozenset(
    {
        CaseStatus.WAITING_INPUT,
        CaseStatus.WAITING_ATTACHMENT,
        CaseStatus.RESOLVED,
        CaseStatus.PARTIALLY_RESOLVED,
        CaseStatus.UNRESOLVED,
    }
)


def _require_applied_outcome_receipt(receipt: Any, *, stage: str) -> OutcomeReceipt:
    if not isinstance(receipt, OutcomeReceipt) or (
        receipt.disposition is not OutcomeDisposition.APPLIED
    ):
        raise _fail(
            ErrorCode.OUTCOME_INVALID,
            f"The {stage} Outcome was not applied to the isolated replay Case.",
            f"{stage.upper()}_OUTCOME_NOT_APPLIED",
        )
    return receipt


def _require_completed_case_state(
    aggregate: Any,
    *,
    allowed_statuses: frozenset[CaseStatus],
    stop_reason: str,
    message: str,
) -> None:
    case = aggregate.case
    if case.status not in allowed_statuses or case.active_job_id is not None:
        raise _fail(ErrorCode.OUTCOME_INVALID, message, stop_reason)


def run_replay_job(
    request: ReplayRequest,
    settings: Settings,
    *,
    service_factory: Callable[[Settings], Any],
) -> ReplayResult:
    """Run one replay and leave all source bytes untouched."""

    if not isinstance(request, ReplayRequest) or not isinstance(settings, Settings):
        raise TypeError("request and settings must be validated replay inputs")
    validate_replay_paths(request, settings)

    progress = _ReplayProgress(str(uuid.uuid4()), request)
    output_data_root = request.output_dir / "data"
    manifest_path = request.output_dir / "replay-manifest.json"
    result_path = request.output_dir / "replay-result.json"
    active_stage = "projection"
    composition: Any | None = None
    try:
        _, rebound_job, manifest = _prepare_projection(
            progress,
            request,
            settings,
            output_data_root,
        )
        log_event(
            "replay.started",
            replay_id=progress.replay_id,
            mode=request.mode,
            job_id=request.job_id,
        )
        _publish_new(manifest_path, canonical_json_bytes(manifest), read_only=True)

        replay_settings = replace(
            settings,
            data_root=output_data_root,
            dfx_log_dir=request.output_dir,
        )
        active_stage = "isolated-composition"
        composition = service_factory(replay_settings)
        progress.stage(
            "isolated-composition-built",
            input_bytes=canonical_json_bytes(manifest),
            output_bytes=canonical_json_bytes(composition.repository.read_snapshot()),
        )

        runtime_epoch = str(uuid.uuid4())
        if request.mode is ReplayMode.REVIEW_ONLY:
            active_stage = "review-runtime"
            progress.review_job_id = rebound_job.job_id
            receipt = _execute_one(composition, rebound_job.job_id, runtime_epoch)
            progress.review_outcome_id = receipt.job_outcome.outcome_id
            progress.stage(
                "review-runtime-completed-not-submitted",
                input_bytes=canonical_json_bytes(rebound_job),
                output_bytes=canonical_json_bytes(receipt.job_outcome),
            )
            progress.final_case_status = composition.repository.read_case(
                rebound_job.case_id
            ).case.status.value
            stop_reason = "REVIEW_OUTCOME_READY_NOT_SUBMITTED"
        else:
            active_stage = "diagnosis-runtime"
            progress.diagnosis_job_id = rebound_job.job_id
            diagnosis = _execute_one(composition, rebound_job.job_id, runtime_epoch)
            progress.diagnosis_outcome_id = diagnosis.job_outcome.outcome_id
            progress.stage(
                "diagnosis-runtime-completed",
                input_bytes=canonical_json_bytes(rebound_job),
                output_bytes=canonical_json_bytes(diagnosis.job_outcome),
            )
            if request.mode is ReplayMode.DIAGNOSE_ONLY:
                progress.final_case_status = composition.repository.read_case(
                    rebound_job.case_id
                ).case.status.value
                stop_reason = "DIAGNOSIS_OUTCOME_READY_NOT_SUBMITTED"
            else:
                active_stage = "diagnosis-submit"
                diagnosis_receipt = composition.application.submit_outcome(
                    diagnosis.job_outcome,
                    diagnosis.outcome_file_ref,
                )
                diagnosis_receipt = _require_applied_outcome_receipt(
                    diagnosis_receipt,
                    stage="diagnosis",
                )
                progress.stage(
                    "diagnosis-outcome-submitted",
                    input_bytes=canonical_json_bytes(diagnosis.job_outcome),
                    output_bytes=canonical_json_bytes(diagnosis_receipt),
                )
                review_job = _find_unique_review(composition, rebound_job.case_id)
                if review_job is None:
                    aggregate = composition.repository.read_case(rebound_job.case_id)
                    _require_completed_case_state(
                        aggregate,
                        allowed_statuses=_VALID_NO_REVIEW_STATUSES,
                        stop_reason="INVALID_NO_REVIEW_STATE",
                        message=(
                            "A diagnosis without a REVIEW Job must end in an explicit "
                            "waiting or unresolved state."
                        ),
                    )
                    progress.final_case_status = aggregate.case.status.value
                    progress.stage(
                        "no-review-job",
                        input_bytes=canonical_json_bytes(diagnosis.job_outcome),
                        output_bytes=canonical_json_bytes(
                            {
                                "case_status": aggregate.case.status.value,
                                "active_job_id": aggregate.case.active_job_id,
                            }
                        ),
                    )
                    stop_reason = "NO_REVIEW_JOB"
                else:
                    progress.review_job_id = review_job.job_id
                    active_stage = "review-runtime"
                    review = _execute_one(composition, review_job.job_id, runtime_epoch)
                    progress.review_outcome_id = review.job_outcome.outcome_id
                    progress.stage(
                        "review-runtime-completed",
                        input_bytes=canonical_json_bytes(review_job),
                        output_bytes=canonical_json_bytes(review.job_outcome),
                    )
                    active_stage = "review-submit"
                    review_receipt = composition.application.submit_outcome(
                        review.job_outcome,
                        review.outcome_file_ref,
                    )
                    review_receipt = _require_applied_outcome_receipt(
                        review_receipt,
                        stage="review",
                    )
                    progress.stage(
                        "review-outcome-submitted",
                        input_bytes=canonical_json_bytes(review.job_outcome),
                        output_bytes=canonical_json_bytes(review_receipt),
                    )
                    aggregate = composition.repository.read_case(rebound_job.case_id)
                    _require_completed_case_state(
                        aggregate,
                        allowed_statuses=_VALID_REVIEW_COMPLETION_STATUSES,
                        stop_reason="INVALID_REVIEW_COMPLETION_STATE",
                        message=(
                            "An applied REVIEW Outcome must end in an explicit "
                            "resolved, unresolved, or waiting state."
                        ),
                    )
                    progress.final_case_status = aggregate.case.status.value
                    stop_reason = "THROUGH_REVIEW_COMPLETED"

        result = progress.result(success=True, stop_reason=stop_reason, error=None)
        _publish_new(result_path, canonical_json_bytes(result), read_only=True)
        log_event(
            "replay.completed",
            replay_id=progress.replay_id,
            stop_reason=stop_reason,
            final_case_status=progress.final_case_status,
        )
        return result
    except ReplayError as exc:
        progress.failed_stage(active_stage)
        result = progress.result(
            success=False,
            stop_reason=exc.stop_reason,
            error=exc.error,
        )
        if progress.output_created:
            try:
                _publish_new(result_path, canonical_json_bytes(result), read_only=True)
            except OSError:
                pass
        log_event(
            "replay.failed",
            level=logging.ERROR,
            replay_id=progress.replay_id,
            stop_reason=exc.stop_reason,
            error_code=exc.error.code,
        )
        raise ReplayError(
            exc.error,
            stop_reason=exc.stop_reason,
            result=result,
        ) from None
    except ApplicationPortError as exc:
        progress.failed_stage(active_stage)
        result = progress.result(
            success=False,
            stop_reason="APPLICATION_PORT_FAILED",
            error=exc.error,
        )
        if progress.output_created:
            try:
                _publish_new(result_path, canonical_json_bytes(result), read_only=True)
            except OSError:
                pass
        raise ReplayError(
            exc.error,
            stop_reason="APPLICATION_PORT_FAILED",
            result=result,
        ) from None
    except Exception as exc:
        progress.failed_stage(active_stage)
        error = _error(
            ErrorCode.RESOURCE_PUBLISH_FAILED,
            "The isolated replay failed unexpectedly.",
        )
        result = progress.result(
            success=False,
            stop_reason="REPLAY_RUNTIME_FAILED",
            error=error,
        )
        if progress.output_created:
            try:
                _publish_new(result_path, canonical_json_bytes(result), read_only=True)
            except OSError:
                pass
        log_event(
            "replay.failed",
            level=logging.ERROR,
            replay_id=progress.replay_id,
            stop_reason="REPLAY_RUNTIME_FAILED",
            error=exc,
        )
        raise ReplayError(
            error,
            stop_reason="REPLAY_RUNTIME_FAILED",
            result=result,
        ) from None
    finally:
        if composition is not None:
            try:
                composition.close()
            except Exception:
                # The replay result is already authoritative; best-effort close
                # must not rewrite it or touch the source installation.
                pass
        if progress.output_created:
            configure_journey()


__all__ = [
    "ReplayAssetDiff",
    "ReplayError",
    "ReplayManifest",
    "ReplayMode",
    "ReplayRequest",
    "ReplayResult",
    "ReplayStageRecord",
    "run_replay_job",
    "validate_replay_paths",
]
