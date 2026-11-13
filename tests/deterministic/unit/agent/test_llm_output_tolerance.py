"""Harmless model formatting does not discard usable intake or memory data."""
from __future__ import annotations

import json
import logging

import pytest

from problem_locator.agent.intake import ClaudeIntakeEngine, intake_processing_receipt, parse_intake_response
from problem_locator.memory.corrections import CorrectionMemoryHandler
from problem_locator.memory.extraction import parse_card_json
from tests.deterministic.unit.agent.test_feedback_comment_review import analysis_response, feedback_task
from tests.deterministic.unit.agent.test_followup_corrections import prepare_logged_task, response
from tests.deterministic.unit.agent.test_intake import _Backend, _decision, _request, _requirement, _value
from tests.deterministic.unit.agent.test_report_followup import system
from tests.deterministic.unit.runtime.test_experience_memory import card
from tests.deterministic.unit.runtime.test_experience_memory import Backend, Store, task, worker
from tests.deterministic.unit.runtime.test_memory_correction_review import usage, review, validate


@pytest.mark.parametrize("variation", ["extra_root", "extra_fact", "missing_fields", "missing_message", "missing_action", "wrong_schema"])
def test_intake_keeps_grounded_facts_despite_optional_output_variations(variation, caplog):
    fact = _value("device_model", "X1")
    payload = _decision(problem_fields=[], user_facts=[fact]).model_dump(mode="json")
    if variation == "extra_root":
        payload["reason"] = "用户明确提供型号。"
    elif variation == "extra_fact":
        payload["user_facts"][0]["confidence"] = 1
    elif variation == "missing_fields":
        payload.pop("problem_fields")
    elif variation == "missing_message":
        payload.pop("message")
    elif variation == "missing_action":
        payload.pop("action")
    else:
        payload["schema_version"] = "1"
    result = parse_intake_response(json.dumps(payload), _request(requirements=[_requirement("device_model")]))
    assert result.action == "SUBMIT_SUPPLEMENT" and result.user_facts == [fact]
    assert intake_processing_receipt(result) is None
    assert not caplog.records


@pytest.mark.parametrize("variation", ["extra", "fence", "empty_optional", "string_list", "long_item", "extra_item"])
def test_memory_card_ignores_unused_format_restrictions(variation, caplog):
    value = card()
    if variation == "extra":
        value["reason"] = "提炼说明。"
    elif variation == "empty_optional":
        value.pop("limitations")
        value["applicability"] = []
    elif variation == "string_list":
        value["steps"] = value["steps"][0]
    elif variation == "long_item":
        value["problem_features"] = ["故障特征" * 30]
    elif variation == "extra_item":
        value["steps"].append(None)
    raw = json.dumps(value, ensure_ascii=False)
    if variation == "fence":
        raw = "```json\n" + raw + "\n```"
    result = json.loads(parse_card_json(raw))
    assert set(result) == {"problem_features", "applicability", "steps", "limitations"}
    assert result["problem_features"] and result["steps"]
    assert not caplog.records


@pytest.mark.parametrize("variation", ["extra", "missing_scope", "duplicate_indexes"])
def test_memory_review_ignores_metadata_and_redundant_indexes(variation):
    source = usage()
    value = review(source)
    if variation == "extra":
        value["reason"] = "已核对引用。"
    elif variation == "missing_scope":
        value.pop("scope_terms")
    else:
        value["evidence_indexes"] = [0, 0]
    assert validate(value, source) == review(source)


@pytest.mark.parametrize("variation", ["extra", "missing_note", "unused_effective"])
def test_legacy_followup_draft_uses_only_fields_needed_for_intent(tmp_path, variation, caplog):
    payload = {"intent": "FOLLOWUP", "effective_problem_text": None, "change_note": "解释已有报告。"}
    if variation == "extra":
        payload["reason"] = "无需新建诊断。"
    elif variation == "missing_note":
        payload.pop("change_note")
    else:
        payload["effective_problem_text"] = "模型复述，不用于修改原问题。"
    backend = _Backend(json.dumps(payload))
    engine = ClaudeIntakeEngine("unused", workspace_root=tmp_path, backend=backend)
    result = engine.prepare_continuation(source_problem_text="原问题。", text="解释报告。")
    assert result["intent"] == "FOLLOWUP" and result["effective_problem_text"] == "原问题。"
    assert set(result) == {"intent", "effective_problem_text", "change_note"}
    assert len(backend.calls) == 1 and not caplog.records


@pytest.mark.parametrize("correction", [False, True])
def test_declined_memory_finishes_without_exception_log_or_retry(tmp_path, caplog, correction):
    caplog.set_level(logging.INFO)
    class Handler:
        calls = 0

        def extract(self, source, *, cancellation):
            self.calls += 1
            return None

    source = task(**({"source_kind": "CORRECTION"} if correction else {}))
    store, backend, handler = Store([source]), Backend("null"), Handler()
    consumer = worker(tmp_path, store, backend, correction_handler=handler)
    assert not consumer.run_once()
    assert not consumer.run_once()
    assert not store.finished and store.failed == [source["task_id"]]
    assert (len(backend.calls), handler.calls) == ((0, 1) if correction else (1, 0))
    assert not caplog.records


@pytest.mark.parametrize("value", [{}, {"problem_features": ["请求排队"]}, {"steps": ["核对队列长度"]}])
def test_unusable_memory_content_finishes_without_exception_log_or_retry(tmp_path, caplog, value):
    caplog.set_level(logging.INFO)
    source = task()
    store, backend = Store([source]), Backend(json.dumps(value))
    consumer = worker(tmp_path, store, backend)
    assert not consumer.run_once()
    assert not consumer.run_once()
    assert store.failed == [source["task_id"]] and not store.finished
    assert len(backend.calls) == 1 and not caplog.records


@pytest.mark.parametrize("origin", ["FOLLOWUP", "FEEDBACK_COMMENT"])
@pytest.mark.parametrize("value", [None, {}, {"problem_features": ["请求排队"]}, {"steps": ["核对队列长度"]}])
def test_unusable_correction_card_finishes_without_exception_log_or_retry(system, tmp_path, caplog, origin, value):
    source = prepare_logged_task(system) if origin == "FOLLOWUP" else feedback_task(system)
    output = json.loads(response() if origin == "FOLLOWUP" else analysis_response())
    output["card"] = value
    backend = _Backend(json.dumps(output))
    handler = CorrectionMemoryHandler(system.service, "claude", backend=backend)
    store = Store([{**source, "status": "PENDING"}])
    consumer = worker(tmp_path, store, Backend(), correction_handler=handler)
    caplog.clear()
    caplog.set_level(logging.INFO)
    assert not consumer.run_once()
    assert not consumer.run_once()
    assert store.failed == [source["task_id"]] and not store.finished
    assert len(backend.calls) == 1 and not caplog.records
