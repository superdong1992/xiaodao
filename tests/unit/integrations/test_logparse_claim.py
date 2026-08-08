"""Atomicity and filesystem-safety tests for the one-shot parse claim."""

from __future__ import annotations

import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from problem_locator.contracts import LogparseParseClaim, canonical_json_bytes
from problem_locator.integrations.logparse.claim import create_parse_claim


CONTRACT_FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "contracts"
    / "positive"
    / "logparse-parse-claim.json"
)
CLAIM_RELATIVE_PATH = Path("runtime/tool-state/logparse-parse.claim")


def _assert_claim_creation_mode(metadata: os.stat_result) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    if os.name == "nt":
        # The Windows CRT maps the DOS read-only attribute onto every rw class;
        # per-principal access is enforced by the inherited workspace ACL, not
        # represented by st_mode.  A newly-created writable file is therefore
        # reported as 0666 even though os.open received 0600.
        assert mode == 0o666
    else:
        assert mode == 0o600


def _symlink_or_skip(
    link: Path,
    target: Path,
    *,
    target_is_directory: bool = False,
) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except NotImplementedError as exc:
        pytest.skip(f"symbolic link creation is unavailable: {exc}")
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("symbolic link creation requires Windows developer mode")
        raise


def _claim(**updates: Any) -> LogparseParseClaim:
    payload = json.loads(CONTRACT_FIXTURE.read_text(encoding="utf-8"))
    payload.update(updates)
    return LogparseParseClaim.model_validate(payload)


def test_create_parse_claim_writes_the_exact_s00_bytes_at_the_reserved_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    claim = _claim()

    target = create_parse_claim(workspace, claim)

    assert target == workspace / CLAIM_RELATIVE_PATH
    assert target.read_bytes() == canonical_json_bytes(claim)
    metadata = target.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_nlink == 1
    _assert_claim_creation_mode(metadata)
    assert (workspace / "runtime").is_dir()
    assert (workspace / "runtime/tool-state").is_dir()


@pytest.mark.parametrize(
    "updates",
    [
        {"request_sha256": "1" * 64},
        {"artifact_proposal_key": "different-proposal"},
        {
            "attachment_id": "00000000-0000-0000-0000-000000000099",
            "attachment_sha256": "3" * 64,
        },
    ],
    ids=["different-request", "different-proposal", "different-attachment"],
)
def test_create_parse_claim_rejects_every_second_parse_without_touching_the_first(
    tmp_path: Path,
    updates: dict[str, Any],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_claim = _claim()
    target = create_parse_claim(workspace, first_claim)
    expected = canonical_json_bytes(first_claim)
    before = target.lstat()

    with pytest.raises(FileExistsError, match="already exists"):
        create_parse_claim(workspace, _claim(**updates))

    after = target.lstat()
    assert target.read_bytes() == expected
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_size == before.st_size
    _assert_claim_creation_mode(after)


@pytest.mark.parametrize("node_kind", ["file", "directory", "symlink"])
def test_create_parse_claim_rejects_every_preexisting_target_node(
    tmp_path: Path,
    node_kind: str,
) -> None:
    workspace = tmp_path / "workspace"
    state_root = workspace / "runtime/tool-state"
    state_root.mkdir(parents=True)
    target = state_root / "logparse-parse.claim"
    sentinel = b"preexisting node must survive"
    external = tmp_path / "external-claim"

    if node_kind == "file":
        target.write_bytes(sentinel)
    elif node_kind == "directory":
        target.mkdir()
    else:
        external.write_bytes(sentinel)
        _symlink_or_skip(target, external)

    expected_error = (
        ValueError if os.name == "nt" and node_kind == "directory" else FileExistsError
    )
    expected_message = (
        "cannot be reserved" if expected_error is ValueError else "already exists"
    )
    with pytest.raises(expected_error, match=expected_message):
        create_parse_claim(workspace, _claim())

    if node_kind == "file":
        assert not target.is_symlink()
        assert target.read_bytes() == sentinel
    elif node_kind == "directory":
        assert target.is_dir()
        assert list(target.iterdir()) == []
    else:
        assert target.is_symlink()
        assert external.read_bytes() == sentinel


@pytest.mark.parametrize(
    "symlink_position",
    ["workspace-root", "runtime", "tool-state", "workspace-parent"],
)
def test_create_parse_claim_rejects_symlinks_in_the_workspace_path(
    tmp_path: Path,
    symlink_position: str,
) -> None:
    if symlink_position == "workspace-root":
        actual_workspace = tmp_path / "actual-workspace"
        actual_workspace.mkdir()
        workspace = tmp_path / "workspace"
        _symlink_or_skip(
            workspace,
            actual_workspace,
            target_is_directory=True,
        )
        escaped_claim = actual_workspace / CLAIM_RELATIVE_PATH
    elif symlink_position == "runtime":
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        external_runtime = tmp_path / "external-runtime"
        external_runtime.mkdir()
        _symlink_or_skip(
            workspace / "runtime",
            external_runtime,
            target_is_directory=True,
        )
        escaped_claim = external_runtime / "tool-state/logparse-parse.claim"
    elif symlink_position == "tool-state":
        workspace = tmp_path / "workspace"
        (workspace / "runtime").mkdir(parents=True)
        external_state = tmp_path / "external-state"
        external_state.mkdir()
        _symlink_or_skip(
            workspace / "runtime/tool-state",
            external_state,
            target_is_directory=True,
        )
        escaped_claim = external_state / "logparse-parse.claim"
    else:
        actual_parent = tmp_path / "actual-parent"
        actual_parent.mkdir()
        actual_workspace = actual_parent / "workspace"
        actual_workspace.mkdir()
        linked_parent = tmp_path / "linked-parent"
        _symlink_or_skip(
            linked_parent,
            actual_parent,
            target_is_directory=True,
        )
        workspace = linked_parent / "workspace"
        escaped_claim = actual_workspace / CLAIM_RELATIVE_PATH

    with pytest.raises(ValueError, match="(Workspace|directory)"):
        create_parse_claim(workspace, _claim())

    assert not escaped_claim.exists()


@pytest.mark.parametrize(
    ("failure_point", "expected_bytes"),
    [
        ("claim_reserved", b""),
        ("claim_written", None),
    ],
)
def test_create_parse_claim_never_removes_or_retries_a_faulted_reservation(
    tmp_path: Path,
    failure_point: str,
    expected_bytes: bytes | None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    claim = _claim()
    observed: list[str] = []

    def inject_fault(point: str) -> None:
        observed.append(point)
        if point == failure_point:
            raise RuntimeError(f"injected {point}")

    with pytest.raises(RuntimeError, match=failure_point):
        create_parse_claim(workspace, claim, fault_point=inject_fault)

    target = workspace / CLAIM_RELATIVE_PATH
    retained_bytes = (
        canonical_json_bytes(claim) if expected_bytes is None else expected_bytes
    )
    assert target.read_bytes() == retained_bytes
    assert observed == (
        ["claim_reserved", "claim_written"]
        if failure_point == "claim_written"
        else ["claim_reserved"]
    )

    with pytest.raises(FileExistsError, match="already exists"):
        create_parse_claim(workspace, claim)
    assert target.read_bytes() == retained_bytes


def test_create_parse_claim_allows_exactly_one_concurrent_winner(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worker_count = 8
    barrier = threading.Barrier(worker_count)
    claims = [
        _claim(request_sha256=f"{index + 1:x}" * 64)
        for index in range(worker_count)
    ]

    def attempt(index: int) -> tuple[str, int]:
        barrier.wait()
        try:
            create_parse_claim(workspace, claims[index])
        except FileExistsError:
            return "rejected", index
        return "created", index

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(attempt, range(worker_count)))

    winners = [index for result, index in results if result == "created"]
    assert len(winners) == 1
    assert sum(result == "rejected" for result, _index in results) == worker_count - 1
    assert (workspace / CLAIM_RELATIVE_PATH).read_bytes() == canonical_json_bytes(
        claims[winners[0]]
    )
