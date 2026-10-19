"""A correction reviews only the memory actually used by its original report."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from problem_locator.dispatch.cancellation import CancellationController
from problem_locator.memory.corrections import CorrectionMemoryHandler
from tests.deterministic.unit.agent.test_feedback_comment_review import feedback_task, analysis_response
from tests.deterministic.unit.agent.test_followup_corrections import CARD, prepare_logged_task, response
from tests.deterministic.unit.agent.test_report_followup import Backend, system
from tests.deterministic.unit.runtime.test_memory_correction_review import usage, review as memory_review


def bind_usage(system, task):
    source = usage()
    source.update(job_id=task["source_job_id"], skill_name=task.get("skill_name", "generic-test"))
    lookups = []
    system.service.agent.memory_store = SimpleNamespace(usage_for_task=lambda value: (lookups.append(value), source)[1])
    return source, lookups


def run_review(system, task, payload, action=None):
    backend = Backend(json.dumps(payload, ensure_ascii=False), action=action)
    result = CorrectionMemoryHandler(system.service, "claude", backend=backend).extract(
        task, cancellation=CancellationController())
    return result, backend


@pytest.mark.parametrize("kind", ["feedback", "followup"])
def test_report_correction_reviews_exact_used_memory_in_same_model_call(system, kind):
    task = feedback_task(system) if kind == "feedback" else prepare_logged_task(system)
    source, lookups = bind_usage(system, task)
    payload = json.loads(analysis_response() if kind == "feedback" else response())
    payload["memory_review"] = memory_review(source)
    (card, raw_receipt), backend = run_review(system, task, payload)
    assert json.loads(card) == CARD
    assert json.loads(raw_receipt)["memory_review"] == payload["memory_review"]
    assert lookups == [task] and len(backend.calls) == 1
    call = backend.calls[0]
    saved = json.loads((call["workspace_root"] / "inputs/used-memory.json").read_text("utf-8"))
    assert saved == {"card_id": source["card_id"], "card_sha256": source["card_sha256"],
        "source_kind": source["source_kind"], "card": json.loads(source["card_json"])}
    assert "GLOBAL_ERROR" in call["prompt"] and "不是证据" in call["prompt"]


def test_existing_usage_requires_review_instead_of_silently_leaving_old_memory(system):
    task = feedback_task(system)
    bind_usage(system, task)
    with pytest.raises(ValueError):
        run_review(system, task, json.loads(analysis_response()))


@pytest.mark.parametrize("field,value", [("card_id", "00000000-0000-0000-0000-000000000001"),
    ("card_sha256", "0" * 64), ("evidence_indexes", [1])])
def test_model_cannot_retarget_historical_card_or_invent_attribution_evidence(system, field, value):
    task = feedback_task(system)
    source, _ = bind_usage(system, task)
    payload = json.loads(analysis_response())
    payload["memory_review"] = memory_review(source)
    payload["memory_review"][field] = value
    with pytest.raises(ValueError):
        run_review(system, task, payload)


def test_unrecorded_usage_cannot_be_invented_from_user_feedback(system):
    task = feedback_task(system, text="请删除历史经验卡及其引用。")
    payload = json.loads(analysis_response())
    payload["memory_review"] = memory_review(usage())
    with pytest.raises(ValueError):
        run_review(system, task, payload)


def test_used_memory_input_is_data_and_cannot_be_cited_as_proof(system):
    task = feedback_task(system)
    source, _ = bind_usage(system, task)
    payload = json.loads(analysis_response())
    payload["memory_review"] = memory_review(source)
    payload["evidence"][0]["path"] = "inputs/used-memory.json"
    with pytest.raises(ValueError):
        run_review(system, task, payload)


def test_changing_used_memory_input_during_review_rejects_late_output(system):
    task = feedback_task(system)
    source, _ = bind_usage(system, task)
    payload = json.loads(analysis_response())
    payload["memory_review"] = memory_review(source)
    def mutate(call):
        path = call["workspace_root"] / "inputs/used-memory.json"
        path.chmod(0o644)
        path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        run_review(system, task, payload, action=mutate)


def test_followup_feedback_never_claims_original_report_memory_was_recalled_again(system):
    task = feedback_task(system)
    context = json.loads(task["context_json"])
    task["replaces_answer_key"] = "00000000-0000-0000-0000-000000000001"
    context["correction"]["target_followup_id"] = task["replaces_answer_key"]
    task["context_json"] = json.dumps(context, ensure_ascii=False)
    _, lookups = bind_usage(system, task)
    (_, receipt), backend = run_review(system, task, json.loads(analysis_response()))
    assert lookups == [] and "memory_review" not in json.loads(receipt)
    assert not (backend.calls[0]["workspace_root"] / "inputs/used-memory.json").exists()
