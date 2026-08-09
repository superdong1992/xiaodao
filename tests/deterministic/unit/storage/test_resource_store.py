from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

from problem_locator.contracts import (
    ApplicationErrorDetail,
    ApplicationPortError,
    AttachmentFilenameSuffix,
    AttachmentStagedRef,
    ErrorCode,
    PlannedResourceTarget,
    ResourceKind,
    ResourceStore,
    ResourceType,
    StagedResourceRef,
    canonical_json_bytes,
    workspace_attachment_relative_path,
)
from problem_locator.contracts.limits import (
    MAX_ATTACHMENT_BYTES,
    MAX_CASE_RESOURCE_BYTES,
)
from problem_locator.storage.atomic import is_read_only
from problem_locator.storage.coordination import (
    AttachmentUploadRegistry,
    InProcessPublicationCommitGuard,
    StorageCoordinationLock,
)
from problem_locator.storage.layout import StorageLayout
from problem_locator.storage.paths import proposal_stage_path
from problem_locator.storage.platform import PlatformFileSync
from problem_locator.storage.resource_store import (
    FileResourceStore,
    StagePathRegistry,
)
from tests.deterministic.contracts.fakes import DeterministicIdGenerator, InMemoryBinaryStream
from tests.deterministic.unit.storage.fakes import FakeFileSync, FaultInjectingReplace


CASE_ID = "00000000-0000-0000-0000-000000000101"
OTHER_CASE_ID = "00000000-0000-0000-0000-000000000102"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000201"
RESOURCE_ID = "00000000-0000-0000-0000-000000000202"
OTHER_RESOURCE_ID = "00000000-0000-0000-0000-000000000203"
FIRST_BATCH_RESOURCE_ID = "00000000-0000-0000-0000-000000000204"
SECOND_BATCH_RESOURCE_ID = "00000000-0000-0000-0000-000000000205"
JOB_ID = "00000000-0000-0000-0000-000000000301"
GENERATED_STAGE_ID = "00000000-0000-0000-0000-000000000302"


class DurableRecordingFileSync(FakeFileSync):
    """Retain fault observations while applying real durability/permissions."""

    def __init__(self) -> None:
        super().__init__()
        self._platform = PlatformFileSync()

    def sync_file(self, path_or_handle: object) -> None:
        super().sync_file(path_or_handle)  # type: ignore[arg-type]
        self._platform.sync_file(path_or_handle)  # type: ignore[arg-type]

    def sync_directory(self, path: os.PathLike[str] | str) -> None:
        super().sync_directory(path)
        self._platform.sync_directory(path)

    def make_read_only(self, path: os.PathLike[str] | str) -> None:
        super().make_read_only(path)
        self._platform.make_read_only(path)


class RecordingStream(InMemoryBinaryStream):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.requests: list[int] = []

    def read(self, max_bytes: int) -> bytes:
        self.requests.append(max_bytes)
        return super().read(max_bytes)


class BlockingStream:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._sent = False
        self.entered = threading.Event()
        self.proceed = threading.Event()

    def read(self, max_bytes: int) -> bytes:
        if self._sent:
            return b""
        self.entered.set()
        assert self.proceed.wait(2)
        self._sent = True
        return self._payload[:max_bytes]

    def close(self) -> None:
        pass


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.layout = StorageLayout.at(tmp_path / "data")
        self.layout.ensure_directories()
        self.lock = StorageCoordinationLock()
        self.guard = InProcessPublicationCommitGuard(self.lock)
        self.attachments = AttachmentUploadRegistry()
        self.ids = DeterministicIdGenerator(seed="s02-file-resource-store")
        self.sync = DurableRecordingFileSync()
        self.replacer = FaultInjectingReplace()
        self.stages = StagePathRegistry()
        self.store = FileResourceStore(
            self.layout,
            self.lock,
            self.attachments,
            self.ids,
            file_sync=self.sync,
            replacer=self.replacer,
            stage_registry=self.stages,
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _error_code(callback) -> ErrorCode:
    with pytest.raises(ApplicationPortError) as raised:
        callback()
    return raised.value.error.code


def _stage_file(
    harness: Harness,
    payload: bytes = b"resource-bytes",
    proposal_key: str = "artifact",
) -> StagedResourceRef:
    return harness.store.stage_file(
        JOB_ID,
        proposal_key,
        InMemoryBinaryStream(payload),
        expected_size=len(payload),
        expected_sha256=_sha(payload),
    )


def _plan(
    harness: Harness,
    staged: StagedResourceRef,
    *,
    resource_id: str = RESOURCE_ID,
    resource_type: ResourceType = ResourceType.ARTIFACT,
) -> PlannedResourceTarget:
    return harness.store.plan_target(
        CASE_ID,
        resource_type,
        resource_id,
        staged.resource_kind,
        staged.size,
        staged.sha256,
    )


def _write_formal_file(
    harness: Harness,
    target: PlannedResourceTarget,
    payload: bytes,
) -> Path:
    assert target.resource_kind is ResourceKind.FILE
    assert target.size == len(payload)
    assert target.sha256 == _sha(payload)
    path = harness.layout.data_root / target.final_storage_key
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    return path


def test_store_structurally_implements_the_frozen_port(harness: Harness) -> None:
    assert isinstance(harness.store, ResourceStore)


def test_stage_file_reads_one_stream_and_publishes_exact_marker_last(
    harness: Harness,
) -> None:
    payload = b"one-forward-stream"
    stream = RecordingStream(payload)

    staged = harness.store.stage_file(
        JOB_ID,
        "proposal/unsafe-as-path",
        stream,
        expected_size=len(payload),
        expected_sha256=_sha(payload),
    )

    directory = proposal_stage_path(
        harness.layout.data_root,
        JOB_ID,
        "proposal/unsafe-as-path",
    )
    assert (directory / "payload").read_bytes() == payload
    assert (directory / "staged.json").read_bytes() == canonical_json_bytes(staged)
    assert [event.destination.name for event in harness.replacer.events] == [
        "payload",
        "staged.json",
    ]
    assert stream.requests == [1024 * 1024, 1024 * 1024]
    assert harness.store.validate_staged(staged) is None
    assert harness.store.validate_staged(staged) is None


def test_generated_file_stage_is_deterministic_and_idempotent(
    harness: Harness,
) -> None:
    payload = b"server-owned-audit-bundle"
    first = harness.store.stage_generated_file(
        JOB_ID,
        "server-audit-bundle",
        GENERATED_STAGE_ID,
        InMemoryBinaryStream(payload),
        len(payload),
        _sha(payload),
    )
    retry_stream = RecordingStream(payload)
    retried = harness.store.stage_generated_file(
        JOB_ID,
        "server-audit-bundle",
        GENERATED_STAGE_ID,
        retry_stream,
        len(payload),
        _sha(payload),
    )

    assert retried == first
    assert retry_stream.requests == []
    assert harness.store.validate_staged(retried) is None

    assert (
        _error_code(
            lambda: harness.store.stage_generated_file(
                JOB_ID,
                "server-audit-bundle",
                GENERATED_STAGE_ID,
                InMemoryBinaryStream(b"wrong"),
                5,
                _sha(b"wrong"),
            )
        )
        is ErrorCode.RESOURCE_HASH_MISMATCH
    )


@pytest.mark.parametrize(
    ("expected_size", "expected_sha256", "expected_code"),
    [
        (1, _sha(b"payload"), ErrorCode.RESOURCE_SIZE_MISMATCH),
        (7, "0" * 64, ErrorCode.RESOURCE_HASH_MISMATCH),
    ],
)
def test_stage_file_maps_declared_metadata_failures(
    harness: Harness,
    expected_size: int,
    expected_sha256: str,
    expected_code: ErrorCode,
) -> None:
    code = _error_code(
        lambda: harness.store.stage_file(
            JOB_ID,
            "bad-metadata",
            InMemoryBinaryStream(b"payload"),
            expected_size,
            expected_sha256,
        )
    )
    assert code is expected_code
    directory = proposal_stage_path(
        harness.layout.data_root,
        JOB_ID,
        "bad-metadata",
    )
    assert not (directory / "staged.json").exists()


def test_stage_tree_persists_manifest_and_detects_byte_drift(
    harness: Harness,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "a.txt").write_bytes(b"alpha")
    (source / "nested" / "b.txt").write_bytes(b"beta")

    staged = harness.store.stage_tree(JOB_ID, "tree", source)

    assert staged.resource_kind is ResourceKind.DIRECTORY
    assert staged.tree_manifest is not None
    assert [entry.path for entry in staged.tree_manifest.entries] == [
        "a.txt",
        "nested/b.txt",
    ]
    harness.store.validate_staged(staged)
    directory = proposal_stage_path(harness.layout.data_root, JOB_ID, "tree")
    (directory / "tree" / "a.txt").chmod(0o600)
    (directory / "tree" / "a.txt").write_bytes(b"drift")
    assert _error_code(lambda: harness.store.validate_staged(staged)) is (
        ErrorCode.RESOURCE_HASH_MISMATCH
    )


def test_validate_staged_distinguishes_missing_marker_from_marker_drift(
    harness: Harness,
) -> None:
    missing = _stage_file(harness, proposal_key="missing-marker")
    missing_directory = proposal_stage_path(
        harness.layout.data_root,
        JOB_ID,
        "missing-marker",
    )
    (missing_directory / "staged.json").unlink()
    assert _error_code(lambda: harness.store.validate_staged(missing)) is (
        ErrorCode.RESOURCE_NOT_FOUND
    )

    drifted = _stage_file(harness, proposal_key="drifted-marker")
    drifted_directory = proposal_stage_path(
        harness.layout.data_root,
        JOB_ID,
        "drifted-marker",
    )
    (drifted_directory / "staged.json").write_bytes(b'{"invalid":true}\n')
    assert _error_code(lambda: harness.store.validate_staged(drifted)) is (
        ErrorCode.RESOURCE_HASH_MISMATCH
    )


def test_stage_tree_rejects_links_as_path_violation(
    harness: Harness,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (source / "link").symlink_to(outside)

    assert _error_code(lambda: harness.store.stage_tree(JOB_ID, "tree", source)) is (
        ErrorCode.PATH_VIOLATION
    )


def test_attachment_stage_requires_exact_live_registry_capability(
    harness: Harness,
) -> None:
    payload = b"attachment"
    lease = harness.attachments.acquire(ATTACHMENT_ID)
    try:
        staged = harness.store.stage_attachment(
            ATTACHMENT_ID,
            lease,
            InMemoryBinaryStream(payload),
            len(payload),
            _sha(payload),
        )
        assert staged == AttachmentStagedRef(
            attachment_id=ATTACHMENT_ID,
            resource_kind=ResourceKind.FILE,
            size=len(payload),
            sha256=_sha(payload),
        )
        marker = harness.layout.uploads / ATTACHMENT_ID / "staged.json"
        assert marker.read_bytes() == canonical_json_bytes(staged)
    finally:
        lease.release()

    assert _error_code(
        lambda: harness.store.stage_attachment(
            ATTACHMENT_ID,
            lease,
            InMemoryBinaryStream(payload),
        )
    ) is ErrorCode.UPLOAD_INCOMPLETE


def test_attachment_limit_stops_at_first_forbidden_byte(harness: Harness) -> None:
    from tests.deterministic.unit.storage.fakes import CountingBinaryStream

    lease = harness.attachments.acquire(ATTACHMENT_ID)
    stream = CountingBinaryStream(logical_size=MAX_ATTACHMENT_BYTES + 1)
    try:
        assert _error_code(
            lambda: harness.store.stage_attachment(
                ATTACHMENT_ID,
                lease,
                stream,
            )
        ) is ErrorCode.RESOURCE_LIMIT_EXCEEDED
    finally:
        lease.release()
    assert stream.read_calls == 2561
    assert stream.returned_logical_bytes == MAX_ATTACHMENT_BYTES + 1


@pytest.mark.parametrize(
    ("resource_type", "resource_kind", "tail"),
    [
        (ResourceType.ATTACHMENT, ResourceKind.FILE, "attachments"),
        (ResourceType.EVIDENCE, ResourceKind.FILE, "evidence"),
        (ResourceType.EVIDENCE, ResourceKind.DIRECTORY, "evidence"),
        (ResourceType.ARTIFACT, ResourceKind.DIRECTORY, "artifacts"),
    ],
)
def test_plan_target_consumes_r3_identity_contract(
    harness: Harness,
    resource_type: ResourceType,
    resource_kind: ResourceKind,
    tail: str,
) -> None:
    target = harness.store.plan_target(
        CASE_ID,
        resource_type,
        RESOURCE_ID,
        resource_kind,
        17,
        "7" * 64,
    )
    leaf = "tree" if resource_kind is ResourceKind.DIRECTORY else "payload"
    assert target.final_storage_key == (
        f"resources/cases/{CASE_ID}/{tail}/{RESOURCE_ID}/{leaf}"
    )
    assert harness.store.plan_target(
        CASE_ID,
        resource_type,
        RESOURCE_ID,
        resource_kind,
        17,
        "7" * 64,
    ) == target


def test_plan_target_rejects_attachment_tree_and_oversized_value(
    harness: Harness,
) -> None:
    assert _error_code(
        lambda: harness.store.plan_target(
            CASE_ID,
            ResourceType.ATTACHMENT,
            RESOURCE_ID,
            ResourceKind.DIRECTORY,
            1,
            "7" * 64,
        )
    ) is ErrorCode.VALIDATION_ERROR
    assert _error_code(
        lambda: harness.store.plan_target(
            CASE_ID,
            ResourceType.ATTACHMENT,
            RESOURCE_ID,
            ResourceKind.FILE,
            MAX_ATTACHMENT_BYTES + 1,
            "7" * 64,
        )
    ) is ErrorCode.VALIDATION_ERROR


def test_batch_capacity_requires_lease_is_sorted_and_has_no_partial_publish(
    harness: Harness,
) -> None:
    first = _stage_file(harness, b"a", "first")
    second = _stage_file(harness, b"b", "second")
    first_target = harness.store.plan_target(
        CASE_ID,
        ResourceType.ARTIFACT,
        RESOURCE_ID,
        ResourceKind.FILE,
        MAX_CASE_RESOURCE_BYTES,
        first.sha256,
    )
    second_target = harness.store.plan_target(
        CASE_ID,
        ResourceType.ARTIFACT,
        OTHER_RESOURCE_ID,
        ResourceKind.FILE,
        1,
        second.sha256,
    )

    with pytest.raises(RuntimeError, match="publication lease"):
        harness.store.validate_case_capacity(CASE_ID, [first_target])

    with harness.guard.acquire(), pytest.raises(ApplicationPortError) as raised:
        harness.store.validate_case_capacity(
            CASE_ID,
            sorted(
                [first_target, second_target],
                key=lambda item: item.final_storage_key,
            ),
        )
    assert raised.value.error.code is ErrorCode.RESOURCE_LIMIT_EXCEEDED
    assert raised.value.error.retryable is False
    assert raised.value.error.details == [
        ApplicationErrorDetail(
            field="case_resource_bytes",
            resource_type="CASE",
            resource_id=CASE_ID,
            resource_ref=None,
            expected=None,
            actual=None,
            limit=MAX_CASE_RESOURCE_BYTES,
            observed=MAX_CASE_RESOURCE_BYTES + 1,
        )
    ]
    assert (proposal_stage_path(harness.layout.data_root, JOB_ID, "first") / "payload").exists()
    assert (proposal_stage_path(harness.layout.data_root, JOB_ID, "second") / "payload").exists()
    assert not (harness.layout.data_root / first_target.final_storage_key).exists()
    assert not (harness.layout.data_root / second_target.final_storage_key).exists()


def test_capacity_error_observed_counts_every_physical_formal_class_atomically(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import problem_locator.storage.resource_store as resource_store_module

    # Capacity accounting is deliberately reference-agnostic once an object is
    # under the formal root: state-referenced, durable-outbox-protected, and
    # ordinary-orphan targets all contribute their unique physical key once.
    state_payload = b"state-ref"
    outbox_payload = b"durable-outbox"
    orphan_payload = b"orphan"
    state_referenced = harness.store.plan_target(
        CASE_ID,
        ResourceType.ATTACHMENT,
        ATTACHMENT_ID,
        ResourceKind.FILE,
        len(state_payload),
        _sha(state_payload),
    )
    durable_outbox = harness.store.plan_target(
        CASE_ID,
        ResourceType.EVIDENCE,
        RESOURCE_ID,
        ResourceKind.FILE,
        len(outbox_payload),
        _sha(outbox_payload),
    )
    ordinary_orphan = harness.store.plan_target(
        CASE_ID,
        ResourceType.ARTIFACT,
        OTHER_RESOURCE_ID,
        ResourceKind.FILE,
        len(orphan_payload),
        _sha(orphan_payload),
    )
    formal_paths = {
        _write_formal_file(harness, state_referenced, state_payload),
        _write_formal_file(harness, durable_outbox, outbox_payload),
        _write_formal_file(harness, ordinary_orphan, orphan_payload),
    }
    current_bytes = sum(path.stat().st_size for path in formal_paths)

    first_new = harness.store.plan_target(
        CASE_ID,
        ResourceType.ARTIFACT,
        FIRST_BATCH_RESOURCE_ID,
        ResourceKind.FILE,
        MAX_CASE_RESOURCE_BYTES - current_bytes,
        "d" * 64,
    )
    second_new = harness.store.plan_target(
        CASE_ID,
        ResourceType.ARTIFACT,
        SECOND_BATCH_RESOURCE_ID,
        ResourceKind.FILE,
        1,
        "e" * 64,
    )
    batch = sorted(
        [first_new, second_new],
        key=lambda item: item.final_storage_key,
    )

    real_scan = resource_store_module.scan_case_resources
    scan_lock_observations: list[tuple[bool, bool]] = []

    def scan_while_observing_lock(
        layout: StorageLayout,
        case_id: str,
    ):
        scan_lock_observations.append(
            (
                harness.lock.held_by_current_thread(),
                harness.lock.publication_held_by_current_thread(),
            )
        )
        return real_scan(layout, case_id)

    monkeypatch.setattr(
        resource_store_module,
        "scan_case_resources",
        scan_while_observing_lock,
    )

    with harness.guard.acquire(), pytest.raises(ApplicationPortError) as raised:
        harness.store.validate_case_capacity(CASE_ID, batch)

    assert scan_lock_observations == [(True, True)]
    assert raised.value.error.code is ErrorCode.RESOURCE_LIMIT_EXCEEDED
    assert raised.value.error.retryable is False
    assert raised.value.error.details == [
        ApplicationErrorDetail(
            field="case_resource_bytes",
            resource_type="CASE",
            resource_id=CASE_ID,
            resource_ref=None,
            expected=None,
            actual=None,
            limit=MAX_CASE_RESOURCE_BYTES,
            observed=MAX_CASE_RESOURCE_BYTES + 1,
        )
    ]
    assert all(path.exists() for path in formal_paths)
    assert not (harness.layout.data_root / first_new.final_storage_key).exists()
    assert not (harness.layout.data_root / second_new.final_storage_key).exists()


def test_publish_moves_then_adopts_without_old_staged_content(harness: Harness) -> None:
    payload = b"publish-once"
    staged = _stage_file(harness, payload)
    target = _plan(harness, staged)

    with harness.guard.acquire():
        usage = harness.store.validate_case_capacity(CASE_ID, [target])
        published = harness.store.publish(staged, target.final_storage_key)

    final = harness.layout.data_root / target.final_storage_key
    assert usage.current_bytes == 0
    assert usage.new_bytes == len(payload)
    assert final.read_bytes() == payload
    assert is_read_only(final)
    assert published.storage_key == target.final_storage_key
    assert not (
        proposal_stage_path(harness.layout.data_root, JOB_ID, "artifact") / "payload"
    ).exists()

    with harness.guard.acquire():
        replay_usage = harness.store.validate_case_capacity(CASE_ID, [target])
        adopted = harness.store.publish(staged, target.final_storage_key)
    assert replay_usage.new_bytes == 0
    assert adopted == published


def test_publish_never_overwrites_a_conflicting_formal_target(harness: Harness) -> None:
    staged = _stage_file(harness, b"expected")
    target = _plan(harness, staged)
    final = harness.layout.data_root / target.final_storage_key
    final.parent.mkdir(parents=True)
    final.write_bytes(b"different")

    with harness.guard.acquire():
        assert _error_code(
            lambda: harness.store.publish(staged, target.final_storage_key)
        ) is ErrorCode.RESOURCE_HASH_MISMATCH
    assert final.read_bytes() == b"different"


def test_publish_treats_wrong_existing_node_kind_as_content_conflict(
    harness: Harness,
) -> None:
    staged = _stage_file(harness, b"expected")
    target = _plan(harness, staged)
    final = harness.layout.data_root / target.final_storage_key
    final.mkdir(parents=True)

    with harness.guard.acquire():
        assert _error_code(
            lambda: harness.store.publish(staged, target.final_storage_key)
        ) is ErrorCode.RESOURCE_HASH_MISMATCH
    assert final.is_dir()


def test_attachment_publish_identity_and_live_lease_are_enforced(
    harness: Harness,
) -> None:
    payload = b"attachment"
    lease = harness.attachments.acquire(ATTACHMENT_ID)
    staged = harness.store.stage_attachment(
        ATTACHMENT_ID,
        lease,
        InMemoryBinaryStream(payload),
    )
    target = harness.store.plan_target(
        CASE_ID,
        ResourceType.ATTACHMENT,
        ATTACHMENT_ID,
        ResourceKind.FILE,
        staged.size,
        staged.sha256,
    )
    with harness.guard.acquire():
        published = harness.store.publish(staged, target.final_storage_key)
    lease.release()
    assert published.storage_key == target.final_storage_key

    with harness.guard.acquire():
        with pytest.raises(RuntimeError, match="active upload lease"):
            harness.store.publish(staged, target.final_storage_key)

    replay_lease = harness.attachments.acquire(ATTACHMENT_ID)
    try:
        with harness.guard.acquire():
            assert harness.store.publish(staged, target.final_storage_key) == published
    finally:
        replay_lease.release()


def test_attachment_archive_suffix_changes_only_the_workspace_path(
    harness: Harness,
) -> None:
    payload = b"opaque tar-gzip attachment bytes"
    lease = harness.attachments.acquire(ATTACHMENT_ID)
    try:
        staged = harness.store.stage_attachment(
            ATTACHMENT_ID,
            lease,
            InMemoryBinaryStream(payload),
        )
        target = harness.store.plan_target(
            CASE_ID,
            ResourceType.ATTACHMENT,
            ATTACHMENT_ID,
            ResourceKind.FILE,
            staged.size,
            staged.sha256,
        )
        with harness.guard.acquire():
            published = harness.store.publish(staged, target.final_storage_key)
    finally:
        lease.release()

    assert published.storage_key.endswith(f"/attachments/{ATTACHMENT_ID}/payload")
    destination = (
        harness.layout.workspaces
        / JOB_ID
        / Path(
            workspace_attachment_relative_path(
                ATTACHMENT_ID,
                AttachmentFilenameSuffix.TAR_GZ,
            )
        )
    )

    materialized = harness.store.materialize_read_only(published, destination)

    assert materialized.path == str(destination)
    assert materialized.read_only is True
    assert destination.name == "payload.tar.gz"
    assert destination.read_bytes() == payload
    assert is_read_only(destination)


def test_open_and_materialize_file_are_revalidated_and_read_only(
    harness: Harness,
) -> None:
    payload = b"readable"
    staged = _stage_file(harness, payload)
    target = _plan(harness, staged)
    with harness.guard.acquire():
        resource_ref = harness.store.publish(staged, target.final_storage_key)

    with harness.store.open_read(resource_ref) as stream:
        assert stream.read(1024) == payload
        assert stream.read(1024) == b""

    destination = (
        harness.layout.workspaces
        / JOB_ID
        / "inputs"
        / "artifacts"
        / RESOURCE_ID
        / "payload"
    )
    materialized = harness.store.materialize_read_only(resource_ref, destination)
    assert materialized.path == str(destination)
    assert materialized.read_only
    assert destination.read_bytes() == payload
    assert is_read_only(destination)

    final = harness.layout.data_root / target.final_storage_key
    final.chmod(0o600)
    final.write_bytes(b"drifted")
    assert _error_code(lambda: harness.store.open_read(resource_ref)) is (
        ErrorCode.RESOURCE_SIZE_MISMATCH
    )


def test_tree_publish_and_materialization_rebuilds_manifest(
    harness: Harness,
    tmp_path: Path,
) -> None:
    source = tmp_path / "tree"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "data.txt").write_bytes(b"tree-data")
    staged = harness.store.stage_tree(JOB_ID, "tree", source)
    target = harness.store.plan_target(
        CASE_ID,
        ResourceType.ARTIFACT,
        RESOURCE_ID,
        staged.resource_kind,
        staged.size,
        staged.sha256,
    )
    with harness.guard.acquire():
        resource_ref = harness.store.publish(staged, target.final_storage_key)

    destination = (
        harness.layout.workspaces
        / JOB_ID
        / "inputs"
        / "artifacts"
        / RESOURCE_ID
        / "tree"
    )
    result = harness.store.materialize_read_only(resource_ref, destination)
    assert result.path == str(destination)
    assert (destination / "nested" / "data.txt").read_bytes() == b"tree-data"
    assert is_read_only(destination)
    assert is_read_only(destination / "nested" / "data.txt")
    assert "materialization" in harness.ids.new_calls


def test_discard_is_idempotent_and_attachment_discard_requires_lease(
    harness: Harness,
) -> None:
    proposal = _stage_file(harness)
    proposal_directory = proposal_stage_path(
        harness.layout.data_root,
        proposal.owner_job_id,
        proposal.proposal_key,
    )
    harness.store.discard(proposal)
    harness.store.discard(proposal)
    assert not proposal_directory.exists()

    lease = harness.attachments.acquire(ATTACHMENT_ID)
    attachment = harness.store.stage_attachment(
        ATTACHMENT_ID,
        lease,
        InMemoryBinaryStream(b"attachment"),
    )
    lease.release()
    with pytest.raises(RuntimeError, match="no active attachment"):
        harness.store.discard(attachment)
    lease = harness.attachments.acquire(ATTACHMENT_ID)
    try:
        harness.store.discard(attachment)
    finally:
        lease.release()
    assert not (harness.layout.uploads / ATTACHMENT_ID).exists()


def test_discard_retry_reapplies_parent_sync_after_successful_removal(
    harness: Harness,
) -> None:
    proposal = _stage_file(harness, proposal_key="discard-sync-retry")
    directory = proposal_stage_path(
        harness.layout.data_root,
        proposal.owner_job_id,
        proposal.proposal_key,
    )
    harness.sync.fail_next(
        "sync_directory",
        OSError("injected discard parent sync failure"),
    )

    with pytest.raises(OSError, match="parent sync failure"):
        harness.store.discard(proposal)
    assert not directory.exists()

    harness.store.discard(proposal)
    assert harness.sync.calls("sync_directory")[-1].path == directory.parent


def test_discard_rejects_linked_stage_content_without_touching_target(
    harness: Harness,
    tmp_path: Path,
) -> None:
    proposal = _stage_file(harness, proposal_key="linked-discard")
    directory = proposal_stage_path(
        harness.layout.data_root,
        proposal.owner_job_id,
        proposal.proposal_key,
    )
    outside = tmp_path / "outside-sentinel"
    outside.write_bytes(b"must survive")
    (directory / "payload").unlink()
    (directory / "payload").symlink_to(outside)

    with pytest.raises(ValueError, match="ordinary"):
        harness.store.discard(proposal)

    assert outside.read_bytes() == b"must survive"
    assert directory.exists()


def test_stage_path_capabilities_exclude_cleaner_and_writer_claims(
    tmp_path: Path,
) -> None:
    registry = StagePathRegistry()
    directory = tmp_path / "stage"
    stage = registry.acquire_stage(directory)
    assert registry.is_stage_active(directory)
    assert registry.active_stage_directories() == (directory,)
    assert registry.try_acquire_cleanup(directory) is None
    stage.release()

    cleanup = registry.try_acquire_cleanup(directory)
    assert cleanup is not None
    assert not registry.is_stage_active(directory)
    with pytest.raises(RuntimeError, match="active claim"):
        registry.acquire_stage(directory)
    cleanup.release()
    assert registry.active_stage_directories() == ()


def test_markerless_in_progress_stage_is_visible_to_cleaner_registry(
    harness: Harness,
) -> None:
    stream = BlockingStream(b"blocked")
    outcomes: list[object] = []

    def stage() -> None:
        outcomes.append(harness.store.stage_file(JOB_ID, "blocked", stream))

    thread = threading.Thread(target=stage)
    thread.start()
    assert stream.entered.wait(1)
    directory = proposal_stage_path(
        harness.layout.data_root,
        JOB_ID,
        "blocked",
    )
    assert harness.store.active_stage_directories() == (directory,)
    assert harness.stages.try_acquire_cleanup(directory) is None
    assert not (directory / "staged.json").exists()
    stream.proceed.set()
    thread.join(2)
    assert not thread.is_alive()
    assert isinstance(outcomes[0], StagedResourceRef)
    assert harness.store.active_stage_directories() == ()


def test_injected_marker_replace_failure_is_typed_and_never_completes_stage(
    harness: Harness,
) -> None:
    # Content publication is replace #1; marker publication is replace #2.
    harness.replacer.fail_on(2, OSError("marker replace fault"))
    assert _error_code(lambda: _stage_file(harness)) is ErrorCode.RESOURCE_STAGE_FAILED
    directory = proposal_stage_path(harness.layout.data_root, JOB_ID, "artifact")
    assert (directory / "payload").exists()
    assert not (directory / "staged.json").exists()
    assert harness.store.active_stage_directories() == ()
