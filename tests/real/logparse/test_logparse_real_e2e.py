"""Opt-in end-to-end proof against the pinned real logparse installation.

The fixture is decoded only to materialize the opaque Attachment bytes.  This
test never opens, lists, or interprets the archive; all archive handling and
target selection are performed by the real logparse CLI through the job-scoped
broker and the installed Agent stub.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from problem_locator.contracts import (
    AttachmentFilenameSuffix,
    ArtifactKind,
    AssetKind,
    Job,
    LogparseParseClaim,
    LogparseParseParameters,
    LogparseRunMetadata,
    ResourceKind,
    WorkspaceArtifactInput,
    WorkspaceAttachmentInput,
    WorkspaceInputManifest,
    canonical_json_bytes,
    parse_canonical_json_bytes,
    validate_logparse_claim_for_job,
    workspace_attachment_relative_path,
)
from problem_locator.integrations.logparse import (
    Anchor,
    ParseTargetsRequest,
    TargetLogsRequest,
    build_logparse_runtime,
)
from problem_locator.integrations.logparse.cli import run as run_agent_stub
from problem_locator.integrations.logparse.outputs import inspect_controlled_run
from tests.deterministic.contracts.fakes import InMemoryCancellationSignal
from tests.v2_helpers import resolved_logparse_plan


_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_JOB_TEMPLATE = (
    _REPOSITORY_ROOT / "tests/fixtures/contracts/positive/job-diagnose.json"
)
_ARCHIVE_FIXTURE = (
    _REPOSITORY_ROOT
    / "tests/fixtures/components/logparse/real/synthetic-rpc-service-takeover.zip.b64"
)

_RAW_CONFIGURATION_KEYS = (
    "LOGPARSE_REPO",
    "LOGPARSE_CONFIG_PATH",
    "LOGPARSE_PYTHON",
)
_ENDPOINT_KEY = "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT"
_TOKEN_KEY = "PROBLEM_LOCATOR_LOGPARSE_TOKEN"

_CASE_ID = "00000000-0000-0000-0000-000000000001"
_FIRST_JOB_ID = "00000000-0000-0000-0000-000000000071"
_CONTINUATION_JOB_ID = "00000000-0000-0000-0000-000000000072"
_ATTACHMENT_ID = "00000000-0000-0000-0000-000000000073"
_ARTIFACT_ID = "00000000-0000-0000-0000-000000000074"
_INPUT_SOURCE_ID = "00000000-0000-0000-0000-000000000075"

_PRODUCT = "compact"
_PROBLEM_TIME = "2026-07-31T00:00:03.000Z"
_PARAMETERS_A = {
    "caller_service": "checkout-synthetic",
    "server_service": "inventory-synthetic",
    "rpc_method": "ReserveStock",
    "problem_time": _PROBLEM_TIME,
}
_PARAMETER_B = {"order_id": "synthetic-order-0001"}
_ARCHIVE_SHA256 = "194f69fecd8dc8d40d1aedeb6fc25d2b7b4922b176be2b15be73ffe386cc5064"
_RESOLVED_ANCHORS = [
    {
        "label": "client",
        "module": "COMPACT",
        "slot": "1",
        "process_name": "checkout-client",
        "pid": "101",
    },
    {
        "label": "server",
        "module": "COMPACT",
        "slot": "2",
        "process_name": "inventory-server",
        "pid": "202",
    },
]


def _configured_real_logparse(pytestconfig: pytest.Config) -> tuple[Path, Path, Path]:
    if not pytestconfig.getoption("--run-real-logparse"):
        pytest.skip("real logparse validation requires --run-real-logparse")

    missing = [name for name in _RAW_CONFIGURATION_KEYS if not os.environ.get(name)]
    if missing:
        pytest.fail(
            "--run-real-logparse requires explicit environment values for: "
            + ", ".join(missing),
            pytrace=False,
        )
    return tuple(Path(os.environ[name]) for name in _RAW_CONFIGURATION_KEYS)  # type: ignore[return-value]


def _materialize_archive(workspace: Path, archive_bytes: bytes) -> Path:
    path = workspace / workspace_attachment_relative_path(
        _ATTACHMENT_ID,
        AttachmentFilenameSuffix.ZIP,
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(archive_bytes)
    path.chmod(0o444)
    return path


def _materialize_manifest(
    workspace: Path,
    manifest: WorkspaceInputManifest,
) -> None:
    path = workspace / "inputs/manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(manifest))
    path.chmod(0o444)


def _write_request(workspace: Path, proposal_key: str, request: object) -> tuple[str, str]:
    proposal_root = workspace / f"output/proposals/{proposal_key}"
    proposal_root.mkdir(parents=True)
    request_path = f"output/proposals/{proposal_key}/request.json"
    result_path = f"output/proposals/{proposal_key}/target_logs.json"
    (workspace / request_path).write_bytes(canonical_json_bytes(request))
    return request_path, result_path


def _user_fact(
    *,
    item_id: str,
    name: str,
    value: str,
    revision: int,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "statement": value,
        "status": "ACTIVE",
        "provenance": {
            "source_type": "USER_INPUT",
            "source_ref": _INPUT_SOURCE_ID,
            "input_name": name,
        },
        "evidence_refs": [],
        "created_revision": revision,
        "supersedes": [],
    }


def _job(
    *,
    job_id: str,
    logparse_tool_ref: object,
    artifact_refs: list[str],
    continuation: bool,
) -> Job:
    payload = json.loads(_JOB_TEMPLATE.read_bytes())
    payload.update(
        {
            "job_id": job_id,
            "case_id": _CASE_ID,
            "goal": (
                "Continue with order_id using the immutable LOGPARSE_RUN."
                if continuation
                else "Parse the one fixed archive using parameter group A."
            ),
            "evidence_refs": [],
            "attachment_refs": [_ATTACHMENT_ID],
            "artifact_refs": artifact_refs,
            "previous_outcome_refs": [],
            "logparse_tool_ref": logparse_tool_ref,
            "logparse_product": _PRODUCT,
            "base_state_revision": 3 if continuation else 2,
        }
    )
    snapshot = payload["context_snapshot"]
    snapshot["diagnosis_state_revision"] = 3 if continuation else 2
    snapshot["evidence_refs"] = []
    values = _PARAMETERS_A | (_PARAMETER_B if continuation else {})
    snapshot["user_facts"] = [
        _user_fact(
            item_id=f"00000000-0000-0000-0000-{80 + index:012d}",
            name=name,
            value=value,
            revision=3 if name in _PARAMETER_B else 2,
        )
        for index, (name, value) in enumerate(values.items())
    ]
    return Job.model_validate(payload)


def _input_values(job: Job) -> dict[str, str]:
    return {
        item.provenance.input_name: item.statement
        for item in job.context_snapshot.user_facts
        if item.provenance.input_name is not None
    }


def _attachment_entry(archive_bytes: bytes) -> WorkspaceAttachmentInput:
    return WorkspaceAttachmentInput(
        input_kind="ATTACHMENT",
        resource_id=_ATTACHMENT_ID,
        relative_path=workspace_attachment_relative_path(
            _ATTACHMENT_ID,
            AttachmentFilenameSuffix.ZIP,
        ),
        resource_kind=ResourceKind.FILE,
        size=len(archive_bytes),
        sha256=hashlib.sha256(archive_bytes).hexdigest(),
        content_type="application/zip",
        filename_suffix=AttachmentFilenameSuffix.ZIP,
    )


def _first_manifest(job: Job, archive_bytes: bytes) -> WorkspaceInputManifest:
    return WorkspaceInputManifest(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        logparse_tool_ref=job.logparse_tool_ref,
        logparse_product=job.logparse_product,
        entries=[_attachment_entry(archive_bytes)],
        resolved_logparse_plan=resolved_logparse_plan(
            job,
            problem_time=_PROBLEM_TIME,
            anchors=_RESOLVED_ANCHORS,
        ),
        review_subject=None,
    )


def _continuation_manifest(
    job: Job,
    archive_bytes: bytes,
    *,
    run_size: int,
    run_sha256: str,
    parse_manifest_relative_path: str,
) -> WorkspaceInputManifest:
    assert job.logparse_tool_ref is not None
    metadata = LogparseRunMetadata(
        tree_manifest_sha256=run_sha256,
        logparse_version_ref=job.logparse_tool_ref,
        parse_manifest_relative_path=parse_manifest_relative_path,
        source_attachment_id=_ATTACHMENT_ID,
        source_attachment_sha256=hashlib.sha256(archive_bytes).hexdigest(),
        parse_parameters=LogparseParseParameters(product=_PRODUCT),
    )
    artifact = WorkspaceArtifactInput(
        input_kind="ARTIFACT",
        resource_id=_ARTIFACT_ID,
        relative_path=f"inputs/artifacts/{_ARTIFACT_ID}/tree",
        resource_kind=ResourceKind.DIRECTORY,
        size=run_size,
        sha256=run_sha256,
        artifact_kind=ArtifactKind.LOGPARSE_RUN,
        name="synthetic-rpc-logparse-run",
        content_type="application/vnd.problem-locator.logparse-run+directory",
        metadata=metadata,
    )
    return WorkspaceInputManifest(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        logparse_tool_ref=job.logparse_tool_ref,
        logparse_product=job.logparse_product,
        entries=[_attachment_entry(archive_bytes), artifact],
        resolved_logparse_plan=resolved_logparse_plan(
            job,
            problem_time=_PROBLEM_TIME,
            anchors=_RESOLVED_ANCHORS,
        ),
        review_subject=None,
    )


def _make_tree_read_only(root: Path) -> None:
    for directory, directory_names, file_names in os.walk(root, topdown=False):
        directory_path = Path(directory)
        for name in file_names:
            (directory_path / name).chmod(0o444)
        for name in directory_names:
            (directory_path / name).chmod(0o555)
        directory_path.chmod(0o555)


def _assert_agent_environment(
    environment: dict[str, str],
    raw_configuration: tuple[Path, Path, Path],
) -> None:
    assert set(environment) == {_ENDPOINT_KEY, _TOKEN_KEY}
    assert all(key not in environment for key in _RAW_CONFIGURATION_KEYS)
    exposed = "\n".join(environment.values())
    assert all(os.fspath(value.resolve()) not in exposed for value in raw_configuration)


def _assert_dual_anchor_result(payload: dict[str, Any], run_root: Path) -> None:
    assert payload["schema_version"] == 1
    assert payload["api_version"] == 1
    targets = payload["target_logs"]
    assert len(targets) == 2
    assert [target["label"] for target in targets] == ["client", "server"]
    assert [target["module_name"] for target in targets] == ["COMPACT", "COMPACT"]
    assert [str(target["slot"]).removeprefix("slot_") for target in targets] == [
        "1",
        "2",
    ]
    assert [target["process_name"] for target in targets] == [
        "checkout-client",
        "inventory-server",
    ]
    assert [target["pid"] for target in targets] == ["101", "202"]
    assert [target["match_status"] for target in targets] == ["exact", "exact"]
    for target in targets:
        relative_path = Path(target["log_path"])
        assert not relative_path.is_absolute()
        resolved = (run_root / relative_path).resolve(strict=True)
        assert resolved.is_relative_to(run_root.resolve(strict=True))
        assert stat.S_ISREG(resolved.stat(follow_symlinks=False).st_mode)


def _tracked_real_cli(
    real_popen: type[subprocess.Popen[bytes]],
    logparse_repo: Path,
    invocations: list[tuple[str, ...]],
):
    def tracked(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, Sequence) and not isinstance(command, (str, bytes)):
            argv = tuple(os.fspath(value) for value in command)
            if len(argv) >= 3 and Path(argv[1]) == logparse_repo / "cli.py":
                invocations.append(argv)
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    return tracked


def test_real_parse_then_parameter_b_continuation_parses_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pytestconfig: pytest.Config,
) -> None:
    logparse_repo, logparse_config, logparse_python = _configured_real_logparse(
        pytestconfig
    )
    encoded = b"".join(_ARCHIVE_FIXTURE.read_bytes().split())
    archive_bytes = base64.b64decode(encoded, validate=True)
    assert hashlib.sha256(archive_bytes).hexdigest() == _ARCHIVE_SHA256

    asset, factory = build_logparse_runtime(
        logparse_repo,
        logparse_config,
        logparse_python,
    )
    assert asset.asset_kind is AssetKind.LOGPARSE_TOOL

    for key in _RAW_CONFIGURATION_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")

    anchors = [
        Anchor(
            label="client",
            module="COMPACT",
            slot="1",
            process_name="checkout-client",
            pid="101",
        ),
        Anchor(
            label="server",
            module="COMPACT",
            slot="2",
            process_name="inventory-server",
            pid="202",
        ),
    ]
    first_job = _job(
        job_id=_FIRST_JOB_ID,
        logparse_tool_ref=asset.ref.model_dump(mode="json"),
        artifact_refs=[],
        continuation=False,
    )
    assert _input_values(first_job) == _PARAMETERS_A
    first_workspace = tmp_path / "first-job"
    first_workspace.mkdir()
    first_archive_path = _materialize_archive(first_workspace, archive_bytes)
    first_manifest = _first_manifest(first_job, archive_bytes)
    assert (
        first_archive_path
        == first_workspace / first_manifest.entries[0].relative_path
    )
    _materialize_manifest(first_workspace, first_manifest)
    parse_request = ParseTargetsRequest(
        schema_version=1,
        problem_time=_PROBLEM_TIME,
        anchors=anchors,
        attachment_id=_ATTACHMENT_ID,
        artifact_proposal_key="logparse-run",
    )
    parse_request_path, parse_result_path = _write_request(
        first_workspace,
        "logparse-run",
        parse_request,
    )
    parse_request_bytes = canonical_json_bytes(parse_request)
    claim_path = first_workspace / "runtime/tool-state/logparse-parse.claim"
    assert not claim_path.exists()

    session = factory.open(
        first_job,
        first_workspace,
        first_manifest,
        InMemoryCancellationSignal(),
    )
    environment = session.agent_environment()
    raw_configuration = (logparse_repo, logparse_config, logparse_python)
    _assert_agent_environment(environment, raw_configuration)
    real_popen = subprocess.Popen
    invocations: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        subprocess,
        "Popen",
        _tracked_real_cli(real_popen, logparse_repo.resolve(), invocations),
    )
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(first_workspace)
    try:
        assert run_agent_stub(
            "parse-targets",
            parse_request_path,
            parse_result_path,
        ) is None
        accepted_request_bytes = session.parse_request_bytes()
        assert accepted_request_bytes == parse_request_bytes
        assert claim_path.is_file()
        claim = parse_canonical_json_bytes(
            claim_path.read_bytes(),
            LogparseParseClaim,
        )
        assert (
            validate_logparse_claim_for_job(
                claim,
                first_job,
                first_manifest,
                accepted_request_bytes,
            )
            == claim
        )
        with pytest.raises(ValueError, match="request bytes require a parse claim"):
            validate_logparse_claim_for_job(
                None,
                first_job,
                first_manifest,
                accepted_request_bytes,
            )
    finally:
        session.close()
    closed_request_bytes = session.parse_request_bytes()
    assert closed_request_bytes == parse_request_bytes
    assert (
        validate_logparse_claim_for_job(
            claim,
            first_job,
            first_manifest,
            closed_request_bytes,
        )
        == claim
    )
    with pytest.raises(RuntimeError, match="closed"):
        session.agent_environment()

    first_result_bytes = (first_workspace / parse_result_path).read_bytes()
    first_result = parse_canonical_json_bytes(first_result_bytes)
    assert isinstance(first_result, dict)
    assert first_result_bytes == canonical_json_bytes(first_result)
    first_run = inspect_controlled_run(
        first_workspace / "output/proposals/logparse-run/tree",
        product=_PRODUCT,
    )
    _assert_dual_anchor_result(first_result, first_run.root)
    assert first_result["logparse_run_artifact_draft"] == {
        "artifact_kind": "LOGPARSE_RUN",
        "content_type": "application/vnd.problem-locator.logparse-run+directory",
        "declared_sha256": None,
        "declared_size": None,
        "metadata": {
            "logparse_version_ref": asset.ref.model_dump(mode="json"),
            "parse_manifest_relative_path": first_run.parse_manifest_relative_path,
            "parse_parameters": {"product": _PRODUCT},
            "source_attachment_id": _ATTACHMENT_ID,
            "source_attachment_sha256": hashlib.sha256(archive_bytes).hexdigest(),
            "tree_manifest_sha256": first_run.sha256,
        },
        "name": "logparse-run",
        "proposal_key": "logparse-run",
        "resource_kind": "DIRECTORY",
        "workspace_relative_path": "output/proposals/logparse-run/tree",
    }

    assert claim.job_id == first_job.job_id
    assert claim.attachment_id == _ATTACHMENT_ID
    assert claim.artifact_proposal_key == "logparse-run"
    assert claim.logparse_tool_ref == asset.ref
    assert claim.request_sha256 == hashlib.sha256(parse_request_bytes).hexdigest()

    continuation_job = _job(
        job_id=_CONTINUATION_JOB_ID,
        logparse_tool_ref=asset.ref.model_dump(mode="json"),
        artifact_refs=[_ARTIFACT_ID],
        continuation=True,
    )
    assert _input_values(continuation_job) == _PARAMETERS_A | _PARAMETER_B
    continuation_workspace = tmp_path / "continuation-job"
    continuation_workspace.mkdir()
    continuation_archive_path = _materialize_archive(
        continuation_workspace,
        archive_bytes,
    )
    materialized_run = (
        continuation_workspace / f"inputs/artifacts/{_ARTIFACT_ID}/tree"
    )
    materialized_run.parent.mkdir(parents=True)
    shutil.copytree(first_run.root, materialized_run)
    _make_tree_read_only(materialized_run)
    continuation_manifest = _continuation_manifest(
        continuation_job,
        archive_bytes,
        run_size=first_run.size,
        run_sha256=first_run.sha256,
        parse_manifest_relative_path=first_run.parse_manifest_relative_path,
    )
    assert (
        continuation_archive_path
        == continuation_workspace / continuation_manifest.entries[0].relative_path
    )
    _materialize_manifest(continuation_workspace, continuation_manifest)
    target_request = TargetLogsRequest(
        schema_version=1,
        problem_time=_PROBLEM_TIME,
        anchors=anchors,
        artifact_id=_ARTIFACT_ID,
    )
    assert set(target_request.model_dump(mode="json")) == {
        "schema_version",
        "problem_time",
        "anchors",
        "artifact_id",
    }
    target_request_path, target_result_path = _write_request(
        continuation_workspace,
        "continuation",
        target_request,
    )

    continuation_session = factory.open(
        continuation_job,
        continuation_workspace,
        continuation_manifest,
        InMemoryCancellationSignal(),
    )
    continuation_environment = continuation_session.agent_environment()
    _assert_agent_environment(continuation_environment, raw_configuration)
    for key, value in continuation_environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(continuation_workspace)
    workspace_mode = stat.S_IMODE(continuation_workspace.stat().st_mode)
    continuation_workspace.chmod(workspace_mode & ~0o222)
    try:
        assert run_agent_stub(
            "target-logs",
            target_request_path,
            target_result_path,
        ) is None
        assert continuation_session.parse_request_bytes() is None
    finally:
        continuation_workspace.chmod(workspace_mode)
        continuation_session.close()
    assert continuation_session.parse_request_bytes() is None
    assert not (continuation_workspace / "runtime/tool-state/logparse-parse.claim").exists()

    continuation_result_bytes = (
        continuation_workspace / target_result_path
    ).read_bytes()
    continuation_result = parse_canonical_json_bytes(continuation_result_bytes)
    assert isinstance(continuation_result, dict)
    _assert_dual_anchor_result(continuation_result, materialized_run)
    assert "logparse_run_artifact_draft" not in continuation_result
    first_targets_only = dict(first_result)
    first_targets_only.pop("logparse_run_artifact_draft")
    assert continuation_result == first_targets_only

    real_operations = [argv[2] for argv in invocations]
    assert {argv[0] for argv in invocations} == {
        os.fspath(Path(os.path.abspath(logparse_python)))
    }
    assert real_operations == [
        "parse",
        "mech-target-logs",
        "mech-target-logs",
        "mech-target-logs",
        "mech-target-logs",
    ]
    assert real_operations.count("parse") == 1
