from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from problem_locator.contracts import (
    Attachment,
    AttachmentStatus,
    CancellationReason,
    CaseAggregate,
    ErrorCode,
    GenericDiagnosisOutcomeV2,
    Job,
    OutcomeResultType,
    ResourceKind,
    ResourceRef,
)
from problem_locator.runtime.agent_backend import BackendExecutionLimits
from problem_locator.integrations.logparse import broker as broker_module
from problem_locator.runtime import diagnosis_runtime as diagnosis_module
from problem_locator.runtime.catalog import VersionedAssetCatalog
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.generic_logs import freeze_generic_logs, verify_generic_logs
from problem_locator.runtime.workspace import WorkspaceManager
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
    InMemoryCancellationSignal,
    InMemoryExecutionRecordStore,
    InMemoryResourceStore,
)
from tests.deterministic.unit.integrations.test_logparse_fake_e2e import (
    ATTACHMENT_ID,
    _factory,
    pinned_asset,
)
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    _Clock,
    _GenericRuntimeBackend,
    _StateView,
    _generic_aggregate,
    _generic_v2_result_bytes,
    _running_generic_job,
)


@pytest.fixture(autouse=True)
def restore_test_permissions(tmp_path: Path):
    yield
    for path in tmp_path.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)


def _files(tmp_path: Path, content: bytes = b"root-cause=connection-pool\n"):
    root = tmp_path / "workspace"
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    metadata = inputs.stat()
    workspace = SimpleNamespace(root=root, inputs_device=metadata.st_dev, inputs_inode=metadata.st_ino)
    source_root = tmp_path / "parsed"
    relative = "task/mech_modules/COMPACT/slot_1/cycle/service.log"
    source = source_root / relative
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    parsed = {"schema_version": 1, "logs": [{"relative_path": relative,
        "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}]}
    return workspace, source_root, source, parsed


def test_generic_logs_stream_large_files_outside_the_prompt_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = b"root-cause=connection-pool\n" + b"x" * (3 * 1024 * 1024)
    workspace, source_root, _, parsed = _files(tmp_path, content)
    original = Path.read_bytes

    def forbid_buffering_logs(path: Path) -> bytes:
        if path.suffix == ".log":
            pytest.fail("complete logs must not be buffered with read_bytes")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", forbid_buffering_logs)
    cancellation = InMemoryCancellationSignal()
    receipt = freeze_generic_logs(workspace, controlled_root=source_root, parsed=parsed, cancellation=cancellation)
    assert len(receipt) < 1024
    value = json.loads(receipt)
    destination = workspace.root / value["logs"][0]["log_path"]
    assert destination.stat().st_size == len(content)
    assert not destination.stat().st_mode & 0o222
    assert "root-cause" not in receipt.decode()
    verify_generic_logs(workspace, receipt=receipt, parsed=parsed, cancellation=cancellation)


def test_generic_logs_reject_source_drift_before_copy(tmp_path: Path) -> None:
    workspace, source_root, source, parsed = _files(tmp_path)
    source.write_bytes(b"x" * parsed["logs"][0]["size"])
    with pytest.raises(ValueError):
        freeze_generic_logs(workspace, controlled_root=source_root, parsed=parsed, cancellation=InMemoryCancellationSignal())
    assert not (workspace.root / "inputs/generic_logs.json").exists()


@pytest.mark.parametrize("target", ["log", "manifest"])
def test_generic_logs_reject_input_tampering_after_copy(tmp_path: Path, target: str) -> None:
    workspace, source_root, _, parsed = _files(tmp_path)
    signal = InMemoryCancellationSignal()
    receipt = freeze_generic_logs(workspace, controlled_root=source_root, parsed=parsed, cancellation=signal)
    path = workspace.root / ("inputs/generic-logs/log-000001.log" if target == "log" else "inputs/generic_logs.json")
    path.chmod(0o644)
    path.write_bytes(b"forged\n")
    with pytest.raises(ValueError):
        verify_generic_logs(workspace, receipt=receipt, parsed=parsed, cancellation=signal)


def test_generic_logs_reject_escaped_sources(tmp_path: Path) -> None:
    workspace, source_root, _, parsed = _files(tmp_path)
    parsed["logs"][0]["relative_path"] = "../outside.log"
    with pytest.raises(ValueError):
        freeze_generic_logs(workspace, controlled_root=source_root, parsed=parsed, cancellation=InMemoryCancellationSignal())


def test_generic_logs_honor_cancellation_and_manifest_budget(tmp_path: Path) -> None:
    workspace, source_root, _, parsed = _files(tmp_path)
    cancelled = InMemoryCancellationSignal(CancellationReason.USER_CANCEL)
    with pytest.raises(RuntimeExecutionError) as raised:
        freeze_generic_logs(workspace, controlled_root=source_root, parsed=parsed, cancellation=cancelled)
    assert raised.value.failure.code is ErrorCode.BACKEND_CANCELLED
    budget_workspace, other_root, _, other_parsed = _files(tmp_path / "budget")
    other_parsed.update(logs=[], padding="x" * 2_000_000)
    with pytest.raises(ValueError, match="budget"):
        freeze_generic_logs(budget_workspace, controlled_root=other_root, parsed=other_parsed, cancellation=InMemoryCancellationSignal())


def test_empty_generic_logs_honor_cancellation(tmp_path: Path) -> None:
    workspace, source_root, _, parsed = _files(tmp_path)
    parsed["logs"] = []
    receipt = freeze_generic_logs(workspace, controlled_root=source_root, parsed=parsed,
        cancellation=InMemoryCancellationSignal())
    with pytest.raises(RuntimeExecutionError) as raised:
        verify_generic_logs(workspace, receipt=receipt, parsed=parsed,
            cancellation=InMemoryCancellationSignal(CancellationReason.USER_CANCEL))
    assert raised.value.failure.code is ErrorCode.BACKEND_CANCELLED


class _ReadingBackend(_GenericRuntimeBackend):
    def __init__(self, *, tamper: bool = False) -> None:
        super().__init__(None)
        self.tamper = tamper
        self.observed: list[str] = []

    def execute(self, **kwargs):
        root = Path(kwargs["workspace_root"])
        manifest = json.loads((root / "inputs/generic_logs.json").read_bytes())
        for entry in manifest["logs"]:
            path = root / entry["log_path"]
            assert not path.stat().st_mode & 0o222
            with path.open(encoding="utf-8") as stream:
                self.observed.append(stream.read(4096))
        assert not (root / "inputs/attachments").exists()
        assert "PROBLEM_LOCATOR_LOGPARSE_TOKEN" not in kwargs
        actual_logs = "\n".join(self.observed)
        clue = "dns resolution failed" if "dns resolution failed" in actual_logs else "connection pool wait 2800ms"
        assert clue in actual_logs
        assert clue not in kwargs["prompt"]
        assert "inputs/generic_logs.json" in kwargs["prompt"]
        assert len(kwargs["prompt"].encode()) < kwargs["resource_limits"].context_bytes
        self.v2_result_bytes = _generic_v2_result_bytes(f"# 日志定位报告\n\n日志依据：{clue}。\n")
        if self.tamper:
            first = root / manifest["logs"][0]["log_path"]
            first.chmod(0o644)
            first.write_bytes(b"changed after analysis\n")
        return super().execute(**kwargs)


def _runtime(tmp_path: Path, asset, backend, *, marker: bytes = b"VALID", mode: str = "normal", limits=None,
             ready: bool = True, supplements=(), experience_retriever=None):
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()

    def parsed_output(point: str) -> None:
        if point != "process_finished":
            return
        for path in (tmp_path / "data").rglob("*.log"):
            if mode == "empty":
                path.unlink()
            elif mode == "dns" and path.name == "inventory-server-202.log":
                path.write_bytes(b"dns resolution failed\n")
            elif mode == "large" and path.name == "checkout-client-101.log":
                path.write_bytes(b"unrelated line\n" * 250_000)

    factory = _factory(asset, fault_point=parsed_output)
    catalog = VersionedAssetCatalog(skill_dir=skill_dir, generic_skill_name="generic-problem-locator-smoke",
        generic_logparse_product="compact", logparse_tool=asset, logparse_broker_factory=factory)
    base_job = _running_generic_job(catalog)
    payload = base_job.model_dump(mode="json")
    payload.update(catalog.generic_diagnose_bindings(with_logs=True).model_dump(mode="json"))
    payload.update(attachment_refs=[ATTACHMENT_ID] if ready else [], generic_log_archive_expected=True,
        generic_supplement_texts=list(supplements))
    job = Job.model_validate(payload)
    digest = hashlib.sha256(marker).hexdigest()
    key = f"resources/cases/{job.case_id}/attachments/{ATTACHMENT_ID}/input.zip"
    attachment = Attachment(attachment_id=ATTACHMENT_ID, case_id=job.case_id, status=AttachmentStatus.READY,
        name="input.zip", content_type="application/zip", declared_size=len(marker), declared_sha256=digest,
        size=len(marker), sha256=digest, storage_key=key, created_at="2026-07-31T00:00:00.000Z", updated_at="2026-07-31T00:00:00.000Z")
    aggregate = _generic_aggregate(base_job).model_dump(mode="json")
    aggregate["jobs"] = {job.job_id: job.model_dump(mode="json")}
    aggregate["attachments"][ATTACHMENT_ID] = attachment.model_dump(mode="json")
    aggregate["case"]["diagnosis_state"]["user_facts"] = []
    aggregate["case"]["diagnosis_state"]["pending_requirements"] = []
    state = _StateView(CaseAggregate.model_validate(aggregate))
    resources = InMemoryResourceStore()
    resources.seed_formal_resource(ResourceRef(resource_kind=ResourceKind.FILE, storage_key=key, size=len(marker), sha256=digest),
        state_reference_count=1, payload=marker)
    records = InMemoryExecutionRecordStore()
    runtime = DiagnosisRuntime(state_repository=state, resource_store=resources, asset_catalog=catalog,
        logparse_broker_factory=factory, execution_records=records, clock=_Clock(), id_generator=DeterministicIdGenerator(),
        workspace_manager=WorkspaceManager(tmp_path / "data"), backend=backend, backend_test_limits=limits,
        experience_retriever=experience_retriever)
    return runtime, job, records


@pytest.mark.parametrize("mode,expected", [("normal", "connection pool wait 2800ms"), ("dns", "dns resolution failed"), ("large", "connection pool wait 2800ms")])
def test_generic_runtime_report_depends_on_actual_logs_without_required_parameters(tmp_path: Path, pinned_asset, mode: str, expected: str) -> None:
    backend = _ReadingBackend()
    runtime, job, _ = _runtime(tmp_path, pinned_asset, backend, mode=mode)
    assert job.context_snapshot is None
    receipt = runtime.execute(job, InMemoryCancellationSignal())
    assert receipt.job_outcome.error is None
    assert receipt.job_outcome.result_type is OutcomeResultType.COMPLETED
    assert isinstance(receipt.job_outcome.payload, GenericDiagnosisOutcomeV2)
    assert expected in receipt.job_outcome.payload.report_markdown
    assert len(backend.calls) == 1


@pytest.mark.parametrize("memory_fits", [True, False], ids=["memory-fits", "memory-exceeds-budget"])
def test_generic_logs_supplements_and_memory_share_one_bounded_skill_invocation(
    tmp_path: Path, pinned_asset, monkeypatch: pytest.MonkeyPatch, memory_fits: bool,
) -> None:
    from problem_locator.runtime.generic_locator import GenericLocatorExecutor

    reference = '历史参考（未经验证）\n{"card_id":"card-1","steps":["检查连接释放逻辑"]}'
    supplement = "补充：异常发生前刚调整了连接池配置。"
    selections = []

    def select(skill_name, problem_text):
        selections.append((skill_name, problem_text))
        return SimpleNamespace(reference_text=reference)

    backend = _ReadingBackend()
    runtime, job, records = _runtime(tmp_path, pinned_asset, backend, supplements=[supplement],
        experience_retriever=SimpleNamespace(select=select))
    if not memory_fits:
        build_prompt = GenericLocatorExecutor.build_prompt
        assets = runtime._resolve_assets(job)
        padding = " " * (job.resource_limits.context_bytes - len(build_prompt(job, assets).encode("utf-8")) - 1)
        monkeypatch.setattr(GenericLocatorExecutor, "build_prompt", staticmethod(
            lambda *args, **kwargs: padding + build_prompt(*args, **kwargs)))

    receipt = runtime.execute(job, InMemoryCancellationSignal())
    assert receipt.job_outcome.error is None
    assert "connection pool wait 2800ms" in receipt.job_outcome.payload.report_markdown
    assert len(backend.calls) == 1 and backend.observed
    assert selections == [(job.generic_skill_name, job.generic_problem_text)]
    prompt = backend.calls[0]["prompt"]
    assert f"{job.generic_problem_text}\n<<<END_RAW_PROBLEM_TEXT>>>" in prompt
    assert f"{supplement}\n<<<END_SUPPLEMENT_TEXT>>>" in prompt
    assert "inputs/generic_logs.json" in prompt
    assert (reference in prompt) is memory_fits
    assert len(prompt.encode("utf-8")) <= job.resource_limits.context_bytes
    assert records.read_audit_bytes(job.job_id, "context.txt") == prompt.encode("utf-8")


def test_generic_runtime_empty_parsed_logs_resolves_without_backend(tmp_path: Path, pinned_asset) -> None:
    backend = _GenericRuntimeBackend(None)
    runtime, job, _ = _runtime(tmp_path, pinned_asset, backend, mode="empty")
    receipt = runtime.execute(job, InMemoryCancellationSignal())
    assert receipt.job_outcome.error is None
    assert receipt.job_outcome.payload.status.value == "UNRESOLVED"
    assert "未找到可分析日志" in receipt.job_outcome.payload.report_markdown
    assert backend.calls == []


def test_empty_runtime_cancel(tmp_path: Path, pinned_asset, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _GenericRuntimeBackend(None)
    runtime, job, _ = _runtime(tmp_path, pinned_asset, backend, mode="empty")
    signal = InMemoryCancellationSignal()
    original = runtime._prepare_generic_logs

    def cancel_after_prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        signal.cancel(CancellationReason.USER_CANCEL)
        return result

    monkeypatch.setattr(runtime, "_prepare_generic_logs", cancel_after_prepare)
    receipt = runtime.execute(job, signal)
    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert receipt.job_outcome.error.code is ErrorCode.BACKEND_CANCELLED
    assert backend.calls == []


@pytest.mark.parametrize("marker,code", [(b"NONZERO", ErrorCode.LOGPARSE_FAILED), (b"MISSING_MANIFEST", ErrorCode.LOGPARSE_OUTPUT_INVALID)])
def test_generic_runtime_parse_failure_never_becomes_text_only_success(tmp_path: Path, pinned_asset, marker: bytes, code: ErrorCode) -> None:
    backend = _GenericRuntimeBackend(None)
    runtime, job, _ = _runtime(tmp_path, pinned_asset, backend, marker=marker)
    receipt = runtime.execute(job, InMemoryCancellationSignal())
    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert receipt.job_outcome.error.code is code
    assert backend.calls == []


def test_generic_runtime_rejects_log_tampering_after_backend(tmp_path: Path, pinned_asset) -> None:
    backend = _ReadingBackend(tamper=True)
    runtime, job, _ = _runtime(tmp_path, pinned_asset, backend)
    receipt = runtime.execute(job, InMemoryCancellationSignal())
    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert receipt.job_outcome.error.code is ErrorCode.OUTCOME_INVALID
    assert len(backend.calls) == 1


def test_generic_runtime_enforces_preprocessing_workspace_budget(tmp_path: Path, pinned_asset) -> None:
    backend = _GenericRuntimeBackend(None)
    limits = BackendExecutionLimits(wall_time_seconds=20, stdout_stderr_bytes=1024 * 1024, workspace_bytes=32)
    runtime, job, _ = _runtime(tmp_path, pinned_asset, backend, limits=limits)
    receipt = runtime.execute(job, InMemoryCancellationSignal())
    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert receipt.job_outcome.error.code is ErrorCode.WORKSPACE_LIMIT
    assert backend.calls == []


@pytest.mark.parametrize("stage", ["broker-tree", "broker-text", "revalidation"])
@pytest.mark.parametrize("reason", ["cancel", "timeout"])
def test_generic_scan_limits(tmp_path: Path, pinned_asset, monkeypatch: pytest.MonkeyPatch,
                             stage: str, reason: str) -> None:
    backend = _GenericRuntimeBackend(None)
    limits = BackendExecutionLimits(wall_time_seconds=20, stdout_stderr_bytes=1024 * 1024,
        workspace_bytes=1024 * 1024)
    runtime, job, _ = _runtime(tmp_path, pinned_asset, backend, limits=limits)
    signal = InMemoryCancellationSignal()
    monotonic = time.monotonic
    offset = 0.0
    monkeypatch.setattr(diagnosis_module.time, "monotonic", lambda: monotonic() + offset)
    owner, name = {
        "broker-tree": (broker_module, "inspect_controlled_run"),
        "broker-text": (broker_module, "generic_parse_result"),
        "revalidation": (diagnosis_module, "validate_generic_parse_result"),
    }[stage]
    original = getattr(owner, name)
    reached = False

    def interrupt(*args, **kwargs):
        nonlocal offset, reached
        reached = True
        if reason == "cancel":
            signal.cancel(CancellationReason.USER_CANCEL)
        else:
            offset = 30.0
        # No scan may begin once its caller observes cancellation or timeout.
        assert callable(kwargs.get("check_abort"))
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, interrupt)
    receipt = runtime.execute(job, signal)
    assert reached
    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    expected = ErrorCode.BACKEND_CANCELLED if reason == "cancel" else ErrorCode.BACKEND_TIMEOUT
    assert receipt.job_outcome.error.code is expected
    assert backend.calls == []


def test_generic_runtime_waits_for_selected_upload_without_model_or_parameters(tmp_path: Path, pinned_asset, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _GenericRuntimeBackend(None)
    runtime, job, _ = _runtime(tmp_path, pinned_asset, backend, ready=False)
    record_path = tmp_path / "invocations.json"
    monkeypatch.setenv("S07_FAKE_LOGPARSE_RECORD", str(record_path))
    receipt = runtime.execute(job, InMemoryCancellationSignal())
    assert receipt.job_outcome.error is None
    assert receipt.job_outcome.result_type is OutcomeResultType.NEED_ATTACHMENT
    payload = receipt.job_outcome.payload
    assert payload.requested_input == []
    requirements = payload.state_delta.add_pending_requirements
    assert len(requirements) == 1
    assert requirements[0].name == "log_archive"
    assert payload.requested_attachments == [requirements[0].requirement_id]
    assert backend.calls == []
    assert not record_path.exists()
