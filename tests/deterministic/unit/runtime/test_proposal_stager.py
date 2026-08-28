from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from problem_locator.contracts import (
    AgentArtifactProposalDraft,
    AgentExecutionFailure,
    AgentJobOutcome,
    ApplicationError,
    ApplicationErrorDetail,
    ApplicationPortError,
    ArtifactKind,
    ErrorCode,
    ExecutionFailure,
    ExecutionStage,
    Job,
    LogparseParseClaim,
    LogparseRunMetadata,
    ResourceKind,
    StagedResourceRef,
    TreeManifest,
    TreeManifestEntry,
    UserResultPayload,
    WorkspaceInputManifest,
    canonical_json_bytes,
)
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.output_reader import (
    ValidatedAgentOutput,
    ValidatedProposalResource,
    _snapshot_source,
)
from problem_locator.runtime.proposal_stager import stage_validated_output

from tests.deterministic.contracts.fakes import InMemoryResourceStore


REPOSITORY_ROOT = Path(__file__).parents[4]
CONTRACT_FIXTURES = REPOSITORY_ROOT / "tests/fixtures/contracts/positive"
OTHER_JOB_ID = "00000000-0000-0000-0000-000000000099"


def _fixture_payload(name: str) -> dict[str, Any]:
    return json.loads((CONTRACT_FIXTURES / name).read_bytes())


def _route_inputs() -> tuple[Job, WorkspaceInputManifest]:
    job = Job.model_validate(_fixture_payload("job-route.json"))
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
    return job, manifest


def _diagnostic_draft(
    *,
    proposal_key: str,
    workspace_relative_path: str,
    resource_kind: ResourceKind,
    size: int,
    sha256: str,
) -> AgentArtifactProposalDraft:
    return AgentArtifactProposalDraft(
        proposal_key=proposal_key,
        artifact_kind=ArtifactKind.DIAGNOSTIC_EXPORT,
        name=f"{proposal_key}-diagnostic",
        content_type="application/octet-stream",
        resource_kind=resource_kind,
        workspace_relative_path=workspace_relative_path,
        declared_size=size,
        declared_sha256=sha256,
        metadata={
            "schema_version": 1,
            "format_id": "proposal-stager-test",
            "description": "Diagnostic payload for proposal staging tests.",
        },
    )


def _agent_outcome(
    drafts: list[AgentArtifactProposalDraft],
) -> AgentJobOutcome:
    payload = _fixture_payload("agent-job-outcome-route.json")
    payload["proposed_artifact_drafts"] = [
        draft.model_dump(mode="json") for draft in drafts
    ]
    return AgentJobOutcome.model_validate(payload)


def _file_resource(
    tmp_path: Path,
    *,
    proposal_key: str,
    payload: bytes,
) -> ValidatedProposalResource:
    relative_path = f"output/proposals/{proposal_key}/payload.bin"
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    sha256 = hashlib.sha256(payload).hexdigest()
    draft = _diagnostic_draft(
        proposal_key=proposal_key,
        workspace_relative_path=relative_path,
        resource_kind=ResourceKind.FILE,
        size=len(payload),
        sha256=sha256,
    )
    return ValidatedProposalResource(
        draft=draft,
        proposal_key=proposal_key,
        workspace_relative_path=relative_path,
        path=path,
        resource_kind=ResourceKind.FILE,
        size=len(payload),
        sha256=sha256,
        tree_manifest=None,
        source_snapshot=_snapshot_source(tmp_path, relative_path),
    )


def _tree_metadata(root: Path) -> tuple[int, str, TreeManifest]:
    entries: list[TreeManifestEntry] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        entries.append(
            TreeManifestEntry(
                path=path.relative_to(root).as_posix(),
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
    manifest = TreeManifest(version=1, entries=entries)
    size = sum(entry.size for entry in entries)
    sha256 = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    return size, sha256, manifest


def _tree_resource(
    *,
    root: Path,
    proposal_key: str,
) -> ValidatedProposalResource:
    relative_path = f"output/proposals/{proposal_key}/tree"
    size, sha256, manifest = _tree_metadata(root)
    draft = _diagnostic_draft(
        proposal_key=proposal_key,
        workspace_relative_path=relative_path,
        resource_kind=ResourceKind.DIRECTORY,
        size=size,
        sha256=sha256,
    )
    return ValidatedProposalResource(
        draft=draft,
        proposal_key=proposal_key,
        workspace_relative_path=relative_path,
        path=root,
        resource_kind=ResourceKind.DIRECTORY,
        size=size,
        sha256=sha256,
        tree_manifest=manifest,
        source_snapshot=_snapshot_source(root.parents[3], relative_path),
    )


def _validated(
    resources: list[ValidatedProposalResource],
    *,
    outcome: AgentJobOutcome | None = None,
    user_result: UserResultPayload | None = None,
) -> ValidatedAgentOutput:
    if outcome is None:
        outcome = _agent_outcome([resource.draft for resource in resources])
    return ValidatedAgentOutput(
        outcome=outcome,
        canonical_bytes=canonical_json_bytes(outcome),
        proposal_resources=tuple(resources),
        user_result=user_result,
    )


def _stage(
    *,
    job: Job,
    manifest: WorkspaceInputManifest,
    validated: ValidatedAgentOutput,
    store: InMemoryResourceStore,
    claim: LogparseParseClaim | None = None,
    parse_request_bytes: bytes | None = None,
):
    return stage_validated_output(
        job=job,
        workspace_manifest=manifest,
        validated=validated,
        resource_store=store,
        claim=claim,
        parse_request_bytes=parse_request_bytes,
    )


def test_agent_failure_is_upgraded_only_at_server_job_outcome_boundary() -> None:
    job, manifest = _route_inputs()
    agent = AgentJobOutcome.model_validate(
        _fixture_payload("agent-job-outcome-failure.json")
    )
    assert type(agent.error) is AgentExecutionFailure

    staged = _stage(
        job=job,
        manifest=manifest,
        validated=_validated([], outcome=agent),
        store=InMemoryResourceStore(),
    )

    assert type(staged.outcome.error) is ExecutionFailure
    assert staged.outcome.error.reason_code is None
    assert staged.outcome.error.diagnostic_id is None


def _detail() -> ApplicationErrorDetail:
    return ApplicationErrorDetail(
        field="workspace_relative_path",
        resource_type=None,
        resource_id=None,
        resource_ref=None,
        expected="expected-value",
        actual="actual-value",
        limit=None,
        observed=None,
    )


def _port_error(
    code: ErrorCode,
) -> ApplicationPortError:
    return ApplicationPortError(
        ApplicationError(
            code=code,
            message=f"Injected {code.value}.",
            details=[_detail()],
            retryable=False,
        )
    )


def _assert_failure(
    captured: pytest.ExceptionInfo[RuntimeExecutionError],
    *,
    stage: ExecutionStage,
    code: ErrorCode,
    retryable: bool,
    details: list[ApplicationErrorDetail] | None = None,
) -> None:
    failure = captured.value.failure
    assert failure.stage is stage
    assert failure.code is code
    assert failure.retryable is retryable
    assert failure.details == ([] if details is None else details)


def test_stages_file_and_tree_then_audits_exact_receipts(tmp_path: Path) -> None:
    job, manifest = _route_inputs()
    file_resource = _file_resource(
        tmp_path,
        proposal_key="file_export",
        payload=b"one diagnostic file\n",
    )
    tree_root = tmp_path / "output/proposals/tree_export/tree"
    (tree_root / "nested").mkdir(parents=True)
    (tree_root / "summary.txt").write_bytes(b"summary\n")
    (tree_root / "nested/details.json").write_bytes(b'{"ok":true}\n')
    tree_resource = _tree_resource(
        root=tree_root,
        proposal_key="tree_export",
    )
    store = InMemoryResourceStore()

    staged = _stage(
        job=job,
        manifest=manifest,
        validated=_validated([file_resource, tree_resource]),
        store=store,
    )

    assert store.stage_file_calls == [(job.job_id, "file_export")]
    assert store.stage_tree_calls == [(job.job_id, "tree_export", tree_root)]
    assert store.validate_staged_calls == list(staged.staged_refs)
    assert store.discard_calls == []
    assert store.staged_resource_count == 2
    assert [proposal.proposal_key for proposal in staged.outcome.proposed_artifacts] == [
        "file_export",
        "tree_export",
    ]
    assert tuple(
        proposal.staged_resource_ref
        for proposal in staged.outcome.proposed_artifacts
    ) == staged.staged_refs
    assert staged.staged_refs[0].model_dump(mode="python") == {
        "staging_id": staged.staged_refs[0].staging_id,
        "owner_job_id": job.job_id,
        "proposal_key": "file_export",
        "resource_kind": ResourceKind.FILE,
        "size": file_resource.size,
        "sha256": file_resource.sha256,
        "tree_manifest": None,
    }
    assert staged.staged_refs[1].tree_manifest == tree_resource.tree_manifest


class _ReceiptMismatchStore(InMemoryResourceStore):
    def stage_file(
        self,
        owner_job_id: str,
        proposal_key: str,
        stream: Any,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StagedResourceRef:
        staged = super().stage_file(
            owner_job_id,
            proposal_key,
            stream,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
        return staged.model_copy(update={"owner_job_id": OTHER_JOB_ID})


def test_foreign_receipt_is_rejected_without_cross_job_discard(
    tmp_path: Path,
) -> None:
    job, manifest = _route_inputs()
    resource = _file_resource(
        tmp_path,
        proposal_key="bad_receipt",
        payload=b"receipt bytes",
    )
    store = _ReceiptMismatchStore()

    with pytest.raises(RuntimeExecutionError) as captured:
        _stage(
            job=job,
            manifest=manifest,
            validated=_validated([resource]),
            store=store,
        )

    _assert_failure(
        captured,
        stage=ExecutionStage.RESOURCE_STAGE,
        code=ErrorCode.RESOURCE_STAGE_FAILED,
        retryable=False,
    )
    assert store.stage_file_calls == [(job.job_id, "bad_receipt")]
    assert store.validate_staged_calls == []
    assert store.discard_calls == []
    # The mismatched receipt is untrusted.  Its staging id must not be used to
    # delete what could be another concurrently executing Job's resource.
    assert store.staged_resource_count == 1


@pytest.mark.parametrize(
    ("port_code", "expected_retryable"),
    [
        (ErrorCode.PATH_VIOLATION, False),
        (ErrorCode.RESOURCE_STAGE_FAILED, True),
    ],
)
def test_typed_stage_errors_collapse_to_frozen_stage_failure(
    tmp_path: Path,
    port_code: ErrorCode,
    expected_retryable: bool,
) -> None:
    job, manifest = _route_inputs()
    resource = _file_resource(
        tmp_path,
        proposal_key="stage_error",
        payload=b"never staged",
    )
    store = InMemoryResourceStore()
    store.inject_failure(
        "stage_file",
        _port_error(port_code),
    )

    with pytest.raises(RuntimeExecutionError) as captured:
        _stage(
            job=job,
            manifest=manifest,
            validated=_validated([resource]),
            store=store,
        )

    _assert_failure(
        captured,
        stage=ExecutionStage.RESOURCE_STAGE,
        code=ErrorCode.RESOURCE_STAGE_FAILED,
        retryable=expected_retryable,
        details=[_detail()],
    )
    assert store.stage_file_calls == []
    assert store.validate_staged_calls == []
    assert store.discard_calls == []
    assert store.staged_resource_count == 0


@pytest.mark.parametrize(
    ("port_code", "expected_code", "expected_retryable"),
    [
        (
            ErrorCode.RESOURCE_NOT_FOUND,
            ErrorCode.RESOURCE_STAGE_FAILED,
            True,
        ),
        (
            ErrorCode.RESOURCE_HASH_MISMATCH,
            ErrorCode.RESOURCE_HASH_MISMATCH,
            False,
        ),
    ],
)
def test_validate_missing_or_drift_maps_and_rolls_back(
    tmp_path: Path,
    port_code: ErrorCode,
    expected_code: ErrorCode,
    expected_retryable: bool,
) -> None:
    job, manifest = _route_inputs()
    resource = _file_resource(
        tmp_path,
        proposal_key="audit_error",
        payload=b"staged before audit",
    )
    store = InMemoryResourceStore()
    store.inject_failure("validate_staged", _port_error(port_code))

    with pytest.raises(RuntimeExecutionError) as captured:
        _stage(
            job=job,
            manifest=manifest,
            validated=_validated([resource]),
            store=store,
        )

    _assert_failure(
        captured,
        stage=ExecutionStage.RESOURCE_STAGE,
        code=expected_code,
        retryable=expected_retryable,
        details=[_detail()],
    )
    assert store.stage_file_calls == [(job.job_id, "audit_error")]
    assert store.validate_staged_calls == []
    assert len(store.discard_calls) == 1
    assert store.discard_calls[0].proposal_key == "audit_error"
    assert store.staged_resource_count == 0


def test_later_typed_stage_error_rolls_back_an_audited_prefix(
    tmp_path: Path,
) -> None:
    job, manifest = _route_inputs()
    file_resource = _file_resource(
        tmp_path,
        proposal_key="first_file",
        payload=b"must be rolled back",
    )
    tree_root = tmp_path / "output/proposals/second_tree/tree"
    tree_root.mkdir(parents=True)
    (tree_root / "result.txt").write_bytes(b"tree result")
    tree_resource = _tree_resource(
        root=tree_root,
        proposal_key="second_tree",
    )
    store = InMemoryResourceStore()
    store.inject_failure(
        "stage_tree",
        _port_error(ErrorCode.RESOURCE_STAGE_FAILED),
    )

    with pytest.raises(RuntimeExecutionError) as captured:
        _stage(
            job=job,
            manifest=manifest,
            validated=_validated([file_resource, tree_resource]),
            store=store,
        )

    _assert_failure(
        captured,
        stage=ExecutionStage.RESOURCE_STAGE,
        code=ErrorCode.RESOURCE_STAGE_FAILED,
        retryable=True,
        details=[_detail()],
    )
    assert store.stage_file_calls == [(job.job_id, "first_file")]
    assert store.stage_tree_calls == []
    assert [call.proposal_key for call in store.validate_staged_calls] == [
        "first_file"
    ]
    assert [call.proposal_key for call in store.discard_calls] == ["first_file"]
    assert store.staged_resource_count == 0


def test_user_result_is_canonically_revalidated_after_staging(
    tmp_path: Path,
) -> None:
    job = Job.model_validate(_fixture_payload("job-diagnose.json"))
    manifest = WorkspaceInputManifest.model_validate(
        _fixture_payload("workspace-input-manifest.json")
    )
    user_result_payload = _fixture_payload("user-result.json")
    user_result_payload["root_cause"] = "A different diagnosis."
    user_result = UserResultPayload.model_validate(user_result_payload)
    result_bytes = canonical_json_bytes(user_result)
    path = tmp_path / "output/proposals/user_result/diagnosis-result.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(result_bytes)

    outcome_payload = _fixture_payload("agent-job-outcome-diagnosis.json")
    draft_payload = outcome_payload["proposed_artifact_drafts"][0]
    draft_payload["declared_size"] = len(result_bytes)
    draft_payload["declared_sha256"] = hashlib.sha256(result_bytes).hexdigest()
    outcome = AgentJobOutcome.model_validate(outcome_payload)
    draft = outcome.proposed_artifact_drafts[0]
    resource = ValidatedProposalResource(
        draft=draft,
        proposal_key=draft.proposal_key,
        workspace_relative_path=draft.workspace_relative_path,
        path=path,
        resource_kind=ResourceKind.FILE,
        size=len(result_bytes),
        sha256=hashlib.sha256(result_bytes).hexdigest(),
        tree_manifest=None,
        source_snapshot=_snapshot_source(tmp_path, draft.workspace_relative_path),
    )
    store = InMemoryResourceStore()

    with pytest.raises(RuntimeExecutionError) as captured:
        _stage(
            job=job,
            manifest=manifest,
            validated=_validated(
                [resource],
                outcome=outcome,
                user_result=user_result,
            ),
            store=store,
        )

    _assert_failure(
        captured,
        stage=ExecutionStage.OUTCOME_VALIDATE,
        code=ErrorCode.OUTCOME_INVALID,
        retryable=False,
    )
    assert store.stage_file_calls == [(job.job_id, "user_result")]
    assert [call.proposal_key for call in store.validate_staged_calls] == [
        "user_result"
    ]
    assert [call.proposal_key for call in store.discard_calls] == ["user_result"]
    assert store.staged_resource_count == 0


def _logparse_inputs(
    tmp_path: Path,
    *,
    tree_shape: str = "valid",
) -> tuple[
    Job,
    WorkspaceInputManifest,
    ValidatedAgentOutput,
    LogparseParseClaim,
    bytes,
    Path,
]:
    job_payload = _fixture_payload("job-diagnose.json")
    job_payload["artifact_refs"] = []
    job = Job.model_validate(job_payload)

    manifest_payload = _fixture_payload("workspace-input-manifest.json")
    manifest_payload["entries"] = [
        entry
        for entry in manifest_payload["entries"]
        if entry["input_kind"] != "ARTIFACT"
    ]
    attachment_id = next(
        entry["resource_id"]
        for entry in manifest_payload["entries"]
        if entry["input_kind"] == "ATTACHMENT"
    )
    manifest_payload["resolved_logparse_plan"].update(
        attachment_id=attachment_id,
        artifact_id=None,
    )
    manifest = WorkspaceInputManifest.model_validate(manifest_payload)

    root = tmp_path / "output/proposals/logparse_run/tree"
    task = root / "task-0001"
    task.mkdir(parents=True)
    if tree_shape != "missing_manifest":
        (task / "parse_manifest.json").write_bytes(
            canonical_json_bytes(
                {
                    "product": job.logparse_product,
                    "target_logs": ["targets/request.log"],
                    "version": 1,
                }
            )
        )
    (task / "targets").mkdir()
    (task / "targets/request.log").write_bytes(b"request timed out\n")
    if tree_shape == "multiple_tasks":
        second = root / "task-0002"
        second.mkdir()
        (second / "parse_manifest.json").write_bytes(b"{}\n")

    size, tree_sha256, tree_manifest = _tree_metadata(root)
    attachment = next(
        entry for entry in manifest.entries if entry.input_kind == "ATTACHMENT"
    )
    relative_path = "output/proposals/logparse_run/tree"
    draft = AgentArtifactProposalDraft(
        proposal_key="logparse_run",
        artifact_kind=ArtifactKind.LOGPARSE_RUN,
        name="new-logparse-run",
        content_type="application/vnd.problem-locator.logparse-run+directory",
        resource_kind=ResourceKind.DIRECTORY,
        workspace_relative_path=relative_path,
        declared_size=size,
        declared_sha256=tree_sha256,
        metadata={
            "tree_manifest_sha256": "0" * 64,
            "logparse_version_ref": job.logparse_tool_ref,
            "parse_manifest_relative_path": "declared/parse_manifest.json",
            "source_attachment_id": attachment.resource_id,
            "source_attachment_sha256": attachment.sha256,
            "parse_parameters": {"product": job.logparse_product},
        },
    )
    outcome_payload = _fixture_payload("agent-job-outcome-diagnosis.json")
    outcome_payload["payload"]["candidate_conclusion_draft"] = None
    outcome_payload["proposed_artifact_drafts"] = [draft.model_dump(mode="json")]
    outcome = AgentJobOutcome.model_validate(outcome_payload)
    resource = ValidatedProposalResource(
        draft=outcome.proposed_artifact_drafts[0],
        proposal_key="logparse_run",
        workspace_relative_path=relative_path,
        path=root,
        resource_kind=ResourceKind.DIRECTORY,
        size=size,
        sha256=tree_sha256,
        tree_manifest=tree_manifest,
        source_snapshot=_snapshot_source(tmp_path, relative_path),
    )

    request_bytes = canonical_json_bytes(
        {
            "attachment_id": attachment.resource_id,
            "product": job.logparse_product,
        }
    )
    claim_payload = _fixture_payload("logparse-parse-claim.json")
    claim_payload["request_sha256"] = hashlib.sha256(request_bytes).hexdigest()
    claim = LogparseParseClaim.model_validate(claim_payload)
    return (
        job,
        manifest,
        _validated([resource], outcome=outcome),
        claim,
        request_bytes,
        root,
    )


def test_logparse_run_rebuilds_metadata_and_closes_claim_request_hash_seam(
    tmp_path: Path,
) -> None:
    job, manifest, validated, claim, request_bytes, root = _logparse_inputs(
        tmp_path
    )
    store = InMemoryResourceStore()

    staged = _stage(
        job=job,
        manifest=manifest,
        validated=validated,
        store=store,
        claim=claim,
        parse_request_bytes=request_bytes,
    )

    assert claim.request_sha256 == hashlib.sha256(request_bytes).hexdigest()
    assert store.stage_tree_calls == [(job.job_id, "logparse_run", root)]
    assert store.validate_staged_calls == list(staged.staged_refs)
    assert store.discard_calls == []
    artifact = staged.outcome.proposed_artifacts[0]
    assert isinstance(artifact.metadata, LogparseRunMetadata)
    assert artifact.metadata.tree_manifest_sha256 == staged.staged_refs[0].sha256
    assert (
        artifact.metadata.parse_manifest_relative_path
        == "task-0001/parse_manifest.json"
    )
    assert artifact.metadata.logparse_version_ref == job.logparse_tool_ref
    assert artifact.metadata.source_attachment_id == claim.attachment_id
    assert artifact.metadata.source_attachment_sha256 == claim.attachment_sha256
    assert artifact.metadata.parse_parameters.product == job.logparse_product
    original_metadata = validated.outcome.proposed_artifact_drafts[0].metadata
    assert isinstance(original_metadata, LogparseRunMetadata)
    assert original_metadata.tree_manifest_sha256 == "0" * 64
    assert (
        original_metadata.parse_manifest_relative_path
        == "declared/parse_manifest.json"
    )


def test_logparse_shape_uses_immutable_staged_manifest_after_workspace_mutation(
    tmp_path: Path,
) -> None:
    job, manifest, validated, claim, request_bytes, root = _logparse_inputs(
        tmp_path,
        tree_shape="multiple_tasks",
    )

    class MutatingStore(InMemoryResourceStore):
        def stage_tree(
            self,
            owner_job_id: str,
            proposal_key: str,
            source_dir: Path,
            expected_manifest_hash: str | None = None,
        ) -> StagedResourceRef:
            staged = super().stage_tree(
                owner_job_id,
                proposal_key,
                source_dir,
                expected_manifest_hash=expected_manifest_hash,
            )
            shutil.rmtree(source_dir / "task-0002")
            return staged

    store = MutatingStore()

    with pytest.raises(RuntimeExecutionError) as captured:
        _stage(
            job=job,
            manifest=manifest,
            validated=validated,
            store=store,
            claim=claim,
            parse_request_bytes=request_bytes,
        )

    _assert_failure(
        captured,
        stage=ExecutionStage.RESOURCE_STAGE,
        code=ErrorCode.RESOURCE_STAGE_FAILED,
        retryable=False,
    )
    assert store.validate_staged_calls == []
    assert len(store.discard_calls) == 1
    assert store.discard_calls[0].proposal_key == "logparse_run"
    assert store.staged_resource_count == 0


def test_file_leaf_replacement_before_stage_fails_without_store_call(
    tmp_path: Path,
) -> None:
    job, manifest = _route_inputs()
    resource = _file_resource(
        tmp_path,
        proposal_key="raced_file",
        payload=b"validated bytes",
    )
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"outside replacement")
    replacement.replace(resource.path)
    store = InMemoryResourceStore()

    with pytest.raises(RuntimeExecutionError) as captured:
        _stage(
            job=job,
            manifest=manifest,
            validated=_validated([resource]),
            store=store,
        )

    _assert_failure(
        captured,
        stage=ExecutionStage.RESOURCE_STAGE,
        code=ErrorCode.RESOURCE_STAGE_FAILED,
        retryable=False,
    )
    assert store.stage_file_calls == []
    assert store.validate_staged_calls == []
    assert store.staged_resource_count == 0


@pytest.mark.parametrize("tree_shape", ["multiple_tasks", "missing_manifest"])
def test_logparse_tree_shape_or_manifest_failure_rolls_back(
    tmp_path: Path,
    tree_shape: str,
) -> None:
    job, manifest, validated, claim, request_bytes, root = _logparse_inputs(
        tmp_path,
        tree_shape=tree_shape,
    )
    store = InMemoryResourceStore()

    with pytest.raises(RuntimeExecutionError) as captured:
        _stage(
            job=job,
            manifest=manifest,
            validated=validated,
            store=store,
            claim=claim,
            parse_request_bytes=request_bytes,
        )

    _assert_failure(
        captured,
        stage=ExecutionStage.TOOL_EXECUTE,
        code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
        retryable=False,
    )
    assert store.stage_tree_calls == [(job.job_id, "logparse_run", root)]
    assert [call.proposal_key for call in store.validate_staged_calls] == [
        "logparse_run"
    ]
    assert [call.proposal_key for call in store.discard_calls] == ["logparse_run"]
    assert store.staged_resource_count == 0


def test_logparse_request_hash_mismatch_fails_before_staging(
    tmp_path: Path,
) -> None:
    job, manifest, validated, claim, _, _ = _logparse_inputs(tmp_path)
    different_canonical_request = canonical_json_bytes(
        {"attachment_id": claim.attachment_id, "product": "different-product"}
    )
    store = InMemoryResourceStore()

    with pytest.raises(RuntimeExecutionError) as captured:
        _stage(
            job=job,
            manifest=manifest,
            validated=validated,
            store=store,
            claim=claim,
            parse_request_bytes=different_canonical_request,
        )

    _assert_failure(
        captured,
        stage=ExecutionStage.TOOL_EXECUTE,
        code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
        retryable=False,
    )
    assert store.stage_tree_calls == []
    assert store.validate_staged_calls == []
    assert store.discard_calls == []
    assert store.staged_resource_count == 0


def test_logparse_claim_proposal_mismatch_fails_before_staging(
    tmp_path: Path,
) -> None:
    job, manifest, validated, claim, request_bytes, _ = _logparse_inputs(tmp_path)
    mismatched_claim = claim.model_copy(
        update={"artifact_proposal_key": "different_logparse_run"}
    )
    store = InMemoryResourceStore()

    with pytest.raises(RuntimeExecutionError) as captured:
        _stage(
            job=job,
            manifest=manifest,
            validated=validated,
            store=store,
            claim=mismatched_claim,
            parse_request_bytes=request_bytes,
        )

    _assert_failure(
        captured,
        stage=ExecutionStage.TOOL_EXECUTE,
        code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
        retryable=False,
    )
    assert store.stage_tree_calls == []
    assert store.validate_staged_calls == []
    assert store.discard_calls == []
    assert store.staged_resource_count == 0
