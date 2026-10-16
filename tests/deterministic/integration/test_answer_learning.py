"""Real service/store/worker composition; only model execution is replaced."""
from __future__ import annotations

from problem_locator.memory.corrections import CorrectionMemoryHandler
from problem_locator.memory.extraction import MemoryExtractionWorker
from problem_locator.memory.models import FeedbackSource
from problem_locator.memory.retrieval import ExperienceRetriever
from problem_locator.memory.service import FeedbackService
from tests.deterministic.unit.agent.test_followup_corrections import (
    LOG, correct, enable_memory, response,
)
from tests.deterministic.unit.agent.test_report_followup import (
    Backend, OWNER, make_logs, system, view,
)


def test_disliked_report_to_checked_correction_to_recall_with_independent_feedback(system):
    state = system
    memory = enable_memory(state)
    state.agent.store.memory_store = memory
    state.agent.followups = state.service
    feedback = FeedbackService(state.agent, memory, memory_enabled=True)
    make_logs(state, content=LOG)
    assert state.service.observe_report(state.cid, state.rid)
    assert state.service.snapshot_worker.run_once()
    original = state.source.report_markdown
    source = FeedbackSource(case_id=state.source.case_id, source_job_id=state.source.source_job_id,
        skill_name=state.source.skill_name, problem_text=state.source.problem_text,
        report_markdown=original, report_sha256=state.source.report_sha256)
    memory.put_feedback(state.cid, state.rid, owner_key=OWNER, request_id="report-down",
        rating="DISLIKE", source=source)
    submitted = correct(state)
    assert state.service.worker.run_once()
    answer = view(state).items[-1]
    assert answer.status == "COMPLETED" and len(state.backend.calls) == 1
    reviewer = Backend(response())
    handler = CorrectionMemoryHandler(state.service, "claude", backend=reviewer)
    consumer = MemoryExtractionWorker(memory, "unused", workspace_root=state.service.layout.workspaces,
        backend=Backend(), correction_handler=handler)
    assert consumer.run_once()
    assert not consumer.run_once() and len(reviewer.calls) == 1
    selected = ExperienceRetriever(memory).select("generic-test", "连接池请求排队，队列溢出错误")
    assert selected is not None and selected.source_kind == "CORRECTION"
    assert memory.get_feedback(state.cid, state.rid, owner_key=OWNER, can_rate=True).rating == "DISLIKE"
    assert feedback.get_followup_feedback(state.cid, state.rid, submitted.followup_id, owner_key=OWNER).rating is None
    assert state.service.store._source(state.service.store.snapshot(state.rid)).report_markdown == original
    assert view(state).items[-1].model_dump() == answer.model_dump()
    feedback.put_followup_feedback(state.cid, state.rid, submitted.followup_id, "answer-up", "LIKE", owner_key=OWNER)
    down = feedback.put_followup_feedback(state.cid, state.rid, submitted.followup_id, "answer-down", "DISLIKE", owner_key=OWNER)
    assert feedback.put_followup_feedback(state.cid, state.rid, submitted.followup_id, "answer-up", "LIKE", owner_key=OWNER) == down
    assert memory.active_cards("generic-test") == []
    feedback.put_followup_feedback(state.cid, state.rid, submitted.followup_id, "answer-relike", "LIKE", owner_key=OWNER)
    assert not consumer.run_once() and len(reviewer.calls) == 1
    assert len(memory.active_cards("generic-test")) == 1
    state.agent.store.request_delete(state.cid, owner_key=OWNER)
    assert memory.active_cards("generic-test") == []
    assert not consumer.run_once()


def test_delete_during_correction_model_cannot_publish_or_repeat(system):
    state = system
    memory = enable_memory(state)
    state.agent.store.memory_store = memory
    make_logs(state, content=LOG)
    assert state.service.observe_report(state.cid, state.rid)
    assert state.service.snapshot_worker.run_once()
    correct(state)
    assert state.service.worker.run_once()
    reviewer = Backend(response(), action=lambda _: state.agent.store.request_delete(state.cid, owner_key=OWNER))
    consumer = MemoryExtractionWorker(memory, "unused", workspace_root=state.service.layout.workspaces,
        backend=Backend(), correction_handler=CorrectionMemoryHandler(state.service, "claude", backend=reviewer))
    assert not consumer.run_once()
    assert not consumer.run_once() and len(reviewer.calls) == 1
    assert memory.active_cards("generic-test") == []
    with state.repository.database_read() as db:
        assert db.execute("SELECT status,context_json,evidence_json,card_json FROM memory_tasks").fetchone() == (
            "DELETED", None, None, None)
