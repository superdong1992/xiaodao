"""A single continuation worker must not delay ordinary Agent intake."""
from __future__ import annotations

import threading

import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.contracts import CancellationReason
from problem_locator.continuation.models import CreateDraftRequest
from tests.deterministic.unit.agent.test_report_continuation import OWNER, create, system


def _result(kwargs):
    return {"intent": "CONTINUE", "effective_problem_text": kwargs["source_problem_text"],
            "change_note": "重新核对原问题。"}


def test_slow_preparation_does_not_block_new_conversation_intake(system):
    s = system
    entered, release, advanced = threading.Event(), threading.Event(), threading.Event()
    selected = []

    def prepare(**kwargs):
        s.engine.calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        return _result(kwargs)

    def advance(cid):
        if selected and cid == selected[0]:
            advanced.set()
        return False

    s.engine.prepare_continuation = prepare
    s.agent._advance_guarded = advance
    create(s, text="重新核对原问题。")
    s.agent.start()
    try:
        assert entered.wait(2)
        ordinary = s.agent.create_conversation("ordinary", owner_key=OWNER)
        selected.append(ordinary.conversation_id)
        s.agent.send_message(ordinary.conversation_id, "question", "另一台设备无法连接。", owner_key=OWNER)
        assert advanced.wait(2), "普通会话必须在草稿模型尚未完成时开始推进"
        assert not release.is_set() and len(s.engine.calls) == 1
    finally:
        release.set()
        assert s.agent.shutdown(2)


def test_slow_ordinary_intake_does_not_block_preparation(system):
    s = system
    entered, release, prepared = threading.Event(), threading.Event(), threading.Event()
    ordinary = s.agent.create_conversation("ordinary", owner_key=OWNER)
    s.agent.send_message(ordinary.conversation_id, "question", "另一台设备无法连接。", owner_key=OWNER)

    def advance(cid):
        if cid == ordinary.conversation_id:
            entered.set()
            assert release.wait(5)
        return False

    def prepare(**kwargs):
        s.engine.calls.append(kwargs)
        prepared.set()
        return _result(kwargs)

    s.agent._advance_guarded = advance
    s.engine.prepare_continuation = prepare
    s.agent.start()
    try:
        assert entered.wait(2)
        create(s, text="重新核对原问题。")
        assert prepared.wait(2), "普通 INTAKE 等待时，草稿整理也应能独立启动"
        assert not release.is_set() and len(s.engine.calls) == 1
    finally:
        release.set()
        assert s.agent.shutdown(2)


def test_manual_preparation_is_bounded_and_agent_run_once_never_calls_it(system):
    s = system
    entered, release = threading.Event(), threading.Event()
    first = create(s, text="重新核对原问题。")
    second = s.service.create(s.cid, s.rid,
        CreateDraftRequest(request_id="second-draft", text="重新核对原问题。"), owner_key=OWNER)

    def prepare(**kwargs):
        s.engine.calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        return _result(kwargs)

    s.engine.prepare_continuation = prepare
    assert not s.agent.run_once(s.cid)
    assert s.engine.calls == []
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(s.service.run_once(s.cid)))
    worker.start()
    try:
        assert entered.wait(2)
        assert not s.service.run_once(s.cid)
        assert not s.agent.run_once(s.cid)
        assert len(s.engine.calls) == 1
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and outcome == [True]
    assert s.service.run_once(s.cid)
    assert len(s.engine.calls) == 2
    assert not s.service.run_once(s.cid)
    assert {s.service.get(s.cid, item.draft_id, owner_key=OWNER).status
            for item in (first, second)} == {"DRAFT"}


@pytest.mark.parametrize("action", ["shutdown", "delete"])
def test_background_preparation_keeps_cancellation_and_shutdown(system, action):
    s = system
    entered, cancelled = threading.Event(), threading.Event()
    signals = []

    def prepare(**kwargs):
        signal = kwargs["cancellation"]
        signals.append(signal)
        entered.set()
        if signal.wait(5):
            cancelled.set()
        return _result(kwargs)

    s.engine.prepare_continuation = prepare
    draft = create(s, text="重新核对原问题。")
    s.agent.start()
    try:
        assert entered.wait(2)
        if action == "delete":
            s.agent.delete_conversation(s.cid, owner_key=OWNER)
            assert cancelled.wait(2)
        assert s.agent.shutdown(2)
        assert cancelled.is_set()
        expected = CancellationReason.USER_CANCEL if action == "delete" else CancellationReason.SERVICE_SHUTDOWN
        assert signals[0].reason == expected
        assert not s.agent._continuation_thread.is_alive()
        assert s.agent._active_runs == {}
        if action == "delete":
            with pytest.raises(AgentStoreError) as missing:
                s.service.get(s.cid, draft.draft_id, owner_key=OWNER)
            assert missing.value.status_code == 404
        else:
            assert s.service.get(s.cid, draft.draft_id, owner_key=OWNER).status == "FAILED"
            assert not s.service.run_once(s.cid)
    finally:
        assert s.agent.shutdown(2)
