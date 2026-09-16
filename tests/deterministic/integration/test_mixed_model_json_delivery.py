"""Mixed presentations cross the real website path without another model call."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace

import pytest

from problem_locator.agent.intake import parse_intake_response
from problem_locator.contracts import ReviewPolicy
from problem_locator.runtime.agent_telemetry import AgentStreamTelemetry
from tests.deterministic.integration.test_website_agent import (
    PARAMETER_GROUP_A, _converse_to_waiting, _post, website,
)


def _mixed(text, style):
    prefix = "## 分析说明\r\n已经检查输入，最终结果如下。\r\n"
    return prefix + ("```json\r\n" + text + "\r\n```" if style == "fence" else text)


@pytest.mark.parametrize("style,review,policy", [
    ("fence", False, "strict"), ("tail", True, "strict"), ("fence", True, "advisory"),
])
def test_mixed_model_json_reaches_report_archive_and_replay_once(
    website, monkeypatch, caplog, style, review, policy,
):
    stack, store, engine, service, client = website
    stack.catalog._specialized_review_policy = ReviewPolicy.INDEPENDENT if review else ReviewPolicy.NONE
    stack.runtime._methods_evidence_validation = policy
    caplog.set_level(logging.INFO, logger="problem_locator.dfx")
    observed = {}
    phases = []

    def inject(backend):
        execute = backend.execute

        def mixed(**kwargs):
            result = execute(**kwargs)
            phase = kwargs.get("backend_phase")
            phases.append(phase)
            if phase == "METHODS_REVIEWER":
                path = kwargs["workspace_root"] / "output" / "method-review.draft.json"
                original = path.read_text(encoding="utf-8")
                text = _mixed(original, style)
                path.write_bytes(text.encode())
            elif phase in {"ROUTE", "METHODS_SPECIALIST"}:
                original = result.final_result
                text = _mixed(original, style)
                stream = AgentStreamTelemetry()
                for event in [
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
                    {"type": "result", "subtype": "success", "is_error": False, "result": text},
                ]:
                    stream.write(json.dumps(event, ensure_ascii=False).encode() + b"\r\n")
                assert stream.final_result == text
                result = replace(result, final_result=stream.final_result)
            else:
                return result
            observed[phase] = (text.encode(), json.loads(original))
            return result

        monkeypatch.setattr(backend, "execute", mixed)

    for backend in {id(value): value for value in (
        stack.runtime._route_backend, stack.runtime._diagnose_backend,
    )}.values():
        inject(backend)
    intake = engine.intake

    def mixed_intake(request):
        decision = intake(request)
        return parse_intake_response(_mixed(decision.model_dump_json(), style), request)

    monkeypatch.setattr(engine, "intake", mixed_intake)
    conversation, prefix, _ = _converse_to_waiting(website)
    _post(client, prefix + "/messages", {"request_id": "message:2",
        "text": "\n".join(f"{name}={value}" for name, value in PARAMETER_GROUP_A.items())})
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(25)
    view = service.get_conversation(conversation)
    assert view.failure is None
    assert view.case_status == ("RESOLVED" if policy == "strict" else "PARTIALLY_RESOLVED")
    state = stack.repository.read_case(view.case_id)
    records = stack.runtime._execution_records
    extracted_jobs = 0
    for job in state.jobs.values():
        receipt_bytes = records.read_audit_bytes(job.job_id, "model-json-extraction.json")
        if receipt_bytes is None:
            outcome = records.read_published_outcome(job.job_id).job_outcome
            assert outcome.result_type.value in {"NEED_INPUT", "NEED_ATTACHMENT"}
            continue
        extracted_jobs += 1
        phase = {"ROUTE": "ROUTE", "DIAGNOSE": "METHODS_SPECIALIST", "REVIEW": "METHODS_REVIEWER"}[job.job_type.value]
        raw, expected = observed[phase]
        receipt = json.loads(receipt_bytes)
        extracted = records.read_audit_bytes(job.job_id, "model-response.extracted.txt")
        assert records.read_audit_bytes(job.job_id, receipt["raw_file"]) == raw
        assert extracted == raw[receipt["start_byte"]:receipt["end_byte"]]
        assert json.loads(extracted) == expected
        assert receipt["raw_sha256"] == hashlib.sha256(raw).hexdigest()
        assert receipt["effective_sha256"] == hashlib.sha256(extracted).hexdigest()
        assert receipt["case_id"] == view.case_id and receipt["job_id"] == job.job_id
        events = [r for r in caplog.records if getattr(r, "dfx_event", None) == "runtime.model_json.extracted"
                  and r.dfx_fields["job_id"] == job.job_id]
        assert len(events) == 1
        assert events[0].dfx_fields["diagnostic_id"] == receipt["diagnostic_id"]
        assert "最终结果" not in json.dumps(events[0].dfx_fields, ensure_ascii=False)
    assert extracted_jobs == 2 + int(review)

    artifact_path = f"/api/v1/cases/{view.case_id}/artifacts"
    artifacts = client.get(artifact_path).json()["data"]["artifacts"]
    report_ref = next(item for item in artifacts if item["kind"] == "USER_RESULT")
    report = client.get(report_ref["download_url"])
    assert report.status_code == 200
    assert hashlib.sha256(report.content).hexdigest() == report_ref["sha256"]
    assert report.json()["status"] == ("COMPLETED" if policy == "strict" else "PARTIAL")
    assert stack.archive.run_once()
    assert service.get_conversation(conversation).archive_status == "READY"
    replay = client.get(prefix + "/events")
    assert b"result.available" in replay.content and b"conversation.completed" in replay.content
    assert phases.count("ROUTE") == phases.count("METHODS_SPECIALIST") == 1
    assert phases.count("METHODS_REVIEWER") == int(review)
    assert len(engine.calls) == 1


def test_two_route_json_candidates_fail_visibly_without_model_retry(website, monkeypatch):
    stack, store, engine, service, client = website
    backend = stack.runtime._route_backend
    execute = backend.execute
    calls = []

    def ambiguous(**kwargs):
        calls.append(kwargs.get("backend_phase"))
        result = execute(**kwargs)
        return replace(result, final_result="## 说明\n" + result.final_result + "\n" + result.final_result)

    monkeypatch.setattr(backend, "execute", ambiguous)
    conversation = _post(client, "/api/v1/agent/conversations", {"request_id": "create:1"})["conversation_id"]
    prefix = f"/api/v1/agent/conversations/{conversation}"
    _post(client, prefix + "/messages", {"request_id": "message:1", "text": "RPC timeout"})
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(15)
    view = client.get(prefix).json()["data"]
    assert view["failure"]["code"] == "OUTCOME_INVALID"
    assert view["failure"]["retryable"] is False
    assert calls == ["ROUTE"] and not engine.calls
    assert not any(event.type == "result.available" for event in store.list_events(conversation))
