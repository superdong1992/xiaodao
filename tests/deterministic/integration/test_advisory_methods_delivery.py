"""Advisory Methods output crosses the real website/API/runtime/archive path."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import zipfile
from dataclasses import replace

import pytest

from problem_locator.contracts import ReviewPolicy, canonical_json_bytes
from tests.deterministic.integration.test_website_agent import (
    PARAMETER_GROUP_A, _converse_to_waiting, _post, website,
)


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("case", [
    "quoted_line_differs", "missing_sources", "missing_identity",
    "unknown_method", "conflicting_identity", "empty_evidence",
])
def test_advisory_output_delivers_report_and_available_archive_once(website, monkeypatch, review, case):
    stack, store, engine, service, client = website
    stack.runtime._methods_evidence_validation = "advisory"
    stack.catalog._specialized_review_policy = ReviewPolicy.INDEPENDENT if review else ReviewPolicy.NONE
    backend = stack.runtime._diagnose_backend
    execute = backend.execute
    observed = {"phases": []}

    def altered(**kwargs):
        phase = kwargs.get("backend_phase")
        if phase == "METHODS_SPECIALIST":
            assert "Server evidence policy: advisory" in kwargs["prompt"]
        elif phase == "METHODS_REVIEWER":
            assert "Evidence policy is advisory: judge the whole result semantically" in kwargs["prompt"]
            assert "identity tokens alone are not grounds for rejection" in kwargs["prompt"]
            assert "Preserve each exact (method_id, identity_tokens) pair" not in kwargs["prompt"]
        result = execute(**kwargs)
        observed["phases"].append(phase)
        if phase != "METHODS_SPECIALIST":
            return result
        value = json.loads(result.final_result)
        assert value["evidence"], "The deterministic fixture must produce a model finding."
        if case == "quoted_line_differs":
            for item in value["evidence"]:
                for source in item["sources"]:
                    source["line"] = "MODEL QUOTATION IS NOT AN ACTUAL LOG EXCERPT"
        elif case == "missing_sources":
            for item in value["evidence"]:
                item.pop("sources")
        elif case == "missing_identity":
            for item in value["evidence"]:
                item.pop("identity_tokens")
        elif case == "unknown_method":
            value["confirmed_methods"] = ["model-only-method"]
            for item in value["evidence"]:
                item["method_id"] = "model-only-method"
        elif case == "conflicting_identity":
            additional = copy.deepcopy(value["evidence"][0])
            additional["summary"] = "同一身份标记的另一项模型判断，仍待核对。"
            value["evidence"].append(additional)
        else:
            value["evidence"] = []
        observed["raw"] = canonical_json_bytes(value)
        observed["summaries"] = {item["summary"] for item in value["evidence"]}
        return replace(result, final_result=observed["raw"].decode())

    monkeypatch.setattr(backend, "execute", altered)
    conversation, prefix, _ = _converse_to_waiting(website)
    _post(client, prefix + "/messages", {"request_id": "message:2",
        "text": "\n".join(f"{name}={value}" for name, value in PARAMETER_GROUP_A.items())})
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(25)
    view = service.get_conversation(conversation)
    inconclusive = case == "empty_evidence"
    assert view.case_status == ("UNRESOLVED" if inconclusive else "PARTIALLY_RESOLVED"), view
    assert view.failure is None
    assert observed["phases"].count("METHODS_SPECIALIST") == 1
    assert observed["phases"].count("METHODS_REVIEWER") == int(review and not inconclusive)
    state = stack.repository.read_case(view.case_id)
    diagnose = next(job for job in state.jobs.values() if job.job_type.value == "DIAGNOSE")
    assert diagnose.status.value == "SUCCEEDED"
    records = stack.runtime._execution_records
    assert records.read_audit_bytes(diagnose.job_id, "method-diagnosis.raw.txt") == observed["raw"]
    effective = records.read_audit_bytes(diagnose.job_id, "method-diagnosis.effective.json")
    receipt = json.loads(records.read_audit_bytes(diagnose.job_id, "method-evidence-advisory.json"))
    assert receipt["validation_mode"] == "advisory" and receipt["diagnostic_id"]
    assert receipt["raw_response_sha256"] == hashlib.sha256(observed["raw"]).hexdigest()
    assert receipt["effective_draft_sha256"] == hashlib.sha256(effective).hexdigest()
    assert json.loads(records.read_audit_bytes(diagnose.job_id, "method-grounding-audit.json"))["validation_mode"] == "advisory"

    artifacts_url = f"/api/v1/cases/{view.case_id}/artifacts"
    artifacts = client.get(artifacts_url).json()["data"]["artifacts"]
    report_ref = next(item for item in artifacts if item["kind"] == "USER_RESULT")
    response = client.get(report_ref["download_url"])
    assert response.status_code == 200
    assert len(response.content) == report_ref["size"]
    assert hashlib.sha256(response.content).hexdigest() == report_ref["sha256"]
    report = response.json()
    assert report["status"] == ("INCONCLUSIVE" if inconclusive else "PARTIAL")
    assert report["root_cause"] is None and report["evidence_gaps"]
    assert all(item["status"] == "UNKNOWN" for item in report["completion_criteria_mapping"])
    assert any("未" in text and "复核" in text for text in report["limitations"])
    assert "MODEL QUOTATION IS NOT AN ACTUAL LOG EXCERPT" not in response.text
    assert observed["summaries"] <= {item["statement"] for item in report["findings"]}
    if not inconclusive:
        assert report["findings"]
        assert all(item["confidence"] != 1.0 and item["evidence_bindings"] for item in report["findings"])
        assert all(rule["status"] == "SEMANTIC_ONLY" for rule in report["verification_rules"])
        if case == "missing_sources":
            assert all(citation["line_start"] is citation["archive_name"] is citation["excerpt"] is None
                       for item in report["findings"] for citation in item["citations"])
        assert stack.archive.run_once()
        assert service.get_conversation(conversation).archive_status == "READY"
        artifacts = client.get(artifacts_url).json()["data"]["artifacts"]
        archive_ref = next(item for item in artifacts if item["kind"] == "USER_RESULT_ARCHIVE")
        archive_response = client.get(archive_ref["download_url"])
        assert archive_response.status_code == 200
        assert hashlib.sha256(archive_response.content).hexdigest() == archive_ref["sha256"]
        with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
            for finding in report["findings"]:
                for citation in finding["citations"]:
                    if citation["archive_name"] is None:
                        continue
                    raw_lines = archive.read(citation["archive_name"]).splitlines(keepends=True)
                    raw = b"".join(raw_lines[citation["line_start"] - 1:citation["line_end"]])
                    assert hashlib.sha256(raw).hexdigest() == citation["raw_bytes_sha256"]
    else:
        assert not report["findings"]
        assert not any(item["kind"] == "USER_RESULT_ARCHIVE" for item in artifacts)
    assert client.get(prefix).json()["data"]["failure"] is None
    stream = client.get(prefix + "/events")
    assert b"result.available" in stream.content and b"conversation.completed" in stream.content
    assert observed["phases"].count("METHODS_SPECIALIST") == 1
    assert len(engine.calls) == 1
