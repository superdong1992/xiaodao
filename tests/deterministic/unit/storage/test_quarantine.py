from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest

import problem_locator.storage.quarantine as quarantine_module
from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.layout import StorageLayout
from problem_locator.storage.quarantine import QuarantineMover
from tests.deterministic.unit.storage.fakes import FakeFileSync, FaultInjectingReplace


CASE_ID = "00000000-0000-0000-0000-000000000101"
RESOURCE_ID = "00000000-0000-0000-0000-000000000102"
CLEANUP_ID = "00000000-0000-0000-0000-000000000103"


def _mover(
    tmp_path: Path,
    *,
    replacer: FaultInjectingReplace | None = None,
) -> tuple[
    StorageLayout,
    StorageCoordinationLock,
    FakeFileSync,
    FaultInjectingReplace,
    QuarantineMover,
]:
    layout = StorageLayout.at(tmp_path)
    layout.ensure_directories()
    lock = StorageCoordinationLock()
    sync = FakeFileSync()
    replace = replacer or FaultInjectingReplace()
    return layout, lock, sync, replace, QuarantineMover(layout, lock, sync, replace)


def _formal_payload(layout: StorageLayout) -> Path:
    return (
        layout.cases_resources
        / CASE_ID
        / "attachments"
        / RESOURCE_ID
        / "payload"
    )


def test_candidate_is_revalidated_and_moved_while_shared_lock_is_held(
    tmp_path: Path,
) -> None:
    layout, lock, sync, replace, mover = _mover(tmp_path)
    candidate = _formal_payload(layout)
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"orphan")
    revalidated: list[bool] = []

    def revalidate() -> bool:
        revalidated.append(lock.held_by_current_thread())
        return True

    isolated = mover.move_if(CLEANUP_ID, candidate, revalidate)

    assert revalidated == [True]
    assert isolated == layout.quarantine / CLEANUP_ID / candidate.relative_to(tmp_path)
    assert isolated.read_bytes() == b"orphan"
    assert not candidate.exists()
    assert replace.events[-1].source == candidate
    assert replace.events[-1].destination == isolated
    assert [event.path for event in sync.calls("sync_directory")][-2:] == [
        candidate.parent,
        isolated.parent,
    ]

    mover.delete(isolated)
    assert not isolated.exists()
    assert isolated.parent in {
        event.path for event in sync.calls("sync_directory")
    }


def test_failed_revalidation_has_no_quarantine_side_effect(
    tmp_path: Path,
) -> None:
    layout, _, sync, replace, mover = _mover(tmp_path)
    candidate = layout.uploads / RESOURCE_ID
    candidate.mkdir()
    (candidate / "payload").write_bytes(b"upload")

    assert mover.move_if(CLEANUP_ID, candidate, lambda: False) is None

    assert candidate.is_dir()
    assert replace.events == []
    assert sync.events == []
    assert not (layout.quarantine / CLEANUP_ID).exists()


def test_replace_failure_never_deletes_or_partially_moves_candidate(
    tmp_path: Path,
) -> None:
    replace = FaultInjectingReplace()
    replace.fail_next(OSError("quarantine replace failed"))
    layout, _, _, _, mover = _mover(tmp_path, replacer=replace)
    candidate = layout.workspaces / RESOURCE_ID
    candidate.mkdir()

    with pytest.raises(OSError, match="quarantine replace failed"):
        mover.move_if(CLEANUP_ID, candidate, lambda: True)

    assert candidate.is_dir()
    assert not (
        layout.quarantine / CLEANUP_ID / candidate.relative_to(tmp_path)
    ).exists()


@pytest.mark.parametrize(
    "relative",
    [
        "state.json",
        "resources/cases",
        f"tmp/quarantine/{CLEANUP_ID}/victim",
        f"tmp/uploads/{RESOURCE_ID}/nested",
        f"jobs/{RESOURCE_ID}/job.json",
    ],
)
def test_only_exact_s02_cleanup_candidate_shapes_are_accepted(
    tmp_path: Path,
    relative: str,
) -> None:
    layout, _, _, _, mover = _mover(tmp_path)
    candidate = layout.data_root / relative

    with pytest.raises(ValueError, match="exact S02 cleanup candidate"):
        mover.move_if(CLEANUP_ID, candidate, lambda: True)


def test_candidate_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    layout, _, _, _, mover = _mover(tmp_path)
    target = tmp_path / "outside"
    target.write_bytes(b"keep")
    candidate = layout.uploads / RESOURCE_ID
    candidate.symlink_to(target)

    with pytest.raises(ValueError, match="symbolic links"):
        mover.move_if(CLEANUP_ID, candidate, lambda: True)

    assert target.read_bytes() == b"keep"


def test_delete_rejects_quarantine_symlink_ancestor_without_touching_sentinel(
    tmp_path: Path,
) -> None:
    layout, _, _, _, mover = _mover(tmp_path)
    outside = tmp_path / "outside-quarantine"
    external_target = outside / "tmp" / "uploads" / RESOURCE_ID
    external_target.mkdir(parents=True)
    sentinel = external_target / "sentinel"
    sentinel.write_bytes(b"keep")
    (layout.quarantine / CLEANUP_ID).symlink_to(outside, target_is_directory=True)

    apparent_target = (
        layout.quarantine / CLEANUP_ID / "tmp" / "uploads" / RESOURCE_ID
    )
    with pytest.raises(ValueError, match="symbolic links"):
        mover.delete(apparent_target)

    assert sentinel.read_bytes() == b"keep"


def test_read_only_tree_is_deleted_only_after_lock_release(tmp_path: Path) -> None:
    layout, lock, _, _, mover = _mover(tmp_path)
    tree = (
        layout.cases_resources
        / CASE_ID
        / "artifacts"
        / RESOURCE_ID
        / "tree"
    )
    nested = tree / "nested"
    nested.mkdir(parents=True)
    payload = nested / "result.json"
    payload.write_bytes(b"{}\n")
    os.chmod(payload, 0o444)
    os.chmod(nested, 0o555)
    os.chmod(tree, 0o555)
    isolated = mover.move_if(CLEANUP_ID, tree, lambda: True)
    assert isolated is not None

    with lock, pytest.raises(RuntimeError, match="outside"):
        mover.delete(isolated)
    assert isolated.exists()

    mover.delete(isolated)
    assert not isolated.exists()


def test_publication_owner_excludes_cleanup_revalidation_until_release(
    tmp_path: Path,
) -> None:
    layout, lock, _, _, mover = _mover(tmp_path)
    candidate = layout.jobs / RESOURCE_ID
    candidate.mkdir()
    lock.acquire()
    entered = threading.Event()
    finished = threading.Event()

    def clean() -> None:
        mover.move_if(CLEANUP_ID, candidate, lambda: entered.set() or True)
        finished.set()

    worker = threading.Thread(target=clean, daemon=True)
    worker.start()
    try:
        time.sleep(0.02)
        assert not entered.is_set()
        assert not finished.is_set()
    finally:
        lock.release()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert entered.is_set() and finished.is_set()
    assert not candidate.exists()


def test_new_mover_discovers_leftover_candidates_and_prunes_empty_run(
    tmp_path: Path,
) -> None:
    layout, lock, sync, replace, mover = _mover(tmp_path)
    upload = layout.uploads / RESOURCE_ID
    upload.mkdir()
    job = layout.jobs / CASE_ID
    job.mkdir()
    isolated_upload = mover.move_if(CLEANUP_ID, upload, lambda: True)
    isolated_job = mover.move_if(CLEANUP_ID, job, lambda: True)
    assert isolated_upload is not None and isolated_job is not None

    restarted = QuarantineMover(layout, lock, sync, replace)
    assert restarted.discover() == tuple(sorted((isolated_job, isolated_upload)))

    restarted.delete(isolated_upload)
    assert restarted.discover() == (isolated_job,)
    restarted.delete(isolated_job)
    assert restarted.discover() == ()
    assert tuple(layout.quarantine.iterdir()) == ()


def test_failed_quarantine_delete_remains_discoverable_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, _, _, _, mover = _mover(tmp_path)
    candidate = layout.workspaces / RESOURCE_ID
    candidate.mkdir()
    (candidate / "partial").write_bytes(b"workspace")
    isolated = mover.move_if(CLEANUP_ID, candidate, lambda: True)
    assert isolated is not None
    real_rmtree = quarantine_module.shutil.rmtree

    def fail_delete(*args: object, **kwargs: object) -> None:
        raise OSError("injected recursive delete failure")

    monkeypatch.setattr(quarantine_module.shutil, "rmtree", fail_delete)
    with pytest.raises(OSError, match="recursive delete"):
        mover.delete(isolated)
    assert mover.discover() == (isolated,)

    monkeypatch.setattr(quarantine_module.shutil, "rmtree", real_rmtree)
    mover.delete(isolated)
    assert mover.discover() == ()


def test_delete_parent_sync_failure_is_idempotently_completed_by_retry(
    tmp_path: Path,
) -> None:
    layout, _, sync, _, mover = _mover(tmp_path)
    candidate = layout.state_temporary / "old-state.tmp"
    candidate.write_bytes(b"old")
    isolated = mover.move_if(CLEANUP_ID, candidate, lambda: True)
    assert isolated is not None
    sync.fail_next("sync_directory", OSError("delete parent sync failed"))

    with pytest.raises(OSError, match="delete parent sync"):
        mover.delete(isolated)

    assert not isolated.exists()
    mover.delete(isolated)
    assert mover.discover() == ()


def test_directory_creation_sync_failure_is_reapplied_before_move_retry(
    tmp_path: Path,
) -> None:
    layout, _, sync, replace, mover = _mover(tmp_path)
    candidate = layout.state_temporary / "old-state.tmp"
    candidate.write_bytes(b"old")
    sync.fail_next("sync_directory", OSError("new directory sync failed"))

    with pytest.raises(OSError, match="new directory sync"):
        mover.move_if(CLEANUP_ID, candidate, lambda: True)

    assert candidate.exists()
    assert replace.call_count == 0
    isolated = mover.move_if(CLEANUP_ID, candidate, lambda: True)
    assert isolated is not None and isolated.read_bytes() == b"old"


@pytest.mark.parametrize("sync_occurrence", [4, 5])
def test_post_move_directory_sync_failure_is_recovered_by_discovery(
    tmp_path: Path,
    sync_occurrence: int,
) -> None:
    layout = StorageLayout.at(tmp_path)
    layout.ensure_directories()
    lock = StorageCoordinationLock()
    sync = FakeFileSync()
    replace = FaultInjectingReplace()
    mover = QuarantineMover(layout, lock, sync, replace)
    candidate = layout.state_temporary / "old-state.tmp"
    candidate.write_bytes(b"old")
    destination = (
        layout.quarantine
        / CLEANUP_ID
        / candidate.relative_to(layout.data_root)
    )
    destination.parent.mkdir(parents=True)
    sync.fail_on(
        "sync_directory",
        sync_occurrence,
        OSError(f"directory sync {sync_occurrence} failed"),
    )

    with pytest.raises(OSError, match="directory sync"):
        mover.move_if(CLEANUP_ID, candidate, lambda: True)

    assert not candidate.exists()
    assert destination.read_bytes() == b"old"
    assert mover.discover() == (destination,)

    assert mover.move_if(CLEANUP_ID, candidate, lambda: False) == destination
    assert mover.discover() == (destination,)
