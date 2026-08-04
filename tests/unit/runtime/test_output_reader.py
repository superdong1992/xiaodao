from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

import problem_locator.runtime.output_reader as output_reader_module

from problem_locator.contracts.enums import ErrorCode, ExecutionStage, ResourceKind
from problem_locator.contracts.models import (
    AgentJobOutcome,
    FixtureManifest,
    Job,
    TreeManifest,
    TreeManifestEntry,
    WorkspaceInputManifest,
)
from problem_locator.contracts.serialization import (
    bytes_sha256,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.integrations.result_archive import build_result_archive
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.output_reader import read_agent_output
from problem_locator.runtime.workspace import PreparedWorkspace


REPOSITORY_ROOT = Path(__file__).parents[3]
CONTRACT_FIXTURES = REPOSITORY_ROOT / "tests/fixtures/contracts/positive"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests/fixtures/components/runtime-output"


def _fixture_payload(name: str) -> dict[str, Any]:
    return json.loads((CONTRACT_FIXTURES / name).read_bytes())


def _diagnosis_inputs() -> tuple[Job, WorkspaceInputManifest, dict[str, Any], bytes]:
    job = parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / "job-diagnose.json").read_bytes(),
        model_type=Job,
    )
    manifest = parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / "workspace-input-manifest.json").read_bytes(),
        model_type=WorkspaceInputManifest,
    )
    return (
        job,
        manifest,
        _fixture_payload("agent-job-outcome-diagnosis.json"),
        (CONTRACT_FIXTURES / "user-result.json").read_bytes(),
    )


def _route_inputs() -> tuple[Job, WorkspaceInputManifest, dict[str, Any]]:
    job = parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / "job-route.json").read_bytes(),
        model_type=Job,
    )
    manifest = WorkspaceInputManifest(
        schema_version=1,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        logparse_tool_ref=None,
        logparse_product=None,
        entries=[],
    )
    return job, manifest, _fixture_payload("agent-job-outcome-route.json")


def _write_outcome(root: Path, payload: dict[str, Any] | bytes) -> Path:
    output = root / "output"
    output.mkdir(parents=True, exist_ok=True)
    data = payload if isinstance(payload, bytes) else canonical_json_bytes(payload)
    path = output / "job_outcome.json"
    path.write_bytes(data)
    return path


def _write_file_proposal(root: Path, relative_path: str, content: bytes) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _diagnostic_draft(
    *,
    proposal_key: str = "diagnostic_export",
    relative_path: str = "output/proposals/diagnostic_export/export.bin",
    resource_kind: str = "FILE",
    declared_size: int | None = None,
    declared_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "proposal_key": proposal_key,
        "artifact_kind": "DIAGNOSTIC_EXPORT",
        "name": "diagnostic-export",
        "content_type": "application/octet-stream",
        "resource_kind": resource_kind,
        "workspace_relative_path": relative_path,
        "declared_size": declared_size,
        "declared_sha256": declared_sha256,
        "metadata": {
            "schema_version": 1,
            "format_id": "runtime-output",
            "description": "Opaque diagnostic export for output-reader tests.",
        },
    }


def _assert_failure(
    captured: pytest.ExceptionInfo[RuntimeExecutionError],
    code: ErrorCode,
) -> None:
    failure = captured.value.failure
    assert failure.stage is ExecutionStage.OUTCOME_VALIDATE
    assert failure.code is code
    assert failure.retryable is False
    assert failure.details == []


def test_reads_only_final_canonical_outcome_and_validates_user_result(
    tmp_path: Path,
) -> None:
    job, manifest, payload, user_result_bytes = _diagnosis_inputs()
    outcome_path = _write_outcome(tmp_path, payload)
    (outcome_path.parent / "job_outcome.json.part").write_bytes(b"not JSON")
    draft = payload["proposed_artifact_drafts"][0]
    _write_file_proposal(tmp_path, draft["workspace_relative_path"], user_result_bytes)

    result = read_agent_output(tmp_path, job, manifest)

    expected_outcome = AgentJobOutcome.model_validate(payload)
    assert result.outcome == expected_outcome
    assert result.canonical_bytes == canonical_json_bytes(expected_outcome)
    assert result.user_result is not None
    assert result.user_result.candidate_statement == (
        "The inventory RPC exceeded its deadline."
    )
    assert len(result.proposal_resources) == 1
    resource = result.proposal_resources[0]
    assert resource.proposal_key == "user_result"
    assert resource.resource_kind is ResourceKind.FILE
    assert resource.size == len(user_result_bytes)
    assert resource.sha256 == hashlib.sha256(user_result_bytes).hexdigest()
    assert resource.tree_manifest is None


def _add_manual_result_archive(
    root: Path,
    payload: dict[str, Any],
    *,
    result_text: str,
    target_log_paths: list[str] | None = None,
) -> bytes:
    target_paths = [] if target_log_paths is None else target_log_paths
    proposal_key = "user_result_archive"
    request_path = f"output/proposals/{proposal_key}/request.json"
    result_path = f"output/proposals/{proposal_key}/result.zip"
    _write_file_proposal(
        root,
        request_path,
        canonical_json_bytes(
            {
                "schema_version": 1,
                "result_text": result_text,
                "target_log_paths": target_paths,
            }
        ),
    )
    archive_path = build_result_archive(root, request_path, result_path)
    archive_bytes = archive_path.read_bytes()
    payload["proposed_artifact_drafts"].append(
        {
            "proposal_key": proposal_key,
            "artifact_kind": "USER_RESULT_ARCHIVE",
            "name": "result.zip",
            "content_type": "application/zip",
            "resource_kind": "FILE",
            "workspace_relative_path": result_path,
            "declared_size": len(archive_bytes),
            "declared_sha256": hashlib.sha256(archive_bytes).hexdigest(),
            "metadata": {
                "schema_version": 1,
                "format_id": "problem-locator-result-archive-v1",
                "description": "Controlled user-facing diagnosis archive.",
                "user_result_proposal_key": "user_result",
                "target_log_count": len(target_paths),
            },
        }
    )
    return archive_bytes


def test_manual_candidate_archive_is_bound_to_exact_candidate_statement(
    tmp_path: Path,
) -> None:
    job, manifest, payload, user_result_bytes = _diagnosis_inputs()
    statement = payload["payload"]["candidate_conclusion_draft"]["statement"]
    archive_bytes = _add_manual_result_archive(
        tmp_path,
        payload,
        result_text=statement + "\n",
    )
    draft = payload["proposed_artifact_drafts"][0]
    _write_file_proposal(tmp_path, draft["workspace_relative_path"], user_result_bytes)
    _write_outcome(tmp_path, payload)

    result = read_agent_output(tmp_path, job, manifest)

    assert [resource.proposal_key for resource in result.proposal_resources] == [
        "user_result",
        "user_result_archive",
    ]
    archive = result.proposal_resources[1]
    assert archive.size == len(archive_bytes)
    assert archive.sha256 == hashlib.sha256(archive_bytes).hexdigest()


def test_candidate_archive_reads_fixed_target_logs_from_inputs(
    tmp_path: Path,
) -> None:
    job, manifest, payload, user_result_bytes = _diagnosis_inputs()
    manifest_payload = manifest.model_dump(mode="json")
    evidence_entry = next(
        entry
        for entry in manifest_payload["entries"]
        if entry["input_kind"] == "EVIDENCE"
    )
    artifact_entry = next(
        entry
        for entry in manifest_payload["entries"]
        if entry["input_kind"] == "ARTIFACT"
    )
    evidence_entry.update(
        source_type="LOGPARSE",
        source_ref=artifact_entry["resource_id"],
        locator={
            "kind": "LOGPARSE",
            "relative_path": "logs/target.log",
            "start_time": "2026-07-31T00:00:00.000Z",
            "end_time": "2026-07-31T00:00:01.000Z",
            "start_line": 1,
            "end_line": 1,
        },
    )
    manifest = WorkspaceInputManifest.model_validate(manifest_payload)
    target_path = f'{artifact_entry["relative_path"]}/logs/target.log'
    _write_file_proposal(tmp_path, target_path, b"fixed target log\n")

    statement = payload["payload"]["candidate_conclusion_draft"]["statement"]
    archive_bytes = _add_manual_result_archive(
        tmp_path,
        payload,
        result_text=statement + "\n",
        target_log_paths=[target_path],
    )
    draft = payload["proposed_artifact_drafts"][0]
    _write_file_proposal(tmp_path, draft["workspace_relative_path"], user_result_bytes)
    _write_outcome(tmp_path, payload)

    result = read_agent_output(tmp_path, job, manifest)

    archive = next(
        resource
        for resource in result.proposal_resources
        if resource.proposal_key == "user_result_archive"
    )
    assert archive.size == len(archive_bytes)
    assert archive.sha256 == hashlib.sha256(archive_bytes).hexdigest()


def test_candidate_archive_rejects_unbound_result_text(tmp_path: Path) -> None:
    job, manifest, payload, user_result_bytes = _diagnosis_inputs()
    _add_manual_result_archive(
        tmp_path,
        payload,
        result_text="A different diagnosis.\n",
    )
    draft = payload["proposed_artifact_drafts"][0]
    _write_file_proposal(tmp_path, draft["workspace_relative_path"], user_result_bytes)
    _write_outcome(tmp_path, payload)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)


def test_part_file_without_final_is_outcome_missing(tmp_path: Path) -> None:
    job, manifest, _ = _route_inputs()
    output = tmp_path / "output"
    output.mkdir()
    (output / "job_outcome.json.part").write_bytes(b"complete-looking bytes")

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_MISSING)


@pytest.mark.parametrize(
    "invalid_bytes",
    [
        b"not-json\n",
        b' {"valid":"json but not canonical"}\n',
        b'{"unexpected":true}\n',
    ],
)
def test_invalid_json_canonical_form_or_schema_is_outcome_invalid(
    tmp_path: Path,
    invalid_bytes: bytes,
) -> None:
    job, manifest, _ = _route_inputs()
    _write_outcome(tmp_path, invalid_bytes)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)


def test_outcome_job_binding_must_match_exactly(tmp_path: Path) -> None:
    job, manifest, payload = _route_inputs()
    payload["job_id"] = "00000000-0000-0000-0000-000000000099"
    _write_outcome(tmp_path, payload)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)


def test_outcome_must_be_an_ordinary_single_link_file(tmp_path: Path) -> None:
    job, manifest, payload = _route_inputs()
    output = tmp_path / "output"
    output.mkdir()
    source = tmp_path / "source.json"
    source.write_bytes(canonical_json_bytes(payload))
    os.link(source, output / "job_outcome.json")

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)


def test_outcome_symlink_is_not_followed(tmp_path: Path) -> None:
    job, manifest, payload = _route_inputs()
    output = tmp_path / "output"
    output.mkdir()
    target = tmp_path / "target.json"
    target.write_bytes(canonical_json_bytes(payload))
    try:
        (output / "job_outcome.json").symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)


def test_prepared_workspace_root_replacement_is_rejected(tmp_path: Path) -> None:
    job, manifest, payload = _route_inputs()
    root = tmp_path / "workspace"
    for relative in ("inputs", "runtime/tool-state", "output"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    _write_outcome(root, payload)
    root_stat = root.stat(follow_symlinks=False)
    inputs_stat = (root / "inputs").stat(follow_symlinks=False)
    runtime_stat = (root / "runtime").stat(follow_symlinks=False)
    tool_state_stat = (root / "runtime/tool-state").stat(follow_symlinks=False)
    output_stat = (root / "output").stat(follow_symlinks=False)
    prepared = PreparedWorkspace(
        root=root,
        root_device=root_stat.st_dev,
        root_inode=root_stat.st_ino,
        inputs_device=inputs_stat.st_dev,
        inputs_inode=inputs_stat.st_ino,
        runtime_device=runtime_stat.st_dev,
        runtime_inode=runtime_stat.st_ino,
        tool_state_device=tool_state_stat.st_dev,
        tool_state_inode=tool_state_stat.st_ino,
        output_device=output_stat.st_dev,
        output_inode=output_stat.st_ino,
        manifest=manifest,
        manifest_bytes=canonical_json_bytes(manifest),
        attachments=(),
        evidence=(),
        artifacts=(),
        previous_outcomes=(),
    )
    root.rename(tmp_path / "original-workspace")
    _write_outcome(root, payload)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(prepared, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)


def test_valid_directory_proposal_builds_the_frozen_tree_manifest(
    tmp_path: Path,
) -> None:
    job, manifest, payload = _route_inputs()
    root_relative = "output/proposals/diagnostic_export/tree"
    files = {
        "a.txt": b"alpha",
        "a/result.bin": b"globally sorted after a.txt",
        "nested/b.bin": b"beta\x00bytes",
    }
    entries = [
        TreeManifestEntry(
            path=path,
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        for path, content in sorted(files.items())
    ]
    expected_manifest = TreeManifest(version=1, entries=entries)
    expected_hash = bytes_sha256(canonical_json_bytes(expected_manifest))
    expected_size = sum(len(content) for content in files.values())
    payload["proposed_artifact_drafts"] = [
        _diagnostic_draft(
            relative_path=root_relative,
            resource_kind="DIRECTORY",
            declared_size=expected_size,
            declared_sha256=expected_hash,
        )
    ]
    _write_outcome(tmp_path, payload)
    for path, content in files.items():
        _write_file_proposal(tmp_path, f"{root_relative}/{path}", content)

    result = read_agent_output(tmp_path, job, manifest)

    assert result.user_result is None
    assert len(result.proposal_resources) == 1
    resource = result.proposal_resources[0]
    assert resource.resource_kind is ResourceKind.DIRECTORY
    assert resource.size == expected_size
    assert resource.sha256 == expected_hash
    assert resource.tree_manifest == expected_manifest


def test_valid_directory_proposal_works_without_directory_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, manifest, payload = _route_inputs()
    root_relative = "output/proposals/diagnostic_export/tree"
    content = b"fallback tree bytes"
    expected_manifest = TreeManifest(
        version=1,
        entries=[
            TreeManifestEntry(
                path="nested/result.bin",
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        ],
    )
    payload["proposed_artifact_drafts"] = [
        _diagnostic_draft(
            relative_path=root_relative,
            resource_kind="DIRECTORY",
            declared_size=len(content),
            declared_sha256=bytes_sha256(canonical_json_bytes(expected_manifest)),
        )
    ]
    _write_outcome(tmp_path, payload)
    _write_file_proposal(tmp_path, f"{root_relative}/nested/result.bin", content)
    monkeypatch.setattr(output_reader_module, "_supports_anchored_tree", lambda: False)

    result = read_agent_output(tmp_path, job, manifest)

    assert result.proposal_resources[0].tree_manifest == expected_manifest


@pytest.mark.parametrize("field", ["declared_size", "declared_sha256"])
def test_declared_proposal_size_and_hash_are_verified(
    tmp_path: Path,
    field: str,
) -> None:
    job, manifest, payload = _route_inputs()
    content = b"actual proposal bytes"
    draft = _diagnostic_draft(
        declared_size=len(content),
        declared_sha256=hashlib.sha256(content).hexdigest(),
    )
    draft[field] = len(content) + 1 if field == "declared_size" else "9" * 64
    payload["proposed_artifact_drafts"] = [draft]
    _write_outcome(tmp_path, payload)
    _write_file_proposal(tmp_path, draft["workspace_relative_path"], content)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)


def test_missing_or_linked_proposal_is_outcome_invalid(tmp_path: Path) -> None:
    job, manifest, payload = _route_inputs()
    draft = _diagnostic_draft()
    payload["proposed_artifact_drafts"] = [draft]
    _write_outcome(tmp_path, payload)

    with pytest.raises(RuntimeExecutionError) as missing:
        read_agent_output(tmp_path, job, manifest)
    _assert_failure(missing, ErrorCode.OUTCOME_INVALID)

    target = tmp_path / "target.bin"
    target.write_bytes(b"outside")
    path = tmp_path / draft["workspace_relative_path"]
    path.parent.mkdir(parents=True)
    try:
        path.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")
    with pytest.raises(RuntimeExecutionError) as linked:
        read_agent_output(tmp_path, job, manifest)
    _assert_failure(linked, ErrorCode.OUTCOME_INVALID)


def test_fifo_proposal_is_rejected_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are unavailable on this platform")
    job, manifest, payload = _route_inputs()
    draft = _diagnostic_draft()
    payload["proposed_artifact_drafts"] = [draft]
    _write_outcome(tmp_path, payload)
    path = tmp_path / draft["workspace_relative_path"]
    path.parent.mkdir(parents=True)
    os.mkfifo(path)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)


def test_growing_proposal_read_is_bounded_by_its_frozen_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, manifest, payload = _route_inputs()
    content = b"frozen"
    draft = _diagnostic_draft(
        declared_size=len(content),
        declared_sha256=hashlib.sha256(content).hexdigest(),
    )
    payload["proposed_artifact_drafts"] = [draft]
    _write_outcome(tmp_path, payload)
    proposal = _write_file_proposal(
        tmp_path,
        draft["workspace_relative_path"],
        content,
    )
    proposal_inode = proposal.stat().st_ino
    proposal_reads = 0
    original_read = os.read

    def simulate_concurrent_append(descriptor: int, count: int) -> bytes:
        nonlocal proposal_reads
        if os.fstat(descriptor).st_ino == proposal_inode:
            proposal_reads += 1
            if proposal_reads > 1:
                return b"x"
        return original_read(descriptor, count)

    monkeypatch.setattr(os, "read", simulate_concurrent_append)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)
    assert proposal_reads == 2


def test_proposal_parent_swap_between_snapshot_and_open_never_reads_outside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, manifest, payload = _route_inputs()
    draft = _diagnostic_draft()
    payload["proposed_artifact_drafts"] = [draft]
    _write_outcome(tmp_path, payload)
    proposal = _write_file_proposal(
        tmp_path,
        draft["workspace_relative_path"],
        b"validated",
    )
    outside_directory = tmp_path / "outside-parent"
    outside_directory.mkdir()
    outside_file = outside_directory / proposal.name
    outside_file.write_bytes(b"outside bytes must not be read")
    outside_inode = outside_file.stat().st_ino
    outside_read = False
    original_open_snapshot = output_reader_module._open_snapshot_path
    original_read = os.read
    swapped = False

    def swap_before_open(snapshot: object, relative_path: str) -> int:
        nonlocal swapped
        if relative_path == draft["workspace_relative_path"] and not swapped:
            swapped = True
            proposal.parent.rename(tmp_path / "original-proposal-parent")
            outside_directory.rename(proposal.parent)
        return original_open_snapshot(snapshot, relative_path)

    def observe_read(descriptor: int, count: int) -> bytes:
        nonlocal outside_read
        if os.fstat(descriptor).st_ino == outside_inode:
            outside_read = True
        return original_read(descriptor, count)

    monkeypatch.setattr(output_reader_module, "_open_snapshot_path", swap_before_open)
    monkeypatch.setattr(os, "read", observe_read)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)
    assert swapped is True
    assert outside_read is False


def test_tree_root_swap_between_snapshot_and_open_never_reads_outside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not output_reader_module._supports_anchored_tree():
        pytest.skip("directory-fd traversal is unavailable on this platform")
    job, manifest, payload = _route_inputs()
    relative_path = "output/proposals/diagnostic_export/tree"
    content = b"validated tree bytes"
    expected_manifest = TreeManifest(
        version=1,
        entries=[
            TreeManifestEntry(
                path="result.bin",
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        ],
    )
    payload["proposed_artifact_drafts"] = [
        _diagnostic_draft(
            relative_path=relative_path,
            resource_kind="DIRECTORY",
            declared_size=len(content),
            declared_sha256=bytes_sha256(canonical_json_bytes(expected_manifest)),
        )
    ]
    _write_outcome(tmp_path, payload)
    tree_root = tmp_path / relative_path
    tree_root.mkdir(parents=True)
    (tree_root / "result.bin").write_bytes(content)
    outside_tree = tmp_path / "outside-tree"
    outside_tree.mkdir()
    outside_file = outside_tree / "result.bin"
    outside_file.write_bytes(b"outside bytes must not be read")
    outside_inode = outside_file.stat().st_ino
    outside_read = False
    original_open_directory = output_reader_module._open_snapshot_directory
    original_read = os.read
    swapped = False

    def swap_before_open(snapshot: object, candidate: str) -> int:
        nonlocal swapped
        if candidate == relative_path and not swapped:
            swapped = True
            tree_root.rename(tmp_path / "original-tree")
            outside_tree.rename(tree_root)
        return original_open_directory(snapshot, candidate)

    def observe_read(descriptor: int, count: int) -> bytes:
        nonlocal outside_read
        if os.fstat(descriptor).st_ino == outside_inode:
            outside_read = True
        return original_read(descriptor, count)

    monkeypatch.setattr(
        output_reader_module,
        "_open_snapshot_directory",
        swap_before_open,
    )
    monkeypatch.setattr(os, "read", observe_read)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)
    assert swapped is True
    assert outside_read is False


def test_tree_child_swap_before_open_never_reads_outside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not output_reader_module._supports_anchored_tree():
        pytest.skip("directory-fd traversal is unavailable on this platform")
    job, manifest, payload = _route_inputs()
    relative_path = "output/proposals/diagnostic_export/tree"
    content = b"validated nested bytes"
    expected_manifest = TreeManifest(
        version=1,
        entries=[
            TreeManifestEntry(
                path="nested/result.bin",
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        ],
    )
    payload["proposed_artifact_drafts"] = [
        _diagnostic_draft(
            relative_path=relative_path,
            resource_kind="DIRECTORY",
            declared_size=len(content),
            declared_sha256=bytes_sha256(canonical_json_bytes(expected_manifest)),
        )
    ]
    _write_outcome(tmp_path, payload)
    tree_root = tmp_path / relative_path
    nested = tree_root / "nested"
    nested.mkdir(parents=True)
    (nested / "result.bin").write_bytes(content)
    outside_nested = tmp_path / "outside-nested"
    outside_nested.mkdir()
    outside_file = outside_nested / "result.bin"
    outside_file.write_bytes(b"outside bytes must not be read")
    outside_inode = outside_file.stat().st_ino
    outside_read = False
    original_open = os.open
    original_read = os.read
    swapped = False

    def swap_child_open(
        path: str | bytes | int,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "nested" and dir_fd is not None and not swapped:
            swapped = True
            nested.rename(tmp_path / "original-nested")
            outside_nested.rename(nested)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def observe_read(descriptor: int, count: int) -> bytes:
        nonlocal outside_read
        if os.fstat(descriptor).st_ino == outside_inode:
            outside_read = True
        return original_read(descriptor, count)

    monkeypatch.setattr(os, "open", swap_child_open)
    monkeypatch.setattr(os, "read", observe_read)
    monkeypatch.setattr(output_reader_module, "_supports_anchored_tree", lambda: True)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)
    assert swapped is True
    assert outside_read is False


def test_fallback_tree_child_swap_is_rejected_before_file_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, manifest, payload = _route_inputs()
    relative_path = "output/proposals/diagnostic_export/tree"
    content = b"validated fallback bytes"
    expected_manifest = TreeManifest(
        version=1,
        entries=[
            TreeManifestEntry(
                path="nested/result.bin",
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        ],
    )
    payload["proposed_artifact_drafts"] = [
        _diagnostic_draft(
            relative_path=relative_path,
            resource_kind="DIRECTORY",
            declared_size=len(content),
            declared_sha256=bytes_sha256(canonical_json_bytes(expected_manifest)),
        )
    ]
    _write_outcome(tmp_path, payload)
    tree_root = tmp_path / relative_path
    nested = tree_root / "nested"
    nested.mkdir(parents=True)
    (nested / "result.bin").write_bytes(content)
    outside_nested = tmp_path / "outside-fallback-nested"
    outside_nested.mkdir()
    outside_file = outside_nested / "result.bin"
    outside_file.write_bytes(b"outside bytes must not be read")
    outside_inode = outside_file.stat().st_ino
    outside_read = False
    swapped = False
    original_scandir = os.scandir
    original_read = os.read

    def swap_before_scandir(path: str | bytes | int | os.PathLike[str]):
        nonlocal swapped
        if not isinstance(path, int) and Path(path) == nested and not swapped:
            swapped = True
            nested.rename(tmp_path / "original-fallback-nested")
            outside_nested.rename(nested)
        return original_scandir(path)

    def observe_read(descriptor: int, count: int) -> bytes:
        nonlocal outside_read
        if os.fstat(descriptor).st_ino == outside_inode:
            outside_read = True
        return original_read(descriptor, count)

    monkeypatch.setattr(output_reader_module, "_supports_anchored_tree", lambda: False)
    monkeypatch.setattr(os, "scandir", swap_before_scandir)
    monkeypatch.setattr(os, "read", observe_read)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)
    assert swapped is True
    assert outside_read is False


def test_proposal_parent_symlink_cannot_escape_workspace(tmp_path: Path) -> None:
    job, manifest, payload = _route_inputs()
    draft = _diagnostic_draft()
    payload["proposed_artifact_drafts"] = [draft]
    _write_outcome(tmp_path, payload)
    outside = tmp_path / "outside"
    (outside / "diagnostic_export").mkdir(parents=True)
    (outside / "diagnostic_export/export.bin").write_bytes(b"outside")
    try:
        (tmp_path / "output/proposals").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)


def test_user_result_must_be_canonical_and_semantically_exact(tmp_path: Path) -> None:
    job, manifest, payload, canonical_result = _diagnosis_inputs()
    result_payload = json.loads(canonical_result)
    result_payload["candidate_statement"] = "A different conclusion."
    wrong_result = canonical_json_bytes(result_payload)
    draft = payload["proposed_artifact_drafts"][0]
    draft["declared_size"] = len(wrong_result)
    draft["declared_sha256"] = hashlib.sha256(wrong_result).hexdigest()
    _write_outcome(tmp_path, payload)
    _write_file_proposal(tmp_path, draft["workspace_relative_path"], wrong_result)

    with pytest.raises(RuntimeExecutionError) as semantic:
        read_agent_output(tmp_path, job, manifest)
    _assert_failure(semantic, ErrorCode.OUTCOME_INVALID)

    noncanonical = json.dumps(result_payload, indent=2).encode("utf-8") + b"\n"
    draft["declared_size"] = len(noncanonical)
    draft["declared_sha256"] = hashlib.sha256(noncanonical).hexdigest()
    _write_outcome(tmp_path, payload)
    _write_file_proposal(tmp_path, draft["workspace_relative_path"], noncanonical)
    with pytest.raises(RuntimeExecutionError) as spelling:
        read_agent_output(tmp_path, job, manifest)
    _assert_failure(spelling, ErrorCode.OUTCOME_INVALID)


def test_duplicate_proposal_key_is_rejected_before_any_resource_is_returned(
    tmp_path: Path,
) -> None:
    job, manifest, payload = _route_inputs()
    first = _diagnostic_draft()
    second = _diagnostic_draft(relative_path="output/proposals/diagnostic_export/two.bin")
    payload["proposed_artifact_drafts"] = [first, second]
    _write_outcome(tmp_path, payload)
    _write_file_proposal(tmp_path, first["workspace_relative_path"], b"first")
    _write_file_proposal(tmp_path, second["workspace_relative_path"], b"second")

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)


def _secret_cases() -> dict[str, Any]:
    return json.loads((FIXTURE_ROOT / "output-reader-cases.json").read_bytes())


def test_secret_in_canonical_outcome_is_rejected_without_leak(tmp_path: Path) -> None:
    cases = _secret_cases()
    endpoint = cases["endpoint"]
    token = cases["token"]
    job, manifest, payload = _route_inputs()
    payload["payload"]["reason"] = f"unsafe endpoint {endpoint}"
    _write_outcome(tmp_path, payload)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest, secrets=(endpoint, token))

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)
    rendered = f"{captured.value!r} {captured.value} {captured.value.failure}"
    assert endpoint not in rendered
    assert token not in rendered
    assert captured.value.__context__ is None


def test_secret_crossing_a_canonical_outcome_chunk_is_rejected(tmp_path: Path) -> None:
    cases = _secret_cases()
    endpoint = cases["endpoint"]
    job, manifest, payload = _route_inputs()
    payload["payload"]["reason"] = endpoint
    marker_offset = canonical_json_bytes(payload).index(endpoint.encode())
    padding = 65_536 - marker_offset - 5
    assert padding > 0
    payload["payload"]["reason"] = "x" * padding + endpoint
    outcome_bytes = canonical_json_bytes(payload)
    secret_offset = outcome_bytes.index(endpoint.encode())
    assert secret_offset < 65_536 < secret_offset + len(endpoint.encode())
    _write_outcome(tmp_path, outcome_bytes)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest, secrets=(endpoint,))

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)
    assert endpoint not in str(captured.value)


def test_schema_invalid_secret_input_is_not_retained_as_exception_context(
    tmp_path: Path,
) -> None:
    token = _secret_cases()["token"]
    job, manifest, _ = _route_inputs()
    _write_outcome(tmp_path, canonical_json_bytes({"agent_value": token}))

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest, secrets=(token,))

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)
    assert token not in repr(captured.value)
    assert captured.value.__context__ is None


def test_secret_in_proposal_relative_path_is_rejected(tmp_path: Path) -> None:
    cases = _secret_cases()
    token = cases["token"]
    job, manifest, payload = _route_inputs()
    relative_path = f"output/proposals/diagnostic_export/{token}.bin"
    draft = _diagnostic_draft(relative_path=relative_path)
    payload["proposed_artifact_drafts"] = [draft]
    _write_outcome(tmp_path, payload)
    _write_file_proposal(tmp_path, relative_path, b"safe bytes")

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest, secrets=(token,))

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)


def test_secret_in_file_content_is_found_across_read_chunks(tmp_path: Path) -> None:
    cases = _secret_cases()
    endpoint = cases["endpoint"].encode()
    prefix = b"x" * cases["cross_chunk_prefix_bytes"]
    content = prefix + endpoint + b"-suffix"
    job, manifest, payload = _route_inputs()
    draft = _diagnostic_draft(
        declared_size=len(content),
        declared_sha256=hashlib.sha256(content).hexdigest(),
    )
    payload["proposed_artifact_drafts"] = [draft]
    _write_outcome(tmp_path, payload)
    _write_file_proposal(tmp_path, draft["workspace_relative_path"], content)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest, secrets=(endpoint,))

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)
    assert endpoint.decode() not in str(captured.value)


@pytest.mark.parametrize("secret_location", ["content", "path"])
def test_secret_in_directory_tree_content_or_path_is_rejected(
    tmp_path: Path,
    secret_location: str,
) -> None:
    cases = _secret_cases()
    token = cases["token"]
    job, manifest, payload = _route_inputs()
    root_relative = "output/proposals/diagnostic_export/tree"
    draft = _diagnostic_draft(
        relative_path=root_relative,
        resource_kind="DIRECTORY",
    )
    payload["proposed_artifact_drafts"] = [draft]
    _write_outcome(tmp_path, payload)
    if secret_location == "content":
        child = "nested/result.bin"
        content = b"left-" + token.encode() + b"-right"
    else:
        child = f"nested/{token}.bin"
        content = b"safe"
    _write_file_proposal(tmp_path, f"{root_relative}/{child}", content)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest, secrets=(token,))

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)


def test_partial_secret_bytes_do_not_false_positive(tmp_path: Path) -> None:
    cases = _secret_cases()
    token = cases["token"]
    partial = cases["safe_partial_token"].encode()
    job, manifest, payload = _route_inputs()
    draft = _diagnostic_draft(
        declared_size=len(partial),
        declared_sha256=hashlib.sha256(partial).hexdigest(),
    )
    payload["proposed_artifact_drafts"] = [draft]
    _write_outcome(tmp_path, payload)
    _write_file_proposal(tmp_path, draft["workspace_relative_path"], partial)

    result = read_agent_output(tmp_path, job, manifest, secrets=(token,))

    assert result.proposal_resources[0].size == len(partial)


def test_runtime_output_fixture_manifest_is_complete_and_hash_valid() -> None:
    manifest_path = FIXTURE_ROOT / "fixture-manifest.json"
    manifest = FixtureManifest.model_validate_json(manifest_path.read_bytes())
    assert manifest.owner_spec == "S04"
    assert manifest.root == "tests/fixtures/components/runtime-output"

    actual = {
        path.relative_to(FIXTURE_ROOT).as_posix(): path
        for path in FIXTURE_ROOT.rglob("*")
        if path.is_file() and path != manifest_path
    }
    assert [entry.path for entry in manifest.files] == sorted(actual)
    for entry in manifest.files:
        data = actual[entry.path].read_bytes()
        assert entry.size == len(data)
        assert entry.sha256 == hashlib.sha256(data).hexdigest()
