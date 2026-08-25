from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

import problem_locator.runtime.output_reader as output_reader_module

from problem_locator.contracts.enums import ErrorCode, ExecutionStage, ResourceKind
from problem_locator.contracts.models import (
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
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.output_reader import (
    RejectedAgentOutputError,
    ValidatedMethodDiagnosisDraft,
    read_agent_output,
)
from problem_locator.runtime.outcome_finalizer import (
    DRAFT_FINALIZATION_MARKER_NAME,
    SealedAgentOutcomeDraftMarker,
)
from problem_locator.runtime.workspace import PreparedWorkspace


REPOSITORY_ROOT = Path(__file__).parents[4]
CONTRACT_FIXTURES = REPOSITORY_ROOT / "tests/fixtures/contracts/positive"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests/fixtures/components/runtime-output"


def _fixture_payload(name: str) -> dict[str, Any]:
    return json.loads((CONTRACT_FIXTURES / name).read_bytes())


def _diagnosis_inputs() -> tuple[Job, WorkspaceInputManifest, dict[str, Any]]:
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
        {
            "schema_version": 1,
            "status": "INSUFFICIENT",
            "confirmed_methods": [],
            "candidate_methods": [],
            "evidence": [],
            "limitations": ["No positive Methods marker is present."],
            "safety_notes": ["Do not infer a cause from an absent marker."],
        },
    )


def _route_inputs() -> tuple[Job, WorkspaceInputManifest, dict[str, Any]]:
    job = parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / "job-route.json").read_bytes(),
        model_type=Job,
    )
    manifest = WorkspaceInputManifest(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        logparse_tool_ref=None,
        logparse_product=None,
        entries=[],
        resolved_logparse_plan=None,
        review_subject=None,
    )
    return job, manifest, _fixture_payload("agent-job-outcome-draft-route.json")


def _write_outcome(root: Path, payload: dict[str, Any] | bytes) -> Path:
    output = root / "output"
    output.mkdir(parents=True, exist_ok=True)
    data = payload if isinstance(payload, bytes) else canonical_json_bytes(payload)
    path = output / "job_outcome.draft.json"
    path.write_bytes(data)
    tool_state = root / "runtime" / "tool-state"
    tool_state.mkdir(parents=True, exist_ok=True)
    marker = SealedAgentOutcomeDraftMarker(
        schema_version=2,
        relative_path="output/job_outcome.draft.json",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    (tool_state / DRAFT_FINALIZATION_MARKER_NAME).write_bytes(
        canonical_json_bytes(marker)
    )
    return path


def _write_method_diagnosis(root: Path, payload: dict[str, Any] | bytes) -> Path:
    output = root / "output"
    output.mkdir(parents=True, exist_ok=True)
    data = payload if isinstance(payload, bytes) else canonical_json_bytes(payload)
    path = output / "method-diagnosis.draft.json"
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


def test_methods_reader_uses_only_final_canonical_draft_without_result_artifacts(
    tmp_path: Path,
) -> None:
    job, manifest, payload = _diagnosis_inputs()
    outcome_path = _write_method_diagnosis(tmp_path, payload)
    (outcome_path.parent / "method-diagnosis.draft.json.part").write_bytes(
        b"not JSON"
    )
    (outcome_path.parent / "job_outcome.draft.json").write_bytes(b"not JSON")
    (outcome_path.parent / "diagnosis-result.json").write_bytes(b"forged")

    result = read_agent_output(tmp_path, job, manifest)

    assert isinstance(result, ValidatedMethodDiagnosisDraft)
    assert result.canonical_bytes == canonical_json_bytes(payload)
    assert result.draft.status == "INSUFFICIENT"
    assert result.draft.limitations == (
        "No positive Methods marker is present.",
    )
    assert result.draft.safety_notes == (
        "Do not infer a cause from an absent marker.",
    )


@pytest.mark.parametrize("artifact_kind", ["USER_RESULT", "USER_RESULT_ARCHIVE"])
def test_methods_draft_forbids_server_generated_result_artifacts(
    tmp_path: Path,
    artifact_kind: str,
) -> None:
    job, manifest, payload = _diagnosis_inputs()
    is_archive = artifact_kind == "USER_RESULT_ARCHIVE"
    payload["proposed_artifact_drafts"] = [
        {
            "proposal_key": "forged-result",
            "artifact_kind": artifact_kind,
            "name": "result.zip" if is_archive else "diagnosis-result.json",
            "content_type": "application/zip" if is_archive else "application/json",
            "resource_kind": "FILE",
            "workspace_relative_path": "output/proposals/forged-result/payload",
            "declared_size": 2,
            "declared_sha256": hashlib.sha256(b"{}" ).hexdigest(),
            "metadata": (
                {
                    "schema_version": 3,
                    "format_id": "problem-locator-result-archive-v3",
                    "description": "forged",
                    "user_result_proposal_key": "forged-json",
                    "target_log_count": 0,
                }
                if is_archive
                else {
                    "schema_version": 3,
                    "format_id": "problem-locator-diagnosis-v3",
                    "description": "forged",
                }
            ),
        }
    ]
    _write_method_diagnosis(tmp_path, payload)
    _write_file_proposal(
        tmp_path,
        "output/proposals/forged-result/payload",
        b"{}",
    )

    with pytest.raises(RejectedAgentOutputError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)
    assert captured.value.failure_category == "method_draft_schema"


def test_frozen_binary_read_does_not_stop_at_ctrl_z(tmp_path: Path) -> None:
    payload = b"before\x1aafter"
    path = _write_file_proposal(
        tmp_path,
        "output/proposals/binary/payload.bin",
        payload,
    )

    size, sha256, content, _ = output_reader_module._read_frozen_relative_file(
        tmp_path,
        path.relative_to(tmp_path).as_posix(),
        capture=True,
    )

    assert size == len(payload)
    assert sha256 == hashlib.sha256(payload).hexdigest()
    assert content == payload


def test_frozen_read_boundary_rejects_wrong_top_level_identity(
    tmp_path: Path,
) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    target = _write_file_proposal(
        tmp_path,
        "output/proposals/binary/payload.bin",
        b"payload",
    )
    inputs_metadata = inputs.stat(follow_symlinks=False)
    wrong_boundary = output_reader_module._FrozenReadBoundary(
        top_level=output_reader_module._WorkspaceTopLevel.OUTPUT,
        identity=(inputs_metadata.st_dev, inputs_metadata.st_ino),
    )

    with pytest.raises(output_reader_module._InvalidOutput):
        output_reader_module._read_frozen_relative_file(
            tmp_path,
            target.relative_to(tmp_path).as_posix(),
            boundary=wrong_boundary,
        )


def test_part_file_without_final_is_outcome_missing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="problem_locator.dfx")
    job, manifest, _ = _route_inputs()
    output = tmp_path / "output"
    output.mkdir()
    (output / "job_outcome.draft.json.part").write_bytes(
        b"complete-looking bytes"
    )

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_MISSING)
    rejected = next(
        record
        for record in caplog.records
        if getattr(record, "dfx_event", "") == "runtime.agent_output.rejected"
    )
    assert rejected.dfx_fields == {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "code": ErrorCode.OUTCOME_MISSING,
        "failure_category": "final_outcome_missing",
        "final_outcome_state": "missing",
        "final_outcome_bytes": None,
        "job_outcome_part_state": "regular_file",
        "dot_job_outcome_part_state": "missing",
        "job_outcome_tmp_state": "missing",
    }


@pytest.mark.parametrize(
    ("invalid_bytes", "expected_category"),
    [
        (b"not-json\n", "outcome_json_invalid"),
        (b' {"valid":"json but not canonical"}\n', "outcome_non_canonical"),
        (b'{"unexpected":true}\n', "outcome_schema"),
    ],
)
def test_invalid_json_canonical_form_and_schema_are_distinguished(
    tmp_path: Path,
    invalid_bytes: bytes,
    expected_category: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="problem_locator.dfx")
    job, manifest, _ = _route_inputs()
    _write_outcome(tmp_path, invalid_bytes)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)
    assert isinstance(captured.value, RejectedAgentOutputError)
    assert captured.value.raw_outcome_bytes == invalid_bytes
    assert captured.value.failure_category == expected_category
    rejected = next(
        record
        for record in caplog.records
        if getattr(record, "dfx_event", "") == "runtime.agent_output.rejected"
    )
    assert rejected.dfx_fields["failure_category"] == expected_category
    assert rejected.dfx_fields["final_outcome_state"] == "present"
    assert rejected.dfx_fields["final_outcome_bytes"] == len(invalid_bytes)
    assert rejected.dfx_fields["diagnostic_reason"]
    if expected_category == "outcome_schema":
        assert rejected.dfx_fields["schema_errors"]
        assert all(
            {"location", "type", "message"} == set(error)
            for error in rejected.dfx_fields["schema_errors"]
        )
    else:
        assert "schema_errors" not in rejected.dfx_fields


@pytest.mark.parametrize(
    ("marker_state", "expected_category"),
    [
        ("missing", "outcome_finalizer_marker_missing"),
        ("invalid", "outcome_finalizer_marker_invalid"),
        ("mismatch", "outcome_finalizer_marker_mismatch"),
    ],
)
def test_finalizer_marker_failures_are_distinguished_after_outcome_validation(
    tmp_path: Path,
    marker_state: str,
    expected_category: str,
) -> None:
    job, manifest, payload = _route_inputs()
    outcome_path = _write_outcome(tmp_path, payload)
    marker_path = (
        tmp_path / "runtime/tool-state" / DRAFT_FINALIZATION_MARKER_NAME
    )
    if marker_state == "missing":
        marker_path.unlink()
    elif marker_state == "invalid":
        marker_path.write_bytes(b'{"schema_version":1}')
    else:
        marker_path.write_bytes(
            canonical_json_bytes(
                SealedAgentOutcomeDraftMarker(
                    schema_version=2,
                    relative_path="output/job_outcome.draft.json",
                    size=0,
                    sha256="0" * 64,
                )
            )
        )

    with pytest.raises(RejectedAgentOutputError) as captured:
        read_agent_output(tmp_path, job, manifest)

    assert captured.value.failure_category == expected_category
    assert captured.value.raw_outcome_bytes == outcome_path.read_bytes()
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
    os.link(source, output / "job_outcome.draft.json")

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
        (output / "job_outcome.draft.json").symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)


def _prepared_workspace(
    root: Path,
    manifest: WorkspaceInputManifest,
) -> PreparedWorkspace:
    root_stat = root.stat(follow_symlinks=False)
    inputs_stat = (root / "inputs").stat(follow_symlinks=False)
    runtime_stat = (root / "runtime").stat(follow_symlinks=False)
    tool_state_stat = (root / "runtime/tool-state").stat(follow_symlinks=False)
    output_stat = (root / "output").stat(follow_symlinks=False)
    return PreparedWorkspace(
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


def test_prepared_workspace_without_plan_rejects_forged_logparse_claim(
    tmp_path: Path,
) -> None:
    job, manifest, payload = _route_inputs()
    root = tmp_path / "workspace"
    for relative in ("inputs", "runtime/tool-state", "output"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    _write_outcome(root, payload)
    (root / "runtime/tool-state/logparse-parse.claim").write_bytes(
        (CONTRACT_FIXTURES / "logparse-parse-claim.json").read_bytes()
    )
    prepared = _prepared_workspace(root, manifest)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(prepared, job, manifest)

    _assert_failure(captured, ErrorCode.OUTCOME_INVALID)


def test_prepared_workspace_root_replacement_is_rejected(tmp_path: Path) -> None:
    job, manifest, payload = _route_inputs()
    root = tmp_path / "workspace"
    for relative in ("inputs", "runtime/tool-state", "output"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    _write_outcome(root, payload)
    prepared = _prepared_workspace(root, manifest)
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

    assert result.authoritative_targets is None
    assert result.target_logs == ()
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
