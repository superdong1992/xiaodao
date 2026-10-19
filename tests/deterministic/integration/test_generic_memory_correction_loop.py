"""A real stored feedback correction changes the next Generic diagnostic prompt."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from problem_locator.memory.corrections import CorrectionMemoryHandler
from problem_locator.memory.extraction import MemoryExtractionWorker, parse_card_json
from problem_locator.memory.models import FeedbackSource
from problem_locator.memory.retrieval import ExperienceRetriever
from problem_locator.memory.service import FeedbackService
from problem_locator.memory.store import MemoryStore
from tests.deterministic.contracts.fakes import InMemoryCancellationSignal
from tests.deterministic.unit.agent.test_report_followup import Backend, OWNER, system
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    _GenericRuntimeBackend, _generic_aggregate, _generic_runtime_fixture, _generic_v2_result_bytes,
)

REPORT = "# 诊断报告\n支付后页面仍显示处理中，需要核查订单状态同步。\n"
ANALYSIS = "核对支付平台的交易回执确认交易成功；检查订单状态同步队列发现回写滞后。\n恢复状态回写后，订单页面显示成功。"
OLD = {"problem_features": ["订单支付", "页面处理中"], "applicability": ["刷新后仍未恢复"],
    "steps": ["仅凭页面状态判定支付失败"], "limitations": ["需要本次证据支持"]}
FIXED = {**OLD, "steps": ["检查支付回执和订单状态回写是否一致", "区分状态同步延迟与页面缓存"],
    "limitations": ["不能仅凭页面状态推断支付失败"]}


def prepare(system, tmp_path):
    memory = MemoryStore(system.repository, system.clock)
    system.agent.memory_store = system.agent.store.memory_store = memory
    system.service.store.memory_store = memory
    retriever = ExperienceRetriever(memory)
    backend = _GenericRuntimeBackend(None, v2_result_bytes=_generic_v2_result_bytes(REPORT))
    (tmp_path / "first-runtime").mkdir()
    runtime, job, state, records = _generic_runtime_fixture(tmp_path / "first-runtime", backend,
        experience_retriever=retriever)
    system.source = replace(system.source, case_id=job.case_id, source_job_id=job.job_id,
        skill_name=job.generic_skill_name, problem_text=job.generic_problem_text,
        report_markdown=REPORT, report_sha256=hashlib.sha256(REPORT.encode()).hexdigest())
    with system.repository.database_transaction() as db:
        db.execute("UPDATE agent_conversation_runs SET case_id=? WHERE run_id=?", (job.case_id, system.rid))
    old = system.agent.store.create_conversation("historical-answer", owner_key=OWNER)
    old_case = str(uuid.uuid5(uuid.NAMESPACE_URL, "memory-correction-history-case"))
    source = FeedbackSource(case_id=old_case,
        source_job_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "memory-correction-history-job")),
        skill_name=job.generic_skill_name, problem_text=job.generic_problem_text,
        report_markdown="# 历史报告\n需要分析支付后的页面状态。", report_sha256="")
    source = replace(source, report_sha256=hashlib.sha256(source.report_markdown.encode()).hexdigest())
    with system.repository.database_transaction() as db:
        db.execute("UPDATE agent_conversation_runs SET case_id=? WHERE run_id=?", (old_case, old.run_id))
    memory.put_feedback(old.conversation_id, old.run_id, owner_key=OWNER,
        request_id="like-history", rating="LIKE", source=source)
    original = memory.claim_task()
    assert memory.finish_task(original["task_id"], json.dumps(OLD, ensure_ascii=False))
    selected = retriever.select(job.generic_skill_name, job.generic_problem_text)
    assert selected.card_id == original["task_id"]
    target = FeedbackSource(case_id=job.case_id, source_job_id=job.job_id,
        skill_name=job.generic_skill_name, problem_text=job.generic_problem_text,
        report_markdown=REPORT, report_sha256=system.source.report_sha256)
    feedback = FeedbackService(system.agent, memory, memory_enabled=True)
    feedback._source = lambda cid, rid: target
    return SimpleNamespace(memory=memory, retriever=retriever, runtime=runtime, job=job,
        backend=backend, records=records, selected=selected, feedback=feedback)


def rows(memory, query, args=()):
    with memory.repository.database_read() as db:
        return db.execute(query, args).fetchall()


@pytest.mark.parametrize("relation", ["GLOBAL_ERROR", "NOT_APPLICABLE"])
def test_feedback_correction_reaches_the_next_generic_diagnosis(system, tmp_path, relation):
    state = prepare(system, tmp_path)
    first = state.runtime.execute(state.job, InMemoryCancellationSignal())
    assert first.job_outcome.error is None
    assert first.job_outcome.payload.report_markdown == REPORT
    assert len(state.backend.calls) == 1
    assert state.selected.reference_text in state.backend.calls[0]["prompt"]
    assert rows(state.memory, "SELECT job_id,card_id,card_sha256 FROM memory_usages") == [
        (state.job.job_id, state.selected.card_id, state.selected.card_sha256)]

    vote = state.feedback.put_feedback(system.cid, system.rid, "down-original", "DISLIKE", owner_key=OWNER)
    assert set(vote.model_dump()) == {"schema_version", "conversation_id", "run_id", "can_rate", "rating", "updated_at"}
    assert vote.rating == "DISLIKE" and vote.can_rate
    receipt = state.feedback.post_feedback_comment(system.cid, system.rid, "describe-error",
        text="页面状态不能证明支付失败。", analysis_text=ANALYSIS,
        analysis_reference="payment-review", owner_key=OWNER)
    assert set(receipt.model_dump()) == {"schema_version", "conversation_id", "run_id", "followup_id",
        "comment_id", "request_id", "created_at"}
    assert receipt.followup_id is None
    assert state.feedback.post_feedback_comment(system.cid, system.rid, "describe-error",
        text="页面状态不能证明支付失败。", analysis_text=ANALYSIS,
        analysis_reference="payment-review", owner_key=OWNER) == receipt

    reply = {"adopt": True, "card": FIXED, "evidence": [{"path": "inputs/feedback-analysis.txt",
        "sha256": hashlib.sha256(ANALYSIS.encode()).hexdigest(), "start_line": 1, "end_line": 1,
        "quote": ANALYSIS.splitlines()[0]}], "memory_review": {"card_id": state.selected.card_id,
        "card_sha256": state.selected.card_sha256, "relation": relation, "evidence_indexes": [0],
        "scope_terms": ["支付", "页面"] if relation == "NOT_APPLICABLE" else []}}
    reviewer, ordinary = Backend(json.dumps(reply, ensure_ascii=False)), Backend()
    worker = MemoryExtractionWorker(state.memory, "claude", workspace_root=system.repository.layout.workspaces,
        backend=ordinary, correction_handler=CorrectionMemoryHandler(system.service, "claude", backend=reviewer))
    assert worker.run_once()
    assert not worker.run_once()
    assert len(reviewer.calls) == 1 and ordinary.calls == [] and system.backend.calls == []
    used = json.loads((reviewer.calls[0]["workspace_root"] / "inputs/used-memory.json").read_bytes())
    assert used["card_id"] == state.selected.card_id and used["card"] == OLD
    assert rows(state.memory, "SELECT count(*) FROM agent_followup_tasks") == [(0,)]
    assert rows(state.memory, "SELECT count(*) FROM agent_followup_events") == [(0,)]
    assert state.memory.is_card_active(state.job.generic_skill_name, state.selected) is (relation != "GLOBAL_ERROR")

    later_backend = _GenericRuntimeBackend(None, v2_result_bytes=_generic_v2_result_bytes(REPORT))
    (tmp_path / "later-runtime").mkdir()
    later, job, aggregate, _ = _generic_runtime_fixture(tmp_path / "later-runtime", later_backend,
        experience_retriever=state.retriever)
    job = job.model_copy(update={"job_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "next-memory-diagnosis"))})
    aggregate.aggregate = _generic_aggregate(job)
    second = later.execute(job, InMemoryCancellationSignal())
    assert second.job_outcome.error is None and len(later_backend.calls) == 1
    prompt = later_backend.calls[0]["prompt"]
    assert parse_card_json(json.dumps(FIXED, ensure_ascii=False)) in prompt
    assert parse_card_json(json.dumps(OLD, ensure_ascii=False)) not in prompt
    assert f"{job.generic_problem_text}\n<<<END_RAW_PROBLEM_TEXT>>>" in prompt
    assert len(prompt.encode()) <= job.resource_limits.context_bytes
    assert rows(state.memory, "SELECT count(*) FROM memory_usages") == [(2,)]
    assert state.records.read_audit_bytes(state.job.job_id, "context.txt") == state.backend.calls[0]["prompt"].encode()


@pytest.mark.parametrize("kind", ["budget", "usage-store-failure"])
def test_optional_memory_receipt_failure_or_budget_never_triggers_an_extra_model_call(system, tmp_path, monkeypatch, kind):
    state = prepare(system, tmp_path)
    if kind == "budget":
        executor = state.runtime._generic_locator_executor
        build_prompt = executor.build_prompt
        assets = state.runtime._resolve_assets(state.job)
        padding = " " * (state.job.resource_limits.context_bytes
            - len(build_prompt(state.job, assets).encode()) - 100)
        monkeypatch.setattr(executor, "build_prompt", lambda *args, **kwargs: padding + build_prompt(*args, **kwargs))
    else:
        def fail(*args):
            raise RuntimeError("private database error")
        monkeypatch.setattr(state.memory, "record_usage", fail)
    receipt = state.runtime.execute(state.job, InMemoryCancellationSignal())
    assert receipt.job_outcome.error is None
    assert receipt.job_outcome.payload.report_markdown == REPORT
    assert receipt.job_outcome.payload.report_sha256 == hashlib.sha256(REPORT.encode()).hexdigest()
    assert len(state.backend.calls) == 1 and system.backend.calls == []
    assert rows(state.memory, "SELECT count(*) FROM memory_usages") == [(0,)]
    assert (state.selected.reference_text in state.backend.calls[0]["prompt"]) is (kind != "budget")
