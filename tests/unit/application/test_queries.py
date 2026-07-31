from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from problem_locator.application.queries import ApplicationQueryService
from problem_locator.contracts import (
    ApplicationError,
    ApplicationPortError,
    Artifact,
    ArtifactKind,
    CaseStatus,
    ERROR_SPECS,
    ErrorCode,
    JobStatus,
    ResourceKind,
    ResourceRef,
    StateFile,
)
from tests.contracts.fakes import InMemoryResourceStore, InMemoryStateRepository


ROOT = Path(__file__).resolve().parents[3]
CASE_ID = "00000000-0000-0000-0000-000000000001"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000061"
NOW = "2026-07-31T00:00:20.000Z"


def _state() -> StateFile:
    payload = json.loads(
        (ROOT / "tests/fixtures/contracts/positive/state.json").read_text(
            encoding="utf-8"
        )
    )
    return StateFile.model_validate(payload)


class _Notifier:
    def __init__(
        self,
        on_wait=None,
        *,
        changed: bool = False,
        failure: BaseException | None = None,
    ) -> None:
        self.on_wait = on_wait
        self.changed = changed
        self.failure = failure
        self.wait_calls: list[tuple[str, int, float]] = []

    def notify(self, case_id: str, generation: int) -> None:
        return None

    def wait_for_change(
        self,
        case_id: str,
        after_generation: int,
        timeout_seconds: float,
    ) -> bool:
        self.wait_calls.append((case_id, after_generation, timeout_seconds))
        if self.on_wait is not None:
            self.on_wait()
        if self.failure is not None:
            raise self.failure
        return self.changed


class _Monotonic:
    def __init__(self, *values: float) -> None:
        self.values = iter(values)

    def __call__(self) -> float:
        return next(self.values)


class _FailingReadRepository(InMemoryStateRepository):
    def __init__(self, code: ErrorCode) -> None:
        super().__init__(_state())
        self.error = ApplicationPortError(
            ApplicationError(
                code=code,
                message="injected state read failure",
                details=[],
                retryable=ERROR_SPECS[code].application_retryable,
            )
        )

    def read_snapshot(self) -> StateFile:
        raise self.error


class _CountingRepository(InMemoryStateRepository):
    def __init__(self, state: StateFile) -> None:
        super().__init__(state)
        self.read_snapshot_calls = 0

    def read_snapshot(self) -> StateFile:
        self.read_snapshot_calls += 1
        return super().read_snapshot()


def _service(
    repository: InMemoryStateRepository,
    store: InMemoryResourceStore,
    notifier: _Notifier,
    *,
    monotonic=None,
) -> ApplicationQueryService:
    kwargs = {} if monotonic is None else {"monotonic": monotonic}
    return ApplicationQueryService(repository, store, notifier, **kwargs)


def _diagnostic_artifact(payload: bytes) -> Artifact:
    digest = hashlib.sha256(payload).hexdigest()
    return Artifact(
        artifact_id=ARTIFACT_ID,
        case_id=CASE_ID,
        kind=ArtifactKind.DIAGNOSTIC_EXPORT,
        name="diagnostic.json",
        content_type="application/json",
        resource_kind=ResourceKind.FILE,
        size=len(payload),
        sha256=digest,
        storage_key=(
            f"resources/cases/{CASE_ID}/artifacts/{ARTIFACT_ID}/payload"
        ),
        metadata={
            "schema_version": 1,
            "format_id": "diagnostic-export-v1",
            "description": "A diagnostic export.",
        },
        created_by_job_id="00000000-0000-0000-0000-000000000010",
        created_at=NOW,
    )


def _with_artifact(state: StateFile, artifact: Artifact) -> StateFile:
    aggregate = state.cases[CASE_ID]
    changed = aggregate.model_copy(update={"artifacts": {artifact.artifact_id: artifact}})
    return state.model_copy(update={"cases": {CASE_ID: changed}})


def test_get_case_uses_one_snapshot_when_no_wait_is_requested() -> None:
    state = _state()
    repository = InMemoryStateRepository(state)
    notifier = _Notifier()

    response = _service(
        repository, InMemoryResourceStore(), notifier
    ).get_case(CASE_ID)

    assert response.case_view.case_id == CASE_ID
    assert response.case_view.active_job is not None
    assert response.wait_timed_out is False
    assert notifier.wait_calls == []


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        ("get_case", ("not-a-case-id", None, 0)),
        ("get_case", (CASE_ID, "not-a-job-id", 0)),
        ("get_case", (CASE_ID, None, -1)),
        ("get_case", (CASE_ID, None, 31)),
        ("get_case", (CASE_ID, None, True)),
        ("get_case", (CASE_ID, None, 1.5)),
        ("get_case", (CASE_ID, None, "1")),
        ("list_artifacts", ("not-a-case-id", False)),
        ("list_artifacts", (CASE_ID, 0)),
        ("list_artifacts", (CASE_ID, 1)),
        ("list_artifacts", (CASE_ID, "true")),
        ("open_artifact", ("not-a-case-id", ARTIFACT_ID)),
        ("open_artifact", (CASE_ID, "not-an-artifact-id")),
    ],
)
def test_raw_query_input_is_rebuilt_before_any_dependency_call(
    operation: str,
    args: tuple[object, ...],
) -> None:
    repository = _CountingRepository(_state())
    store = InMemoryResourceStore()
    notifier = _Notifier()
    service = _service(repository, store, notifier)

    with pytest.raises(ApplicationPortError) as captured:
        getattr(service, operation)(*args)

    assert captured.value.error.code is ErrorCode.VALIDATION_ERROR
    assert captured.value.error.retryable is False
    assert repository.read_snapshot_calls == 0
    assert notifier.wait_calls == []


def test_wait_notification_is_only_a_hint_and_service_rereads_snapshot() -> None:
    initial = _state()
    repository = InMemoryStateRepository(initial)
    aggregate = initial.cases[CASE_ID]
    active = aggregate.jobs[aggregate.case.active_job_id]
    finished = active.model_copy(
        update={"status": JobStatus.SUCCEEDED, "finished_at": NOW}
    )
    waiting_case = aggregate.case.model_copy(
        update={
            "status": CaseStatus.WAITING_INPUT,
            "active_job_id": None,
            "case_revision": aggregate.case.case_revision + 1,
            "updated_at": NOW,
        }
    )
    final_aggregate = aggregate.model_copy(
        update={"case": waiting_case, "jobs": {finished.job_id: finished}}
    )
    final = initial.model_copy(
        update={
            "generation": initial.generation + 1,
            "cases": {CASE_ID: final_aggregate},
        }
    )
    notifier = _Notifier(lambda: repository.seed(final), changed=True)

    response = _service(
        repository,
        InMemoryResourceStore(),
        notifier,
        monotonic=_Monotonic(0.0, 0.0),
    ).get_case(CASE_ID, wait_seconds=10)

    assert response.case_view.status is CaseStatus.WAITING_INPUT
    assert response.wait_timed_out is False
    assert notifier.wait_calls == [(CASE_ID, initial.generation, 10.0)]


def test_wait_timeout_does_not_cancel_or_mutate_the_job() -> None:
    state = _state()
    repository = InMemoryStateRepository(state)
    notifier = _Notifier(changed=False)

    response = _service(
        repository,
        InMemoryResourceStore(),
        notifier,
        monotonic=_Monotonic(0.0, 0.0),
    ).get_case(CASE_ID, wait_seconds=5)

    assert response.wait_timed_out is True
    assert response.case_view.active_job.status is JobStatus.PENDING
    assert repository.commit_calls == []


def test_wait_notifier_failure_still_rereads_and_returns_normal_timeout() -> None:
    initial = _state()
    repository = InMemoryStateRepository(initial)
    aggregate = initial.cases[CASE_ID]
    refreshed_case = aggregate.case.model_copy(
        update={
            "case_revision": aggregate.case.case_revision + 1,
            "updated_at": NOW,
        }
    )
    refreshed = initial.model_copy(
        update={
            "generation": initial.generation + 1,
            "cases": {
                CASE_ID: aggregate.model_copy(update={"case": refreshed_case})
            },
        }
    )
    notifier = _Notifier(
        lambda: repository.seed(refreshed),
        failure=RuntimeError("notifier unavailable"),
    )

    response = _service(
        repository,
        InMemoryResourceStore(),
        notifier,
        monotonic=_Monotonic(0.0, 0.0),
    ).get_case(CASE_ID, wait_seconds=5)

    assert response.wait_timed_out is True
    assert response.case_view.case_revision == refreshed_case.case_revision
    assert response.case_view.active_job.status is JobStatus.PENDING
    assert notifier.wait_calls == [(CASE_ID, initial.generation, 5.0)]
    assert repository.commit_calls == []


def test_query_not_found_failures_use_the_frozen_typed_port_error() -> None:
    service = _service(
        InMemoryStateRepository(_state()),
        InMemoryResourceStore(),
        _Notifier(),
    )

    with pytest.raises(ApplicationPortError) as missing_case:
        service.get_case("00000000-0000-0000-0000-000000000999")
    assert missing_case.value.error.code is ErrorCode.CASE_NOT_FOUND

    with pytest.raises(ApplicationPortError) as missing_job:
        service.get_case(
            CASE_ID,
            wait_for_job_id="00000000-0000-0000-0000-000000000999",
        )
    assert missing_job.value.error.code is ErrorCode.JOB_NOT_FOUND


@pytest.mark.parametrize(
    "code",
    [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED],
)
@pytest.mark.parametrize("operation", ["get_case", "list_artifacts", "open_artifact"])
def test_query_preflight_preserves_typed_state_read_failure(
    code: ErrorCode,
    operation: str,
) -> None:
    repository = _FailingReadRepository(code)
    service = _service(repository, InMemoryResourceStore(), _Notifier())

    with pytest.raises(ApplicationPortError) as captured:
        if operation == "get_case":
            service.get_case(CASE_ID)
        elif operation == "list_artifacts":
            service.list_artifacts(CASE_ID)
        else:
            service.open_artifact(CASE_ID, ARTIFACT_ID)

    assert captured.value is repository.error


def test_list_and_open_artifact_hide_storage_and_validate_immutable_bytes() -> None:
    payload = b"{}"
    artifact = _diagnostic_artifact(payload)
    state = _with_artifact(_state(), artifact)
    repository = InMemoryStateRepository(state)
    store = InMemoryResourceStore()
    store.seed_formal_resource(
        ResourceRef(
            resource_kind=artifact.resource_kind,
            storage_key=artifact.storage_key,
            size=artifact.size,
            sha256=artifact.sha256,
        ),
        state_reference_count=1,
        payload=payload,
    )
    service = _service(repository, store, _Notifier())

    listed = service.list_artifacts(CASE_ID)
    opened = service.open_artifact(CASE_ID, ARTIFACT_ID)

    assert [item.artifact_id for item in listed.artifacts] == [ARTIFACT_ID]
    assert "storage_key" not in listed.artifacts[0].model_dump(mode="json")
    assert opened.stream.read(3) == payload
    assert opened.artifact.downloadable is True


def test_open_internal_artifact_is_indistinguishable_from_not_found() -> None:
    payload = b"{}"
    artifact = _diagnostic_artifact(payload).model_copy(
        update={
            "kind": ArtifactKind.LOGPARSE_RUN,
            "content_type": "application/vnd.problem-locator.logparse-run+directory",
            "resource_kind": ResourceKind.DIRECTORY,
            "metadata": {
                "tree_manifest_sha256": hashlib.sha256(payload).hexdigest(),
                "logparse_version_ref": {
                    "id": "logparse",
                    "version": "1.0.0",
                    "content_hash": "a" * 64,
                },
                "parse_manifest_relative_path": "parse_manifest.json",
                "source_attachment_id": "00000000-0000-0000-0000-000000000020",
                "source_attachment_sha256": "b" * 64,
                "parse_parameters": {"product": "generic"},
            },
        }
    )
    service = _service(
        InMemoryStateRepository(_with_artifact(_state(), artifact)),
        InMemoryResourceStore(),
        _Notifier(),
    )

    with pytest.raises(ApplicationPortError) as captured:
        service.open_artifact(CASE_ID, ARTIFACT_ID)
    assert captured.value.error.code is ErrorCode.ARTIFACT_NOT_FOUND
