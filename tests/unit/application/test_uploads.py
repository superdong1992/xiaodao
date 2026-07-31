from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from problem_locator.application.uploads import AttachmentUploadService
from problem_locator.application.mutations import build_state_mutation
from problem_locator.contracts import (
    ApplicationError,
    ApplicationPortError,
    Attachment,
    AttachmentStatus,
    CaseAggregate,
    ERROR_SPECS,
    ErrorCode,
    MAX_CASE_RESOURCE_BYTES,
    ResourceKind,
    ResourceType,
    StateFile,
    UploadAttachmentContent,
)
from tests.contracts.fakes import (
    FakeClock,
    InMemoryAttachmentUploadGuard,
    InMemoryBinaryStream,
    InMemoryPublicationCommitGuard,
    InMemoryResourceStore,
    InMemoryStateChangeNotifier,
    InMemoryStateRepository,
)


ROOT = Path(__file__).resolve().parents[3]
CASE_ID = "00000000-0000-0000-0000-000000000001"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000090"
OTHER_RESOURCE_ID = "00000000-0000-0000-0000-000000000091"
OTHER_ATTACHMENT_ID = "00000000-0000-0000-0000-000000000092"
NOW = "2026-07-31T06:07:08.000Z"
PAYLOAD = b"one forward-only attachment payload"
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
OTHER_PAYLOAD = b"another independently streamed attachment"


def _application_error(code: ErrorCode, message: str = "injected") -> ApplicationPortError:
    return ApplicationPortError(
        ApplicationError(
            code=code,
            message=message,
            details=[],
            retryable=ERROR_SPECS[code].application_retryable,
        )
    )


def _state(
    *,
    content_type: str = "application/octet-stream",
    declared_size: int | None = None,
    declared_sha256: str | None = None,
) -> StateFile:
    base = StateFile.model_validate_json(
        (ROOT / "tests/fixtures/contracts/positive/state.json").read_text(
            encoding="utf-8"
        )
    )
    aggregate = base.cases[CASE_ID]
    attachment = Attachment(
        attachment_id=ATTACHMENT_ID,
        case_id=CASE_ID,
        status=AttachmentStatus.UPLOADING,
        name="server.log",
        content_type=content_type,
        declared_size=declared_size,
        declared_sha256=declared_sha256,
        size=None,
        sha256=None,
        storage_key=None,
        created_at=NOW,
        updated_at=NOW,
    )
    aggregate_payload = aggregate.model_dump(mode="python")
    aggregate_payload["attachments"][ATTACHMENT_ID] = attachment
    updated_aggregate = CaseAggregate.model_validate(aggregate_payload)
    state_payload = base.model_dump(mode="python")
    state_payload["cases"][CASE_ID] = updated_aggregate
    return StateFile.model_validate(state_payload)


def _state_with_other_attachment() -> StateFile:
    state = _state()
    aggregate = state.cases[CASE_ID]
    other = Attachment(
        attachment_id=OTHER_ATTACHMENT_ID,
        case_id=CASE_ID,
        status=AttachmentStatus.UPLOADING,
        name="other.log",
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
    aggregate_payload["attachments"][OTHER_ATTACHMENT_ID] = other
    state_payload = state.model_dump(mode="python")
    state_payload["cases"][CASE_ID] = CaseAggregate.model_validate(aggregate_payload)
    return StateFile.model_validate(state_payload)


def _command(
    stream: InMemoryBinaryStream,
    *,
    payload: bytes = PAYLOAD,
    content_type: str = "application/octet-stream",
    attachment_id: str = ATTACHMENT_ID,
) -> UploadAttachmentContent:
    return UploadAttachmentContent(
        idempotency_key=attachment_id,
        attachment_id=attachment_id,
        expected_content_type=content_type,
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        byte_stream=stream,
    )


class _ObservedStream(InMemoryBinaryStream):
    def __init__(
        self,
        data: bytes,
        publication_guard: InMemoryPublicationCommitGuard,
    ) -> None:
        super().__init__(data)
        self._publication_guard = publication_guard
        self.publication_lease_during_reads: list[bool] = []

    def read(self, max_bytes: int) -> bytes:
        self.publication_lease_during_reads.append(
            self._publication_guard.held_by_current_thread()
        )
        return super().read(max_bytes)


class _InterleavingStream(_ObservedStream):
    def __init__(
        self,
        data: bytes,
        publication_guard: InMemoryPublicationCommitGuard,
        on_first_read: Callable[[], None],
    ) -> None:
        super().__init__(data, publication_guard)
        self._on_first_read = on_first_read
        self._interleaved = False
        self.publication_lease_before_interleave: bool | None = None

    def read(self, max_bytes: int) -> bytes:
        if not self._interleaved:
            self._interleaved = True
            self.publication_lease_before_interleave = (
                self._publication_guard.held_by_current_thread()
            )
            self._on_first_read()
        return super().read(max_bytes)


class _CloseFailingStream(InMemoryBinaryStream):
    def close(self) -> None:
        super().close()
        raise RuntimeError("injected stream close failure")


class _ObservedNotifier(InMemoryStateChangeNotifier):
    def __init__(self, publication_guard: InMemoryPublicationCommitGuard) -> None:
        super().__init__()
        self._publication_guard = publication_guard
        self.publication_lease_during_notifications: list[bool] = []

    def notify(self, case_id: str, generation: int) -> None:
        self.publication_lease_during_notifications.append(
            self._publication_guard.held_by_current_thread()
        )
        super().notify(case_id, generation)


class _CountingRepository(InMemoryStateRepository):
    def __init__(self, state: StateFile) -> None:
        super().__init__(state)
        self.snapshot_reads = 0
        self.commit_attempts: list[tuple[int, int | None]] = []

    def read_snapshot(self) -> StateFile:
        self.snapshot_reads += 1
        return super().read_snapshot()

    def commit(self, expected_generation: int, expected_case_revision: int | None, mutation: Any):
        self.commit_attempts.append((expected_generation, expected_case_revision))
        return super().commit(expected_generation, expected_case_revision, mutation)


class _ReadFailsAfterCommitRepository(_CountingRepository):
    def __init__(self, state: StateFile) -> None:
        super().__init__(state)
        self.fail_next_read = False

    def read_snapshot(self) -> StateFile:
        if self.fail_next_read:
            self.fail_next_read = False
            raise _application_error(ErrorCode.STATE_CORRUPT)
        return super().read_snapshot()

    def commit(self, expected_generation: int, expected_case_revision: int | None, mutation: Any):
        receipt = super().commit(expected_generation, expected_case_revision, mutation)
        self.fail_next_read = True
        return receipt


class _ReadyOnPostStageRepository(_CountingRepository):
    """Expose a concurrent READY finalize on the post-stage snapshot."""

    def read_snapshot(self) -> StateFile:
        snapshot = super().read_snapshot()
        if self.snapshot_reads != 2:
            return snapshot
        aggregate = snapshot.cases[CASE_ID]
        attachment = aggregate.attachments[ATTACHMENT_ID]
        ready_attachment = attachment.model_copy(
            update={
                "status": AttachmentStatus.READY,
                "size": len(PAYLOAD),
                "sha256": PAYLOAD_SHA256,
                "storage_key": (
                    f"resources/cases/{CASE_ID}/attachments/"
                    f"{ATTACHMENT_ID}/payload"
                ),
                "updated_at": NOW,
            }
        )
        ready_case = aggregate.case.model_copy(
            update={
                "case_revision": aggregate.case.case_revision + 1,
                "updated_at": NOW,
            }
        )
        ready_aggregate = aggregate.model_copy(
            update={
                "case": ready_case,
                "attachments": {
                    **aggregate.attachments,
                    ATTACHMENT_ID: ready_attachment,
                },
            }
        )
        ready_state = snapshot.model_copy(
            update={
                "generation": snapshot.generation + 1,
                "cases": {**snapshot.cases, CASE_ID: ready_aggregate},
            }
        )
        self.seed(ready_state)
        return ready_state


class _FailingNotifier(InMemoryStateChangeNotifier):
    def notify(self, case_id: str, generation: int) -> None:
        super().notify(case_id, generation)
        raise RuntimeError("injected notification failure")


class _GenerationAdvancingResourceStore(InMemoryResourceStore):
    """Commit one unrelated generation after the stream reaches EOF."""

    def __init__(
        self,
        repository: _CountingRepository,
        upload_guard: InMemoryAttachmentUploadGuard,
        publication_guard: InMemoryPublicationCommitGuard,
    ) -> None:
        super().__init__(
            upload_guard=upload_guard,
            publication_guard=publication_guard,
        )
        self._repository = repository
        self._publication_guard = publication_guard

    def stage_attachment(self, *args: Any, **kwargs: Any):
        staged_ref = super().stage_attachment(*args, **kwargs)
        lease = self._publication_guard.acquire()
        try:
            snapshot = self._repository.read_snapshot()
            self._repository.commit(
                snapshot.generation,
                None,
                build_state_mutation(),
            )
        finally:
            lease.release()
        return staged_ref


def _rig(
    state: StateFile | None = None,
    *,
    notifier: InMemoryStateChangeNotifier | None = None,
    repository: _CountingRepository | None = None,
) -> tuple[
    AttachmentUploadService,
    _CountingRepository,
    InMemoryResourceStore,
    InMemoryAttachmentUploadGuard,
    InMemoryPublicationCommitGuard,
    InMemoryStateChangeNotifier,
    FakeClock,
]:
    upload_guard = InMemoryAttachmentUploadGuard()
    publication_guard = InMemoryPublicationCommitGuard()
    repository = repository or _CountingRepository(state or _state())
    resources = InMemoryResourceStore(
        upload_guard=upload_guard,
        publication_guard=publication_guard,
    )
    actual_notifier = notifier or _ObservedNotifier(publication_guard)
    clock = FakeClock(NOW)
    service = AttachmentUploadService(
        repository=repository,
        resource_store=resources,
        publication_guard=publication_guard,
        upload_guard=upload_guard,
        clock=clock,
        notifier=actual_notifier,
    )
    return (
        service,
        repository,
        resources,
        upload_guard,
        publication_guard,
        actual_notifier,
        clock,
    )


def test_upload_streams_once_then_publishes_and_returns_persisted_receipt() -> None:
    (
        service,
        repository,
        resources,
        upload_guard,
        publication_guard,
        notifier,
        clock,
    ) = _rig()
    body = _ObservedStream(PAYLOAD, publication_guard)

    receipt = service.execute(_command(body))

    persisted = repository.read_snapshot()
    attachment = persisted.cases[CASE_ID].attachments[ATTACHMENT_ID]
    record = persisted.idempotency_records[
        f"UploadAttachmentContent:{ATTACHMENT_ID}"
    ]
    assert receipt == record.business_receipt
    assert receipt.primary_resource_id == ATTACHMENT_ID
    assert receipt.case_revision == 2
    assert receipt.status == "READY"
    assert attachment.status is AttachmentStatus.READY
    assert attachment.size == len(PAYLOAD)
    assert attachment.sha256 == PAYLOAD_SHA256
    assert attachment.storage_key == (
        f"resources/cases/{CASE_ID}/attachments/{ATTACHMENT_ID}/payload"
    )
    assert resources.stage_attachment_calls == [ATTACHMENT_ID]
    assert resources.validate_staged_calls == []
    assert len(resources.plan_target_calls) == 1
    assert len(resources.capacity_calls) == 1
    assert len(resources.publish_calls) == 1
    assert body.bytes_read == len(PAYLOAD)
    assert body.returned_sizes == [len(PAYLOAD), 0]
    assert body.publication_lease_during_reads == [False, False]
    assert body.close_calls == 1
    assert upload_guard.release_calls == [ATTACHMENT_ID]
    assert publication_guard.acquire_calls == publication_guard.release_calls == 1
    assert notifier.notify_calls == [(CASE_ID, 2)]
    assert isinstance(notifier, _ObservedNotifier)
    assert notifier.publication_lease_during_notifications == [False]
    assert clock.calls == 1


def test_post_commit_state_read_failure_cannot_replace_upload_success() -> None:
    repository = _ReadFailsAfterCommitRepository(_state())
    service, _, resources, upload_guard, publication_guard, _, _ = _rig(
        repository=repository
    )
    body = InMemoryBinaryStream(PAYLOAD)

    receipt = service.execute(_command(body))

    assert receipt.status == AttachmentStatus.READY.value
    assert receipt.case_revision == 2
    assert repository.fail_next_read is True
    assert len(resources.publish_calls) == 1
    assert publication_guard.acquire_calls == publication_guard.release_calls == 1
    assert upload_guard.release_calls == [ATTACHMENT_ID]
    repository.fail_next_read = False
    assert repository.read_snapshot().cases[CASE_ID].attachments[
        ATTACHMENT_ID
    ].status is AttachmentStatus.READY


@pytest.mark.parametrize(
    ("state", "payload", "content_type", "expected_code"),
    [
        (
            _state(content_type="text/plain"),
            PAYLOAD,
            "application/octet-stream",
            ErrorCode.VALIDATION_ERROR,
        ),
        (
            _state(declared_size=len(PAYLOAD) + 1),
            PAYLOAD,
            "application/octet-stream",
            ErrorCode.VALIDATION_ERROR,
        ),
        (
            _state(declared_sha256="f" * 64),
            PAYLOAD,
            "application/octet-stream",
            ErrorCode.VALIDATION_ERROR,
        ),
    ],
)
def test_prepared_header_mismatch_is_rejected_before_the_body_is_read(
    state: StateFile,
    payload: bytes,
    content_type: str,
    expected_code: ErrorCode,
) -> None:
    service, _, resources, upload_guard, publication_guard, _, clock = _rig(state)
    body = InMemoryBinaryStream(payload)

    with pytest.raises(ApplicationPortError) as captured:
        service.execute(_command(body, payload=payload, content_type=content_type))

    assert captured.value.error.code is expected_code
    assert body.read_requests == []
    assert body.close_calls == 1
    assert resources.stage_attachment_calls == []
    assert publication_guard.acquire_calls == 0
    assert upload_guard.release_calls == [ATTACHMENT_ID]
    assert clock.calls == 0


def test_ready_idempotency_replay_and_conflict_never_consume_a_second_body() -> None:
    service, repository, resources, _, publication_guard, _, _ = _rig()
    first = InMemoryBinaryStream(PAYLOAD)
    first_receipt = service.execute(_command(first))
    calls_after_first = (
        len(resources.stage_attachment_calls),
        publication_guard.acquire_calls,
        len(repository.commit_attempts),
    )

    replay_body = InMemoryBinaryStream(b"ignored because the header receipt replays")
    replay = service.execute(_command(replay_body))
    assert replay == first_receipt
    assert replay_body.read_requests == []
    assert replay_body.close_calls == 1
    assert (
        len(resources.stage_attachment_calls),
        publication_guard.acquire_calls,
        len(repository.commit_attempts),
    ) == calls_after_first

    conflict_payload = b"a different immutable attachment"
    conflict_body = InMemoryBinaryStream(conflict_payload)
    with pytest.raises(ApplicationPortError) as captured:
        service.execute(_command(conflict_body, payload=conflict_payload))
    assert captured.value.error.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert conflict_body.read_requests == []
    assert conflict_body.close_calls == 1
    assert len(resources.stage_attachment_calls) == calls_after_first[0]


def test_body_hash_mismatch_fails_before_publication_and_releases_the_guard() -> None:
    service, _, resources, upload_guard, publication_guard, _, _ = _rig()
    body = InMemoryBinaryStream(PAYLOAD)
    claimed_payload = b"another payload with a different digest"

    with pytest.raises(ApplicationPortError) as captured:
        service.execute(_command(body, payload=claimed_payload))

    assert captured.value.error.code in {
        ErrorCode.RESOURCE_SIZE_MISMATCH,
        ErrorCode.RESOURCE_HASH_MISMATCH,
    }
    assert body.bytes_read == len(PAYLOAD)
    assert body.close_calls == 1
    assert resources.publish_calls == []
    assert publication_guard.acquire_calls == 0
    assert upload_guard.release_calls == [ATTACHMENT_ID]


def test_stream_failure_is_typed_and_still_closes_and_releases() -> None:
    service, _, resources, upload_guard, publication_guard, _, _ = _rig()
    body = InMemoryBinaryStream(PAYLOAD, fail_on_read_number=1)

    with pytest.raises(ApplicationPortError) as captured:
        service.execute(_command(body))

    assert captured.value.error.code is ErrorCode.UPLOAD_INCOMPLETE
    assert captured.value.error.retryable is True
    assert body.close_calls == 1
    assert resources.publish_calls == []
    assert publication_guard.acquire_calls == 0
    assert upload_guard.release_calls == [ATTACHMENT_ID]

    retry = InMemoryBinaryStream(PAYLOAD)
    receipt = service.execute(_command(retry))

    assert receipt.status == AttachmentStatus.READY.value
    assert retry.returned_sizes == [len(PAYLOAD), 0]
    assert upload_guard.acquire_calls == [ATTACHMENT_ID, ATTACHMENT_ID]
    assert upload_guard.release_calls == [ATTACHMENT_ID, ATTACHMENT_ID]


def test_close_failure_does_not_mask_a_committed_upload_receipt() -> None:
    service, repository, _, upload_guard, publication_guard, _, _ = _rig()
    body = _CloseFailingStream(PAYLOAD)

    receipt = service.execute(_command(body))

    assert receipt.status == "READY"
    assert repository.read_snapshot().cases[CASE_ID].attachments[
        ATTACHMENT_ID
    ].status is AttachmentStatus.READY
    assert body.close_calls == 1
    assert upload_guard.release_calls == [ATTACHMENT_ID]
    assert publication_guard.acquire_calls == publication_guard.release_calls == 1


def test_close_failure_does_not_mask_a_precommit_typed_failure() -> None:
    service, _, resources, upload_guard, publication_guard, _, _ = _rig()
    body = _CloseFailingStream(PAYLOAD, fail_on_read_number=1)

    with pytest.raises(ApplicationPortError) as captured:
        service.execute(_command(body))

    assert captured.value.error.code is ErrorCode.UPLOAD_INCOMPLETE
    assert body.close_calls == 1
    assert resources.publish_calls == []
    assert publication_guard.acquire_calls == 0
    assert upload_guard.release_calls == [ATTACHMENT_ID]


def test_revision_conflicts_recompute_post_stage_three_times_without_rereading() -> None:
    (
        service,
        repository,
        resources,
        upload_guard,
        publication_guard,
        _,
        clock,
    ) = _rig()
    repository.fail_next_commit(_application_error(ErrorCode.REVISION_CONFLICT))
    repository.fail_next_commit(_application_error(ErrorCode.REVISION_CONFLICT))
    body = _ObservedStream(PAYLOAD, publication_guard)

    receipt = service.execute(_command(body))

    assert receipt.status == "READY"
    assert repository.commit_attempts == [(1, 1), (1, 1), (1, 1)]
    assert len(resources.capacity_calls) == 3
    assert [usage[0] for usage in resources.capacity_calls] == [CASE_ID] * 3
    assert len(resources.publish_calls) == 3
    assert resources.capacity_calls[0][1] == resources.capacity_calls[1][1]
    assert resources.capacity_calls[1][1] == resources.capacity_calls[2][1]
    assert body.returned_sizes == [len(PAYLOAD), 0]
    assert body.publication_lease_during_reads == [False, False]
    assert body.close_calls == 1
    assert publication_guard.acquire_calls == publication_guard.release_calls == 3
    assert upload_guard.release_calls == [ATTACHMENT_ID]
    assert clock.calls == 1


def test_generation_advance_during_stream_uses_only_fresh_post_stage_snapshot() -> None:
    upload_guard = InMemoryAttachmentUploadGuard()
    publication_guard = InMemoryPublicationCommitGuard()
    repository = _CountingRepository(_state())
    resources = _GenerationAdvancingResourceStore(
        repository,
        upload_guard,
        publication_guard,
    )
    notifier = _ObservedNotifier(publication_guard)
    service = AttachmentUploadService(
        repository=repository,
        resource_store=resources,
        publication_guard=publication_guard,
        upload_guard=upload_guard,
        clock=FakeClock(NOW),
        notifier=notifier,
    )
    body = _ObservedStream(PAYLOAD, publication_guard)

    receipt = service.execute(_command(body))

    assert receipt.status == "READY"
    # The first commit is an unrelated write after EOF; the upload must not
    # reuse generation 1 from its early validation snapshot.
    assert repository.commit_attempts == [(1, None), (2, 1)]
    assert repository.read_snapshot().generation == 3
    assert body.returned_sizes == [len(PAYLOAD), 0]
    assert body.publication_lease_during_reads == [False, False]
    assert resources.stage_attachment_calls == [ATTACHMENT_ID]
    assert publication_guard.acquire_calls == publication_guard.release_calls == 2


def test_different_attachment_streams_can_interleave_without_a_global_lease() -> None:
    service, repository, _, upload_guard, publication_guard, _, clock = _rig(
        _state_with_other_attachment()
    )
    other_receipts = []
    other_body = _ObservedStream(OTHER_PAYLOAD, publication_guard)

    def upload_other() -> None:
        other_receipts.append(
            service.execute(
                _command(
                    other_body,
                    payload=OTHER_PAYLOAD,
                    attachment_id=OTHER_ATTACHMENT_ID,
                )
            )
        )

    first_body = _InterleavingStream(PAYLOAD, publication_guard, upload_other)

    first_receipt = service.execute(_command(first_body))

    assert first_receipt.status == AttachmentStatus.READY.value
    assert [receipt.status for receipt in other_receipts] == [
        AttachmentStatus.READY.value
    ]
    persisted = repository.read_snapshot().cases[CASE_ID]
    assert persisted.attachments[ATTACHMENT_ID].status is AttachmentStatus.READY
    assert persisted.attachments[OTHER_ATTACHMENT_ID].status is AttachmentStatus.READY
    assert repository.commit_attempts == [(1, 1), (2, 2)]
    assert upload_guard.acquire_calls == [ATTACHMENT_ID, OTHER_ATTACHMENT_ID]
    assert upload_guard.release_calls == [OTHER_ATTACHMENT_ID, ATTACHMENT_ID]
    assert first_body.publication_lease_before_interleave is False
    assert first_body.publication_lease_during_reads == [False, False]
    assert other_body.publication_lease_during_reads == [False, False]
    assert first_body.returned_sizes == [len(PAYLOAD), 0]
    assert other_body.returned_sizes == [len(OTHER_PAYLOAD), 0]
    assert publication_guard.acquire_calls == publication_guard.release_calls == 2
    assert clock.calls == 2


def test_post_stage_replay_is_not_masked_when_stage_cleanup_fails() -> None:
    upload_guard = InMemoryAttachmentUploadGuard()
    publication_guard = InMemoryPublicationCommitGuard()
    repository = _ReadyOnPostStageRepository(_state())
    resources = InMemoryResourceStore(
        upload_guard=upload_guard,
        publication_guard=publication_guard,
    )
    resources.inject_failure("discard", RuntimeError("cleanup unavailable"))
    service = AttachmentUploadService(
        repository=repository,
        resource_store=resources,
        publication_guard=publication_guard,
        upload_guard=upload_guard,
        clock=FakeClock(NOW),
        notifier=_ObservedNotifier(publication_guard),
    )
    body = InMemoryBinaryStream(PAYLOAD)

    receipt = service.execute(_command(body))

    assert receipt.status == AttachmentStatus.READY.value
    assert receipt.case_revision == 2
    assert resources.publish_calls == []
    assert resources.staged_resource_count == 1
    assert body.returned_sizes == [len(PAYLOAD), 0]
    assert upload_guard.release_calls == [ATTACHMENT_ID]
    assert publication_guard.acquire_calls == publication_guard.release_calls == 1


def test_third_revision_conflict_is_returned_and_completed_stage_is_discarded() -> None:
    service, repository, resources, upload_guard, publication_guard, _, _ = _rig()
    for _ in range(3):
        repository.fail_next_commit(_application_error(ErrorCode.REVISION_CONFLICT))
    body = InMemoryBinaryStream(PAYLOAD)

    with pytest.raises(ApplicationPortError) as captured:
        service.execute(_command(body))

    assert captured.value.error.code is ErrorCode.REVISION_CONFLICT
    assert len(repository.commit_attempts) == 3
    assert len(resources.publish_calls) == 3
    assert resources.discard_calls
    assert body.returned_sizes == [len(PAYLOAD), 0]
    assert body.close_calls == 1
    assert publication_guard.acquire_calls == publication_guard.release_calls == 3
    assert upload_guard.release_calls == [ATTACHMENT_ID]
    assert repository.read_snapshot().cases[CASE_ID].attachments[
        ATTACHMENT_ID
    ].status is AttachmentStatus.UPLOADING


def test_capacity_failure_moves_no_stage_and_discards_after_lease_release() -> None:
    service, repository, resources, upload_guard, publication_guard, _, _ = _rig()
    existing = resources.plan_target(
        CASE_ID,
        ResourceType.ARTIFACT,
        OTHER_RESOURCE_ID,
        ResourceKind.FILE,
        MAX_CASE_RESOURCE_BYTES,
        "e" * 64,
    )
    resources.seed_formal_resource(existing)
    resources.plan_target_calls.clear()
    body = InMemoryBinaryStream(PAYLOAD)

    with pytest.raises(ApplicationPortError) as captured:
        service.execute(_command(body))

    assert captured.value.error.code is ErrorCode.RESOURCE_LIMIT_EXCEEDED
    assert len(resources.capacity_calls) == 1
    assert resources.publish_calls == []
    assert len(resources.discard_calls) == 1
    assert body.returned_sizes == [len(PAYLOAD), 0]
    assert body.close_calls == 1
    assert publication_guard.acquire_calls == publication_guard.release_calls == 1
    assert upload_guard.release_calls == [ATTACHMENT_ID]
    assert repository.read_snapshot().cases[CASE_ID].attachments[
        ATTACHMENT_ID
    ].status is AttachmentStatus.UPLOADING


def test_cleanup_failure_does_not_mask_the_primary_capacity_failure() -> None:
    service, _, resources, upload_guard, publication_guard, _, _ = _rig()
    existing = resources.plan_target(
        CASE_ID,
        ResourceType.ARTIFACT,
        OTHER_RESOURCE_ID,
        ResourceKind.FILE,
        MAX_CASE_RESOURCE_BYTES,
        "e" * 64,
    )
    resources.seed_formal_resource(existing)
    resources.plan_target_calls.clear()
    resources.inject_failure("discard", RuntimeError("cleanup unavailable"))
    body = InMemoryBinaryStream(PAYLOAD)

    with pytest.raises(ApplicationPortError) as captured:
        service.execute(_command(body))

    assert captured.value.error.code is ErrorCode.RESOURCE_LIMIT_EXCEEDED
    assert resources.publish_calls == []
    assert resources.staged_resource_count == 1
    assert body.close_calls == 1
    assert upload_guard.release_calls == [ATTACHMENT_ID]
    assert publication_guard.acquire_calls == publication_guard.release_calls == 1


def test_publish_then_commit_failure_is_adopted_by_same_hash_and_rejects_other_hash() -> None:
    service, repository, resources, _, publication_guard, _, _ = _rig(
        _state(declared_size=None, declared_sha256=None)
    )
    repository.fail_next_commit(_application_error(ErrorCode.STATE_WRITE_FAILED))
    first = InMemoryBinaryStream(PAYLOAD)

    with pytest.raises(ApplicationPortError) as failed_commit:
        service.execute(_command(first))
    assert failed_commit.value.error.code is ErrorCode.STATE_WRITE_FAILED
    assert repository.read_snapshot().cases[CASE_ID].attachments[
        ATTACHMENT_ID
    ].status is AttachmentStatus.UPLOADING
    formal_keys = resources.published_storage_keys
    assert len(formal_keys) == 1
    assert len(resources.publish_calls) == 1

    other_payload = b"different bytes but still one immutable target"
    other = InMemoryBinaryStream(other_payload)
    with pytest.raises(ApplicationPortError) as target_conflict:
        service.execute(_command(other, payload=other_payload))
    assert target_conflict.value.error.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert resources.published_storage_keys == formal_keys
    assert len(resources.publish_calls) == 1
    assert other.returned_sizes == [len(other_payload), 0]
    assert other.close_calls == 1

    retry = InMemoryBinaryStream(PAYLOAD)
    receipt = service.execute(_command(retry))
    assert receipt.status == "READY"
    assert resources.capacity_calls[-1][1][0].sha256 == PAYLOAD_SHA256
    assert len(resources.publish_calls) == 2
    assert resources.published_storage_keys == formal_keys
    assert retry.returned_sizes == [len(PAYLOAD), 0]
    assert publication_guard.acquire_calls == publication_guard.release_calls == 3


def test_notification_failure_does_not_rollback_a_committed_upload() -> None:
    notifier = _FailingNotifier()
    service, repository, _, _, publication_guard, _, _ = _rig(notifier=notifier)
    body = InMemoryBinaryStream(PAYLOAD)

    receipt = service.execute(_command(body))

    assert receipt.status == "READY"
    assert notifier.notify_calls == [(CASE_ID, 2)]
    assert publication_guard.acquire_calls == publication_guard.release_calls == 1
    assert repository.read_snapshot().cases[CASE_ID].attachments[
        ATTACHMENT_ID
    ].status is AttachmentStatus.READY


def test_publication_error_discards_stage_and_releases_both_leases() -> None:
    service, _, resources, upload_guard, publication_guard, _, _ = _rig()
    resources.inject_failure(
        "publish",
        _application_error(ErrorCode.RESOURCE_PUBLISH_FAILED),
    )
    body = InMemoryBinaryStream(PAYLOAD)

    with pytest.raises(ApplicationPortError) as captured:
        service.execute(_command(body))

    assert captured.value.error.code is ErrorCode.RESOURCE_PUBLISH_FAILED
    assert len(resources.discard_calls) == 1
    assert body.close_calls == 1
    assert upload_guard.release_calls == [ATTACHMENT_ID]
    assert publication_guard.acquire_calls == publication_guard.release_calls == 1
