from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from problem_locator.contracts.limits import (
    ORPHAN_RESOURCE_RETENTION_SECONDS,
    PROPOSAL_STAGING_RETENTION_SECONDS,
    UPLOAD_TEMP_RETENTION_SECONDS,
    WORKSPACE_RETENTION_SECONDS,
)
from problem_locator.storage.layout import StorageLayout
from problem_locator.storage.paths import proposal_stage_path
from problem_locator.storage.retention import RetentionScanner
from problem_locator.storage.streams import hash_file
from problem_locator.storage.tree import inspect_tree
from tests.deterministic.unit.storage.fakes import FixedClock


NOW = "2026-01-10T12:00:00.000Z"
NOW_SECONDS = datetime.fromisoformat(NOW.replace("Z", "+00:00")).timestamp()

CASE_ID = "00000000-0000-0000-0000-000000000101"
OTHER_CASE_ID = "00000000-0000-0000-0000-000000000102"

UPLOAD_OLD_ID = "00000000-0000-0000-0000-000000000201"
UPLOAD_EXACT_ID = "00000000-0000-0000-0000-000000000202"
UPLOAD_FUTURE_ID = "00000000-0000-0000-0000-000000000203"

PROPOSAL_OLD_JOB_ID = "00000000-0000-0000-0000-000000000301"
PROPOSAL_EXACT_JOB_ID = "00000000-0000-0000-0000-000000000302"

WORKSPACE_OLD_ID = "00000000-0000-0000-0000-000000000401"
WORKSPACE_EXACT_ID = "00000000-0000-0000-0000-000000000402"
WORKSPACE_FUTURE_ID = "00000000-0000-0000-0000-000000000403"

RESOURCE_OLD_ID = "00000000-0000-0000-0000-000000000501"
RESOURCE_SAME_HASH_ID = "00000000-0000-0000-0000-000000000502"
RESOURCE_EXACT_ID = "00000000-0000-0000-0000-000000000503"
RESOURCE_FUTURE_ID = "00000000-0000-0000-0000-000000000504"
RESOURCE_TREE_ID = "00000000-0000-0000-0000-000000000505"

JOB_OLD_ID = "00000000-0000-0000-0000-000000000601"
JOB_EXACT_ID = "00000000-0000-0000-0000-000000000602"
JOB_FUTURE_ID = "00000000-0000-0000-0000-000000000603"


def _layout(tmp_path: Path) -> StorageLayout:
    layout = StorageLayout.at(tmp_path / "data")
    layout.ensure_directories()
    return layout


def _set_age(path: Path, age_seconds: int) -> None:
    timestamp_ns = int((NOW_SECONDS - age_seconds) * 1_000_000_000)
    os.utime(path, ns=(timestamp_ns, timestamp_ns), follow_symlinks=False)


def _candidate_pairs(scanner: RetentionScanner) -> set[tuple[str, Path]]:
    return {(candidate.kind, candidate.path) for candidate in scanner.discover()}


def _staged_directory(parent: Path, identifier: str) -> tuple[Path, Path]:
    directory = parent / identifier
    directory.mkdir(parents=True)
    (directory / "payload").write_bytes(b"staged")
    marker = directory / "staged.json"
    marker.write_bytes(b"{}\n")
    return directory, marker


def _formal_file(
    layout: StorageLayout,
    resource_id: str,
    payload: bytes,
    *,
    case_id: str = CASE_ID,
    category: str = "evidence",
) -> Path:
    path = (
        layout.cases_resources
        / case_id
        / category
        / resource_id
        / "payload"
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    return path


def test_marker_mtime_is_the_anchor_and_clock_is_read_once_per_round(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    old_upload, old_upload_marker = _staged_directory(
        layout.uploads,
        UPLOAD_OLD_ID,
    )
    exact_upload, exact_upload_marker = _staged_directory(
        layout.uploads,
        UPLOAD_EXACT_ID,
    )
    future_upload, future_upload_marker = _staged_directory(
        layout.uploads,
        UPLOAD_FUTURE_ID,
    )

    old_proposal = proposal_stage_path(
        layout.data_root,
        PROPOSAL_OLD_JOB_ID,
        "old-proposal",
    )
    old_proposal.mkdir(parents=True)
    (old_proposal / "payload").write_bytes(b"proposal")
    old_proposal_marker = old_proposal / "staged.json"
    old_proposal_marker.write_bytes(b"{}\n")

    exact_proposal = proposal_stage_path(
        layout.data_root,
        PROPOSAL_EXACT_JOB_ID,
        "exact-proposal",
    )
    exact_proposal.mkdir(parents=True)
    (exact_proposal / "payload").write_bytes(b"proposal")
    exact_proposal_marker = exact_proposal / "staged.json"
    exact_proposal_marker.write_bytes(b"{}\n")

    # Deliberately oppose directory and marker ages so selecting the right
    # clock anchor is observable. Equality is not expiration.
    _set_age(old_upload, -1)
    _set_age(old_upload_marker, UPLOAD_TEMP_RETENTION_SECONDS + 1)
    _set_age(exact_upload, UPLOAD_TEMP_RETENTION_SECONDS + 1)
    _set_age(exact_upload_marker, UPLOAD_TEMP_RETENTION_SECONDS)
    _set_age(future_upload, UPLOAD_TEMP_RETENTION_SECONDS + 1)
    _set_age(future_upload_marker, -1)

    _set_age(old_proposal, -1)
    _set_age(old_proposal_marker, PROPOSAL_STAGING_RETENTION_SECONDS + 1)
    _set_age(exact_proposal, PROPOSAL_STAGING_RETENTION_SECONDS + 1)
    _set_age(exact_proposal_marker, PROPOSAL_STAGING_RETENTION_SECONDS)

    clock = FixedClock(
        "2000-01-01T00:00:00.000Z",
        scripted_values=(NOW, "2099-01-01T00:00:00.000Z"),
    )
    scanner = RetentionScanner(layout, clock)

    assert _candidate_pairs(scanner) == {
        ("UPLOAD", old_upload),
        ("PROPOSAL", old_proposal),
    }
    assert clock.calls == 1


def test_all_physical_candidate_kinds_use_strict_thresholds_and_exact_paths(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)

    old_workspace = layout.workspaces / WORKSPACE_OLD_ID
    exact_workspace = layout.workspaces / WORKSPACE_EXACT_ID
    future_workspace = layout.workspaces / WORKSPACE_FUTURE_ID
    for path in (old_workspace, exact_workspace, future_workspace):
        path.mkdir()
    _set_age(old_workspace, WORKSPACE_RETENTION_SECONDS + 1)
    _set_age(exact_workspace, WORKSPACE_RETENTION_SECONDS)
    _set_age(future_workspace, -1)

    old_state_temp = layout.state_temporary / "old.tmp"
    exact_state_temp = layout.state_temporary / "exact.tmp"
    future_state_temp = layout.state_temporary / "future.tmp"
    for path in (old_state_temp, exact_state_temp, future_state_temp):
        path.write_bytes(b"temporary state")
    _set_age(old_state_temp, UPLOAD_TEMP_RETENTION_SECONDS + 1)
    _set_age(exact_state_temp, UPLOAD_TEMP_RETENTION_SECONDS)
    _set_age(future_state_temp, -1)

    shared_payload = b"same bytes under distinct formal keys"
    old_resource = _formal_file(layout, RESOURCE_OLD_ID, shared_payload)
    same_hash_resource = _formal_file(
        layout,
        RESOURCE_SAME_HASH_ID,
        shared_payload,
        category="artifacts",
    )
    exact_resource = _formal_file(layout, RESOURCE_EXACT_ID, b"exact")
    future_resource = _formal_file(layout, RESOURCE_FUTURE_ID, b"future")
    tree_resource = (
        layout.cases_resources
        / OTHER_CASE_ID
        / "artifacts"
        / RESOURCE_TREE_ID
        / "tree"
    )
    tree_resource.mkdir(parents=True)
    (tree_resource / "result.json").write_bytes(b"{}\n")
    inspect_tree(tree_resource)
    _set_age(old_resource.parent, ORPHAN_RESOURCE_RETENTION_SECONDS + 1)
    _set_age(
        same_hash_resource.parent,
        ORPHAN_RESOURCE_RETENTION_SECONDS + 1,
    )
    _set_age(exact_resource.parent, ORPHAN_RESOURCE_RETENTION_SECONDS)
    _set_age(future_resource.parent, -1)
    _set_age(tree_resource.parent, ORPHAN_RESOURCE_RETENTION_SECONDS + 1)

    old_job = layout.jobs / JOB_OLD_ID
    exact_job = layout.jobs / JOB_EXACT_ID
    future_job = layout.jobs / JOB_FUTURE_ID
    for path in (old_job, exact_job, future_job):
        path.mkdir()
        (path / "job.json").write_bytes(b"{}\n")
    _set_age(old_job, ORPHAN_RESOURCE_RETENTION_SECONDS + 1)
    _set_age(exact_job, ORPHAN_RESOURCE_RETENTION_SECONDS)
    _set_age(future_job, -1)

    # These old nodes are outside every scanner root and must never leak into
    # the physical candidate set.
    ignored_tmp = layout.temporary / "loose.tmp"
    ignored_tmp.write_bytes(b"ignored")
    ignored_quarantine = layout.quarantine / JOB_OLD_ID
    ignored_quarantine.mkdir()
    (ignored_quarantine / "payload").write_bytes(b"ignored")
    _set_age(ignored_tmp, ORPHAN_RESOURCE_RETENTION_SECONDS + 1)
    _set_age(ignored_quarantine, ORPHAN_RESOURCE_RETENTION_SECONDS + 1)

    candidates = _candidate_pairs(RetentionScanner(layout, FixedClock(NOW)))

    assert candidates == {
        ("WORKSPACE", old_workspace),
        ("STATE_TEMP", old_state_temp),
        ("FORMAL_RESOURCE", old_resource),
        ("FORMAL_RESOURCE", same_hash_resource),
        ("FORMAL_RESOURCE", tree_resource),
        ("JOB", old_job),
    }
    assert ("FORMAL_RESOURCE", old_resource) in candidates
    assert ("FORMAL_RESOURCE", same_hash_resource) in candidates
    assert hash_file(old_resource).sha256 == hash_file(same_hash_resource).sha256
    assert all(path not in {ignored_tmp, ignored_quarantine} for _, path in candidates)


@pytest.mark.parametrize(
    "invalid_shape",
    [
        "upload_id",
        "proposal_job_id",
        "proposal_directory",
        "workspace_id",
        "formal_case_id",
        "formal_resource_id",
        "job_id",
    ],
)
def test_invalid_identifiers_and_managed_directory_names_are_rejected(
    tmp_path: Path,
    invalid_shape: str,
) -> None:
    layout = _layout(tmp_path)
    if invalid_shape == "upload_id":
        (layout.uploads / "not-an-opaque-id").mkdir()
    elif invalid_shape == "proposal_job_id":
        (layout.proposals / "not-an-opaque-id").mkdir()
    elif invalid_shape == "proposal_directory":
        invalid = layout.proposals / PROPOSAL_OLD_JOB_ID / "not-a-proposal-hash"
        invalid.mkdir(parents=True)
    elif invalid_shape == "workspace_id":
        (layout.workspaces / "not-an-opaque-id").mkdir()
    elif invalid_shape == "formal_case_id":
        (layout.cases_resources / "not-an-opaque-id").mkdir()
    elif invalid_shape == "formal_resource_id":
        invalid = layout.cases_resources / CASE_ID / "evidence" / "not-an-id"
        invalid.mkdir(parents=True)
        (invalid / "payload").write_bytes(b"invalid")
    elif invalid_shape == "job_id":
        (layout.jobs / "not-an-opaque-id").mkdir()
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(invalid_shape)

    with pytest.raises(ValueError):
        RetentionScanner(layout, FixedClock(NOW)).discover()


@pytest.mark.parametrize(
    "invalid_node",
    [
        "upload_symlink",
        "upload_regular_file",
        "upload_marker_symlink",
        "proposal_symlink",
        "proposal_marker_fifo",
        "workspace_fifo",
        "state_temp_directory",
        "state_temp_symlink",
        "formal_leaf_symlink",
        "job_regular_file",
    ],
)
def test_links_and_nonordinary_managed_nodes_are_rejected(
    tmp_path: Path,
    invalid_node: str,
) -> None:
    layout = _layout(tmp_path)
    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    outside_file = tmp_path / "outside-file"
    outside_file.write_bytes(b"outside")

    if invalid_node == "upload_symlink":
        (layout.uploads / UPLOAD_OLD_ID).symlink_to(
            outside_directory,
            target_is_directory=True,
        )
    elif invalid_node == "upload_regular_file":
        (layout.uploads / UPLOAD_OLD_ID).write_bytes(b"not a directory")
    elif invalid_node == "upload_marker_symlink":
        upload = layout.uploads / UPLOAD_OLD_ID
        upload.mkdir()
        (upload / "staged.json").symlink_to(outside_file)
    elif invalid_node == "proposal_symlink":
        (layout.proposals / PROPOSAL_OLD_JOB_ID).symlink_to(
            outside_directory,
            target_is_directory=True,
        )
    elif invalid_node == "proposal_marker_fifo":
        proposal = proposal_stage_path(
            layout.data_root,
            PROPOSAL_OLD_JOB_ID,
            "invalid-marker",
        )
        proposal.mkdir(parents=True)
        os.mkfifo(proposal / "staged.json")
    elif invalid_node == "workspace_fifo":
        os.mkfifo(layout.workspaces / WORKSPACE_OLD_ID)
    elif invalid_node == "state_temp_directory":
        (layout.state_temporary / "state.tmp").mkdir()
    elif invalid_node == "state_temp_symlink":
        (layout.state_temporary / "state.tmp").symlink_to(outside_file)
    elif invalid_node == "formal_leaf_symlink":
        leaf = (
            layout.cases_resources
            / CASE_ID
            / "evidence"
            / RESOURCE_OLD_ID
            / "payload"
        )
        leaf.parent.mkdir(parents=True)
        leaf.symlink_to(outside_file)
    elif invalid_node == "job_regular_file":
        (layout.jobs / JOB_OLD_ID).write_bytes(b"not a directory")
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(invalid_node)

    with pytest.raises(ValueError):
        RetentionScanner(layout, FixedClock(NOW)).discover()
