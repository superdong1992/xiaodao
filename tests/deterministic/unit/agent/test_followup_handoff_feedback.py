"""A diagnostic handoff notice is not an answer eligible for feedback learning."""
from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.followup.models import FollowupSource
from problem_locator.memory.service import FeedbackService
from tests.deterministic.unit.agent.test_answer_feedback_memory import FID
from tests.deterministic.unit.agent.test_generic_feedback import OWNER, system

DIAGNOSIS_RUN = "00000000-0000-0000-0000-000000000099"


def feedback_service(state, *, diagnosis_run_id, correction=False):
    source = FollowupSource(case_id=state.source.case_id,
        source_job_id=state.source.source_job_id, problem_text=state.source.problem_text,
        report_markdown=state.source.report_markdown, report_sha256=state.source.report_sha256,
        source_kind="GENERIC", has_logs=False, occurred_at=state.clock.now(),
        skill_name=state.source.skill_name)
    item = SimpleNamespace(status="COMPLETED", answer_markdown="已开始重新诊断，结果将在新报告中展示。",
        text="请重新核对这项判断。", diagnosis_run_id=diagnosis_run_id)
    metadata = {"intent": "CORRECTION", "target_followup_id": None} if correction else {}
    followups = SimpleNamespace(store=SimpleNamespace(
        learning_source=lambda *args: (source, item, metadata, [])))
    agent = SimpleNamespace(followups=followups, operation_lease=lambda *args, **kwargs: nullcontext())
    return FeedbackService(agent, state.memory, memory_enabled=True)


@pytest.mark.parametrize("correction", [False, True])
def test_handoff_notice_has_no_feedback_or_learning_source(system, correction):
    service = feedback_service(system, diagnosis_run_id=DIAGNOSIS_RUN, correction=correction)
    cid, rid = system.created.conversation_id, system.created.run_id
    view = service.get_followup_feedback(cid, rid, FID, owner_key=OWNER)
    assert view.can_rate is False and view.rating is None
    assert service._followup_source(cid, rid, FID, OWNER) is None
    for rating in ("LIKE", "DISLIKE"):
        with pytest.raises(AgentStoreError) as denied:
            service.put_followup_feedback(cid, rid, FID, rating, rating, owner_key=OWNER)
        assert denied.value.code == "AGENT_FEEDBACK_UNSUPPORTED"
    with pytest.raises(AgentStoreError) as denied:
        service.post_feedback_comment(cid, rid, "handoff-comment", text="请核对日志。",
            analysis_text="后台已有排查记录。", analysis_reference="analysis-1",
            owner_key=OWNER, followup_id=FID)
    assert denied.value.code == "AGENT_FEEDBACK_UNSUPPORTED"
    with system.repository.database_read() as db:
        for table in ("memory_feedback", "memory_feedback_requests", "memory_tasks", "memory_feedback_comments"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_ordinary_answer_still_accepts_independent_negative_feedback_and_analysis(system):
    service = feedback_service(system, diagnosis_run_id=None)
    cid, rid = system.created.conversation_id, system.created.run_id
    assert service.get_followup_feedback(cid, rid, FID, owner_key=OWNER).can_rate
    vote = service.put_followup_feedback(cid, rid, FID, "answer-down", "DISLIKE", owner_key=OWNER)
    receipt = service.post_feedback_comment(cid, rid, "answer-comment", text="遗漏排队现象。",
        analysis_text="监控显示请求排队增长。", analysis_reference="analysis-1",
        owner_key=OWNER, followup_id=FID)
    assert vote.rating == "DISLIKE" and receipt.followup_id == FID
    assert service.get_followup_feedback(cid, rid, FID, owner_key=OWNER) == vote
    with system.repository.database_read() as db:
        row = db.execute("SELECT run_id,answer_key,analysis_text,analysis_reference FROM memory_feedback_comments").fetchone()
        assert tuple(row) == (rid, FID, "监控显示请求排队增长。", "analysis-1")
        assert db.execute("SELECT count(*) FROM memory_tasks").fetchone()[0] == 1
