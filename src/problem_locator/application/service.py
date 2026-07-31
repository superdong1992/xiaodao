"""Composition root and public Port facade for the S03 application service."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from problem_locator.contracts import (
    ApplicationCommand,
    ApplicationPortError,
    ApplicationResponse,
    ArtifactListResponse,
    AttachmentUploadGuard,
    Case,
    CaseQueryResponse,
    ClaimReceipt,
    Clock,
    ContextSnapshotProjector,
    Coordinator,
    Dispatcher,
    ExecutionFailure,
    ExecutionFileRef,
    ExecutionRecordStore,
    ErrorCode,
    FailureReceipt,
    IdGenerator,
    JobOutcome,
    OpenArtifactResult,
    OutcomeReceipt,
    PublicationCommitGuard,
    RecoveryReceipt,
    ResourceStore,
    StateChangeNotifier,
    StateRepository,
    UploadAttachmentContent,
)
from problem_locator.contracts.ports import AssetCatalogPort

from .external_commands import ExternalCommandHandler
from .job_control import JobControlService
from .outcome_submission import OutcomeSubmissionService
from .queries import ApplicationQueryService
from .uploads import AttachmentUploadService


@dataclass(frozen=True, slots=True)
class ApplicationService:
    """One facade implementing command, query, and JobControl public Ports."""

    external_commands: ExternalCommandHandler
    uploads: AttachmentUploadService
    queries: ApplicationQueryService
    job_control: JobControlService
    outcomes: OutcomeSubmissionService

    def execute(self, command: ApplicationCommand) -> ApplicationResponse:
        if isinstance(command, UploadAttachmentContent):
            receipt = self.uploads.execute(command)
            assert receipt.case_id is not None
            try:
                query = self.queries.get_case(receipt.case_id)
            except ApplicationPortError as error:
                if error.error.code not in {
                    ErrorCode.STATE_CORRUPT,
                    ErrorCode.STATE_SCHEMA_UNSUPPORTED,
                }:
                    raise
                # The upload mutation and its receipt are already durable.
                # A post-commit projection failure is exposed through
                # readiness and must not turn this command into a false
                # negative that invites a second body upload.
                return ApplicationResponse(
                    business_receipt=receipt,
                    case_view=None,
                    wait_timed_out=False,
                    dispatch_pending=False,
                )
            return ApplicationResponse(
                business_receipt=receipt,
                case_view=query.case_view,
                wait_timed_out=False,
                dispatch_pending=False,
            )
        return self.external_commands.execute(command)

    def get_case(
        self,
        case_id: str,
        wait_for_job_id: str | None = None,
        wait_seconds: int = 0,
    ) -> CaseQueryResponse:
        return self.queries.get_case(case_id, wait_for_job_id, wait_seconds)

    def list_artifacts(
        self,
        case_id: str,
        include_internal: bool = False,
    ) -> ArtifactListResponse:
        return self.queries.list_artifacts(case_id, include_internal)

    def open_artifact(
        self,
        case_id: str,
        artifact_id: str,
    ) -> OpenArtifactResult:
        return self.queries.open_artifact(case_id, artifact_id)

    def claim_job(self, job_id: str, runtime_epoch: str) -> ClaimReceipt:
        return self.job_control.claim_job(job_id, runtime_epoch)

    def submit_outcome(
        self,
        job_outcome: JobOutcome,
        outcome_file_ref: ExecutionFileRef,
    ) -> OutcomeReceipt:
        return self.outcomes.submit_outcome(job_outcome, outcome_file_ref)

    def report_execution_infrastructure_failure(
        self,
        job_id: str,
        runtime_epoch: str,
        failure_id: str,
        execution_failure: ExecutionFailure,
    ) -> FailureReceipt:
        return self.job_control.report_execution_infrastructure_failure(
            job_id,
            runtime_epoch,
            failure_id,
            execution_failure,
        )

    def interrupt_previous_epoch(
        self,
        current_runtime_epoch: str,
        recovery_id: str,
    ) -> RecoveryReceipt:
        return self.job_control.interrupt_previous_epoch(
            current_runtime_epoch,
            recovery_id,
        )


def build_application_service(
    *,
    repository: StateRepository,
    resource_store: ResourceStore,
    publication_guard: PublicationCommitGuard,
    upload_guard: AttachmentUploadGuard,
    execution_records: ExecutionRecordStore,
    coordinator: Coordinator,
    projector: ContextSnapshotProjector,
    asset_catalog: AssetCatalogPort,
    dispatcher: Dispatcher,
    notifier: StateChangeNotifier,
    clock: Clock,
    ids: IdGenerator,
    stable_target_detector: Callable[[Case, Mapping[str, str]], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ApplicationService:
    """Wire every S03 handler from only frozen S00 Port dependencies."""

    external = ExternalCommandHandler(
        repository=repository,
        coordinator=coordinator,
        projector=projector,
        publication_guard=publication_guard,
        resource_store=resource_store,
        execution_records=execution_records,
        asset_catalog=asset_catalog,
        dispatcher=dispatcher,
        notifier=notifier,
        clock=clock,
        ids=ids,
        stable_target_detector=stable_target_detector,
        monotonic=monotonic,
    )
    upload = AttachmentUploadService(
        repository=repository,
        resource_store=resource_store,
        publication_guard=publication_guard,
        upload_guard=upload_guard,
        clock=clock,
        notifier=notifier,
    )
    query = ApplicationQueryService(
        repository,
        resource_store,
        notifier,
        monotonic=monotonic,
    )
    job_control = JobControlService(
        repository=repository,
        publication_guard=publication_guard,
        coordinator=coordinator,
        asset_catalog=asset_catalog,
        notifier=notifier,
        clock=clock,
        ids=ids,
    )
    outcomes = OutcomeSubmissionService(
        repository,
        resource_store,
        publication_guard,
        execution_records,
        coordinator,
        projector,
        asset_catalog,
        dispatcher,
        notifier,
        clock,
        ids,
    )
    return ApplicationService(external, upload, query, job_control, outcomes)


__all__ = ["ApplicationService", "build_application_service"]
