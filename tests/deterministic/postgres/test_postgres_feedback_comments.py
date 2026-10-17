"""Standalone feedback transaction guarantees on the production database."""
from concurrent.futures import ThreadPoolExecutor

from tests.postgres_helpers import postgres_database_url, postgres_repository
from tests.deterministic.postgres.test_postgres_memory import durable_memory
from tests.deterministic.unit.runtime.test_experience_memory import card_json


def submit(state, request_id="comment"):
    return state.memory.post_feedback_comment(state.created.conversation_id, state.created.run_id,
        owner_key="owner", request_id=request_id, text="连接池等待需要核对。",
        analysis_text="检查线程栈发现等待连接，释放连接后排队恢复。", analysis_reference="web-analysis-1",
        source=state.source)


def test_postgres_comment_concurrency_and_vote_replay(durable_memory):
    state = durable_memory
    state.vote("DISLIKE", "down")
    with ThreadPoolExecutor(max_workers=4) as executor:
        receipts = list(executor.map(lambda _: submit(state), range(4)))
    assert all(row == receipts[0] for row in receipts)
    task = state.memory.claim_task()
    assert task and state.memory.claim_task() is None
    state.vote("LIKE", "up")
    assert submit(state) == receipts[0]
    state.agents.request_delete(state.created.conversation_id, owner_key="owner")
    assert not state.memory.finish_task(task["task_id"], card_json(), evidence_json='{"evidence":[]}')
    with state.repository.database_read() as db:
        assert db.execute("SELECT text,analysis_text,analysis_reference FROM memory_feedback_comments").fetchone() == (None, None, None)


def test_postgres_comment_savepoint_preserves_feedback_on_enqueue_failure(durable_memory, monkeypatch):
    state = durable_memory
    state.vote("DISLIKE", "down")
    original = state.memory._enqueue
    def fail(*args):
        original(*args)
        # A real PG statement failure aborts the savepoint until rolled back.
        args[0].execute("SELECT nonexistent_feedback_column FROM memory_tasks")
    monkeypatch.setattr(state.memory, "_enqueue", fail)
    receipt = submit(state)
    assert submit(state) == receipt
    assert state.memory.claim_task() is None
