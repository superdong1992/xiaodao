"""Model presentation differences must not interrupt production routing."""
from __future__ import annotations

import io
import json
import logging
from dataclasses import replace

import pytest

from problem_locator.contracts import DiagnosisMode, JobType
from problem_locator.journey import configure_journey
from tests.deterministic.integration.test_website_agent import _post, website
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    _GenericRuntimeBackend, _generic_result_bytes,
)


@pytest.fixture
def route_observability(caplog):
    events = io.StringIO()
    caplog.set_level(logging.INFO, logger="problem_locator.dfx")
    configure_journey(stream=events)
    try:
        yield events, caplog
    finally:
        configure_journey()


def _assert_no_route_exception(route, route_observability):
    stream, caplog = route_observability
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    events = [event for event in events if event["job_id"] == route.job_id]
    assert any(event["event"] == "job.stage.completed" for event in events)
    assert not any(event["event"] == "job.stage.failed" for event in events)
    records = [record for record in caplog.records
               if getattr(record, "dfx_context", {}).get("job_id") == route.job_id
               or getattr(record, "dfx_fields", {}).get("job_id") == route.job_id]
    assert any(getattr(record, "dfx_event", None) == "runtime.route.admission" for record in records)
    assert not any(record.levelno >= logging.WARNING or record.exc_info for record in records)


def _inject(stack, monkeypatch, scenario):
    execute = stack.runtime._route_backend.execute
    calls = []

    def route(**kwargs):
        result = execute(**kwargs)
        calls.append(kwargs["backend_phase"])
        if kwargs["backend_phase"] != "ROUTE":
            return result
        value = json.loads(result.final_result)
        assert set(value) == {"skill_id", "reason", "confidence"}
        if scenario == "low_confidence":
            value["confidence"] = 0.94
        elif scenario == "no_match":
            value["skill_id"] = None
        elif scenario == "missing_selection":
            del value["skill_id"]
        elif scenario == "unknown_skill":
            value["skill_id"] = "diagnosis-skill/unknown"
        elif scenario == "bad_json":
            return replace(result, final_result="{broken")
        elif scenario == "unencodable":
            return replace(result, final_result="\ud800")
        elif scenario == "legacy_assessments":
            value["assessments"] = [{"skill_id": value["skill_id"], "exclusions": [{
                "id": "not-rpc", "verdict": "REFUTED",
            }]}]
        elif scenario == "unusable_extra_assessments":
            value["assessments"] = "not used by the three-field protocol"
        elif scenario == "extra_fields":
            value["description"] = "补充说明不参与路由。"
        return replace(result, final_result=json.dumps(value, ensure_ascii=False))

    monkeypatch.setattr(stack.runtime._route_backend, "execute", route)
    return calls


def _start(client, service, stack, *, text="RPC timeout"):
    cid = _post(client, "/api/v1/agent/conversations", {"request_id": "create:route"})["conversation_id"]
    prefix = f"/api/v1/agent/conversations/{cid}"
    _post(client, prefix + "/messages", {"request_id": "message:route", "text": text})
    assert service.run_once(cid)
    assert stack.scheduler.wait_until_idle(20)
    view = service.get_conversation(cid)
    return view, stack.repository.read_case(view.case_id)


@pytest.mark.parametrize("scenario", ["low_confidence", "no_match"])
def test_simple_no_match_delivers_generic_without_specialized_material_requests(website, monkeypatch, scenario):
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
    assert audit["effective_skill_id"] is None
    assert stack.records.read_published_outcome(route.job_id).job_outcome.payload.skill_ref is None


@pytest.mark.parametrize("scenario", ["missing_selection", "unknown_skill", "bad_json", "unencodable"])
def test_unusable_route_output_delivers_generic_without_exception(website, monkeypatch, route_observability, scenario):
    stack, store, engine, service, client = website
    calls = _inject(stack, monkeypatch, scenario)
    generic = _GenericRuntimeBackend(_generic_result_bytes())
    monkeypatch.setattr(stack.runtime._generic_locator_executor, "_backend", generic)
    view, aggregate = _start(client, service, stack)
    assert view.case_status == "RESOLVED", view
    assert aggregate.case.failure is None
    diagnoses = [job for job in aggregate.jobs.values() if job.job_type is JobType.DIAGNOSE]
    assert len(diagnoses) == 1 and diagnoses[0].diagnosis_mode is DiagnosisMode.GENERIC
    assert calls == ["ROUTE"] and len(generic.calls) == 1 and engine.calls == []
    route = next(job for job in aggregate.jobs.values() if job.job_type is JobType.ROUTE)
    raw = stack.records.read_audit_bytes(route.job_id, "route-response.raw.txt")
    assert raw is None if scenario == "unencodable" else raw
    _assert_no_route_exception(route, route_observability)


@pytest.mark.parametrize("scenario", [
    "clear", "legacy_assessments", "unusable_extra_assessments", "extra_fields",
])
def test_three_field_route_and_ignored_extra_fields_keep_specialized_route_without_exception(
    website, monkeypatch, route_observability, scenario,
):
    stack, store, engine, service, client = website
    calls = _inject(stack, monkeypatch, scenario)
    view, aggregate = _start(client, service, stack)
    assert view.case_status in {"WAITING_INPUT", "WAITING_ATTACHMENT"}, view
    assert aggregate.case.failure is None
    assert aggregate.case.selected_skill_ref is not None
    assert aggregate.case.diagnosis_state.pending_requirements
    diagnoses = [job for job in aggregate.jobs.values() if job.job_type is JobType.DIAGNOSE]
    assert len(diagnoses) == 1 and diagnoses[0].diagnosis_mode is DiagnosisMode.SPECIALIZED
    assert calls == ["ROUTE"] and engine.calls == []
    route = next(job for job in aggregate.jobs.values() if job.job_type is JobType.ROUTE)
    assert route.status.value == "SUCCEEDED"
    audit = json.loads(stack.records.read_audit_bytes(route.job_id, "route-admission.json"))
    assert audit["reason_code"] == "ADMITTED"
    _assert_no_route_exception(route, route_observability)
