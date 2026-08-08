from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from problem_locator.contracts import (
    AttachmentFilenameSuffix,
    ArtifactKind,
    JobType,
    LogparseParseParameters,
    LogparseRunMetadata,
    ResourceKind,
    ResolvedLogparseAnchor,
    ResolvedLogparsePlanInput,
    UserResultMetadata,
    VersionedRef,
    WorkspaceArtifactInput,
    WorkspaceAttachmentInput,
    WorkspaceInputManifest,
    canonical_json_bytes,
    derive_attachment_filename_suffix,
    workspace_attachment_relative_path,
)
from problem_locator.integrations.logparse.outputs import (
    ControlledRun,
    inspect_controlled_run,
)
from problem_locator.integrations.logparse.workspace import (
    bind_attachment,
    bind_logparse_run,
    has_logparse_run,
    load_workspace_manifest,
)


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "components" / "logparse"
FAKE_CLI = FIXTURES / "fake" / "repo" / "cli.py"
FAKE_CONFIG = FIXTURES / "fake" / "repo" / "config.yaml"

JOB_ID = "00000000-0000-0000-0000-000000000101"
OTHER_JOB_ID = "00000000-0000-0000-0000-000000000102"
CASE_ID = "00000000-0000-0000-0000-000000000201"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000301"
OTHER_ATTACHMENT_ID = "00000000-0000-0000-0000-000000000302"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000401"
OTHER_ARTIFACT_ID = "00000000-0000-0000-0000-000000000402"
SOURCE_BYTES = b"synthetic plain attachment bytes; this is not an archive\n"
SOURCE_SHA256 = hashlib.sha256(SOURCE_BYTES).hexdigest()
TOOL_REF = VersionedRef(
    id="logparse-tool/logparse",
    version="sha256-0123456789abcdef",
    content_hash="0" * 64,
)
OTHER_TOOL_REF = VersionedRef(
    id="logparse-tool/logparse",
    version="sha256-fedcba9876543210",
    content_hash="1" * 64,
)


def _attachment_entry(
    *,
    attachment_id: str = ATTACHMENT_ID,
    payload: bytes = SOURCE_BYTES,
    content_type: str = "application/octet-stream",
    filename_suffix: AttachmentFilenameSuffix | None = None,
) -> WorkspaceAttachmentInput:
    return WorkspaceAttachmentInput(
        input_kind="ATTACHMENT",
        resource_id=attachment_id,
        relative_path=workspace_attachment_relative_path(
            attachment_id,
            filename_suffix,
        ),
        resource_kind=ResourceKind.FILE,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        content_type=content_type,
        filename_suffix=filename_suffix,
    )


def _manifest(
    entries: list[WorkspaceAttachmentInput | WorkspaceArtifactInput],
    *,
    tool_ref: VersionedRef = TOOL_REF,
    product: str = "compact",
) -> WorkspaceInputManifest:
    run_entry = next(
        (
            entry
            for entry in entries
            if isinstance(entry, WorkspaceArtifactInput)
            and entry.artifact_kind is ArtifactKind.LOGPARSE_RUN
        ),
        None,
    )
    attachment_entry = next(
        entry for entry in entries if isinstance(entry, WorkspaceAttachmentInput)
    )
    return WorkspaceInputManifest(
        schema_version=2,
        job_id=JOB_ID,
        case_id=CASE_ID,
        job_type=JobType.DIAGNOSE,
        logparse_tool_ref=tool_ref,
        logparse_product=product,
        entries=entries,
        resolved_logparse_plan=ResolvedLogparsePlanInput(
            schema_version=2,
            attachment_id=(
                attachment_entry.resource_id if run_entry is None else None
            ),
            artifact_id=(run_entry.resource_id if run_entry is not None else None),
            problem_time="2026-07-31T00:00:03.000Z",
            anchors=[
                ResolvedLogparseAnchor(
                    label="client",
                    module="COMPACT",
                    slot="1",
                    process_name="checkout-client",
                    pid="101",
                )
            ],
        ),
        review_subject=None,
    )


def _write_read_only(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return path


def _materialize_attachment(
    workspace: Path,
    entry: WorkspaceAttachmentInput,
    payload: bytes = SOURCE_BYTES,
) -> Path:
    return _write_read_only(workspace / entry.relative_path, payload)


def _write_manifest(
    workspace: Path,
    manifest: WorkspaceInputManifest,
    *,
    canonical: bool = True,
    read_only: bool = True,
) -> Path:
    path = workspace / "inputs" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(0o644)
    if canonical:
        payload = canonical_json_bytes(manifest)
    else:
        payload = (
            json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2)
            + "\n"
        ).encode("utf-8")
    path.write_bytes(payload)
    path.chmod(0o444 if read_only else 0o644)
    return path


def _make_tree_read_only(root: Path) -> None:
    paths = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for path in paths:
        if path.is_dir():
            path.chmod(0o555)
        else:
            path.chmod(0o444)
    root.chmod(0o555)


def _generate_run(
    workspace: Path,
    attachment_path: Path,
    *,
    artifact_id: str = ARTIFACT_ID,
) -> ControlledRun:
    root = workspace / "inputs" / "artifacts" / artifact_id / "tree"
    root.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            os.fspath(FAKE_CLI),
            "parse",
            os.fspath(attachment_path),
            "-c",
            os.fspath(FAKE_CONFIG),
            "-o",
            os.fspath(root),
            "--product",
            "compact",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    run = inspect_controlled_run(root, product="compact")
    _make_tree_read_only(root)
    assert inspect_controlled_run(root, product="compact") == run
    return run


def _run_entry(
    run: ControlledRun,
    *,
    artifact_id: str = ARTIFACT_ID,
    tool_ref: VersionedRef = TOOL_REF,
    product: str = "compact",
    source_attachment_id: str = ATTACHMENT_ID,
    source_attachment_sha256: str = SOURCE_SHA256,
    parse_manifest_relative_path: str | None = None,
    size: int | None = None,
    sha256: str | None = None,
) -> WorkspaceArtifactInput:
    frozen_sha256 = run.sha256 if sha256 is None else sha256
    metadata = LogparseRunMetadata(
        tree_manifest_sha256=frozen_sha256,
        logparse_version_ref=tool_ref,
        parse_manifest_relative_path=(
            run.parse_manifest_relative_path
            if parse_manifest_relative_path is None
            else parse_manifest_relative_path
        ),
        source_attachment_id=source_attachment_id,
        source_attachment_sha256=source_attachment_sha256,
        parse_parameters=LogparseParseParameters(product=product),
    )
    return WorkspaceArtifactInput(
        input_kind="ARTIFACT",
        resource_id=artifact_id,
        relative_path=f"inputs/artifacts/{artifact_id}/tree",
        resource_kind=ResourceKind.DIRECTORY,
        size=run.size if size is None else size,
        sha256=frozen_sha256,
        artifact_kind=ArtifactKind.LOGPARSE_RUN,
        name="synthetic logparse run",
        content_type="application/vnd.problem-locator.logparse-run+directory",
        metadata=metadata,
    )


def _valid_workspace(
    tmp_path: Path,
) -> tuple[
    Path,
    WorkspaceAttachmentInput,
    WorkspaceArtifactInput,
    WorkspaceInputManifest,
    ControlledRun,
]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    attachment = _attachment_entry()
    attachment_path = _materialize_attachment(workspace, attachment)
    run = _generate_run(workspace, attachment_path)
    artifact = _run_entry(run)
    manifest = _manifest([attachment, artifact])
    _write_manifest(workspace, manifest)
    return workspace, attachment, artifact, manifest, run


def test_load_manifest_requires_fixed_canonical_read_only_bytes_and_object_equality(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    attachment = _attachment_entry()
    manifest = _manifest([attachment])
    path = _write_manifest(workspace, manifest)

    loaded = load_workspace_manifest(workspace, manifest)

    assert loaded == manifest
    assert path.read_bytes() == canonical_json_bytes(manifest)

    path.chmod(0o644)
    with pytest.raises(ValueError, match="read-only plain file"):
        load_workspace_manifest(workspace, manifest)

    _write_manifest(workspace, manifest, canonical=False)
    with pytest.raises(ValueError, match="canonical"):
        load_workspace_manifest(workspace, manifest)

    _write_manifest(workspace, manifest)
    different_runtime_object = manifest.model_copy(update={"job_id": OTHER_JOB_ID})
    with pytest.raises(ValueError, match="differs from the Runtime binding"):
        load_workspace_manifest(workspace, different_runtime_object)


def test_manifest_and_attachment_binding_never_scan_or_guess_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    attachment = _attachment_entry()
    manifest = _manifest([attachment])
    expected_path = _materialize_attachment(workspace, attachment)
    _write_manifest(workspace, manifest)
    _write_read_only(
        workspace / f"inputs/attachments/{OTHER_ATTACHMENT_ID}/payload",
        SOURCE_BYTES,
    )
    _write_read_only(workspace / "inputs/guessed-log.bin", SOURCE_BYTES)

    def forbidden_scan(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Workspace input discovery is forbidden")

    monkeypatch.setattr(Path, "glob", forbidden_scan)
    monkeypatch.setattr(Path, "rglob", forbidden_scan)
    monkeypatch.setattr(Path, "iterdir", forbidden_scan)
    monkeypatch.setattr(os, "scandir", forbidden_scan)

    loaded = load_workspace_manifest(workspace, manifest)
    bound = bind_attachment(workspace, loaded, ATTACHMENT_ID)

    assert bound.entry == attachment
    assert bound.path == expected_path.resolve()
    with pytest.raises(ValueError, match="not fixed"):
        bind_attachment(workspace, loaded, OTHER_ATTACHMENT_ID)


def test_bind_attachment_uses_exact_fixed_id_path_size_and_hash(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    attachment = _attachment_entry()
    manifest = _manifest([attachment])
    path = _materialize_attachment(workspace, attachment)

    bound = bind_attachment(workspace, manifest, ATTACHMENT_ID)

    assert bound.entry.resource_id == ATTACHMENT_ID
    assert bound.entry.relative_path == f"inputs/attachments/{ATTACHMENT_ID}/payload"
    assert bound.entry.size == len(SOURCE_BYTES)
    assert bound.entry.sha256 == SOURCE_SHA256
    assert bound.path == path.resolve()
    with pytest.raises(ValueError, match="not fixed"):
        bind_attachment(workspace, manifest, OTHER_ATTACHMENT_ID)


@pytest.mark.parametrize(
    ("name", "content_type", "filename_suffix"),
    [
        ("logs.zip", "application/zip", AttachmentFilenameSuffix.ZIP),
        ("logs.tar", "application/x-tar", AttachmentFilenameSuffix.TAR),
        ("logs.gz", "application/gzip", AttachmentFilenameSuffix.GZ),
        ("logs.tar.gz", "application/gzip", AttachmentFilenameSuffix.TAR_GZ),
        ("logs.tgz", "application/gzip", AttachmentFilenameSuffix.TGZ),
    ],
)
def test_bind_attachment_uses_exact_manifest_archive_path_for_every_frozen_suffix(
    tmp_path: Path,
    name: str,
    content_type: str,
    filename_suffix: AttachmentFilenameSuffix,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    derived_suffix = derive_attachment_filename_suffix(name, content_type)
    assert derived_suffix is filename_suffix
    attachment = _attachment_entry(
        content_type=content_type,
        filename_suffix=derived_suffix,
    )
    manifest = _manifest([attachment])
    exact_path = _materialize_attachment(workspace, attachment)

    bound = bind_attachment(workspace, manifest, ATTACHMENT_ID)

    assert attachment.relative_path == workspace_attachment_relative_path(
        ATTACHMENT_ID,
        filename_suffix,
    )
    assert bound.path == exact_path.resolve()
    assert bound.path.name == f"payload{filename_suffix.value}"
    if filename_suffix is AttachmentFilenameSuffix.TAR_GZ:
        assert bound.path.name == "payload.tar.gz"


@pytest.mark.parametrize(
    ("content_type", "filename_suffix"),
    [
        ("application/gzip", AttachmentFilenameSuffix.ZIP),
        ("application/zip", AttachmentFilenameSuffix.GZ),
        ("application/x-tar", AttachmentFilenameSuffix.TGZ),
        ("text/plain", AttachmentFilenameSuffix.TAR),
    ],
)
def test_attachment_entry_rejects_content_type_suffix_matrix_drift(
    content_type: str,
    filename_suffix: AttachmentFilenameSuffix,
) -> None:
    with pytest.raises(ValueError, match="filename_suffix"):
        _attachment_entry(
            content_type=content_type,
            filename_suffix=filename_suffix,
        )


def test_bind_attachment_accepts_s02_read_only_hardlink_materialization(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    attachment = _attachment_entry()
    manifest = _manifest([attachment])
    source = _write_read_only(tmp_path / "resource-store-payload", SOURCE_BYTES)
    path = workspace / attachment.relative_path
    path.parent.mkdir(parents=True)
    try:
        os.link(source, path)
    except OSError:
        pytest.skip("hard links are unavailable on this platform")

    bound = bind_attachment(workspace, manifest, ATTACHMENT_ID)

    assert bound.path == path.resolve()
    assert path.stat().st_nlink == 2


@pytest.mark.parametrize("fault", ["writable", "symlink", "drift"])
def test_bind_attachment_rejects_mutable_symlinked_or_drifted_materialization(
    tmp_path: Path,
    fault: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    attachment = _attachment_entry()
    manifest = _manifest([attachment])
    path = _materialize_attachment(workspace, attachment)

    if fault == "writable":
        path.chmod(0o644)
    elif fault == "symlink":
        external = _write_read_only(tmp_path / "external.bin", SOURCE_BYTES)
        if os.name == "nt":
            path.chmod(0o644)
        path.unlink()
        try:
            path.symlink_to(external)
        except NotImplementedError as exc:
            pytest.skip(f"symbolic link creation is unavailable: {exc}")
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                pytest.skip(
                    "symbolic link creation requires Windows developer mode"
                )
            raise
    elif fault == "drift":
        path.chmod(0o644)
        path.write_bytes(b"different bytes after the manifest was frozen\n")
        path.chmod(0o444)
    else:  # pragma: no cover - the parametrization is exhaustive
        raise AssertionError(f"unknown fault: {fault}")

    with pytest.raises(ValueError):
        bind_attachment(workspace, manifest, ATTACHMENT_ID)


def test_has_logparse_run_detects_any_logparse_artifact(tmp_path: Path) -> None:
    workspace, attachment, artifact, manifest, _run = _valid_workspace(tmp_path)

    assert has_logparse_run(manifest) is True
    assert has_logparse_run(_manifest([attachment])) is False
    assert artifact.resource_id == ARTIFACT_ID
    assert workspace.is_dir()


def test_bind_logparse_run_accepts_only_the_requested_artifact_id_and_full_tree(
    tmp_path: Path,
) -> None:
    workspace, _attachment, artifact, manifest, run = _valid_workspace(tmp_path)

    bound = bind_logparse_run(workspace, manifest, ARTIFACT_ID)

    assert bound.entry == artifact
    assert bound.metadata == artifact.metadata
    assert bound.run == run
    assert all(
        path.stat(follow_symlinks=False).st_mode & 0o222 == 0
        for path in [run.root, *run.root.rglob("*")]
    )
    with pytest.raises(ValueError, match="not fixed"):
        bind_logparse_run(workspace, manifest, OTHER_ARTIFACT_ID)


def test_bind_logparse_run_rejects_a_non_logparse_artifact_with_the_target_id(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    attachment = _attachment_entry()
    _materialize_attachment(workspace, attachment)
    payload = b"{}\n"
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    user_result = WorkspaceArtifactInput(
        input_kind="ARTIFACT",
        resource_id=ARTIFACT_ID,
        relative_path=f"inputs/artifacts/{ARTIFACT_ID}/payload",
        resource_kind=ResourceKind.FILE,
        size=len(payload),
        sha256=payload_sha256,
        artifact_kind=ArtifactKind.USER_RESULT,
        name="diagnosis-result.json",
        content_type="application/json",
        metadata=UserResultMetadata(
            schema_version=1,
            format_id="problem-locator-diagnosis-v1",
            description="synthetic result",
        ),
    )
    _write_read_only(workspace / user_result.relative_path, payload)  # type: ignore[attr-defined]
    manifest = _manifest([attachment, user_result])  # type: ignore[list-item]

    with pytest.raises(ValueError, match="LOGPARSE_RUN is not fixed"):
        bind_logparse_run(workspace, manifest, ARTIFACT_ID)


@pytest.mark.parametrize(
    "fault",
    [
        "tool_ref",
        "product",
        "source_attachment_id",
        "source_attachment_sha256",
        "parse_manifest_relative_path",
    ],
)
def test_continuation_rejects_mismatched_logparse_metadata(
    tmp_path: Path,
    fault: str,
) -> None:
    workspace, attachment, artifact, _manifest_value, run = _valid_workspace(tmp_path)
    metadata = artifact.metadata
    assert isinstance(metadata, LogparseRunMetadata)
    updates: dict[str, object] = {
        "tool_ref": {"logparse_version_ref": OTHER_TOOL_REF},
        "product": {
            "parse_parameters": LogparseParseParameters(product="default")
        },
        "source_attachment_id": {"source_attachment_id": OTHER_ATTACHMENT_ID},
        "source_attachment_sha256": {"source_attachment_sha256": "f" * 64},
        "parse_manifest_relative_path": {
            "parse_manifest_relative_path": "task-synthetic/result.json"
        },
    }[fault]  # type: ignore[assignment]
    bad_metadata = metadata.model_copy(update=updates)
    bad_artifact = artifact.model_copy(update={"metadata": bad_metadata})
    manifest = _manifest([attachment, bad_artifact])

    with pytest.raises(ValueError):
        bind_logparse_run(workspace, manifest, ARTIFACT_ID)

    assert run.root.is_dir()


@pytest.mark.parametrize(
    "fault",
    ["declared_size", "declared_hash", "tree_content", "missing_tree_file"],
)
def test_continuation_revalidates_complete_tree_size_and_hash(
    tmp_path: Path,
    fault: str,
) -> None:
    workspace, attachment, artifact, _manifest_value, run = _valid_workspace(tmp_path)

    if fault == "declared_size":
        artifact = artifact.model_copy(update={"size": run.size + 1})
    elif fault == "declared_hash":
        wrong_hash = "f" * 64
        metadata = artifact.metadata
        assert isinstance(metadata, LogparseRunMetadata)
        artifact = artifact.model_copy(
            update={
                "sha256": wrong_hash,
                "metadata": metadata.model_copy(
                    update={"tree_manifest_sha256": wrong_hash}
                ),
            }
        )
    elif fault in {"tree_content", "missing_tree_file"}:
        log_path = (
            run.root
            / "task-synthetic"
            / "mech_modules"
            / "COMPACT"
            / "slot_1"
            / "cycle"
            / "checkout-client-101.log"
        )
        log_path.parent.chmod(0o755)
        if fault == "tree_content":
            log_path.chmod(0o644)
            log_path.write_bytes(b"changed controlled output bytes\n")
            log_path.chmod(0o444)
        else:
            if os.name == "nt":
                log_path.chmod(0o644)
            log_path.unlink()
        log_path.parent.chmod(0o555)
    else:  # pragma: no cover - the parametrization is exhaustive
        raise AssertionError(f"unknown fault: {fault}")

    manifest = _manifest([attachment, artifact])

    with pytest.raises(ValueError):
        bind_logparse_run(workspace, manifest, ARTIFACT_ID)
