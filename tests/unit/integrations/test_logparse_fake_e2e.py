from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from problem_locator.contracts import (
    AgentArtifactProposalDraft,
    AgentJobOutcome,
    AttachmentFilenameSuffix,
    ArtifactProposal,
    ArtifactKind,
    AssetKind,
    DiagnosisOutcome,
    DiagnosisStateDelta,
    ErrorCode,
    ExecutionFailure,
    ExecutionStage,
    InputRequirementConstraints,
    Job,
    JobOutcome,
    JobType,
    LogparseBrokerError,
    LogparseBrokerFactory,
    LogparseBrokerSession,
    LogparseParseClaim,
    LogparseParseParameters,
    LogparseRunMetadata,
    OutcomeResultType,
    PendingRequirement,
    RequirementKind,
    RequirementStatus,
    ResourceKind,
    ResolvedAsset,
    StagedResourceRef,
    VersionedRef,
    WorkspaceArtifactInput,
    WorkspaceAttachmentInput,
    WorkspaceInputManifest,
    canonical_json_bytes,
    parse_canonical_json_bytes,
    validate_logparse_claim_for_job,
    validate_outcome_for_job,
    workspace_attachment_relative_path,
)
from problem_locator.integrations.logparse import (
    Anchor,
    ParseTargetsRequest,
    PinnedLogparseBrokerFactory,
    TargetLogsRequest,
    build_logparse_runtime,
    cli,
)
from problem_locator.integrations.logparse.outputs import (
    ControlledRun,
    inspect_controlled_run,
)
from tests.contracts.fakes import InMemoryCancellationSignal, InMemoryResourceStore


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_JOB_FIXTURE = (
    REPOSITORY_ROOT
    / "tests"
    / "fixtures"
    / "contracts"
    / "positive"
    / "job-diagnose.json"
)
FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "components" / "logparse"
FAKE_REPO = FIXTURES / "fake" / "repo"
FAKE_CONFIG = FAKE_REPO / "config.yaml"
EXPECTED_TARGETS = FIXTURES / "fake" / "expected-target-logs.json"
EXPECTED_PARSE_COUNTER = FIXTURES / "fake" / "expected-parse-counter.json"

CASE_ID = "00000000-0000-0000-0000-000000000201"
FIRST_JOB_ID = "00000000-0000-0000-0000-000000000101"
SECOND_JOB_ID = "00000000-0000-0000-0000-000000000102"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000301"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000401"
PROBLEM_TIME = "2026-07-31T00:00:03.000Z"
ORDER_REQUIREMENT_ID = "00000000-0000-0000-0000-000000000501"
FIRST_OUTCOME_ID = "00000000-0000-0000-0000-000000000601"
PRODUCED_AT = "2026-07-31T00:01:30.000Z"


def _client_anchor(
    *, label: str = "client", process_name: str = "checkout-client"
) -> Anchor:
    return Anchor(
        label=label,
        module="COMPACT",
        slot="1",
        process_name=process_name,
        pid="101",
    )


def _server_anchor() -> Anchor:
    return Anchor(
        label="server",
        module="COMPACT",
        slot="2",
        process_name="inventory-server",
        pid="202",
    )


def _job(
    tool_ref: VersionedRef,
    *,
    job_id: str = FIRST_JOB_ID,
    attachment_refs: list[str] | None = None,
    artifact_refs: list[str] | None = None,
) -> Job:
    payload = json.loads(CONTRACT_JOB_FIXTURE.read_text(encoding="utf-8"))
    payload.update(
        {
            "job_id": job_id,
            "case_id": CASE_ID,
            "goal": "Diagnose the synthetic RPC timeout with pinned logparse.",
            "attachment_refs": (
                [ATTACHMENT_ID] if attachment_refs is None else attachment_refs
            ),
            "artifact_refs": [] if artifact_refs is None else artifact_refs,
            "evidence_refs": [],
            "previous_outcome_refs": [],
            "logparse_tool_ref": tool_ref.model_dump(mode="json"),
            "logparse_product": "compact",
        }
    )
    payload["context_snapshot"]["evidence_refs"] = []
    return Job.model_validate(payload)


def _attachment(payload: bytes) -> WorkspaceAttachmentInput:
    return WorkspaceAttachmentInput(
        input_kind="ATTACHMENT",
        resource_id=ATTACHMENT_ID,
        relative_path=workspace_attachment_relative_path(
            ATTACHMENT_ID,
            AttachmentFilenameSuffix.ZIP,
        ),
        resource_kind=ResourceKind.FILE,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        content_type="application/zip",
        filename_suffix=AttachmentFilenameSuffix.ZIP,
    )


def _manifest(
    job: Job,
    entries: list[WorkspaceAttachmentInput | WorkspaceArtifactInput],
) -> WorkspaceInputManifest:
    return WorkspaceInputManifest(
        schema_version=1,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=JobType.DIAGNOSE,
        logparse_tool_ref=job.logparse_tool_ref,
        logparse_product=job.logparse_product,
        entries=entries,
    )


def _write_read_only(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o444)


def _materialize_workspace(
    root: Path,
    job: Job,
    payload: bytes,
    *,
    artifact: WorkspaceArtifactInput | None = None,
    artifact_source: Path | None = None,
) -> tuple[WorkspaceInputManifest, WorkspaceAttachmentInput]:
    root.mkdir()
    attachment = _attachment(payload)
    _write_read_only(root / attachment.relative_path, payload)
    entries: list[WorkspaceAttachmentInput | WorkspaceArtifactInput] = [attachment]
    if artifact is not None:
        assert artifact_source is not None
        destination = root / artifact.relative_path
        destination.parent.mkdir(parents=True)
        shutil.copytree(artifact_source, destination)
        _make_tree_read_only(destination)
        entries.append(artifact)
    manifest = _manifest(job, entries)
    _write_read_only(root / "inputs" / "manifest.json", canonical_json_bytes(manifest))
    return manifest, attachment


def _write_request(root: Path, proposal_key: str, request: object) -> bytes:
    proposal = root / "output" / "proposals" / proposal_key
    proposal.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(request)
    (proposal / "request.json").write_bytes(payload)
    return payload


def _parse_request(
    proposal_key: str,
    *,
    anchors: list[Anchor] | None = None,
) -> ParseTargetsRequest:
    return ParseTargetsRequest(
        schema_version=1,
        problem_time=PROBLEM_TIME,
        anchors=[_client_anchor(), _server_anchor()] if anchors is None else anchors,
        attachment_id=ATTACHMENT_ID,
        artifact_proposal_key=proposal_key,
    )


def _target_request(*, anchors: list[Anchor] | None = None) -> TargetLogsRequest:
    return TargetLogsRequest(
        schema_version=1,
        problem_time=PROBLEM_TIME,
        anchors=[_client_anchor(), _server_anchor()] if anchors is None else anchors,
        artifact_id=ARTIFACT_ID,
    )


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    environment: dict[str, str],
    operation: str,
    proposal_key: str,
) -> int:
    with monkeypatch.context() as scoped:
        scoped.chdir(workspace)
        for name, value in environment.items():
            scoped.setenv(name, value)
        return cli.main(
            [
                operation,
                "--request",
                f"output/proposals/{proposal_key}/request.json",
                "--result",
                f"output/proposals/{proposal_key}/target_logs.json",
            ]
        )


def _record(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _expected_target_bytes() -> bytes:
    return canonical_json_bytes(
        json.loads(EXPECTED_TARGETS.read_text(encoding="utf-8"))
    )


def _make_tree_read_only(root: Path) -> None:
    paths = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for path in paths:
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _artifact(
    run: ControlledRun, asset: ResolvedAsset, source: bytes
) -> WorkspaceArtifactInput:
    metadata = LogparseRunMetadata(
        tree_manifest_sha256=run.sha256,
        logparse_version_ref=asset.ref,
        parse_manifest_relative_path=run.parse_manifest_relative_path,
        source_attachment_id=ATTACHMENT_ID,
        source_attachment_sha256=hashlib.sha256(source).hexdigest(),
        parse_parameters=LogparseParseParameters(product="compact"),
    )
    return WorkspaceArtifactInput(
        input_kind="ARTIFACT",
        resource_id=ARTIFACT_ID,
        relative_path=f"inputs/artifacts/{ARTIFACT_ID}/tree",
        resource_kind=ResourceKind.DIRECTORY,
        size=run.size,
        sha256=run.sha256,
        artifact_kind=ArtifactKind.LOGPARSE_RUN,
        name="synthetic logparse run",
        content_type="application/vnd.problem-locator.logparse-run+directory",
        metadata=metadata,
    )


def _need_input_outcomes(
    job: Job,
    artifact: WorkspaceArtifactInput,
    staged: StagedResourceRef,
) -> tuple[AgentJobOutcome, JobOutcome]:
    requirement = PendingRequirement(
        requirement_id=ORDER_REQUIREMENT_ID,
        kind=RequirementKind.INPUT,
        name="order_id",
        prompt="Provide the order identifier for the synthetic timeout.",
        required=True,
        constraints=InputRequirementConstraints(
            value_type="STRING",
            min_utf8_bytes=1,
            max_utf8_bytes=128,
            pattern=r"^[A-Za-z0-9._-]+$",
            allowed_values=[],
        ),
        status=RequirementStatus.OPEN,
        requested_by_job_id=job.job_id,
        fulfilled_by_refs=[],
    )
    payload = DiagnosisOutcome(
        findings=[],
        state_delta=DiagnosisStateDelta(
            problem_spec_patch=None,
            add_user_facts=[],
            proposed_facts=[],
            add_active_hypotheses=[],
            update_hypotheses=[],
            reject_hypotheses=[],
            add_open_questions=[],
            resolve_questions=[],
            add_pending_requirements=[requirement],
            fulfill_requirements=[],
            add_evidence_bindings=[],
        ),
        requested_input=[requirement.requirement_id],
        requested_attachments=[],
        candidate_conclusion_draft=None,
        recommended_next_step="Provide order_id; retain the parsed run.",
    )
    draft = AgentArtifactProposalDraft(
        proposal_key="logparse-run",
        artifact_kind=artifact.artifact_kind,
        name=artifact.name,
        content_type=artifact.content_type,
        resource_kind=artifact.resource_kind,
        workspace_relative_path="output/proposals/logparse-run/tree",
        declared_size=artifact.size,
        declared_sha256=artifact.sha256,
        metadata=artifact.metadata,
    )
    agent = AgentJobOutcome(
        outcome_id=FIRST_OUTCOME_ID,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        base_state_revision=job.base_state_revision,
        result_type=OutcomeResultType.NEED_INPUT,
        payload=payload,
        consumed_evidence_refs=[],
        proposed_evidence_drafts=[],
        proposed_artifact_drafts=[draft],
        error=None,
        produced_at=PRODUCED_AT,
    )
    proposal = ArtifactProposal(
        proposal_key=draft.proposal_key,
        artifact_kind=draft.artifact_kind,
        name=draft.name,
        content_type=draft.content_type,
        resource_kind=draft.resource_kind,
        size=staged.size,
        sha256=staged.sha256,
        staged_resource_ref=staged,
        metadata=draft.metadata,
    )
    normalized = JobOutcome(
        outcome_id=agent.outcome_id,
        job_id=agent.job_id,
        case_id=agent.case_id,
        job_type=agent.job_type,
        base_state_revision=agent.base_state_revision,
        result_type=agent.result_type,
        payload=agent.payload,
        consumed_evidence_refs=agent.consumed_evidence_refs,
        proposed_evidence=[],
        proposed_artifacts=[proposal],
        error=None,
        produced_at=agent.produced_at,
    )
    return agent, normalized


def _workspace_artifact(proposal: ArtifactProposal) -> WorkspaceArtifactInput:
    return WorkspaceArtifactInput(
        input_kind="ARTIFACT",
        resource_id=ARTIFACT_ID,
        relative_path=f"inputs/artifacts/{ARTIFACT_ID}/tree",
        resource_kind=proposal.resource_kind,
        size=proposal.size,
        sha256=proposal.sha256,
        artifact_kind=proposal.artifact_kind,
        name=proposal.name,
        content_type=proposal.content_type,
        metadata=proposal.metadata,
    )


def _factory(
    asset: ResolvedAsset,
    *,
    token_factory: Callable[[], str] | None = None,
    session_id_factory: Callable[[], str] | None = None,
    fault_point: Callable[[str], None] | None = None,
) -> PinnedLogparseBrokerFactory:
    options: dict[str, object] = {}
    if token_factory is not None:
        options["token_factory"] = token_factory
    if session_id_factory is not None:
        options["session_id_factory"] = session_id_factory
    if fault_point is not None:
        options["fault_point"] = fault_point
    return PinnedLogparseBrokerFactory(
        asset,
        FAKE_REPO,
        FAKE_CONFIG,
        Path(sys.executable),
        **options,
    )


@pytest.fixture(scope="module")
def pinned_asset() -> ResolvedAsset:
    asset, factory = build_logparse_runtime(FAKE_REPO, FAKE_CONFIG, sys.executable)
    assert asset.asset_kind is AssetKind.LOGPARSE_TOOL
    assert asset.root_path == os.fspath(FAKE_REPO.resolve())
    assert isinstance(factory, LogparseBrokerFactory)
    assert factory.resolved_asset == asset
    return asset


def test_builder_ref_and_open_use_one_pinned_public_contract(
    tmp_path: Path,
    pinned_asset: ResolvedAsset,
) -> None:
    job = _job(pinned_asset.ref)
    workspace = tmp_path / "workspace"
    manifest, _entry = _materialize_workspace(workspace, job, b"builder-ref")
    factory = _factory(pinned_asset)

    session = factory.open(
        job,
        workspace,
        manifest,
        InMemoryCancellationSignal(),
    )
    try:
        assert isinstance(session, LogparseBrokerSession)
        assert session.parse_request_bytes() is None
        environment = session.agent_environment()
        assert set(environment) == {
            "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT",
            "PROBLEM_LOCATOR_LOGPARSE_TOKEN",
        }
        assert environment["PROBLEM_LOCATOR_LOGPARSE_ENDPOINT"].startswith(
            "http://127.0.0.1:"
        )
    finally:
        session.close()


def test_public_asset_error_has_no_endpoint_claim_or_process_side_effect(
    tmp_path: Path,
    pinned_asset: ResolvedAsset,
) -> None:
    other_ref = VersionedRef(
        id=pinned_asset.ref.id,
        version="sha256-ffffffffffffffff",
        content_hash="f" * 64,
    )
    job = _job(other_ref)
    workspace = tmp_path / "workspace"
    manifest, _entry = _materialize_workspace(workspace, job, b"asset-mismatch")
    record_path = tmp_path / "fake-invocations.json"
    events: list[str] = []
    factory = _factory(pinned_asset, fault_point=events.append)

    with pytest.raises(LogparseBrokerError) as raised:
        factory.open(job, workspace, manifest, InMemoryCancellationSignal())

    failure = raised.value.failure
    assert isinstance(failure, ExecutionFailure)
    assert failure.stage is ExecutionStage.ASSET_RESOLUTION
    assert failure.code is ErrorCode.ASSET_VERSION_UNAVAILABLE
    assert failure.retryable is False
    assert failure.details == []
    assert events == []
    assert not record_path.exists()
    assert not (workspace / "runtime").exists()


def test_first_parse_dual_anchor_claim_audit_close_and_fixed_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pinned_asset: ResolvedAsset,
) -> None:
    source = b"synthetic fake archive"
    job = _job(pinned_asset.ref)
    workspace = tmp_path / "workspace"
    manifest, attachment = _materialize_workspace(workspace, job, source)
    record_path = tmp_path / "fake-invocations.json"
    monkeypatch.setenv("S07_FAKE_LOGPARSE_RECORD", os.fspath(record_path))
    session = _factory(pinned_asset).open(
        job, workspace, manifest, InMemoryCancellationSignal()
    )
    request = _parse_request("logparse-run")
    request_bytes = _write_request(workspace, "logparse-run", request)
    draft_value = request.model_dump(mode="json")

    def reverse_objects(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: reverse_objects(child)
                for key, child in reversed(list(value.items()))
            }
        if isinstance(value, list):
            return [reverse_objects(child) for child in value]
        return value

    request_path = (
        workspace / "output/proposals/logparse-run/request.json"
    )
    request_path.write_bytes(
        json.dumps(
            reverse_objects(draft_value),
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
    )

    try:
        assert session.parse_request_bytes() is None
        assert (
            _invoke(
                monkeypatch,
                workspace,
                session.agent_environment(),
                "parse-targets",
                "logparse-run",
            )
            == 0
        )
        captured = capsys.readouterr()
        assert captured.out == "problem-locator-logparse: broker request completed\n"
        assert captured.err == ""
        result_path = (
            workspace / "output" / "proposals" / "logparse-run" / "target_logs.json"
        )
        result_payload = json.loads(result_path.read_bytes())
        artifact_draft = result_payload.pop("logparse_run_artifact_draft")
        assert canonical_json_bytes(result_payload) == _expected_target_bytes()
        tree = workspace / "output" / "proposals" / "logparse-run" / "tree"
        controlled_run = inspect_controlled_run(tree, product="compact")
        assert artifact_draft == {
            "artifact_kind": "LOGPARSE_RUN",
            "content_type": (
                "application/vnd.problem-locator.logparse-run+directory"
            ),
            "declared_sha256": None,
            "declared_size": None,
            "metadata": {
                "logparse_version_ref": pinned_asset.ref.model_dump(mode="json"),
                "parse_manifest_relative_path": (
                    controlled_run.parse_manifest_relative_path
                ),
                "parse_parameters": {"product": "compact"},
                "source_attachment_id": attachment.resource_id,
                "source_attachment_sha256": attachment.sha256,
                "tree_manifest_sha256": controlled_run.sha256,
            },
            "name": "logparse-run",
            "proposal_key": "logparse-run",
            "resource_kind": "DIRECTORY",
            "workspace_relative_path": (
                "output/proposals/logparse-run/tree"
            ),
        }

        audited = session.parse_request_bytes()
        assert audited == request_bytes
        assert request_path.read_bytes() == request_bytes
        assert type(audited) is bytes
        assert memoryview(audited).readonly

        claim_path = workspace / "runtime" / "tool-state" / "logparse-parse.claim"
        claim_bytes = claim_path.read_bytes()
        expected_claim = LogparseParseClaim(
            schema_version=1,
            job_id=job.job_id,
            attachment_id=attachment.resource_id,
            attachment_sha256=attachment.sha256,
            artifact_proposal_key="logparse-run",
            logparse_tool_ref=pinned_asset.ref,
            request_sha256=hashlib.sha256(request_bytes).hexdigest(),
        )
        assert claim_bytes == canonical_json_bytes(expected_claim)
        assert (
            parse_canonical_json_bytes(claim_bytes, LogparseParseClaim)
            == expected_claim
        )
        assert stat.S_IMODE(claim_path.stat().st_mode) == 0o600
        assert (
            validate_logparse_claim_for_job(
                expected_claim,
                job,
                manifest,
                audited,
            )
            is expected_claim
        )
        with pytest.raises(ValueError, match="request bytes require a parse claim"):
            validate_logparse_claim_for_job(None, job, manifest, audited)

        record = _record(record_path)
        assert record["parse_count"] == 1
        assert record["target_logs_count"] == 2
        assert record["invocations"] == [
            {
                "command": "parse",
                "argv": [
                    "parse",
                    os.fspath((workspace / attachment.relative_path).resolve()),
                    "-c",
                    os.fspath(FAKE_CONFIG.resolve()),
                    "-o",
                    os.fspath(tree.resolve()),
                    "--product",
                    "compact",
                ],
                "reserved_environment_present": False,
            },
            {
                "command": "mech-target-logs",
                "argv": [
                    "mech-target-logs",
                    "task-synthetic",
                    "--output",
                    os.fspath(tree.resolve()),
                    "--problem-time",
                    PROBLEM_TIME,
                    "--module",
                    "COMPACT",
                    "--slot",
                    "1",
                    "--process-name",
                    "checkout-client",
                    "--pid",
                    "101",
                    "--label",
                    "client",
                ],
                "reserved_environment_present": False,
            },
            {
                "command": "mech-target-logs",
                "argv": [
                    "mech-target-logs",
                    "task-synthetic",
                    "--output",
                    os.fspath(tree.resolve()),
                    "--problem-time",
                    PROBLEM_TIME,
                    "--module",
                    "COMPACT",
                    "--slot",
                    "2",
                    "--process-name",
                    "inventory-server",
                    "--pid",
                    "202",
                    "--label",
                    "server",
                ],
                "reserved_environment_present": False,
            },
        ]
    finally:
        session.close()

    assert session.parse_request_bytes() == request_bytes
    assert (
        validate_logparse_claim_for_job(
            expected_claim,
            job,
            manifest,
            session.parse_request_bytes(),
        )
        is expected_claim
    )
    with pytest.raises(RuntimeError, match="closed"):
        session.agent_environment()


@pytest.mark.parametrize(
    ("failure_point", "claim_is_complete", "request_is_audited"),
    [
        ("claim_reserved", False, False),
        ("claim_written", True, True),
    ],
)
def test_claim_fault_never_publishes_request_before_create_new_claim_is_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pinned_asset: ResolvedAsset,
    failure_point: str,
    claim_is_complete: bool,
    request_is_audited: bool,
) -> None:
    source = b"faulted synthetic archive"
    job = _job(pinned_asset.ref)
    workspace = tmp_path / "workspace"
    manifest, attachment = _materialize_workspace(workspace, job, source)
    request = _parse_request("faulted-run")
    request_bytes = _write_request(workspace, "faulted-run", request)

    def inject_fault(point: str) -> None:
        if point == failure_point:
            raise OSError(f"injected {point}")

    session = _factory(pinned_asset, fault_point=inject_fault).open(
        job,
        workspace,
        manifest,
        InMemoryCancellationSignal(),
    )
    try:
        assert (
            _invoke(
                monkeypatch,
                workspace,
                session.agent_environment(),
                "parse-targets",
                "faulted-run",
            )
            == 2
        )
        captured = capsys.readouterr()
        assert captured.err == (
            "problem-locator-logparse: TOOL_EXECUTE/LOGPARSE_FAILED\n"
        )
    finally:
        session.close()

    audited = session.parse_request_bytes()
    assert (audited == request_bytes) is request_is_audited
    claim_path = workspace / "runtime" / "tool-state" / "logparse-parse.claim"
    if not claim_is_complete:
        assert claim_path.read_bytes() == b""
        assert audited is None
        assert validate_logparse_claim_for_job(None, job, manifest, audited) is None
        return

    claim = parse_canonical_json_bytes(claim_path.read_bytes(), LogparseParseClaim)
    assert claim.attachment_id == attachment.resource_id
    assert audited is not None
    assert validate_logparse_claim_for_job(claim, job, manifest, audited) is claim
    with pytest.raises(ValueError, match="request bytes require a parse claim"):
        validate_logparse_claim_for_job(None, job, manifest, audited)


def test_same_session_rejects_same_and_changed_parse_key_without_second_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pinned_asset: ResolvedAsset,
) -> None:
    job = _job(pinned_asset.ref)
    workspace = tmp_path / "workspace"
    manifest, _entry = _materialize_workspace(workspace, job, b"one-parse-only")
    record_path = tmp_path / "fake-invocations.json"
    monkeypatch.setenv("S07_FAKE_LOGPARSE_RECORD", os.fspath(record_path))
    session = _factory(pinned_asset).open(
        job, workspace, manifest, InMemoryCancellationSignal()
    )
    first_bytes = _write_request(workspace, "run-a", _parse_request("run-a"))

    try:
        assert (
            _invoke(
                monkeypatch,
                workspace,
                session.agent_environment(),
                "parse-targets",
                "run-a",
            )
            == 0
        )
        capsys.readouterr()

        _write_request(
            workspace, "run-a", _parse_request("run-a", anchors=[_server_anchor()])
        )
        assert (
            _invoke(
                monkeypatch,
                workspace,
                session.agent_environment(),
                "parse-targets",
                "run-a",
            )
            == 2
        )
        same_key = capsys.readouterr()
        assert same_key.err == (
            "problem-locator-logparse: TOOL_EXECUTE/LOGPARSE_FAILED\n"
        )

        _write_request(workspace, "run-b", _parse_request("run-b"))
        assert (
            _invoke(
                monkeypatch,
                workspace,
                session.agent_environment(),
                "parse-targets",
                "run-b",
            )
            == 2
        )
        changed_key = capsys.readouterr()
        assert changed_key.err == (
            "problem-locator-logparse: TOOL_EXECUTE/LOGPARSE_FAILED\n"
        )

        record = _record(record_path)
        assert record["parse_count"] == 1
        assert record["target_logs_count"] == 2
        assert session.parse_request_bytes() == first_bytes
        assert not (workspace / "output" / "proposals" / "run-b" / "tree").exists()
    finally:
        session.close()


def test_second_job_reuses_formal_run_and_total_parse_process_count_stays_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pinned_asset: ResolvedAsset,
) -> None:
    source = b"two-job synthetic archive"
    record_path = tmp_path / "fake-invocations.json"
    monkeypatch.setenv("S07_FAKE_LOGPARSE_RECORD", os.fspath(record_path))

    first_job = _job(pinned_asset.ref)
    first_workspace = tmp_path / "job-1"
    first_manifest, _entry = _materialize_workspace(first_workspace, first_job, source)
    first_session = _factory(pinned_asset).open(
        first_job,
        first_workspace,
        first_manifest,
        InMemoryCancellationSignal(),
    )
    first_request_bytes = _write_request(
        first_workspace, "logparse-run", _parse_request("logparse-run")
    )
    try:
        assert (
            _invoke(
                monkeypatch,
                first_workspace,
                first_session.agent_environment(),
                "parse-targets",
                "logparse-run",
            )
            == 0
        )
        capsys.readouterr()
    finally:
        first_session.close()
    assert first_session.parse_request_bytes() == first_request_bytes

    first_tree = first_workspace / "output" / "proposals" / "logparse-run" / "tree"
    run = inspect_controlled_run(first_tree, product="compact")
    claim = parse_canonical_json_bytes(
        (
            first_workspace / "runtime" / "tool-state" / "logparse-parse.claim"
        ).read_bytes(),
        LogparseParseClaim,
    )
    resource_store = InMemoryResourceStore()
    staged = resource_store.stage_tree(
        first_job.job_id,
        "logparse-run",
        first_tree,
        expected_manifest_hash=run.sha256,
    )
    assert resource_store.stage_tree_calls == [
        (first_job.job_id, "logparse-run", first_tree)
    ]
    assert staged.resource_kind is ResourceKind.DIRECTORY
    assert staged.size == run.size
    assert staged.sha256 == run.sha256
    assert staged.tree_manifest is not None

    draft_artifact = _artifact(run, pinned_asset, source)
    agent_outcome, normalized_outcome = _need_input_outcomes(
        first_job,
        draft_artifact,
        staged,
    )
    assert (
        validate_outcome_for_job(first_job, agent_outcome, first_manifest)
        is agent_outcome
    )
    assert (
        validate_logparse_claim_for_job(
            claim,
            first_job,
            first_manifest,
            first_request_bytes,
            agent_outcome,
        )
        is claim
    )
    assert (
        validate_outcome_for_job(first_job, normalized_outcome, first_manifest)
        is normalized_outcome
    )
    assert (
        validate_logparse_claim_for_job(
            claim,
            first_job,
            first_manifest,
            first_request_bytes,
            normalized_outcome,
        )
        is claim
    )
    normalized_bytes = canonical_json_bytes(normalized_outcome)
    assert (
        parse_canonical_json_bytes(normalized_bytes, JobOutcome) == normalized_outcome
    )
    artifact = _workspace_artifact(normalized_outcome.proposed_artifacts[0])
    assert artifact.metadata == draft_artifact.metadata
    assert artifact.sha256 == staged.sha256
    second_job = _job(
        pinned_asset.ref,
        job_id=SECOND_JOB_ID,
        artifact_refs=[ARTIFACT_ID],
    )
    second_workspace = tmp_path / "job-2"
    second_manifest, _second_attachment = _materialize_workspace(
        second_workspace,
        second_job,
        source,
        artifact=artifact,
        artifact_source=first_tree,
    )
    second_session = _factory(pinned_asset).open(
        second_job,
        second_workspace,
        second_manifest,
        InMemoryCancellationSignal(),
    )
    _write_request(
        second_workspace, "illegal-reparse", _parse_request("illegal-reparse")
    )
    _write_request(second_workspace, "reuse", _target_request())
    second_environment = second_session.agent_environment()

    try:
        assert (
            _invoke(
                monkeypatch,
                second_workspace,
                second_environment,
                "parse-targets",
                "illegal-reparse",
            )
            == 2
        )
        rejected = capsys.readouterr()
        assert rejected.err == (
            "problem-locator-logparse: TOOL_EXECUTE/LOGPARSE_FAILED\n"
        )
        assert second_session.parse_request_bytes() is None
        assert not (second_workspace / "runtime" / "tool-state").exists()

        assert (
            _invoke(
                monkeypatch,
                second_workspace,
                second_environment,
                "target-logs",
                "reuse",
            )
            == 0
        )
        capsys.readouterr()
        assert (
            second_workspace / "output" / "proposals" / "reuse" / "target_logs.json"
        ).read_bytes() == _expected_target_bytes()

        record = _record(record_path)
        assert record["parse_count"] == 1
        assert record["target_logs_count"] == 4
        commands = [entry["command"] for entry in record["invocations"]]
        assert commands == [
            "parse",
            "mech-target-logs",
            "mech-target-logs",
            "mech-target-logs",
            "mech-target-logs",
        ]
        continuation_root = second_workspace / artifact.relative_path
        for invocation in record["invocations"][-2:]:
            argv = invocation["argv"]
            assert argv[argv.index("--output") + 1] == os.fspath(
                continuation_root.resolve()
            )
    finally:
        second_session.close()

    assert second_session.parse_request_bytes() is None
    counter_bytes = record_path.with_name("parse_counter.json").read_bytes()
    assert counter_bytes == EXPECTED_PARSE_COUNTER.read_bytes()
    assert counter_bytes == canonical_json_bytes({"parse_count": 1})
    assert parse_canonical_json_bytes(counter_bytes) == {"parse_count": 1}


def test_capabilities_are_session_and_workspace_bound_then_invalid_after_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pinned_asset: ResolvedAsset,
) -> None:
    first_job = _job(pinned_asset.ref)
    first_workspace = tmp_path / "job-a"
    first_manifest, _entry = _materialize_workspace(
        first_workspace, first_job, b"workspace-a"
    )
    second_job = _job(pinned_asset.ref, job_id=SECOND_JOB_ID)
    second_workspace = tmp_path / "job-b"
    second_manifest, _second_entry = _materialize_workspace(
        second_workspace, second_job, b"workspace-b"
    )
    first_request = _write_request(
        first_workspace, "bound", _parse_request("bound", anchors=[_client_anchor()])
    )
    _write_request(
        second_workspace, "bound", _parse_request("bound", anchors=[_server_anchor()])
    )
    tokens = iter(("session-token-alpha", "session-token-bravo"))
    session_ids = iter(("session-alpha", "session-bravo"))
    factory = _factory(
        pinned_asset,
        token_factory=lambda: next(tokens),
        session_id_factory=lambda: next(session_ids),
    )
    first_session = factory.open(
        first_job, first_workspace, first_manifest, InMemoryCancellationSignal()
    )
    second_session = factory.open(
        second_job, second_workspace, second_manifest, InMemoryCancellationSignal()
    )
    first_environment = first_session.agent_environment()
    second_environment = second_session.agent_environment()

    try:
        assert first_environment != second_environment
        assert (
            _invoke(
                monkeypatch,
                second_workspace,
                first_environment,
                "parse-targets",
                "bound",
            )
            == 2
        )
        cross_workspace = capsys.readouterr()
        assert cross_workspace.err == (
            "problem-locator-logparse: TOOL_EXECUTE/LOGPARSE_FAILED\n"
        )
        assert first_session.parse_request_bytes() is None

        mixed_capability = {
            "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT": first_environment[
                "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT"
            ],
            "PROBLEM_LOCATOR_LOGPARSE_TOKEN": second_environment[
                "PROBLEM_LOCATOR_LOGPARSE_TOKEN"
            ],
        }
        assert (
            _invoke(
                monkeypatch,
                first_workspace,
                mixed_capability,
                "parse-targets",
                "bound",
            )
            == 2
        )
        unauthorized = capsys.readouterr()
        assert unauthorized.err == "problem-locator-logparse: broker request failed\n"
        assert first_session.parse_request_bytes() is None
        first_result = (
            first_workspace / "output" / "proposals" / "bound" / "target_logs.json"
        )
        assert not first_result.exists()
        assert not (first_workspace / "runtime").exists()
        assert not (second_workspace / "runtime").exists()

        first_session.close()
        assert (
            _invoke(
                monkeypatch,
                first_workspace,
                first_environment,
                "parse-targets",
                "bound",
            )
            == 2
        )
        closed = capsys.readouterr()
        assert closed.err == "problem-locator-logparse: broker request failed\n"
        assert not first_result.exists()
        assert first_session.parse_request_bytes() is None
        assert (
            canonical_json_bytes(parse_canonical_json_bytes(first_request))
            == first_request
        )
    finally:
        first_session.close()
        second_session.close()


@pytest.mark.parametrize(
    ("source", "anchor", "expected_code", "target_count"),
    [
        (b"NONZERO", _client_anchor(), ErrorCode.LOGPARSE_FAILED, 0),
        (b"UNSUPPORTED_FORMAT", _client_anchor(), ErrorCode.LOGPARSE_FAILED, 0),
        (b"MISSING_MANIFEST", _client_anchor(), ErrorCode.LOGPARSE_OUTPUT_INVALID, 0),
        (b"MANIFEST_DIRECTORY", _client_anchor(), ErrorCode.LOGPARSE_OUTPUT_INVALID, 0),
        (b"SECOND_TASK", _client_anchor(), ErrorCode.LOGPARSE_OUTPUT_INVALID, 0),
        (
            b"target-nonzero",
            _client_anchor(process_name="fail"),
            ErrorCode.LOGPARSE_FAILED,
            1,
        ),
        (
            b"target-invalid-json",
            _client_anchor(process_name="invalid-json"),
            ErrorCode.LOGPARSE_OUTPUT_INVALID,
            1,
        ),
        (
            b"target-path-escape",
            _client_anchor(label="escape"),
            ErrorCode.LOGPARSE_OUTPUT_INVALID,
            1,
        ),
    ],
    ids=(
        "parse-nonzero",
        "unsupported-format",
        "manifest-missing",
        "manifest-not-file",
        "manifest-ambiguous-tree",
        "target-nonzero",
        "target-invalid-json",
        "target-path-escape",
    ),
)
def test_execution_failures_keep_the_frozen_error_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pinned_asset: ResolvedAsset,
    source: bytes,
    anchor: Anchor,
    expected_code: ErrorCode,
    target_count: int,
) -> None:
    job = _job(pinned_asset.ref)
    workspace = tmp_path / "workspace"
    manifest, attachment = _materialize_workspace(workspace, job, source)
    record_path = tmp_path / "fake-invocations.json"
    monkeypatch.setenv("S07_FAKE_LOGPARSE_RECORD", os.fspath(record_path))
    session = _factory(pinned_asset).open(
        job, workspace, manifest, InMemoryCancellationSignal()
    )
    request_bytes = _write_request(
        workspace,
        "failure",
        _parse_request("failure", anchors=[anchor]),
    )
    observed: list[ExecutionFailure | None] = []
    real_run = cli.run

    def recording_run(
        operation: str, request_path: str, result_path: str
    ) -> ExecutionFailure | None:
        failure = real_run(operation, request_path, result_path)
        observed.append(failure)
        return failure

    monkeypatch.setattr(cli, "run", recording_run)
    try:
        assert (
            _invoke(
                monkeypatch,
                workspace,
                session.agent_environment(),
                "parse-targets",
                "failure",
            )
            == 2
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == (
            f"problem-locator-logparse: TOOL_EXECUTE/{expected_code.value}\n"
        )
        assert len(observed) == 1
        failure = observed[0]
        assert isinstance(failure, ExecutionFailure)
        assert failure.stage is ExecutionStage.TOOL_EXECUTE
        assert failure.code is expected_code
        assert failure.retryable is False
        assert failure.details == []
        result_path = (
            workspace / "output" / "proposals" / "failure" / "target_logs.json"
        )
        assert result_path.read_bytes() == canonical_json_bytes(failure)
        assert (
            parse_canonical_json_bytes(result_path.read_bytes(), ExecutionFailure)
            == failure
        )
        assert session.parse_request_bytes() == request_bytes

        record = _record(record_path)
        assert record["parse_count"] == 1
        assert record["target_logs_count"] == target_count
        claim = parse_canonical_json_bytes(
            (
                workspace / "runtime" / "tool-state" / "logparse-parse.claim"
            ).read_bytes(),
            LogparseParseClaim,
        )
        assert claim.attachment_id == attachment.resource_id
        assert claim.request_sha256 == hashlib.sha256(request_bytes).hexdigest()
    finally:
        session.close()


def test_close_synchronously_reclaims_a_parse_process_hanging_after_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pinned_asset: ResolvedAsset,
) -> None:
    job = _job(pinned_asset.ref)
    workspace = tmp_path / "workspace"
    manifest, _entry = _materialize_workspace(workspace, job, b"HANG_AFTER_PARSE")
    record_path = tmp_path / "fake-invocations.json"
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("S07_FAKE_LOGPARSE_RECORD", os.fspath(record_path))
    session = _factory(pinned_asset).open(
        job, workspace, manifest, InMemoryCancellationSignal()
    )
    request_bytes = _write_request(workspace, "hang", _parse_request("hang"))
    for name, value in session.agent_environment().items():
        monkeypatch.setenv(name, value)

    returns: list[int] = []
    failures: list[BaseException] = []

    def invoke_main() -> None:
        try:
            returns.append(
                cli.main(
                    [
                        "parse-targets",
                        "--request",
                        "output/proposals/hang/request.json",
                        "--result",
                        "output/proposals/hang/target_logs.json",
                    ]
                )
            )
        except BaseException as exc:  # pragma: no cover - failure evidence
            failures.append(exc)

    client_thread = threading.Thread(target=invoke_main, name="s07-hanging-client")
    client_thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if record_path.is_file() and _record(record_path)["parse_count"] == 1:
            break
        time.sleep(0.02)
    else:
        session.close()
        client_thread.join(timeout=5.0)
        pytest.fail("fake parse process did not start before the close test deadline")

    started = time.monotonic()
    session.close()
    close_seconds = time.monotonic() - started
    client_thread.join(timeout=5.0)

    assert close_seconds < 5.0
    assert not client_thread.is_alive()
    assert failures == []
    assert returns == [2]
    assert session.parse_request_bytes() == request_bytes
    with pytest.raises(RuntimeError, match="closed"):
        session.agent_environment()
    assert _record(record_path)["parse_count"] == 1
    assert not (
        workspace / "output" / "proposals" / "hang" / "target_logs.json"
    ).exists()


def test_concurrent_close_calls_wait_for_the_same_cleanup(
    tmp_path: Path,
    pinned_asset: ResolvedAsset,
) -> None:
    job = _job(pinned_asset.ref)
    workspace = tmp_path / "workspace"
    manifest, _entry = _materialize_workspace(workspace, job, b"close-race")
    close_entered = threading.Event()
    release_close = threading.Event()

    def block_close(name: str) -> None:
        if name == "close_started":
            close_entered.set()
            assert release_close.wait(timeout=5.0)

    session = _factory(pinned_asset, fault_point=block_close).open(
        job, workspace, manifest, InMemoryCancellationSignal()
    )
    completed: list[str] = []
    failures: list[BaseException] = []

    def close_in_thread(label: str) -> None:
        try:
            session.close()
            completed.append(label)
        except BaseException as exc:  # pragma: no cover - failure evidence
            failures.append(exc)

    owner = threading.Thread(target=close_in_thread, args=("owner",))
    waiter = threading.Thread(target=close_in_thread, args=("waiter",))
    owner.start()
    assert close_entered.wait(timeout=5.0)
    waiter.start()
    waiter.join(timeout=0.2)
    assert waiter.is_alive()

    release_close.set()
    owner.join(timeout=5.0)
    waiter.join(timeout=5.0)

    assert not owner.is_alive()
    assert not waiter.is_alive()
    assert failures == []
    assert sorted(completed) == ["owner", "waiter"]
    with pytest.raises(RuntimeError, match="closed"):
        session.agent_environment()


def test_failed_server_close_is_retryable_without_revalidating_the_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pinned_asset: ResolvedAsset,
) -> None:
    job = _job(pinned_asset.ref)
    workspace = tmp_path / "workspace"
    manifest, _entry = _materialize_workspace(workspace, job, b"close-retry")
    session = _factory(pinned_asset).open(
        job, workspace, manifest, InMemoryCancellationSignal()
    )
    server = session._server
    assert server is not None
    real_server_close = server.server_close
    calls = 0

    def fail_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected server close failure")
        real_server_close()

    monkeypatch.setattr(server, "server_close", fail_once)

    with pytest.raises(OSError, match="injected"):
        session.close()
    with pytest.raises(RuntimeError, match="closed"):
        session.agent_environment()
    assert server.socket.fileno() >= 0

    session.close()

    assert calls == 2
    assert server.socket.fileno() == -1
    session.close()
