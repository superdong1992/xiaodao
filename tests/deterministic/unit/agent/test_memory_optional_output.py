"""Optional model metadata must not turn reusable memory into a failed task."""
from __future__ import annotations

import json
import logging

import pytest

from problem_locator.memory.corrections import CorrectionMemoryHandler
from problem_locator.memory.extraction import MemoryExtractionWorker
from tests.deterministic.unit.agent.test_feedback_comment_review import analysis_response, feedback_task
from tests.deterministic.unit.agent.test_followup_corrections import prepare_logged_task, response
from tests.deterministic.unit.agent.test_memory_correction_attribution_review import bind_usage
from tests.deterministic.unit.agent.test_report_followup import Backend, system
from tests.deterministic.unit.runtime.test_experience_memory import Store
from tests.deterministic.unit.runtime.test_memory_correction_review import NEW, review, usage, validate


class _ReceiptStore(Store):
    def __init__(self, task):
        super().__init__([{**task, "status": "PENDING"}])
        self.receipts = []

    def finish_task(self, identity, raw, *, evidence_json=None):
        self.receipts.append(json.loads(evidence_json))
        return super().finish_task(identity, raw)


def _source(system, origin):
    return ((prepare_logged_task(system), json.loads(response())) if origin == "FOLLOWUP"
        else (feedback_task(system), json.loads(analysis_response())))


def _worker(system, task, value):
    backend = Backend(json.dumps(value))
    store = _ReceiptStore(task)
    worker = MemoryExtractionWorker(store, "unused", workspace_root=system.repository.layout.workspaces,
        correction_handler=CorrectionMemoryHandler(system.service, "unused", backend=backend))
    return worker, store, backend


@pytest.mark.parametrize("origin", ["FOLLOWUP", "FEEDBACK_COMMENT"])
@pytest.mark.parametrize("variation", [
    "empty_root", "null_root", "missing_adopt", "adopt_false", "missing_card", "null_card", "empty_card",
    "missing_evidence", "empty_evidence", "null_evidence", "missing_memory_review", "null_memory_review", "empty_memory_review",
])
def test_missing_correction_content_finishes_once_without_failure_event(system, caplog, origin, variation):
    task, value = _source(system, origin)
    if "memory_review" in variation:
        recorded, _ = bind_usage(system, task)
        value["memory_review"] = review(recorded)
    if variation == "empty_root":
        value = {}
    elif variation == "null_root":
        value = None
    elif variation == "adopt_false":
        value = {"adopt": False}
    elif variation.startswith("missing_"):
        value.pop(variation.removeprefix("missing_"))
    elif variation.startswith("null_"):
        value[variation.removeprefix("null_")] = None
    else:
        field = variation.removeprefix("empty_")
        value[field] = [] if field == "evidence" else {}
    worker, store, backend = _worker(system, task, value)
    caplog.clear()
    caplog.set_level(logging.INFO)
    assert not worker.run_once()
    assert not worker.run_once()
    assert len(backend.calls) == 1 and store.failed == [task["task_id"]]
    assert not store.finished and not store.receipts and not caplog.records


@pytest.mark.parametrize("origin", ["FOLLOWUP", "FEEDBACK_COMMENT"])
@pytest.mark.parametrize("variation", ["unrelated_missing_indexes", "global_null_scope", "unrelated_null_scope", "missing_identity"])
def test_unused_review_metadata_does_not_block_a_supported_correction(system, caplog, origin, variation):
    task, value = _source(system, origin)
    recorded, _ = bind_usage(system, task)
    relation = "UNRELATED" if variation.startswith("unrelated") else "GLOBAL_ERROR"
    expected = review(recorded, relation)
    value["memory_review"] = dict(expected)
    if variation == "unrelated_missing_indexes":
        value["memory_review"].pop("evidence_indexes")
    elif variation == "missing_identity":
        value["memory_review"].pop("card_id")
        value["memory_review"].pop("card_sha256")
    else:
        value["memory_review"]["scope_terms"] = None
    worker, store, backend = _worker(system, task, value)
    caplog.clear()
    caplog.set_level(logging.INFO)
    assert worker.run_once()
    assert not worker.run_once()
    assert len(backend.calls) == len(store.finished) == 1 and not store.failed
    assert store.receipts[0]["memory_review"] == expected
    assert not caplog.records


@pytest.mark.parametrize("omitted", [("card_id",), ("card_sha256",), ("card_id", "card_sha256")])
def test_review_uses_recorded_identity_when_model_omits_the_echo(omitted):
    recorded = usage()
    expected = review(recorded)
    value = dict(expected)
    for field in omitted:
        value.pop(field)
    assert validate(value, recorded) == expected


@pytest.mark.parametrize("field", ["card_id", "card_sha256"])
def test_review_still_rejects_a_forged_identity_echo(field):
    recorded = usage()
    value = review(recorded)
    value[field] = "00000000-0000-0000-0000-000000000001" if field == "card_id" else "0" * 64
    with pytest.raises(ValueError):
        validate(value, recorded)


@pytest.mark.parametrize("field", ["card_id", "card_sha256", "card_json"])
def test_omitted_echo_does_not_bypass_corrupt_recorded_identity(field):
    recorded = usage()
    value = review(recorded)
    value.pop("card_id")
    value.pop("card_sha256")
    recorded[field] = {"card_id": "invalid", "card_sha256": "0" * 64, "card_json": "{}"}[field]
    with pytest.raises(ValueError):
        validate(value, recorded)


@pytest.mark.parametrize("origin", ["FOLLOWUP", "FEEDBACK_COMMENT"])
@pytest.mark.parametrize("field", ["path", "start_line", "end_line", "quote"])
def test_incomplete_citation_does_not_publish_or_shift_evidence_indexes(system, caplog, origin, field):
    task, value = _source(system, origin)
    value["evidence"].append(dict(value["evidence"][0]))
    value["evidence"][0].pop(field)
    worker, store, backend = _worker(system, task, value)
    caplog.clear()
    caplog.set_level(logging.INFO)
    assert not worker.run_once()
    assert not worker.run_once()
    assert len(backend.calls) == 1 and store.failed == [task["task_id"]]
    assert not store.finished and not store.receipts and not caplog.records


@pytest.mark.parametrize("origin", ["FOLLOWUP", "FEEDBACK_COMMENT"])
def test_citation_hash_echo_is_optional_and_bound_to_verified_manifest(system, caplog, origin):
    task, value = _source(system, origin)
    expected_hash = value["evidence"][0].pop("sha256")
    worker, store, backend = _worker(system, task, value)
    caplog.clear()
    caplog.set_level(logging.INFO)
    assert worker.run_once()
    assert not worker.run_once()
    assert len(backend.calls) == len(store.finished) == 1
    assert store.receipts[0]["evidence"][0]["sha256"] == expected_hash
    assert not store.failed and not caplog.records


@pytest.mark.parametrize("origin", ["FOLLOWUP", "FEEDBACK_COMMENT"])
@pytest.mark.parametrize("quote", ["", " ", "\n\t"])
def test_empty_citation_quote_is_no_usable_evidence(system, caplog, origin, quote):
    task, value = _source(system, origin)
    value["evidence"][0]["quote"] = quote
    worker, store, backend = _worker(system, task, value)
    caplog.clear()
    caplog.set_level(logging.INFO)
    assert not worker.run_once()
    assert not worker.run_once()
    assert len(backend.calls) == 1 and store.failed == [task["task_id"]]
    assert not store.finished and not store.receipts and not caplog.records


@pytest.mark.parametrize("origin", ["FOLLOWUP", "FEEDBACK_COMMENT"])
@pytest.mark.parametrize("field", ["relation", "evidence_indexes", "scope_terms"])
def test_missing_required_review_content_does_not_publish(system, caplog, origin, field):
    task, value = _source(system, origin)
    recorded, _ = bind_usage(system, task)
    value["card"] = NEW
    value["memory_review"] = review(recorded, "NOT_APPLICABLE" if field == "scope_terms" else "GLOBAL_ERROR")
    value["memory_review"].pop(field)
    worker, store, backend = _worker(system, task, value)
    caplog.clear()
    caplog.set_level(logging.INFO)
    assert not worker.run_once()
    assert not worker.run_once()
    assert len(backend.calls) == 1 and store.failed == [task["task_id"]]
    assert not store.finished and not store.receipts and not caplog.records


@pytest.mark.parametrize("field", ["memory_review", "relation", "evidence_indexes", "scope_terms"])
def test_incomplete_review_stays_unpublished_and_preserves_used_history(system, tmp_path, caplog, field):
    from tests.deterministic.integration.test_generic_memory_correction_loop import ANALYSIS, FIXED, prepare, rows
    from tests.deterministic.unit.agent.test_report_followup import OWNER

    state = prepare(system, tmp_path)
    state.memory.record_usage(state.job, state.selected)
    state.feedback.put_feedback(system.cid, system.rid, "down-original", "DISLIKE", owner_key=OWNER)
    state.feedback.post_feedback_comment(system.cid, system.rid, "describe-error",
        text="补充核查结果。", analysis_text=ANALYSIS, analysis_reference="payment-review", owner_key=OWNER)
    value = {"adopt": True, "card": FIXED, "evidence": [{"path": "inputs/feedback-analysis.txt",
        "start_line": 1, "end_line": 1, "quote": ANALYSIS.splitlines()[0]}],
        "memory_review": {"relation": "NOT_APPLICABLE" if field == "scope_terms" else "GLOBAL_ERROR",
            "evidence_indexes": [0], "scope_terms": ["支付", "页面"] if field == "scope_terms" else []}}
    if field == "memory_review":
        value.pop(field)
    else:
        value["memory_review"].pop(field)
    old_rows = rows(state.memory, "SELECT task_id,status,active,superseded,card_sha256 FROM memory_tasks WHERE status='READY'")
    old_revisions = rows(state.memory, "SELECT * FROM memory_revisions")
    backend = Backend(json.dumps(value))
    worker = MemoryExtractionWorker(state.memory, "unused", workspace_root=system.repository.layout.workspaces,
        correction_handler=CorrectionMemoryHandler(system.service, "unused", backend=backend))
    caplog.clear()
    caplog.set_level(logging.INFO)
    assert not worker.run_once()
    assert not worker.run_once()
    assert rows(state.memory, "SELECT status,active,card_json,evidence_json FROM memory_tasks WHERE source_kind='CORRECTION'") == [
        ("FAILED", 0, None, None)]
    assert rows(state.memory, "SELECT task_id,status,active,superseded,card_sha256 FROM memory_tasks WHERE status='READY'") == old_rows
    assert rows(state.memory, "SELECT * FROM memory_revisions") == old_revisions
    assert state.memory.is_card_active(state.job.generic_skill_name, state.selected)
    assert len(backend.calls) == 1 and not caplog.records
