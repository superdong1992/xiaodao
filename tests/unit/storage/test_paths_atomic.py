from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from problem_locator.contracts.enums import ResourceKind
from problem_locator.contracts.models import ResourceRef
from problem_locator.storage.atomic import (
    atomic_write_bytes,
    require_ordinary_file,
    require_real_directory,
    write_synced_file,
)
from problem_locator.storage.paths import (
    ensure_no_symlink_ancestors,
    ensure_within,
    formal_storage_key,
    parse_storage_key,
    proposal_directory_name,
    resource_path,
    validate_data_root,
)
from tests.unit.storage.fakes import FakeFileSync, FaultInjectingReplace


CASE_ID = "00000000-0000-0000-0000-000000000001"
RESOURCE_ID = "00000000-0000-0000-0000-000000000002"
ZERO_HASH = hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/resources/cases/x/evidence/y/payload",
        "resources\\cases\\x\\evidence\\y\\payload",
        f"resources/cases/{CASE_ID}/evidence/../payload",
        f"resources/cases/{CASE_ID}//{RESOURCE_ID}/payload",
        f"resources/cases/{CASE_ID}/unknown/{RESOURCE_ID}/payload",
        f"resources/cases/{CASE_ID}/attachments/{RESOURCE_ID}/tree",
        f"resources/cases/{CASE_ID}/evidence/{RESOURCE_ID}/tree",
        f"resources/cases/{CASE_ID}/artifacts/{RESOURCE_ID}/other",
        f"other/cases/{CASE_ID}/evidence/{RESOURCE_ID}/payload",
        f"resources/cases/not-a-uuid/evidence/{RESOURCE_ID}/payload",
    ],
)
def test_parse_storage_key_rejects_non_frozen_or_unsafe_shapes(key: str) -> None:
    with pytest.raises(ValueError):
        parse_storage_key(key)


def test_formal_storage_key_round_trips_all_legal_shapes() -> None:
    expected = {
        ("attachments", ResourceKind.FILE): "payload",
        ("evidence", ResourceKind.FILE): "payload",
        ("artifacts", ResourceKind.FILE): "payload",
        ("artifacts", ResourceKind.DIRECTORY): "tree",
    }

    for (category, kind), leaf in expected.items():
        key = formal_storage_key(CASE_ID, category, RESOURCE_ID, kind)
        assert key == f"resources/cases/{CASE_ID}/{category}/{RESOURCE_ID}/{leaf}"
        address = parse_storage_key(key)
        assert address.case_id == CASE_ID
        assert address.category == category
        assert address.resource_id == RESOURCE_ID
        assert address.resource_kind is kind

    with pytest.raises(ValueError, match="only artifacts"):
        formal_storage_key(CASE_ID, "attachments", RESOURCE_ID, ResourceKind.DIRECTORY)


def test_absolute_root_and_containment_checks_reject_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        validate_data_root(Path("relative/root"))

    root = tmp_path / "root"
    root.mkdir()
    assert ensure_within(root, root / "inside") == root / "inside"
    with pytest.raises(ValueError, match="escapes"):
        ensure_within(root, root / ".." / "outside")


def test_symlink_ancestor_and_final_symlink_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        ensure_no_symlink_ancestors(root, root / "linked" / "payload")

    real_file = root / "real"
    real_file.write_bytes(b"data")
    final_link = root / "final"
    final_link.symlink_to(real_file)
    with pytest.raises(ValueError, match="symbolic"):
        ensure_no_symlink_ancestors(root, final_link)


def test_resource_path_rejects_kind_mismatch_and_symlinked_case_root(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    resources = data_root / "resources"
    cases = resources / "cases"
    cases.mkdir(parents=True)
    file_key = formal_storage_key(CASE_ID, "artifacts", RESOURCE_ID, ResourceKind.FILE)
    wrong_kind = ResourceRef(
        resource_kind=ResourceKind.DIRECTORY,
        storage_key=file_key,
        size=0,
        sha256=ZERO_HASH,
    )
    with pytest.raises(ValueError, match="kind"):
        resource_path(data_root, wrong_kind)

    outside = tmp_path / "outside-case"
    outside.mkdir()
    (cases / CASE_ID).symlink_to(outside, target_is_directory=True)
    valid_ref = ResourceRef(
        resource_kind=ResourceKind.FILE,
        storage_key=formal_storage_key(CASE_ID, "evidence", RESOURCE_ID, ResourceKind.FILE),
        size=0,
        sha256=ZERO_HASH,
    )
    with pytest.raises(ValueError):
        resource_path(data_root, valid_ref)


def test_proposal_directory_name_is_stable_safe_and_non_aliasing() -> None:
    first = proposal_directory_name("../../same? proposal")
    assert first == proposal_directory_name("../../same? proposal")
    assert first.startswith("p-")
    assert "/" not in first and "\\" not in first and ".." not in first
    assert first != proposal_directory_name("another")
    with pytest.raises(ValueError):
        proposal_directory_name("   ")


def test_atomic_write_requires_temporary_file_in_destination_directory(tmp_path: Path) -> None:
    destination = tmp_path / "final" / "state.json"
    temporary = tmp_path / "tmp" / "state.tmp"

    with pytest.raises(ValueError, match="share"):
        atomic_write_bytes(
            destination,
            b"new",
            temporary_path=temporary,
            file_sync=FakeFileSync(),
            replacer=FaultInjectingReplace(),
        )
    assert not temporary.exists()
    assert not destination.exists()


def test_atomic_write_syncs_file_replaces_then_syncs_parent(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"
    destination.write_bytes(b"old")
    temporary = tmp_path / "state.tmp"
    sync = FakeFileSync()
    replacer = FaultInjectingReplace()

    atomic_write_bytes(
        destination,
        b"new",
        temporary_path=temporary,
        file_sync=sync,
        replacer=replacer,
    )

    assert destination.read_bytes() == b"new"
    assert not temporary.exists()
    assert [(event.source, event.destination) for event in replacer.events] == [
        (temporary, destination)
    ]
    assert [event.operation for event in sync.events] == ["sync_file", "sync_directory"]
    assert sync.events[0].path == temporary
    assert sync.events[1].path == tmp_path


def test_atomic_write_file_sync_failure_keeps_temp_and_skips_replace(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"
    temporary = tmp_path / "state.tmp"
    sync = FakeFileSync()
    sync.fail_next("sync_file", OSError("fsync failed"))
    replacer = FaultInjectingReplace()

    with pytest.raises(OSError, match="fsync failed"):
        atomic_write_bytes(
            destination,
            b"new",
            temporary_path=temporary,
            file_sync=sync,
            replacer=replacer,
        )

    assert temporary.read_bytes() == b"new"
    assert not destination.exists()
    assert replacer.call_count == 0
    assert sync.count("sync_directory") == 0


def test_atomic_write_replace_failure_keeps_synced_temp_and_old_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "state.json"
    destination.write_bytes(b"old")
    temporary = tmp_path / "state.tmp"
    sync = FakeFileSync()
    replacer = FaultInjectingReplace()
    replacer.fail_next(OSError("replace failed"))

    with pytest.raises(OSError, match="replace failed"):
        atomic_write_bytes(
            destination,
            b"new",
            temporary_path=temporary,
            file_sync=sync,
            replacer=replacer,
        )

    assert destination.read_bytes() == b"old"
    assert temporary.read_bytes() == b"new"
    assert sync.count("sync_file") == 1
    assert sync.count("sync_directory") == 0


def test_atomic_write_directory_sync_failure_occurs_after_replace(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"
    temporary = tmp_path / "state.tmp"
    sync = FakeFileSync()
    sync.fail_next("sync_directory", OSError("directory sync failed"))

    with pytest.raises(OSError, match="directory sync failed"):
        atomic_write_bytes(
            destination,
            b"new",
            temporary_path=temporary,
            file_sync=sync,
            replacer=FaultInjectingReplace(),
        )

    assert destination.read_bytes() == b"new"
    assert not temporary.exists()


def test_write_synced_file_is_create_only_and_node_guards_reject_links(
    tmp_path: Path,
) -> None:
    path = tmp_path / "value"
    sync = FakeFileSync()
    write_synced_file(path, b"first", sync)
    with pytest.raises(FileExistsError):
        write_synced_file(path, b"second", sync)
    assert require_ordinary_file(path).st_size == 5

    directory = tmp_path / "directory"
    directory.mkdir()
    assert require_real_directory(directory)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="ordinary"):
        require_ordinary_file(link)
    with pytest.raises(ValueError, match="directory"):
        require_real_directory(link)
