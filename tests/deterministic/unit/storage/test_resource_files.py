from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from problem_locator.contracts import (
    AttachmentFilenameSuffix,
    ResourceKind,
    ResourceRef,
    workspace_attachment_relative_path,
)
from problem_locator.contracts.limits import MAX_CASE_RESOURCE_BYTES
from problem_locator.storage import resource_files
from problem_locator.storage.atomic import finalize_read_only_tree, is_read_only
from problem_locator.storage.coordination import (
    InProcessPublicationCommitGuard,
    StorageCoordinationLock,
)
from problem_locator.storage.layout import StorageLayout
from problem_locator.storage.paths import formal_storage_key, proposal_stage_path
from problem_locator.storage.platform import PlatformFileSync
from problem_locator.storage.resource_files import (
    FormalResourcePublisher,
    FormalResourceReader,
    calculate_case_usage,
    scan_all_resources,
    scan_case_resources,
)
from problem_locator.storage.tree import TreeInspection, inspect_tree
from tests.deterministic.unit.storage.fakes import FakeFileSync, FaultInjectingReplace
from tests.deterministic.unit.storage.platform_support import symlink_or_skip


CASE_ID = "00000000-0000-0000-0000-000000000101"
OTHER_CASE_ID = "00000000-0000-0000-0000-000000000102"
RESOURCE_ID = "00000000-0000-0000-0000-000000000201"
OTHER_RESOURCE_ID = "00000000-0000-0000-0000-000000000202"
JOB_ID = "00000000-0000-0000-0000-000000000301"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class DurableRecordingFileSync(FakeFileSync):
    """Exercise real chmod/fsync while retaining deterministic fault points."""

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


def _layout(tmp_path: Path) -> StorageLayout:
    tmp_path.mkdir(parents=True, exist_ok=True)
    layout = StorageLayout.at(tmp_path / "data")
    layout.ensure_directories()
    return layout


def _key(
    *,
    case_id: str = CASE_ID,
    category: str = "evidence",
    resource_id: str = RESOURCE_ID,
    kind: ResourceKind = ResourceKind.FILE,
) -> str:
    return formal_storage_key(case_id, category, resource_id, kind)


def _formal_file(
    layout: StorageLayout,
    payload: bytes,
    *,
    case_id: str = CASE_ID,
    category: str = "evidence",
    resource_id: str = RESOURCE_ID,
    read_only: bool = False,
) -> tuple[Path, ResourceRef]:
    storage_key = _key(
        case_id=case_id,
        category=category,
        resource_id=resource_id,
    )
    path = layout.data_root / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    if read_only:
        path.chmod(0o444)
    return path, ResourceRef(
        resource_kind=ResourceKind.FILE,
        storage_key=storage_key,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _staged_file(layout: StorageLayout, payload: bytes = b"staged bytes") -> Path:
    directory = layout.uploads / RESOURCE_ID
    directory.mkdir(parents=True)
    path = directory / "payload"
    path.write_bytes(payload)
    (directory / "staged.json").write_bytes(b"{}\n")
    return path


def _staged_tree(layout: StorageLayout) -> tuple[Path, TreeInspection]:
    directory = proposal_stage_path(layout.data_root, JOB_ID, "tree-proposal")
    tree = directory / "tree"
    (tree / "nested").mkdir(parents=True)
    (tree / "root.txt").write_bytes(b"root")
    (tree / "nested" / "child.txt").write_bytes(b"child")
    (directory / "staged.json").write_bytes(b"{}\n")
    return tree, inspect_tree(tree)


def _publisher(
    layout: StorageLayout,
    *,
    sync: FakeFileSync | None = None,
    replacer: FaultInjectingReplace | None = None,
) -> tuple[
    StorageCoordinationLock,
    InProcessPublicationCommitGuard,
    FakeFileSync,
    FaultInjectingReplace,
    FormalResourcePublisher,
]:
    lock = StorageCoordinationLock()
    guard = InProcessPublicationCommitGuard(lock)
    selected_sync = sync or DurableRecordingFileSync()
    selected_replacer = replacer or FaultInjectingReplace()
    return (
        lock,
        guard,
        selected_sync,
        selected_replacer,
        FormalResourcePublisher(layout, lock, selected_sync, selected_replacer),
    )


def _publish_file(
    publisher: FormalResourcePublisher,
    staged: Path,
    payload: bytes,
    storage_key: str,
) -> object:
    return publisher.publish(
        staged,
        storage_key,
        expected_kind=ResourceKind.FILE,
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_tree_manifest=None,
    )


def _assert_read_only_tree(root: Path) -> None:
    assert is_read_only(root)
    for current, directory_names, filenames in os.walk(root):
        current_path = Path(current)
        assert is_read_only(current_path)
        assert all(is_read_only(current_path / name) for name in directory_names)
        assert all(is_read_only(current_path / name) for name in filenames)


def test_strict_scan_counts_orphans_and_excludes_tmp_and_quarantine(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    orphan, orphan_ref = _formal_file(layout, b"orphan")
    other, other_ref = _formal_file(
        layout,
        b"second",
        category="artifacts",
        resource_id=OTHER_RESOURCE_ID,
    )
    (layout.uploads / RESOURCE_ID).mkdir()
    (layout.uploads / RESOURCE_ID / "payload").write_bytes(b"not formal")
    quarantined = layout.quarantine / "cleanup" / "resources" / "cases"
    quarantined.mkdir(parents=True)
    (quarantined / "payload").write_bytes(b"not formal either")

    observed = scan_case_resources(layout, CASE_ID)
    global_observed = scan_all_resources(layout)

    assert set(observed) == {orphan_ref.storage_key, other_ref.storage_key}
    assert set(global_observed) == set(observed)
    assert observed[orphan_ref.storage_key].path == orphan
    assert observed[other_ref.storage_key].path == other
    assert sum(item.size for item in observed.values()) == len(b"orphansecond")


@pytest.mark.parametrize(
    "build_invalid",
    [
        lambda layout: (layout.cases_resources / CASE_ID / "unknown").mkdir(
            parents=True
        ),
        lambda layout: (
            layout.cases_resources / CASE_ID / "attachments" / RESOURCE_ID / "tree"
        ).mkdir(parents=True),
        lambda layout: (
            layout.cases_resources / CASE_ID / "evidence" / RESOURCE_ID / "payload"
        ).mkdir(parents=True),
    ],
)
def test_strict_scan_rejects_invalid_hierarchy(
    tmp_path: Path,
    build_invalid: Callable[[StorageLayout], None],
) -> None:
    layout = _layout(tmp_path)
    build_invalid(layout)

    with pytest.raises(ValueError):
        scan_case_resources(layout, CASE_ID)


def test_strict_scan_rejects_links_and_hardlinked_tree_entries(tmp_path: Path) -> None:
    symlink_layout = _layout(tmp_path / "symlink")
    outside = tmp_path / "outside"
    outside.mkdir()
    case_root = symlink_layout.cases_resources / CASE_ID
    case_root.mkdir()
    symlink_or_skip(case_root / "evidence", outside, target_is_directory=True)
    with pytest.raises(ValueError):
        scan_case_resources(symlink_layout, CASE_ID)

    hardlink_layout = _layout(tmp_path / "hardlink")
    tree = (
        hardlink_layout.cases_resources
        / CASE_ID
        / "artifacts"
        / RESOURCE_ID
        / "tree"
    )
    tree.mkdir(parents=True)
    first = tree / "first"
    first.write_bytes(b"shared")
    os.link(first, tree / "second")
    with pytest.raises(ValueError, match="hard-linked"):
        scan_case_resources(hardlink_layout, CASE_ID)


def test_calculate_usage_accepts_exact_five_gib_and_counts_equal_hash_keys(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    first = _key()
    second = _key(resource_id=OTHER_RESOURCE_ID)

    current, new, total = calculate_case_usage(
        layout,
        CASE_ID,
        [
            (first, ResourceKind.FILE, MAX_CASE_RESOURCE_BYTES - 1, EMPTY_SHA256),
            (second, ResourceKind.FILE, 1, EMPTY_SHA256),
        ],
    )

    assert (current, new, total) == (
        0,
        MAX_CASE_RESOURCE_BYTES,
        MAX_CASE_RESOURCE_BYTES,
    )
    with pytest.raises(OverflowError):
        calculate_case_usage(
            layout,
            CASE_ID,
            [
                (first, ResourceKind.FILE, MAX_CASE_RESOURCE_BYTES, EMPTY_SHA256),
                (second, ResourceKind.FILE, 1, EMPTY_SHA256),
            ],
        )


def test_calculate_usage_same_target_is_delta_zero_and_duplicate_is_deduplicated(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    _, existing = _formal_file(layout, b"present")
    target = (
        existing.storage_key,
        existing.resource_kind,
        existing.size,
        existing.sha256,
    )

    assert calculate_case_usage(layout, CASE_ID, [target, target]) == (
        existing.size,
        0,
        existing.size,
    )


def test_calculate_usage_rejects_cross_case_and_same_key_conflicts(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    key = _key()
    cross_case = _key(case_id=OTHER_CASE_ID)

    with pytest.raises(ValueError, match="another Case"):
        calculate_case_usage(
            layout,
            CASE_ID,
            [(cross_case, ResourceKind.FILE, 1, EMPTY_SHA256)],
        )
    with pytest.raises(ValueError, match="conflicting planned content"):
        calculate_case_usage(
            layout,
            CASE_ID,
            [
                (key, ResourceKind.FILE, 1, EMPTY_SHA256),
                (key, ResourceKind.FILE, 2, EMPTY_SHA256),
            ],
        )


def test_publisher_requires_real_publication_lease_and_completed_marker(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    payload = b"lease-bound"
    staged = _staged_file(layout, payload)
    _, guard, _, _, publisher = _publisher(layout)
    storage_key = _key()

    with pytest.raises(RuntimeError, match="publication lease"):
        _publish_file(publisher, staged, payload, storage_key)

    (staged.parent / "staged.json").unlink()
    with guard.acquire():
        with pytest.raises(FileNotFoundError):
            _publish_file(publisher, staged, payload, storage_key)
    assert staged.exists()


def test_publisher_moves_first_file_then_idempotently_adopts_same_target(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    payload = b"immutable"
    staged = _staged_file(layout, payload)
    _, guard, sync, replacer, publisher = _publisher(layout)
    storage_key = _key()
    final = layout.data_root / storage_key

    with guard.acquire():
        receipt = _publish_file(publisher, staged, payload, storage_key)

    assert receipt.path == final
    assert final.read_bytes() == payload
    assert not staged.exists()
    assert (staged.parent / "staged.json").exists()
    assert is_read_only(final)
    assert [(event.source, event.destination) for event in replacer.events] == [
        (staged, final)
    ]
    assert sync.calls("readonly_file")[-1].path == final
    assert sync.calls("sync_directory")[-1].path == final.parent

    # Adoption does not inspect or require the obsolete staging path.
    with guard.acquire():
        adopted = _publish_file(
            publisher,
            staged.parent / "missing-payload",
            payload,
            storage_key,
        )
    assert adopted.path == final
    assert replacer.call_count == 1


def test_publisher_rejects_existing_target_with_different_content(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    final, _ = _formal_file(layout, b"different", read_only=True)
    missing_stage = layout.uploads / RESOURCE_ID / "payload"
    _, guard, _, replacer, publisher = _publisher(layout)

    with guard.acquire():
        with pytest.raises(ValueError, match="size|hash"):
            _publish_file(publisher, missing_stage, b"expected", _key())

    assert final.read_bytes() == b"different"
    assert replacer.events == []


def test_publisher_moves_and_finalizes_tree(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    staged, inspection = _staged_tree(layout)
    _, guard, _, replacer, publisher = _publisher(layout)
    storage_key = _key(category="artifacts", kind=ResourceKind.DIRECTORY)
    final = layout.data_root / storage_key

    with guard.acquire():
        publisher.publish(
            staged,
            storage_key,
            expected_kind=ResourceKind.DIRECTORY,
            expected_size=inspection.size,
            expected_sha256=inspection.sha256,
            expected_tree_manifest=inspection.manifest,
        )

    assert replacer.events[0].source == staged
    assert final.joinpath("nested", "child.txt").read_bytes() == b"child"
    _assert_read_only_tree(final)


def test_replace_failure_leaves_stage_and_retry_completes_publication(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    payload = b"replace retry"
    staged = _staged_file(layout, payload)
    replacer = FaultInjectingReplace()
    replacer.fail_next(OSError("replace failed"))
    _, guard, _, _, publisher = _publisher(layout, replacer=replacer)
    final = layout.data_root / _key()

    with guard.acquire():
        with pytest.raises(OSError, match="replace failed"):
            _publish_file(publisher, staged, payload, _key())
    assert staged.exists()
    assert not final.exists()

    with guard.acquire():
        _publish_file(publisher, staged, payload, _key())
    assert final.read_bytes() == payload
    assert not staged.exists()
    assert replacer.call_count == 2


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        ("readonly_file", "readonly failed"),
        ("sync_file", "file sync failed"),
        ("sync_directory", "directory sync failed"),
    ],
)
def test_finalize_failure_after_move_is_completed_by_idempotent_retry(
    tmp_path: Path,
    operation: str,
    message: str,
) -> None:
    layout = _layout(tmp_path)
    payload = b"finalize retry"
    staged = _staged_file(layout, payload)
    sync = DurableRecordingFileSync()
    if operation == "sync_directory":
        sync.fail_on(operation, 5, OSError(message))
    else:
        sync.fail_next(operation, OSError(message))  # type: ignore[arg-type]
    _, guard, _, replacer, publisher = _publisher(layout, sync=sync)
    final = layout.data_root / _key()
    if operation == "sync_directory":
        # Isolate the post-publication durability boundary from the earlier,
        # independently durable creation of the formal parent hierarchy.
        final.parent.mkdir(parents=True)

    with guard.acquire():
        with pytest.raises(OSError, match=message):
            _publish_file(publisher, staged, payload, _key())
    assert final.read_bytes() == payload
    assert not staged.exists()

    with guard.acquire():
        _publish_file(publisher, staged, payload, _key())
    assert final.read_bytes() == payload
    assert is_read_only(final)
    assert replacer.call_count == 1


def test_reader_open_file_strictly_validates_bytes_kind_and_read_only(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    path, ref = _formal_file(layout, b"read me", read_only=True)
    reader = FormalResourceReader(layout, DurableRecordingFileSync())

    stream = reader.open_file(ref)
    try:
        assert stream.read(1024) == b"read me"
        assert stream.read(1) == b""
    finally:
        stream.close()

    wrong_hash = ref.model_copy(update={"sha256": EMPTY_SHA256})
    with pytest.raises(ValueError, match="hash"):
        reader.open_file(wrong_hash)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="read-only"):
        reader.open_file(ref)
    directory_ref = ref.model_copy(update={"resource_kind": ResourceKind.DIRECTORY})
    with pytest.raises(ValueError, match="directory"):
        reader.open_file(directory_ref)


def test_reader_materializes_file_at_fixed_workspace_path_by_hardlink(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    source, ref = _formal_file(layout, b"hard link", read_only=True)
    sync = DurableRecordingFileSync()
    reader = FormalResourceReader(layout, sync)
    destination = (
        layout.workspaces
        / JOB_ID
        / "inputs"
        / "evidence"
        / RESOURCE_ID
        / "payload"
    )

    assert reader.materialize(ref, destination) == destination

    assert destination.read_bytes() == b"hard link"
    assert destination.stat().st_ino == source.stat().st_ino
    assert is_read_only(destination)
    assert sync.calls("sync_directory")[-1].path == destination.parent
    # Exact bytes at the frozen destination are an idempotent adoption.  This
    # lets a retry close a prior chmod/fsync/parent-sync durability boundary.
    assert reader.materialize(ref, destination) == destination
    assert destination.stat().st_ino == source.stat().st_ino
    with pytest.raises(ValueError, match="frozen workspace"):
        reader.materialize(ref, layout.workspaces / JOB_ID / "inputs" / "wrong")


def test_reader_materializes_one_resource_into_main_and_logparse_workspaces(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    source, ref = _formal_file(layout, b"two-pass resource", read_only=True)
    reader = FormalResourceReader(layout, DurableRecordingFileSync())
    relative = Path("inputs") / "evidence" / RESOURCE_ID / "payload"
    main_destination = layout.workspaces / JOB_ID / relative
    logparse_destination = (
        layout.workspaces / f"{JOB_ID}.logparse-preprocess" / relative
    )

    assert reader.materialize(ref, main_destination) == main_destination
    assert reader.materialize(ref, logparse_destination) == logparse_destination

    assert main_destination.read_bytes() == b"two-pass resource"
    assert logparse_destination.read_bytes() == b"two-pass resource"
    assert {
        source.stat().st_ino,
        main_destination.stat().st_ino,
        logparse_destination.stat().st_ino,
    } == {source.stat().st_ino}


@pytest.mark.parametrize(
    "workspace_segment",
    [
        f"{JOB_ID}.review",
        f"{JOB_ID}.logparse-preprocess.extra",
        f"{JOB_ID}.logparse-preprocess.logparse-preprocess",
        "not-a-job-id.logparse-preprocess",
    ],
)
def test_reader_rejects_non_product_workspace_suffixes(
    tmp_path: Path,
    workspace_segment: str,
) -> None:
    layout = _layout(tmp_path)
    source, ref = _formal_file(layout, b"fixed source", read_only=True)
    destination = (
        layout.workspaces
        / workspace_segment
        / "inputs"
        / "evidence"
        / RESOURCE_ID
        / "payload"
    )

    with pytest.raises(ValueError):
        FormalResourceReader(layout, DurableRecordingFileSync()).materialize(
            ref,
            destination,
        )

    assert source.read_bytes() == b"fixed source"
    assert not destination.exists()


def test_reader_rejects_traversal_from_logparse_workspace(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    source, ref = _formal_file(layout, b"fixed source", read_only=True)
    destination = (
        layout.workspaces
        / f"{JOB_ID}.logparse-preprocess"
        / "inputs"
        / ".."
        / "evidence"
        / RESOURCE_ID
        / "payload"
    )

    with pytest.raises(ValueError, match="traversal"):
        FormalResourceReader(layout, DurableRecordingFileSync()).materialize(
            ref,
            destination,
        )

    assert source.read_bytes() == b"fixed source"


@pytest.mark.parametrize("suffix", [None, *tuple(AttachmentFilenameSuffix)])
def test_reader_materializes_attachment_file_at_exact_archive_workspace_path(
    tmp_path: Path,
    suffix: AttachmentFilenameSuffix | None,
) -> None:
    layout = _layout(tmp_path)
    source, ref = _formal_file(
        layout,
        b"opaque attachment archive bytes",
        category="attachments",
        read_only=True,
    )
    destination = (
        layout.workspaces
        / JOB_ID
        / Path(workspace_attachment_relative_path(RESOURCE_ID, suffix))
    )
    reader = FormalResourceReader(layout, DurableRecordingFileSync())

    assert reader.materialize(ref, destination) == destination

    assert destination.read_bytes() == b"opaque attachment archive bytes"
    assert destination.stat().st_ino == source.stat().st_ino
    assert destination.name == f"payload{'' if suffix is None else suffix.value}"
    assert is_read_only(destination)
    assert ref.storage_key.endswith(f"/attachments/{RESOURCE_ID}/payload")
    assert source.name == "payload"


def test_reader_rejects_attachment_path_traversal_and_suffix_drift(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    source, ref = _formal_file(
        layout,
        b"untrusted destination must not move source",
        category="attachments",
        read_only=True,
    )
    attachment_root = (
        layout.workspaces / JOB_ID / "inputs" / "attachments" / RESOURCE_ID
    )
    reader = FormalResourceReader(layout, DurableRecordingFileSync())
    invalid_destinations = [
        attachment_root / "payload.GZ",
        attachment_root / "payload.rar",
        attachment_root / "payload.tar.gz-extra",
        attachment_root / "nested" / "payload.zip",
        attachment_root / "payload.zip" / ".." / "payload.tar.gz",
        attachment_root / ".." / OTHER_RESOURCE_ID / "payload.zip",
        tmp_path / "outside" / "payload.zip",
    ]

    for destination in invalid_destinations:
        with pytest.raises(ValueError, match="frozen|traversal|escapes"):
            reader.materialize(ref, destination)

    assert source.read_bytes() == b"untrusted destination must not move source"
    assert not attachment_root.exists()


def test_attachment_archive_copy_uses_private_temp_and_retries_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    source, ref = _formal_file(
        layout,
        b"complete tar gzip bytes",
        category="attachments",
        read_only=True,
    )
    destination = (
        layout.workspaces
        / JOB_ID
        / Path(
            workspace_attachment_relative_path(
                RESOURCE_ID,
                AttachmentFilenameSuffix.TAR_GZ,
            )
        )
    )
    replacer = FaultInjectingReplace()
    replacer.fail_next(OSError("injected archive materialization replace failure"))
    reader = FormalResourceReader(
        layout,
        DurableRecordingFileSync(),
        replacer,
        temp_token_factory=lambda: "archive-copy-retry",
    )

    def unavailable_hardlink(*args: object, **kwargs: object) -> None:
        raise OSError("cross-device")

    monkeypatch.setattr(resource_files.os, "link", unavailable_hardlink)

    with pytest.raises(OSError, match="replace failure"):
        reader.materialize(ref, destination)
    assert not destination.exists()
    assert not list(destination.parent.glob(".*.materializing"))
    assert source.read_bytes() == b"complete tar gzip bytes"

    assert reader.materialize(ref, destination) == destination
    assert destination.read_bytes() == b"complete tar gzip bytes"
    assert is_read_only(destination)
    assert replacer.call_count == 2


@pytest.mark.parametrize(
    ("operation", "occurrence"),
    [
        ("readonly_file", 1),
        ("sync_file", 1),
        ("sync_directory", 5),
    ],
)
def test_reader_materialization_finalize_failure_is_retried_by_adoption(
    tmp_path: Path,
    operation: str,
    occurrence: int,
) -> None:
    layout = _layout(tmp_path)
    _, ref = _formal_file(layout, b"retry materialization", read_only=True)
    sync = DurableRecordingFileSync()
    sync.fail_on(
        operation,  # type: ignore[arg-type]
        occurrence,
        OSError("injected materialization durability failure"),
    )
    reader = FormalResourceReader(layout, sync)
    destination = (
        layout.workspaces
        / JOB_ID
        / "inputs"
        / "evidence"
        / RESOURCE_ID
        / "payload"
    )

    with pytest.raises(OSError, match="durability failure"):
        reader.materialize(ref, destination)

    assert destination.read_bytes() == b"retry materialization"
    assert reader.materialize(ref, destination) == destination
    assert is_read_only(destination)


def test_reader_falls_back_to_copy_when_hardlink_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    source, ref = _formal_file(layout, b"copied", read_only=True)
    sync = DurableRecordingFileSync()
    reader = FormalResourceReader(layout, sync)
    destination = (
        layout.workspaces
        / JOB_ID
        / "inputs"
        / "evidence"
        / RESOURCE_ID
        / "payload"
    )
    def unavailable_hardlink(*args: object, **kwargs: object) -> None:
        raise OSError("cross-device")

    monkeypatch.setattr(resource_files.os, "link", unavailable_hardlink)

    reader.materialize(ref, destination)

    assert destination.read_bytes() == b"copied"
    assert destination.stat().st_ino != source.stat().st_ino
    assert is_read_only(destination)
    assert sync.count("sync_file") >= 2


def test_reader_file_partial_copy_never_claims_final_name_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    _, ref = _formal_file(layout, b"complete file bytes", read_only=True)
    reader = FormalResourceReader(
        layout,
        DurableRecordingFileSync(),
        temp_token_factory=lambda: "file-copy-retry",
    )
    destination = (
        layout.workspaces
        / JOB_ID
        / "inputs"
        / "evidence"
        / RESOURCE_ID
        / "payload"
    )
    real_copy = resource_files.copy_binary_stream
    failed = False

    def unavailable_hardlink(*args: object, **kwargs: object) -> None:
        raise OSError("cross-device")

    def fail_first_copy(*args: object, **kwargs: object):
        nonlocal failed
        if not failed:
            failed = True
            Path(args[1]).write_bytes(b"partial")
            raise OSError("injected mid-copy failure")
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(resource_files.os, "link", unavailable_hardlink)
    monkeypatch.setattr(resource_files, "copy_binary_stream", fail_first_copy)

    with pytest.raises(OSError, match="mid-copy"):
        reader.materialize(ref, destination)
    assert not destination.exists()
    assert not list(destination.parent.glob("*.materializing"))

    assert reader.materialize(ref, destination) == destination
    assert destination.read_bytes() == b"complete file bytes"


def test_reader_materializes_read_only_tree_and_rejects_linked_trees(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    storage_key = _key(category="artifacts", kind=ResourceKind.DIRECTORY)
    source = layout.data_root / storage_key
    (source / "nested").mkdir(parents=True)
    (source / "a.txt").write_bytes(b"a")
    (source / "nested" / "b.txt").write_bytes(b"bb")
    inspection = inspect_tree(source)
    finalize_read_only_tree(source, PlatformFileSync())
    ref = ResourceRef(
        resource_kind=ResourceKind.DIRECTORY,
        storage_key=storage_key,
        size=inspection.size,
        sha256=inspection.sha256,
    )
    destination = (
        layout.workspaces
        / JOB_ID
        / "inputs"
        / "artifacts"
        / RESOURCE_ID
        / "tree"
    )
    reader = FormalResourceReader(layout, PlatformFileSync())

    reader.materialize(ref, destination)

    assert destination.joinpath("nested", "b.txt").read_bytes() == b"bb"
    _assert_read_only_tree(destination)
    assert inspect_tree(destination) == inspection
    with pytest.raises(ValueError, match="frozen workspace"):
        reader.materialize(ref, destination.with_name("tree.tar.gz"))
    assert ref.storage_key.endswith(f"/artifacts/{RESOURCE_ID}/tree")

    linked_layout = _layout(tmp_path / "linked")
    linked_key = _key(category="artifacts", kind=ResourceKind.DIRECTORY)
    linked_source = linked_layout.data_root / linked_key
    linked_source.mkdir(parents=True)
    target = tmp_path / "external-target"
    target.write_bytes(b"outside")
    symlink_or_skip(linked_source / "link", target)
    linked_ref = ResourceRef(
        resource_kind=ResourceKind.DIRECTORY,
        storage_key=linked_key,
        size=0,
        sha256=EMPTY_SHA256,
    )
    linked_destination = (
        linked_layout.workspaces
        / JOB_ID
        / "inputs"
        / "artifacts"
        / RESOURCE_ID
        / "tree"
    )
    with pytest.raises(ValueError, match="symbolic"):
        FormalResourceReader(linked_layout, PlatformFileSync()).materialize(
            linked_ref,
            linked_destination,
        )


def test_reader_tree_partial_copy_never_claims_final_name_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    storage_key = _key(category="artifacts", kind=ResourceKind.DIRECTORY)
    source = layout.data_root / storage_key
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "complete.txt").write_bytes(b"complete tree bytes")
    inspection = inspect_tree(source)
    finalize_read_only_tree(source, PlatformFileSync())
    ref = ResourceRef(
        resource_kind=ResourceKind.DIRECTORY,
        storage_key=storage_key,
        size=inspection.size,
        sha256=inspection.sha256,
    )
    destination = (
        layout.workspaces
        / JOB_ID
        / "inputs"
        / "artifacts"
        / RESOURCE_ID
        / "tree"
    )
    reader = FormalResourceReader(
        layout,
        PlatformFileSync(),
        temp_token_factory=lambda: "tree-copy-retry",
    )
    real_inspect = resource_files.inspect_tree
    failed = False

    def fail_first_tree_copy(root: Path, **kwargs: object):
        nonlocal failed
        copy_to = kwargs.get("copy_to")
        if copy_to is not None and not failed:
            failed = True
            partial = Path(copy_to)
            partial.mkdir()
            (partial / "partial.txt").write_bytes(b"partial")
            raise OSError("injected mid-tree-copy failure")
        return real_inspect(root, **kwargs)

    monkeypatch.setattr(resource_files, "inspect_tree", fail_first_tree_copy)

    with pytest.raises(OSError, match="mid-tree-copy"):
        reader.materialize(ref, destination)
    assert not destination.exists()
    assert not list(destination.parent.glob("*.materializing"))

    assert reader.materialize(ref, destination) == destination
    assert (destination / "nested" / "complete.txt").read_bytes() == (
        b"complete tree bytes"
    )
