"""Web messages traverse v3 routing, intake, real runtime preparation and report delivery."""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from problem_locator.agent.intake import IntakeDecision, IntakeValue
from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.contracts import JobType, MarkInitialLogArchiveExpected
from problem_locator.interfaces.http_app import create_http_app
from problem_locator.runtime.agent_backend import BackendExecution
from tests.document_skill_helpers import document_input, document_registration, write_document_registration
from tests.route_helpers import route_response_for_prompt
from tests.deterministic.integration.test_direct_skill_delivery import _assert_public_report
from tests.deterministic.integration.test_website_agent import OWNER_KEY, _post
from tests.deterministic.journey import test_rpc_timeout as rpc
from tests.deterministic.unit.interfaces.fakes import FakeStateAdmin
from tests.deterministic.unit.interfaces.helpers import readiness


class GroundedIntake:
    def __init__(self):
        self.calls = []

    def intake(self, request):
        self.calls.append(request)
        values = []
        for definition in request.requirements + request.optional_inputs:
            if definition.kind != "INPUT":
                continue
            prefix = definition.name + "="
            for message in reversed(request.messages):
                if message.role != "USER":
                    continue
                line = next((line for line in message.text.splitlines() if line.startswith(prefix)), None)
                if line:
                    values.append(IntakeValue(name=definition.name, value=line[len(prefix):],
                        source_message_id=message.message_id, source_quote=line))
                    break
        return IntakeDecision(action="SUBMIT_SUPPLEMENT" if values else "NEED_CLARIFICATION",
            message="已核对当前参数。", problem_fields=[], user_facts=values)


class ReadingDocumentBackend:
    """Stub only model reasoning; consume the real sealed execution files."""
    def __init__(self, skill_ref):
        self.skill_ref, self.routes, self.requests, self.contents = skill_ref, [], [], []
        self.report = None

    def execute(self, **kwargs):
        if kwargs["backend_phase"] == "ROUTE":
            self.routes.append(kwargs["prompt"])
            output = json.dumps(route_response_for_prompt(kwargs["prompt"], self.skill_ref.id), ensure_ascii=False)
        else:
            root = Path(kwargs["workspace_root"])
            assert kwargs["file_access"] == "read-only"
            assert kwargs["broker_environment"] is None
            request = json.loads((root / "inputs/request.json").read_bytes())
            manifest = json.loads((root / "inputs/evidence-manifest.json").read_bytes())
            self.requests.append(request)
            for item in manifest["files"]:
                path = root / item["path"]
                assert not path.stat().st_mode & 0o222
                content = path.read_bytes()
                assert hashlib.sha256(content).hexdigest() == item["sha256"]
                self.contents.append(content)
            values = request["user_inputs"]
            self.report = f"# 定位报告\n\n服务：{values.get('service', '未声明')}。地区：{values.get('region', '未声明')}。\n"
            if self.contents:
                evidence = json.loads(self.contents[0])
                self.report += f"材料记录：{evidence['state']}。\n"
            else:
                self.report += "本方法只使用用户参数。\n"
            output = "<<<SKILL_DIAGNOSIS_RESULT_V1:RESOLVED>>>\n" + self.report
        sinks = kwargs["log_sinks"]
        for sink in {id(sinks.stdout): sinks.stdout, id(sinks.stderr): sinks.stderr}.values():
            sink.flush()
            sink.close()
        return BackendExecution(returncode=0, stdout_stderr_bytes=0, workspace_bytes=0,
            elapsed_seconds=0.01, final_result=output)


@pytest.mark.parametrize("mode,optional_materials_only,late_optional_materials", [
    pytest.param("none", False, False, id="none"),
    pytest.param("direct_files", False, False, id="direct_files"),
    pytest.param("direct_files", True, False, id="optional_materials_without_inputs"),
    pytest.param("direct_files", False, "together", id="optional_materials_while_waiting"),
    pytest.param("direct_files", False, "separate", id="optional_materials_before_parameters"),
    pytest.param("direct_files", False, "invalid_type", id="optional_materials_invalid_type_keeps_parameters"),
    pytest.param("direct_files", False, "route_waiting", id="optional_materials_route_marker_race_waiting"),
    pytest.param("direct_files", False, "route_running", id="optional_materials_route_marker_race_running"),
])
def test_web_required_and_optional_inputs_reach_real_document_runtime(tmp_path, monkeypatch, mode, optional_materials_only, late_optional_materials):
    skills = tmp_path / "skills"
    user_inputs = [] if optional_materials_only else [
        document_input("service", prompt="请提供服务名称。"),
        document_input("region", required=False, prompt="地区（选填）", allowed_values=["cn", "eu"]),
    ]
    registration = document_registration(mode=mode, user_inputs=user_inputs,
        attachment_required=not (optional_materials_only or late_optional_materials))
    if late_optional_materials == "invalid_type":
        registration["attachment"]["allowed_content_types"] = ["application/json"]
    write_document_registration(skills, registration=registration)
    monkeypatch.setattr(rpc, "SKILL_DIR", skills)
    released = tmp_path / "released"
    released.write_text("pass\n", encoding="utf-8")
    stack = rpc._Stack(tmp_path / "data", logparse_record=tmp_path / "logparse.json",
        agent_record=tmp_path / "agent.jsonl", review_entered=tmp_path / "entered",
        review_release=released, seed="document-web-" + mode)
    backend = ReadingDocumentBackend(stack.catalog.route_bindings().available_skill_refs[0])
    stack.runtime._route_backend = backend
    stack.runtime._diagnose_backend = backend
    store = AgentStore(stack.repository, stack.clock, stack.ids, runtime_epoch="document-web-epoch")
    engine = GroundedIntake()
    service = AgentConversationService(store, stack.application, engine, stack.layout)
    service.dispatcher = stack.scheduler
    stack.runtime._public_progress = store.append_case_progress
    app = create_http_app(command_port=stack.application, query_port=stack.application,
        state_admin=FakeStateAdmin(readiness=readiness()), public_base_url="http://testserver", agent_service=service)
    racing = late_optional_materials in {"route_waiting", "route_running"}
    route_entered, route_release = threading.Event(), threading.Event()
    specialized_entered, specialized_release = threading.Event(), threading.Event()
    if racing:
        original_runtime, original_execute = stack.runtime.execute, type(stack.application).execute

        def hold_runtime(job, cancellation):
            if job.job_type is JobType.ROUTE:
                route_entered.set()
                assert route_release.wait(15)
            else:
                specialized_entered.set()
                assert specialized_release.wait(15)
            return original_runtime(job, cancellation)

        def route_wins(application, command):
            if isinstance(command, MarkInitialLogArchiveExpected) and not route_release.is_set():
                route_release.set()
                assert specialized_entered.wait(15)
                if late_optional_materials == "route_waiting":
                    specialized_release.set()
                    assert stack.scheduler.wait_until_idle(15)
            return original_execute(application, command)

        monkeypatch.setattr(stack.runtime, "execute", hold_runtime)
        monkeypatch.setattr(type(stack.application), "execute", route_wins)
    stack.start()
    try:
        with TestClient(app, headers={"X-Agent-Owner-Key": OWNER_KEY}) as client:
            conversation = _post(client, "/api/v1/agent/conversations", {"request_id": "create"})["conversation_id"]
            prefix = f"/api/v1/agent/conversations/{conversation}"
            content = b'{"state":"connection-limit"}\n'
            def upload_material(request_id="upload", invalid=False):
                prepared = _post(client, "/api/v1/agent/attachments", {"conversation_id": conversation,
                    "request_id": request_id, "name": "state.txt" if invalid else "state.json",
                    "content_type": "text/plain" if invalid else "application/json",
                    "declared_size": len(content), "declared_sha256": hashlib.sha256(content).hexdigest()})
                upload = prepared["upload"]
                headers = {key: value for key, value in upload["required_headers"].items() if value is not None}
                assert client.put(upload["url"], content=content, headers=headers).status_code == 200
                return prepared["attachment"]["attachment_id"]

            attachment_id = None if late_optional_materials else upload_material()
            first = {"request_id": "problem", "text": "服务出现故障，请按服务定位文档检查。",
                "attachment_ids": [] if late_optional_materials else [attachment_id]}
            _post(client, prefix + "/messages", first)
            assert service.run_once(conversation)
            if racing:
                assert route_entered.wait(5)
                attachment_id = upload_material()
                raced = {"request_id": "parameters", "text": "service=orders\nregion=cn", "attachment_ids": [attachment_id]}
                receipt = _post(client, prefix + "/messages", raced)
                assert _post(client, prefix + "/messages", raced) == receipt
                assert store.pending_generic_restarts() == []
                specialized_release.set()
            assert stack.scheduler.wait_until_idle(15)
            assert len(backend.routes) == 1
            initial = service.get_conversation(conversation)
            expected_status = "WAITING_ATTACHMENT" if optional_materials_only else "WAITING_INPUT"
            assert initial.case_status == expected_status, rpc._case_failure_diagnostics(stack, initial.case_id)
            assert backend.requests == []
            assert service.run_once(conversation)
            assert stack.scheduler.wait_until_idle(15)
            waiting = service.get_conversation(conversation)
            if racing:
                assert waiting.case_status == "WAITING_ATTACHMENT"
                assert backend.requests == [] and len(engine.calls) == 1
                assert service.run_once(conversation)
            elif not optional_materials_only:
                assert waiting.current_questions == ["请提供服务名称。"]
                assert len(engine.calls) == 1
                assert [item.name for item in engine.calls[0].optional_inputs] == ["region"]
                assert all(item.name != "region" for item in engine.calls[0].requirements)
                second = {"request_id": "parameters", "text": "service=orders\nregion=cn",
                    "target_run_id": waiting.run_id}
                if late_optional_materials:
                    attachment_id = upload_material(invalid=late_optional_materials == "invalid_type")
                    if late_optional_materials == "separate":
                        materials = {"request_id": "materials", "attachment_ids": [attachment_id],
                            "target_run_id": waiting.run_id}
                        material_receipt = _post(client, prefix + "/messages", materials)
                        assert _post(client, prefix + "/messages", materials) == material_receipt
                        assert service.run_once(conversation)
                        held = service.get_conversation(conversation)
                        assert held.case_status == "WAITING_INPUT"
                        assert held.current_questions == ["请提供服务名称。"]
                        assert held.messages[-1].status == "APPLIED"
                        assert held.messages[-1].notice == "材料已收到，补齐当前参数后会按所选方法校验，符合要求的材料将用于诊断。"
                        assert store.get_attachment(attachment_id).status == "READY"
                        assert backend.requests == [] and len(engine.calls) == 1
                        assert not service.run_once(conversation)
                    else:
                        second["attachment_ids"] = [attachment_id]
                receipt = _post(client, prefix + "/messages", second)
                assert _post(client, prefix + "/messages", second) == receipt
                assert service.run_once(conversation)
                if late_optional_materials:
                    assert stack.scheduler.wait_until_idle(15)
                    pending = service.get_conversation(conversation)
                    assert pending.case_status == "WAITING_ATTACHMENT", rpc._case_failure_diagnostics(stack, pending.case_id)
                    assert backend.requests == []
                    assert service.run_once(conversation)
                    if late_optional_materials == "invalid_type":
                        rejected = service.get_conversation(conversation)
                        assert rejected.case_status == "WAITING_ATTACHMENT"
                        assert any("所选材料格式不符合要求" in question and "JSON" in question
                            for question in rejected.current_questions)
                        facts = stack.repository.read_case(rejected.case_id).case.diagnosis_state.user_facts
                        assert {item.provenance.input_name: item.statement for item in facts} == {"service": "orders", "region": "cn"}
                        assert backend.requests == [] and len(engine.calls) == 2
                        assert store.get_attachment(attachment_id).status == "READY"
                        assert not service.run_once(conversation)
                        attachment_id = upload_material(request_id="replacement")
                        _post(client, prefix + "/messages", {"request_id": "replacement", "attachment_ids": [attachment_id],
                            "target_run_id": rejected.run_id})
                        assert service.run_once(conversation)
            stack.wait_idle()
            complete = service.get_conversation(conversation)
            assert complete.case_status == "RESOLVED", rpc._case_failure_diagnostics(stack, complete.case_id)
            assert len(engine.calls) == (0 if optional_materials_only else 1 if racing else 2) and len(backend.requests) == 1
            assert backend.requests[0]["user_inputs"] == ({} if optional_materials_only else {"service": "orders", "region": "cn"})
            assert backend.contents == ([] if mode == "none" else [content])
            aggregate = stack.repository.read_case(complete.case_id)
            assert aggregate.case.selected_skill_ref == backend.skill_ref
            assert all(job.job_type is not JobType.REVIEW for job in aggregate.jobs.values())
            assert all(item.name != "region" for item in aggregate.case.diagnosis_state.pending_requirements)
            assert all(event.data.get("stage") != "LOGPARSE" for event in store.list_events(conversation, limit=500))
            assert not (tmp_path / "logparse.json").exists()
            _assert_public_report(stack, client, complete.case_id, prefix, backend.report, "RESOLVED")
            if mode == "none":
                assert complete.messages[0].status == "APPLIED"
                assert "材料未用于本次诊断" in complete.messages[0].notice
                assert store.get_attachment(attachment_id).status == "READY"
            else:
                assert store.get_attachment(attachment_id).status == "IMPORTED"
                assert all("补齐当前参数后" not in (item.notice or "") for item in complete.messages)
    finally:
        route_release.set()
        specialized_release.set()
        assert service.shutdown(2)
        stack.shutdown()
        for path in tmp_path.rglob("*"):
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() else 0o644)
