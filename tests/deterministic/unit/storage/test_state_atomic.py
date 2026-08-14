from __future__ import annotations

from pathlib import Path

import pytest

from problem_locator.storage.layout import StorageLayout
from problem_locator.storage.atomic import read_stable_file_bytes
from problem_locator.storage.state_atomic import AtomicStateFileWriter
from tests.deterministic.unit.storage.fakes import (
    DeterministicIdGenerator,
    FakeFileSync,
    FaultInjectingReplace,
)
from tests.deterministic.unit.storage.platform_support import symlink_or_skip


def _writer(
    tmp_path: Path,
    *,
    file_sync: FakeFileSync | None = None,
    replacer: FaultInjectingReplace | None = None,
) -> tuple[
    StorageLayout,
    AtomicStateFileWriter,
    FakeFileSync,
    FaultInjectingReplace,
]:
    layout = StorageLayout.at(tmp_path)
    layout.ensure_directories()
    sync = file_sync or FakeFileSync()
    replace = replacer or FaultInjectingReplace()
    writer = AtomicStateFileWriter(
        layout,
        sync,
        replace,
        DeterministicIdGenerator(seed="state-atomic-tests"),
    )
    return layout, writer, sync, replace


def test_initial_state_write_syncs_temp_replaces_current_and_verifies_bytes(
    tmp_path: Path,
) -> None:
    layout, writer, sync, replace = _writer(tmp_path)
    payload = b'{"generation":1}\n'

    assert writer.write(payload) == payload

    assert layout.state.read_bytes() == payload
    assert not layout.previous_state.exists()
    assert [event.destination for event in replace.events] == [layout.state]
    assert sync.count("sync_file") == 1
    assert [event.path for event in sync.calls("sync_directory")] == [
        layout.data_root
    ]


def test_update_copies_exact_current_bytes_to_prev_before_replacing_state(
    tmp_path: Path,
) -> None:
    layout, writer, sync, replace = _writer(tmp_path)
    previous = b'{"generation":1}\n'
    current = b'{"generation":2}\n'
    layout.state.write_bytes(previous)

    assert writer.write(current) == current

    assert layout.previous_state.read_bytes() == previous
    assert layout.state.read_bytes() == current
    assert [event.destination for event in replace.events] == [
        layout.previous_state,
        layout.state,
    ]
    assert sync.count("sync_file") == 2
    assert sync.count("sync_directory") == 1


def test_new_temp_sync_failure_does_not_replace_authoritative_state(
    tmp_path: Path,
) -> None:
    sync = FakeFileSync()
    sync.fail_next("sync_file", OSError("new temp sync failed"))
    layout, writer, _, replace = _writer(tmp_path, file_sync=sync)
    layout.state.write_bytes(b"old\n")

    with pytest.raises(OSError, match="new temp sync failed"):
        writer.write(b"new\n")

    assert layout.state.read_bytes() == b"old\n"
    assert not layout.previous_state.exists()
    assert replace.events == []
    assert len(tuple(layout.state_temporary.iterdir())) == 1


def test_prev_replace_failure_leaves_current_state_authoritative(
    tmp_path: Path,
) -> None:
    replace = FaultInjectingReplace()
    replace.fail_on(1, OSError("prev replace failed"))
    layout, writer, _, _ = _writer(tmp_path, replacer=replace)
    layout.state.write_bytes(b"old\n")

    with pytest.raises(OSError, match="prev replace failed"):
        writer.write(b"new\n")

    assert layout.state.read_bytes() == b"old\n"
    assert not layout.previous_state.exists()
    assert [event.destination for event in replace.events] == [layout.previous_state]


def test_current_replace_failure_may_update_prev_but_keeps_current_state(
    tmp_path: Path,
) -> None:
    replace = FaultInjectingReplace()
    replace.fail_on(2, OSError("current replace failed"))
    layout, writer, _, _ = _writer(tmp_path, replacer=replace)
    layout.state.write_bytes(b"old\n")

    with pytest.raises(OSError, match="current replace failed"):
        writer.write(b"new\n")

    assert layout.state.read_bytes() == b"old\n"
    assert layout.previous_state.read_bytes() == b"old\n"
    assert [event.destination for event in replace.events] == [
        layout.previous_state,
        layout.state,
    ]


def test_directory_sync_failure_reports_failure_even_after_state_replace(
    tmp_path: Path,
) -> None:
    sync = FakeFileSync()
    sync.fail_next("sync_directory", OSError("root sync failed"))
    layout, writer, _, _ = _writer(tmp_path, file_sync=sync)
    layout.state.write_bytes(b"old\n")

    with pytest.raises(OSError, match="root sync failed"):
        writer.write(b"new\n")

    # The repository must reload this disk truth after the failed durability
    # boundary instead of retaining its pre-call in-memory snapshot.
    assert layout.state.read_bytes() == b"new\n"
    assert layout.previous_state.read_bytes() == b"old\n"


def test_existing_state_symlink_is_rejected_without_changing_its_target(
    tmp_path: Path,
) -> None:
    layout, writer, _, replace = _writer(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-state"
    outside.write_bytes(b"outside\n")
    symlink_or_skip(layout.state, outside)
    try:
        with pytest.raises(ValueError, match="ordinary file"):
            writer.write(b"new\n")
        assert outside.read_bytes() == b"outside\n"
        assert replace.events == []
    finally:
        outside.unlink(missing_ok=True)


def test_previous_state_temp_sync_failure_stops_before_either_replace(
    tmp_path: Path,
) -> None:
    sync = FakeFileSync()
    sync.fail_on("sync_file", 2, OSError("prev temp sync failed"))
    layout, writer, _, replace = _writer(tmp_path, file_sync=sync)
    layout.state.write_bytes(b"old\n")

    with pytest.raises(OSError, match="prev temp sync failed"):
        writer.write(b"new\n")

    assert layout.state.read_bytes() == b"old\n"
    assert not layout.previous_state.exists()
    assert replace.events == []
    assert len(tuple(layout.state_temporary.iterdir())) == 2


def test_final_byte_verification_failure_is_reported_after_replace(
    tmp_path: Path,
) -> None:
    layout = StorageLayout.at(tmp_path)
    layout.ensure_directories()
    calls = 0

    def mismatching_final_read(path: Path) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return read_stable_file_bytes(path)
        return b"different\n"

    layout.state.write_bytes(b"old\n")
    writer = AtomicStateFileWriter(
        layout,
        FakeFileSync(),
        FaultInjectingReplace(),
        DeterministicIdGenerator(seed="state-final-verification"),
        read_file=mismatching_final_read,
    )

    with pytest.raises(OSError, match="differ"):
        writer.write(b"new\n")

    assert calls == 2
    assert layout.state.read_bytes() == b"new\n"
    assert layout.previous_state.read_bytes() == b"old\n"
