"""The zero-model route still creates DIAGNOSE and collects missing materials."""
from __future__ import annotations

import json
import shutil

import pytest

from problem_locator.contracts import DiagnosisMode, JobType, canonical_json_bytes
from tests.document_skill_helpers import document_input, document_registration, write_document_registration
from tests.deterministic.integration.test_website_agent import _post, website
from tests.deterministic.journey import test_rpc_timeout as rpc


@pytest.fixture(params=["methods", "document"])
def keyword_website(tmp_path, monkeypatch, request):
    skills = tmp_path / "keyword-skills"
    if request.param == "methods":
        shutil.copytree(rpc.SKILL_DIR, skills)
        registration = skills / "rpc-log-analysis/registration-template.json"
        value = json.loads(registration.read_bytes())
        value["routing"]["direct_match_keywords"] = ["ACME_RPC_DEADLINE"]
        registration.write_bytes(canonical_json_bytes(value))
    else:
        value = document_registration(mode="direct_files", user_inputs=[document_input("service")])
        value["routing"]["direct_match_keywords"] = ["ACME_RPC_DEADLINE"]
        write_document_registration(skills, registration=value)
    monkeypatch.setattr(rpc, "SKILL_DIR", skills)
    yield from website.__wrapped__(tmp_path)


def test_keyword_route_creates_specialized_diagnose_and_requests_missing_inputs(keyword_website, monkeypatch):
    stack, store, engine, service, client = keyword_website
    calls = []
    execute = stack.runtime._route_backend.execute
    def observed(**kwargs):
        calls.append(kwargs["backend_phase"])
        return execute(**kwargs)
    monkeypatch.setattr(stack.runtime._route_backend, "execute", observed)
    cid = _post(client, "/api/v1/agent/conversations", {"request_id": "create:keyword"})["conversation_id"]
    prefix = f"/api/v1/agent/conversations/{cid}"
    message = {"request_id": "message:keyword", "text": "服务报错 ACME_RPC_DEADLINE，请定位。"}
    accepted = _post(client, prefix + "/messages", message)
    assert _post(client, prefix + "/messages", message) == accepted
    assert service.run_once(cid)
    assert stack.scheduler.wait_until_idle(20)
    view = service.get_conversation(cid)
    assert view.case_status in {"WAITING_INPUT", "WAITING_ATTACHMENT"}, view
    aggregate = stack.repository.read_case(view.case_id)
    route = next(job for job in aggregate.jobs.values() if job.job_type is JobType.ROUTE)
    diagnoses = [job for job in aggregate.jobs.values() if job.job_type is JobType.DIAGNOSE]
    assert len(diagnoses) == 1 and diagnoses[0].diagnosis_mode is DiagnosisMode.SPECIALIZED
    assert diagnoses[0].skill_ref == aggregate.case.selected_skill_ref
    assert aggregate.case.diagnosis_state.pending_requirements
    assert calls == [] and engine.calls == []
    audit = json.loads(stack.records.read_audit_bytes(route.job_id, "route-admission.json"))
    assert audit["model_called"] is False
    assert audit["effective_skill_id"] == diagnoses[0].skill_ref.id
