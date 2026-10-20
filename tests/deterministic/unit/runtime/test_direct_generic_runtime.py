"""Direct Generic jobs use actual uploaded bytes without a Logparse runtime."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from problem_locator.contracts import (
    Attachment, AttachmentStatus, CancellationReason, CaseAggregate, ErrorCode,
    Job, OutcomeResultType, ResourceKind, ResourceRef,
)
from problem_locator.runtime.catalog import VersionedAssetCatalog
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime
from problem_locator.runtime.workspace import WorkspaceManager
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator, InMemoryCancellationSignal, InMemoryExecutionRecordStore,
    InMemoryResourceStore,
)
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    _Clock, _GenericRuntimeBackend, _StateView, _generic_aggregate,
    _generic_v2_result_bytes, _running_generic_job,
)


@pytest.fixture(autouse=True)
def restore_permissions(tmp_path):
    yield
    for path in tmp_path.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)


class ReadingBackend(_GenericRuntimeBackend):
    def __init__(self):
        super().__init__(None)
        self.observed = []

    def execute(self, **kwargs):
        root = Path(kwargs["workspace_root"])
        manifest = json.loads((root / "inputs/generic_logs.json").read_bytes())
        assert manifest["schema_version"] == 3
        assert not (root / "inputs/attachments").exists()
        for entry in manifest["logs"]:
            path = root / entry["log_path"]
            assert not path.stat().st_mode & 0o222
            # Read bounded ranges, including evidence beyond the first chunk.
            with path.open("rb") as source:
                while chunk := source.read(4096):
                    if b"unique-uploaded-evidence" in chunk:
                        self.observed.append((entry["relative_path"], chunk.decode("utf-8")))
        if manifest["logs"]:
            assert self.observed
            assert "unique-uploaded-evidence" not in kwargs["prompt"]
            report = "# 定位报告\n\n日志依据：" + str(self.observed)
        else:
            assert "still invoke the Skill" in kwargs["prompt"]
            report = "# 定位报告\n\n本次日志未参与分析，按问题描述继续排查。"
        self.v2_result_bytes = _generic_v2_result_bytes(report)
        return super().execute(**kwargs)


def direct_runtime(tmp_path, files, *, ready=True, backend=None):
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    # Intentionally no product parser, broker or product setting.
    catalog = VersionedAssetCatalog(skill_dir=skill_dir, generic_skill_name="generic-problem-locator-smoke")
    base_job = _running_generic_job(catalog)
    attachments, resources = [], InMemoryResourceStore()
    for ordinal, (name, content_type, content) in enumerate(files, start=1):
        aid = f"10000000-0000-0000-0000-{ordinal:012d}"
        digest = hashlib.sha256(content).hexdigest()
        key = f"resources/cases/{base_job.case_id}/attachments/{aid}/input.bin"
        attachments.append(Attachment(attachment_id=aid, case_id=base_job.case_id,
            status=AttachmentStatus.READY, name=name, content_type=content_type,
            declared_size=len(content), declared_sha256=digest, size=len(content),
            sha256=digest, storage_key=key, created_at="2026-07-31T00:00:00.000Z",
            updated_at="2026-07-31T00:00:00.000Z"))
        resources.seed_formal_resource(ResourceRef(resource_kind=ResourceKind.FILE, storage_key=key,
            size=len(content), sha256=digest), state_reference_count=1, payload=content)
    job = Job.model_validate({**base_job.model_dump(mode="python"),
        "attachment_refs": [item.attachment_id for item in attachments] if ready else [],
        "generic_log_archive_expected": True, "generic_supplement_texts": ["重启前有告警"]})
    aggregate = _generic_aggregate(base_job).model_dump(mode="python")
    aggregate["jobs"] = {job.job_id: job}
    aggregate["attachments"].update({item.attachment_id: item for item in attachments})
    aggregate["case"]["diagnosis_state"]["user_facts"] = []
    aggregate["case"]["diagnosis_state"]["pending_requirements"] = []
    state = _StateView(CaseAggregate.model_validate(aggregate))
    records, progress = InMemoryExecutionRecordStore(), []
    backend = backend or ReadingBackend()
    runtime = DiagnosisRuntime(state_repository=state, resource_store=resources, asset_catalog=catalog,
        logparse_broker_factory=None,
        execution_records=records, clock=_Clock(), id_generator=DeterministicIdGenerator(),
        workspace_manager=WorkspaceManager(tmp_path / "data"), backend=backend,
        public_progress=lambda *args: progress.append(args[-1]))
    return runtime, job, records, backend, progress


def archive(content):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as output:
        output.writestr("ordinary/app.txt", content)
    return stream.getvalue()


@pytest.mark.parametrize("kind", ["text", "zip", "mixed", "large"])
def test_direct_runtime_reads_upload_bytes_without_logparse_or_locator_parameters(tmp_path, kind):
    content = b"unique-uploaded-evidence: upstream-reset\n"
    if kind == "large":
        content = b"x" * (3 * 1024 * 1024) + content
    files = [("application.log", "text/plain", content)]
    if kind == "zip":
        files = [("ordinary.zip", "application/zip", archive(content))]
    if kind == "mixed":
        files += [("ordinary.zip", "application/zip", archive(content))]
    runtime, job, records, backend, progress = direct_runtime(tmp_path, files)
    assert job.logparse_tool_ref is None and job.logparse_product is None
    assert job.generic_log_preparation == "direct-v1" and job.context_snapshot is None
    receipt = runtime.execute(job, InMemoryCancellationSignal())
    assert receipt.job_outcome.error is None, receipt.job_outcome.error
    assert "unique-uploaded-evidence" in receipt.job_outcome.payload.report_markdown
    assert len(backend.calls) == 1
    assert progress[-2:] == ["GENERIC_LOG_PREPARE", "DIAGNOSING"]
    assert "LOGPARSE" not in progress
    manifest = json.loads(records.read_audit_bytes(job.job_id, "generic_logs.json"))
    assert len(manifest["logs"]) == len(files)
    assert len({item["source_attachment_id"] for item in manifest["logs"]}) == len(files)
    assert "重启前有告警" in backend.calls[0]["prompt"]


def test_direct_empty_logs_still_invoke_skill_with_explicit_text_fallback(tmp_path):
    runtime, job, records, backend, _ = direct_runtime(tmp_path, [
        ("binary.log", "text/plain", b"\x00\xff")])
    receipt = runtime.execute(job, InMemoryCancellationSignal())
    assert receipt.job_outcome.error is None, receipt.job_outcome.error
    assert "本次日志未参与分析" in receipt.job_outcome.payload.report_markdown
    assert len(backend.calls) == 1
    manifest = json.loads(records.read_audit_bytes(job.job_id, "generic_logs.json"))
    assert manifest["logs"] == [] and manifest["skipped"]


def test_direct_invalid_archive_never_silently_falls_back_to_text(tmp_path):
    runtime, job, _, backend, _ = direct_runtime(tmp_path, [
        ("broken.zip", "application/zip", b"not-a-zip")])
    receipt = runtime.execute(job, InMemoryCancellationSignal())
    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert receipt.job_outcome.error.code is ErrorCode.WORKSPACE_PREPARE_FAILED
    assert receipt.job_outcome.error.details[0].resource_type == "GENERIC_LOG_INPUT"
    assert backend.calls == []


def test_direct_preflight_admits_text_and_archives_without_required_facts(tmp_path):
    runtime, job, _, backend, _ = direct_runtime(tmp_path, [], ready=False)
    receipt = runtime.execute(job, InMemoryCancellationSignal())
    assert receipt.job_outcome.error is None, receipt.job_outcome.error
    assert receipt.job_outcome.result_type is OutcomeResultType.NEED_ATTACHMENT
    requirement = receipt.job_outcome.payload.state_delta.add_pending_requirements[0]
    assert "text/plain" in requirement.constraints.allowed_content_types
    assert "application/zip" in requirement.constraints.allowed_content_types
    assert receipt.job_outcome.payload.requested_input == []
    assert backend.calls == []


def test_direct_cancel_between_preparation_and_skill_prevents_execution(tmp_path, monkeypatch):
    runtime, job, _, backend, _ = direct_runtime(tmp_path, [
        ("app.log", "text/plain", b"unique-uploaded-evidence\n")])
    signal = InMemoryCancellationSignal()
    prepare = runtime._prepare_generic_logs
    def cancel_after(*args, **kwargs):
        result = prepare(*args, **kwargs)
        signal.cancel(CancellationReason.USER_CANCEL)
        return result
    monkeypatch.setattr(runtime, "_prepare_generic_logs", cancel_after)
    receipt = runtime.execute(job, signal)
    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert receipt.job_outcome.error.code is ErrorCode.BACKEND_CANCELLED
    assert backend.calls == []


def test_direct_sources_observe_cancellation_while_copying(tmp_path, monkeypatch):
    runtime, job, _, backend, _ = direct_runtime(tmp_path, [
        ("app.log", "text/plain", b"x" * 2_000_000)])
    signal = InMemoryCancellationSignal()
    original = runtime._resource_store.open_read
    def cancelled_read(reference):
        source = original(reference)
        read = source.read
        def cancel_on_read(size):
            result = read(size)
            signal.cancel(CancellationReason.USER_CANCEL)
            return result
        source.read = cancel_on_read
        return source
    monkeypatch.setattr(runtime._resource_store, "open_read", cancelled_read)
    receipt = runtime.execute(job, signal)
    assert receipt.job_outcome.error.code is ErrorCode.BACKEND_CANCELLED
    assert backend.calls == []
