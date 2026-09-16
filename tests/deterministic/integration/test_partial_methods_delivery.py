"""Reviewer-off evidence selection crosses the real HTTP/runtime/storage seams."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace

import pytest

from problem_locator.contracts import ReviewPolicy, canonical_json_bytes
from tests.deterministic.integration.test_website_agent import (
    PARAMETER_GROUP_A, _converse_to_waiting, _post, website,
)


@pytest.mark.parametrize("review,all_invalid", [(False, False), (False, True), (True, False)])
def test_selected_findings_reach_http_report_and_persisted_case(website, monkeypatch, review, all_invalid):
    stack, store, engine, service, client = website
    stack.catalog._specialized_review_policy = ReviewPolicy.INDEPENDENT if review else ReviewPolicy.NONE
    backend = stack.runtime._diagnose_backend
    execute = backend.execute
    calls = []
    original = None

    def mixed(**kwargs):
        nonlocal original
        result = execute(**kwargs)
        calls.append(kwargs.get("backend_phase"))
        if kwargs.get("backend_phase") == "METHODS_SPECIALIST":
            value = json.loads(result.final_result)
            bad = copy.deepcopy(value["evidence"][0])
            bad["summary"] = "UNVERIFIED SUMMARY MUST NOT BE PUBLISHED"
            bad["identity_tokens"] = ["synthetic-absent-identity"]
            bad["sources"][0]["line"] = "fabricated evidence line"
            value["evidence"] = [bad] if all_invalid else [*value["evidence"], bad]
            original = canonical_json_bytes(value)
            return replace(result, final_result=original.decode())
        return result

    monkeypatch.setattr(backend, "execute", mixed)
    conversation, prefix, _ = _converse_to_waiting(website)
    _post(client, prefix + "/messages", {"request_id": "message:2",
        "text": "\n".join(f"{name}={value}" for name, value in PARAMETER_GROUP_A.items())})
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(20)
    view = service.get_conversation(conversation)
    assert calls.count("METHODS_SPECIALIST") == 1
    assert "METHODS_REVIEWER" not in calls
    if review:
        assert view.case_status == "FAILED", view
        assert not any(event.type == "result.available" for event in store.list_events(conversation))
        return

    expected = "UNRESOLVED" if all_invalid else "PARTIALLY_RESOLVED"
    assert view.case_status == expected, stack.application.get_case(view.case_id).case_view.failure
    state = stack.repository.read_case(view.case_id)
    assert state.case.status.value == expected
    artifacts = client.get(f"/api/v1/cases/{view.case_id}/artifacts").json()["data"]["artifacts"]
    report_ref = next(item for item in artifacts if item["kind"] == "USER_RESULT")
    downloaded = client.get(report_ref["download_url"])
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == report_ref["sha256"]
    report = downloaded.json()
    assert report["status"] == ("INCONCLUSIVE" if all_invalid else "PARTIAL")
    assert report["root_cause"] is None
    assert report["evidence_gaps"]
    assert b"UNVERIFIED SUMMARY" not in downloaded.content
    assert all(item["status"] == "UNKNOWN" for item in report["completion_criteria_mapping"])
    if all_invalid:
        assert not report["findings"] and not report["causal_factors"]
    else:
        assert report["findings"] and report["causal_factors"]
        assert all(item["citations"] for item in report["findings"])
    diagnose = next(job for job in state.jobs.values() if job.review_policy is ReviewPolicy.NONE
                    and job.status.value == "SUCCEEDED")
    records = stack.runtime._execution_records
    assert records.read_audit_bytes(diagnose.job_id, "method-diagnosis.raw.txt") == original
    receipt = json.loads(records.read_audit_bytes(diagnose.job_id, "method-evidence-selection.json"))
    assert receipt["raw_response_sha256"] == hashlib.sha256(original).hexdigest()
    assert receipt["rejected"] and receipt["diagnostic_id"]
    assert len(engine.calls) == 1
    if not all_invalid:
        assert view.archive_status == "PENDING"
        assert stack.archive.run_once()
        assert service.get_conversation(conversation).archive_status == "READY"
        events = client.get(prefix + "/events").content
        assert b"conversation.completed" in events
        assert calls.count("METHODS_SPECIALIST") == 1
