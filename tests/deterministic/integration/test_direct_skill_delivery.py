"""Public website, REST and MCP delivery of the Skill's unmodified report."""
from __future__ import annotations

import hashlib
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.contracts import JobType, ReviewPolicy, is_specialized_direct
from problem_locator.entrypoints.settings import Settings
from problem_locator.interfaces.http_app import create_http_app
from problem_locator.runtime import diagnosis_runtime
from problem_locator.runtime.agent_backend import BackendExecution
from problem_locator.runtime.catalog import VersionedAssetCatalog
from tests.deterministic.integration import test_website_agent as website_helpers
from tests.deterministic.journey import test_rpc_timeout as rpc
from tests.deterministic.unit.interfaces.fakes import FakeStateAdmin
from tests.deterministic.unit.interfaces.helpers import readiness
from tests.deterministic.unit.interfaces.test_settings import environment


REPORT = (
    "\n# 定位结果\n\n"
    "根因是下游连接池耗尽。此处直接采用 Skill 的判断。  \n"
    "模型未提供日志引用、行号、identity_tokens 或方法编号。\n\n"
    "```text\n保留原样：中文、α、<tag> 与尾部空格  \n```\n\n"
)
UNRESOLVED_REPORT = (
    "# 本次分析\n\n"
    "Skill 判断当前资料不足，暂时无法确定原因。\n"
    "这是 Skill 自己给出的限制，请保留这段说明。\n\n"
)


class DirectSkillBackend:
    def __init__(self):
        self.status = "RESOLVED"
        self.report = REPORT
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["backend_phase"] == "METHODS_SPECIALIST"
        assert "<<<SKILL_DIAGNOSIS_RESULT_V1:RESOLVED>>>" in kwargs["prompt"]
        assert "Server evidence policy: advisory" not in kwargs["prompt"]
        sinks = kwargs["log_sinks"]
        for sink in {id(sinks.stdout): sinks.stdout, id(sinks.stderr): sinks.stderr}.values():
            sink.flush()
            sink.close()
        return BackendExecution(
            returncode=0, stdout_stderr_bytes=0, workspace_bytes=0,
            elapsed_seconds=0.01,
            final_result=f"<<<SKILL_DIAGNOSIS_RESULT_V1:{self.status}>>>\n{self.report}",
        )


@pytest.fixture
def direct_website(tmp_path, monkeypatch, request):
    settings = Settings.load(environ=environment(tmp_path))
    assert settings.methods_evidence_validation == "off"

    def catalog(**kwargs):
        # The old Reviewer flag is deliberately enabled by _Stack. The default
        # evidence policy must freeze a direct, no-review Job regardless.
        assert kwargs["specialized_reviewer_enabled"] is True
        return VersionedAssetCatalog(
            **kwargs, methods_evidence_validation=settings.methods_evidence_validation,
        )

    monkeypatch.setattr(rpc, "VersionedAssetCatalog", catalog)
    if getattr(request, "param", None) == "no-markers":
        monkeypatch.setattr(rpc, "RPC_CLIENT_LOG", "client input without diagnostic keywords\n")
        monkeypatch.setattr(rpc, "RPC_SERVER_LOG", "server input without diagnostic keywords\n")

    root = tmp_path.parent / ("ds-" + uuid.uuid4().hex[:8])
    root.mkdir()
    if os.name == "nt":
        root = rpc._windows_extended_path(root)
    released = root / "released"
    released.write_text("pass\n", encoding="utf-8")
    stack_args = dict(
        logparse_record=root / "logparse.json", agent_record=root / "agent.jsonl",
        review_entered=root / "entered", review_release=released,
        seed="direct-skill-delivery",
    )
    stack = rpc._Stack(root / "data", **stack_args)
    # _Stack's runtime is explicitly strict. Frozen Job bindings, rather than
    # a mutable runtime setting, decide whether this execution is direct.
    assert stack.runtime._methods_evidence_validation == "strict"
    backend = DirectSkillBackend()
    stack.runtime._diagnose_backend = backend
    scans = []
    scan = diagnosis_runtime.scan_method_markers

    def record_scan(**kwargs):
        receipt = scan(**kwargs)
        scans.append(receipt)
        return receipt

    monkeypatch.setattr(diagnosis_runtime, "scan_method_markers", record_scan)

    def forbidden_verification(*args, **kwargs):
        raise AssertionError("Skill 直出不得重新核验、筛选或改写模型结论。")

    for name in (
        "verify_method_diagnosis", "select_method_diagnosis",
        "accept_method_diagnosis_advisory", "map_verified_methods_draft",
    ):
        monkeypatch.setattr(diagnosis_runtime, name, forbidden_verification)
    finalize = diagnosis_runtime.finalize_server_outcome

    def forbid_direct_finalizer(*args, **kwargs):
        assert not is_specialized_direct(kwargs["job"]), "Skill 直出不得经过证据 finalizer。"
        return finalize(*args, **kwargs)

    monkeypatch.setattr(diagnosis_runtime, "finalize_server_outcome", forbid_direct_finalizer)
    store = AgentStore(stack.repository, stack.clock, stack.ids, runtime_epoch="direct-epoch")
    engine = website_helpers.ScriptedIntake()
    service = AgentConversationService(store, stack.application, engine, stack.layout)
    service.dispatcher = stack.scheduler
    stack.runtime._public_progress = store.append_case_progress
    app = create_http_app(
        command_port=stack.application, query_port=stack.application,
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://testserver", agent_service=service,
    )
    stack.start()
    with TestClient(app, headers={"X-Agent-Owner-Key": website_helpers.OWNER_KEY}) as client:
        yield SimpleNamespace(
            website=(stack, store, engine, service, client), backend=backend,
            scans=scans, root=root, stack_args=stack_args,
        )
    assert service.shutdown(2)
    stack.shutdown()


def _get(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["ok"] is True and envelope["error"] is None
    return envelope["data"]


def _finish(direct, facts=None):
    stack, store, _, service, client = direct.website
    conversation, prefix, _ = website_helpers._converse_to_waiting(direct.website)
    facts = rpc.PARAMETER_GROUP_A if facts is None else facts
    website_helpers._post(client, prefix + "/messages", {
        "request_id": "facts",
        "text": "\n".join(f"{name}={value}" for name, value in facts.items()),
    })
    assert service.run_once(conversation)
    service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(20)
    view = service.get_conversation(conversation)
    assert view.case_status == direct.backend.status, (
        view, stack.application.get_case(view.case_id).case_view.failure,
    )
    aggregate = stack.repository.read_case(view.case_id)
    assert all(job.job_type is not JobType.REVIEW for job in aggregate.jobs.values())
    diagnoses = [job for job in aggregate.jobs.values() if job.job_type is JobType.DIAGNOSE]
    assert diagnoses and all(is_specialized_direct(job) for job in diagnoses)
    assert all(job.review_policy is ReviewPolicy.NONE for job in diagnoses)
    assert all(job.output_contract_ref.id == "output-contract/skill-direct" for job in diagnoses)
    assert len(direct.backend.calls) == 1
    source_job_id = aggregate.case.generic_result_v2.source_job_id
    outcome = next(value for value in aggregate.outcomes.values() if value.job_id == source_job_id)
    assert outcome.decision_audit is None
    for name in (
        "method-grounding-audit.json", "method-evidence-selection.json",
        "method-evidence-advisory.json", "method-diagnosis.effective.json",
    ):
        assert stack.records.read_audit_bytes(source_job_id, name) is None
    events = store.list_events(conversation, limit=500)
    result = [event for event in events if event.type == "result.available"]
    assert len(result) == 1 and result[0].data["result_field"] == "generic_result_v2"
    assert not any(
        event.type == "agent.progress" and event.job_id == source_job_id
        and event.data["stage"] in {"VERIFYING", "REVIEWING"}
        for event in events
    )
    return view.case_id, prefix


def _assert_public_report(stack, client, case_id, prefix, body, status):
    expected = body.encode("utf-8")
    digest = hashlib.sha256(expected).hexdigest()
    rest = _get(client, f"/api/v1/cases/{case_id}")["case_view"]
    mcp = rpc._query(stack.mcp, case_id)
    result = rest["generic_result_v2"]
    assert mcp["generic_result_v2"] == result
    assert result["status"] == status
    assert result["report_markdown"] == body
    assert result["report_utf8_size"] == len(expected)
    assert result["report_sha256"] == digest
    native = _get(client, prefix + "?include=report")["result"]
    assert native["report_state"] == "READY" and native["format"] == "markdown"
    assert native["markdown"] == body and native["report"] is None
    artifact = native["artifact"]
    assert artifact["name"] == "skill-diagnosis-report.md"
    downloaded = client.get(
        f"/api/v1/artifacts/{artifact['artifact_id']}/content?case_id={case_id}"
    )
    assert downloaded.status_code == 200 and downloaded.content == expected
    assert int(downloaded.headers["content-length"]) == len(expected)
    assert artifact["sha256"] == digest
    assert artifact["artifact_id"] == result["report_artifact_id"]
    return result


@pytest.mark.parametrize("status,body", [("RESOLVED", REPORT), ("UNRESOLVED", UNRESOLVED_REPORT)])
def test_direct_skill_keeps_model_status_and_exact_report_across_public_surfaces(direct_website, status, body):
    direct = direct_website
    direct.backend.status, direct.backend.report = status, body
    case_id, prefix = _finish(direct)
    stack, _, _, _, client = direct.website
    _assert_public_report(stack, client, case_id, prefix, body, status)
    assert len(direct.scans) == 1
    assert direct.scans[0].loaded_method_ids == ("rpc-call-timeout",)
    assert '<<<METHODS_SKILL_FILE path="references/rpc-call-timeout.md">>>' in direct.backend.calls[0]["prompt"]


@pytest.mark.parametrize("direct_website", ["no-markers"], indirect=True)
def test_direct_skill_keeps_marker_card_selection_without_downgrading_its_final_answer(direct_website):
    direct = direct_website
    case_id, prefix = _finish(direct)
    stack, _, _, _, client = direct.website
    _assert_public_report(stack, client, case_id, prefix, REPORT, "RESOLVED")
    assert len(direct.scans) == 1
    assert direct.scans[0].marker_hits == direct.scans[0].loaded_method_ids == ()
    assert '<<<METHODS_SKILL_FILE path="references/rpc-call-timeout.md">>>' not in direct.backend.calls[0]["prompt"]


def test_direct_skill_with_all_targets_missing_still_delivers_its_report(direct_website, monkeypatch):
    direct = direct_website
    facts = {**rpc.PARAMETER_GROUP_A, "client_slot": "slot_99", "server_slot": "slot_98"}
    monkeypatch.setattr(website_helpers, "PARAMETER_GROUP_A", facts)
    case_id, prefix = _finish(direct, facts)
    stack, _, _, _, client = direct.website
    _assert_public_report(stack, client, case_id, prefix, REPORT, "RESOLVED")
    assert direct.scans == []
    assert '"target_logs":[]' in direct.backend.calls[0]["prompt"]


def test_direct_skill_report_is_durable_and_hash_bound_after_repository_restart(direct_website):
    direct = direct_website
    case_id, prefix = _finish(direct)
    stack, _, _, _, client = direct.website
    expected = _assert_public_report(stack, client, case_id, prefix, REPORT, "RESOLVED")
    # A new application/repository reads the persisted terminal snapshot and
    # report resource without an active scheduler or another model call.
    reopened = rpc._Stack(direct.root / "data", **direct.stack_args)
    try:
        with TestClient(reopened.http_app) as restored_client:
            restored = _get(restored_client, f"/api/v1/cases/{case_id}")["case_view"]
            assert restored["generic_result_v2"] == expected
            assert rpc._query(reopened.mcp, case_id)["generic_result_v2"] == expected
            report = reopened.application.queries.get_report(case_id)
            assert report.markdown == REPORT and report.format == "markdown"
            downloaded = restored_client.get(
                f"/api/v1/artifacts/{expected['report_artifact_id']}/content?case_id={case_id}"
            )
            assert downloaded.status_code == 200 and downloaded.content == REPORT.encode("utf-8")
            assert hashlib.sha256(downloaded.content).hexdigest() == expected["report_sha256"]
        assert len(direct.backend.calls) == 1
    finally:
        reopened.repository.close()
