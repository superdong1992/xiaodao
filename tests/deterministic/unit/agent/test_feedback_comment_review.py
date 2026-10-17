"""Standalone negative-feedback reviews never create a chat turn."""
from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from problem_locator.dispatch.cancellation import CancellationController
from problem_locator.memory.corrections import CorrectionMemoryHandler
from problem_locator.memory.extraction import MemoryExtractionWorker
from problem_locator.memory.models import FeedbackSource
from tests.deterministic.unit.agent.test_followup_corrections import CARD, LOG, enable_memory, response
from tests.deterministic.unit.agent.test_report_followup import (
    Backend, OWNER, make_logs, system,
)

ANALYSIS = "检查连接池等待队列，发现 QUEUE_OVERFLOW 与请求超时同时出现。\n修复连接释放逻辑后，队列恢复且同类请求不再超时。"


def feedback_task(system, *, text="原回答遗漏了连接池排队。", analysis=ANALYSIS):
    comment_id = str(uuid.uuid4())
    target = system.source.report_markdown
    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()
    context = {
        "schema_version": 1, "origin": "FEEDBACK_COMMENT",
        "original_problem": system.source.problem_text, "original_report": target,
        "target_answer_markdown": target, "target_answer_sha256": digest,
        "feedback": {"comment_id": comment_id, "text": text, "analysis_text": analysis,
            "analysis_reference": "analysis-record-1" if analysis else None},
        "correction": {"intent": "CORRECTION", "target_followup_id": None},
    }
    return {
        "task_id": str(uuid.uuid4()), "conversation_id": system.cid, "run_id": system.rid,
        "answer_key": "feedback:" + comment_id, "source_kind": "CORRECTION",
        "source_job_id": system.source.source_job_id, "problem_text": system.source.problem_text,
        "report_markdown": target, "report_sha256": digest, "replaces_answer_key": "",
        "context_json": json.dumps(context, ensure_ascii=False),
    }


def analysis_response(*, path="inputs/feedback-analysis.txt", quote=None):
    return json.dumps({"adopt": True, "card": CARD, "evidence": [{
        "path": path, "sha256": hashlib.sha256(ANALYSIS.encode("utf-8")).hexdigest(),
        "start_line": 1, "end_line": 1, "quote": quote or ANALYSIS.splitlines()[0],
    }]}, ensure_ascii=False)


def review(system, task, backend):
    return CorrectionMemoryHandler(system.service, "claude", backend=backend).extract(
        task, cancellation=CancellationController())


@pytest.mark.parametrize("user_text", [None, "原回答遗漏了连接池排队。"])
def test_web_analysis_review_works_without_followups_or_original_logs(system, user_text):
    system.service.enabled = False
    task = feedback_task(system, text=user_text)
    backend = Backend(analysis_response())
    card, receipt = review(system, task, backend)
    assert json.loads(card) == CARD
    proof = json.loads(receipt)
    assert proof["origin"] == "FEEDBACK_COMMENT"
    assert proof["comment_id"] == task["answer_key"].removeprefix("feedback:")
    assert proof["answer_sha256"] == task["report_sha256"]
    assert proof["evidence"][0]["source_kind"] == "WEB_ANALYSIS"
    assert proof["evidence"][0]["analysis_reference"] == "analysis-record-1"
    assert "quote" not in proof["evidence"][0]
    assert ANALYSIS not in card and ANALYSIS not in receipt
    call = backend.calls[0]
    assert len(backend.calls) == 1 and system.backend.calls == []
    assert call["backend_phase"] == "MEMORY_CORRECTION"
    assert call["file_access"] == "read-search"
    assert "管理员身份" in call["prompt"] and "没有新的追问回答" in call["prompt"]
    inputs = call["workspace_root"] / "inputs"
    manifest = json.loads((inputs / "feedback-evidence.json").read_text("utf-8"))
    assert manifest["sources"][0]["source_kind"] == "WEB_ANALYSIS"
    assert (inputs / "feedback-analysis.txt").read_text("utf-8") == ANALYSIS
    context = json.loads((inputs / "correction.json").read_text("utf-8"))
    assert "answer" not in context and "question" not in context
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM agent_followup_tasks").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM agent_followup_events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM agent_followup_snapshots").fetchone()[0] == 0


def test_user_comment_alone_requires_available_log_evidence_before_model_call(system):
    task = feedback_task(system, analysis=None)
    backend = Backend(analysis_response())
    with pytest.raises(ValueError, match="缺少可核对"):
        review(system, task, backend)
    assert backend.calls == []


def test_user_comment_can_use_existing_original_logs_with_followups_disabled(system):
    make_logs(system, content=LOG)
    assert system.service.observe_report(system.cid, system.rid)
    assert system.service.snapshot_worker.run_once()
    system.service.enabled = False
    task = feedback_task(system, analysis=None)
    backend = Backend(response())
    card, receipt = review(system, task, backend)
    assert json.loads(card) == CARD
    proof = json.loads(receipt)
    assert proof["evidence"][0]["source_kind"] == "ORIGINAL_LOG"
    assert "analysis_reference" not in proof["evidence"][0]
    inputs = backend.calls[0]["workspace_root"] / "inputs"
    assert not (inputs / "feedback-analysis.txt").exists()


@pytest.mark.parametrize("field,value", [
    ("target_answer_sha256", "0" * 64),
    ("target_answer_markdown", "不是目标回答"),
    ("original_problem", "其他问题"),
    ("original_report", "其他报告"),
    ("schema_version", 2),
])
def test_feedback_source_drift_is_rejected_before_model_call(system, field, value):
    task = feedback_task(system)
    context = json.loads(task["context_json"])
    context[field] = value
    task["context_json"] = json.dumps(context, ensure_ascii=False)
    backend = Backend(analysis_response())
    with pytest.raises(ValueError, match="来源校验失败"):
        review(system, task, backend)
    assert backend.calls == []


def test_feedback_identity_cannot_point_at_another_comment_or_target(system):
    task = feedback_task(system)
    task["answer_key"] = "feedback:" + str(uuid.uuid4())
    backend = Backend(analysis_response())
    with pytest.raises(ValueError, match="来源校验失败"):
        review(system, task, backend)
    assert backend.calls == []


@pytest.mark.parametrize("result", [
    analysis_response(quote="管理员没有提供过这一条证据"),
    analysis_response(path="inputs/correction.json"),
    analysis_response(path="inputs/report.md"),
    json.dumps({"adopt": False, "card": None, "evidence": []}),
    json.dumps({"adopt": True, "card": CARD, "evidence": []}),
])
def test_unsupported_or_fabricated_analysis_cannot_be_adopted(system, result):
    backend = Backend(result)
    with pytest.raises(ValueError):
        review(system, feedback_task(system), backend)
    assert len(backend.calls) == 1


def test_analysis_input_drift_invalidates_review_result(system):
    def mutate(call):
        path = call["workspace_root"] / "inputs/feedback-analysis.txt"
        path.chmod(0o644)
        path.write_text("后到的资料不能替换本次输入", encoding="utf-8")
    backend = Backend(analysis_response(), action=mutate)
    with pytest.raises(ValueError):
        review(system, feedback_task(system), backend)
    assert len(backend.calls) == 1


def test_conversation_deleted_during_analysis_rejects_late_result(system):
    backend = Backend(analysis_response(), action=lambda call:
        system.agent.store.request_delete(system.cid, owner_key=OWNER))
    with pytest.raises(ValueError, match="已停止"):
        review(system, feedback_task(system), backend)
    assert len(backend.calls) == 1


def test_feedback_reviewer_rejects_unknown_model_launcher_without_calling_it(system):
    system.service.read_search_supported = False
    backend = Backend(analysis_response())
    with pytest.raises(ValueError, match="核对能力"):
        review(system, feedback_task(system), backend)
    assert backend.calls == []

def test_log_and_web_analysis_citations_keep_distinct_provenance(system):
    make_logs(system, content=LOG)
    assert system.service.observe_report(system.cid, system.rid)
    assert system.service.snapshot_worker.run_once()
    payload = json.loads(analysis_response())
    payload["evidence"].extend(json.loads(response())["evidence"])
    backend = Backend(json.dumps(payload, ensure_ascii=False))
    _, receipt = review(system, feedback_task(system), backend)
    evidence = json.loads(receipt)["evidence"]
    assert [item["source_kind"] for item in evidence] == ["WEB_ANALYSIS", "ORIGINAL_LOG"]
    assert "analysis_reference" not in evidence[1]


def test_mismatched_original_snapshot_cannot_be_bypassed_with_web_analysis(system):
    make_logs(system, content=LOG)
    assert system.service.observe_report(system.cid, system.rid)
    assert system.service.snapshot_worker.run_once()
    task = feedback_task(system)
    task["source_job_id"] = str(uuid.uuid4())
    backend = Backend(analysis_response())
    with pytest.raises(ValueError, match="不匹配"):
        review(system, task, backend)
    assert backend.calls == []


def test_stored_feedback_runs_through_worker_without_creating_chat(system):
    memory = enable_memory(system)
    system.service.enabled = False
    source = FeedbackSource(case_id=system.source.case_id, source_job_id=system.source.source_job_id,
        skill_name=system.source.skill_name, problem_text=system.source.problem_text,
        report_markdown=system.source.report_markdown, report_sha256=system.source.report_sha256)
    memory.put_feedback(system.cid, system.rid, owner_key=OWNER, request_id="like-original",
        rating="LIKE", source=source)
    original = memory.claim_task()
    assert memory.finish_task(original["task_id"], json.dumps(CARD, ensure_ascii=False))
    memory.put_feedback(system.cid, system.rid, owner_key=OWNER, request_id="dislike-original",
        rating="DISLIKE", source=source)
    receipt = memory.post_feedback_comment(system.cid, system.rid, owner_key=OWNER,
        request_id="submit-comment", text="原回答遗漏连接池排队。", analysis_text=ANALYSIS,
        analysis_reference="analysis-record-1", source=source)
    assert memory.post_feedback_comment(system.cid, system.rid, owner_key=OWNER,
        request_id="submit-comment", text="原回答遗漏连接池排队。", analysis_text=ANALYSIS,
        analysis_reference="analysis-record-1", source=source) == receipt
    backend = Backend(analysis_response())
    ordinary_backend = Backend()
    worker = MemoryExtractionWorker(memory, "claude", workspace_root=system.repository.layout.workspaces,
        backend=ordinary_backend, correction_handler=CorrectionMemoryHandler(system.service, "claude", backend=backend))
    assert worker.run_once()
    assert not worker.run_once()
    cards = memory.active_cards(system.source.skill_name)
    assert len(cards) == 1 and cards[0]["answer_key"] == "feedback:" + receipt.comment_id
    assert cards[0]["source_kind"] == "CORRECTION"
    assert len(backend.calls) == 1 and ordinary_backend.calls == [] and system.backend.calls == []
    with system.repository.database_read() as db:
        assert db.execute("SELECT active,superseded FROM memory_tasks WHERE task_id=?",
            (original["task_id"],)).fetchone() == (0, 1)
        assert db.execute("SELECT count(*) FROM memory_feedback_comments").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM agent_followup_tasks").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM agent_followup_events").fetchone()[0] == 0
