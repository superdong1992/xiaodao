from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest

from problem_locator.contracts import (
    ErrorCode,
    ExecutionStage,
    LogparseParseClaim,
    VersionedRef,
    WorkspaceInputManifest,
)
from problem_locator.contracts.serialization import canonical_json_bytes
from problem_locator.runtime import workspace as workspace_module
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.workspace import PreparedWorkspace, WorkspaceManager


SAFE_DIR_FDS = workspace_module._safe_dir_fd_operations_supported()


def _identity(path: Path) -> tuple[int, int]:
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


def _prepared(tmp_path: Path) -> PreparedWorkspace:
    root = tmp_path / "data" / "workspace"
    (root / "inputs").mkdir(parents=True)
    (root / "runtime" / "tool-state").mkdir(parents=True)
    (root / "output" / "proposals").mkdir(parents=True)
    root_device, root_inode = _identity(root)
    inputs_device, inputs_inode = _identity(root / "inputs")
    runtime_device, runtime_inode = _identity(root / "runtime")
    tool_state_device, tool_state_inode = _identity(root / "runtime" / "tool-state")
    output_device, output_inode = _identity(root / "output")
    return PreparedWorkspace(
        root=root,
        root_device=root_device,
        root_inode=root_inode,
        inputs_device=inputs_device,
        inputs_inode=inputs_inode,
        runtime_device=runtime_device,
        runtime_inode=runtime_inode,
        tool_state_device=tool_state_device,
        tool_state_inode=tool_state_inode,
        output_device=output_device,
        output_inode=output_inode,
        manifest=cast(WorkspaceInputManifest, object()),
        manifest_bytes=b"",
        attachments=(),
        evidence=(),
        artifacts=(),
        previous_outcomes=(),
    )


def _claim() -> LogparseParseClaim:
    return LogparseParseClaim(
        schema_version=1,
        job_id="00000000-0000-4000-8000-000000000001",
        attachment_id="00000000-0000-4000-8000-000000000002",
        attachment_sha256="a" * 64,
        artifact_proposal_key="parsed-log",
        logparse_tool_ref=VersionedRef(
            id="logparse/tool",
            version="1.0.0",
            content_hash="b" * 64,
        ),
        request_sha256="c" * 64,
    )


def _assert_failure(
    captured: pytest.ExceptionInfo[RuntimeExecutionError],
    *,
    stage: ExecutionStage,
    code: ErrorCode,
) -> None:
    assert captured.value.failure.stage is stage
    assert captured.value.failure.code is code


@pytest.mark.skipif(not SAFE_DIR_FDS, reason="safe dirfd primitives unavailable")
def test_fd_anchored_operations_accept_ordinary_workspace_nodes(tmp_path: Path) -> None:
    workspace = _prepared(tmp_path)
    body = "fixed context\n"
    WorkspaceManager.write_context(workspace, body)
    proposal = workspace.root / "output" / "proposals" / "result.json"
    proposal.write_bytes(b"12345")
    claim_path = workspace.tool_state_root / "logparse-parse.claim"
    claim_path.write_bytes(canonical_json_bytes(_claim()))

    assert WorkspaceManager.temporary_output_bytes(workspace) == (
        len(body.encode("utf-8")) + 5 + claim_path.stat().st_size
    )
    assert WorkspaceManager.read_claim(workspace) == _claim()


@pytest.mark.skipif(not SAFE_DIR_FDS, reason="safe dirfd primitives unavailable")
def test_write_context_rejects_replaced_workspace_root(tmp_path: Path) -> None:
    workspace = _prepared(tmp_path)
    detached = tmp_path / "detached-workspace"
    workspace.root.rename(detached)
    (workspace.root / "runtime").mkdir(parents=True)

    with pytest.raises(RuntimeExecutionError) as captured:
        WorkspaceManager.write_context(workspace, "must not escape\n")

    _assert_failure(
        captured,
        stage=ExecutionStage.WORKSPACE_PREPARE,
        code=ErrorCode.WORKSPACE_PREPARE_FAILED,
    )
    assert not (workspace.root / "runtime" / "context.txt").exists()
    assert not (detached / "runtime" / "context.txt").exists()


@pytest.mark.skipif(not SAFE_DIR_FDS, reason="safe dirfd primitives unavailable")
def test_temporary_output_rejects_symlink_escape_and_hardlink(tmp_path: Path) -> None:
    workspace = _prepared(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    escape = workspace.root / "output" / "proposals" / "escape"
    escape.symlink_to(outside)

    with pytest.raises(RuntimeExecutionError) as captured:
        WorkspaceManager.temporary_output_bytes(workspace)
    _assert_failure(
        captured,
        stage=ExecutionStage.BACKEND_EXECUTE,
        code=ErrorCode.WORKSPACE_LIMIT,
    )

    escape.unlink()
    os.link(outside, workspace.root / "output" / "proposals" / "linked")
    with pytest.raises(RuntimeExecutionError) as captured:
        WorkspaceManager.temporary_output_bytes(workspace)
    _assert_failure(
        captured,
        stage=ExecutionStage.BACKEND_EXECUTE,
        code=ErrorCode.WORKSPACE_LIMIT,
    )


@pytest.mark.skipif(not SAFE_DIR_FDS, reason="safe dirfd primitives unavailable")
def test_read_claim_rejects_parent_symlink_and_extra_nodes(tmp_path: Path) -> None:
    workspace = _prepared(tmp_path)
    original = workspace.tool_state_root
    original.rmdir()
    outside = tmp_path / "outside-tool-state"
    outside.mkdir()
    (outside / "logparse-parse.claim").write_bytes(canonical_json_bytes(_claim()))
    original.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeExecutionError) as captured:
        WorkspaceManager.read_claim(workspace)
    _assert_failure(
        captured,
        stage=ExecutionStage.TOOL_EXECUTE,
        code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
    )

    original.unlink()
    original.mkdir()
    (original / "logparse-parse.claim").write_bytes(canonical_json_bytes(_claim()))
    (original / "unexpected").write_bytes(b"")
    with pytest.raises(RuntimeExecutionError) as captured:
        WorkspaceManager.read_claim(workspace)
    _assert_failure(
        captured,
        stage=ExecutionStage.TOOL_EXECUTE,
        code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
    )


@pytest.mark.skipif(not SAFE_DIR_FDS, reason="safe dirfd primitives unavailable")
def test_read_claim_rejects_workspace_ancestor_swap(tmp_path: Path) -> None:
    workspace = _prepared(tmp_path)
    parent = workspace.root.parent
    parent.rename(tmp_path / "detached-data")
    outside_parent = tmp_path / "outside-parent"
    outside_tool_state = outside_parent / "workspace" / "runtime" / "tool-state"
    outside_tool_state.mkdir(parents=True)
    (outside_parent / "workspace" / "inputs").mkdir()
    (outside_parent / "workspace" / "output").mkdir()
    (outside_tool_state / "logparse-parse.claim").write_bytes(
        canonical_json_bytes(_claim())
    )
    parent.symlink_to(outside_parent, target_is_directory=True)

    with pytest.raises(RuntimeExecutionError) as captured:
        WorkspaceManager.read_claim(workspace)

    _assert_failure(
        captured,
        stage=ExecutionStage.TOOL_EXECUTE,
        code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
    )


@pytest.mark.skipif(not SAFE_DIR_FDS, reason="safe dirfd primitives unavailable")
def test_read_claim_rejects_hardlink_and_fifo_without_blocking(tmp_path: Path) -> None:
    workspace = _prepared(tmp_path)
    claim_path = workspace.tool_state_root / "logparse-parse.claim"
    source = tmp_path / "claim-source"
    source.write_bytes(canonical_json_bytes(_claim()))
    os.link(source, claim_path)

    with pytest.raises(RuntimeExecutionError) as captured:
        WorkspaceManager.read_claim(workspace)
    _assert_failure(
        captured,
        stage=ExecutionStage.TOOL_EXECUTE,
        code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
    )

    claim_path.unlink()
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    os.mkfifo(claim_path)
    with pytest.raises(RuntimeExecutionError) as captured:
        WorkspaceManager.read_claim(workspace)
    _assert_failure(
        captured,
        stage=ExecutionStage.TOOL_EXECUTE,
        code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
    )


@pytest.mark.skipif(not SAFE_DIR_FDS, reason="safe dirfd primitives unavailable")
def test_read_claim_is_bounded_and_rejects_a_concurrent_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _prepared(tmp_path)
    claim_path = workspace.tool_state_root / "logparse-parse.claim"
    claim_path.write_bytes(b"x" * (workspace_module._MAX_PARSE_CLAIM_BYTES + 1))

    with pytest.raises(RuntimeExecutionError) as captured:
        WorkspaceManager.read_claim(workspace)
    _assert_failure(
        captured,
        stage=ExecutionStage.TOOL_EXECUTE,
        code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
    )

    claim_path.write_bytes(canonical_json_bytes(_claim()))
    original_read = os.read
    changed = False

    def _read_then_change(descriptor: int, count: int) -> bytes:
        nonlocal changed
        data = original_read(descriptor, count)
        if data and not changed:
            changed = True
            claim_path.write_bytes(data + b" ")
        return data

    monkeypatch.setattr(workspace_module.os, "read", _read_then_change)
    with pytest.raises(RuntimeExecutionError) as captured:
        WorkspaceManager.read_claim(workspace)
    _assert_failure(
        captured,
        stage=ExecutionStage.TOOL_EXECUTE,
        code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
    )


@pytest.mark.skipif(not SAFE_DIR_FDS, reason="safe dirfd primitives unavailable")
def test_purge_does_not_follow_replaced_output_or_ancestor(tmp_path: Path) -> None:
    workspace = _prepared(tmp_path)
    outside = tmp_path / "outside-output"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_bytes(b"keep")
    original_output = workspace.root / "output"
    moved_output = workspace.root / "moved-output"
    original_output.rename(moved_output)
    original_output.symlink_to(outside, target_is_directory=True)

    WorkspaceManager.purge_agent_output(workspace)

    assert sentinel.read_bytes() == b"keep"
    assert (moved_output / "proposals").is_dir()

    parent = workspace.root.parent
    detached_parent = tmp_path / "detached-data"
    parent.rename(detached_parent)
    outside_parent = tmp_path / "outside-parent"
    (outside_parent / "workspace" / "output").mkdir(parents=True)
    outside_sentinel = outside_parent / "workspace" / "output" / "keep.txt"
    outside_sentinel.write_bytes(b"outside")
    parent.symlink_to(outside_parent, target_is_directory=True)

    WorkspaceManager.purge_agent_output(workspace)

    assert outside_sentinel.read_bytes() == b"outside"


@pytest.mark.skipif(not SAFE_DIR_FDS, reason="safe dirfd primitives unavailable")
def test_purge_removes_only_the_frozen_output_tree(tmp_path: Path) -> None:
    workspace = _prepared(tmp_path)
    (workspace.root / "output" / "proposals" / "secret.json").write_bytes(b"secret")
    (workspace.root / "inputs" / "keep.txt").write_bytes(b"input")

    WorkspaceManager.purge_agent_output(workspace)

    assert not (workspace.root / "output").exists()
    assert (workspace.root / "inputs" / "keep.txt").read_bytes() == b"input"


def test_platform_without_safe_dirfds_uses_conservative_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _prepared(tmp_path)
    claim_path = workspace.tool_state_root / "logparse-parse.claim"
    claim_path.write_bytes(canonical_json_bytes(_claim()))
    secret = workspace.root / "output" / "proposals" / "secret.json"
    secret.write_bytes(b"secret")
    monkeypatch.setattr(
        workspace_module,
        "_safe_dir_fd_operations_supported",
        lambda: False,
    )
    for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"):
        monkeypatch.delattr(workspace_module.os, flag, raising=False)

    WorkspaceManager.write_context(workspace, "context\n")

    assert WorkspaceManager.temporary_output_bytes(workspace) == (
        len(b"context\n") + claim_path.stat().st_size + len(b"secret")
    )
    assert WorkspaceManager.read_claim(workspace) == _claim()
    outside = tmp_path / "outside-secret"
    outside.write_bytes(b"keep")
    outside_link = workspace.root / "output" / "proposals" / "outside-link"
    link_created = False
    try:
        outside_link.symlink_to(outside)
        link_created = True
    except (NotImplementedError, OSError):
        pass

    WorkspaceManager.purge_agent_output(workspace)

    assert outside.read_bytes() == b"keep"
    if os.name == "nt" and link_created:
        assert secret.read_bytes() == b"secret"
    else:
        assert not (workspace.root / "output").exists()


def test_fallback_rejects_links_hardlinks_fifo_and_root_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workspace_module,
        "_safe_dir_fd_operations_supported",
        lambda: False,
    )

    linked_workspace = _prepared(tmp_path / "linked-parent")
    outside = tmp_path / "outside"
    outside.mkdir()
    tool_state = linked_workspace.tool_state_root
    tool_state.rmdir()
    try:
        tool_state.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        tool_state.mkdir()
    else:
        with pytest.raises(RuntimeExecutionError) as linked_failure:
            WorkspaceManager.read_claim(linked_workspace)
        _assert_failure(
            linked_failure,
            stage=ExecutionStage.TOOL_EXECUTE,
            code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
        )

    hardlink_workspace = _prepared(tmp_path / "hardlink-parent")
    source = tmp_path / "hardlink-claim"
    source.write_bytes(canonical_json_bytes(_claim()))
    try:
        os.link(source, hardlink_workspace.tool_state_root / "logparse-parse.claim")
    except (NotImplementedError, OSError):
        pass
    else:
        with pytest.raises(RuntimeExecutionError) as hardlink_failure:
            WorkspaceManager.read_claim(hardlink_workspace)
        _assert_failure(
            hardlink_failure,
            stage=ExecutionStage.TOOL_EXECUTE,
            code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
        )

    if hasattr(os, "mkfifo"):
        fifo_workspace = _prepared(tmp_path / "fifo-parent")
        os.mkfifo(fifo_workspace.tool_state_root / "logparse-parse.claim")
        with pytest.raises(RuntimeExecutionError) as fifo_failure:
            WorkspaceManager.read_claim(fifo_workspace)
        _assert_failure(
            fifo_failure,
            stage=ExecutionStage.TOOL_EXECUTE,
            code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
        )

    swapped_workspace = _prepared(tmp_path / "swapped-parent")
    original_root = swapped_workspace.root
    original_root.rename(tmp_path / "detached-root")
    replacement = tmp_path / "replacement-root"
    (replacement / "inputs").mkdir(parents=True)
    (replacement / "runtime" / "tool-state").mkdir(parents=True)
    (replacement / "output").mkdir(parents=True)
    try:
        original_root.symlink_to(replacement, target_is_directory=True)
    except (NotImplementedError, OSError):
        pass
    else:
        with pytest.raises(RuntimeExecutionError) as swapped_failure:
            WorkspaceManager.temporary_output_bytes(swapped_workspace)
        _assert_failure(
            swapped_failure,
            stage=ExecutionStage.BACKEND_EXECUTE,
            code=ErrorCode.WORKSPACE_LIMIT,
        )


def test_fallback_purge_does_not_follow_an_ancestor_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _prepared(tmp_path)
    parent = workspace.root.parent
    parent.rename(tmp_path / "detached-data")
    outside_parent = tmp_path / "outside-parent"
    (outside_parent / "workspace" / "output").mkdir(parents=True)
    sentinel = outside_parent / "workspace" / "output" / "keep.txt"
    sentinel.write_bytes(b"keep")
    try:
        parent.symlink_to(outside_parent, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symbolic links are unavailable")
    monkeypatch.setattr(
        workspace_module,
        "_safe_dir_fd_operations_supported",
        lambda: False,
    )

    WorkspaceManager.purge_agent_output(workspace)

    assert sentinel.read_bytes() == b"keep"
