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
from problem_locator.followup.models import FollowupRequest
from problem_locator.runtime.failures import RuntimeExecutionError, runtime_failure
from tests.deterministic.unit.agent.test_report_followup import (
    OWNER, complete_historical_task, diagnostic_system, make_logs, original_rows, submit, system, view,
)


@pytest.mark.parametrize("failure_kind", ["model_failure", "backend_timeout"])
def test_dispatch_failure_and_timeout_have_safe_terminal_state_without_retry(system, failure_kind, monkeypatch):
    before = original_rows(system)
    failure = (RuntimeError("dispatch stderr /private/token") if failure_kind == "model_failure" else
        TimeoutError("dispatch timeout stderr /private/token"))
    attempts = []
    def fail_dispatch(task):
        attempts.append(task["followup_id"])
        raise failure
    monkeypatch.setattr(system.service, "dispatch_generic", fail_dispatch)
    receipt = submit(system)
    assert not system.service.worker.run_once()
    item = view(system).items[0]
    assert item.status == "FAILED" and item.answer_markdown is None
    assert item.failure.code == "AGENT_FOLLOWUP_FAILED"
    assert "/private" not in item.failure.message and "stderr" not in item.failure.message
    assert submit(system) == receipt
    assert not system.service.worker.run_once()
    assert len(attempts) == 1 and not system.backend.calls
    assert system.service.list_events(system.cid, system.rid, owner_key=OWNER).stream_closed
    assert original_rows(system) == before


def test_snapshot_copy_enospc_preserves_report_and_allows_report_only_answer(system, monkeypatch):
    original, job = make_logs(system)
    before = original_rows(system)
    original_bytes = (original / "inputs/logs/source.log").read_bytes()
    report = system.source.report_markdown
    copies = []

    def disk_full(source, size, digest, *, destination, cancellation, capacity=None):
        copies.append((source, destination))
        assert destination is not None
        raise OSError(errno.ENOSPC, "no space /private/log-copy")

    assert system.service.store.enqueue_snapshot(system.cid, system.rid, system.source)
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
    assert complete_historical_task(system, "历史回答：未重新核对原始日志。")
    answer = view(system).items[0]
    assert answer.status == "COMPLETED" and answer.context_mode == "REPORT_ONLY"
    assert "未重新核对原始日志" in answer.answer_markdown
    assert not system.backend.calls and original_rows(system) == before


def test_handoff_storage_enospc_fails_without_a_model_or_orphan_run(diagnostic_system, monkeypatch):
    s = diagnostic_system
    before = original_rows(s)
    receipt = submit(s)
    writes = []
    original_save = s.store._save
    def disk_full(db, body):
        if body["run_id"] != s.rid:
            writes.append(body["run_id"])
            raise OSError(errno.ENOSPC, "no space /private/continuation")
        return original_save(db, body)
    monkeypatch.setattr(s.store, "_save", disk_full)
    assert not s.service.worker.run_once()
    assert len(writes) == 1
    item = view(s).items[0]
    assert item.status == "FAILED" and item.failure.code == "AGENT_FOLLOWUP_FAILED"
    assert item.answer_markdown is None and item.diagnosis_run_id is None
    assert "/private" not in item.failure.message
    assert submit(s) == receipt and not s.service.worker.run_once()
    assert not s.backend.calls and original_rows(s) == before


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
    assert system.service.store.enqueue_snapshot(system.cid, system.rid, system.source)
    assert system.service.snapshot_worker.run_once()
    first = system.service.store.snapshot(system.rid)
    assert 12_000 < first["reserved_bytes"] < 20_000
    assert system.service.store.enqueue_snapshot(second.conversation_id, second.run_id, second_source)
    assert not system.service.snapshot_worker.run_once()
    rejected = system.service.store.snapshot(second.run_id)
    assert rejected["status"] == "FAILED" and rejected["reserved_bytes"] == 0
    assert not (second_job / "followup-inputs.pending").exists()
    assert not (second_job / "followup-inputs").exists()
    assert system.service.store.snapshot(system.rid) == first
    assert (first_job / "followup-inputs/inputs/logs/log-000001.log").read_bytes() == b"x" * 12_000
    system.service.store.submit(second.conversation_id, second.run_id,
        FollowupRequest(request_id="second-question", text="解释这份报告"), second_source, OWNER)
    assert complete_historical_task(system, "历史回答：未重新核对原始日志。")
    answer = system.service.get(second.conversation_id, second.run_id, owner_key=OWNER).items[0]
    assert answer.status == "COMPLETED" and answer.context_mode == "REPORT_ONLY"
    assert original_rows(system) == before


def test_service_disable_and_reenable_preserves_history_and_never_restarts_old_work(system):
    before = original_rows(system)
    completed = submit(system)
    assert complete_historical_task(system, "升级前的历史回答。")
    saved = view(system).items[0]
    queued = submit(system, "queued-before-disable")
    system.service.shutdown(0)
    disabled = ReportFollowupService(system.agent, "claude", enabled=False, backend=system.backend)
    disabled._source = lambda *args: system.source
    try:
        disabled.start()
        state = disabled.get(system.cid, system.rid, owner_key=OWNER)
        assert not state.can_ask and state.reason == "DISABLED"
        assert state.items[0] == saved and state.items[1].status == "CANCELLED"
        assert disabled.submit(system.cid, system.rid, "question-1", "为什么得出这个结论？", owner_key=OWNER) == completed
        with pytest.raises(AgentStoreError) as error:
            disabled.submit(system.cid, system.rid, "disabled", "新追问", owner_key=OWNER)
        assert error.value.code == "AGENT_FOLLOWUP_DISABLED"
        assert not disabled.worker.run_once()
    finally:
        disabled.shutdown(0)
    reenabled = ReportFollowupService(system.agent, "claude", enabled=True, backend=system.backend)
    reenabled._source = lambda *args: system.source
    try:
        state = reenabled.get(system.cid, system.rid, owner_key=OWNER)
        assert state.can_ask and state.items[0] == saved and state.items[1].status == "CANCELLED"
        assert reenabled.submit(system.cid, system.rid, "queued-before-disable", "为什么得出这个结论？", owner_key=OWNER) == queued
        assert not reenabled.worker.run_once()
        receipt = reenabled.submit(system.cid, system.rid, "after-reenable", "新追问", owner_key=OWNER)
        assert receipt.status == "ACCEPTED"
        reenabled.stop(system.cid, system.rid, receipt.followup_id, "stop-new", owner_key=OWNER)
        assert not reenabled.worker.run_once()
        assert original_rows(system) == before and not system.backend.calls
    finally:
        reenabled.shutdown(0)


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
    assert complete_historical_task(system, "历史回答：未重新核对原始日志。") and not system.backend.calls
    events = system.service.list_events(system.cid, system.rid, owner_key=OWNER)
    assert [event.sequence for event in events.events] == [1, 2, 3]
    assert original_rows(system) == before


def test_missing_original_log_degrades_to_report_only_without_reparsing(system):
    original, job = make_logs(system)
    (original / "inputs/logs/source.log").unlink()
    before = original_rows(system)
    assert system.service.store.enqueue_snapshot(system.cid, system.rid, system.source)
    assert not system.service.snapshot_worker.run_once()
    assert view(system).snapshot_status == "FAILED" and view(system).can_ask
    assert not (job / "followup-inputs").exists()
    assert not system.backend.calls
    submit(system)
    assert complete_historical_task(system, "历史回答：未重新核对原始日志。")
    answer = view(system).items[0]
    assert answer.context_mode == "REPORT_ONLY" and answer.status == "COMPLETED"
    assert "未重新核对原始日志" in answer.answer_markdown
    assert not system.backend.calls and original_rows(system) == before
