"""Snapshot-consistent read side for the S03 application service."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import ExitStack, suppress

from problem_locator.contracts import (
    ApplicationPortError,
    ArtifactListResponse,
    CaseQueryResponse,
    CaseStatus,
    ErrorCode,
    GetCase,
    Job,
    JobStatus,
    ListArtifacts,
    OpenArtifact,
    OpenArtifactResult,
    ResourceKind,
    ResourceRef,
    StateFile,
)
from problem_locator.contracts.ports import (
    ResourceStore,
    StateChangeNotifier,
    StateRepository,
)
from problem_locator.operational import OperationalState

from .errors import raise_port_error
from .reports import PublishedReport, read_published_report, published_artifacts
from .resource_usage import CaseResourceStream, case_resource_usage
from .projection import (
    project_artifact_summaries,
    project_artifact_summary,
    project_case_view,
)


_WAIT_STOP_CASE_STATUSES = frozenset(
    {
        CaseStatus.WAITING_INPUT,
        CaseStatus.WAITING_ATTACHMENT,
        CaseStatus.RESOLVED,
        CaseStatus.PARTIALLY_RESOLVED,
        CaseStatus.UNRESOLVED,
        CaseStatus.FAILED,
        CaseStatus.CANCELLED,
        CaseStatus.INTERRUPTED,
    }
)


class ApplicationQueryService:
    """Implement the frozen synchronous ``ApplicationQueryPort``."""

    def __init__(
        self,
        repository: StateRepository,
        resource_store: ResourceStore,
        notifier: StateChangeNotifier,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        operational_state: OperationalState | None = None,
    ) -> None:
        self._repository = repository
        self._resource_store = resource_store
        self._notifier = notifier
        self._monotonic = monotonic
        self._operational = operational_state

    def _checked_snapshot(self, case_id: str) -> StateFile:
        try:
            snapshot = self._repository.read_snapshot(case_id)
        except ApplicationPortError:
            error = None if self._operational is None else self._operational.error_for_case(case_id, None,
                include_archive_faults=False)
            if error is not None:
                raise ApplicationPortError(error) from None
            raise
        return self.check_snapshot(case_id, snapshot)

    def check_snapshot(self, case_id: str, snapshot: StateFile) -> StateFile:
        """Check an already captured Case without taking another repository read."""
        self._require_visible(case_id)
        aggregate = snapshot.cases.get(case_id)
        if aggregate is not None and self._operational is not None:
            for job in aggregate.jobs.values():
                if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}:
                    self._operational.confirm_finished_job(job.job_id)
            error = self._operational.error_for_case(case_id, aggregate.case.status.value,
                aggregate.case.archive_status, include_archive_faults=False)
            if error is not None:
                raise ApplicationPortError(error)
        return snapshot

    def _require_visible(self, case_id):
        deleted = getattr(self._repository, "is_agent_case_deleted", None)
        if deleted is not None and deleted(case_id):
            raise_port_error(ErrorCode.CASE_NOT_FOUND, "定位任务不存在或已删除。")

    def read_conversation_delivery(self, case_id: str, snapshot: StateFile, *, report: bool, artifacts: bool):
        with case_resource_usage(self._repository, case_id):
            aggregate = self.check_snapshot(case_id, snapshot).cases.get(case_id)
            if aggregate is None:
                raise_port_error(ErrorCode.CASE_NOT_FOUND, "定位任务不存在。")
            # The caller holds the conversation lease around this capture;
            # retain its Case resources until all selected report IO finishes.
            result = read_published_report(aggregate, self._resource_store) if report else None
            items = published_artifacts(aggregate) if artifacts else None
            return aggregate.case, result, items

    def get_case(
        self,
        case_id: str,
        wait_for_job_id: str | None = None,
        wait_seconds: int = 0,
    ) -> CaseQueryResponse:
        try:
            query = GetCase.model_validate(
                {
                    "case_id": case_id,
                    "wait_for_job_id": wait_for_job_id,
                    "wait_seconds": wait_seconds,
                },
                strict=True,
            )
        except (TypeError, ValueError):
            raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "ApplicationQueryPort.get_case received invalid raw input.",
            )
        case_id = query.case_id
        wait_for_job_id = query.wait_for_job_id
        wait_seconds = query.wait_seconds

        snapshot = self._checked_snapshot(case_id)
        aggregate = snapshot.cases.get(case_id)
        if aggregate is None:
            raise_port_error(ErrorCode.CASE_NOT_FOUND, "The Case does not exist.")

        target_job_id = wait_for_job_id
        if target_job_id is not None:
            target_job = self._repository.read_job(target_job_id)
            if target_job is None:
                raise_port_error(ErrorCode.JOB_NOT_FOUND, "The Job does not exist.")
            if target_job.case_id != case_id:
                raise_port_error(
                    ErrorCode.JOB_CASE_MISMATCH,
                    "The Job belongs to a different Case.",
                )
        elif aggregate.case.active_job_id is not None:
            target_job_id = aggregate.case.active_job_id

        timed_out = False
        if target_job_id is not None and wait_seconds > 0:
            deadline = self._monotonic() + wait_seconds
            while not _wait_is_complete(snapshot, case_id, target_job_id):
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                # A notification is only a hint.  Always refresh the complete
                # StateFile before inspecting Case and Job again.
                try:
                    changed = self._notifier.wait_for_change(
                        case_id,
                        snapshot.generation,
                        remaining,
                    )
                except Exception:
                    # Waiting is only an optimization.  Even when the notifier
                    # fails, refresh authoritative state before deciding whether
                    # the finite wait completed or timed out.
                    changed = False
                snapshot = self._checked_snapshot(case_id)
                if not changed and not _wait_is_complete(
                    snapshot, case_id, target_job_id
                ):
                    timed_out = True
                    break

        return CaseQueryResponse(
            case_view=project_case_view(snapshot, case_id),
            wait_timed_out=timed_out,
        )

    def get_report(self, case_id: str) -> PublishedReport:
        """Select and read the public report against one checked snapshot."""
        try:
            case_id = GetCase.model_validate({"case_id": case_id}, strict=True).case_id
        except (TypeError, ValueError):
            raise_port_error(ErrorCode.VALIDATION_ERROR, "任务标识无效。")
        with case_resource_usage(self._repository, case_id):
            aggregate = self._checked_snapshot(case_id).cases.get(case_id)
            if aggregate is None:
                raise_port_error(ErrorCode.CASE_NOT_FOUND, "定位任务不存在。")
            return read_published_report(aggregate, self._resource_store)

    def list_artifacts(
        self,
        case_id: str,
        include_internal: bool = False,
    ) -> ArtifactListResponse:
        try:
            query = ListArtifacts.model_validate(
                {
                    "case_id": case_id,
                    "include_internal": include_internal,
                },
                strict=True,
            )
        except (TypeError, ValueError):
            raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "ApplicationQueryPort.list_artifacts received invalid raw input.",
            )
        case_id = query.case_id
        include_internal = query.include_internal
        self._require_visible(case_id)
        snapshot = self._repository.read_snapshot(case_id)
        aggregate = snapshot.cases.get(case_id)
        if aggregate is None:
            raise_port_error(ErrorCode.CASE_NOT_FOUND, "The Case does not exist.")
        return ArtifactListResponse(
            artifacts=project_artifact_summaries(
                aggregate.case,
                aggregate.artifacts.values(),
                include_internal=include_internal,
            )
        )

    def open_artifact(
        self,
        case_id: str,
        artifact_id: str,
    ) -> OpenArtifactResult:
        try:
            query = OpenArtifact.model_validate(
                {"case_id": case_id, "artifact_id": artifact_id},
                strict=True,
            )
        except (TypeError, ValueError):
            raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "ApplicationQueryPort.open_artifact received invalid raw input.",
            )
        case_id = query.case_id
        artifact_id = query.artifact_id
        with ExitStack() as resources:
            resources.enter_context(case_resource_usage(self._repository, case_id))
            opened = self._open_artifact(case_id, artifact_id)
            resources.callback(opened.stream.close)
            stream = CaseResourceStream(opened.stream, resources.pop_all())
            try:
                return OpenArtifactResult(artifact=opened.artifact, stream=stream)
            except BaseException:
                with suppress(Exception):
                    stream.close()
                raise

    def _open_artifact(self, case_id: str, artifact_id: str) -> OpenArtifactResult:
        self._require_visible(case_id)
        snapshot = self._repository.read_snapshot(case_id)
        aggregate = snapshot.cases.get(case_id)
        if aggregate is None:
            raise_port_error(ErrorCode.CASE_NOT_FOUND, "The Case does not exist.")
        artifact = aggregate.artifacts.get(artifact_id)
        if artifact is None or artifact.case_id != case_id:
            raise_port_error(
                ErrorCode.ARTIFACT_NOT_FOUND,
                "The downloadable Artifact does not exist.",
            )
        summary = project_artifact_summary(aggregate.case, artifact)
        if not summary.downloadable or artifact.resource_kind is not ResourceKind.FILE:
            raise_port_error(
                ErrorCode.ARTIFACT_NOT_FOUND,
                "The downloadable Artifact does not exist.",
            )
        resource_ref = ResourceRef(
            resource_kind=artifact.resource_kind,
            storage_key=artifact.storage_key,
            size=artifact.size,
            sha256=artifact.sha256,
        )
        try:
            stream = self._resource_store.open_read(resource_ref)
        except ApplicationPortError as error:
            # A public Artifact can only contain a contract-valid immutable
            # storage key.  Do not leak S02's PATH_VIOLATION vocabulary through
            # a query Port that does not permit it.
            if error.error.code is ErrorCode.PATH_VIOLATION:
                raise_port_error(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    "The Artifact resource is unavailable.",
                )
            raise
        return OpenArtifactResult(artifact=summary, stream=stream)


def _find_job(snapshot: StateFile, job_id: str) -> Job | None:
    for aggregate in snapshot.cases.values():
        job = aggregate.jobs.get(job_id)
        if job is not None:
            return job
    return None


def _wait_is_complete(snapshot: StateFile, case_id: str, job_id: str) -> bool:
    aggregate = snapshot.cases.get(case_id)
    if aggregate is None:
        raise_port_error(ErrorCode.CASE_NOT_FOUND, "The Case does not exist.")
    if aggregate.case.status in _WAIT_STOP_CASE_STATUSES:
        return True
    job = aggregate.jobs.get(job_id)
    if job is None:
        # The target was already validated in the initial snapshot.  Losing it
        # would indicate state corruption, which cannot be represented by the
        # read Port; refresh projection validation will surface the invariant.
        return True
    return job.status not in {JobStatus.PENDING, JobStatus.RUNNING}


__all__ = ["ApplicationQueryService"]
