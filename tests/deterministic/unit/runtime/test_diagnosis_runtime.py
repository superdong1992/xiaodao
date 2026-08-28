from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import threading
import shutil
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from problem_locator.contracts import (
    AgentArtifactProposalDraft,
    AgentJobOutcomeDraftV2,
    ApplicationError,
    ApplicationErrorDetail,
    ApplicationPortError,
    Attachment,
    AttachmentRequirementConstraints,
    AttachmentStatus,
    ArtifactKind,
    AssetKind,
    CaseAggregate,
    DiagnosisMode,
    ErrorCode,
    ERROR_SPECS,
    ExecutionFileRef,
    ExecutionFailure,
    ExecutionLogSinks,
    ExecutionStage,
    FixtureManifest,
    GenericDiagnosisOutcome,
    GenericDiagnosisOutcomeV2,
    GenericResultStatus,
    Job,
    JobOutcome,
    JobStatus,
    InputRequirementConstraints,
    LogparseBrokerError,
    LogparseParseClaim,
    MaterializedPath,
    OutcomeResultType,
    ResolvedAsset,
    ResourceKind,
    ResourceRef,
    RouteDecision,
    RouteKind,
    RuntimeExecutionReceipt,
    RuntimeInfrastructureError,
    StateFile,
    VersionedRef,
    WorkspaceInputManifest,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.contracts.enums import MethodsValidationReasonCode
from problem_locator.diagnostics import bind_diagnostics
from problem_locator.integrations.logparse import Anchor, ParseTargetsRequest
from problem_locator.journey import configure_journey
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeLogparseBrokerFactory,
    InMemoryCancellationSignal,
    InMemoryExecutionRecordStore,
    InMemoryResourceStore,
    InMemoryStateRepository,
)
from problem_locator.runtime.agent_backend import BackendExecution
from problem_locator.runtime.catalog import BUILTIN_ASSET_ROOT, VersionedAssetCatalog
from problem_locator.runtime.context_builder import ContextBuilder, ContextLimitExceeded
from problem_locator.runtime.context_policy import RuntimeAssetResolver
from problem_locator.runtime.diagnosis_runtime import (
    DiagnosisRuntime,
    MethodsPreprocessingExecution,
    _discard_unreferenced_staged,
    _method_validation_reason_code,
)
from problem_locator.runtime.failures import RuntimeExecutionError, runtime_failure
from problem_locator.runtime.generic_locator import (
    GENERIC_RESULT_FILENAME,
    GENERIC_RESULT_V2_FILENAME,
    MAX_GENERIC_REPORT_BYTES,
    MAX_GENERIC_RESULT_BYTES,
)
from problem_locator.runtime.outcome_publisher import OutcomePublisher
from problem_locator.runtime.outcome_finalizer import (
    DRAFT_FINALIZATION_MARKER_RELATIVE_PATH,
    SealedAgentOutcomeDraftMarker,
)
from problem_locator.runtime.workspace import (
    WorkspaceManager,
    _verify_materialized,
    inspect_tree,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def _write_finalization_marker(workspace_root: Path, outcome_bytes: bytes) -> None:
    marker_path = workspace_root / DRAFT_FINALIZATION_MARKER_RELATIVE_PATH
    marker_path.write_bytes(
        canonical_json_bytes(
            SealedAgentOutcomeDraftMarker(
                schema_version=2,
                relative_path="output/job_outcome.draft.json",
                size=len(outcome_bytes),
                sha256=hashlib.sha256(outcome_bytes).hexdigest(),
            )
        )
    )


@pytest.fixture
def journey_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_journey(stream=stream)
    yield stream
    configure_journey()
CONTRACT_FIXTURES = REPOSITORY_ROOT / "tests/fixtures/contracts/positive"
CATALOG_FIXTURES = REPOSITORY_ROOT / "tests/fixtures/components/runtime-catalog"
LOGPARSE_ROOT = CATALOG_FIXTURES / "logparse-tool"
LOGPARSE_FIXTURES = REPOSITORY_ROOT / "tests/fixtures/components/runtime-logparse"


def _json(name: str) -> Any:
    return json.loads((CONTRACT_FIXTURES / name).read_bytes())


def _route_job() -> Job:
    return Job.model_validate(_json("job-route.json"))


class _Clock:
    def __init__(self) -> None:
        self.calls = 0

    def now(self) -> str:
        self.calls += 1
        return "2026-01-02T03:04:05.000Z"


class _Ids:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def new(self, kind: str) -> str:
        self.calls.append(kind)
        values = {
            "job_outcome": "00000000-0000-4000-8000-000000000401",
            "execution_failure": "00000000-0000-4000-8000-000000000402",
            "diagnostic": "00000000-0000-4000-8000-000000000403",
        }
        return values[kind]

    def derive(self, kind: str, stable_parts: list[str]) -> str:
        raise AssertionError("Runtime publication must not derive IDs")


class _Records:
    def __init__(
        self,
        failures: int = 0,
        *,
        after_write_failures: int = 0,
        read_failures: int = 0,
    ) -> None:
        self.failures = failures
        self.after_write_failures = after_write_failures
        self.read_failures = read_failures
        self.calls: list[tuple[str, bytes]] = []
        self.read_calls: list[str] = []
        self.published: dict[str, bytes] = {}

    @staticmethod
    def _file_ref(job_id: str, canonical_bytes: bytes) -> ExecutionFileRef:
        return ExecutionFileRef(
            relative_key=f"jobs/{job_id}/job_outcome.json",
            size=len(canonical_bytes),
            sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        )

    def publish_outcome_bytes(self, job_id: str, canonical_bytes: bytes) -> ExecutionFileRef:
        self.calls.append((job_id, canonical_bytes))
        if self.failures:
            self.failures -= 1
            raise ApplicationPortError(
                ApplicationError(
                    code=ErrorCode.EXECUTION_RECORD_FAILED,
                    message="Injected execution record failure.",
                    details=[],
                    retryable=ERROR_SPECS[
                        ErrorCode.EXECUTION_RECORD_FAILED
                    ].application_retryable,
                )
            )
        existing = self.published.get(job_id)
        if existing is not None and existing != canonical_bytes:
            raise _application_port_error(ErrorCode.IDEMPOTENCY_CONFLICT)
        self.published[job_id] = canonical_bytes
        if self.after_write_failures:
            self.after_write_failures -= 1
            raise _application_port_error(ErrorCode.EXECUTION_RECORD_FAILED)
        return self._file_ref(job_id, canonical_bytes)

    def read_published_outcome(
        self,
        job_id: str,
    ) -> RuntimeExecutionReceipt | None:
        self.read_calls.append(job_id)
        if self.read_failures:
            self.read_failures -= 1
            raise _application_port_error(ErrorCode.EXECUTION_RECORD_FAILED)
        canonical_bytes = self.published.get(job_id)
        if canonical_bytes is None:
            return None
        outcome = parse_canonical_json_bytes(canonical_bytes, JobOutcome)
        return RuntimeExecutionReceipt(
            job_outcome=outcome,
            outcome_file_ref=self._file_ref(job_id, canonical_bytes),
        )


class _AfterWriteExecutionRecordStore(InMemoryExecutionRecordStore):
    def __init__(self, after_write_failures: int) -> None:
        super().__init__()
        self.after_write_failures = after_write_failures

    def publish_outcome_bytes(
        self,
        job_id: str,
        canonical_bytes: bytes,
    ) -> ExecutionFileRef:
        receipt = super().publish_outcome_bytes(job_id, canonical_bytes)
        if self.after_write_failures:
            self.after_write_failures -= 1
            raise _application_port_error(ErrorCode.EXECUTION_RECORD_FAILED)
        return receipt


def _failure() -> RuntimeExecutionError:
    return runtime_failure(
        stage=ExecutionStage.BACKEND_EXECUTE,
        code=ErrorCode.BACKEND_TIMEOUT,
        message="Agent execution exceeded the fixed wall time.",
        retryable=True,
    )


def test_system_failure_outcome_uses_only_injected_clock_and_id() -> None:
    records = _Records()
    clock = _Clock()
    ids = _Ids()
    publisher = OutcomePublisher(records, clock, ids)

    receipt = publisher.publish_failure(_route_job(), _failure().failure)

    outcome = receipt.job_outcome
    assert outcome.outcome_id == "00000000-0000-4000-8000-000000000401"
    assert outcome.produced_at == "2026-01-02T03:04:05.000Z"
    assert outcome.error is not None
    assert outcome.error.code is ErrorCode.BACKEND_TIMEOUT
    assert ids.calls == ["job_outcome"]
    assert clock.calls == 1
    assert records.calls == [(outcome.job_id, canonical_json_bytes(outcome))]


def test_success_publication_failure_falls_back_to_replayable_failure_outcome() -> None:
    job = _route_job()
    success = parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / "job-outcome-route.json").read_bytes(),
        model_type=JobOutcome,
    )
    records = _Records(failures=1)
    ids = _Ids()
    publisher = OutcomePublisher(records, _Clock(), ids)

    receipt = publisher.publish_success(job, success)

    assert len(records.calls) == 2
    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.EXECUTION_RECORD_FAILED
    assert ids.calls == ["job_outcome"]
    assert records.read_calls == [job.job_id]


def test_after_replace_failure_republishes_exact_success_bytes_before_return() -> None:
    job = _route_job()
    success = parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / "job-outcome-route.json").read_bytes(),
        model_type=JobOutcome,
    )
    records = _Records(after_write_failures=1)
    clock = _Clock()
    ids = _Ids()
    publisher = OutcomePublisher(records, clock, ids)

    receipt = publisher.publish_success(job, success)

    expected = canonical_json_bytes(success)
    assert receipt.job_outcome == success
    assert records.calls == [(job.job_id, expected), (job.job_id, expected)]
    assert records.read_calls == [job.job_id]
    assert records.published[job.job_id] == expected
    assert ids.calls == []
    assert clock.calls == 0


def test_different_existing_outcome_is_validated_and_readopted_before_return() -> None:
    job = _route_job()
    success = parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / "job-outcome-route.json").read_bytes(),
        model_type=JobOutcome,
    )
    records = _Records()
    first = OutcomePublisher(records, _Clock(), _Ids()).publish_failure(
        job,
        _failure().failure,
    )
    publisher = OutcomePublisher(records, _Clock(), _Ids())

    adopted = publisher.publish_success(job, success)

    existing_bytes = canonical_json_bytes(first.job_outcome)
    assert adopted == first
    assert records.read_calls == [job.job_id]
    assert records.calls[-2:] == [
        (job.job_id, canonical_json_bytes(success)),
        (job.job_id, existing_bytes),
    ]


def test_persistent_after_replace_failure_is_context_free_and_keeps_success() -> None:
    job = _route_job()
    success = parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / "job-outcome-route.json").read_bytes(),
        model_type=JobOutcome,
    )
    records = _Records(after_write_failures=2)
    publisher = OutcomePublisher(records, _Clock(), _Ids())

    with pytest.raises(RuntimeInfrastructureError) as captured:
        publisher.publish_success(job, success)

    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None
    assert records.published[job.job_id] == canonical_json_bytes(success)
    assert len(records.calls) == 2
    assert records.read_calls == [job.job_id]


def test_after_replace_failure_outcome_is_readopted_without_infrastructure_id() -> None:
    job = _route_job()
    records = _Records(after_write_failures=1)
    ids = _Ids()
    publisher = OutcomePublisher(records, _Clock(), ids)

    receipt = publisher.publish_failure(job, _failure().failure)

    expected = canonical_json_bytes(receipt.job_outcome)
    assert records.calls == [(job.job_id, expected), (job.job_id, expected)]
    assert records.read_calls == [job.job_id]
    assert ids.calls == ["job_outcome"]


def test_failure_record_publication_is_the_only_infrastructure_exception() -> None:
    ids = _Ids()
    publisher = OutcomePublisher(_Records(failures=1), _Clock(), ids)

    with pytest.raises(RuntimeInfrastructureError) as captured:
        publisher.publish_failure(_route_job(), _failure().failure)

    assert captured.value.failure_id == "00000000-0000-4000-8000-000000000402"
    assert captured.value.execution_failure.stage is ExecutionStage.EXECUTION_RECORD
    assert captured.value.execution_failure.code is ErrorCode.EXECUTION_RECORD_FAILED
    assert ids.calls == ["job_outcome", "execution_failure"]
    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None


def test_execution_record_read_failure_is_context_free_infrastructure_error() -> None:
    ids = _Ids()
    records = _Records(failures=1, read_failures=1)
    publisher = OutcomePublisher(records, _Clock(), ids)

    with pytest.raises(RuntimeInfrastructureError) as captured:
        publisher.publish_failure(_route_job(), _failure().failure)

    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None
    assert ids.calls == ["job_outcome", "execution_failure"]


class _UnusedResourceStore:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"ROUTE Workspace must not call ResourceStore.{name}")


class _BrokerFactory:
    def open(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("asset resolution must not open the broker")


def _route_aggregate(job: Job) -> CaseAggregate:
    state = StateFile.model_validate(_json("state.json"))
    aggregate = next(iter(state.cases.values()))
    payload = aggregate.model_dump(mode="json")
    payload["jobs"] = {job.job_id: job.model_dump(mode="json")}
    return CaseAggregate.model_validate(payload)


def _make_route_catalog(tmp_path: Path) -> VersionedAssetCatalog:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    shutil.copytree(
        CATALOG_FIXTURES / "skill-dir/manual-triage",
        skill_dir / "manual-triage",
    )
    return VersionedAssetCatalog(
        skill_dir=skill_dir,
        generic_skill_name="generic-problem-locator-smoke",
    )


def _job_from_catalog(catalog: VersionedAssetCatalog) -> Job:
    base = _route_job().model_dump(mode="json")
    bindings = catalog.route_bindings()
    base.update(bindings.model_dump(mode="json"))
    return Job.model_validate(base)


def test_route_skill_index_v2_exposes_only_the_complete_namespaced_ref(
    tmp_path: Path,
) -> None:
    catalog = _make_route_catalog(tmp_path)
    job = _job_from_catalog(catalog)
    resolved = RuntimeAssetResolver(catalog).resolve_job(job)
    assert resolved.skill_index_text is not None
    index = json.loads(resolved.skill_index_text)

    assert index["schema_version"] == 2
    assert len(index["skills"]) == 1
    skill = index["skills"][0]
    assert "registration_id" not in skill
    assert skill["ref"] == job.available_skill_refs[0].model_dump(mode="json")
    assert skill["ref"]["id"] == "diagnosis-skill/manual-triage"

    route_contract = (
        BUILTIN_ASSET_ROOT / "output-contracts" / "route" / "output-contract.md"
    ).read_text(encoding="utf-8")
    assert "complete `ref` object is the only valid source" in route_contract
    assert "never remove the `diagnosis-skill/` namespace" in route_contract
    assert "exactly equal to" in route_contract
    assert catalog.route_bindings().output_contract_ref.version == "3.0.0"


def _restore_permissions(root: Path) -> None:
    inputs = root / "inputs"
    if not inputs.exists():
        return
    for path in sorted(inputs.rglob("*"), reverse=True):
        path.chmod(0o755 if path.is_dir() else 0o644)
    inputs.chmod(0o755)


def test_workspace_asset_resolution_context_and_manifest_are_one_fixed_view(
    tmp_path: Path,
) -> None:
    catalog = _make_route_catalog(tmp_path)
    job = _job_from_catalog(catalog)
    manager = WorkspaceManager(tmp_path / "data")
    workspace = manager.prepare(job, _route_aggregate(job), _UnusedResourceStore())
    try:
        resolved = RuntimeAssetResolver(catalog).resolve(job, workspace)
        context = ContextBuilder().build(job, resolved.materials)
        manager.write_context(workspace, context.body)

        assert workspace.manifest_bytes == canonical_json_bytes(workspace.manifest)
        assert resolved.materials.manifest == workspace.manifest
        assert workspace.context_path.read_bytes() == context.body.encode("utf-8")
        assert context.body.endswith(
            "<<<END SECTION>>>\n"
        )
        assert canonical_json_bytes(workspace.manifest) in context.body.encode("utf-8")
        assert [ref.id for ref in job.available_skill_refs] == [
            "diagnosis-skill/manual-triage"
        ]
    finally:
        _restore_permissions(workspace.root)


def test_asset_content_drift_never_substitutes_the_frozen_job_version(
    tmp_path: Path,
) -> None:
    catalog = _make_route_catalog(tmp_path)
    job = _job_from_catalog(catalog)
    manager = WorkspaceManager(tmp_path / "data")
    workspace = manager.prepare(job, _route_aggregate(job), _UnusedResourceStore())
    try:
        skill = catalog.resolve(job.available_skill_refs[0])
        entry = (
            Path(skill.root_path)
            / "package"
            / "manual-triage"
            / "SKILL.md"
        )
        entry.write_text(entry.read_text() + "\nchanged after startup\n", encoding="utf-8")

        with pytest.raises(RuntimeExecutionError) as captured:
            RuntimeAssetResolver(catalog).resolve(job, workspace)

        assert captured.value.failure.stage is ExecutionStage.ASSET_RESOLUTION
        assert captured.value.failure.code is ErrorCode.ASSET_VERSION_UNAVAILABLE
    finally:
        _restore_permissions(workspace.root)


def _running_route_job(catalog: VersionedAssetCatalog) -> Job:
    payload = _job_from_catalog(catalog).model_dump(mode="json")
    payload.update(
        {
            "status": "RUNNING",
            "started_at": "2026-07-31T00:00:01.000Z",
            "runtime_epoch": "00000000-0000-4000-8000-000000000499",
        }
    )
    return Job.model_validate(payload)


_GENERIC_RAW_PROBLEM = (
    "订单支付成功后页面仍显示“处理中”。\n"
    "request-id: 订单-α-42\n"
    "已确认：刷新三次仍复现"
)


def _running_generic_job(catalog: VersionedAssetCatalog) -> Job:
    payload = _route_job().model_dump(mode="json")
    bindings = catalog.generic_diagnose_bindings()
    payload.update(bindings.model_dump(mode="json"))
    payload.update(
        {
            "job_type": "DIAGNOSE",
            "diagnosis_mode": "GENERIC",
            "generic_skill_name": "generic-problem-locator-smoke",
            "generic_problem_text": _GENERIC_RAW_PROBLEM,
            "status": "RUNNING",
            "goal": "Run the configured generic problem locator.",
            "context_snapshot": None,
            "evidence_refs": [],
            "attachment_refs": [],
            "previous_outcome_refs": [],
            "artifact_refs": [],
            "available_skill_refs": [],
            "skill_ref": None,
            "review_target": None,
            "started_at": "2026-07-31T00:00:01.000Z",
            "runtime_epoch": "00000000-0000-4000-8000-000000000499",
        }
    )
    return Job.model_validate(payload)


def _generic_aggregate(job: Job) -> CaseAggregate:
    state = StateFile.model_validate(_json("state.json"))
    aggregate = next(iter(state.cases.values()))
    payload = aggregate.model_dump(mode="json")
    payload["case"].update(
        raw_problem_text=job.generic_problem_text,
        active_job_id=job.job_id,
        selected_skill_ref=None,
    )
    payload["jobs"] = {job.job_id: job.model_dump(mode="json")}
    return CaseAggregate.model_validate(payload)


def _generic_result_bytes(
    *,
    status: str = "RESOLVED",
    conclusion: str = "generic-skill-input-contract-ok",
    root_cause: str = "已逐字确认通用定位输入与预期一致。",
) -> bytes:
    return (
        "<<<GENERIC_DIAGNOSIS_RESULT_V1>>>\n"
        f"STATUS: {status}\n"
        "CONCLUSION:\n"
        f"{conclusion}\n"
        "ROOT_CAUSE_ANALYSIS:\n"
        f"{root_cause}\n"
        "<<<END_GENERIC_DIAGNOSIS_RESULT_V1>>>\n"
    ).encode("utf-8")


def _generic_v2_result_bytes(
    report_markdown: str | bytes,
    *,
    status: str = "RESOLVED",
) -> bytes:
    body = (
        report_markdown.encode("utf-8")
        if isinstance(report_markdown, str)
        else report_markdown
    )
    return (
        f"<<<GENERIC_DIAGNOSIS_RESULT_V2:{status}>>>\n".encode("ascii") + body
    )


def _route_agent_outcome(job: Job) -> AgentJobOutcomeDraftV2:
    payload = _json("agent-job-outcome-draft-route.json")
    payload.update(
        {
            "job_id": job.job_id,
            "case_id": job.case_id,
            "job_type": job.job_type.value,
            "base_state_revision": job.base_state_revision,
        }
    )
    payload["payload"]["skill_ref"] = job.available_skill_refs[0].model_dump(
        mode="json"
    )
    return AgentJobOutcomeDraftV2.model_validate(payload)


def _route_agent_no_capability(job: Job) -> AgentJobOutcomeDraftV2:
    payload = _route_agent_outcome(job).model_dump(mode="python")
    payload.update(
        result_type=OutcomeResultType.NO_CAPABILITY,
        payload=RouteDecision(
            kind=RouteKind.NO_CAPABILITY,
            skill_ref=None,
            reason="No specialized Skill semantically matches the problem.",
            confidence=0.91,
        ),
    )
    return AgentJobOutcomeDraftV2.model_validate(payload)


class _StateView:
    def __init__(
        self,
        aggregate: CaseAggregate,
        *,
        failure: BaseException | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.aggregate = aggregate
        self.failure = failure
        self.calls: list[str] = []
        self.events = events

    def read_case(self, case_id: str) -> CaseAggregate:
        self.calls.append(case_id)
        if self.events is not None:
            self.events.append("state")
        if self.failure is not None:
            raise self.failure
        return self.aggregate.model_copy(deep=True)


class _RuntimeBackend:
    def __init__(
        self,
        outcome_bytes: bytes | None,
        *,
        events: list[str] | None = None,
        proposal_files: dict[str, bytes] | None = None,
        tool_state_files: dict[str, bytes] | None = None,
    ) -> None:
        self.outcome_bytes = outcome_bytes
        self.calls: list[dict[str, Any]] = []
        self.events = events
        self.proposal_files = proposal_files or {}
        self.tool_state_files = tool_state_files or {}

    def execute(self, **kwargs: Any) -> BackendExecution:
        self.calls.append(kwargs)
        if self.events is not None:
            self.events.append("backend")
        workspace_root = Path(kwargs["workspace_root"])
        for name, payload in self.tool_state_files.items():
            (workspace_root / "runtime" / "tool-state" / name).write_bytes(payload)
        for relative_path, payload in self.proposal_files.items():
            proposal_path = workspace_root / relative_path
            proposal_path.parent.mkdir(parents=True, exist_ok=True)
            proposal_path.write_bytes(payload)
        if self.outcome_bytes is not None:
            temporary = workspace_root / "output" / ".job_outcome.draft.json.part"
            temporary.write_bytes(self.outcome_bytes)
            os.replace(
                temporary,
                workspace_root / "output" / "job_outcome.draft.json",
            )
            _write_finalization_marker(workspace_root, self.outcome_bytes)
        sinks: ExecutionLogSinks = kwargs["log_sinks"]
        unique = {id(sinks.stdout): sinks.stdout, id(sinks.stderr): sinks.stderr}
        for sink in unique.values():
            sink.flush()
            sink.close()
        return BackendExecution(
            returncode=0,
            stdout_stderr_bytes=0,
            workspace_bytes=0,
            elapsed_seconds=0.01,
        )


class _NeverBackend:
    def execute(self, **kwargs: Any) -> BackendExecution:
        del kwargs
        raise AssertionError("Backend must not be called")


class _GenericRuntimeBackend:
    def __init__(
        self,
        result_bytes: bytes | None,
        *,
        v2_result_bytes: bytes | None = None,
        result_writer: Callable[[Path], None] | None = None,
        failure: RuntimeExecutionError | None = None,
    ) -> None:
        self.result_bytes = result_bytes
        self.v2_result_bytes = v2_result_bytes
        self.result_writer = result_writer
        self.failure = failure
        self.calls: list[dict[str, Any]] = []

    def execute(self, **kwargs: Any) -> BackendExecution:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        workspace_root = Path(kwargs["workspace_root"])
        if self.result_bytes is not None:
            (workspace_root / "output" / GENERIC_RESULT_FILENAME).write_bytes(
                self.result_bytes
            )
        if self.v2_result_bytes is not None:
            (workspace_root / "output" / GENERIC_RESULT_V2_FILENAME).write_bytes(
                self.v2_result_bytes
            )
        if self.result_writer is not None:
            self.result_writer(workspace_root / "output")
        sinks: ExecutionLogSinks = kwargs["log_sinks"]
        unique = {id(sinks.stdout): sinks.stdout, id(sinks.stderr): sinks.stderr}
        for sink in unique.values():
            sink.flush()
            sink.close()
        return BackendExecution(
            returncode=0,
            stdout_stderr_bytes=0,
            workspace_bytes=0,
            elapsed_seconds=0.01,
        )


class _TooLargeContext:
    def build(self, job: Job, materials: Any) -> object:
        del job, materials
        raise ContextLimitExceeded(131073, 131072)


class _UnsafeExplodingContext:
    def build(self, job: Job, materials: Any) -> object:
        del job, materials
        raise OSError("/private/unsafe-path/with-runtime-token")


def _application_port_error(code: ErrorCode) -> ApplicationPortError:
    return ApplicationPortError(
        ApplicationError(
            code=code,
            message="Injected typed Port failure.",
            details=[],
            retryable=ERROR_SPECS[code].application_retryable,
        )
    )


def _runtime_fixture(
    tmp_path: Path,
    *,
    backend: _RuntimeBackend | _NeverBackend | None = None,
    state_failure: BaseException | None = None,
    records: InMemoryExecutionRecordStore | None = None,
    resource_store: Any = None,
    context_builder: Any = None,
) -> tuple[
    DiagnosisRuntime,
    Job,
    _StateView,
    _RuntimeBackend | _NeverBackend,
    Any,
]:
    catalog = _make_route_catalog(tmp_path)
    job = _running_route_job(catalog)
    state = _StateView(_route_aggregate(job), failure=state_failure)
    actual_backend = backend or _RuntimeBackend(
        canonical_json_bytes(_route_agent_outcome(job))
    )
    actual_records = records or InMemoryExecutionRecordStore()
    actual_resource_store = (
        _UnusedResourceStore() if resource_store is None else resource_store
    )
    runtime = DiagnosisRuntime(
        state_repository=state,
        resource_store=actual_resource_store,
        asset_catalog=catalog,
        logparse_broker_factory=_BrokerFactory(),
        execution_records=actual_records,
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "data"),
        backend=actual_backend,  # type: ignore[arg-type]
        context_builder=context_builder,
    )
    return runtime, job, state, actual_backend, actual_records


def _generic_runtime_fixture(
    tmp_path: Path,
    backend: _GenericRuntimeBackend,
) -> tuple[DiagnosisRuntime, Job, _StateView, InMemoryExecutionRecordStore]:
    catalog = _make_route_catalog(tmp_path)
    job = _running_generic_job(catalog)
    state = _StateView(_generic_aggregate(job))
    records = InMemoryExecutionRecordStore()
    runtime = DiagnosisRuntime(
        state_repository=state,
        resource_store=_UnusedResourceStore(),
        asset_catalog=catalog,
        logparse_broker_factory=None,
        execution_records=records,
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "generic-data"),
        backend=backend,  # type: ignore[arg-type]
    )
    return runtime, job, state, records


def test_extra_user_fact_keeps_registered_route_candidate_for_semantic_router(
    tmp_path: Path,
) -> None:
    catalog = _make_route_catalog(tmp_path)
    payload = _route_job().model_dump(mode="json")
    bindings = catalog.route_bindings(["unknown_fact"])
    assert [ref.id for ref in bindings.available_skill_refs] == [
        "diagnosis-skill/manual-triage"
    ]
    payload.update(bindings.model_dump(mode="json"))
    payload["context_snapshot"]["user_facts"] = [
        {
            "item_id": "00000000-0000-4000-8000-000000000498",
            "statement": "opaque fact value",
            "status": "ACTIVE",
            "provenance": {
                "source_type": "USER_INPUT",
                "source_ref": "00000000-0000-4000-8000-000000000497",
                "input_name": "unknown_fact",
            },
            "evidence_refs": [],
            "created_revision": 1,
            "supersedes": [],
        }
    ]
    payload.update(
        {
            "status": "RUNNING",
            "started_at": "2026-07-31T00:00:01.000Z",
            "runtime_epoch": "00000000-0000-4000-8000-000000000499",
        }
    )
    job = Job.model_validate(payload)
    state = _StateView(_route_aggregate(job))
    records = InMemoryExecutionRecordStore()
    backend = _RuntimeBackend(canonical_json_bytes(_route_agent_no_capability(job)))
    runtime = DiagnosisRuntime(
        state_repository=state,
        resource_store=_UnusedResourceStore(),
        asset_catalog=catalog,
        logparse_broker_factory=None,
        execution_records=records,
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "empty-route-data"),
        backend=backend,  # type: ignore[arg-type]
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.result_type is OutcomeResultType.NO_CAPABILITY
    assert receipt.job_outcome.payload is not None
    assert receipt.job_outcome.payload.kind.value == "NO_CAPABILITY"
    assert receipt.job_outcome.payload.skill_ref is None
    assert receipt.job_outcome.payload.reason == (
        "No specialized Skill semantically matches the problem."
    )
    assert len(backend.calls) == 1
    assert state.calls == [job.case_id]
    assert len(records.publish_outcome_calls) == 1


def test_empty_production_catalog_publishes_no_capability_without_router(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "empty-skills"
    skill_dir.mkdir()
    catalog = VersionedAssetCatalog(
        skill_dir=skill_dir,
        generic_skill_name="generic-problem-locator-smoke",
    )
    payload = _route_job().model_dump(mode="json")
    bindings = catalog.route_bindings(["unknown_fact"])
    assert bindings.available_skill_refs == []
    payload.update(bindings.model_dump(mode="json"))
    payload.update(
        {
            "status": "RUNNING",
            "started_at": "2026-07-31T00:00:01.000Z",
            "runtime_epoch": "00000000-0000-4000-8000-000000000499",
        }
    )
    job = Job.model_validate(payload)
    state = _StateView(_route_aggregate(job))
    records = InMemoryExecutionRecordStore()
    runtime = DiagnosisRuntime(
        state_repository=state,
        resource_store=_UnusedResourceStore(),
        asset_catalog=catalog,
        logparse_broker_factory=None,
        execution_records=records,
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "empty-route-data"),
        backend=_NeverBackend(),  # type: ignore[arg-type]
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.result_type is OutcomeResultType.NO_CAPABILITY
    assert receipt.job_outcome.payload is not None
    assert receipt.job_outcome.payload.kind is RouteKind.NO_CAPABILITY
    assert receipt.job_outcome.payload.reason == (
        "No diagnosis skill is available in the production catalog."
    )
    assert state.calls == []
    assert records.log_sinks == {}
    assert len(records.publish_outcome_calls) == 1


def test_router_semantic_no_match_publishes_no_capability_after_backend(
    tmp_path: Path,
) -> None:
    backend = _RuntimeBackend(None)
    runtime, job, state, _, records = _runtime_fixture(tmp_path, backend=backend)
    backend.outcome_bytes = canonical_json_bytes(_route_agent_no_capability(job))

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert len(backend.calls) == 1
    assert state.calls == [job.case_id]
    assert receipt.job_outcome.result_type is OutcomeResultType.NO_CAPABILITY
    assert receipt.job_outcome.payload is not None
    assert receipt.job_outcome.payload.kind is RouteKind.NO_CAPABILITY
    assert receipt.job_outcome.payload.skill_ref is None
    assert len(records.publish_outcome_calls) == 1


def test_generic_runtime_passes_only_exact_multiline_unicode_and_reads_result_file(
    tmp_path: Path,
) -> None:
    backend = _GenericRuntimeBackend(_generic_result_bytes())
    runtime, job, state, records = _generic_runtime_fixture(tmp_path, backend)

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert job.diagnosis_mode is DiagnosisMode.GENERIC
    assert job.context_snapshot is None
    assert job.evidence_refs == job.attachment_refs == job.previous_outcome_refs == []
    assert job.artifact_refs == []
    assert len(backend.calls) == 1
    prompt = backend.calls[0]["prompt"]
    marker = (
        "<<<RAW_PROBLEM_TEXT_UTF8_BYTES:"
        f"{len(_GENERIC_RAW_PROBLEM.encode('utf-8'))}>>>\n"
    )
    payload = prompt.split(marker, 1)[1].split(
        "\n<<<END_RAW_PROBLEM_TEXT>>>", 1
    )[0]
    assert payload == _GENERIC_RAW_PROBLEM
    workspace_root = Path(backend.calls[0]["workspace_root"])
    manifest = json.loads((workspace_root / "inputs" / "manifest.json").read_bytes())
    assert manifest["entries"] == []
    assert manifest["logparse_tool_ref"] is None
    assert manifest["logparse_product"] is None
    assert state.calls == [job.case_id]
    assert receipt.job_outcome.result_type is OutcomeResultType.COMPLETED
    assert isinstance(receipt.job_outcome.payload, GenericDiagnosisOutcome)
    assert receipt.job_outcome.payload.status is GenericResultStatus.RESOLVED
    assert receipt.job_outcome.payload.conclusion == "generic-skill-input-contract-ok"
    assert receipt.job_outcome.consumed_evidence_refs == []
    assert receipt.job_outcome.proposed_evidence == []
    assert receipt.job_outcome.proposed_artifacts == []
    assert receipt.job_outcome.decision_audit is None
    assert len(records.publish_outcome_calls) == 1


@pytest.mark.parametrize("status", ["RESOLVED", "UNRESOLVED"])
def test_generic_runtime_preserves_complete_v2_markdown_bytes_and_digest(
    tmp_path: Path,
    status: str,
) -> None:
    report = (
        "# 定位结论\r\n\r\n"
        "保留 Unicode、表格、CRLF 和代码围栏。\r\n\r\n"
        "| 字段 | 值 |\n| --- | --- |\n| request | 订单-α-42 |\n\n"
        "```json\n{\"ok\": true}\n```\n\n"
        "<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\n"
    )
    report_bytes = report.encode("utf-8")
    backend = _GenericRuntimeBackend(
        None,
        v2_result_bytes=_generic_v2_result_bytes(report, status=status),
    )
    runtime, job, state, records = _generic_runtime_fixture(tmp_path, backend)

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert state.calls == [job.case_id]
    assert receipt.job_outcome.result_type is OutcomeResultType.COMPLETED
    assert isinstance(receipt.job_outcome.payload, GenericDiagnosisOutcomeV2)
    assert receipt.job_outcome.payload.format_version == 2
    assert receipt.job_outcome.payload.status is GenericResultStatus(status)
    assert receipt.job_outcome.payload.report_markdown == report
    assert receipt.job_outcome.payload.report_utf8_size == len(report_bytes)
    assert receipt.job_outcome.payload.report_sha256 == hashlib.sha256(
        report_bytes
    ).hexdigest()
    assert receipt.job_outcome.payload.skill_name == job.generic_skill_name
    assert receipt.job_outcome.consumed_evidence_refs == []
    assert receipt.job_outcome.proposed_evidence == []
    assert receipt.job_outcome.proposed_artifacts == []
    assert receipt.job_outcome.decision_audit is None
    assert len(records.publish_outcome_calls) == 1


def test_generic_runtime_accepts_exact_v2_markdown_body_byte_limit(
    tmp_path: Path,
) -> None:
    report_bytes = b"x" * MAX_GENERIC_REPORT_BYTES
    backend = _GenericRuntimeBackend(
        None,
        v2_result_bytes=_generic_v2_result_bytes(report_bytes),
    )
    runtime, job, _, _ = _generic_runtime_fixture(tmp_path, backend)

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert isinstance(receipt.job_outcome.payload, GenericDiagnosisOutcomeV2)
    assert receipt.job_outcome.payload.report_utf8_size == MAX_GENERIC_REPORT_BYTES
    assert (
        receipt.job_outcome.payload.report_markdown
        == "x" * MAX_GENERIC_REPORT_BYTES
    )


@pytest.mark.parametrize(
    "v2_result_bytes",
    [
        b"",
        b"<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\r\n# report\n",
        b"<<<GENERIC_DIAGNOSIS_RESULT_V2:UNKNOWN>>>\n# report\n",
        b"<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>",
        b"<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\n",
        b"<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\n \t\n",
        b"<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\n\xff",
        b"\xef\xbb\xbf<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\n# report\n",
        b"<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\n\xef\xbb\xbf# report\n",
        b"<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\n"
        + b"x" * (MAX_GENERIC_REPORT_BYTES + 1),
    ],
    ids=[
        "empty-file",
        "crlf-status-line",
        "unknown-status",
        "missing-status-lf",
        "empty-body",
        "whitespace-body",
        "invalid-body-utf8",
        "file-utf8-bom",
        "body-utf8-bom",
        "oversize-body",
    ],
)
def test_generic_runtime_rejects_invalid_v2_result(
    tmp_path: Path,
    v2_result_bytes: bytes,
) -> None:
    backend = _GenericRuntimeBackend(
        None,
        v2_result_bytes=v2_result_bytes,
    )
    runtime, job, _, _ = _generic_runtime_fixture(tmp_path, backend)

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.OUTCOME_INVALID
    assert receipt.job_outcome.error.retryable is False


def test_generic_runtime_never_falls_back_from_invalid_v2_to_valid_v1(
    tmp_path: Path,
) -> None:
    backend = _GenericRuntimeBackend(
        _generic_result_bytes(),
        v2_result_bytes=b"<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\n",
    )
    runtime, job, _, _ = _generic_runtime_fixture(tmp_path, backend)

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.OUTCOME_INVALID
    assert receipt.job_outcome.error.retryable is False


def test_generic_runtime_rejects_valid_v1_and_v2_files_as_ambiguous(
    tmp_path: Path,
) -> None:
    backend = _GenericRuntimeBackend(
        _generic_result_bytes(),
        v2_result_bytes=_generic_v2_result_bytes("# complete report\n"),
    )
    runtime, job, _, _ = _generic_runtime_fixture(tmp_path, backend)

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.OUTCOME_INVALID


@pytest.mark.parametrize("unsafe_kind", ["directory", "hardlink"])
def test_generic_runtime_rejects_unsafe_v2_result_nodes(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    def write_unsafe(output: Path) -> None:
        result = output / GENERIC_RESULT_V2_FILENAME
        if unsafe_kind == "directory":
            result.mkdir()
            return
        source = output / "untrusted-source.md"
        source.write_bytes(_generic_v2_result_bytes("# report\n"))
        os.link(source, result)

    backend = _GenericRuntimeBackend(None, result_writer=write_unsafe)
    runtime, job, _, _ = _generic_runtime_fixture(tmp_path, backend)

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.OUTCOME_INVALID


@pytest.mark.parametrize(
    "result_bytes",
    [
        None,
        b"\xff",
        b"prefix\n" + _generic_result_bytes(),
        _generic_result_bytes(conclusion="```not-allowed```"),
        b"x" * (MAX_GENERIC_RESULT_BYTES + 1),
    ],
    ids=["missing", "invalid-utf8", "extra-text", "code-fence", "oversize"],
)
def test_generic_runtime_maps_invalid_result_file_to_nonretryable_failure(
    tmp_path: Path,
    result_bytes: bytes | None,
) -> None:
    backend = _GenericRuntimeBackend(result_bytes)
    runtime, job, _, _ = _generic_runtime_fixture(tmp_path, backend)

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert len(backend.calls) == 1
    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.OUTCOME_INVALID
    assert receipt.job_outcome.error.retryable is False


def test_generic_runtime_never_retries_a_retryable_backend_failure(
    tmp_path: Path,
) -> None:
    backend = _GenericRuntimeBackend(
        None,
        failure=runtime_failure(
            stage=ExecutionStage.BACKEND_EXECUTE,
            code=ErrorCode.BACKEND_EXIT_FAILED,
            message="Injected backend failure.",
            retryable=True,
        ),
    )
    runtime, job, _, _ = _generic_runtime_fixture(tmp_path, backend)

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert len(backend.calls) == 1
    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.BACKEND_EXIT_FAILED
    assert receipt.job_outcome.error.retryable is False


def test_public_asset_fake_typed_resolve_failure_preserves_details_as_outcome(
    tmp_path: Path,
) -> None:
    source_catalog = _make_route_catalog(tmp_path)
    job = _running_route_job(source_catalog)
    detail = ApplicationErrorDetail(
        field="asset_ref",
        resource_type="ASSET",
        resource_id=None,
        resource_ref=job.agent_profile_ref,
        expected="available",
        actual="unavailable",
        limit=None,
        observed=None,
    )
    typed_failure = ApplicationPortError(
        ApplicationError(
            code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
            message="Injected public AssetCatalog resolve failure.",
            details=[detail],
            retryable=ERROR_SPECS[
                ErrorCode.ASSET_VERSION_UNAVAILABLE
            ].application_retryable,
        )
    )
    catalog = FakeAssetCatalog(
        assets=[
            ResolvedAsset(
                ref=job.agent_profile_ref,
                asset_kind=AssetKind.AGENT_PROFILE,
                root_path=str(tmp_path),
            )
        ]
    )
    catalog.inject_failure("resolve", typed_failure)
    state = _StateView(_route_aggregate(job))
    records = InMemoryExecutionRecordStore()
    runtime = DiagnosisRuntime(
        state_repository=state,
        resource_store=_UnusedResourceStore(),
        asset_catalog=catalog,
        logparse_broker_factory=None,
        execution_records=records,
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "asset-failure-data"),
        backend=_NeverBackend(),  # type: ignore[arg-type]
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.stage is ExecutionStage.ASSET_RESOLUTION
    assert receipt.job_outcome.error.code is ErrorCode.ASSET_VERSION_UNAVAILABLE
    assert receipt.job_outcome.error.details == [detail]
    assert state.calls == []
    assert records.log_sinks == {}
    assert len(records.publish_outcome_calls) == 1
    assert catalog.check_calls == [(job.agent_profile_ref,)]


def test_runtime_executes_one_frozen_route_and_publishes_canonical_receipt(
    tmp_path: Path,
    journey_stream: io.StringIO,
) -> None:
    runtime, job, state, backend, records = _runtime_fixture(tmp_path)

    with bind_diagnostics(
        case_id=job.case_id,
        job_id=job.job_id,
        job_type=job.job_type.value,
    ):
        receipt = runtime.execute(job, InMemoryCancellationSignal())

    journey_events = [
        json.loads(line) for line in journey_stream.getvalue().splitlines()
    ]
    stage_events = [
        event for event in journey_events if event["event"].startswith("job.stage.")
    ]
    assert [event["data"]["stage"] for event in stage_events] == [
        "ASSET_RESOLUTION",
        "ASSET_RESOLUTION",
        "WORKSPACE_PREPARE",
        "WORKSPACE_PREPARE",
        "CONTEXT_BUILD",
        "CONTEXT_BUILD",
        "OUTCOME_VALIDATE",
        "OUTCOME_VALIDATE",
        "RESOURCE_STAGE",
        "RESOURCE_STAGE",
        "EXECUTION_RECORD",
        "EXECUTION_RECORD",
    ]
    assert all(event["case_id"] == job.case_id for event in journey_events)
    assert all(event["job_id"] == job.job_id for event in journey_events)
    assert journey_events[-1]["event"] == "job.outcome.produced"
    assert journey_events[-1]["outcome_id"] == receipt.job_outcome.outcome_id

    assert receipt.job_outcome.result_type is OutcomeResultType.COMPLETED
    assert receipt.job_outcome.outcome_id == (
        "00000000-0000-4000-8000-000000000401"
    )
    assert state.calls == [job.case_id]
    assert len(backend.calls) == 1  # type: ignore[union-attr]
    backend_call = backend.calls[0]  # type: ignore[union-attr]
    assert backend_call["prompt"] == (
        Path(backend_call["workspace_root"]) / "runtime" / "context.txt"
    ).read_text(encoding="utf-8")
    assert records.publish_outcome_calls == [
        (job.job_id, canonical_json_bytes(receipt.job_outcome))
    ]
    replay = records.read_published_outcome(job.job_id)
    assert replay == receipt


def test_runtime_context_never_reads_latest_case_diagnosis_state(
    tmp_path: Path,
) -> None:
    runtime, job, state, backend, _ = _runtime_fixture(tmp_path)
    latest = state.aggregate.model_dump(mode="json")
    latest_statement = "A newer mutable Case diagnosis must stay outside this Job."
    latest["case"]["diagnosis_state"]["problem_spec"]["statement"] = latest_statement
    state.aggregate = CaseAggregate.model_validate(latest)

    runtime.execute(job, InMemoryCancellationSignal())

    prompt = backend.calls[0]["prompt"]  # type: ignore[union-attr]
    assert job.context_snapshot.problem_spec.statement in prompt
    assert latest_statement not in prompt
    assert state.calls == [job.case_id]


def test_stored_job_immutable_drift_is_rejected_before_backend(
    tmp_path: Path,
) -> None:
    runtime, job, state, _, _ = _runtime_fixture(
        tmp_path,
        backend=_NeverBackend(),
    )
    drifted_job = job.model_copy(update={"goal": "A different immutable goal."})
    state.aggregate = state.aggregate.model_copy(
        update={"jobs": {job.job_id: drifted_job}},
        deep=True,
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.stage is ExecutionStage.OUTCOME_VALIDATE
    assert receipt.job_outcome.error.code is ErrorCode.OUTCOME_INVALID
    assert state.calls == [job.case_id]


def test_missing_job_fixed_resource_id_is_not_replaced_from_latest_state(
    tmp_path: Path,
) -> None:
    catalog = _make_route_catalog(tmp_path)
    base = _running_route_job(catalog)
    missing_id = "00000000-0000-4000-8000-000000000488"
    snapshot = base.context_snapshot.model_copy(
        update={"evidence_refs": [missing_id]},
        deep=True,
    )
    job = base.model_copy(
        update={"context_snapshot": snapshot, "evidence_refs": [missing_id]},
        deep=True,
    )
    aggregate = _route_aggregate(base).model_copy(
        update={"jobs": {job.job_id: job}},
        deep=True,
    )
    state = _StateView(aggregate)
    records = InMemoryExecutionRecordStore()
    runtime = DiagnosisRuntime(
        state_repository=state,
        resource_store=_UnusedResourceStore(),
        asset_catalog=catalog,
        logparse_broker_factory=_BrokerFactory(),
        execution_records=records,
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "missing-fixed-resource-data"),
        backend=_NeverBackend(),  # type: ignore[arg-type]
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.stage is ExecutionStage.WORKSPACE_PREPARE
    assert receipt.job_outcome.error.code is ErrorCode.RESOURCE_NOT_FOUND
    assert state.calls == [job.case_id]


def test_context_limit_publishes_failure_without_calling_backend(
    tmp_path: Path,
) -> None:
    runtime, job, state, _, _ = _runtime_fixture(
        tmp_path,
        backend=_NeverBackend(),
        context_builder=_TooLargeContext(),
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.stage is ExecutionStage.CONTEXT_BUILD
    assert receipt.job_outcome.error.code is ErrorCode.CONTEXT_LIMIT
    assert receipt.job_outcome.error.details[0].limit == 131072
    assert receipt.job_outcome.error.details[0].observed == 131073
    assert state.calls == [job.case_id]


def test_infrastructure_error_never_retains_unsafe_runtime_exception_context(
    tmp_path: Path,
) -> None:
    records = _Records(failures=1)
    runtime, job, _, _, _ = _runtime_fixture(
        tmp_path,
        backend=_NeverBackend(),
        records=records,  # type: ignore[arg-type]
        context_builder=_UnsafeExplodingContext(),
    )

    with pytest.raises(RuntimeInfrastructureError) as captured:
        runtime.execute(job, InMemoryCancellationSignal())

    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None
    assert records.read_calls == [job.job_id]


def test_ambiguous_success_keeps_staged_refs_for_durable_outbox_replay(
    tmp_path: Path,
) -> None:
    catalog = _make_route_catalog(tmp_path)
    job = _running_route_job(catalog)
    payload = b"durable diagnostic proposal\n"
    relative_path = "output/proposals/diagnostic_export/payload.bin"
    draft = AgentArtifactProposalDraft(
        proposal_key="diagnostic_export",
        artifact_kind=ArtifactKind.DIAGNOSTIC_EXPORT,
        name="durable-diagnostic",
        content_type="application/octet-stream",
        resource_kind=ResourceKind.FILE,
        workspace_relative_path=relative_path,
        declared_size=len(payload),
        declared_sha256=hashlib.sha256(payload).hexdigest(),
        metadata={
            "schema_version": 1,
            "format_id": "runtime-ambiguous-publish-test",
            "description": "A staged resource protected by the durable outbox.",
        },
    )
    outcome_payload = _route_agent_outcome(job).model_dump(mode="json")
    outcome_payload["proposed_artifact_drafts"] = [draft.model_dump(mode="json")]
    agent_outcome = AgentJobOutcomeDraftV2.model_validate(outcome_payload)
    backend = _RuntimeBackend(
        canonical_json_bytes(agent_outcome),
        proposal_files={relative_path: payload},
    )
    records = _AfterWriteExecutionRecordStore(after_write_failures=2)
    resources = InMemoryResourceStore()
    runtime = DiagnosisRuntime(
        state_repository=_StateView(_route_aggregate(job)),
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=_BrokerFactory(),
        execution_records=records,
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "ambiguous-data"),
        backend=backend,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeInfrastructureError):
        runtime.execute(job, InMemoryCancellationSignal())

    assert resources.staged_resource_count == 1
    assert resources.discard_calls == []
    replay = records.read_published_outcome(job.job_id)
    assert replay is not None
    assert replay.job_outcome.proposed_artifacts[0].staged_resource_ref is not None
    assert replay.job_outcome.proposed_artifacts[0].staged_resource_ref.proposal_key == (
        "diagnostic_export"
    )


def test_explicit_prepublish_validation_failure_discards_staged_resource(
    tmp_path: Path,
) -> None:
    catalog = _make_route_catalog(tmp_path)
    job = _running_route_job(catalog)
    payload = b"proposal rejected before execution-record I/O\n"
    relative_path = "output/proposals/prepublish_rejected/payload.bin"
    draft = AgentArtifactProposalDraft(
        proposal_key="prepublish_rejected",
        artifact_kind=ArtifactKind.DIAGNOSTIC_EXPORT,
        name="prepublish-rejected",
        content_type="application/octet-stream",
        resource_kind=ResourceKind.FILE,
        workspace_relative_path=relative_path,
        declared_size=len(payload),
        declared_sha256=hashlib.sha256(payload).hexdigest(),
        metadata={
            "schema_version": 1,
            "format_id": "prepublish-validation-test",
            "description": "The manifest changed before publication validation.",
        },
    )
    outcome_payload = _route_agent_outcome(job).model_dump(mode="json")
    outcome_payload["proposed_artifact_drafts"] = [draft.model_dump(mode="json")]
    backend = _RuntimeBackend(
        canonical_json_bytes(AgentJobOutcomeDraftV2.model_validate(outcome_payload)),
        proposal_files={relative_path: payload},
    )
    records = InMemoryExecutionRecordStore()
    resources = InMemoryResourceStore()
    runtime = DiagnosisRuntime(
        state_repository=_StateView(_route_aggregate(job)),
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=_BrokerFactory(),
        execution_records=records,
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "prepublish-data"),
        backend=backend,  # type: ignore[arg-type]
    )
    delegate = OutcomePublisher(records, _Clock(), _Ids())

    class RejectingPublisher:
        def publish_success(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise ValueError("injected explicit prepublish validation failure")

        def publish_failure(
            self,
            fixed_job: Job,
            failure: ExecutionFailure,
        ) -> RuntimeExecutionReceipt:
            return delegate.publish_failure(fixed_job, failure)

    runtime._publisher = RejectingPublisher()  # type: ignore[assignment]

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.OUTCOME_INVALID
    assert resources.staged_resource_count == 0
    assert [ref.proposal_key for ref in resources.discard_calls] == [
        "prepublish_rejected"
    ]


def test_different_authoritative_outcome_discards_only_unreferenced_stage() -> None:
    job = _route_job()
    store = InMemoryResourceStore()
    kept = store.stage_file(
        job.job_id,
        "kept",
        io.BytesIO(b"kept"),
        expected_size=4,
        expected_sha256=hashlib.sha256(b"kept").hexdigest(),
    )
    discarded = store.stage_file(
        job.job_id,
        "discarded",
        io.BytesIO(b"discarded"),
        expected_size=9,
        expected_sha256=hashlib.sha256(b"discarded").hexdigest(),
    )
    artifact = {
        "proposal_key": "kept",
        "artifact_kind": "DIAGNOSTIC_EXPORT",
        "name": "kept-resource",
        "content_type": "application/octet-stream",
        "resource_kind": "FILE",
        "size": kept.size,
        "sha256": kept.sha256,
        "staged_resource_ref": kept,
        "metadata": {
            "schema_version": 1,
            "format_id": "authoritative-outcome-test",
            "description": "The authoritative Outcome still references this stage.",
        },
    }
    outcome_payload = _json("job-outcome-route.json")
    outcome_payload["proposed_artifacts"] = [artifact]
    authoritative = JobOutcome.model_validate(outcome_payload)
    receipt = RuntimeExecutionReceipt(
        job_outcome=authoritative,
        outcome_file_ref=ExecutionFileRef(
            relative_key=f"jobs/{job.job_id}/job_outcome.json",
            size=len(canonical_json_bytes(authoritative)),
            sha256=hashlib.sha256(canonical_json_bytes(authoritative)).hexdigest(),
        ),
    )

    _discard_unreferenced_staged(store, (kept, discarded), receipt)

    assert store.discard_calls == [discarded]
    assert store.staged_resource_count == 1


def test_state_read_typed_missing_maps_without_backend_or_state_fallback(
    tmp_path: Path,
) -> None:
    runtime, job, state, _, _ = _runtime_fixture(
        tmp_path,
        backend=_NeverBackend(),
        state_failure=_application_port_error(ErrorCode.CASE_NOT_FOUND),
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.stage is ExecutionStage.WORKSPACE_PREPARE
    assert receipt.job_outcome.error.code is ErrorCode.RESOURCE_NOT_FOUND
    assert state.calls == [job.case_id]


@pytest.mark.parametrize(
    "code",
    [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED],
)
def test_state_read_fault_propagates_exact_public_fake_error_without_records(
    code: ErrorCode,
    tmp_path: Path,
) -> None:
    runtime, job, _, _, records = _runtime_fixture(
        tmp_path,
        backend=_NeverBackend(),
    )
    state_payload = _json("state.json")
    state_payload["cases"] = {
        job.case_id: _route_aggregate(job).model_dump(mode="json")
    }
    state = InMemoryStateRepository(StateFile.model_validate(state_payload))
    expected = _application_port_error(code)
    state.inject_read_failure("read_case", expected)
    runtime._state_repository = state

    with pytest.raises(ApplicationPortError) as caught:
        runtime.execute(job, InMemoryCancellationSignal())

    assert caught.value is expected
    assert records.publish_outcome_calls == []
    assert records.log_sinks == {}


@pytest.mark.parametrize(
    "state_failure",
    [
        _application_port_error(ErrorCode.EXECUTION_RECORD_FAILED),
        OSError("unsafe adapter detail must not escape"),
    ],
    ids=["disallowed-typed-code", "ordinary-exception"],
)
def test_disallowed_state_read_failures_remain_safe_outcomes(
    state_failure: BaseException,
    tmp_path: Path,
) -> None:
    records = InMemoryExecutionRecordStore()
    runtime, job, _, _, _ = _runtime_fixture(
        tmp_path,
        backend=_NeverBackend(),
        state_failure=state_failure,
        records=records,
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.stage is ExecutionStage.WORKSPACE_PREPARE
    assert receipt.job_outcome.error.code is ErrorCode.WORKSPACE_PREPARE_FAILED
    assert len(records.publish_outcome_calls) == 1
    assert b"unsafe adapter detail" not in records.publish_outcome_calls[0][1]


@pytest.mark.parametrize(
    "port_code",
    [
        ErrorCode.RESOURCE_NOT_FOUND,
        ErrorCode.RESOURCE_SIZE_MISMATCH,
        ErrorCode.RESOURCE_HASH_MISMATCH,
        ErrorCode.PATH_VIOLATION,
    ],
)
def test_materialize_port_errors_use_the_r2_typed_code_without_text_matching(
    port_code: ErrorCode,
    tmp_path: Path,
) -> None:
    class Store:
        def materialize_read_only(self, resource_ref: object, destination: object) -> None:
            del resource_ref, destination
            raise _application_port_error(port_code)

    reference = ResourceRef(
        resource_kind=ResourceKind.FILE,
        storage_key="cases/test/resources/payload",
        size=0,
        sha256=hashlib.sha256(b"").hexdigest(),
    )

    with pytest.raises(RuntimeExecutionError) as captured:
        _verify_materialized(Store(), reference, tmp_path / "payload")  # type: ignore[arg-type]

    assert captured.value.failure.stage is ExecutionStage.WORKSPACE_PREPARE
    assert captured.value.failure.code is port_code


def test_materialized_file_may_use_s02_read_only_hard_link(
    tmp_path: Path,
) -> None:
    payload = b"immutable attachment bytes\n"
    source = tmp_path / "resource-store-source"
    source.write_bytes(payload)
    source.chmod(0o444)
    reference = ResourceRef(
        resource_kind=ResourceKind.FILE,
        storage_key="cases/test/attachments/payload",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )

    class Store:
        def materialize_read_only(
            self,
            resource_ref: ResourceRef,
            destination: Path,
        ) -> MaterializedPath:
            assert resource_ref == reference
            destination.parent.mkdir(parents=True)
            os.link(source, destination)
            return MaterializedPath(path=str(destination), read_only=True)

    destination = tmp_path / "workspace/inputs/attachment/payload"

    _verify_materialized(Store(), reference, destination)  # type: ignore[arg-type]

    assert destination.read_bytes() == payload
    assert destination.stat().st_ino == source.stat().st_ino
    assert destination.stat().st_nlink == 2


def test_nonconforming_materialize_exception_cannot_forge_runtime_failure(
    tmp_path: Path,
) -> None:
    class Store:
        def materialize_read_only(self, resource_ref: object, destination: object) -> None:
            del resource_ref, destination
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.BACKEND_TIMEOUT,
                message="A private adapter exception must not cross the Port.",
                retryable=True,
            )

    reference = ResourceRef(
        resource_kind=ResourceKind.FILE,
        storage_key="cases/test/resources/payload",
        size=0,
        sha256=hashlib.sha256(b"").hexdigest(),
    )

    with pytest.raises(RuntimeExecutionError) as captured:
        _verify_materialized(Store(), reference, tmp_path / "payload")  # type: ignore[arg-type]

    assert captured.value.failure.stage is ExecutionStage.WORKSPACE_PREPARE
    assert captured.value.failure.code is ErrorCode.WORKSPACE_PREPARE_FAILED
    assert captured.value.failure.retryable is True


def test_saved_logparse_run_fixture_materializes_as_one_fixed_read_only_tree(
    tmp_path: Path,
) -> None:
    source = LOGPARSE_FIXTURES / "saved-run"
    size, sha256, expected_manifest = inspect_tree(source)
    reference = ResourceRef(
        resource_kind=ResourceKind.DIRECTORY,
        storage_key="cases/test/artifacts/logparse-run/tree",
        size=size,
        sha256=sha256,
    )

    class Store:
        def __init__(self) -> None:
            self.calls: list[tuple[ResourceRef, Path]] = []

        def materialize_read_only(
            self,
            resource_ref: ResourceRef,
            destination: Path,
        ) -> MaterializedPath:
            self.calls.append((resource_ref, destination))
            shutil.copytree(source, destination)
            for path in sorted(destination.rglob("*"), reverse=True):
                path.chmod(0o555 if path.is_dir() else 0o444)
            destination.chmod(0o555)
            return MaterializedPath(path=str(destination), read_only=True)

    store = Store()
    destination = tmp_path / "materialized-logparse-run"

    _verify_materialized(store, reference, destination)  # type: ignore[arg-type]

    actual_size, actual_sha256, actual_manifest = inspect_tree(destination)
    assert (actual_size, actual_sha256, actual_manifest) == (
        size,
        sha256,
        expected_manifest,
    )
    assert store.calls == [(reference, destination)]
    assert (destination / "task-0001/parse_manifest.json").is_file()
    for path in sorted(destination.rglob("*"), reverse=True):
        path.chmod(0o755 if path.is_dir() else 0o644)
    destination.chmod(0o755)


def test_open_log_sinks_typed_failure_is_replayable_execution_failure(
    tmp_path: Path,
) -> None:
    records = InMemoryExecutionRecordStore()
    records.inject_failure(
        "open_log_sinks",
        _application_port_error(ErrorCode.EXECUTION_RECORD_FAILED),
    )
    runtime, job, _, _, _ = _runtime_fixture(
        tmp_path,
        backend=_NeverBackend(),
        records=records,
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.stage is ExecutionStage.EXECUTION_RECORD
    assert receipt.job_outcome.error.code is ErrorCode.EXECUTION_RECORD_FAILED
    assert len(records.publish_outcome_calls) == 1


def test_successful_backend_with_missing_final_file_publishes_outcome_missing(
    tmp_path: Path,
) -> None:
    records = InMemoryExecutionRecordStore()
    runtime, job, _, _, _ = _runtime_fixture(
        tmp_path,
        backend=_RuntimeBackend(None),
        records=records,
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.stage is ExecutionStage.OUTCOME_VALIDATE
    assert receipt.job_outcome.error.code is ErrorCode.OUTCOME_MISSING
    assert records.publish_rejected_agent_output_calls == []


class _RuntimeBrokerSession:
    def __init__(self, *, close_fails: bool = False) -> None:
        self.closed = False
        self.close_fails = close_fails
        self.events: list[str] = []

    def agent_environment(self) -> dict[str, str]:
        assert not self.closed
        return {
            "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT": "inmemory://runtime-test",
            "PROBLEM_LOCATOR_LOGPARSE_TOKEN": "runtime-test-token",
        }

    def close(self) -> None:
        self.events.append("close")
        self.closed = True
        if self.close_fails:
            raise OSError("injected broker close failure")

    def parse_request_bytes(self) -> None:
        assert self.closed
        self.events.append("parse_request_bytes")
        return None

    def audit_bytes(self) -> bytes:
        assert self.closed
        self.events.append("audit_bytes")
        return canonical_json_bytes(
            {
                "schema_version": 1,
                "job_id": "00000000-0000-0000-0000-000000000011",
                "operations": [],
            }
        )


class _RuntimeBrokerFactory:
    def __init__(
        self,
        *,
        failure: LogparseBrokerError | None = None,
        close_fails: bool = False,
    ) -> None:
        self.failure = failure
        self.session = _RuntimeBrokerSession(close_fails=close_fails)
        self.calls: list[tuple[Job, Path, Any, Any]] = []

    def open(
        self,
        job: Job,
        workspace_root: Path,
        workspace_manifest: Any,
        cancellation: Any,
    ) -> _RuntimeBrokerSession:
        self.calls.append((job, workspace_root, workspace_manifest, cancellation))
        if self.failure is not None:
            raise self.failure
        return self.session


def _logparse_catalog(
    tmp_path: Path,
    factory: _RuntimeBrokerFactory | FakeLogparseBrokerFactory,
    *,
    logparse_product: str | None = None,
) -> VersionedAssetCatalog:
    skill_dir = tmp_path / "logparse-skills"
    shutil.copytree(CATALOG_FIXTURES / "skill-dir", skill_dir)
    if logparse_product is not None:
        template = skill_dir / "rpc-log-analysis/registration-template.json"
        value = json.loads(template.read_bytes())
        value["runtime"]["preprocessing"]["logparse_product"] = logparse_product
        template.write_bytes(canonical_json_bytes(value))
    asset = ResolvedAsset(
        ref=VersionedRef(
            id="logparse-tool/fake",
            version="3.4.5",
            # S07 fingerprints repository/config/interpreter facts, not the
            # S04 product-directory preimage used by built-in assets.
            content_hash="f" * 64,
        ),
        asset_kind=AssetKind.LOGPARSE_TOOL,
        root_path=str(LOGPARSE_ROOT),
    )
    return VersionedAssetCatalog(
        skill_dir=skill_dir,
        generic_skill_name="generic-problem-locator-smoke",
        logparse_tool=asset,
        logparse_broker_factory=factory,
    )


def _running_logparse_job(catalog: VersionedAssetCatalog) -> Job:
    skill_ref = next(
        ref
        for ref in catalog.route_bindings().available_skill_refs
        if ref.id == "diagnosis-skill/rpc-log-analysis"
    )
    payload = _route_job().model_dump(mode="json")
    payload.update(catalog.diagnose_bindings(skill_ref).model_dump(mode="json"))
    payload.update(
        {
            "job_type": "DIAGNOSE",
            "goal": "Run the fixed logparse diagnosis skill.",
            "status": "RUNNING",
            "started_at": "2026-07-31T00:00:01.000Z",
            "runtime_epoch": "00000000-0000-4000-8000-000000000498",
        }
    )
    return Job.model_validate(payload)


_LOGPARSE_ATTACHMENT_ID = "00000000-0000-4000-8000-000000000450"
_LOGPARSE_USER_FACTS = [
    {
        "item_id": "00000000-0000-4000-8000-000000000451",
        "statement": "checkout-service",
        "status": "ACTIVE",
        "provenance": {
            "source_type": "USER_INPUT",
            "source_ref": "00000000-0000-4000-8000-000000000001",
            "input_name": "caller_service",
        },
        "evidence_refs": [],
        "created_revision": 1,
        "supersedes": [],
    },
    {
        "item_id": "00000000-0000-4000-8000-000000000452",
        "statement": "2026-07-31T00:00:00.000Z",
        "status": "ACTIVE",
        "provenance": {
            "source_type": "USER_INPUT",
            "source_ref": "00000000-0000-4000-8000-000000000001",
            "input_name": "problem_time",
        },
        "evidence_refs": [],
        "created_revision": 1,
        "supersedes": [],
    },
    {
        "item_id": "00000000-0000-4000-8000-000000000453",
        "statement": "client",
        "status": "ACTIVE",
        "provenance": {
            "source_type": "USER_INPUT",
            "source_ref": "00000000-0000-4000-8000-000000000001",
            "input_name": "client_slot",
        },
        "evidence_refs": [],
        "created_revision": 1,
        "supersedes": [],
    },
    {
        "item_id": "00000000-0000-4000-8000-000000000454",
        "statement": "checkout-service",
        "status": "ACTIVE",
        "provenance": {
            "source_type": "USER_INPUT",
            "source_ref": "00000000-0000-4000-8000-000000000001",
            "input_name": "client_process_name",
        },
        "evidence_refs": [],
        "created_revision": 1,
        "supersedes": [],
    },
    {
        "item_id": "00000000-0000-4000-8000-000000000455",
        "statement": "server",
        "status": "ACTIVE",
        "provenance": {
            "source_type": "USER_INPUT",
            "source_ref": "00000000-0000-4000-8000-000000000001",
            "input_name": "server_slot",
        },
        "evidence_refs": [],
        "created_revision": 1,
        "supersedes": [],
    },
    {
        "item_id": "00000000-0000-4000-8000-000000000456",
        "statement": "inventory-service",
        "status": "ACTIVE",
        "provenance": {
            "source_type": "USER_INPUT",
            "source_ref": "00000000-0000-4000-8000-000000000001",
            "input_name": "server_process_name",
        },
        "evidence_refs": [],
        "created_revision": 1,
        "supersedes": [],
    },
    {
        "item_id": "00000000-0000-4000-8000-000000000457",
        "statement": "inventory-service",
        "status": "ACTIVE",
        "provenance": {
            "source_type": "USER_INPUT",
            "source_ref": "00000000-0000-4000-8000-000000000001",
            "input_name": "server_service",
        },
        "evidence_refs": [],
        "created_revision": 1,
        "supersedes": [],
    },
    {
        "item_id": "00000000-0000-4000-8000-000000000458",
        "statement": "ReserveStock",
        "status": "ACTIVE",
        "provenance": {
            "source_type": "USER_INPUT",
            "source_ref": "00000000-0000-4000-8000-000000000001",
            "input_name": "rpc_method",
        },
        "evidence_refs": [],
        "created_revision": 1,
        "supersedes": [],
    },
]


def _claimed_logparse_job_state_and_resources(
    catalog: VersionedAssetCatalog,
) -> tuple[Job, CaseAggregate, InMemoryResourceStore]:
    attachment_bytes = b"request timed out while calling inventory\n"
    attachment_sha256 = hashlib.sha256(attachment_bytes).hexdigest()
    job_payload = _running_logparse_job(catalog).model_dump(mode="json")
    job_payload["attachment_refs"] = [_LOGPARSE_ATTACHMENT_ID]
    job_payload["context_snapshot"]["user_facts"] = _LOGPARSE_USER_FACTS
    job = Job.model_validate(job_payload)
    storage_key = (
        f"resources/cases/{job.case_id}/attachments/"
        f"{_LOGPARSE_ATTACHMENT_ID}/request.log"
    )
    attachment = Attachment(
        attachment_id=_LOGPARSE_ATTACHMENT_ID,
        case_id=job.case_id,
        status=AttachmentStatus.READY,
        name="request.log",
        content_type="text/plain",
        declared_size=len(attachment_bytes),
        declared_sha256=attachment_sha256,
        size=len(attachment_bytes),
        sha256=attachment_sha256,
        storage_key=storage_key,
        created_at="2026-07-31T00:00:00.000Z",
        updated_at="2026-07-31T00:00:00.000Z",
    )
    state = StateFile.model_validate(_json("state.json"))
    aggregate_payload = next(iter(state.cases.values())).model_dump(mode="json")
    aggregate_payload["jobs"] = {job.job_id: job.model_dump(mode="json")}
    aggregate_payload["case"]["diagnosis_state"]["user_facts"] = (
        _LOGPARSE_USER_FACTS
    )
    aggregate_payload["attachments"] = {
        attachment.attachment_id: attachment.model_dump(mode="json")
    }
    aggregate = CaseAggregate.model_validate(aggregate_payload)
    resources = InMemoryResourceStore()
    resources.seed_formal_resource(
        ResourceRef(
            resource_kind=ResourceKind.FILE,
            storage_key=storage_key,
            size=len(attachment_bytes),
            sha256=attachment_sha256,
        ),
        state_reference_count=1,
        payload=attachment_bytes,
    )
    return job, aggregate, resources


class _PublicFakeClaimingBackend:
    def __init__(
        self,
        factory: FakeLogparseBrokerFactory,
        job: Job,
        result: str,
        *,
        accept_request: bool = True,
        emit_claim: bool = True,
    ) -> None:
        self.factory = factory
        self.job = job
        self.result = result
        self.accept_request = accept_request
        self.emit_claim = emit_claim
        self.claim: LogparseParseClaim | None = None
        self.request_bytes: bytes | None = None
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _close_sinks(kwargs: dict[str, Any]) -> None:
        sinks: ExecutionLogSinks = kwargs["log_sinks"]
        unique = {id(sinks.stdout): sinks.stdout, id(sinks.stderr): sinks.stderr}
        for sink in unique.values():
            sink.flush()
            sink.close()

    @staticmethod
    def _completed_outcome(
        job: Job,
        workspace_root: Path,
        claim: LogparseParseClaim,
    ) -> AgentJobOutcomeDraftV2:
        proposal_root = workspace_root / "output/proposals/logparse_run/tree"
        task_root = proposal_root / "task-0001"
        (task_root / "targets").mkdir(parents=True)
        (task_root / "parse_manifest.json").write_bytes(
            canonical_json_bytes(
                {
                    "product": job.logparse_product,
                    "target_logs": ["targets/request.log"],
                    "version": 1,
                }
            )
        )
        (task_root / "targets/request.log").write_bytes(b"request timed out\n")
        size, tree_sha256, _ = inspect_tree(proposal_root)
        draft = AgentArtifactProposalDraft(
            proposal_key=claim.artifact_proposal_key,
            artifact_kind=ArtifactKind.LOGPARSE_RUN,
            name="new-logparse-run",
            content_type="application/vnd.problem-locator.logparse-run+directory",
            resource_kind=ResourceKind.DIRECTORY,
            workspace_relative_path="output/proposals/logparse_run/tree",
            declared_size=None,
            declared_sha256=None,
            metadata={
                "tree_manifest_sha256": tree_sha256,
                "logparse_version_ref": job.logparse_tool_ref,
                "parse_manifest_relative_path": "task-0001/parse_manifest.json",
                "source_attachment_id": claim.attachment_id,
                "source_attachment_sha256": claim.attachment_sha256,
                "parse_parameters": {"product": job.logparse_product},
            },
        )
        payload = _json("agent-job-outcome-draft-diagnosis.json")
        payload.update(
            {
                "job_id": job.job_id,
                "case_id": job.case_id,
                "job_type": job.job_type.value,
                "base_state_revision": job.base_state_revision,
            }
        )
        payload["payload"]["candidate_conclusion_draft"] = None
        payload["consumed_evidence_refs"] = []
        payload["proposed_evidence_drafts"] = []
        payload["proposed_artifact_drafts"] = [draft.model_dump(mode="json")]
        # This seam proves the Logparse run publication, not a positive diagnosis.
        # An explicit UNKNOWN claim keeps the draft structurally complete while
        # the server deterministically seals it as INCONCLUSIVE and retains the run.
        payload["rule_claims"] = [
            {
                "rule_id": rule_id,
                "claimed_result": "UNKNOWN",
                "fact_refs": [],
                "citations": [],
                "explanation": "This test does not claim a positive diagnosis.",
            }
            for rule_id in (
                "client_timeout_present",
                "client_timeout_in_window",
                "caller_matches",
                "timeout_causality",
            )
        ]
        return AgentJobOutcomeDraftV2.model_validate(payload)

    @staticmethod
    def _successful_audit_bytes(
        job_id: str,
        request_bytes: bytes,
        outcome: AgentJobOutcomeDraftV2,
        *,
        match_status: str = "exact",
    ) -> bytes:
        request = parse_canonical_json_bytes(request_bytes)
        proposal = outcome.proposed_artifact_drafts[0]
        target_log = {
            "label": "client",
            "module": "compact",
            "module_key": "compact",
            "module_name": "compact",
            "slot": "client",
            "process_name": "checkout-service",
            "match_status": match_status,
            "caveats": [],
        }
        if match_status in {"exact", "nearest"}:
            target_log["log_path"] = "task-0001/targets/request.log"
        else:
            target_log["caveats"] = [f"Synthetic {match_status} target."]
        result = {
            "schema_version": 1,
            "api_version": 1,
            "target_logs": [target_log],
            "logparse_run_artifact_draft": proposal.model_dump(mode="json"),
        }
        return canonical_json_bytes(
            {
                "schema_version": 1,
                "job_id": job_id,
                "operations": [
                    {
                        "operation": "parse-targets",
                        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
                        "request": request,
                        "http_status": 200,
                        "result_sha256": hashlib.sha256(
                            canonical_json_bytes(result)
                        ).hexdigest(),
                        "result": result,
                    }
                ],
            }
        )

    def execute(self, **kwargs: Any) -> BackendExecution:
        self.calls.append(kwargs)
        workspace_root = Path(kwargs["workspace_root"])
        manifest = parse_canonical_json_bytes(
            (workspace_root / "inputs/manifest.json").read_bytes(),
            WorkspaceInputManifest,
        )
        attachment = next(
            entry for entry in manifest.entries if entry.input_kind == "ATTACHMENT"
        )
        plan = manifest.resolved_logparse_plan
        assert plan is not None and plan.attachment_id == attachment.resource_id
        request_bytes = canonical_json_bytes(
            ParseTargetsRequest(
                schema_version=1,
                problem_time=plan.problem_time,
                anchors=[
                    Anchor(
                        label=item.label,
                        module=item.module,
                        slot=item.slot,
                        process_name=item.process_name,
                        pid=item.pid,
                    )
                    for item in plan.anchors
                ],
                attachment_id=attachment.resource_id,
                artifact_proposal_key="logparse_run",
            )
        )
        if self.accept_request:
            session = self.factory.sessions[-1]
            record_request = getattr(session, "_record_parse_request")
            record_request(request_bytes)
            self.request_bytes = request_bytes
        if self.emit_claim:
            assert manifest.logparse_tool_ref is not None
            self.claim = LogparseParseClaim(
                schema_version=1,
                job_id=manifest.job_id,
                attachment_id=attachment.resource_id,
                attachment_sha256=attachment.sha256,
                artifact_proposal_key="logparse_run",
                logparse_tool_ref=manifest.logparse_tool_ref,
                request_sha256=hashlib.sha256(request_bytes).hexdigest(),
            )
            (workspace_root / "runtime/tool-state/logparse-parse.claim").write_bytes(
                canonical_json_bytes(self.claim)
            )

        if self.result == "timeout":
            self._close_sinks(kwargs)
            raise _failure()
        if self.result == "failed":
            outcome = _failed_logparse_agent_outcome(self.job)
        elif self.result in {"completed", "reroute_missing"}:
            assert self.claim is not None
            outcome = self._completed_outcome(
                self.job,
                workspace_root,
                self.claim,
            )
            if self.result == "reroute_missing":
                outcome = outcome.model_copy(
                    update={"result_type": OutcomeResultType.REROUTE}
                )
            assert self.request_bytes is not None
            session = self.factory.sessions[-1]
            audit_bytes = self._successful_audit_bytes(
                self.job.job_id,
                self.request_bytes,
                outcome,
                match_status=(
                    "missing" if self.result == "reroute_missing" else "exact"
                ),
            )
            setattr(session, "audit_bytes", lambda: audit_bytes)
        else:
            raise AssertionError("unknown claiming backend result")

        outcome_bytes = canonical_json_bytes(outcome)
        temporary = workspace_root / "output/.job_outcome.draft.json.part"
        temporary.write_bytes(outcome_bytes)
        os.replace(temporary, workspace_root / "output/job_outcome.draft.json")
        _write_finalization_marker(workspace_root, outcome_bytes)
        self._close_sinks(kwargs)
        return BackendExecution(
            returncode=0,
            stdout_stderr_bytes=0,
            workspace_bytes=0,
            elapsed_seconds=0.01,
        )


class _MethodsTwoPassBackend:
    """Exercise product-owned Logparse Pass A followed by Methods-only Pass B."""

    def __init__(
        self,
        factory: FakeLogparseBrokerFactory,
        job: Job,
        result: str,
        *,
        accept_request: bool = True,
        emit_claim: bool = True,
        noncanonical_draft: bool = False,
        malformed_draft: bool = False,
        safety_note: str = "Timeout does not prove downstream cancellation.",
    ) -> None:
        self.factory = factory
        self.job = job
        self.result = result
        self.accept_request = accept_request
        self.emit_claim = emit_claim
        self.noncanonical_draft = noncanonical_draft
        self.malformed_draft = malformed_draft
        self.safety_note = safety_note
        self.claim: LogparseParseClaim | None = None
        self.request_bytes: bytes | None = None
        self.written_draft_bytes: bytes | None = None
        self.calls: list[dict[str, Any]] = []
        self.session_closed_at_call: list[bool | None] = []

    @staticmethod
    def _close_sinks(kwargs: dict[str, Any]) -> None:
        _PublicFakeClaimingBackend._close_sinks(kwargs)

    def _run_preprocessing(self, kwargs: dict[str, Any]) -> BackendExecution:
        workspace_root = Path(kwargs["workspace_root"])
        if self.result == "helper_load_failed":
            self._close_sinks(kwargs)
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.BACKEND_EXIT_FAILED,
                message="The required Logparse Helper could not be loaded.",
            )
        request_path = (
            workspace_root / "output/proposals/methods-preprocess/request.json"
        )
        request_bytes = request_path.read_bytes()
        request = parse_canonical_json_bytes(request_bytes)
        session = self.factory.sessions[-1]
        if self.accept_request:
            record_request = getattr(session, "_record_parse_request")
            record_request(request_bytes)
            self.request_bytes = request_bytes

        manifest = parse_canonical_json_bytes(
            (workspace_root / "inputs/manifest.json").read_bytes(),
            WorkspaceInputManifest,
        )
        attachment = next(
            entry for entry in manifest.entries if entry.input_kind == "ATTACHMENT"
        )
        assert manifest.logparse_tool_ref is not None
        self.claim = LogparseParseClaim(
            schema_version=1,
            job_id=manifest.job_id,
            attachment_id=attachment.resource_id,
            attachment_sha256=attachment.sha256,
            artifact_proposal_key="methods-preprocess",
            logparse_tool_ref=manifest.logparse_tool_ref,
            request_sha256=hashlib.sha256(request_bytes).hexdigest(),
        )
        if self.emit_claim:
            (workspace_root / "runtime/tool-state/logparse-parse.claim").write_bytes(
                canonical_json_bytes(self.claim)
            )

        proposal_root = (
            workspace_root / "output/proposals/methods-preprocess/tree"
        )
        task_root = proposal_root / "task-0001"
        (task_root / "targets").mkdir(parents=True)
        target_contents = {
            "client": b"rpc deadline exceeded request_id=42\n",
            "server": b"connection pool wait request_id=42\n",
        }
        for label, content in target_contents.items():
            (task_root / f"targets/{label}.log").write_bytes(content)
        (task_root / "parse_manifest.json").write_bytes(
            canonical_json_bytes(
                {
                    "product": self.job.logparse_product,
                    "target_logs": [
                        "targets/client.log",
                        "targets/server.log",
                    ],
                    "version": 1,
                }
            )
        )
        _, tree_sha256, _ = inspect_tree(proposal_root)
        proposal = AgentArtifactProposalDraft(
            proposal_key="methods-preprocess",
            artifact_kind=ArtifactKind.LOGPARSE_RUN,
            name="methods-logparse-run",
            content_type="application/vnd.problem-locator.logparse-run+directory",
            resource_kind=ResourceKind.DIRECTORY,
            workspace_relative_path="output/proposals/methods-preprocess/tree",
            declared_size=None,
            declared_sha256=None,
            metadata={
                "tree_manifest_sha256": tree_sha256,
                "logparse_version_ref": manifest.logparse_tool_ref,
                "parse_manifest_relative_path": "task-0001/parse_manifest.json",
                "source_attachment_id": attachment.resource_id,
                "source_attachment_sha256": attachment.sha256,
                "parse_parameters": {"product": self.job.logparse_product},
            },
        )
        plan = manifest.resolved_logparse_plan
        assert plan is not None
        targets: list[dict[str, Any]] = []
        for anchor in plan.anchors:
            missing = self.result == "confirmed_missing" and anchor.label == "client"
            target: dict[str, Any] = {
                "label": anchor.label,
                "module": anchor.module,
                "module_key": anchor.module,
                "module_name": anchor.module,
                "slot": anchor.slot,
                "process_name": anchor.process_name,
                "match_status": "missing" if missing else "exact",
                "caveats": (
                    ["Synthetic missing client target."] if missing else []
                ),
            }
            if not missing:
                target["log_path"] = f"task-0001/targets/{anchor.label}.log"
            targets.append(target)
        result = {
            "schema_version": 1,
            "api_version": 1,
            "target_logs": targets,
            "logparse_run_artifact_draft": proposal.model_dump(mode="json"),
        }
        successful_operation = {
            "operation": "parse-targets",
            "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
            "request": request,
            "http_status": 200,
            "result_sha256": hashlib.sha256(
                canonical_json_bytes(result)
            ).hexdigest(),
            "result": result,
        }
        operations = [successful_operation]
        if self.result == "retry_then_completed":
            failed_operation = dict(successful_operation)
            failed_operation["http_status"] = 503
            operations.insert(0, failed_operation)
        audit_bytes = canonical_json_bytes(
            {
                "schema_version": 1,
                "job_id": manifest.job_id,
                "operations": operations,
            }
        )
        setattr(session, "audit_bytes", lambda: audit_bytes)
        (workspace_root / "output/proposals/methods-preprocess/target_logs.json").write_bytes(
            canonical_json_bytes(result)
        )

        self._close_sinks(kwargs)
        if self.result == "timeout":
            raise _failure()
        if self.result == "failed":
            raise RuntimeExecutionError(
                ExecutionFailure(
                    stage=ExecutionStage.TOOL_EXECUTE,
                    code=ErrorCode.LOGPARSE_FAILED,
                    message="The product-owned Logparse pass failed.",
                    retryable=False,
                    details=[],
                )
            )
        return BackendExecution(
            returncode=0,
            stdout_stderr_bytes=0,
            workspace_bytes=0,
            elapsed_seconds=0.01,
        )

    def _run_methods(self, kwargs: dict[str, Any]) -> BackendExecution:
        workspace_root = Path(kwargs["workspace_root"])
        sources = [
            {
                "source_id": "server",
                "line_number": 1,
                "marker": "connection pool wait",
                "line": "connection pool wait request_id=42",
            }
        ]
        if self.result != "confirmed_missing":
            sources.insert(
                0,
                {
                    "source_id": "client",
                    "line_number": 1,
                    "marker": "rpc deadline exceeded",
                    "line": "rpc deadline exceeded request_id=42",
                },
            )
        if self.result == "invalid_marker":
            sources[0]["marker"] = "rpc deadline"
        value = {
            "schema_version": 1,
            "status": "CONFIRMED",
            "confirmed_methods": ["rpc-call-timeout"],
            "candidate_methods": [],
            "evidence": [
                {
                    "method_id": "rpc-call-timeout",
                    "summary": "The frozen RPC logs contain the declared timeout markers.",
                    "identity_tokens": ["request_id=42"],
                    "sources": sources,
                }
            ],
            "limitations": ["Only the frozen target logs were checked."],
            "safety_notes": [self.safety_note],
        }
        canonical = canonical_json_bytes(value)
        if self.malformed_draft:
            self.written_draft_bytes = b'{"schema_version":'
        elif self.noncanonical_draft:
            self.written_draft_bytes = (
                json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
                + b"\n"
            )
        else:
            self.written_draft_bytes = canonical
        (workspace_root / "output/method-diagnosis.draft.json").write_bytes(
            self.written_draft_bytes
        )
        self._close_sinks(kwargs)
        return BackendExecution(
            returncode=0,
            stdout_stderr_bytes=0,
            workspace_bytes=0,
            elapsed_seconds=0.01,
        )

    def execute(self, **kwargs: Any) -> BackendExecution:
        self.calls.append(kwargs)
        self.session_closed_at_call.append(
            None if not self.factory.sessions else self.factory.sessions[-1].closed  # type: ignore[attr-defined]
        )
        if kwargs.get("broker_environment") is not None:
            return self._run_preprocessing(kwargs)
        return self._run_methods(kwargs)


def _failed_logparse_agent_outcome(job: Job) -> AgentJobOutcomeDraftV2:
    return AgentJobOutcomeDraftV2(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        base_state_revision=job.base_state_revision,
        result_type=OutcomeResultType.FAILED,
        payload=None,
        consumed_evidence_refs=[],
        proposed_evidence_drafts=[],
        proposed_artifact_drafts=[],
        error=ExecutionFailure(
            stage=ExecutionStage.TOOL_EXECUTE,
            code=ErrorCode.LOGPARSE_FAILED,
            message="The fixed logparse execution failed.",
            retryable=False,
            details=[],
        ),
        rule_claims=[],
    )


def _public_fake_claiming_runtime(
    tmp_path: Path,
    result: str,
    *,
    accept_request: bool = True,
    emit_claim: bool = True,
) -> tuple[
    DiagnosisRuntime,
    Job,
    FakeLogparseBrokerFactory,
    _MethodsTwoPassBackend,
    InMemoryResourceStore,
]:
    factory = FakeLogparseBrokerFactory()
    catalog = _logparse_catalog(tmp_path, factory)
    job, aggregate, resources = _claimed_logparse_job_state_and_resources(catalog)
    backend = _MethodsTwoPassBackend(
        factory,
        job,
        result,
        accept_request=accept_request,
        emit_claim=emit_claim,
    )
    runtime = DiagnosisRuntime(
        state_repository=_StateView(aggregate),
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=factory,
        execution_records=InMemoryExecutionRecordStore(),
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "public-fake-logparse-data"),
        backend=backend,  # type: ignore[arg-type]
    )
    return runtime, job, factory, backend, resources


@pytest.mark.parametrize("missing_binding", ("user_facts", "attachment"))
def test_methods_preflight_publishes_waiting_without_backend_or_broker(
    missing_binding: str,
    tmp_path: Path,
) -> None:
    factory = _RuntimeBrokerFactory()
    catalog = _logparse_catalog(tmp_path, factory)
    job, aggregate, resources = _claimed_logparse_job_state_and_resources(catalog)
    job_payload = job.model_dump(mode="json")
    aggregate_payload = aggregate.model_dump(mode="json")
    if missing_binding == "user_facts":
        job_payload["context_snapshot"]["user_facts"] = []
        aggregate_payload["case"]["diagnosis_state"]["user_facts"] = []
    else:
        job_payload["attachment_refs"] = []
        aggregate_payload["attachments"] = {}
        resources = InMemoryResourceStore()
    job = Job.model_validate(job_payload)
    aggregate_payload["jobs"] = {job.job_id: job.model_dump(mode="json")}
    aggregate = CaseAggregate.model_validate(aggregate_payload)
    records = InMemoryExecutionRecordStore()
    workspace_root = tmp_path / f"missing-{missing_binding}-data"
    runtime = DiagnosisRuntime(
        state_repository=_StateView(aggregate),
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=factory,
        execution_records=records,
        clock=_Clock(),
        id_generator=DeterministicIdGenerator(seed=f"methods-{missing_binding}"),
        workspace_manager=WorkspaceManager(workspace_root),
        backend=_NeverBackend(),  # type: ignore[arg-type]
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    outcome = receipt.job_outcome
    assert outcome.decision_audit is None
    assert outcome.payload is not None
    assert factory.calls == []
    assert records.log_sinks == {}
    assert records.read_audit_bytes(job.job_id, "stdout.log") is None
    assert records.read_audit_bytes(job.job_id, "stderr.log") is None
    assert not workspace_root.exists()
    preflight_bytes = records.read_audit_bytes(job.job_id, "methods_preflight.json")
    assert preflight_bytes is not None
    preflight_receipt = json.loads(preflight_bytes)
    assert preflight_receipt["job_id"] == job.job_id
    assert preflight_receipt["result_type"] == outcome.result_type
    requirements = {
        item.name: item
        for item in outcome.payload.state_delta.add_pending_requirements
    }
    if missing_binding == "user_facts":
        assert outcome.result_type is OutcomeResultType.NEED_INPUT
        assert list(requirements) == [
            "problem_time",
            "client_slot",
            "client_process_name",
            "server_slot",
            "server_process_name",
            "caller_service",
            "server_service",
            "rpc_method",
        ]
        problem = requirements["problem_time"]
        assert problem.prompt == "请提供毫秒精度 UTC 问题时间。"
        assert isinstance(problem.constraints, InputRequirementConstraints)
        assert problem.constraints.min_utf8_bytes == 24
        assert problem.constraints.max_utf8_bytes == 24
        assert problem.constraints.pattern == (
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
        )
        role = requirements["client_process_name"]
        assert role.prompt == "请提供 client 角色的 process_name。"
        assert isinstance(role.constraints, InputRequirementConstraints)
        assert role.constraints.max_utf8_bytes == 256
        package_extra = requirements["caller_service"]
        assert isinstance(package_extra.constraints, InputRequirementConstraints)
        assert package_extra.constraints.max_utf8_bytes == 4096
        assert outcome.payload.requested_attachments == []
    else:
        assert outcome.result_type is OutcomeResultType.NEED_ATTACHMENT
        assert list(requirements) == ["log_archive"]
        archive = requirements["log_archive"]
        assert archive.prompt == "请上传 Logparse 支持的日志归档。"
        assert isinstance(archive.constraints, AttachmentRequirementConstraints)
        assert archive.constraints.allowed_content_types == [
            "application/gzip",
            "application/zip",
            "application/x-tar",
        ]
        assert outcome.payload.requested_input == []


def test_ready_logparse_job_rejects_untyped_compiler_omission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _RuntimeBrokerFactory()
    catalog = _logparse_catalog(tmp_path, factory)
    job, aggregate, resources = _claimed_logparse_job_state_and_resources(catalog)
    monkeypatch.setattr(
        "problem_locator.runtime.diagnosis_runtime.compile_resolved_logparse_plan",
        lambda *args: None,
    )
    runtime = DiagnosisRuntime(
        state_repository=_StateView(aggregate),
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=factory,
        execution_records=InMemoryExecutionRecordStore(),
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "omitted-plan-data"),
        backend=_NeverBackend(),  # type: ignore[arg-type]
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.stage is ExecutionStage.ASSET_RESOLUTION
    assert receipt.job_outcome.error.code is ErrorCode.ASSET_VERSION_UNAVAILABLE
    assert factory.calls == []


def test_default_product_survives_compiler_and_workspace_manifest(
    tmp_path: Path,
) -> None:
    factory = FakeLogparseBrokerFactory()
    catalog = _logparse_catalog(
        tmp_path,
        factory,
        logparse_product="default",
    )
    job, aggregate, resources = _claimed_logparse_job_state_and_resources(catalog)
    backend = _MethodsTwoPassBackend(
        factory,
        job,
        "helper_load_failed",
    )
    runtime = DiagnosisRuntime(
        state_repository=_StateView(aggregate),
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=factory,
        execution_records=InMemoryExecutionRecordStore(),
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "default-product-data"),
        backend=backend,  # type: ignore[arg-type]
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert job.logparse_product == "default"
    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.BACKEND_EXIT_FAILED
    preprocessing_root = Path(backend.calls[0]["workspace_root"])
    manifest = parse_canonical_json_bytes(
        (preprocessing_root / "inputs/manifest.json").read_bytes(),
        WorkspaceInputManifest,
    )
    assert manifest.logparse_product == "default"
    assert manifest.resolved_logparse_plan is not None
    request = parse_canonical_json_bytes(
        (
            preprocessing_root
            / "output/proposals/methods-preprocess/request.json"
        ).read_bytes()
    )
    assert "logparse_product" not in request


class _AgentJsonClaimingBackend(_MethodsTwoPassBackend):
    @property
    def authored_bytes(self) -> bytes | None:
        return self.written_draft_bytes


def _agent_json_claiming_runtime(
    tmp_path: Path,
    records: InMemoryExecutionRecordStore,
    *,
    malformed_draft: bool = False,
) -> tuple[DiagnosisRuntime, Job, _AgentJsonClaimingBackend]:
    factory = FakeLogparseBrokerFactory()
    catalog = _logparse_catalog(tmp_path, factory)
    job, aggregate, resources = _claimed_logparse_job_state_and_resources(catalog)
    backend = _AgentJsonClaimingBackend(
        factory,
        job,
        "completed",
        noncanonical_draft=not malformed_draft,
        malformed_draft=malformed_draft,
    )
    runtime = DiagnosisRuntime(
        state_repository=_StateView(aggregate),
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=factory,
        execution_records=records,
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "agent-json-logparse-data"),
        backend=backend,  # type: ignore[arg-type]
    )
    return runtime, job, backend


@pytest.mark.parametrize(
    ("result", "expected_code"),
    [
        ("timeout", ErrorCode.BACKEND_TIMEOUT),
        ("failed", ErrorCode.LOGPARSE_FAILED),
    ],
)
def test_public_broker_fake_closes_claimed_timeout_and_failed_executions(
    result: str,
    expected_code: ErrorCode,
    tmp_path: Path,
) -> None:
    runtime, job, factory, backend, _ = _public_fake_claiming_runtime(
        tmp_path,
        result,
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is expected_code
    assert backend.claim is not None
    assert backend.request_bytes is not None
    session = factory.sessions[0]
    assert session.closed is True  # type: ignore[attr-defined]
    assert session.close_calls == 1  # type: ignore[attr-defined]
    assert session.parse_request_bytes() == backend.request_bytes
    if result == "timeout":
        assert not any(
            detail.field == "logparse_claim"
            for detail in receipt.job_outcome.error.details
        )


def test_unclassified_methods_validation_uses_generic_reason_code() -> None:
    assert _method_validation_reason_code(ValueError("unclassified")) is (
        MethodsValidationReasonCode.VALIDATION_FAILED
    )


def test_methods_helper_load_failure_never_reaches_broker_or_pass_b(
    tmp_path: Path,
) -> None:
    # The fake models a terminal Helper-load failure; it does not claim to
    # observe a real Claude Skill tool call. Fast E2E owns that trace proof.
    runtime, job, factory, backend, _ = _public_fake_claiming_runtime(
        tmp_path,
        "helper_load_failed",
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.BACKEND_EXIT_FAILED
    assert len(backend.calls) == 1
    assert len(factory.sessions) == 1
    session = factory.sessions[0]
    assert session.closed is True  # type: ignore[attr-defined]
    assert session.close_calls == 1  # type: ignore[attr-defined]
    assert session.parse_request_bytes() is None
    records = runtime._execution_records
    assert isinstance(records, InMemoryExecutionRecordStore)
    assert records.read_audit_bytes(job.job_id, "logparse_broker_audit.json") is None
    assert records.read_audit_bytes(job.job_id, "methods_target_logs.json") is None


def test_methods_preprocessing_rejects_failed_operation_before_success(
    tmp_path: Path,
) -> None:
    runtime, job, factory, backend, _ = _public_fake_claiming_runtime(
        tmp_path,
        "retry_then_completed",
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.LOGPARSE_OUTPUT_INVALID
    assert len(backend.calls) == 1
    session = factory.sessions[0]
    assert session.closed is True  # type: ignore[attr-defined]
    assert session.close_calls == 1  # type: ignore[attr-defined]
    assert session.parse_request_bytes() == backend.request_bytes
    records = runtime._execution_records
    assert isinstance(records, InMemoryExecutionRecordStore)
    assert records.read_audit_bytes(job.job_id, "methods_target_logs.json") is None


def test_logparse_broker_error_is_preserved_as_asset_failure(
    tmp_path: Path,
) -> None:
    failure = ExecutionFailure(
        stage=ExecutionStage.ASSET_RESOLUTION,
        code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
        message="The fixed logparse asset is unavailable.",
        retryable=False,
        details=[],
    )
    factory = _RuntimeBrokerFactory(failure=LogparseBrokerError(failure))
    catalog = _logparse_catalog(tmp_path, factory)
    job, aggregate, resources = _claimed_logparse_job_state_and_resources(catalog)
    records = InMemoryExecutionRecordStore()
    runtime = DiagnosisRuntime(
        state_repository=_StateView(aggregate),
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=factory,
        execution_records=records,
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "broker-error-data"),
        backend=_NeverBackend(),  # type: ignore[arg-type]
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error == failure
    assert len(factory.calls) == 1
    assert set(records.log_sinks) == {job.job_id}
    sinks = records.log_sinks[job.job_id]
    assert sinks.stdout.close_calls == 1  # type: ignore[attr-defined]
    assert sinks.stderr.close_calls == 1  # type: ignore[attr-defined]


def test_broker_close_failure_preserves_possible_secret_output(
    tmp_path: Path,
) -> None:
    factory = _RuntimeBrokerFactory(close_fails=True)
    catalog = _logparse_catalog(tmp_path, factory)
    job, aggregate, resources = _claimed_logparse_job_state_and_resources(catalog)
    unsafe = _failed_logparse_agent_outcome(job).model_dump(mode="json")
    unsafe["error"]["message"] = "runtime-test-token"
    backend = _RuntimeBackend(
        canonical_json_bytes(AgentJobOutcomeDraftV2.model_validate(unsafe))
    )
    records = InMemoryExecutionRecordStore()
    runtime = DiagnosisRuntime(
        state_repository=_StateView(aggregate),
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=factory,
        execution_records=records,
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "close-secret-data"),
        backend=backend,  # type: ignore[arg-type]
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.LOGPARSE_FAILED
    assert factory.session.events == ["close", "parse_request_bytes", "audit_bytes"]
    workspace_root = factory.calls[0][1]
    assert (workspace_root / "output/job_outcome.draft.json").is_file()
    assert b"runtime-test-token" not in records.publish_outcome_calls[0][1]


def test_backend_timeout_preserves_primary_when_claim_audit_fails(
    tmp_path: Path,
) -> None:
    class TimeoutBackend:
        def execute(self, **kwargs: Any) -> BackendExecution:
            workspace_root = Path(kwargs["workspace_root"])
            (workspace_root / "runtime/tool-state/unexpected-node").write_bytes(b"x")
            raise _failure()

    factory = _RuntimeBrokerFactory()
    catalog = _logparse_catalog(tmp_path, factory)
    job, aggregate, resources = _claimed_logparse_job_state_and_resources(catalog)
    records = InMemoryExecutionRecordStore()
    runtime = DiagnosisRuntime(
        state_repository=_StateView(aggregate),
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=factory,
        execution_records=records,
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "timeout-claim-data"),
        backend=TimeoutBackend(),  # type: ignore[arg-type]
    )

    cancellation = InMemoryCancellationSignal()
    receipt = runtime.execute(job, cancellation)

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.BACKEND_TIMEOUT
    assert any(
        detail.field == "logparse_claim" and detail.actual == "audit_failed"
        for detail in receipt.job_outcome.error.details
    )
    assert factory.session.events == ["close", "parse_request_bytes", "audit_bytes"]
    assert cancellation.is_cancelled() is False


def test_successful_backend_audits_claim_before_reporting_missing_outcome(
    tmp_path: Path,
) -> None:
    factory = _RuntimeBrokerFactory()
    catalog = _logparse_catalog(tmp_path, factory)
    job, aggregate, resources = _claimed_logparse_job_state_and_resources(catalog)
    backend = _RuntimeBackend(
        None,
        tool_state_files={"unexpected-node": b"not a claim"},
    )
    runtime = DiagnosisRuntime(
        state_repository=_StateView(aggregate),
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=factory,
        execution_records=InMemoryExecutionRecordStore(),
        clock=_Clock(),
        id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "missing-outcome-claim-data"),
        backend=backend,  # type: ignore[arg-type]
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.LOGPARSE_OUTPUT_INVALID
    assert factory.session.events == ["close", "parse_request_bytes", "audit_bytes"]


class _PostCloseAuditSession:
    def __init__(self, *, close_fails: bool = False) -> None:
        self.closed = False
        self.close_fails = close_fails
        self.events: list[str] = []

    def agent_environment(self) -> dict[str, str]:
        raise AssertionError("not used")

    def close(self) -> None:
        self.events.append("close")
        self.closed = True
        if self.close_fails:
            raise OSError("injected close failure")

    def parse_request_bytes(self) -> bytes:
        assert self.closed
        self.events.append("parse_request_bytes")
        return b"{}"

    def audit_bytes(self) -> bytes:
        assert self.closed
        self.events.append("audit_bytes")
        return canonical_json_bytes(
            {
                "schema_version": 1,
                "job_id": "00000000-0000-0000-0000-000000000011",
                "operations": [],
            }
        )


def test_broker_request_bytes_are_audited_only_after_close() -> None:
    session = _PostCloseAuditSession()

    failure, request_bytes, audit_bytes = DiagnosisRuntime._close_and_audit_broker(
        session,  # type: ignore[arg-type]
        None,
    )

    assert failure is None
    assert request_bytes == b"{}"
    assert audit_bytes is not None
    assert session.events == ["close", "parse_request_bytes", "audit_bytes"]


def test_broker_close_failure_does_not_replace_backend_timeout() -> None:
    session = _PostCloseAuditSession(close_fails=True)
    timeout = ExecutionFailure(
        stage=ExecutionStage.BACKEND_EXECUTE,
        code=ErrorCode.BACKEND_TIMEOUT,
        message="Agent execution exceeded the fixed wall time.",
        retryable=True,
        details=[],
    )

    failure, request_bytes, audit_bytes = DiagnosisRuntime._close_and_audit_broker(
        session,  # type: ignore[arg-type]
        timeout,
    )

    assert failure is not None
    assert failure.code is ErrorCode.BACKEND_TIMEOUT
    assert request_bytes == b"{}"
    assert any(
        detail.field == "broker_session"
        and detail.actual == "cleanup_failed"
        for detail in failure.details
    )


def test_diagnosis_runtime_fixture_manifests_remain_contract_valid() -> None:
    for root in (
        REPOSITORY_ROOT / "tests/fixtures/components/runtime-catalog",
        REPOSITORY_ROOT / "tests/fixtures/components/runtime-context",
        REPOSITORY_ROOT / "tests/fixtures/components/runtime-command",
        REPOSITORY_ROOT / "tests/fixtures/components/runtime-backend",
        REPOSITORY_ROOT / "tests/fixtures/components/runtime-output",
        REPOSITORY_ROOT / "tests/fixtures/components/runtime-logparse",
    ):
        manifest = FixtureManifest.model_validate_json(
            (root / "fixture-manifest.json").read_bytes()
        )
        assert manifest.owner_spec == "S04"
        actual_paths = sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and path.name != "fixture-manifest.json"
        )
        assert [entry.path for entry in manifest.files] == actual_paths
        for entry in manifest.files:
            data = (root / entry.path).read_bytes()
            assert entry.size == len(data)
            assert entry.sha256 == hashlib.sha256(data).hexdigest()
