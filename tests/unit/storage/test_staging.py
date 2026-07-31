from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from problem_locator.storage.layout import StorageLayout
from problem_locator.storage.paths import (
    attachment_stage_path,
    proposal_stage_path,
)
from problem_locator.storage.staging import StagedObjectWriter
from problem_locator.storage.tree import inspect_tree
from tests.unit.storage.fakes import (
    CountingBinaryStream,
    DeterministicIdGenerator,
    FakeFileSync,
    FaultInjectingReplace,
)


ATTACHMENT_ID = "00000000-0000-0000-0000-000000000050"
JOB_ID = "00000000-0000-0000-0000-000000000060"
PROPOSAL_KEY = "proposal/with path-unsafe text"
MARKER_BYTES = b'{"schema_version":1,"staging_id":"example"}\n'


class RecordingStream:
    """Forward-only stream that exposes every requested read boundary."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0
        self.requests: list[int] = []
        self.returned_bytes = 0

    def read(self, max_bytes: int) -> bytes:
        self.requests.append(max_bytes)
        chunk = self._payload[self._offset : self._offset + max_bytes]
        self._offset += len(chunk)
        self.returned_bytes += len(chunk)
        return chunk

    def close(self) -> None:
        pass


@dataclass(slots=True)
class WriterHarness:
    layout: StorageLayout
    sync: FakeFileSync
    replacer: FaultInjectingReplace
    ids: DeterministicIdGenerator
    writer: StagedObjectWriter


@pytest.fixture
def harness(tmp_path: Path) -> WriterHarness:
    layout = StorageLayout.at(tmp_path / "data")
    layout.ensure_directories()
    sync = FakeFileSync()
    replacer = FaultInjectingReplace()
    ids = DeterministicIdGenerator(seed="s02-staging-tests")
    writer = StagedObjectWriter(layout, sync, replacer, ids)
    return WriterHarness(layout, sync, replacer, ids, writer)


def _upload_directory(harness: WriterHarness) -> Path:
    return attachment_stage_path(harness.layout.data_root, ATTACHMENT_ID)


def _proposal_directory(harness: WriterHarness) -> Path:
    return proposal_stage_path(harness.layout.data_root, JOB_ID, PROPOSAL_KEY)


def _stage_file(
    harness: WriterHarness,
    payload: bytes = b"abcde",
    *,
    directory: Path | None = None,
    byte_limit: int = 5,
    expected_size: int | None = 5,
    expected_sha256: str | None = None,
) -> RecordingStream:
    stream = RecordingStream(payload)
    harness.writer.stage_file_content(
        directory or _upload_directory(harness),
        stream,
        byte_limit=byte_limit,
        expected_size=expected_size,
        expected_sha256=(
            hashlib.sha256(payload).hexdigest()
            if expected_sha256 is None
            else expected_sha256
        ),
    )
    return stream


def test_file_stage_consumes_one_forward_stream_and_probes_limit_plus_one(
    harness: WriterHarness,
) -> None:
    directory = _upload_directory(harness)
    stream = _stage_file(harness)

    assert stream.requests == [6, 1]
    assert stream.returned_bytes == 5
    assert (directory / "payload").read_bytes() == b"abcde"
    assert not (directory / "staged.json").exists()
    assert harness.ids.new_calls == ["payload"]
    assert [(event.source.name, event.destination.name) for event in harness.replacer.events] == [
        (harness.replacer.events[0].source.name, "payload")
    ]


def test_file_stage_rejects_first_limit_plus_one_byte_before_publication(
    harness: WriterHarness,
) -> None:
    directory = _upload_directory(harness)
    # This is the exact S00 logical stream fake (re-exported by the S02 fake
    # module), so the over-limit path is exercised without a second stream
    # contract implementation or size-dependent retained memory.
    stream = CountingBinaryStream(logical_size=6)

    with pytest.raises(ValueError, match="byte limit"):
        harness.writer.stage_file_content(
            directory,
            stream,
            byte_limit=5,
            expected_size=None,
            expected_sha256=None,
        )

    assert stream.read_calls == 1
    assert stream.returned_logical_bytes == 6
    assert not (directory / "payload").exists()
    assert harness.replacer.call_count == 0
    assert harness.sync.count("sync_file") == 0
    assert len(tuple(directory.glob(".payload-*.tmp"))) == 1


@pytest.mark.parametrize(
    ("expected_size", "expected_sha256", "message"),
    [
        (4, hashlib.sha256(b"abcde").hexdigest(), "size"),
        (5, "0" * 64, "hash"),
    ],
)
def test_file_stage_checks_expected_size_and_hash_before_content_replace(
    harness: WriterHarness,
    expected_size: int,
    expected_sha256: str,
    message: str,
) -> None:
    directory = _proposal_directory(harness)

    with pytest.raises(ValueError, match=message):
        harness.writer.stage_file_content(
            directory,
            RecordingStream(b"abcde"),
            byte_limit=5,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

    assert not (directory / "payload").exists()
    assert not (directory / "staged.json").exists()
    assert harness.replacer.call_count == 0
    # The copied bytes were made durable before their declared metadata was
    # evaluated, and remain incomplete/retry-cleanable without a marker.
    assert harness.sync.count("sync_file") == 1
    assert len(tuple(directory.glob(".payload-*.tmp"))) == 1


def test_file_content_sync_failure_never_replaces_payload(
    harness: WriterHarness,
) -> None:
    directory = _upload_directory(harness)
    harness.sync.fail_next("sync_file", OSError("content fsync failed"))

    with pytest.raises(OSError, match="content fsync failed"):
        _stage_file(harness)

    assert not (directory / "payload").exists()
    assert harness.replacer.call_count == 0
    assert harness.sync.count("sync_directory") == 1  # directory creation only
    assert len(tuple(directory.glob(".payload-*.tmp"))) == 1


def test_file_content_replace_failure_keeps_synced_incomplete_temp(
    harness: WriterHarness,
) -> None:
    directory = _upload_directory(harness)
    harness.replacer.fail_next(OSError("content replace failed"))

    with pytest.raises(OSError, match="content replace failed"):
        _stage_file(harness)

    assert not (directory / "payload").exists()
    assert not (directory / "staged.json").exists()
    assert harness.sync.count("sync_file") == 1
    assert len(tuple(directory.glob(".payload-*.tmp"))) == 1


def test_file_content_directory_sync_failure_occurs_after_atomic_replace(
    harness: WriterHarness,
) -> None:
    directory = _upload_directory(harness)
    # Creating the attachment directory is sync_directory #1; publishing its
    # payload reaches #2 only after the replace.
    harness.sync.fail_on("sync_directory", 2, OSError("content dir fsync failed"))

    with pytest.raises(OSError, match="content dir fsync failed"):
        _stage_file(harness)

    assert (directory / "payload").read_bytes() == b"abcde"
    assert not (directory / "staged.json").exists()
    assert harness.replacer.call_count == 1


def test_marker_is_last_and_same_marker_is_adopted_but_conflict_is_rejected(
    harness: WriterHarness,
) -> None:
    directory = _proposal_directory(harness)
    _stage_file(harness, directory=directory)
    assert [event.destination.name for event in harness.replacer.events] == ["payload"]

    harness.writer.publish_marker(directory, MARKER_BYTES)

    assert [event.destination.name for event in harness.replacer.events] == [
        "payload",
        "staged.json",
    ]
    assert (directory / "staged.json").read_bytes() == MARKER_BYTES
    replace_count = harness.replacer.call_count
    file_sync_count = harness.sync.count("sync_file")
    directory_sync_count = harness.sync.count("sync_directory")

    # Adoption is not a no-op: durability is re-established without replacing
    # the already-identical immutable marker.
    harness.writer.publish_marker(directory, MARKER_BYTES)
    assert harness.replacer.call_count == replace_count
    assert harness.sync.count("sync_file") == file_sync_count + 1
    assert harness.sync.count("sync_directory") == directory_sync_count + 1

    with pytest.raises(FileExistsError, match="different"):
        harness.writer.publish_marker(directory, b'{"different":true}\n')
    assert (directory / "staged.json").read_bytes() == MARKER_BYTES
    assert harness.replacer.call_count == replace_count


def test_marker_replace_failure_leaves_only_synced_temporary_marker(
    harness: WriterHarness,
) -> None:
    directory = _upload_directory(harness)
    _stage_file(harness)
    harness.replacer.fail_next(OSError("marker replace failed"))

    with pytest.raises(OSError, match="marker replace failed"):
        harness.writer.publish_marker(directory, MARKER_BYTES)

    assert not (directory / "staged.json").exists()
    marker_temps = tuple(directory.glob(".staged-marker-*.tmp"))
    assert len(marker_temps) == 1
    assert marker_temps[0].read_bytes() == MARKER_BYTES
    assert (directory / "payload").read_bytes() == b"abcde"


def test_temporary_marker_is_ignored_and_completed_stage_is_not_overwritten(
    harness: WriterHarness,
) -> None:
    directory = _upload_directory(harness)
    directory.mkdir()
    (directory / ".staged-marker-interrupted.tmp").write_bytes(MARKER_BYTES)
    assert harness.writer.read_marker(directory) is None

    _stage_file(harness)
    harness.writer.publish_marker(directory, MARKER_BYTES)
    original_payload = (directory / "payload").read_bytes()
    replace_count = harness.replacer.call_count

    with pytest.raises(FileExistsError, match="completion marker"):
        harness.writer.stage_file_content(
            directory,
            RecordingStream(b"other"),
            byte_limit=5,
            expected_size=5,
            expected_sha256=hashlib.sha256(b"other").hexdigest(),
        )

    assert (directory / "payload").read_bytes() == original_payload
    assert (directory / "staged.json").read_bytes() == MARKER_BYTES
    assert harness.replacer.call_count == replace_count


def test_only_exact_upload_and_hashed_proposal_paths_are_accepted(
    harness: WriterHarness,
) -> None:
    upload = _upload_directory(harness)
    proposal = _proposal_directory(harness)
    _stage_file(harness, directory=upload)
    _stage_file(harness, directory=proposal)

    assert upload == harness.layout.uploads / ATTACHMENT_ID
    assert proposal.parent == harness.layout.proposals / JOB_ID
    assert proposal.name.startswith("p-") and len(proposal.name) == 66
    assert (upload / "payload").read_bytes() == b"abcde"
    assert (proposal / "payload").read_bytes() == b"abcde"

    invalid_paths = (
        harness.layout.uploads / ATTACHMENT_ID / "extra",
        harness.layout.proposals / JOB_ID / PROPOSAL_KEY,
        harness.layout.temporary / "other" / ATTACHMENT_ID,
        harness.layout.data_root.parent / ATTACHMENT_ID,
    )
    for invalid in invalid_paths:
        with pytest.raises(ValueError, match="exact|canonical|escapes"):
            harness.writer.stage_file_content(
                invalid,
                RecordingStream(b"x"),
                byte_limit=1,
                expected_size=1,
                expected_sha256=hashlib.sha256(b"x").hexdigest(),
            )


@pytest.mark.parametrize("stage_kind", ["upload", "proposal"])
def test_staging_path_symlinks_are_rejected_before_stream_is_read(
    harness: WriterHarness,
    tmp_path: Path,
    stage_kind: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    if stage_kind == "upload":
        directory = _upload_directory(harness)
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory = _proposal_directory(harness)
        (harness.layout.proposals / JOB_ID).symlink_to(
            outside,
            target_is_directory=True,
        )
    stream = RecordingStream(b"secret")

    with pytest.raises(ValueError, match="real directory"):
        harness.writer.stage_file_content(
            directory,
            stream,
            byte_limit=6,
            expected_size=6,
            expected_sha256=hashlib.sha256(b"secret").hexdigest(),
        )

    assert stream.requests == []
    assert tuple(outside.iterdir()) == ()


def test_tree_stage_is_a_controlled_copy_with_verified_manifest_hash(
    harness: WriterHarness,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-tree"
    (source / "nested").mkdir(parents=True)
    (source / "root.bin").write_bytes(b"root")
    (source / "nested" / "leaf.bin").write_bytes(b"leaf")
    expected = inspect_tree(source)
    directory = _proposal_directory(harness)

    observed = harness.writer.stage_tree_content(
        directory,
        source,
        byte_limit=8,
        expected_manifest_hash=expected.sha256,
    )

    staged = directory / "tree"
    assert observed == expected
    assert (staged / "root.bin").read_bytes() == b"root"
    assert (staged / "nested" / "leaf.bin").read_bytes() == b"leaf"
    assert os.stat(source / "root.bin").st_ino != os.stat(staged / "root.bin").st_ino
    assert inspect_tree(staged) == expected
    assert not (directory / "staged.json").exists()


def test_tree_hash_mismatch_and_sync_failure_do_not_publish_tree(
    harness: WriterHarness,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-tree"
    source.mkdir()
    (source / "value").write_bytes(b"value")
    directory = _proposal_directory(harness)

    with pytest.raises(ValueError, match="manifest hash"):
        harness.writer.stage_tree_content(
            directory,
            source,
            byte_limit=5,
            expected_manifest_hash="0" * 64,
        )
    assert not (directory / "tree").exists()
    assert len(tuple(directory.glob(".tree-*.tmp"))) == 1
    assert harness.replacer.call_count == 0

    # A new exact path lets us independently exercise a file fsync failure
    # while walking the controlled copy.
    second = proposal_stage_path(harness.layout.data_root, JOB_ID, "second")
    harness.sync.fail_next("sync_file", OSError("tree file fsync failed"))
    with pytest.raises(OSError, match="tree file fsync failed"):
        harness.writer.stage_tree_content(
            second,
            source,
            byte_limit=5,
            expected_manifest_hash=inspect_tree(source).sha256,
        )
    assert not (second / "tree").exists()
    assert len(tuple(second.glob(".tree-*.tmp"))) == 1


def test_incomplete_old_tree_is_abandoned_and_retry_installs_new_tree(
    harness: WriterHarness,
    tmp_path: Path,
) -> None:
    directory = _proposal_directory(harness)
    directory.mkdir(parents=True)
    old_tree = directory / "tree"
    old_tree.mkdir()
    (old_tree / "partial").write_bytes(b"old")

    source = tmp_path / "source-tree"
    source.mkdir()
    (source / "complete").write_bytes(b"new")
    expected = inspect_tree(source)

    observed = harness.writer.stage_tree_content(
        directory,
        source,
        byte_limit=3,
        expected_manifest_hash=expected.sha256,
    )

    assert observed == expected
    assert not (directory / "tree" / "partial").exists()
    assert (directory / "tree" / "complete").read_bytes() == b"new"
    abandoned = tuple(directory.glob(".tree-abandoned-*.tmp"))
    assert len(abandoned) == 1
    assert (abandoned[0] / "partial").read_bytes() == b"old"
    assert [event.destination.name for event in harness.replacer.events] == [
        abandoned[0].name,
        "tree",
    ]


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_tree_stage_rejects_linked_source_without_publishing(
    harness: WriterHarness,
    tmp_path: Path,
    link_kind: str,
) -> None:
    source = tmp_path / "source-tree"
    source.mkdir()
    first = source / "first"
    first.write_bytes(b"shared")
    if link_kind == "symlink":
        (source / "linked").symlink_to(first)
        expected_message = "symbolic"
    else:
        os.link(first, source / "linked")
        expected_message = "hard-linked"

    directory = _proposal_directory(harness)
    with pytest.raises(ValueError, match=expected_message):
        harness.writer.stage_tree_content(
            directory,
            source,
            byte_limit=12,
            expected_manifest_hash=None,
        )

    assert not (directory / "tree").exists()
    assert not (directory / "staged.json").exists()
    assert harness.replacer.call_count == 0
