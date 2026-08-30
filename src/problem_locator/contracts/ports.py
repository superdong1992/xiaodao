"""Synchronous public ports for Problem Locator V1.

The protocols in this module are deliberately framework-neutral.  Async web
and process adapters may wrap them at the outer edge, but the contract visible
to the domain, application, storage, runtime, and dispatch slices stays
synchronous and explicit about ownership of streams and leases.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from .commands import (
    ApplicationCommand,
    ApplicationResponse,
    ArtifactListResponse,
    CancelReceipt,
    CaseQueryResponse,
    ClaimReceipt,
    DispatchReceipt,
    FailureReceipt,
    OpenArtifactResult,
    OutcomeReceipt,
    RecoveryReceipt,
)
from .enums import CancellationReason, ResourceKind, ResourceType
from .errors import ExecutionFailure
from .models import (
    Artifact,
    AssetAvailabilityReport,
    AttachmentStagedRef,
    CaseAggregate,
    CaseResourceUsage,
    CommitReceipt,
    ContextSnapshot,
    DiagnosisState,
    ExecutionFileRef,
    ExecutionLogSinks,
    Job,
    MaterializedPath,
    NonNegativeInt,
    OpaqueId,
    PlannedResourceTarget,
    PublishedJobReceipt,
    ReadinessReport,
    ResolvedAsset,
    ResourceRef,
    RuntimeBindings,
    RuntimeExecutionReceipt,
    Sha256,
    StagedResourceRef,
    StateFile,
    StateMutation,
    ValidationReport,
    VersionedRef,
    WaitSeconds,
    WorkspaceInputManifest,
    UtcTimestamp,
)
from .outcomes import (
    CaseSnapshot,
    CoordinatorPlanResult,
    JobOutcome,
    ValidatedTrigger,
)


@runtime_checkable
class BinaryStream(Protocol):
    """Forward-only immutable byte stream owned by its caller."""

    def read(self, max_bytes: int) -> bytes: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


@runtime_checkable
class AppendOnlyByteSink(Protocol):
    """All-or-error append-only sink used for bounded execution logs."""

    def write(self, chunk: bytes) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class CancellationSignal(Protocol):
    """Thread-safe, one-way cancellation observation surface."""

    @property
    def reason(self) -> CancellationReason | None: ...

    def is_cancelled(self) -> bool: ...

    def wait(self, timeout_seconds: float | None) -> bool: ...


@runtime_checkable
class LogparseBrokerSession(Protocol):
    """Job-scoped broker capability exposed to the runtime."""

    def agent_environment(self) -> dict[str, str]: ...

    def execute_preprocessing(
        self,
        operation: str,
        request_path: str,
        result_path: str,
    ) -> ExecutionFailure | None: ...

    def parse_request_bytes(self) -> bytes | None: ...

    def audit_bytes(self) -> bytes: ...

    def close(self) -> None: ...


@runtime_checkable
class AttachmentUploadLease(Protocol):
    """Opaque per-attachment upload capability."""

    @property
    def attachment_id(self) -> OpaqueId: ...

    def is_released(self) -> bool: ...

    def release(self) -> None: ...


@runtime_checkable
class AttachmentUploadGuard(Protocol):
    """Serialize the full upload lifecycle for one attachment ID."""

    def acquire(self, attachment_id: OpaqueId) -> AttachmentUploadLease: ...


@runtime_checkable
class Coordinator(Protocol):
    def plan(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
    ) -> CoordinatorPlanResult: ...


@runtime_checkable
class ContextSnapshotProjector(Protocol):
    def project(self, target_diagnosis_state: DiagnosisState) -> ContextSnapshot: ...


@runtime_checkable
class StateRepository(Protocol):
    def read_case(self, case_id: OpaqueId) -> CaseAggregate: ...

    def read_job(self, job_id: OpaqueId) -> Job: ...

    def read_artifact(self, artifact_id: OpaqueId) -> Artifact: ...

    def read_snapshot(self) -> StateFile: ...

    def commit(
        self,
        expected_generation: int,
        expected_case_revision: int | None,
        mutation: StateMutation,
    ) -> CommitReceipt: ...

    def validate_all(self) -> ValidationReport: ...

    def export_snapshot(self) -> bytes: ...


@runtime_checkable
class PublicationCommitLease(Protocol):
    def release(self) -> None: ...


@runtime_checkable
class PublicationCommitGuard(Protocol):
    def acquire(self) -> PublicationCommitLease: ...


@runtime_checkable
class ResourceStore(Protocol):
    def stage_file(
        self,
        owner_job_id: OpaqueId,
        proposal_key: str,
        stream: BinaryStream,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StagedResourceRef: ...

    def stage_generated_file(
        self,
        owner_job_id: OpaqueId,
        proposal_key: str,
        staging_id: OpaqueId,
        stream: BinaryStream,
        expected_size: int,
        expected_sha256: str,
    ) -> StagedResourceRef: ...

    def stage_tree(
        self,
        owner_job_id: OpaqueId,
        proposal_key: str,
        root: Path,
        expected_manifest_hash: str | None = None,
    ) -> StagedResourceRef: ...

    def stage_attachment(
        self,
        attachment_id: OpaqueId,
        upload_lease: AttachmentUploadLease,
        stream: BinaryStream,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> AttachmentStagedRef: ...

    def validate_staged(self, staged_ref: StagedResourceRef) -> None: ...

    def plan_target(
        self,
        case_id: OpaqueId,
        resource_type: ResourceType,
        resource_id: OpaqueId,
        resource_kind: ResourceKind,
        size: NonNegativeInt,
        sha256: Sha256,
    ) -> PlannedResourceTarget: ...

    def publish(
        self,
        staged_ref: StagedResourceRef | AttachmentStagedRef,
        final_storage_key: str,
    ) -> ResourceRef: ...

    def validate_case_capacity(
        self,
        case_id: OpaqueId,
        planned_final_targets: Sequence[PlannedResourceTarget],
    ) -> CaseResourceUsage: ...

    def open_read(self, resource_ref: ResourceRef) -> BinaryStream: ...

    def materialize_read_only(
        self,
        resource_ref: ResourceRef,
        destination: Path,
    ) -> MaterializedPath: ...

    def discard(
        self,
        staged_ref: StagedResourceRef | AttachmentStagedRef,
    ) -> None: ...


@runtime_checkable
class AssetCatalogPort(Protocol):
    def check(self, refs: Sequence[VersionedRef]) -> AssetAvailabilityReport: ...

    def resolve(self, ref: VersionedRef) -> ResolvedAsset: ...

    def route_bindings(
        self,
        user_fact_names: Sequence[str] = (),
    ) -> RuntimeBindings: ...

    def diagnose_bindings(self, skill_ref: VersionedRef) -> RuntimeBindings: ...

    def generic_diagnose_bindings(self) -> RuntimeBindings: ...

    def review_bindings(self, skill_ref: VersionedRef) -> RuntimeBindings: ...


@runtime_checkable
class LogparseBrokerFactory(Protocol):
    def open(
        self,
        job: Job,
        workspace_root: Path,
        workspace_manifest: WorkspaceInputManifest,
        cancellation: CancellationSignal,
    ) -> LogparseBrokerSession: ...


@runtime_checkable
class Dispatcher(Protocol):
    def submit(self, job_id: OpaqueId) -> DispatchReceipt: ...

    def cancel(self, job_id: OpaqueId) -> CancelReceipt: ...


@runtime_checkable
class StateChangeNotifier(Protocol):
    def notify(self, case_id: OpaqueId, generation: int) -> None: ...

    def wait_for_change(
        self,
        case_id: OpaqueId,
        after_generation: int,
        timeout_seconds: float,
    ) -> bool: ...


@runtime_checkable
class ExecutionRecordStore(Protocol):
    def publish_job(self, job: Job) -> ExecutionFileRef: ...

    def publish_outcome_bytes(
        self,
        job_id: OpaqueId,
        canonical_bytes: bytes,
    ) -> ExecutionFileRef: ...

    def publish_rejected_agent_output_bytes(
        self,
        job_id: OpaqueId,
        raw_bytes: bytes,
    ) -> ExecutionFileRef: ...

    def publish_audit_bytes(
        self,
        job_id: OpaqueId,
        filename: str,
        raw_bytes: bytes,
    ) -> ExecutionFileRef: ...

    def read_audit_bytes(
        self,
        job_id: OpaqueId,
        filename: str,
    ) -> bytes | None: ...

    def read_published_job(self, job_id: OpaqueId) -> PublishedJobReceipt | None: ...

    def read_published_outcome(
        self,
        job_id: OpaqueId,
    ) -> RuntimeExecutionReceipt | None: ...

    def open_log_sinks(
        self,
        job_id: OpaqueId,
        combined_limit_bytes: int,
    ) -> ExecutionLogSinks: ...


@runtime_checkable
class Runtime(Protocol):
    def execute(
        self,
        job: Job,
        cancellation: CancellationSignal,
    ) -> RuntimeExecutionReceipt: ...


@runtime_checkable
class ApplicationCommandPort(Protocol):
    def execute(self, command: ApplicationCommand) -> ApplicationResponse: ...


@runtime_checkable
class ApplicationQueryPort(Protocol):
    def get_case(
        self,
        case_id: OpaqueId,
        wait_for_job_id: OpaqueId | None = None,
        wait_seconds: WaitSeconds = 0,
    ) -> CaseQueryResponse: ...

    def list_artifacts(
        self,
        case_id: OpaqueId,
        include_internal: bool = False,
    ) -> ArtifactListResponse: ...

    def open_artifact(
        self,
        case_id: OpaqueId,
        artifact_id: OpaqueId,
    ) -> OpenArtifactResult: ...


@runtime_checkable
class JobControlPort(Protocol):
    def claim_job(
        self,
        job_id: OpaqueId,
        runtime_epoch: OpaqueId,
    ) -> ClaimReceipt: ...

    def submit_outcome(
        self,
        job_outcome: JobOutcome,
        outcome_file_ref: ExecutionFileRef,
    ) -> OutcomeReceipt: ...

    def report_execution_infrastructure_failure(
        self,
        job_id: OpaqueId,
        runtime_epoch: OpaqueId,
        failure_id: OpaqueId,
        execution_failure: ExecutionFailure,
    ) -> FailureReceipt: ...

    def interrupt_previous_epoch(
        self,
        current_runtime_epoch: OpaqueId,
        recovery_id: OpaqueId,
    ) -> RecoveryReceipt: ...


@runtime_checkable
class StateAdminPort(Protocol):
    def readiness(self) -> ReadinessReport: ...

    def validate_state(self) -> ValidationReport: ...

    def export_state(self) -> bytes: ...


@runtime_checkable
class Clock(Protocol):
    def now(self) -> UtcTimestamp: ...


@runtime_checkable
class IdGenerator(Protocol):
    def new(self, kind: str) -> OpaqueId: ...

    def derive(self, kind: str, stable_parts: Sequence[str]) -> OpaqueId: ...


__all__ = [
    "AppendOnlyByteSink",
    "ApplicationCommandPort",
    "ApplicationQueryPort",
    "AssetCatalogPort",
    "AttachmentUploadGuard",
    "AttachmentUploadLease",
    "BinaryStream",
    "CancellationSignal",
    "Clock",
    "ContextSnapshotProjector",
    "Coordinator",
    "Dispatcher",
    "ExecutionRecordStore",
    "IdGenerator",
    "JobControlPort",
    "LogparseBrokerFactory",
    "LogparseBrokerSession",
    "PublicationCommitGuard",
    "PublicationCommitLease",
    "ResourceStore",
    "Runtime",
    "StateAdminPort",
    "StateChangeNotifier",
    "StateRepository",
]
