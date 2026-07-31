"""Snapshot-consistent read side for the S03 application service."""

from __future__ import annotations

import time
from collections.abc import Callable

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

from .errors import raise_port_error
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
    ) -> None:
        self._repository = repository
        self._resource_store = resource_store
        self._notifier = notifier
        self._monotonic = monotonic

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

        snapshot = self._repository.read_snapshot()
        aggregate = snapshot.cases.get(case_id)
        if aggregate is None:
            raise_port_error(ErrorCode.CASE_NOT_FOUND, "The Case does not exist.")

        target_job_id = wait_for_job_id
        if target_job_id is not None:
            target_job = _find_job(snapshot, target_job_id)
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
                snapshot = self._repository.read_snapshot()
                if not changed and not _wait_is_complete(
                    snapshot, case_id, target_job_id
                ):
                    timed_out = True
                    break

        return CaseQueryResponse(
            case_view=project_case_view(snapshot, case_id),
            wait_timed_out=timed_out,
        )

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

        snapshot = self._repository.read_snapshot()
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

        snapshot = self._repository.read_snapshot()
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
