from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from problem_locator.contracts import (
    Attachment, CaseAggregate, ErrorCode, Job, OutcomeResultType,
    ResourceKind, ResourceRef,
)
from problem_locator.runtime.agent_backend import BackendExecution
from tests.deterministic.contracts.fakes import DeterministicIdGenerator, InMemoryCancellationSignal
from tests.deterministic.unit.integrations.test_logparse_fake_e2e import pinned_asset
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    _public_fake_claiming_runtime,
)
from tests.deterministic.unit.runtime.test_generic_logs import _ReadingBackend, _runtime


SECOND_ARCHIVE_ID = "00000000-0000-4000-8000-000000000799"


@pytest.mark.parametrize("policy,maximum", [("strict", 1), ("advisory", 1), ("off", 20)])
def test_specialized_preflight_accepts_only_the_archive_count_its_runtime_supports(
    tmp_path: Path, policy: str, maximum: int,
) -> None:
    runtime, original, factory, backend, _resources = _public_fake_claiming_runtime(tmp_path, "success")
    catalog = runtime._asset_resolver._catalog
    catalog._methods_evidence_validation = policy
    runtime._methods_evidence_validation = policy
    runtime._id_generator = DeterministicIdGenerator(seed=f"archive-limit-{policy}")
    job = Job.model_validate({**original.model_dump(mode="json"),
        **catalog.diagnose_bindings(original.skill_ref).model_dump(mode="json"), "attachment_refs": []})
    aggregate = runtime._state_repository.aggregate.model_dump(mode="json")
    aggregate["attachments"] = {}
    aggregate["jobs"][job.job_id] = job.model_dump(mode="json")
    runtime._state_repository.aggregate = CaseAggregate.model_validate(aggregate)

    outcome = runtime.execute(job, InMemoryCancellationSignal()).job_outcome

    assert outcome.result_type is OutcomeResultType.NEED_ATTACHMENT, outcome.error
    requirement, = outcome.payload.state_delta.add_pending_requirements
    assert requirement.name == "log_archive"
    assert requirement.constraints.min_count == 1
    assert requirement.constraints.max_count == maximum
    assert backend.calls == [] and factory.sessions == []


@pytest.fixture(autouse=True)
def restore_test_permissions(tmp_path: Path):
    yield
    for path in tmp_path.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)


def _add_archive(runtime, job: Job, payload: bytes) -> Job:
    aggregate = runtime._state_repository.aggregate.model_dump(mode="json")
    original = aggregate["attachments"][job.attachment_refs[0]]
    key = f"resources/cases/{job.case_id}/attachments/{SECOND_ARCHIVE_ID}/{original['name']}"
    digest = hashlib.sha256(payload).hexdigest()
    attachment = Attachment.model_validate({**original, "attachment_id": SECOND_ARCHIVE_ID,
        "storage_key": key, "size": len(payload), "declared_size": len(payload),
        "sha256": digest, "declared_sha256": digest})
    job = Job.model_validate({**job.model_dump(mode="json"),
        "attachment_refs": [*job.attachment_refs, SECOND_ARCHIVE_ID]})
    aggregate["attachments"][SECOND_ARCHIVE_ID] = attachment.model_dump(mode="json")
    aggregate["jobs"][job.job_id] = job.model_dump(mode="json")
    runtime._state_repository.aggregate = CaseAggregate.model_validate(aggregate)
    runtime._resource_store.seed_formal_resource(ResourceRef(resource_kind=ResourceKind.FILE,
        storage_key=key, size=len(payload), sha256=digest), state_reference_count=1, payload=payload)
    return job


def test_generic_multi_archives_keep_identical_paths_and_independent_claims(tmp_path: Path, pinned_asset) -> None:
    backend = _ReadingBackend()
    runtime, job, records = _runtime(tmp_path, pinned_asset, backend)
    job = _add_archive(runtime, job, b"VALID second archive")

    outcome = runtime.execute(job, InMemoryCancellationSignal()).job_outcome

    assert outcome.result_type is OutcomeResultType.COMPLETED
    assert len(backend.calls) == 1
    main = Path(backend.calls[0]["workspace_root"])
    manifest = json.loads((main / "inputs/generic_logs.json").read_bytes())
    assert manifest["schema_version"] == 2
    assert [source["source_attachment_id"] for source in manifest["sources"]] == job.attachment_refs
    assert len({entry["log_path"] for entry in manifest["logs"]}) == len(manifest["logs"])
    by_archive = {attachment_id: [row for row in manifest["logs"]
        if row["source_attachment_id"] == attachment_id] for attachment_id in job.attachment_refs}
    assert {row["relative_path"] for row in by_archive[job.attachment_refs[0]]} == {
        row["relative_path"] for row in by_archive[SECOND_ARCHIVE_ID]}
    assert all(by_archive.values())
    for attachment_id in job.attachment_refs:
        preprocessing = main.with_name(f"{job.job_id}.logparse-preprocess.{attachment_id}")
        claim = json.loads((preprocessing / "runtime/tool-state/logparse-parse.claim").read_bytes())
        assert claim["attachment_id"] == attachment_id
        assert sorted(path.name for path in (preprocessing / "inputs/attachments").iterdir()) == [attachment_id]
        assert records.read_audit_bytes(job.job_id, f"logparse_broker_audit.{attachment_id}.json")
    assert not main.with_name(f"{job.job_id}.logparse-preprocess").exists()


def test_generic_second_archive_failure_never_calls_diagnosis(tmp_path: Path, pinned_asset) -> None:
    backend = _ReadingBackend()
    runtime, job, records = _runtime(tmp_path, pinned_asset, backend)
    job = _add_archive(runtime, job, b"NONZERO")

    outcome = runtime.execute(job, InMemoryCancellationSignal()).job_outcome

    assert outcome.result_type is OutcomeResultType.FAILED
    assert outcome.error.code is ErrorCode.LOGPARSE_FAILED
    assert backend.calls == []
    assert records.read_audit_bytes(job.job_id, f"logparse_broker_audit.{job.attachment_refs[0]}.json")
    assert records.read_audit_bytes(job.job_id, f"logparse_broker_audit.{SECOND_ARCHIVE_ID}.json")


class _DirectBackend:
    def __init__(self, factory):
        self.factory = factory
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        assert all(session.closed for session in self.factory.sessions)
        assert kwargs["broker_environment"] is None
        assert kwargs["backend_phase"] == "METHODS_SPECIALIST"
        sinks = kwargs["log_sinks"]
        for sink in {id(sinks.stdout): sinks.stdout, id(sinks.stderr): sinks.stderr}.values():
            sink.close()
        return BackendExecution(returncode=0, stdout_stderr_bytes=0, workspace_bytes=0,
            elapsed_seconds=0.01, final_result="<<<SKILL_DIAGNOSIS_RESULT_V1:RESOLVED>>>\n# 两包日志诊断\n完整报告。")


def _direct_runtime(tmp_path: Path, *, fail_second: bool = False, missing_second: bool = False):
    runtime, old_job, factory, parser, resources = _public_fake_claiming_runtime(tmp_path, "success")
    catalog = runtime._asset_resolver._catalog
    catalog._methods_evidence_validation = "off"
    runtime._methods_evidence_validation = "off"
    job = Job.model_validate({**old_job.model_dump(mode="json"),
        **catalog.diagnose_bindings(old_job.skill_ref).model_dump(mode="json")})
    job = _add_archive(runtime, job, b"second source with the same filename")
    backend = _DirectBackend(factory)
    runtime._diagnose_backend = backend

    def parse(session, operation, request_path, result_path):
        assert operation == "parse-targets"
        ordinal = len(factory.sessions)
        parser.target_contents = {"client": f"rpc deadline exceeded source={ordinal}\n".encode(),
            "server": f"connection pool wait source={ordinal}\n".encode()}
        parser.result = ("failed" if fail_second and ordinal == 2 else
            "confirmed_missing" if missing_second and ordinal == 2 else "success")
        parser._run_preprocessing({"workspace_root": session.workspace_root})

    factory.preprocessing_executor = parse
    return runtime, job, factory, backend


def test_direct_multi_archives_keep_same_role_logs_and_all_sources(tmp_path: Path) -> None:
    runtime, job, factory, backend = _direct_runtime(tmp_path)

    outcome = runtime.execute(job, InMemoryCancellationSignal()).job_outcome

    assert outcome.result_type is OutcomeResultType.COMPLETED, outcome.error
    assert len(backend.calls) == 1
    assert len(factory.sessions) == 2
    main = Path(backend.calls[0]["workspace_root"])
    manifest = json.loads((main / "inputs/target_logs.json").read_bytes())
    rows = manifest["target_logs"]
    assert [row["source_id"] for row in rows] == [
        "archive-001-client", "archive-001-server", "archive-002-client", "archive-002-server"]
    assert [row["label"] for row in rows] == ["client", "server", "client", "server"]
    for ordinal, attachment_id in enumerate(job.attachment_refs, start=1):
        selected = [row for row in rows if row["source_archive"]["attachment_id"] == attachment_id]
        assert len(selected) == 2
        assert all(f"source={ordinal}" in (main / row["log_path"]).read_text() for row in selected)
        session = factory.sessions[ordinal - 1]
        assert session.closed and session.close_calls == 1
        root = Path(session.workspace_root)
        assert root.name == f"{job.job_id}.logparse-preprocess.{attachment_id}"
        assert [path.name for path in (root / "inputs/attachments").iterdir()] == [attachment_id]
    request = json.loads((main / "inputs/request.json").read_bytes())
    assert [row["resource_id"] for row in request["consumed_artifacts"]] == job.attachment_refs
    receipt = json.loads((main / "inputs/logparse-receipt.json").read_bytes())
    assert receipt["operation"] == "parse-targets-batch"
    assert [row["attachment_id"] for row in receipt["sources"]] == job.attachment_refs
    assert not (main / "inputs/attachments").exists()


def test_direct_second_archive_failure_closes_every_session_without_diagnosis(tmp_path: Path) -> None:
    runtime, job, factory, backend = _direct_runtime(tmp_path, fail_second=True)

    outcome = runtime.execute(job, InMemoryCancellationSignal()).job_outcome

    assert outcome.result_type is OutcomeResultType.FAILED
    assert outcome.error.code is ErrorCode.LOGPARSE_FAILED
    assert backend.calls == []
    assert len(factory.sessions) == 2
    assert all(session.closed and session.close_calls == 1 for session in factory.sessions)


def test_direct_multi_archives_preserve_missing_targets_and_prior_report_context(tmp_path: Path) -> None:
    runtime, job, factory, backend = _direct_runtime(tmp_path, missing_second=True)
    parent_report = "# 上轮报告\n连接池等待是待验证的旧判断。"
    continuation = {"parent_run_id": "00000000-0000-4000-8000-000000000790",
        "parent_case_id": "00000000-0000-4000-8000-000000000791",
        "parent_report_artifact_id": "00000000-0000-4000-8000-000000000792",
        "parent_report_sha256": hashlib.sha256(parent_report.encode()).hexdigest(),
        "parent_report_markdown": parent_report, "change_note": "补充第二个节点的日志。",
        "goal": "重新核实连接池判断。"}
    job = Job.model_validate({**job.model_dump(mode="json"), "continuation_context": continuation})
    aggregate = runtime._state_repository.aggregate.model_dump(mode="json")
    aggregate["case"]["continuation_context"] = continuation
    aggregate["jobs"][job.job_id] = job.model_dump(mode="json")
    runtime._state_repository.aggregate = CaseAggregate.model_validate(aggregate)

    outcome = runtime.execute(job, InMemoryCancellationSignal()).job_outcome

    assert outcome.result_type is OutcomeResultType.COMPLETED, outcome.error
    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert "连接池等待是待验证的旧判断。" in call["prompt"]
    assert "## 本轮变化" in call["prompt"]
    assert "不能作为本轮已确认事实、日志证据或工具执行结果" in call["prompt"]
    main = Path(call["workspace_root"])
    receipt = json.loads((main / "inputs/logparse-receipt.json").read_bytes())
    assert receipt["sources"][0]["unresolved_targets"] == []
    assert receipt["sources"][1]["unresolved_targets"][0]["label"] == "client"
    assert receipt["sources"][1]["unresolved_targets"][0]["match_status"] == "missing"
    assert [row["source_id"] for row in receipt["target_logs"]] == [
        "archive-001-client", "archive-001-server", "archive-002-server"]
