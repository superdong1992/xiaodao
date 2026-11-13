"""The production ROUTE prompt contains only a recalled, bounded shortlist."""
from __future__ import annotations

import json
import logging
import shutil

import pytest

from problem_locator.contracts import DiagnosisMode, JobType
from problem_locator.runtime.agent_backend import BackendExecution
from tests.document_skill_helpers import document_input, document_registration, write_document_registration
from tests.deterministic.integration.test_route_admission_delivery import (
    _assert_no_route_exception, _start, route_observability,
)
from tests.deterministic.integration.test_website_agent import website
from tests.deterministic.journey import test_rpc_timeout as rpc
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    _GenericRuntimeBackend, _generic_result_bytes,
)
from tests.route_helpers import context_section


_AUTO_CHOICE = object()
_OMIT_CONFIDENCE = object()


@pytest.fixture
def shortlist_website(tmp_path, monkeypatch):
    skills = tmp_path / "shortlist-skills"
    shutil.copytree(rpc.SKILL_DIR, skills)
    registrations = {}
    for number in range(7):
        name = f"candidate-{number}"
        value = document_registration(name, user_inputs=[document_input("service")])
        value["capability"] = "acme-route-shared"
        value["summary"] = "ACME_ROUTE_SHARED incident diagnosis."
        value["routing"] = {
            "applicability": [
                {"id": "incident", "description": "ACME_ROUTE_SHARED incident is present."},
                {"id": "active", "description": f"Candidate {number} handles the currently active incident."},
            ],
            "exclusions": [{"id": "unrelated", "description": f"Candidate {number} excludes unrelated workloads."}],
        }
        write_document_registration(skills, registration=value)
        registrations[f"diagnosis-skill/{name}"] = value
    monkeypatch.setattr(rpc, "SKILL_DIR", skills)
    for bundle in website.__wrapped__(tmp_path):
        yield bundle, registrations


class _ShortlistChoiceBackend:
    def __init__(self, *, outside_ids=(), skill_id=_AUTO_CHOICE, confidence=0.99):
        self.calls = []
        self.outside_ids = outside_ids
        self.skill_id = skill_id
        self.confidence = confidence

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        index = context_section(kwargs["prompt"], "SKILL_INDEX")
        candidates = [item["skill_id"] for item in index["skills"]]
        skill_id = self.skill_id
        if skill_id is _AUTO_CHOICE:
            skill_id = (next(item for item in self.outside_ids if item not in candidates)
                        if self.outside_ids else candidates[0])
        response = {"skill_id": skill_id, "reason": "Return the scenario's predetermined routing choice."}
        if self.confidence is not _OMIT_CONFIDENCE:
            response["confidence"] = self.confidence
        for sink in {id(kwargs["log_sinks"].stdout): kwargs["log_sinks"].stdout,
                     id(kwargs["log_sinks"].stderr): kwargs["log_sinks"].stderr}.values():
            sink.flush()
            sink.close()
        return BackendExecution(returncode=0, stdout_stderr_bytes=0, workspace_bytes=0,
            elapsed_seconds=0.01, final_result=json.dumps(response))


@pytest.mark.parametrize("outside", [False, True])
def test_more_than_five_skills_send_only_shortlist_and_enforce_its_membership(
    shortlist_website, monkeypatch, route_observability, outside,
):
    bundle, registrations = shortlist_website
    stack, store, engine, service, client = bundle
    backend = _ShortlistChoiceBackend(outside_ids=tuple(registrations) if outside else (), confidence=1)
    monkeypatch.setattr(stack.runtime, "_route_backend", backend)
    generic = _GenericRuntimeBackend(_generic_result_bytes())
    monkeypatch.setattr(stack.runtime._generic_locator_executor, "_backend", generic)
    view, aggregate = _start(client, service, stack, text="ACME_ROUTE_SHARED incident")
    assert aggregate.case.failure is None
    assert len(backend.calls) == 1 and engine.calls == []
    index = context_section(backend.calls[0]["prompt"], "SKILL_INDEX")
    assert index["schema_version"] == 1
    assert len(index["skills"]) == 5
    assert len({item["skill_id"] for item in index["skills"]}) == 5
    for item in index["skills"]:
        assert set(item) == {"skill_id", "name", "capability", "summary", "routing"}
        assert item["routing"] == registrations[item["skill_id"]]["routing"]
    assert "content_hash" not in json.dumps(index) and '"ref"' not in json.dumps(index)
    route = next(job for job in aggregate.jobs.values() if job.job_type is JobType.ROUTE)
    assert len(route.available_skill_refs) > 5
    assert route.status.value == "SUCCEEDED"
    diagnoses = [job for job in aggregate.jobs.values() if job.job_type is JobType.DIAGNOSE]
    assert len(diagnoses) == 1
    if outside:
        assert view.case_status == "RESOLVED"
        assert diagnoses[0].diagnosis_mode is DiagnosisMode.GENERIC
        assert aggregate.case.selected_skill_ref is None and len(generic.calls) == 1
    else:
        assert view.case_status == "WAITING_INPUT"
        assert diagnoses[0].diagnosis_mode is DiagnosisMode.SPECIALIZED
        assert aggregate.case.selected_skill_ref.id == index["skills"][0]["skill_id"]
        assert aggregate.case.diagnosis_state.pending_requirements and generic.calls == []
    _assert_no_route_exception(route, route_observability)


@pytest.mark.parametrize("skill_id,confidence", [
    ("diagnosis-skill/candidate-3", 0),
    ("diagnosis-skill/candidate-3", 0.8),
    ("diagnosis-skill/candidate-3", 0.94),
    ("diagnosis-skill/candidate-3", _OMIT_CONFIDENCE),
    ("diagnosis-skill/candidate-3", "invalid"),
    (None, 1),
], ids=["zero", "low", "below-old-threshold", "omitted", "unusable", "explicit-no-match"])
def test_model_choice_reaches_http_delivery_independently_of_optional_confidence(
    shortlist_website, monkeypatch, route_observability, skill_id, confidence,
):
    # The backend supplies a fixed choice; this verifies delivery, not model accuracy.
    bundle, registrations = shortlist_website
    stack, store, engine, service, client = bundle
    backend = _ShortlistChoiceBackend(skill_id=skill_id, confidence=confidence)
    monkeypatch.setattr(stack.runtime, "_route_backend", backend)
    generic = _GenericRuntimeBackend(_generic_result_bytes())
    monkeypatch.setattr(stack.runtime._generic_locator_executor, "_backend", generic)
    view, aggregate = _start(client, service, stack, text="ACME_ROUTE_SHARED incident")
    assert aggregate.case.failure is None and len(backend.calls) == 1 and engine.calls == []
    candidates = context_section(backend.calls[0]["prompt"], "SKILL_INDEX")["skills"]
    assert len(candidates) == 5
    assert "diagnosis-skill/candidate-3" in [item["skill_id"] for item in candidates]
    route = next(job for job in aggregate.jobs.values() if job.job_type is JobType.ROUTE)
    assert route.status.value == "SUCCEEDED"
    diagnoses = [job for job in aggregate.jobs.values() if job.job_type is JobType.DIAGNOSE]
    assert len(diagnoses) == 1
    audit = json.loads(stack.records.read_audit_bytes(route.job_id, "route-admission.json"))
    assert audit["effective_skill_id"] == skill_id
    raw = json.loads(stack.records.read_audit_bytes(route.job_id, "route-response.raw.txt"))
    assert ("confidence" in raw) is (confidence is not _OMIT_CONFIDENCE)
    if skill_id is None:
        assert view.case_status == "RESOLVED"
        assert diagnoses[0].diagnosis_mode is DiagnosisMode.GENERIC
        assert aggregate.case.selected_skill_ref is None and len(generic.calls) == 1
        assert audit["reason_code"] == "MODEL_NO_MATCH"
    else:
        assert view.case_status == "WAITING_INPUT"
        assert diagnoses[0].diagnosis_mode is DiagnosisMode.SPECIALIZED
        assert aggregate.case.selected_skill_ref.id == skill_id and generic.calls == []
        assert aggregate.case.diagnosis_state.pending_requirements
        assert audit["reason_code"] == "ADMITTED"
    _assert_no_route_exception(route, route_observability)


def test_no_recalled_skill_runs_generic_without_route_model_or_exception(
    shortlist_website, monkeypatch, route_observability,
):
    bundle, registrations = shortlist_website
    stack, store, engine, service, client = bundle
    backend = _ShortlistChoiceBackend()
    monkeypatch.setattr(stack.runtime, "_route_backend", backend)
    generic = _GenericRuntimeBackend(_generic_result_bytes())
    monkeypatch.setattr(stack.runtime._generic_locator_executor, "_backend", generic)
    view, aggregate = _start(client, service, stack, text="ZXQ_UNRELATED_947205")
    assert view.case_status == "RESOLVED" and aggregate.case.failure is None
    assert backend.calls == [] and len(generic.calls) == 1 and engine.calls == []
    route = next(job for job in aggregate.jobs.values() if job.job_type is JobType.ROUTE)
    assert len(route.available_skill_refs) > 5 and route.status.value == "SUCCEEDED"
    assert aggregate.case.selected_skill_ref is None
    assert stack.records.read_audit_bytes(route.job_id, "route-response.raw.txt") is None
    audit = json.loads(stack.records.read_audit_bytes(route.job_id, "route-admission.json"))
    assert audit["model_called"] is False and audit["effective_skill_id"] is None
    stream, caplog = route_observability
    assert not any(json.loads(line)["event"] == "job.stage.failed" for line in stream.getvalue().splitlines())
    assert not any(record.levelno >= logging.WARNING or record.exc_info for record in caplog.records)
