"""The production ROUTE gate must reach Generic, or preserve protocol failure."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from problem_locator.contracts import DiagnosisMode, JobType
from tests.deterministic.integration.test_website_agent import _post, website
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    _GenericRuntimeBackend, _generic_result_bytes,
)


def _inject(stack, monkeypatch, scenario):
    execute = stack.runtime._route_backend.execute
    calls = []

    def route(**kwargs):
        result = execute(**kwargs)
        calls.append(kwargs["backend_phase"])
        if kwargs["backend_phase"] != "ROUTE":
            return result
        value = json.loads(result.final_result)
        selected = next(item for item in value["assessments"] if item["skill_id"] == value["skill_id"])
        check = selected["applicability"][0]
        if scenario == "low_confidence":
            value["confidence"] = 0.94
        elif scenario == "scope_refuted":
            value["confidence"] = 1
            check["verdict"] = "REFUTED"
            check["reason"] = "场景不属于该 Skill。"
        elif scenario == "unknown":
            check.update(verdict="UNKNOWN", evidence=[])
        elif scenario == "invalid_quote":
            value["confidence"] = 1
            check["evidence"][0]["quote"] = "输入中不存在的专用产品证据"
        elif scenario == "ambiguous":
            for item in value["assessments"]:
                for condition in item["applicability"]:
                    condition["verdict"] = "SUPPORTED"
        elif scenario == "missing_assessments":
            del value["assessments"]
        elif scenario == "unknown_skill":
            value["skill_id"] = "diagnosis-skill/unknown"
        elif scenario == "bad_json":
            return replace(result, final_result="{broken")
        return replace(result, final_result=json.dumps(value, ensure_ascii=False))

    monkeypatch.setattr(stack.runtime._route_backend, "execute", route)
    return calls


def _start(client, service, stack):
    cid = _post(client, "/api/v1/agent/conversations", {"request_id": "create:route"})["conversation_id"]
    prefix = f"/api/v1/agent/conversations/{cid}"
    _post(client, prefix + "/messages", {"request_id": "message:route", "text": "RPC timeout"})
    assert service.run_once(cid)
    assert stack.scheduler.wait_until_idle(20)
    view = service.get_conversation(cid)
    return view, stack.repository.read_case(view.case_id)


@pytest.mark.parametrize("scenario", ["low_confidence", "scope_refuted", "unknown", "invalid_quote", "ambiguous"])
def test_semantic_rejection_delivers_generic_without_specialized_material_requests(website, monkeypatch, scenario):
    stack, store, engine, service, client = website
    calls = _inject(stack, monkeypatch, scenario)
    generic = _GenericRuntimeBackend(_generic_result_bytes(conclusion="已转入通用定位。"))
    monkeypatch.setattr(stack.runtime._generic_locator_executor, "_backend", generic)
    view, aggregate = _start(client, service, stack)
    assert view.case_status == "RESOLVED", view
    route = next(job for job in aggregate.jobs.values() if job.job_type is JobType.ROUTE)
    diagnose = [job for job in aggregate.jobs.values() if job.job_type is JobType.DIAGNOSE]
    assert len(diagnose) == 1 and diagnose[0].diagnosis_mode is DiagnosisMode.GENERIC
    assert diagnose[0].skill_ref is None and aggregate.case.selected_skill_ref is None
    assert not aggregate.case.diagnosis_state.pending_requirements
    assert aggregate.case.generic_result.conclusion == "已转入通用定位。"
    assert calls == ["ROUTE"] and len(generic.calls) == 1 and engine.calls == []
    audit = json.loads(stack.records.read_audit_bytes(route.job_id, "route-admission.json"))
    assert audit["model_skill_id"] is not None and audit["effective_skill_id"] is None
    assert stack.records.read_published_outcome(route.job_id).job_outcome.payload.skill_ref is None


@pytest.mark.parametrize("scenario", ["missing_assessments", "unknown_skill", "bad_json"])
def test_route_protocol_error_remains_failure_without_generic_fallback(website, monkeypatch, scenario):
    stack, store, engine, service, client = website
    calls = _inject(stack, monkeypatch, scenario)
    generic = _GenericRuntimeBackend(_generic_result_bytes())
    monkeypatch.setattr(stack.runtime._generic_locator_executor, "_backend", generic)
    view, aggregate = _start(client, service, stack)
    assert view.case_status == "FAILED", view
    assert aggregate.case.failure.code.value == "OUTCOME_INVALID"
    assert all(job.job_type is JobType.ROUTE for job in aggregate.jobs.values())
    assert calls == ["ROUTE"] and generic.calls == [] and engine.calls == []
    route = next(iter(aggregate.jobs.values()))
    assert stack.records.read_audit_bytes(route.job_id, "route-response.raw.txt")


def test_clear_route_still_requests_missing_diagnostic_materials(website, monkeypatch):
    stack, store, engine, service, client = website
    calls = _inject(stack, monkeypatch, "clear")
    view, aggregate = _start(client, service, stack)
    assert view.case_status in {"WAITING_INPUT", "WAITING_ATTACHMENT"}, view
    assert aggregate.case.selected_skill_ref is not None
    assert any(job.diagnosis_mode is DiagnosisMode.SPECIALIZED for job in aggregate.jobs.values())
    assert aggregate.case.diagnosis_state.pending_requirements
    assert calls == ["ROUTE"]
