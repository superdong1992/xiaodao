from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import cast

import pytest

from problem_locator.contracts import models as contract_models
from problem_locator.contracts import (
    Attachment,
    AttachmentStatus,
    CaseAggregate,
    ErrorCode,
    ExecutionStage,
    Job,
    LogparseParseClaim,
    MaterializedPath,
    ResourceRef,
    StateFile,
    VersionedRef,
    WorkspaceInputManifest,
)
from problem_locator.contracts.enums import AttachmentFilenameSuffix, ResourceKind
from problem_locator.contracts.serialization import canonical_json_bytes
from problem_locator.runtime import workspace as workspace_module
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.workspace import PreparedWorkspace, WorkspaceManager


SAFE_DIR_FDS = workspace_module._safe_dir_fd_operations_supported()
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_FIXTURES = REPOSITORY_ROOT / "tests/fixtures/contracts/positive"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000050"


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


class _AttachmentStore:
    def __init__(self, payload: bytes, *, failure: BaseException | None = None) -> None:
        self.payload = payload
        self.failure = failure
        self.calls: list[tuple[ResourceRef, Path]] = []

    def materialize_read_only(
        self,
        resource_ref: ResourceRef,
        destination: Path,
    ) -> MaterializedPath:
        self.calls.append((resource_ref, destination))
        if self.failure is not None:
            raise self.failure
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.payload)
        destination.chmod(0o444)
        return MaterializedPath(path=str(destination), read_only=True)


def _attachment_job_and_aggregate(
    *,
    name: str,
    content_type: str,
    payload: bytes,
) -> tuple[Job, CaseAggregate, Attachment]:
    job_payload = Job.model_validate_json(
        (CONTRACT_FIXTURES / "job-route.json").read_bytes()
    ).model_dump(mode="json")
    job_payload["attachment_refs"] = [ATTACHMENT_ID]
    job = Job.model_validate(job_payload)
    attachment = Attachment(
        attachment_id=ATTACHMENT_ID,
        case_id=job.case_id,
        status=AttachmentStatus.READY,
        name=name,
        content_type=content_type,
        declared_size=len(payload),
        declared_sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        storage_key=(
            f"resources/cases/{job.case_id}/attachments/"
            f"{ATTACHMENT_ID}/payload"
        ),
        created_at="2026-07-31T00:00:00.000Z",
        updated_at="2026-07-31T00:00:00.000Z",
    )
    state = StateFile.model_validate_json(
        (CONTRACT_FIXTURES / "state.json").read_bytes()
    )
    aggregate_payload = next(iter(state.cases.values())).model_dump(mode="json")
    aggregate_payload["jobs"] = {job.job_id: job.model_dump(mode="json")}
    aggregate_payload["attachments"] = {
        attachment.attachment_id: attachment.model_dump(mode="json")
    }
    aggregate = CaseAggregate.model_validate(aggregate_payload)
    return job, aggregate, attachment


def _restore_inputs_permissions(root: Path) -> None:
    inputs = root / "inputs"
    if not inputs.exists():
        return
    for path in sorted(inputs.rglob("*"), reverse=True):
        path.chmod(0o755 if path.is_dir() else 0o644)
    inputs.chmod(0o755)


@pytest.mark.parametrize(
    ("name", "content_type", "expected_suffix", "expected_leaf"),
    [
        ("logs.gz", "application/gzip", ".gz", "payload.gz"),
        ("logs.zip", "application/zip", ".zip", "payload.zip"),
        ("logs.tar.gz", "application/gzip", ".tar.gz", "payload.tar.gz"),
        ("logs.tgz", "application/gzip", ".tgz", "payload.tgz"),
        ("logs.tar", "application/x-tar", ".tar", "payload.tar"),
        ("notes.txt", "text/plain", None, "payload"),
    ],
)
def test_attachment_suffix_helper_drives_exact_read_only_workspace_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    content_type: str,
    expected_suffix: str | None,
    expected_leaf: str,
) -> None:
    payload = f"immutable bytes for {name}\n".encode()
    job, aggregate, attachment = _attachment_job_and_aggregate(
        name=name,
        content_type=content_type,
        payload=payload,
    )
    derive_calls: list[tuple[str, str]] = []
    path_calls: list[tuple[str, AttachmentFilenameSuffix | None]] = []

    def derive(
        source_name: str,
        source_content_type: str,
    ) -> AttachmentFilenameSuffix | None:
        derive_calls.append((source_name, source_content_type))
        return contract_models.derive_attachment_filename_suffix(
            source_name,
            source_content_type,
        )

    def relative_path(
        attachment_id: str,
        filename_suffix: AttachmentFilenameSuffix | None,
    ) -> str:
        path_calls.append((attachment_id, filename_suffix))
        return contract_models.workspace_attachment_relative_path(
            attachment_id,
            filename_suffix,
        )

    monkeypatch.setattr(
        workspace_module,
        "derive_attachment_filename_suffix",
        derive,
    )
    monkeypatch.setattr(
        workspace_module,
        "workspace_attachment_relative_path",
        relative_path,
    )
    store = _AttachmentStore(payload)
    manager = WorkspaceManager(tmp_path / "data")

    workspace = manager.prepare(job, aggregate, store)  # type: ignore[arg-type]
    try:
        entry = workspace.manifest.entries[0]
        expected_relative = (
            f"inputs/attachments/{ATTACHMENT_ID}/{expected_leaf}"
        )
        materialized = workspace.root / expected_relative
        assert derive_calls == [(attachment.name, attachment.content_type)]
        assert len(path_calls) == 1
        assert path_calls[0][0] == ATTACHMENT_ID
        assert (
            None if path_calls[0][1] is None else path_calls[0][1].value
        ) == expected_suffix
        assert entry.relative_path == expected_relative
        assert (
            None if entry.filename_suffix is None else entry.filename_suffix.value
        ) == expected_suffix
        assert store.calls == [(_attachment_ref_for_test(attachment), materialized)]
        assert materialized.read_bytes() == payload
        assert materialized.stat().st_mode & (
            stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        ) == 0
        manifest_path = workspace.root / "inputs/manifest.json"
        assert manifest_path.read_bytes() == workspace.manifest_bytes
        assert workspace.manifest_bytes == canonical_json_bytes(workspace.manifest)
    finally:
        _restore_inputs_permissions(workspace.root)


def _attachment_ref_for_test(attachment: Attachment) -> ResourceRef:
    assert attachment.storage_key is not None
    assert attachment.size is not None
    assert attachment.sha256 is not None
    return ResourceRef(
        resource_kind=ResourceKind.FILE,
        storage_key=attachment.storage_key,
        size=attachment.size,
        sha256=attachment.sha256,
    )


def test_attachment_hash_drift_remains_non_retryable_at_suffixed_path(
    tmp_path: Path,
) -> None:
    expected = b"expected"
    job, aggregate, _ = _attachment_job_and_aggregate(
        name="logs.tar.gz",
        content_type="application/gzip",
        payload=expected,
    )
    store = _AttachmentStore(b"tampered")

    with pytest.raises(RuntimeExecutionError) as captured:
        WorkspaceManager(tmp_path / "data").prepare(
            job,
            aggregate,
            store,  # type: ignore[arg-type]
        )

    _assert_failure(
        captured,
        stage=ExecutionStage.WORKSPACE_PREPARE,
        code=ErrorCode.RESOURCE_HASH_MISMATCH,
    )
    assert captured.value.failure.retryable is False
    assert store.calls[0][1].name == "payload.tar.gz"


def test_transient_attachment_materialization_failure_remains_retryable(
    tmp_path: Path,
) -> None:
    payload = b"archive"
    job, aggregate, _ = _attachment_job_and_aggregate(
        name="logs.zip",
        content_type="application/zip",
        payload=payload,
    )
    store = _AttachmentStore(payload, failure=OSError("transient storage fault"))

    with pytest.raises(RuntimeExecutionError) as captured:
        WorkspaceManager(tmp_path / "data").prepare(
            job,
            aggregate,
            store,  # type: ignore[arg-type]
        )

    _assert_failure(
        captured,
        stage=ExecutionStage.WORKSPACE_PREPARE,
        code=ErrorCode.WORKSPACE_PREPARE_FAILED,
    )
    assert captured.value.failure.retryable is True
    assert store.calls[0][1].name == "payload.zip"


def test_attachment_suffix_workspace_is_usable_on_no_dirfd_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"zip archive"
    job, aggregate, _ = _attachment_job_and_aggregate(
        name="logs.zip",
        content_type="application/zip",
        payload=payload,
    )
    monkeypatch.setattr(
        workspace_module,
        "_safe_dir_fd_operations_supported",
        lambda: False,
    )
    workspace = WorkspaceManager(tmp_path / "data").prepare(
        job,
        aggregate,
        _AttachmentStore(payload),  # type: ignore[arg-type]
    )
    try:
        WorkspaceManager.write_context(workspace, "fallback context\n")
        attachment_path = (
            workspace.root
            / f"inputs/attachments/{ATTACHMENT_ID}/payload.zip"
        )
        assert attachment_path.read_bytes() == payload
        assert workspace.manifest.entries[0].relative_path.endswith("/payload.zip")
        assert workspace.context_path.read_text(encoding="utf-8") == "fallback context\n"
    finally:
        _restore_inputs_permissions(workspace.root)


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


def test_read_claim_allows_finalizer_marker_alone_or_beside_claim(
    tmp_path: Path,
) -> None:
    workspace = _prepared(tmp_path)
    marker = workspace.tool_state_root / "agent-job-outcome.finalized"
    marker.write_bytes(b"marker validated by output reader")

    assert WorkspaceManager.read_claim(workspace) is None

    claim_path = workspace.tool_state_root / "logparse-parse.claim"
    claim_path.write_bytes(canonical_json_bytes(_claim()))
    assert WorkspaceManager.read_claim(workspace) == _claim()


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
    assert secret.read_bytes() == b"secret"


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
