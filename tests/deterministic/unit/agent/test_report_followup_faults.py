"""Deterministic fault boundaries for the independent report-followup flow."""
from __future__ import annotations

import errno
import sqlite3
import threading
from dataclasses import replace

import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.contracts.enums import ErrorCode, ExecutionStage
from problem_locator.followup import snapshots as snapshot_module
from problem_locator.followup import worker as worker_module
from problem_locator.followup.service import ReportFollowupService
from problem_locator.runtime.failures import RuntimeExecutionError, runtime_failure
from tests.deterministic.unit.agent.test_report_followup import (
    OWNER, make_logs, original_rows, submit, system, view,
)


@pytest.mark.parametrize("failure_kind", ["model_failure", "backend_timeout"])
def test_model_failure_and_timeout_have_safe_terminal_state_without_retry(system, failure_kind):
    before = original_rows(system)
    failure = (RuntimeError("model stderr /private/token") if failure_kind == "model_failure" else
        runtime_failure(stage=ExecutionStage.BACKEND_EXECUTE, code=ErrorCode.BACKEND_TIMEOUT,
            message="timeout stderr /private/token", retryable=True))
    if failure_kind == "backend_timeout":
        assert isinstance(failure, RuntimeExecutionError)
        assert failure.failure.code is ErrorCode.BACKEND_TIMEOUT

    def fail_model(invocation):
        assert invocation["resource_limits"].wall_time_seconds == 300
        assert invocation["backend_phase"] == "REPORT_FOLLOWUP"
        raise failure

    system.backend.action = fail_model
    receipt = submit(system)
    assert not system.service.worker.run_once()
    answer = view(system).items[0]
    assert answer.status == "FAILED" and answer.answer_markdown is None
    assert answer.failure.code == "AGENT_FOLLOWUP_FAILED"
    assert "/private" not in answer.failure.message and "stderr" not in answer.failure.message
    assert submit(system) == receipt
    assert not system.service.worker.run_once()
    assert len(system.backend.calls) == 1
    assert system.service.list_events(system.cid, system.rid, owner_key=OWNER).stream_closed
    assert original_rows(system) == before


def test_snapshot_copy_enospc_preserves_report_and_allows_report_only_answer(system, monkeypatch):
    original, job = make_logs(system)
    before = original_rows(system)
    original_bytes = (original / "inputs/logs/source.log").read_bytes()
    report = system.source.report_markdown
    copies = []

    def disk_full(source, size, digest, *, destination, cancellation):
        copies.append((source, destination))
        assert destination is not None
        raise OSError(errno.ENOSPC, "no space /private/log-copy")

    assert system.service.observe_report(system.cid, system.rid)
    with monkeypatch.context() as patch:
        patch.setattr(snapshot_module, "_stream_verified", disk_full)
        assert not system.service.snapshot_worker.run_once()
    assert len(copies) == 1
    assert view(system).snapshot_status == "FAILED" and view(system).can_ask
    assert not (job / "followup-inputs").exists()
    assert (original / "inputs/logs/source.log").read_bytes() == original_bytes
    assert system.source.report_markdown == report
    assert original_rows(system) == before and not system.backend.calls
    submit(system)
    assert system.service.worker.run_once()
    answer = view(system).items[0]
    assert answer.status == "COMPLETED" and answer.context_mode == "REPORT_ONLY"
    assert "未重新核对原始日志" in answer.answer_markdown
    assert len(system.backend.calls) == 1 and original_rows(system) == before


def test_worker_input_enospc_fails_before_model_and_keeps_original_tables(system, monkeypatch):
    before = original_rows(system)
    receipt = submit(system)
    writes = []

    def disk_full(path, raw):
        writes.append(path)
        raise OSError(errno.ENOSPC, "no space /private/followup-inputs")

    with monkeypatch.context() as patch:
        patch.setattr(worker_module, "write_new", disk_full)
        assert not system.service.worker.run_once()
    assert len(writes) == 1
    answer = view(system).items[0]
    assert answer.status == "FAILED" and answer.failure.code == "AGENT_FOLLOWUP_FAILED"
    assert answer.answer_markdown is None and "/private" not in answer.failure.message
    assert submit(system) == receipt
    assert not system.service.worker.run_once() and not system.backend.calls
    assert original_rows(system) == before


def test_two_runs_share_total_snapshot_quota_before_second_copy(system):
    original, first_job = make_logs(system, content=b"x" * 12_000)
    first_source = system.source
    second = system.agent.store.create_conversation("second-report", owner_key=OWNER)
    second_job_id = "00000000-0000-0000-0000-000000000012"
    second_source = replace(first_source, source_job_id=second_job_id)
    second_workspace = system.repository.layout.workspaces / second_job_id
    (second_workspace / "inputs/logs").mkdir(parents=True)
    (second_workspace / "inputs/logs/source.log").write_bytes((original / "inputs/logs/source.log").read_bytes())
    raw_manifest = (original / "inputs/generic_logs.json").read_bytes()
    (second_workspace / "inputs/generic_logs.json").write_bytes(raw_manifest)
    second_job = system.repository.layout.jobs / second_job_id
    second_job.mkdir()
    (second_job / "generic_logs.json").write_bytes(raw_manifest)
    system.service._source = lambda cid, rid: first_source if rid == system.rid else second_source
    system.service.snapshot_max_bytes = 20_000
    system.service.snapshot_total_bytes = 20_000
    before = original_rows(system)
    assert system.service.observe_report(system.cid, system.rid)
    assert system.service.snapshot_worker.run_once()
    first = system.service.store.snapshot(system.rid)
    assert 12_000 < first["reserved_bytes"] < 20_000
    assert system.service.observe_report(second.conversation_id, second.run_id)
    assert not system.service.snapshot_worker.run_once()
    rejected = system.service.store.snapshot(second.run_id)
    assert rejected["status"] == "FAILED" and rejected["reserved_bytes"] == 0
    assert not (second_job / "followup-inputs.pending").exists()
    assert not (second_job / "followup-inputs").exists()
    assert system.service.store.snapshot(system.rid) == first
    assert (first_job / "followup-inputs/inputs/logs/log-000001.log").read_bytes() == b"x" * 12_000
    system.service.submit(second.conversation_id, second.run_id, "second-question", "解释这份报告", owner_key=OWNER)
    assert system.service.worker.run_once()
    answer = system.service.get(second.conversation_id, second.run_id, owner_key=OWNER).items[0]
    assert answer.status == "COMPLETED" and answer.context_mode == "REPORT_ONLY"
    assert original_rows(system) == before


def test_service_disable_and_reenable_preserves_history_and_never_restarts_old_work(system, monkeypatch):
    before = original_rows(system)
    completed_receipt = submit(system)
    assert system.service.worker.run_once()
    completed_answer = view(system).items[0]
    queued_receipt = submit(system, "queued-before-disable")
    assert system.service.shutdown(0)

    disabled = ReportFollowupService(system.agent, "claude", enabled=False, backend=system.backend)
    disabled._source = lambda cid, rid: system.source
    try:
        disabled.start()
        state = disabled.get(system.cid, system.rid, owner_key=OWNER)
        assert not state.can_ask and state.reason == "DISABLED"
        assert state.items[0] == completed_answer
        assert state.items[1].status == "CANCELLED" and state.active_followup is None
        assert disabled.submit(system.cid, system.rid, "question-1", "为什么得出这个结论？", owner_key=OWNER) == completed_receipt
        with pytest.raises(AgentStoreError) as caught:
            disabled.submit(system.cid, system.rid, "while-disabled", "新的追问", owner_key=OWNER)
        assert caught.value.code == "AGENT_FOLLOWUP_DISABLED"
        assert disabled.stop(system.cid, system.rid, queued_receipt.followup_id, "stop-while-disabled", owner_key=OWNER).status == "CANCELLED"
        assert disabled.list_events(system.cid, system.rid, owner_key=OWNER).stream_closed
        assert not disabled.worker.run_once() and not disabled.snapshot_worker.run_once()
        assert len(system.backend.calls) == 1
    finally:
        assert disabled.shutdown(0)

    reenabled = ReportFollowupService(system.agent, "claude", enabled=True, backend=system.backend)
    reenabled._source = lambda cid, rid: system.source
    finished = threading.Event()
    finish_task = reenabled.store.finish_task

    def record_finish(*args, **kwargs):
        result = finish_task(*args, **kwargs)
        finished.set()
        return result

    monkeypatch.setattr(reenabled.store, "finish_task", record_finish)
    try:
        reenabled.start()
        state = reenabled.get(system.cid, system.rid, owner_key=OWNER)
        assert state.can_ask and state.items[0] == completed_answer and state.items[1].status == "CANCELLED"
        assert reenabled.submit(system.cid, system.rid, "queued-before-disable", "为什么得出这个结论？", owner_key=OWNER) == queued_receipt
        reenabled.submit(system.cid, system.rid, "after-reenable", "重新启用后的新追问", owner_key=OWNER)
        assert finished.wait(5)
        state = reenabled.get(system.cid, system.rid, owner_key=OWNER)
        assert [item.status for item in state.items] == ["COMPLETED", "CANCELLED", "COMPLETED"]
        assert state.items[0] == completed_answer and len(system.backend.calls) == 2
        assert original_rows(system) == before
    finally:
        assert reenabled.shutdown(5)


def test_submit_failure_after_event_write_rolls_back_request_event_and_source(system, monkeypatch):
    before = original_rows(system)
    original_append = system.service.store._append
    notifications = []

    def append_then_fail(db, cid, item, kind):
        sequence = original_append(db, cid, item, kind)
        assert sequence == 1
        assert db.execute("SELECT count(*) FROM agent_followup_events").fetchone()[0] == 1
        raise sqlite3.OperationalError("injected database write failure")

    with monkeypatch.context() as patch:
        patch.setattr(system.service.store, "_append", append_then_fail)
        patch.setattr(system.service.store, "notify", lambda: notifications.append(True))
        with pytest.raises(sqlite3.OperationalError):
            submit(system)
    assert not notifications and not system.backend.calls
    with system.repository.database_read() as db:
        for table in ("agent_followup_snapshots", "agent_followup_tasks", "agent_followup_events"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert system.service.store.request(system.cid, system.rid, "question-1", "为什么得出这个结论？", OWNER) is None
    assert original_rows(system) == before
    receipt = submit(system)
    assert receipt.event_id == 1 and submit(system) == receipt
    assert system.service.worker.run_once() and len(system.backend.calls) == 1
    events = system.service.list_events(system.cid, system.rid, owner_key=OWNER)
    assert [event.sequence for event in events.events] == [1, 2, 3]
    assert original_rows(system) == before


def test_missing_original_log_degrades_to_report_only_without_reparsing(system):
    original, job = make_logs(system)
    (original / "inputs/logs/source.log").unlink()
    before = original_rows(system)
    assert system.service.observe_report(system.cid, system.rid)
    assert not system.service.snapshot_worker.run_once()
    assert view(system).snapshot_status == "FAILED" and view(system).can_ask
    assert not (job / "followup-inputs").exists()
    assert not system.backend.calls
    submit(system)
    assert system.service.worker.run_once()
    answer = view(system).items[0]
    assert answer.context_mode == "REPORT_ONLY" and answer.status == "COMPLETED"
    assert "未重新核对原始日志" in answer.answer_markdown
    assert len(system.backend.calls) == 1 and original_rows(system) == before
