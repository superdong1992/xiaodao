"""Deterministic intake work budgets; elapsed time is evidence, not a gate.

These checks exercise the real Agent store and service with ScriptedIntake. They
do not estimate production model latency: extracting the first message after
ROUTE can add one model call compared with the former create-only path.
"""
from __future__ import annotations

import json
import threading
import time
from unittest.mock import Mock

import pytest

from tests.deterministic.integration.test_website_agent import _post, website


_IDLE_POLLS = 100


def _settled_conversation(website):
    stack, store, engine, service, client = website
    conversation = _post(client, "/api/v1/agent/conversations", {
        "request_id": "performance-create",
    })["conversation_id"]
    _post(client, f"/api/v1/agent/conversations/{conversation}/messages", {
        "request_id": "performance-first-message", "text": "RPC timeout",
    })
    assert service.run_once(conversation)
    assert engine.calls == [], "Case creation must remain deterministic."
    assert stack.scheduler.wait_until_idle(15)
    assert service.run_once(conversation)
    assert len(engine.calls) == 1, "The original text is extracted once after ROUTE."
    state = store.get_intake_state(conversation)
    assert not state["pending"] and not state["pending_commands"]
    assert store.get_conversation(conversation).case_status == "WAITING_INPUT"
    return conversation


@pytest.mark.parametrize("history_count", [1, 199], ids=["short-history", "near-message-limit"])
def test_idle_poll_cost_does_not_load_history_repeat_intake_or_write_state(
    website, monkeypatch, record_property, history_count,
):
    stack, store, engine, service, _client = website
    conversation = _settled_conversation(website)
    covered = []
    for index in range(1, history_count):
        receipt = service.send_message(
            conversation, f"covered-{index}", f"已处理的历史消息 {index}。",
        )
        store.set_message_status(conversation, receipt.message_id, "APPLIED")
        covered.append(receipt.message_id)
    if covered:
        # Build an already covered history using real receipt/status/coverage
        # operations; creating it must not require 198 irrelevant model calls.
        store.finish_intake(conversation, covered)
    before = store.get_conversation(conversation)
    assert len(before.messages) == history_count
    assert len(engine.calls) == 1
    observed: list[str] = []
    calling_thread = threading.get_ident()

    def trace(statement):
        if threading.get_ident() == calling_thread:
            observed.append(" ".join(statement.upper().split()))

    targets = {
        "conversation_history": (store, "get_conversation"),
        "pending_history": (store, "pending_messages"),
        "case_query": (stack.application, "get_case"),
        "model": (engine, "intake"),
        "command": (stack.application, "execute"),
    }
    with monkeypatch.context() as patch:
        spies = {}
        for name, (owner, attribute) in targets.items():
            spies[name] = Mock(wraps=getattr(owner, attribute))
            if owner is stack.application:
                # ApplicationService is a frozen dataclass. Patch its method on
                # the class while the spy retains the original bound callable.
                def forwarded(_self, *args, _spy=spies[name], **kwargs):
                    return _spy(*args, **kwargs)
                patch.setattr(type(owner), attribute, forwarded)
            else:
                patch.setattr(owner, attribute, spies[name])
        with stack.repository.database_read() as database:
            database.set_trace_callback(trace)
        started = time.perf_counter()
        try:
            for _ in range(_IDLE_POLLS):
                # Use normal worker discovery. The one indexed query excludes
                # settled conversations before any per-run registration/read.
                assert not service.run_once()
        finally:
            elapsed = time.perf_counter() - started
            with stack.repository.database_read() as database:
                database.set_trace_callback(None)
        calls = {name: spy.call_count for name, spy in spies.items()}
    assert calls == dict.fromkeys(targets, 0)
    assert len(engine.calls) == 1
    assert len(observed) == _IDLE_POLLS
    assert all(sql.startswith("SELECT ") for sql in observed)
    assert sum("FROM AGENT_CONVERSATIONS" in sql for sql in observed) == _IDLE_POLLS
    assert sum("FROM AGENT_DISPATCHES" in sql for sql in observed) == _IDLE_POLLS
    assert not any(table in sql for sql in observed for table in (
        "FROM AGENT_MESSAGES", "FROM AGENT_ATTACHMENTS", "FROM AGENT_EVENTS", "FROM COMPLETED_CASES",
    ))
    after = store.get_conversation(conversation)
    assert after.model_dump(mode="json") == before.model_dump(mode="json")
    assert after.last_event_id == before.last_event_id
    assert after.updated_at == before.updated_at
    record_property("idle_history_messages", history_count)
    record_property("idle_polls", _IDLE_POLLS)
    record_property("idle_select_count", len(observed))
    record_property("idle_call_counts", json.dumps(calls, sort_keys=True))
    record_property("idle_elapsed_seconds_observed", elapsed)


def test_pending_core_command_replay_does_not_repeat_completed_intake(website, monkeypatch, record_property):
    stack, store, engine, service, _client = website
    conversation = _settled_conversation(website)
    before = store.get_conversation(conversation)
    case_before = stack.application.get_case(before.case_id).case_view
    create = store.get_dispatch(conversation, store.get_run(conversation)["run_id"] + ":create")
    assert create["status"] == "COMPLETED"
    replay_id = conversation + ":performance-replay-create"
    # A received core command is safe to redeliver only with its exact frozen
    # idempotency identity and payload; this does not create a second Case/Job.
    store.record_dispatch(conversation, replay_id, create["payload"])
    assert store.get_intake_state(conversation)["pending_commands"]
    execute = Mock(wraps=stack.application.execute)
    with monkeypatch.context() as patch:
        patch.setattr(type(stack.application), "execute", lambda _self, *args, **kwargs: execute(*args, **kwargs))
        service.run_once()
        assert execute.call_count == 1
        assert not service.run_once()
        assert execute.call_count == 1
    assert len(engine.calls) == 1
    assert store.get_dispatch(conversation, replay_id)["status"] == "COMPLETED"
    assert store.pending_dispatches(conversation) == []
    case_after = stack.application.get_case(before.case_id).case_view
    assert case_after.model_dump(mode="json") == case_before.model_dump(mode="json")
    record_property("pending_command_replays", execute.call_count)
    record_property("additional_intake_calls_for_replay", 0)


def test_pending_dispatch_probe_uses_an_index_with_large_completed_history(website, record_property):
    stack, store, _engine, _service, _client = website
    conversation = _settled_conversation(website)
    run_id = store.get_run(conversation)["run_id"]
    create = store.get_dispatch(conversation, run_id + ":create")
    payload = json.dumps(create["payload"], sort_keys=True)
    result = json.dumps(create["result"], sort_keys=True)
    # Historical dispatch receipts do not need to replay their core commands to
    # exercise the query plan; every row contains a valid frozen command/result.
    with stack.repository.database_transaction() as database:
        database.executemany(
            "INSERT INTO agent_dispatches(dispatch_id,conversation_id,epoch,status,payload,result,run_id) VALUES (?,?,?,?,?,?,?)",
            [(f"performance-history-{index}", conversation, store.runtime_epoch,
              "COMPLETED", payload, result, run_id) for index in range(2000)],
        )
    with stack.repository.database_read() as database:
        plans = list(database.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM agent_dispatches "
            "WHERE conversation_id=? AND run_id=? AND status='PENDING' AND epoch=? LIMIT 1",
            (conversation, run_id, store.runtime_epoch),
        ))
    details = " | ".join(str(row[3]) for row in plans)
    upper = details.upper()
    assert "SEARCH AGENT_DISPATCHES" in upper
    assert "USING COVERING INDEX" in upper or "USING INDEX" in upper
    assert all(fragment in upper for fragment in ("CONVERSATION_ID=?", "RUN_ID=?", "EPOCH=?"))
    # Either the full run index binds status, or the partial index contains
    # only PENDING rows and therefore needs no status key comparison.
    assert "STATUS=?" in upper or "AGENT_DISPATCHES_PENDING" in upper
    assert "SCAN AGENT_DISPATCHES" not in upper
    assert not store.get_intake_state(conversation)["pending_commands"]
    record_property("completed_dispatch_history", 2000)
    record_property("pending_dispatch_query_plan", details)
