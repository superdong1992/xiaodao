"""Report follow-ups never reopen a Case or rewrite the existing Agent history."""
from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.followup.models import FollowupRequest, FollowupSource
from problem_locator.followup.service import ReportFollowupService
from problem_locator.followup.store import FollowupStore
from problem_locator.memory.store import MemoryStore
from tests.deterministic.unit.storage.test_state_repository import _open

OWNER = "a" * 64
OTHER = "b" * 64
NOW = "2026-09-24T01:00:00.000Z"
CASE = "00000000-0000-0000-0000-000000000001"
JOB = "00000000-0000-0000-0000-000000000011"


class Backend:
    def __init__(self, answer="根据报告，连接池排队是需要继续核实的方向。", action=None):
        self.answer, self.action, self.calls = answer, action, []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        if self.action is not None:
            self.action(kwargs)
        return SimpleNamespace(final_result=self.answer)


@pytest.fixture
def system(tmp_path):
    repository = _open(tmp_path)
    clock = SimpleNamespace(value=NOW)
    clock.now = lambda: clock.value
    agent_store = AgentStore(repository, clock=clock, runtime_epoch="test")
    MemoryStore(repository, clock)
    agent = AgentConversationService(agent_store, SimpleNamespace(), None, repository.layout)
    created = agent_store.create_conversation("create", owner_key=OWNER)
    backend = Backend()
    service = ReportFollowupService(agent, "claude", enabled=True, backend=backend)
    report = "# 定位报告\n连接池可能存在排队，请结合日志核实。\n"
    source = FollowupSource(CASE, JOB, "请求为什么超时？", report,
        hashlib.sha256(report.encode()).hexdigest(), "GENERIC", False, NOW)
    state = SimpleNamespace(repository=repository, agent=agent, service=service, backend=backend,
        source=source, clock=clock, cid=created.conversation_id, rid=created.run_id)
    service._source = lambda cid, rid: state.source
    yield state
    service.shutdown(0)
    repository.close()


def submit(system, request="question-1", text="为什么得出这个结论？"):
    return system.service.submit(system.cid, system.rid, request, text, owner_key=OWNER)


def view(system):
    return system.service.get(system.cid, system.rid, owner_key=OWNER)


def original_rows(system):
    with system.repository.database_read() as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not row[0].startswith("agent_followup_")]
        return {name: db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in tables}


def make_logs(system, *, kind="GENERIC", content=b"incident_code=QUEUE_OVERFLOW\ntrace_key=one\n"):
    system.source = replace(system.source, source_kind=kind, has_logs=True)
    original = system.repository.layout.workspaces / JOB
    (original / "inputs" / "logs").mkdir(parents=True)
    (original / "inputs" / "logs" / "source.log").write_bytes(content)
    row = {"log_path": "inputs/logs/source.log", "size": len(content),
        "sha256" if kind == "GENERIC" else "content_sha256": hashlib.sha256(content).hexdigest()}
    name, audit, key = (("generic_logs.json", "generic_logs.json", "logs") if kind == "GENERIC"
        else ("target_logs.json", "methods_target_logs.json", "target_logs"))
    raw = json.dumps({"schema_version": 1, key: [row]}, sort_keys=True).encode()
    (original / "inputs" / name).write_bytes(raw)
    job = system.repository.layout.jobs / JOB
    job.mkdir()
    (job / audit).write_bytes(raw)
    return original, job


def test_old_report_can_be_explained_without_reparsing_or_changing_any_original_table(system):
    before = original_rows(system)
    assert view(system).can_ask and view(system).snapshot_status == "UNAVAILABLE"
    assert original_rows(system) == before
    first = submit(system)
    assert submit(system) == first
    assert system.service.worker.run_once()
    answer = view(system).items[0]
    assert answer.status == "COMPLETED" and answer.context_mode == "REPORT_ONLY"
    assert "未重新核对原始日志" in answer.answer_markdown
    second = submit(system, "question-2", "但我观察到队列深度为零，这会影响结论吗？")
    assert system.service.worker.run_once()
    call = system.backend.calls[-1]
    context = json.loads((call["workspace_root"] / "inputs/context.json").read_text("utf-8"))
    assert context["prior_turns"][0]["answer"] == answer.answer_markdown
    assert context["question"].startswith("但我观察")
    assert first.followup_id != second.followup_id and len(system.backend.calls) == 2
    assert original_rows(system) == before
    events = system.service.list_events(system.cid, system.rid, owner_key=OWNER)
    assert events.stream_closed and [item.sequence for item in events.events] == list(range(1, 7))
    assert system.agent.store.list_events(system.cid) == []


def test_old_report_submit_does_not_make_surviving_logs_eligible_for_a_new_snapshot(system):
    _, job = make_logs(system)
    system.source = replace(system.source, occurred_at="2026-09-23T01:00:00.000Z")
    assert view(system).can_ask
    assert not system.service.observe_report(system.cid, system.rid)
    submit(system)
    assert not system.service.observe_report(system.cid, system.rid)
    assert not system.service.snapshot_worker.run_once()
    assert not (job / "followup-inputs").exists()
    assert system.service.worker.run_once()
    assert view(system).items[0].context_mode == "REPORT_ONLY"


@pytest.mark.parametrize("occurred_at,expected", [
    ("2026-09-24T00:59:59.999Z", False),
    ("2026-09-24T08:59:59.999+08:00", False),
    ("2026-09-24T01:00:00.000Z", True),
    ("2026-09-24T09:00:00.000+08:00", True),
])
def test_snapshot_admission_uses_same_enable_instant_for_utc_and_local_reports(system, occurred_at, expected):
    assert system.service.enabled_at == "2026-09-24T09:00:00.000+08:00"
    system.source = replace(system.source, occurred_at=occurred_at)
    assert system.service.observe_report(system.cid, system.rid) is expected
    snapshot = system.service.store.snapshot(system.rid)
    assert (snapshot is not None) is expected
    if snapshot is not None:
        assert snapshot["status"] == "PENDING"


def test_followup_items_events_and_snapshots_use_local_offset(system):
    submit(system)
    local_now = "2026-09-24T09:00:00.000+08:00"
    item = view(system).items[0]
    assert item.created_at == item.updated_at == local_now
    snapshot = system.service.store.snapshot(system.rid)
    assert snapshot["created_at"] == snapshot["updated_at"] == local_now
    system.clock.value = "2026-09-24T01:00:01.000Z"
    task = system.service.store.claim_task()
    assert task is not None
    assert system.service.store.finish_task(task["followup_id"], answer="依据报告补充解释。")
    item = view(system).items[0]
    assert item.created_at == local_now
    assert item.updated_at == "2026-09-24T09:00:01.000+08:00"
    events = system.service.list_events(system.cid, system.rid, owner_key=OWNER).events
    assert all(event.created_at.endswith("+08:00") for event in events)
    assert all(event.data.created_at.endswith("+08:00") for event in events)
    assert FollowupStore(system.repository).now().endswith("+08:00")


@pytest.mark.parametrize("occurred_at", ["2026-09-17T01:00:00.001Z", "2026-09-17T09:00:00.001+08:00"])
def test_followup_expiry_preserves_fifteen_day_boundary_across_offsets(system, occurred_at):
    system.source = replace(system.source, occurred_at=occurred_at)
    system.clock.value = "2026-10-02T09:00:00.000+08:00"
    assert view(system).can_ask
    system.clock.value = "2026-10-02T09:00:00.001+08:00"
    assert view(system).reason == "EXPIRED"


def test_snapshot_queue_orders_mixed_offsets_by_instant_without_rewriting_history(system):
    store = system.service.store
    assert store.enqueue_snapshot(system.cid, system.rid, system.source)
    newer = system.agent.store.create_conversation("newer", owner_key=OWNER)
    assert store.enqueue_snapshot(newer.conversation_id, newer.run_id, system.source)
    with system.repository.database_transaction() as db:
        db.execute("UPDATE agent_followup_snapshots SET created_at=? WHERE run_id=?",
            ("2026-09-24T08:00:00.000+08:00", system.rid))
        db.execute("UPDATE agent_followup_snapshots SET created_at=? WHERE run_id=?", (NOW, newer.run_id))
    assert store.claim_snapshot()["run_id"] == system.rid
    assert store.claim_snapshot()["run_id"] == newer.run_id
    assert store.snapshot(newer.run_id)["created_at"] == NOW


@pytest.mark.parametrize("kind", ["GENERIC", "SKILL_DIRECT"])
def test_snapshot_is_async_verified_and_model_only_reads_the_copied_inputs(system, kind):
    original, job = make_logs(system, kind=kind)
    before = original_rows(system)
    assert system.service.observe_report(system.cid, system.rid)
    assert system.service.store.workspace_in_use(JOB)
    assert view(system).snapshot_status == "PENDING"
    assert system.service.snapshot_worker.run_once()
    assert view(system).snapshot_status == "READY"
    assert not system.service.store.workspace_in_use(JOB)
    snapshot_log = job / "followup-inputs/inputs/logs/log-000001.log"
    assert snapshot_log.read_bytes() == (original / "inputs/logs/source.log").read_bytes()
    submit(system)
    assert system.service.worker.run_once()
    invocation = system.backend.calls[0]
    assert invocation["backend_phase"] == "REPORT_FOLLOWUP"
    assert invocation["file_access"] == "read-search" and invocation["broker_environment"] is None
    assert invocation["workspace_root"] != original
    assert (invocation["workspace_root"] / "inputs/logs/log-000001.log").read_bytes() == snapshot_log.read_bytes()
    assert view(system).items[0].context_mode == "REPORT_AND_LOGS"
    assert original_rows(system) == before


@pytest.mark.parametrize("kind", ["GENERIC", "SKILL_DIRECT"])
def test_multi_archive_snapshot_preserves_sources_for_same_named_logs(system, kind):
    original, job = make_logs(system, kind=kind)
    second_content = b"incident_code=QUEUE_OVERFLOW\ntrace_key=two\n"
    second = original / "inputs/logs/second/source.log"
    second.parent.mkdir()
    second.write_bytes(second_content)
    name, audit, key = (("generic_logs.json", "generic_logs.json", "logs") if kind == "GENERIC"
        else ("target_logs.json", "methods_target_logs.json", "target_logs"))
    initial = json.loads((original / "inputs" / name).read_bytes())[key][0]
    rows, sources = [], []
    for ordinal, path in enumerate((original / initial["log_path"], second), 1):
        attachment_id = f"00000000-0000-4000-8000-00000000070{ordinal}"
        source = {"attachment_id": attachment_id, "name": "logs.zip",
            "sha256": str(ordinal) * 64, "size": 100 + ordinal}
        row = {"log_path": path.relative_to(original).as_posix(), "size": path.stat().st_size,
            "sha256" if kind == "GENERIC" else "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        if kind == "GENERIC":
            row.update(relative_path="same/client.log", source_attachment_id=attachment_id,
                source_attachment_name=source["name"])
            sources.append({"source_attachment_id": attachment_id,
                "source_attachment_name": source["name"], "source_attachment_sha256": source["sha256"]})
        else:
            row.update(label="client", source_id=f"archive-{ordinal:03d}-client", source_archive=source)
        rows.append(row)
    value = {"schema_version": 2 if kind == "GENERIC" else 1, key: rows}
    if sources:
        value["sources"] = sources
    raw = json.dumps(value, sort_keys=True).encode()
    (original / "inputs" / name).write_bytes(raw)
    (job / audit).write_bytes(raw)

    assert system.service.observe_report(system.cid, system.rid)
    assert system.service.snapshot_worker.run_once()
    snapshot = json.loads((job / "followup-inputs/inputs/logs.json").read_bytes())
    assert len(snapshot["logs"]) == 2
    assert snapshot["logs"][0]["source_label"] == snapshot["logs"][1]["source_label"]
    assert len({row["log_path"] for row in snapshot["logs"]}) == 2
    for original_row, copied in zip(rows, snapshot["logs"], strict=True):
        if kind == "GENERIC":
            assert copied["source_attachment_id"] == original_row["source_attachment_id"]
            assert copied["source_attachment_name"] == "logs.zip"
        else:
            assert copied["source_id"] == original_row["source_id"]
            assert copied["source_archive"] == original_row["source_archive"]
        assert (job / "followup-inputs" / copied["log_path"]).read_bytes() == (
            original / original_row["log_path"]).read_bytes()
    if sources:
        assert snapshot["sources"] == sources
    submit(system)
    assert system.service.worker.run_once()
    assert json.loads((system.backend.calls[0]["workspace_root"] / "inputs/logs.json").read_bytes()) == snapshot


def test_changed_log_manifest_quota_and_missing_logs_degrade_without_touching_report(system):
    original, _ = make_logs(system)
    (original / "inputs/generic_logs.json").write_text('{"logs": []}', encoding="utf-8")
    assert system.service.observe_report(system.cid, system.rid)
    assert not system.service.snapshot_worker.run_once()
    assert view(system).snapshot_status == "FAILED" and view(system).can_ask
    submit(system)
    assert system.service.worker.run_once()
    assert view(system).items[0].context_mode == "REPORT_ONLY"


def test_snapshot_quota_is_reserved_before_copying_any_logs(system):
    _, job = make_logs(system, content=b"x" * 8192)
    system.service.snapshot_max_bytes = 4096
    system.service.snapshot_total_bytes = 4096
    assert system.service.observe_report(system.cid, system.rid)
    assert not system.service.snapshot_worker.run_once()
    assert not (job / "followup-inputs.pending").exists()
    assert view(system).snapshot_status == "FAILED"


def test_deferred_report_notification_keeps_the_completed_run_after_legacy_client_opens_next(system):
    _, job = make_logs(system)
    original_run = system.agent.store.get_run(system.cid, system.rid)
    original_run["report_available"] = True
    with system.repository.database_transaction() as db:
        db.execute("UPDATE agent_conversation_runs SET body=? WHERE run_id=?",
            (json.dumps(original_run), system.rid))
    system.service.schedule_observe(system.cid)
    next_message = system.agent.store.submit_message(system.cid, "legacy-next", "另一个问题")
    assert next_message.run_id != system.rid
    observed = []
    def source(cid, rid):
        observed.append((cid, rid))
        return system.source if rid == system.rid else None
    system.service._source = source
    assert system.service.snapshot_worker.run_once()
    assert observed == [(system.cid, system.rid)]
    assert system.service.store.snapshot(system.rid)["status"] == "READY"
    assert system.service.store.snapshot(next_message.run_id) is None
    assert (job / "followup-inputs/manifest.json").is_file()


def test_model_input_drift_discards_the_answer_without_retry(system):
    def mutate(kwargs):
        path = kwargs["workspace_root"] / "inputs/report.md"
        path.chmod(0o644)
        path.write_text("被模型改变的报告", encoding="utf-8")
    system.backend.action = mutate
    submit(system)
    assert not system.service.worker.run_once()
    item = view(system).items[0]
    assert item.failure.code == "AGENT_FOLLOWUP_INPUT_CHANGED" and item.answer_markdown is None
    assert not system.service.worker.run_once() and len(system.backend.calls) == 1


def test_snapshot_drift_blocks_answer_and_never_retries_the_model(system):
    _, job = make_logs(system)
    system.service.observe_report(system.cid, system.rid)
    system.service.snapshot_worker.run_once()
    file = job / "followup-inputs/inputs/logs/log-000001.log"
    file.chmod(0o644)
    file.write_bytes(b"changed")
    submit(system)
    assert not system.service.worker.run_once()
    assert not system.service.worker.run_once()
    assert not system.backend.calls
    assert view(system).items[0].status == "FAILED"


def test_concurrent_retries_claim_one_task_and_different_requests_are_rejected_while_busy(system):
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: submit(system), range(4)))
    assert all(reply == replies[0] for reply in replies)
    with pytest.raises(AgentStoreError) as caught:
        submit(system, "another")
    assert caught.value.code == "AGENT_FOLLOWUP_BUSY"
    with pytest.raises(AgentStoreError) as caught:
        submit(system, text="同一ID改变内容")
    assert caught.value.code == "AGENT_IDEMPOTENCY_CONFLICT"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: system.service.worker.run_once(), range(4)))
    assert len(system.backend.calls) == 1


def test_stop_queued_is_idempotent_and_stop_after_completion_keeps_answer(system):
    receipt = submit(system)
    stop = system.service.stop(system.cid, system.rid, receipt.followup_id, "stop", owner_key=OWNER)
    assert stop.status == "CANCELLED"
    assert system.service.stop(system.cid, system.rid, receipt.followup_id, "stop", owner_key=OWNER) == stop
    assert not system.service.worker.run_once()
    assert not system.backend.calls
    next_receipt = submit(system, "next")
    system.service.worker.run_once()
    assert system.service.stop(system.cid, system.rid, next_receipt.followup_id, "done", owner_key=OWNER).status == "ALREADY_FINISHED"
    with pytest.raises(AgentStoreError) as caught:
        system.service.stop(system.cid, system.rid, next_receipt.followup_id, "stop", owner_key=OWNER)
    assert caught.value.code == "AGENT_IDEMPOTENCY_CONFLICT"


def test_cancelling_running_task_waits_for_exit_and_discards_late_answer(system):
    entered, release = threading.Event(), threading.Event()
    def action(_):
        entered.set()
        assert release.wait(5)
    system.backend.action = action
    receipt = submit(system)
    thread = threading.Thread(target=system.service.worker.run_once)
    thread.start()
    assert entered.wait(5)
    try:
        assert system.service.stop(system.cid, system.rid, receipt.followup_id, "stop", owner_key=OWNER).status == "CANCELLING"
        assert view(system).active_followup.status == "CANCELLING"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert view(system).items[0].status == "CANCELLED"
    assert view(system).items[0].answer_markdown is None


def test_revocation_is_transactional_and_cleanup_receipts_include_owned_workspaces(system):
    receipt = submit(system)
    task = system.service.store.claim_task()
    with system.repository.database_transaction() as db:
        assert system.service.store.busy(db, system.cid)
        assert system.service.store.workspace_ids(db, system.cid) == [receipt.followup_id]
        system.service.store.revoke(db, system.cid)
        db.execute("UPDATE agent_conversations SET deleted_at=? WHERE conversation_id=?", (NOW, system.cid))
    assert not system.service.store.execution_allowed(task["followup_id"])
    system.service.store.finish_task(task["followup_id"], answer="不应公开")
    with pytest.raises(AgentStoreError) as caught:
        view(system)
    assert caught.value.status_code == 404
    with system.repository.database_transaction() as db:
        assert not system.service.store.busy(db, system.cid)
        system.service.store.purge(db, system.cid)
        assert db.execute("SELECT count(*) FROM agent_followup_tasks").fetchone()[0] == 0


def test_restart_marks_claimed_work_interrupted_and_disabled_start_cancels_queue(system):
    submit(system)
    system.service.store.claim_task()
    reopened = FollowupStore(system.repository, system.clock)
    reopened.recover()
    assert view(system).items[0].status == "INTERRUPTED"
    assert reopened.claim_task() is None
    submit(system, "queued")
    reopened.recover(enabled=False)
    assert view(system).items[-1].status == "CANCELLED"
    with system.repository.database_read() as db:
        assert not reopened.busy(db, system.cid)


def test_result_commit_failure_never_requeues_or_calls_model_again(system, monkeypatch):
    submit(system)
    def outage(*_args, **_kwargs):
        raise OSError("simulated database outage")
    monkeypatch.setattr(system.service.store, "finish_task", outage)
    assert not system.service.worker.run_once()
    assert not system.service.worker.run_once()
    assert len(system.backend.calls) == 1
    reopened = FollowupStore(system.repository, system.clock)
    reopened.recover()
    assert view(system).items[0].status == "INTERRUPTED"


def test_expired_report_rejects_new_work_but_finishes_already_accepted_followup(system):
    submit(system)
    system.clock.value = "2026-10-09T01:00:00.000Z"
    assert view(system).reason == "EXPIRED"
    with pytest.raises(AgentStoreError) as caught:
        submit(system, "expired")
    assert caught.value.code == "AGENT_FOLLOWUP_EXPIRED"
    assert system.service.worker.run_once()
    assert len(system.backend.calls) == 1
    assert view(system).items[0].status == "COMPLETED"


def test_owner_pagination_sse_cursor_and_old_retry_cannot_move_to_other_run(system):
    for index in range(3):
        submit(system, str(index))
        system.service.worker.run_once()
    page = system.service.get(system.cid, system.rid, owner_key=OWNER, limit=2)
    assert [item.ordinal for item in page.items] == [2, 3]
    earlier = system.service.get(system.cid, system.rid, owner_key=OWNER, cursor=page.next_cursor, limit=2)
    assert [item.ordinal for item in earlier.items] == [1]
    assert not earlier.next_cursor
    resumed = system.service.list_events(system.cid, system.rid, after_sequence=7, owner_key=OWNER)
    assert [event.sequence for event in resumed.events] == [8, 9]
    for owner in (OTHER, None):
        with pytest.raises(AgentStoreError) as caught:
            system.service.get(system.cid, system.rid, owner_key=owner)
        assert caught.value.status_code == 404
    with pytest.raises(AgentStoreError):
        system.service.list_events(system.cid, system.rid, after_sequence=10, owner_key=OWNER)


def test_long_history_stays_complete_in_readable_file_instead_of_being_discarded(system):
    system.backend.answer = "答案：" + "a" * 50_000
    for index in range(5):
        submit(system, str(index), f"追问{index}：" + "q" * 30_000)
        assert system.service.worker.run_once()
    invocation = system.backend.calls[-1]
    context = json.loads((invocation["workspace_root"] / "inputs/context.json").read_text("utf-8"))
    assert context["history_file"] == "inputs/history.md" and context["history_total"] == 4
    history = (invocation["workspace_root"] / "inputs/history.md").read_text("utf-8")
    assert "追问0：" + "q" * 30_000 in history
    assert "追问3：" + "q" * 30_000 in history
    assert len(invocation["prompt"].encode()) <= 262_144
    system.service.read_search_supported = False
    with pytest.raises(AgentStoreError) as caught:
        submit(system, "no-tools", "不能静默舍弃旧问答")
    assert caught.value.code == "AGENT_FOLLOWUP_CONTEXT_LIMIT"


def test_requests_enforce_utf8_budget_and_reject_attachments():
    FollowupRequest(request_id="中文", text="问" * 21_845)
    with pytest.raises(ValueError):
        FollowupRequest(request_id="x", text="问" * 21_846)
    with pytest.raises(ValueError):
        FollowupRequest(request_id="x", text=" ")
    with pytest.raises(ValueError):
        FollowupRequest.model_validate({"request_id": "x", "text": "问题", "attachment_ids": []})
