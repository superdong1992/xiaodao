"""Document registrations run the real preparation and delivery pipeline."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import zipfile
from types import SimpleNamespace

import pytest

from problem_locator.contracts import (
    AssetKind, Attachment, AttachmentStatus, CancellationReason, CaseAggregate,
    Job, OutcomeResultType, ResolvedAsset, ResourceKind, ResourceRef, VersionedRef,
)
from problem_locator.runtime.agent_backend import BackendExecution
from problem_locator.runtime.catalog import VersionedAssetCatalog
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime
from problem_locator.runtime.workspace import WorkspaceManager
from tests.document_skill_helpers import document_input, document_registration, write_document_registration
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator, FakeLogparseBrokerFactory, InMemoryCancellationSignal,
    InMemoryExecutionRecordStore, InMemoryResourceStore,
)
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    LOGPARSE_ROOT, _Clock, _MethodsRuntimeBackend, _StateView, _json, _route_job,
)


@pytest.fixture(autouse=True)
def restore_permissions(tmp_path):
    yield
    for path in tmp_path.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)


class ReadingBackend:
    def __init__(self, after_read=None):
        self.calls, self.requests, self.manifests, self.contents = [], [], [], []
        self.after_read = after_read

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        root = Path(kwargs["workspace_root"])
        assert kwargs["file_access"] == "diagnose"
        assert kwargs["broker_environment"] is None
        assert not (root / "inputs/attachments").exists()
        assert not (root / "inputs/logparse-receipt.json").exists()
        assert "<<<SKILL_DIAGNOSIS_RESULT_V1:RESOLVED>>>" in kwargs["prompt"]
        assert "服务端已完成日志预处理" not in kwargs["prompt"]
        self.requests.append(json.loads((root / "inputs/request.json").read_bytes()))
        assert (root / "inputs/skill/SKILL.md").is_file()
        for document in self.requests[-1]["skill_documents"]:
            assert (root / document["path"]).is_file()
        manifest = json.loads((root / "inputs/evidence-manifest.json").read_bytes())
        self.manifests.append(manifest)
        for item in manifest["files"]:
            path = root / item["path"]
            assert not path.stat().st_mode & 0o222
            content = path.read_bytes()
            assert hashlib.sha256(content).hexdigest() == item["sha256"]
            self.contents.append(content)
        if self.after_read:
            self.after_read(root)
        for sink in {id(kwargs["log_sinks"].stdout): kwargs["log_sinks"].stdout,
                     id(kwargs["log_sinks"].stderr): kwargs["log_sinks"].stderr}.values():
            sink.flush()
            sink.close()
        return BackendExecution(returncode=0, stdout_stderr_bytes=0, workspace_bytes=0,
            elapsed_seconds=0.01,
            final_result="<<<SKILL_DIAGNOSIS_RESULT_V1:UNRESOLVED>>>\n# 定位报告\n保留业务 Skill 的限制。\n")


def _zip(content):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("original/app.log", content)
    return stream.getvalue()


def make_runtime(tmp_path, *, mode="none", facts=None, files=(), registration=None, backend=None):
    value = registration or document_registration(mode=mode)
    store = tmp_path / "skills"
    write_document_registration(store, registration=value)
    mode = value["runtime"]["preprocessing"]["mode"]
    broker = FakeLogparseBrokerFactory()
    kwargs = {}
    if mode == "logparse":
        kwargs.update(logparse_broker_factory=broker, logparse_tool=ResolvedAsset(
            ref=VersionedRef(id="logparse-tool/fake", version="3.4.5", content_hash="f" * 64),
            asset_kind=AssetKind.LOGPARSE_TOOL, root_path=str(LOGPARSE_ROOT)))
    catalog = VersionedAssetCatalog(skill_dir=store, generic_skill_name="generic-problem-locator-smoke",
        methods_evidence_validation="strict", specialized_reviewer_enabled=True, **kwargs)
    skill_ref = catalog.route_bindings().available_skill_refs[0]
    base = _route_job().model_dump(mode="json")
    base.update(catalog.diagnose_bindings(skill_ref).model_dump(mode="json"))
    base.update(job_type="DIAGNOSE", status="RUNNING", started_at="2026-07-31T00:00:01.000Z",
        runtime_epoch="00000000-0000-4000-8000-000000000498")
    user_facts = [{"item_id": f"20000000-0000-0000-0000-{index:012d}", "statement": val,
        "status": "ACTIVE", "provenance": {"source_type": "USER_INPUT",
            "source_ref": base["case_id"], "input_name": name}, "evidence_refs": [],
        "created_revision": 1, "supersedes": []} for index, (name, val) in enumerate((facts or {}).items(), 1)]
    base["context_snapshot"]["user_facts"] = user_facts
    attachments, resources = {}, InMemoryResourceStore()
    for index, (name, ctype, content) in enumerate(files, 1):
        aid = f"10000000-0000-0000-0000-{index:012d}"
        digest = hashlib.sha256(content).hexdigest()
        key = f"resources/cases/{base['case_id']}/attachments/{aid}/payload.bin"
        attachment = Attachment(attachment_id=aid, case_id=base["case_id"], status=AttachmentStatus.READY,
            name=name, content_type=ctype, declared_size=len(content), declared_sha256=digest,
            size=len(content), sha256=digest, storage_key=key,
            created_at="2026-07-31T00:00:00.000Z", updated_at="2026-07-31T00:00:00.000Z")
        attachments[aid] = attachment
        resources.seed_formal_resource(ResourceRef(resource_kind=ResourceKind.FILE, storage_key=key,
            size=len(content), sha256=digest), state_reference_count=1, payload=content)
    base["attachment_refs"] = list(attachments)
    job = Job.model_validate(base)
    aggregate = next(iter(_json("state.json")["cases"].values()))
    aggregate["case"].update(selected_skill_ref=skill_ref.model_dump(mode="json"))
    aggregate["case"]["diagnosis_state"]["user_facts"] = user_facts
    aggregate.update(jobs={job.job_id: job}, attachments=attachments)
    state = _StateView(CaseAggregate.model_validate(aggregate))
    backend = backend or ReadingBackend()
    records, progress = InMemoryExecutionRecordStore(), []
    runtime = DiagnosisRuntime(state_repository=state, resource_store=resources, asset_catalog=catalog,
        logparse_broker_factory=broker, execution_records=records, clock=_Clock(),
        id_generator=DeterministicIdGenerator(), workspace_manager=WorkspaceManager(tmp_path / "data"),
        backend=backend, methods_evidence_validation="strict",
        public_progress=lambda *args: progress.append(args[-1]))
    return runtime, job, backend, broker, records, progress


@pytest.mark.parametrize("mode", ["none", "direct_files"])
def test_empty_or_optional_materials_do_not_require_logparse(tmp_path, mode):
    value = document_registration(mode=mode, attachment_required=False,
        user_inputs=[document_input("region", required=False)])
    runtime, job, backend, broker, _, progress = make_runtime(tmp_path, registration=value)
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.COMPLETED, result.error
    assert result.payload.report_markdown == "# 定位报告\n保留业务 Skill 的限制。\n"
    assert backend.requests[0]["user_inputs"] == {}
    assert backend.manifests[0]["files"] == []
    assert broker.sessions == [] and "LOGPARSE" not in progress
    assert job.review_policy.value == "NONE" and job.logparse_tool_ref is None


@pytest.mark.parametrize("kind", ["text", "json", "zip", "mixed", "large"])
def test_direct_files_read_original_bytes_and_origins(tmp_path, kind):
    content = b'{"evidence":"original-upload"}\n'
    if kind == "large":
        content = b"x" * (2 * 1024 * 1024) + content
    files = [("app.json", "application/json", content)] if kind == "json" else [("app.log", "text/plain", content)]
    if kind == "zip":
        files = [("bundle.zip", "application/zip", _zip(content))]
    elif kind == "mixed":
        files.append(("bundle.zip", "application/zip", _zip(content)))
    runtime, job, backend, broker, records, _ = make_runtime(tmp_path, mode="direct_files", files=files)
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.COMPLETED, result.error
    assert backend.contents == [content] * len(files)
    assert len(backend.manifests[0]["sources"]) == len(files)
    assert broker.sessions == []
    assert json.loads(records.read_audit_bytes(job.job_id, "document-evidence-manifest.json")) == backend.manifests[0]


def test_required_inputs_and_materials_have_declared_prompts_only(tmp_path):
    value = document_registration(mode="direct_files", user_inputs=[
        document_input("service", prompt="请提供服务名称。"), document_input("region", required=False)])
    runtime, job, backend, broker, _, _ = make_runtime(tmp_path, registration=value)
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.NEED_INPUT, result.error
    requirements = result.payload.state_delta.add_pending_requirements
    assert [item.name for item in requirements] == ["service", "materials"]
    assert requirements[0].prompt == "请提供服务名称。"
    assert "文本（.log/.txt）" in requirements[1].prompt
    assert "文件数量：1～20 份。" in requirements[1].prompt
    assert backend.calls == [] and broker.sessions == []


@pytest.mark.parametrize("facts", [{"unknown": "x"}, {"region": "bad"}])
def test_initial_facts_cannot_bypass_declared_input_constraints(tmp_path, facts):
    value = document_registration(user_inputs=[document_input("region", required=False, allowed_values=["cn"])])
    runtime, job, backend, _, _, _ = make_runtime(tmp_path, registration=value, facts=facts)
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.FAILED
    assert "参数" in result.error.message and backend.calls == []


def test_optional_supplied_fact_is_kept_and_not_requested(tmp_path):
    value = document_registration(user_inputs=[document_input("region", required=False)])
    runtime, job, backend, _, _, _ = make_runtime(tmp_path, registration=value, facts={"region": "cn"})
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.COMPLETED, result.error
    assert backend.requests[0]["user_inputs"] == {"region": "cn"}


def test_none_never_reads_previously_attached_materials(tmp_path):
    runtime, job, backend, broker, _, _ = make_runtime(tmp_path,
        files=[("ignored.log", "text/plain", b"must-not-be-read")])
    runtime._resource_store.open_read = lambda *_: pytest.fail("none 模式不应读取附件")
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.COMPLETED, result.error
    assert backend.contents == [] and broker.sessions == []


@pytest.mark.parametrize("content", [b"not a zip", b"PK\x03\x04incomplete"])
def test_broken_archive_is_not_silently_treated_as_text(tmp_path, content):
    runtime, job, backend, _, _, _ = make_runtime(tmp_path, mode="direct_files",
        files=[("broken.zip", "application/zip", content)])
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.FAILED and not backend.calls


def test_unreadable_material_is_disclosed_and_does_not_claim_analysis(tmp_path):
    runtime, job, backend, _, _, _ = make_runtime(tmp_path, mode="direct_files",
        files=[("binary.log", "text/plain", b"\x00\xff")])
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.COMPLETED, result.error
    assert backend.manifests[0]["skipped"] and backend.manifests[0]["limitations"]
    assert backend.manifests[0]["files"] == []


def test_cancel_after_preparation_prevents_model_call(tmp_path, monkeypatch):
    runtime, job, backend, _, _, _ = make_runtime(tmp_path)
    signal = InMemoryCancellationSignal()
    original = runtime._materialize_context
    def cancelled(*args):
        context = original(*args)
        signal.cancel(CancellationReason.USER_CANCEL)
        return context
    monkeypatch.setattr(runtime, "_materialize_context", cancelled)
    result = runtime.execute(job, signal).job_outcome
    assert result.result_type == OutcomeResultType.FAILED and not backend.calls


def test_modified_prepared_material_cannot_be_delivered(tmp_path):
    def tamper(root):
        path = root / "inputs/evidence/log-000001.log"
        path.chmod(0o644)
        path.write_bytes(b"modified")
    runtime, job, _, _, _, _ = make_runtime(tmp_path, mode="direct_files", backend=ReadingBackend(tamper),
        files=[("app.log", "text/plain", b"original")])
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.FAILED and "不一致" in result.error.message


def test_required_request_bytes_count_toward_context_budget(tmp_path):
    value = document_registration()
    value["inputs"]["fixed"] = [{"name": f"fixed_{index}", "value": "x" * 65_000} for index in range(5)]
    runtime, job, backend, _, _, _ = make_runtime(tmp_path, registration=value)
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.FAILED
    assert result.error.code.value == "CONTEXT_LIMIT" and not backend.calls


def test_oversized_entry_prompt_still_fails_context_limit(tmp_path, monkeypatch):
    original = write_document_registration
    def with_large_entry(*args, **kwargs):
        root = original(*args, **kwargs)
        entry = root / "package/document-triage/SKILL.md"
        entry.write_bytes(entry.read_bytes() + ("入口正文不得截断。\n" * 20_000).encode("utf-8"))
        return root
    monkeypatch.setattr(f"{__name__}.write_document_registration", with_large_entry)
    runtime, job, backend, _, _, _ = make_runtime(tmp_path)
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.FAILED
    assert result.error.code.value == "CONTEXT_LIMIT" and not backend.calls


def test_backend_receives_remaining_task_time_after_preparation(tmp_path, monkeypatch):
    from problem_locator.runtime import document_runtime
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(document_runtime, "time", SimpleNamespace(monotonic=lambda: clock.value))
    original = document_runtime._freeze_inputs
    def prepared(*args):
        result = original(*args)
        clock.value = 20.0
        return result
    monkeypatch.setattr(document_runtime, "_freeze_inputs", prepared)
    runtime, job, backend, _, _, _ = make_runtime(tmp_path)
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.COMPLETED, result.error
    assert backend.calls[0]["test_limits"].wall_time_seconds == job.resource_limits.wall_time_seconds - 20


def test_long_reference_is_available_without_inlining_all_documents(tmp_path, monkeypatch):
    original = write_document_registration
    def with_long_reference(*args, **kwargs):
        root = original(*args, **kwargs)
        (root / "package/document-triage/references/diagnosis.md").write_text(
            "# 参考资料\n" + "诊断细节\n" * 30_000, encoding="utf-8")
        return root
    monkeypatch.setattr(f"{__name__}.write_document_registration", with_long_reference)
    runtime, job, backend, _, _, _ = make_runtime(tmp_path)
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.COMPLETED, result.error
    assert "诊断细节" not in backend.calls[0]["prompt"]
    documents = backend.requests[0]["skill_documents"]
    assert documents[1]["size"] > job.resource_limits.context_bytes


def _write_references(monkeypatch, documents):
    original = write_document_registration
    def with_references(*args, **kwargs):
        root = original(*args, **kwargs)
        for name, content in documents.items():
            (root / "package/document-triage/references" / name).write_bytes(content)
        return root
    monkeypatch.setattr(f"{__name__}.write_document_registration", with_references)


@pytest.mark.parametrize("mode", ["none", "direct_files", "logparse"])
def test_all_references_are_inlined_once_with_original_bytes_in_every_preparation_mode(tmp_path, monkeypatch, mode):
    documents = {
        "z-last.md": "# 最后一个参考\r\n\r\n```text\r\n  保留缩进和空白  \t\r\n```\r\n\r\n".encode("utf-8"),
        "diagnosis.md": "# 特有诊断步骤\n\n逐字保留中文与 emoji：🧭\n\t尾行  \n\n".encode("utf-8"),
        "a-first.md": b"# Unique first reference\n\n    indented code\n\n",
    }
    _write_references(monkeypatch, documents)
    value = document_registration(mode=mode)
    facts, files = {}, []
    if mode == "direct_files":
        files = [("app.log", "text/plain", b"uploaded-evidence-only\n")]
    elif mode == "logparse":
        prep = value["runtime"]["preprocessing"]
        prep["roles"][0]["label"] = prep["logparse_plan"]["anchors"][0]["label"] = "client"
        facts = {"problem_time": "2026-07-31T00:00:00.000Z", "process_name": "client"}
        files = [("logs.zip", "application/zip", _zip(b"source"))]
    runtime, job, backend, broker, records, _ = make_runtime(
        tmp_path, registration=value, facts=facts, files=files)
    if mode == "logparse":
        producer = _MethodsRuntimeBackend(broker, job, "confirmed")
        broker.preprocessing_executor = lambda session, *_: (
            producer._run_preprocessing({"workspace_root": session.workspace_root}), None)[1]
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.COMPLETED, result.error
    assert len(backend.calls) == 1
    prompt = backend.calls[0]["prompt"]
    assert records.read_audit_bytes(job.job_id, "document-prompt.txt") == prompt.encode("utf-8")
    positions = []
    for name in sorted(documents):
        content = documents[name]
        marker = f'<<<DOCUMENT_SKILL_FILE path="references/{name}">>>'
        assert prompt.count(marker) == 1
        assert prompt.count(content.decode("utf-8")) == 1
        positions.append(prompt.index(marker))
        document = next(item for item in backend.requests[0]["skill_documents"]
                        if item["path"] == f"inputs/skill/references/{name}")
        assert document["size"] == len(content)
        assert document["sha256"] == hashlib.sha256(content).hexdigest()
    assert positions == sorted(positions)
    assert prompt.count("# 服务故障定位") == 1
    assert '<<<DOCUMENT_SKILL_FILE path="SKILL.md">>>' not in prompt
    evidence = json.dumps(backend.manifests[0], ensure_ascii=False)
    assert "references/" not in evidence and "inputs/skill/" not in evidence
    assert all(content not in backend.contents for content in documents.values())


@pytest.mark.parametrize("budget_source", ["inline-limit", "request-reservation", "job-context"])
def test_reference_group_falls_back_without_partial_inlining_when_total_budget_is_insufficient(
        tmp_path, monkeypatch, budget_source):
    from problem_locator.runtime.final_response import INLINE_INPUT_BYTES
    reference_size = {"inline-limit": INLINE_INPUT_BYTES - 1024,
                      "request-reservation": 65_000, "job-context": 48_000}[budget_source]
    documents = {
        "diagnosis.md": b"# Large reference unique prefix\n" + b"x" * reference_size + b"\nunique ending\n",
        "a-small.md": b"# Small reference must fall back together\n",
    }
    _write_references(monkeypatch, documents)
    value = document_registration()
    if budget_source == "request-reservation":
        value["inputs"]["fixed"] = [{"name": "large_registered_value", "value": "v" * 65_000}]
    if budget_source == "job-context":
        from problem_locator.contracts import limits
        monkeypatch.setattr(limits, "DIAGNOSE_CONTEXT_BYTES", 32_000)
    runtime, job, backend, _, _, _ = make_runtime(tmp_path, registration=value)
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.COMPLETED, result.error
    prompt = backend.calls[0]["prompt"]
    assert "<<<DOCUMENT_SKILL_FILE" not in prompt
    assert "Large reference unique prefix" not in prompt
    assert "unique ending" not in prompt
    assert "Small reference must fall back together" not in prompt
    root = Path(backend.calls[0]["workspace_root"])
    for name, content in documents.items():
        assert (root / "inputs/skill/references" / name).read_bytes() == content
    reserved = sum((root / "inputs" / name).stat().st_size
                   for name in ("request.json", "evidence-manifest.json"))
    assert len(prompt.encode("utf-8")) + reserved <= job.resource_limits.context_bytes
    reference_bytes = sum(map(len, documents.values()))
    assert reference_bytes < INLINE_INPUT_BYTES
    assert len(prompt.encode("utf-8")) + reserved + reference_bytes > min(
        INLINE_INPUT_BYTES, job.resource_limits.context_bytes)
    if budget_source == "request-reservation":
        assert len(prompt.encode("utf-8")) + reference_bytes < INLINE_INPUT_BYTES


def test_modified_inlined_reference_cannot_be_delivered(tmp_path, monkeypatch):
    original = b"# Reference included before backend execution\n"
    _write_references(monkeypatch, {"diagnosis.md": original})
    def tamper(root):
        path = root / "inputs/skill/references/diagnosis.md"
        path.chmod(0o644)
        path.write_bytes(b"# Modified reference\n")
    runtime, job, backend, _, _, _ = make_runtime(tmp_path, backend=ReadingBackend(tamper))
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert original.decode("utf-8") in backend.calls[0]["prompt"]
    assert result.result_type == OutcomeResultType.FAILED
    assert "不一致" in result.error.message


@pytest.mark.parametrize("budget_source", ["inline-limit", "job-context"])
@pytest.mark.parametrize("budget_delta", [0, -1])
def test_reference_inlining_uses_exact_complete_utf8_budget(tmp_path, monkeypatch, budget_source, budget_delta):
    from problem_locator.contracts import limits
    from problem_locator.runtime import document_runtime
    documents = {
        "diagnosis.md": ("# 精确预算边界\r\n\r\n" + "中文原文保留。\n" * 200).encode("utf-8"),
        "unlinked.md": "# 未被入口链接的参考\n\n```text\n  原始文本  \n```\n".encode("utf-8"),
    }
    _write_references(monkeypatch, documents)
    probe_runtime, probe_job, probe_backend, _, _, _ = make_runtime(tmp_path / "probe0")
    probe_result = probe_runtime.execute(probe_job, InMemoryCancellationSignal()).job_outcome
    assert probe_result.result_type == OutcomeResultType.COMPLETED, probe_result.error
    probe_root = Path(probe_backend.calls[0]["workspace_root"])
    complete_bytes = len(probe_backend.calls[0]["prompt"].encode("utf-8")) + sum(
        (probe_root / "inputs" / name).stat().st_size
        for name in ("request.json", "evidence-manifest.json"))
    assert all(content.decode("utf-8") in probe_backend.calls[0]["prompt"] for content in documents.values())
    if budget_source == "inline-limit":
        monkeypatch.setattr(document_runtime, "INLINE_INPUT_BYTES", complete_bytes + budget_delta)
    else:
        monkeypatch.setattr(limits, "DIAGNOSE_CONTEXT_BYTES", complete_bytes + budget_delta)
    runtime, job, backend, _, _, _ = make_runtime(tmp_path / "actual")
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.COMPLETED, result.error
    prompt = backend.calls[0]["prompt"]
    for name, content in documents.items():
        marker = f'<<<DOCUMENT_SKILL_FILE path="references/{name}">>>'
        assert (marker in prompt) is (budget_delta == 0)
        assert (content.decode("utf-8") in prompt) is (budget_delta == 0)


def test_empty_reference_directory_does_not_require_reference_reads(tmp_path, monkeypatch):
    original = write_document_registration
    def without_references(*args, **kwargs):
        root = original(*args, **kwargs)
        package = root / "package/document-triage"
        entry = package / "SKILL.md"
        entry.write_bytes(entry.read_bytes().replace(b"references/diagnosis.md", b"SKILL.md"))
        (package / "references/diagnosis.md").unlink()
        return root
    monkeypatch.setattr(f"{__name__}.write_document_registration", without_references)
    runtime, job, backend, _, _, _ = make_runtime(tmp_path)
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.COMPLETED, result.error
    assert [item["path"] for item in backend.requests[0]["skill_documents"]] == ["inputs/skill/SKILL.md"]
    assert "<<<DOCUMENT_SKILL_FILE" not in backend.calls[0]["prompt"]
    assert backend.calls[0]["prompt"].count("# 服务故障定位") == 1
    assert backend.manifests[0]["files"] == []


def test_replaced_deployment_directory_keeps_frozen_reference_text(tmp_path, monkeypatch):
    original = "# 固定版本的参考\r\n\r\n本次诊断只使用该原文。  \r\n".encode("utf-8")
    _write_references(monkeypatch, {"diagnosis.md": original})
    runtime, job, backend, _, _, _ = make_runtime(tmp_path)
    package = tmp_path / "skills/document-triage/package/document-triage"
    package.rename(tmp_path / "previous-deployment")
    (package / "references").mkdir(parents=True)
    (package / "SKILL.md").write_bytes(b"# Replaced deployment entry\n")
    (package / "references/diagnosis.md").write_bytes(b"# Replaced deployment reference\n")
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.COMPLETED, result.error
    prompt = backend.calls[0]["prompt"]
    assert prompt.count(original.decode("utf-8")) == 1
    assert "Replaced deployment" not in prompt
    root = Path(backend.calls[0]["workspace_root"])
    assert (root / "inputs/skill/references/diagnosis.md").read_bytes() == original


@pytest.mark.parametrize("roles", ["single", "dual", "optional-inactive", "optional-active"])
def test_logparse_uses_declared_roles_and_only_one_model_call(tmp_path, roles):
    value = document_registration(mode="logparse")
    prep = value["runtime"]["preprocessing"]
    prep["roles"][0]["label"] = prep["logparse_plan"]["anchors"][0]["label"] = "client"
    facts = {"problem_time": "2026-07-31T00:00:00.000Z", "process_name": "client"}
    if roles != "single":
        optional = roles.startswith("optional")
        value["inputs"]["user"].append(document_input("server_process", required=not optional))
        prep["roles"].append({"label": "server", "description": "服务端", "presence": "OPTIONAL" if optional else "REQUIRED",
                              "source_reference": "SKILL.md"})
        prep["logparse_plan"]["anchors"].append({"label": "server", "module": {"source": "SKILL_FIXED", "value": "other"},
            "slot": {"source": "SKILL_FIXED", "value": "1"}, "process_name": {"source": "USER_FACT", "name": "server_process"}, "pid": None})
        if roles != "optional-inactive":
            facts["server_process"] = "server"
    runtime, job, backend, broker, _, progress = make_runtime(tmp_path, registration=value, facts=facts,
        files=[("logs.zip", "application/zip", _zip(b"source"))])
    producer = _MethodsRuntimeBackend(broker, job, "confirmed")
    broker.preprocessing_executor = lambda session, *_: (producer._run_preprocessing({"workspace_root": session.workspace_root}), None)[1]
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type == OutcomeResultType.COMPLETED, result.error
    expected = 1 if roles in ("single", "optional-inactive") else 2
    assert len(backend.manifests[0]["files"]) == expected
    assert len(backend.calls) == 1 and len(broker.sessions) == 1
    assert broker.sessions[0].closed and "LOGPARSE" in progress
    assert backend.manifests[0]["logparse_receipts"]
