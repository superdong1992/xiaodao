from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from problem_locator.application.uploads import AttachmentUploadService
from problem_locator.contracts import (
    ApplicationPortError,
    Attachment,
    AttachmentStatus,
    CaseAggregate,
    ErrorCode,
    MAX_CASE_RESOURCE_BYTES,
    ResourceKind,
    ResourceType,
    StateFile,
    UploadAttachmentContent,
)
from problem_locator.storage.coordination import (
    AttachmentUploadRegistry,
    InProcessAttachmentUploadGuard,
    InProcessPublicationCommitGuard,
    StorageCoordinationLock,
)
from tests.contracts.fakes import (
    FakeClock,
    InMemoryBinaryStream,
    InMemoryResourceStore,
    InMemoryStateChangeNotifier,
    InMemoryStateRepository,
)


ROOT = Path(__file__).resolve().parents[2]
CASE_ID = "00000000-0000-0000-0000-000000000001"
FIRST_ATTACHMENT_ID = "00000000-0000-0000-0000-000000000181"
SECOND_ATTACHMENT_ID = "00000000-0000-0000-0000-000000000182"
EXISTING_RESOURCE_ID = "00000000-0000-0000-0000-000000000183"
NOW = "2026-07-31T18:01:02.000Z"
FIRST_PAYLOAD = b"the upload that exactly fills remaining Case capacity"
SECOND_PAYLOAD = b"the concurrent upload that must not overcommit capacity"


class _PhaseRecorder:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._events: list[tuple[str, str, str | None]] = []

    def record(self, phase: str, attachment_id: str | None = None) -> None:
        with self._condition:
            self._events.append(
                (phase, threading.current_thread().name, attachment_id)
            )
            self._condition.notify_all()

    def wait_for(
        self,
        phase: str,
        thread_name: str,
        *,
        timeout: float = 5.0,
    ) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not any(
                event_phase == phase and event_thread == thread_name
                for event_phase, event_thread, _ in self._events
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def contains(self, phase: str, thread_name: str) -> bool:
        with self._condition:
            return any(
                event_phase == phase and event_thread == thread_name
                for event_phase, event_thread, _ in self._events
            )

    def index(self, phase: str, thread_name: str) -> int:
        with self._condition:
            return next(
                index
                for index, (event_phase, event_thread, _) in enumerate(self._events)
                if event_phase == phase and event_thread == thread_name
            )


class _ObservedAttachmentUploadGuard(InProcessAttachmentUploadGuard):
    def __init__(
        self,
        registry: AttachmentUploadRegistry,
        recorder: _PhaseRecorder,
    ) -> None:
        super().__init__(registry)
        self._recorder = recorder

    def acquire(self, attachment_id: str):
        self._recorder.record("upload-attempt", attachment_id)
        lease = super().acquire(attachment_id)
        self._recorder.record("upload-acquired", attachment_id)
        return lease


class _ObservedPublicationCommitGuard(InProcessPublicationCommitGuard):
    def __init__(
        self,
        coordination_lock: StorageCoordinationLock,
        recorder: _PhaseRecorder,
    ) -> None:
        super().__init__(coordination_lock)
        self._recorder = recorder

    def acquire(self):
        self._recorder.record("publication-attempt")
        lease = super().acquire()
        self._recorder.record("publication-acquired")
        return lease

    def _release(self, lease) -> None:
        self._recorder.record("publication-release")
        super()._release(lease)
        self._recorder.record("publication-released")


class _GuardObservedStream(InMemoryBinaryStream):
    def __init__(
        self,
        data: bytes,
        *,
        attachment_id: str,
        registry: AttachmentUploadRegistry,
    ) -> None:
        super().__init__(data)
        self._attachment_id = attachment_id
        self._registry = registry
        self.read_guard_states: list[bool] = []
        self.close_guard_states: list[bool] = []

    def read(self, max_bytes: int) -> bytes:
        self.read_guard_states.append(
            self._registry.held_by_current_thread(self._attachment_id)
        )
        return super().read(max_bytes)

    def close(self) -> None:
        self.close_guard_states.append(
            self._registry.held_by_current_thread(self._attachment_id)
        )
        super().close()


class _ObservedResourceStore(InMemoryResourceStore):
    def __init__(
        self,
        *,
        registry: AttachmentUploadRegistry,
        publication_guard: _ObservedPublicationCommitGuard,
        recorder: _PhaseRecorder,
    ) -> None:
        # The existing fake accepts any frozen lease when upload_guard is None.
        # This integration wrapper additionally validates the real S02 registry
        # at every Attachment phase without duplicating ResourceStore behavior.
        super().__init__(publication_guard=publication_guard)
        self.registry = registry
        self.observed_publication_guard = publication_guard
        self.recorder = recorder
        self.capacity_guard_states: list[tuple[str, bool, bool]] = []
        self.publish_guard_states: list[tuple[str, bool, bool]] = []

    def stage_attachment(
        self,
        attachment_id,
        upload_lease,
        stream,
        expected_size=None,
        expected_sha256=None,
    ):
        self.registry.validate_lease(attachment_id, upload_lease)
        self.recorder.record("stage-enter", attachment_id)
        staged = super().stage_attachment(
            attachment_id,
            upload_lease,
            stream,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
        self.registry.validate_lease(attachment_id, upload_lease)
        self.recorder.record("stage-complete", attachment_id)
        return staged

    def validate_case_capacity(self, case_id, planned_final_targets):
        targets = tuple(planned_final_targets)
        assert len(targets) == 1
        attachment_id = targets[0].resource_id
        states = (
            attachment_id,
            self.registry.held_by_current_thread(attachment_id),
            self.observed_publication_guard.held_by_current_thread(),
        )
        self.capacity_guard_states.append(states)
        self.recorder.record("capacity", attachment_id)
        return super().validate_case_capacity(case_id, targets)

    def publish(self, staged_ref, final_storage_key):
        attachment_id = staged_ref.attachment_id
        states = (
            attachment_id,
            self.registry.held_by_current_thread(attachment_id),
            self.observed_publication_guard.held_by_current_thread(),
        )
        self.publish_guard_states.append(states)
        self.recorder.record("publish", attachment_id)
        return super().publish(staged_ref, final_storage_key)


class _CapacityBarrierResourceStore(_ObservedResourceStore):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.both_staged = threading.Barrier(2)
        self.accepted_capacity_checked = threading.Event()
        self.allow_accepted_publish = threading.Event()
        self.rejected_stage_released = threading.Event()
        self.capacity_usages: dict[str, Any] = {}

    def stage_attachment(self, attachment_id, *args, **kwargs):
        staged = super().stage_attachment(attachment_id, *args, **kwargs)
        self.both_staged.wait(timeout=5.0)
        if attachment_id == SECOND_ATTACHMENT_ID:
            if not self.accepted_capacity_checked.wait(5.0):
                raise AssertionError("accepted upload never reached capacity check")
            self.rejected_stage_released.set()
        return staged

    def validate_case_capacity(self, case_id, planned_final_targets):
        targets = tuple(planned_final_targets)
        usage = super().validate_case_capacity(case_id, targets)
        attachment_id = targets[0].resource_id
        self.capacity_usages[attachment_id] = usage
        if attachment_id == FIRST_ATTACHMENT_ID:
            self.accepted_capacity_checked.set()
            if not self.allow_accepted_publish.wait(5.0):
                raise AssertionError("capacity barrier was not released")
        return usage


class _ObservedRepository(InMemoryStateRepository):
    def __init__(
        self,
        state: StateFile,
        *,
        registry: AttachmentUploadRegistry,
        publication_guard: _ObservedPublicationCommitGuard,
        recorder: _PhaseRecorder,
        blocked_attachment_id: str | None = None,
    ) -> None:
        super().__init__(state)
        self.registry = registry
        self.publication_guard = publication_guard
        self.recorder = recorder
        self.blocked_attachment_id = blocked_attachment_id
        self.commit_entered = threading.Event()
        self.allow_commit = threading.Event()
        self.commit_guard_states: list[tuple[str, bool, bool]] = []
        self._block_lock = threading.Lock()
        self._blocked_once = False

    def read_snapshot(self) -> StateFile:
        snapshot = super().read_snapshot()
        self.recorder.record("snapshot-read")
        return snapshot

    def commit(
        self,
        expected_generation: int,
        expected_case_revision: int | None,
        mutation: Any,
    ):
        attachment_ids = tuple(
            attachment.attachment_id for attachment in mutation.upsert_attachments
        )
        assert len(attachment_ids) == 1
        attachment_id = attachment_ids[0]
        states = (
            attachment_id,
            self.registry.held_by_current_thread(attachment_id),
            self.publication_guard.held_by_current_thread(),
        )
        self.commit_guard_states.append(states)
        self.recorder.record("commit-enter", attachment_id)

        should_block = False
        with self._block_lock:
            if (
                attachment_id == self.blocked_attachment_id
                and not self._blocked_once
            ):
                self._blocked_once = True
                should_block = True
        if should_block:
            self.commit_entered.set()
            if not self.allow_commit.wait(5.0):
                raise AssertionError("commit barrier was not released")

        receipt = super().commit(
            expected_generation,
            expected_case_revision,
            mutation,
        )
        self.recorder.record("commit-applied", attachment_id)
        return receipt


def _upload_state(*attachment_ids: str) -> StateFile:
    base = StateFile.model_validate_json(
        (ROOT / "tests/fixtures/contracts/positive/state.json").read_text(
            encoding="utf-8"
        )
    )
    aggregate = base.cases[CASE_ID]
    attachments = dict(aggregate.attachments)
    for index, attachment_id in enumerate(attachment_ids):
        attachments[attachment_id] = Attachment(
            attachment_id=attachment_id,
            case_id=CASE_ID,
            status=AttachmentStatus.UPLOADING,
            name=f"rpc-{index}.log",
            content_type="application/octet-stream",
            declared_size=None,
            declared_sha256=None,
            size=None,
            sha256=None,
            storage_key=None,
            created_at=NOW,
            updated_at=NOW,
        )
    aggregate_payload = aggregate.model_dump(mode="python")
    aggregate_payload["attachments"] = attachments
    state_payload = base.model_dump(mode="python")
    state_payload["cases"][CASE_ID] = CaseAggregate.model_validate(
        aggregate_payload
    )
    return StateFile.model_validate(state_payload)


def _command(
    attachment_id: str,
    payload: bytes,
    stream: InMemoryBinaryStream,
) -> UploadAttachmentContent:
    return UploadAttachmentContent(
        idempotency_key=attachment_id,
        attachment_id=attachment_id,
        expected_content_type="application/octet-stream",
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        byte_stream=stream,
    )


def _start_upload(
    *,
    name: str,
    service: AttachmentUploadService,
    command: UploadAttachmentContent,
    results: dict[str, Any],
    errors: dict[str, BaseException],
    done: dict[str, threading.Event],
    start_barrier: threading.Barrier | None = None,
) -> threading.Thread:
    done[name] = threading.Event()

    def target() -> None:
        try:
            if start_barrier is not None:
                start_barrier.wait(timeout=5.0)
            results[name] = service.execute(command)
        except BaseException as error:
            errors[name] = error
        finally:
            done[name].set()

    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


def _join_threads(*threads: threading.Thread) -> None:
    for thread in threads:
        thread.join(timeout=5.0)
        assert not thread.is_alive(), f"upload thread did not stop: {thread.name}"


def _service_rig(
    *,
    state: StateFile,
    recorder: _PhaseRecorder,
    blocked_attachment_id: str | None = None,
    capacity_barrier: bool = False,
):
    registry = AttachmentUploadRegistry()
    upload_guard = _ObservedAttachmentUploadGuard(registry, recorder)
    coordination_lock = StorageCoordinationLock()
    publication_guard = _ObservedPublicationCommitGuard(
        coordination_lock,
        recorder,
    )
    repository = _ObservedRepository(
        state,
        registry=registry,
        publication_guard=publication_guard,
        recorder=recorder,
        blocked_attachment_id=blocked_attachment_id,
    )
    resource_type = (
        _CapacityBarrierResourceStore
        if capacity_barrier
        else _ObservedResourceStore
    )
    resources = resource_type(
        registry=registry,
        publication_guard=publication_guard,
        recorder=recorder,
    )
    notifier = InMemoryStateChangeNotifier()
    clock = FakeClock(NOW)
    service = AttachmentUploadService(
        repository=repository,
        resource_store=resources,
        publication_guard=publication_guard,
        upload_guard=upload_guard,
        clock=clock,
        notifier=notifier,
    )
    return (
        service,
        repository,
        resources,
        registry,
        publication_guard,
        notifier,
        clock,
    )


@pytest.mark.parametrize(
    ("second_payload", "expected_error"),
    [
        pytest.param(FIRST_PAYLOAD, None, id="post-commit-replay"),
        pytest.param(
            b"different immutable bytes",
            ErrorCode.IDEMPOTENCY_CONFLICT,
            id="post-commit-conflict",
        ),
    ],
)
def test_same_attachment_guard_spans_stream_publish_and_commit_before_second_decision(
    second_payload: bytes,
    expected_error: ErrorCode | None,
) -> None:
    recorder = _PhaseRecorder()
    (
        service,
        repository,
        resources,
        registry,
        _,
        _,
        _,
    ) = _service_rig(
        state=_upload_state(FIRST_ATTACHMENT_ID),
        recorder=recorder,
        blocked_attachment_id=FIRST_ATTACHMENT_ID,
    )
    first_body = _GuardObservedStream(
        FIRST_PAYLOAD,
        attachment_id=FIRST_ATTACHMENT_ID,
        registry=registry,
    )
    second_body = _GuardObservedStream(
        second_payload,
        attachment_id=FIRST_ATTACHMENT_ID,
        registry=registry,
    )
    results: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}
    done: dict[str, threading.Event] = {}

    first = _start_upload(
        name="same-first",
        service=service,
        command=_command(FIRST_ATTACHMENT_ID, FIRST_PAYLOAD, first_body),
        results=results,
        errors=errors,
        done=done,
    )
    assert repository.commit_entered.wait(5.0)
    second = _start_upload(
        name="same-second",
        service=service,
        command=_command(FIRST_ATTACHMENT_ID, second_payload, second_body),
        results=results,
        errors=errors,
        done=done,
    )

    try:
        assert recorder.wait_for("upload-attempt", "same-second")
        assert not recorder.contains("upload-acquired", "same-second")
        assert not done["same-second"].is_set()
        assert second_body.read_requests == []
        assert registry.active_attachment_ids() == (FIRST_ATTACHMENT_ID,)
        assert repository.read_snapshot().cases[CASE_ID].attachments[
            FIRST_ATTACHMENT_ID
        ].status is AttachmentStatus.UPLOADING
    finally:
        repository.allow_commit.set()

    _join_threads(first, second)

    assert "same-first" not in errors
    assert results["same-first"].status == AttachmentStatus.READY.value
    if expected_error is None:
        assert "same-second" not in errors
        assert results["same-second"] == results["same-first"]
    else:
        assert "same-second" not in results
        assert isinstance(errors["same-second"], ApplicationPortError)
        assert errors["same-second"].error.code is expected_error

    assert recorder.index("commit-applied", "same-first") < recorder.index(
        "upload-acquired", "same-second"
    )
    assert recorder.index("commit-applied", "same-first") < recorder.index(
        "snapshot-read", "same-second"
    )
    assert resources.stage_attachment_calls == [FIRST_ATTACHMENT_ID]
    assert len(resources.capacity_calls) == len(resources.publish_calls) == 1
    assert len(repository.commit_calls) == 1
    assert resources.capacity_guard_states == [
        (FIRST_ATTACHMENT_ID, True, True)
    ]
    assert resources.publish_guard_states == [
        (FIRST_ATTACHMENT_ID, True, True)
    ]
    assert repository.commit_guard_states == [
        (FIRST_ATTACHMENT_ID, True, True)
    ]
    assert first_body.returned_sizes == [len(FIRST_PAYLOAD), 0]
    assert first_body.read_guard_states == [True, True]
    assert first_body.close_guard_states == [True]
    assert second_body.read_requests == []
    assert second_body.close_guard_states == [True]
    assert registry.active_attachment_ids() == ()
    assert repository.read_snapshot().cases[CASE_ID].attachments[
        FIRST_ATTACHMENT_ID
    ].status is AttachmentStatus.READY


def test_distinct_attachment_capacity_is_atomic_under_publication_lease_near_5_gib(
) -> None:
    recorder = _PhaseRecorder()
    (
        service,
        repository,
        resources,
        registry,
        _,
        notifier,
        clock,
    ) = _service_rig(
        state=_upload_state(FIRST_ATTACHMENT_ID, SECOND_ATTACHMENT_ID),
        recorder=recorder,
        capacity_barrier=True,
    )
    assert isinstance(resources, _CapacityBarrierResourceStore)

    existing = resources.plan_target(
        CASE_ID,
        ResourceType.ARTIFACT,
        EXISTING_RESOURCE_ID,
        ResourceKind.FILE,
        MAX_CASE_RESOURCE_BYTES - len(FIRST_PAYLOAD),
        "e" * 64,
    )
    resources.seed_formal_resource(existing, state_reference_count=1)
    resources.plan_target_calls.clear()

    first_body = _GuardObservedStream(
        FIRST_PAYLOAD,
        attachment_id=FIRST_ATTACHMENT_ID,
        registry=registry,
    )
    second_body = _GuardObservedStream(
        SECOND_PAYLOAD,
        attachment_id=SECOND_ATTACHMENT_ID,
        registry=registry,
    )
    results: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}
    done: dict[str, threading.Event] = {}
    start = threading.Barrier(3)

    accepted = _start_upload(
        name="capacity-accepted",
        service=service,
        command=_command(FIRST_ATTACHMENT_ID, FIRST_PAYLOAD, first_body),
        results=results,
        errors=errors,
        done=done,
        start_barrier=start,
    )
    rejected = _start_upload(
        name="capacity-rejected",
        service=service,
        command=_command(SECOND_ATTACHMENT_ID, SECOND_PAYLOAD, second_body),
        results=results,
        errors=errors,
        done=done,
        start_barrier=start,
    )
    start.wait(timeout=5.0)

    try:
        assert resources.accepted_capacity_checked.wait(5.0)
        assert resources.rejected_stage_released.wait(5.0)
        assert recorder.wait_for("publication-attempt", "capacity-rejected")
        assert not recorder.contains("publication-acquired", "capacity-rejected")
        assert not done["capacity-accepted"].is_set()
        assert not done["capacity-rejected"].is_set()
        assert resources.publish_calls == []
        assert first_body.returned_sizes == [len(FIRST_PAYLOAD), 0]
        assert second_body.returned_sizes == [len(SECOND_PAYLOAD), 0]
        assert registry.active_attachment_ids() == (
            FIRST_ATTACHMENT_ID,
            SECOND_ATTACHMENT_ID,
        )
    finally:
        resources.allow_accepted_publish.set()

    _join_threads(accepted, rejected)

    assert "capacity-accepted" not in errors
    assert results["capacity-accepted"].status == AttachmentStatus.READY.value
    assert "capacity-rejected" not in results
    assert isinstance(errors["capacity-rejected"], ApplicationPortError)
    capacity_error = errors["capacity-rejected"].error
    assert capacity_error.code is ErrorCode.RESOURCE_LIMIT_EXCEEDED
    assert capacity_error.details[0].limit == MAX_CASE_RESOURCE_BYTES
    assert capacity_error.details[0].observed == (
        MAX_CASE_RESOURCE_BYTES + len(SECOND_PAYLOAD)
    )

    accepted_usage = resources.capacity_usages[FIRST_ATTACHMENT_ID]
    assert accepted_usage.current_bytes == (
        MAX_CASE_RESOURCE_BYTES - len(FIRST_PAYLOAD)
    )
    assert accepted_usage.new_bytes == len(FIRST_PAYLOAD)
    assert accepted_usage.total_bytes == MAX_CASE_RESOURCE_BYTES
    assert [
        targets[0].resource_id for _, targets in resources.capacity_calls
    ] == [FIRST_ATTACHMENT_ID, SECOND_ATTACHMENT_ID]
    assert resources.capacity_guard_states == [
        (FIRST_ATTACHMENT_ID, True, True),
        (SECOND_ATTACHMENT_ID, True, True),
    ]
    assert resources.publish_guard_states == [
        (FIRST_ATTACHMENT_ID, True, True)
    ]
    assert repository.commit_guard_states == [
        (FIRST_ATTACHMENT_ID, True, True)
    ]

    first_key = (
        f"resources/cases/{CASE_ID}/attachments/"
        f"{FIRST_ATTACHMENT_ID}/payload"
    )
    second_key = (
        f"resources/cases/{CASE_ID}/attachments/"
        f"{SECOND_ATTACHMENT_ID}/payload"
    )
    assert first_key in resources.published_storage_keys
    assert second_key not in resources.published_storage_keys
    assert existing.final_storage_key in resources.published_storage_keys
    assert len(resources.publish_calls) == 1
    assert resources.publish_calls[0][0].attachment_id == FIRST_ATTACHMENT_ID
    assert len(resources.discard_calls) == 1
    assert resources.discard_calls[0].attachment_id == SECOND_ATTACHMENT_ID
    assert resources.staged_resource_count == 0

    persisted = repository.read_snapshot().cases[CASE_ID]
    assert persisted.attachments[
        FIRST_ATTACHMENT_ID
    ].status is AttachmentStatus.READY
    assert persisted.attachments[
        SECOND_ATTACHMENT_ID
    ].status is AttachmentStatus.UPLOADING
    assert len(repository.commit_calls) == 1
    assert notifier.notify_calls == [(CASE_ID, 2)]
    assert clock.calls == 2
    assert first_body.read_guard_states == [True, True]
    assert second_body.read_guard_states == [True, True]
    assert first_body.close_guard_states == [True]
    assert second_body.close_guard_states == [True]
    assert registry.active_attachment_ids() == ()
    assert recorder.index(
        "publication-acquired", "capacity-accepted"
    ) < recorder.index("publication-attempt", "capacity-rejected")
    assert recorder.index(
        "publication-released", "capacity-accepted"
    ) < recorder.index("publication-acquired", "capacity-rejected")
