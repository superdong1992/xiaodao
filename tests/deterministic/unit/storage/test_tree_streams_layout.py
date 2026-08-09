from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from problem_locator.contracts.serialization import canonical_json_bytes
from problem_locator.storage.layout import DATA_FORMAT_MARKER_BYTES, StorageLayout
from problem_locator.storage.streams import FileBinaryStream, copy_binary_stream, hash_file
from problem_locator.storage.tree import inspect_tree, verify_tree
from tests.deterministic.unit.storage.fakes import FakeFileSync


class RecordingStream:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0
        self.requests: list[int] = []

    def read(self, max_bytes: int) -> bytes:
        self.requests.append(max_bytes)
        chunk = self._data[self._offset : self._offset + max_bytes]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        pass


def test_tree_manifest_is_sorted_and_hashes_canonical_manifest(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "z").mkdir(parents=True)
    (root / "a.txt").write_bytes(b"alpha")
    (root / "z" / "b.txt").write_bytes(b"beta")

    observed = inspect_tree(root)

    assert [entry.path for entry in observed.manifest.entries] == ["a.txt", "z/b.txt"]
    assert [entry.size for entry in observed.manifest.entries] == [5, 4]
    assert observed.size == 9
    assert observed.sha256 == hashlib.sha256(
        canonical_json_bytes(observed.manifest)
    ).hexdigest()
    assert verify_tree(
        root,
        expected_manifest=observed.manifest,
        expected_size=observed.size,
        expected_sha256=observed.sha256,
    ) == observed


def test_tree_rejects_symlinks_hardlinks_fifo_and_nested_empty_directory(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"target")

    symlink_tree = tmp_path / "symlink-tree"
    symlink_tree.mkdir()
    (symlink_tree / "link").symlink_to(target)
    with pytest.raises(ValueError, match="symbolic"):
        inspect_tree(symlink_tree)

    hardlink_tree = tmp_path / "hardlink-tree"
    hardlink_tree.mkdir()
    first = hardlink_tree / "first"
    first.write_bytes(b"shared")
    os.link(first, hardlink_tree / "second")
    with pytest.raises(ValueError, match="hard-linked"):
        inspect_tree(hardlink_tree)

    fifo_tree = tmp_path / "fifo-tree"
    fifo_tree.mkdir()
    os.mkfifo(fifo_tree / "pipe")
    with pytest.raises(ValueError, match="regular files and directories"):
        inspect_tree(fifo_tree)

    empty_tree = tmp_path / "empty-tree"
    (empty_tree / "nested-empty").mkdir(parents=True)
    with pytest.raises(ValueError, match="empty directories"):
        inspect_tree(empty_tree)


def test_tree_byte_limit_accepts_exact_boundary_and_rejects_first_excess(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a").write_bytes(b"abc")
    (root / "b").write_bytes(b"de")

    assert inspect_tree(root, byte_limit=5).size == 5
    with pytest.raises(ValueError, match="byte limit"):
        inspect_tree(root, byte_limit=4)


def test_controlled_tree_copy_preserves_only_manifest_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "root.bin").write_bytes(b"root")
    (source / "nested" / "leaf.bin").write_bytes(b"leaf")
    destination = tmp_path / "copy"

    copied = inspect_tree(source, copy_to=destination)

    assert (destination / "root.bin").read_bytes() == b"root"
    assert (destination / "nested" / "leaf.bin").read_bytes() == b"leaf"
    assert inspect_tree(destination) == copied
    with pytest.raises(FileExistsError):
        inspect_tree(source, copy_to=destination)


def test_tree_detects_file_metadata_change_during_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    source = root / "value"
    source.write_bytes(b"stable bytes")
    real_stat = Path.stat
    source_stat_calls = 0

    def changing_stat(path: Path, *args: object, **kwargs: object) -> object:
        nonlocal source_stat_calls
        result = real_stat(path, *args, **kwargs)
        if path == source and kwargs.get("follow_symlinks") is False:
            source_stat_calls += 1
            if source_stat_calls == 2:
                return SimpleNamespace(
                    st_mode=result.st_mode,
                    st_dev=result.st_dev,
                    st_ino=result.st_ino,
                    st_size=result.st_size,
                    st_mtime_ns=result.st_mtime_ns + 1,
                )
        return result

    monkeypatch.setattr(Path, "stat", changing_stat)
    with pytest.raises(ValueError, match="changed"):
        inspect_tree(root)


def test_verify_tree_rejects_manifest_size_and_hash_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "value").write_bytes(b"value")
    observed = inspect_tree(root)

    with pytest.raises(ValueError, match="size"):
        verify_tree(
            root,
            expected_manifest=observed.manifest,
            expected_size=observed.size + 1,
            expected_sha256=observed.sha256,
        )
    with pytest.raises(ValueError, match="hash"):
        verify_tree(
            root,
            expected_manifest=observed.manifest,
            expected_size=observed.size,
            expected_sha256="0" * 64,
        )


def test_stream_copy_requests_only_limit_plus_one_and_syncs_once(tmp_path: Path) -> None:
    stream = RecordingStream(b"abcde")
    destination = tmp_path / "payload"
    sync = FakeFileSync()

    receipt = copy_binary_stream(stream, destination, file_sync=sync, byte_limit=5)

    assert receipt.size == 5
    assert receipt.sha256 == hashlib.sha256(b"abcde").hexdigest()
    assert destination.read_bytes() == b"abcde"
    assert stream.requests == [6, 1]
    assert sync.count("sync_file") == 1


def test_stream_copy_rejects_on_first_limit_plus_one_byte_without_sync(
    tmp_path: Path,
) -> None:
    stream = RecordingStream(b"abcdef")
    destination = tmp_path / "payload"
    sync = FakeFileSync()

    with pytest.raises(ValueError, match="byte limit"):
        copy_binary_stream(stream, destination, file_sync=sync, byte_limit=5)

    assert stream.requests == [6]
    # The chunk containing the first forbidden byte is rejected in full; no
    # byte from that chunk is allowed to become a completed staging payload.
    assert destination.read_bytes() == b""
    assert sync.count("sync_file") == 0


def test_stream_rejects_lying_or_non_bytes_binary_stream(tmp_path: Path) -> None:
    class LyingStream:
        def read(self, max_bytes: int) -> bytes:
            return bytes(max_bytes + 1)

    class TextStream:
        def read(self, max_bytes: int) -> str:
            return "text"

    with pytest.raises(ValueError, match="more than"):
        copy_binary_stream(
            LyingStream(), tmp_path / "lying", file_sync=FakeFileSync(), byte_limit=5
        )
    with pytest.raises(TypeError, match="immutable bytes"):
        copy_binary_stream(
            TextStream(), tmp_path / "text", file_sync=FakeFileSync(), byte_limit=5
        )


def test_file_binary_stream_is_forward_only_bounded_and_idempotently_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "payload"
    path.write_bytes(b"abcdef")

    stream = FileBinaryStream(path)
    assert stream.read(2) == b"ab"
    assert stream.read(3) == b"cde"
    assert stream.read(3) == b"f"
    assert stream.read(3) == b""
    stream.close()
    stream.close()
    with pytest.raises(ValueError, match="closed"):
        stream.read(1)
    invalid_read_stream = FileBinaryStream(path)
    try:
        with pytest.raises(ValueError, match="positive"):
            invalid_read_stream.read(0)
    finally:
        invalid_read_stream.close()

    assert hash_file(path).size == 6
    assert hash_file(path).sha256 == hashlib.sha256(b"abcdef").hexdigest()


def test_file_binary_stream_rejects_directory_and_final_symlink(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises((ValueError, OSError)):
        FileBinaryStream(directory)

    target = tmp_path / "target"
    target.write_bytes(b"value")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        FileBinaryStream(link)


def test_layout_creates_fixed_empty_structure_and_detects_business_content(
    tmp_path: Path,
) -> None:
    layout = StorageLayout.at(tmp_path / "data")
    layout.ensure_directories()

    assert all(
        path.is_dir() and not path.is_symlink()
        for path in (
            layout.resources,
            layout.cases_resources,
            layout.jobs,
            layout.temporary,
            layout.uploads,
            layout.proposals,
            layout.workspaces,
            layout.quarantine,
            layout.state_temporary,
        )
    )
    assert not layout.has_business_content_without_state()

    case_dir = layout.cases_resources / "case"
    case_dir.mkdir()
    assert layout.has_business_content_without_state()


def test_layout_directory_parent_sync_failure_is_reapplied_on_retry(
    tmp_path: Path,
) -> None:
    layout = StorageLayout.at(tmp_path / "data")
    sync = FakeFileSync()
    sync.fail_next("sync_directory", OSError("layout parent sync failed"))

    with pytest.raises(OSError, match="layout parent sync"):
        layout.ensure_directories(sync)

    assert layout.data_root.is_dir()
    layout.ensure_directories(sync)
    assert layout.state_temporary.is_dir()
    assert [event.path for event in sync.calls("sync_directory")[:2]] == [
        tmp_path,
        tmp_path,
    ]


def test_data_format_marker_sync_failure_retries_before_layout_creation(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    layout = StorageLayout.at(data_root)
    sync = FakeFileSync()
    sync.fail_next("sync_directory", OSError("marker parent sync failed"))

    with pytest.raises(OSError, match="marker parent sync failed"):
        layout.initialize_v2_data_root(sync)

    assert layout.data_format_marker.read_bytes() == DATA_FORMAT_MARKER_BYTES
    assert not layout.resources.exists()

    layout.initialize_v2_data_root(sync)
    assert layout.resources.is_dir()
    assert layout.data_format_marker.read_bytes() == DATA_FORMAT_MARKER_BYTES


def test_layout_detects_job_or_previous_state_and_rejects_symlink_nodes(
    tmp_path: Path,
) -> None:
    job_layout = StorageLayout.at(tmp_path / "jobs-data")
    job_layout.ensure_directories()
    (job_layout.jobs / "job").mkdir()
    assert job_layout.has_business_content_without_state()

    prev_layout = StorageLayout.at(tmp_path / "prev-data")
    prev_layout.ensure_directories()
    prev_layout.previous_state.write_bytes(b"candidate")
    assert prev_layout.has_business_content_without_state()

    bad_layout = StorageLayout.at(tmp_path / "bad-data")
    bad_layout.data_root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    bad_layout.resources.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="real directories"):
        bad_layout.ensure_directories()
