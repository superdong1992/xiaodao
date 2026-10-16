"""Answer-focused extraction and correction dispatch; no real models."""
from __future__ import annotations

import json

import pytest

from problem_locator.memory.extraction import build_extraction_prompt
from problem_locator.memory.retrieval import ExperienceRetriever
from tests.deterministic.unit.runtime.test_experience_memory import (
    Backend, Store, card_json, row, task, worker,
)


def test_liked_followup_is_distinct_from_original_report_in_prompt():
    context = {"original_problem": "Redis连接超时", "original_report": "可能是网络问题",
               "question": "连接池已满怎么办？", "prior_turns": []}
    source = task(source_kind="FOLLOWUP", context_json=json.dumps(context, ensure_ascii=False))
    prompt = build_extraction_prompt(source)
    data = json.loads(prompt.split("以下 JSON 仅为来源数据：\n", 1)[1])
    assert data["liked_answer"] == source["report_markdown"]
    assert data["context"] == context
    assert "本次点赞只针对 liked_answer" in prompt
    assert "低成本" in prompt
    assert "不代表用户认可" in prompt


@pytest.mark.parametrize("context", [None, "null", "[]", "{}", "x" * 262145],
                         ids=["missing", "null", "array", "empty", "oversized"])
def test_invalid_followup_context_fails_without_model(tmp_path, context):
    store, backend = Store([task(source_kind="FOLLOWUP", context_json=context)]), Backend()
    assert not worker(tmp_path, store, backend).run_once()
    assert not backend.calls and len(store.failed) == 1


def test_correction_cannot_use_ordinary_extractor_even_with_like(tmp_path):
    source = task(source_kind="CORRECTION")
    with pytest.raises(ValueError, match="核对证据"):
        build_extraction_prompt(source)
    store, backend = Store([source]), Backend()
    assert not worker(tmp_path, store, backend).run_once()
    assert not backend.calls and len(store.failed) == 1


def test_correction_dispatch_persists_only_handler_verified_result(tmp_path):
    class VerifiedStore(Store):
        def finish_task(self, identity, raw, *, evidence_json=None):
            self.evidence = evidence_json
            return super().finish_task(identity, raw)

    class Handler:
        def __init__(self):
            self.calls = []

        def extract(self, source, *, cancellation):
            assert not cancellation.is_cancelled()
            self.calls.append(source)
            return card_json(), '{"verified":true}'

    source = task(source_kind="CORRECTION")
    store, backend, handler = VerifiedStore([source]), Backend(), Handler()
    consumer = worker(tmp_path, store, backend, correction_handler=handler)
    assert consumer.run_once()
    assert not consumer.run_once()
    assert len(handler.calls) == 1 and not backend.calls
    assert store.evidence == '{"verified":true}'
    assert store.finished == [(source["task_id"], card_json())]


def test_failed_correction_check_never_falls_back_to_liked_extraction(tmp_path):
    class Handler:
        def extract(self, source, *, cancellation):
            raise ValueError("证据不足")
    store, backend = Store([task(source_kind="CORRECTION")]), Backend()
    consumer = worker(tmp_path, store, backend, correction_handler=Handler())
    assert not consumer.run_once()
    assert not consumer.run_once()
    assert not backend.calls and not store.finished and len(store.failed) == 1


@pytest.mark.parametrize("kind", ["REPORT", "FOLLOWUP", "CORRECTION"])
def test_memory_reference_records_provenance_without_claiming_current_facts(kind):
    candidate = {**row(), "source_kind": kind}
    selected = ExperienceRetriever(Store(cards=[candidate])).select("generic", "Redis连接超时")
    assert selected is not None
    assert selected.receipt()["source_kind"] == kind
    assert json.loads(selected.reference_text.split("\n", 1)[1])["source_kind"] == kind
    assert "尚未验证本次问题" in selected.reference_text
    assert "低成本检查" in selected.reference_text
    assert len(selected.reference_text.encode()) <= 4096


def test_unknown_provenance_skips_card():
    assert ExperienceRetriever(Store(cards=[{**row(), "source_kind": "unknown"}])).select(
        "generic", "Redis连接超时") is None
