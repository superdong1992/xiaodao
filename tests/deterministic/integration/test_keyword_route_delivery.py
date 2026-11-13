"""The zero-model route still creates DIAGNOSE and collects missing materials."""
from __future__ import annotations

import json
import shutil

import pytest

from problem_locator.contracts import DiagnosisMode, JobType, canonical_json_bytes
from tests.document_skill_helpers import document_input, document_registration, write_document_registration
from tests.deterministic.integration.test_route_admission_delivery import _assert_no_route_exception, route_observability
from tests.deterministic.integration.test_website_agent import _post, website
from tests.deterministic.journey import test_rpc_timeout as rpc
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    _GenericRuntimeBackend, _RuntimeBackend, _generic_result_bytes,
)


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


def test_initial_correction_reaches_semantic_route_and_generic_delivery(
    keyword_website, monkeypatch, route_observability,
):
    stack, store, engine, service, client = keyword_website
    backend = _RuntimeBackend(b'{"payload":{"skill_ref":null,"reason":"The current fact rules out the old keyword."}}')
    generic = _GenericRuntimeBackend(_generic_result_bytes())
    monkeypatch.setattr(stack.runtime, "_route_backend", backend)
    monkeypatch.setattr(stack.runtime._generic_locator_executor, "_backend", generic)
    text = "服务报错 ACME_RPC_DEADLINE，请定位。"
    fact = "已确认不是 ACME_RPC_DEADLINE，实际是 DB_LOCK_BUSY。"
    created = _post(client, "/api/v1/cases", {
        "request_id": "create:keyword-correction", "raw_problem_text": text,
        "problem_spec": {
            "statement": text, "actual_behavior": text, "expected_behavior": "恢复正常。",
            "scope": "当前故障", "goals": ["定位原因"], "non_goals": [],
            "constraints": [], "completion_criteria": ["给出定位结论"],
        },
        "initial_user_facts": [{"name": "scope", "value": fact}], "wait_seconds": 0,
    })
    assert stack.scheduler.wait_until_idle(20)
    aggregate = stack.repository.read_case(created["business_receipt"]["case_id"])
    route = next(job for job in aggregate.jobs.values() if job.job_type is JobType.ROUTE)
    assert not route.previous_outcome_refs and route.context_snapshot.user_facts
    assert aggregate.case.status.value == "RESOLVED" and aggregate.case.failure is None
    assert aggregate.case.selected_skill_ref is None
    diagnoses = [job for job in aggregate.jobs.values() if job.job_type is JobType.DIAGNOSE]
    assert len(diagnoses) == 1 and diagnoses[0].diagnosis_mode is DiagnosisMode.GENERIC
    assert [call["backend_phase"] for call in backend.calls] == ["ROUTE"]
    assert fact in backend.calls[0]["prompt"]
    assert len(generic.calls) == 1 and engine.calls == []
    audit = json.loads(stack.records.read_audit_bytes(route.job_id, "route-admission.json"))
    assert audit["reason_code"] == "MODEL_NO_MATCH" and audit["effective_skill_id"] is None
    _assert_no_route_exception(route, route_observability)
