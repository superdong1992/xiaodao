"""Historical followup storage and snapshots remain readable after Generic handoff."""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
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


def native_init(kwargs):
    session = kwargs["native_session"]
    identifier = session.resume_id or str(uuid.uuid4())
    session.on_session_id(identifier)
    return identifier


def seed_historical_turn(system, request, text, answer):
    """Create the persisted rows of a pre-session conversation, without a CLI."""
    submit(system, request, text)
    task = system.service.store.claim_task()
    assert system.service.store.finish_task(task["followup_id"], answer=answer)


def model_reply(kwargs, *, answer=None, decision=None, action="ANSWER"):
    context = json.loads((kwargs["workspace_root"] / "inputs/context.json").read_bytes())
    contract = context["response_contract"]
    result = {"protocol": contract["protocol"], "request_key": contract["request_key"],
        "action": "CONTINUE_DIAGNOSIS" if decision is not None else action}
    if decision is not None:
        result.update(decision)
    elif action in {"ANSWER", "REQUEST_INPUT"}:
        result["answer_markdown"] = answer
    return json.dumps(result, ensure_ascii=False)


class Backend:
    def __init__(self, answer="根据报告，连接池排队是需要继续核实的方向。", action=None, *, decision=None, raw_result=None):
        self.answer, self.action, self.calls = answer, action, []
        self.decision, self.raw_result = decision, raw_result

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["backend_phase"] == "REPORT_FOLLOWUP":
            native_init(kwargs)
        if self.action is not None:
            self.action(kwargs)
        result = self.raw_result
        if result is None:
            result = (model_reply(kwargs, answer=self.answer, decision=self.decision)
                if kwargs["backend_phase"] == "REPORT_FOLLOWUP" else self.answer)
        return SimpleNamespace(final_result=result)


@pytest.fixture
def system(tmp_path):
    repository = _open(tmp_path)
    clock = SimpleNamespace(value=NOW)
    clock.now = lambda: clock.value
    agent_store = AgentStore(repository, clock=clock, runtime_epoch="test")
    MemoryStore(repository, clock)
    agent = AgentConversationService(agent_store, SimpleNamespace(), None, repository.layout)
    created = agent_store.create_conversation("create", owner_key=OWNER)
    with repository.database_transaction() as db:
        body = agent_store._load(db, created.conversation_id)
        body.update(status="COMPLETED", report_available=True, case_id=CASE, case_status="RESOLVED")
        agent_store._save(db, body)
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


@pytest.fixture
def diagnostic_system(tmp_path):
    """Use a published report and real continuation transactions for worker tests."""
    from tests.deterministic.unit.agent.test_report_continuation import system as continuation_system
    from tests.deterministic.unit.agent.test_followup_handoff import state
    generator = continuation_system.__wrapped__(tmp_path)
    value = state.__wrapped__(next(generator))
    value.service = value.followups
    try:
        yield value
    finally:
        with pytest.raises(StopIteration):
            next(generator)


def complete_historical_task(system, answer="历史报告解释。"):
    """Persist a pre-upgrade answer without invoking the retired model worker."""
    task = system.service.store.claim_task()
    assert task is not None
    return system.service.store.finish_task(task["followup_id"], answer=answer)


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


def test_historical_answers_remain_readable_without_reparsing_or_changing_original_tables(system):
    before = original_rows(system)
    first = submit(system)
    assert submit(system) == first
    assert complete_historical_task(system, "历史回答：连接池有待核实。")
    answer = view(system).items[0]
    assert answer.status == "COMPLETED" and answer.diagnosis_run_id is None
    second = submit(system, "question-2", "但队列深度为零。")
    task = system.service.store.claim_task()
    source, history, snapshot = system.service.store.task_context(task)
    assert source.report_markdown == system.source.report_markdown
    assert history[0]["answer_markdown"] == answer.answer_markdown
    assert system.service.store.finish_task(task["followup_id"], answer="历史补充解释。")
    assert first.followup_id != second.followup_id
    assert original_rows(system) == before and not system.backend.calls
    events = system.service.list_events(system.cid, system.rid, owner_key=OWNER)
    assert events.stream_closed and [event.sequence for event in events.events] == list(range(1, 7))
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
    assert complete_historical_task(system)
    assert not system.backend.calls


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
        assert snapshot["status"] == "UNAVAILABLE"


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
def test_log_expiry_preserves_fifteen_day_boundary_without_expiring_chat(system, occurred_at):
    system.source = replace(system.source, occurred_at=occurred_at)
    with system.repository.database_transaction() as db:
        body = system.agent.store._load(db, system.cid)
        body["completed_at"] = occurred_at
        system.agent.store._save(db, body)
    system.clock.value = "2026-10-02T09:00:00.000+08:00"
    assert view(system).can_ask
    assert not system.service.store.expired(system.source)
    system.clock.value = "2026-10-02T09:00:00.001+08:00"
    assert system.service.store.expired(system.source)
    assert view(system).can_ask


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
def test_historical_snapshot_is_async_verified_and_copies_only_declared_inputs(system, kind):
    from problem_locator.dispatch.cancellation import CancellationController
    from problem_locator.followup.snapshots import copy_snapshot, verify_inputs
    original, job = make_logs(system, kind=kind)
    before = original_rows(system)
    assert system.service.store.enqueue_snapshot(system.cid, system.rid, system.source)
    assert system.service.store.workspace_in_use(JOB)
    assert view(system).snapshot_status == "PENDING"
    assert system.service.snapshot_worker.run_once()
    assert view(system).snapshot_status == "READY"
    assert not system.service.store.workspace_in_use(JOB)
    snapshot_log = job / "followup-inputs/inputs/logs/log-000001.log"
    assert snapshot_log.read_bytes() == (original / "inputs/logs/source.log").read_bytes()
    target = system.repository.layout.workspaces / "historical-copy"
    target.mkdir()
    rows = copy_snapshot(system.service, system.service.store.snapshot(system.rid), target, CancellationController())
    verify_inputs(target, rows, CancellationController())
    assert (target / "inputs/logs/log-000001.log").read_bytes() == snapshot_log.read_bytes()
    assert original_rows(system) == before and not system.backend.calls


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

    assert system.service.store.enqueue_snapshot(system.cid, system.rid, system.source)
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
    assert not system.backend.calls
    assert system.service.store.snapshot(system.rid)["status"] == "READY"


def test_direct_input_snapshot_keeps_original_sources_and_skipped_coverage(system):
    original, job = make_logs(system, content=b"\xef\xbb\xbfincident_code=QUEUE_OVERFLOW\n")
    payload = json.loads((original / "inputs/generic_logs.json").read_bytes())
    payload.update(schema_version=3, preparation="direct-v1",
        sources=[{"source_attachment_id": CASE, "source_attachment_name": "server.zip",
                  "source_attachment_sha256": "a" * 64, "source_kind": "archive", "size": 200,
                  "content_type": "application/zip"}],
        skipped=[{"relative_path": "core.bin", "reason": "NON_TEXT", "size": 4, "sha256": "b" * 64}],
        coverage={"entries": 2, "expanded_bytes": 36, "readable_bytes": 32, "complete": False})
    payload["logs"][0].update(source_attachment_id=CASE, source_attachment_name="server.zip",
        source_attachment_sha256="a" * 64, relative_path="server/stdout.log", encoding="utf-8-sig")
    raw = json.dumps(payload, sort_keys=True).encode()
    (original / "inputs/generic_logs.json").write_bytes(raw)
    (job / "generic_logs.json").write_bytes(raw)
    assert system.service.store.enqueue_snapshot(system.cid, system.rid, system.source)
    assert system.service.snapshot_worker.run_once()
    actual = json.loads((job / "followup-inputs/inputs/logs.json").read_bytes())
    for key in ("preparation", "sources", "skipped", "coverage"):
        assert actual[key] == payload[key]
    for key in ("source_attachment_sha256", "relative_path", "encoding", "sha256"):
        assert actual["logs"][0][key] == payload["logs"][0][key]
    assert (job / "followup-inputs" / actual["logs"][0]["log_path"]).read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("empty_reason", ["NO_FILES", "NO_READABLE_TEXT"])
def test_direct_empty_log_snapshot_preserves_preparation_but_followup_stays_report_only(system, empty_reason):
    original, job = make_logs(system)
    system.source = replace(system.source, has_logs=False, has_log_manifest=True)
    payload = {"schema_version": 3, "preparation": "direct-v1", "logs": [],
        "sources": [{"source_attachment_id": CASE, "source_attachment_name": "server.zip",
            "source_attachment_sha256": "a" * 64, "source_kind": "archive", "size": 200,
            "content_type": "application/zip"}],
        "skipped": [] if empty_reason == "NO_FILES" else [
            {"relative_path": "empty.log", "reason": "EMPTY_FILE", "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}],
        "coverage": {"scope": "INPUT_PREPARATION", "entries": 0 if empty_reason == "NO_FILES" else 1,
            "expanded_bytes": 0, "readable_bytes": 0, "complete": empty_reason == "NO_FILES",
            "empty_reason": empty_reason}}
    raw = json.dumps(payload, sort_keys=True).encode()
    (original / "inputs/generic_logs.json").write_bytes(raw)
    (job / "generic_logs.json").write_bytes(raw)
    assert system.service.store.enqueue_snapshot(system.cid, system.rid, system.source)
    assert system.service.snapshot_worker.run_once()
    snapshot = json.loads((job / "followup-inputs/inputs/logs.json").read_bytes())
    assert snapshot["logs"] == []
    for key in ("preparation", "sources", "skipped", "coverage"):
        assert snapshot[key] == payload[key]
    submit(system, text="为什么没有使用我上传的日志？")
    task = system.service.store.claim_task()
    assert json.loads(task["body"])["context_mode"] == "REPORT_ONLY"
    assert system.service.store.finish_task(task["followup_id"], answer="历史解释：包内没有可读日志。")
    assert not list((job / "followup-inputs/inputs/logs").iterdir())
    assert not system.backend.calls


def test_direct_empty_log_snapshot_still_checks_original_receipt_against_audit(system):
    original, job = make_logs(system)
    system.source = replace(system.source, has_logs=False, has_log_manifest=True)
    (original / "inputs/generic_logs.json").write_text(
        json.dumps({"schema_version": 3, "preparation": "direct-v1", "logs": [], "coverage": {"empty_reason": "NO_FILES"}}),
        encoding="utf-8")
    assert system.service.store.enqueue_snapshot(system.cid, system.rid, system.source)
    assert not system.service.snapshot_worker.run_once()
    assert view(system).snapshot_status == "FAILED"
    assert not (job / "followup-inputs").exists()


@pytest.mark.parametrize("logs,expected", [([], False), ([{"log_path": "inputs/logs/one.log"}], True)])
def test_direct_input_attachment_is_not_enough_to_claim_readable_logs(system, logs, expected):
    job = SimpleNamespace(job_id=JOB, attachment_refs=[CASE], generic_log_preparation="direct-v1")
    root = system.repository.layout.jobs / JOB
    root.mkdir()
    (root / "generic_logs.json").write_text(json.dumps({"schema_version": 3, "logs": logs}), encoding="utf-8")
    assert system.service._has_readable_logs(job, generic=True) is expected
    # Existing Logparse and specialized sources keep their old compatibility.
    assert system.service._has_readable_logs(job, generic=False) is True
    job.generic_log_preparation = None
    assert system.service._has_readable_logs(job, generic=True) is True


def test_direct_input_missing_receipt_never_claims_log_evidence(system):
    job = SimpleNamespace(job_id=JOB, attachment_refs=[CASE], generic_log_preparation="direct-v1")
    assert system.service._has_readable_logs(job, generic=True) is False


def test_changed_log_manifest_quota_and_missing_logs_degrade_without_touching_report(system):
    original, _ = make_logs(system)
    (original / "inputs/generic_logs.json").write_text('{"logs": []}', encoding="utf-8")
    assert system.service.store.enqueue_snapshot(system.cid, system.rid, system.source)
    assert not system.service.snapshot_worker.run_once()
    assert view(system).snapshot_status == "FAILED" and view(system).can_ask
    submit(system)
    assert complete_historical_task(system)
    assert view(system).items[0].context_mode == "REPORT_ONLY"


def test_snapshot_quota_is_reserved_before_copying_any_logs(system):
    _, job = make_logs(system, content=b"x" * 8192)
    system.service.snapshot_max_bytes = 4096
    system.service.snapshot_total_bytes = 4096
    assert system.service.store.enqueue_snapshot(system.cid, system.rid, system.source)
    assert not system.service.snapshot_worker.run_once()
    assert not (job / "followup-inputs.pending").exists()
    assert view(system).snapshot_status == "FAILED"


def test_deferred_report_notification_registers_completed_run_without_copying_logs(system):
    _, job = make_logs(system)
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
    assert system.service.store.snapshot(system.rid)["status"] == "UNAVAILABLE"
    assert system.service.store.snapshot(next_message.run_id) is None
    assert not (job / "followup-inputs").exists()


def test_historical_input_verification_rejects_changed_report_bytes(system):
    from problem_locator.dispatch.cancellation import CancellationController
    from problem_locator.followup.snapshots import InputChanged, copy_snapshot, verify_inputs
    make_logs(system)
    assert system.service.store.enqueue_snapshot(system.cid, system.rid, system.source)
    assert system.service.snapshot_worker.run_once()
    target = system.repository.layout.workspaces / "historical-copy"
    target.mkdir()
    rows = copy_snapshot(system.service, system.service.store.snapshot(system.rid), target, CancellationController())
    path = target / "inputs/report.md"
    path.chmod(0o644)
    path.write_text("改变的报告", encoding="utf-8")
    with pytest.raises(InputChanged):
        verify_inputs(target, rows, CancellationController())
    assert not system.backend.calls


def test_historical_snapshot_drift_blocks_copy_without_model_invocation(system):
    from problem_locator.dispatch.cancellation import CancellationController
    from problem_locator.followup.snapshots import InputChanged, copy_snapshot
    _, job = make_logs(system)
    assert system.service.store.enqueue_snapshot(system.cid, system.rid, system.source)
    assert system.service.snapshot_worker.run_once()
    path = job / "followup-inputs/inputs/logs/log-000001.log"
    path.chmod(0o644)
    path.write_bytes(b"changed")
    target = system.repository.layout.workspaces / "drift-copy"
    target.mkdir()
    with pytest.raises(ValueError, match="frozen ordinary file"):
        copy_snapshot(system.service, system.service.store.snapshot(system.rid), target, CancellationController())
    assert not system.backend.calls


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
        claims = list(pool.map(lambda _: system.service.store.claim_task(), range(4)))
    claimed = [task for task in claims if task is not None]
    assert len(claimed) == 1
    assert system.service.store.finish_task(claimed[0]["followup_id"], answer="历史回答。")
    assert not system.backend.calls


def test_stop_queued_is_idempotent_and_stop_after_completion_keeps_answer(system):
    receipt = submit(system)
    stop = system.service.stop(system.cid, system.rid, receipt.followup_id, "stop", owner_key=OWNER)
    assert stop.status == "CANCELLED"
    assert system.service.stop(system.cid, system.rid, receipt.followup_id, "stop", owner_key=OWNER) == stop
    assert not system.service.worker.run_once()
    assert not system.backend.calls
    next_receipt = submit(system, "next")
    complete_historical_task(system)
    assert system.service.stop(system.cid, system.rid, next_receipt.followup_id, "done", owner_key=OWNER).status == "ALREADY_FINISHED"
    with pytest.raises(AgentStoreError) as caught:
        system.service.stop(system.cid, system.rid, next_receipt.followup_id, "stop", owner_key=OWNER)
    assert caught.value.code == "AGENT_IDEMPOTENCY_CONFLICT"


def test_cancelling_running_historical_task_discards_late_answer(system):
    receipt = submit(system)
    task = system.service.store.claim_task()
    assert system.service.stop(system.cid, system.rid, receipt.followup_id, "stop", owner_key=OWNER).status == "CANCELLING"
    assert view(system).active_followup.status == "CANCELLING"
    system.service.store.finish_task(task["followup_id"], answer="迟到的回答。")
    assert view(system).items[0].status == "CANCELLED"
    assert view(system).items[0].answer_markdown is None
    assert not system.backend.calls


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
    attempts = []
    def dispatch(task):
        attempts.append(task["followup_id"])
        return system.service.store.finish_task(task["followup_id"], answer="历史回答。")
    def outage(*args, **kwargs):
        raise OSError("simulated database outage")
    monkeypatch.setattr(system.service, "dispatch_generic", dispatch)
    monkeypatch.setattr(system.service.store, "finish_task", outage)
    assert not system.service.worker.run_once()
    assert not system.service.worker.run_once()
    assert len(attempts) == 1 and not system.backend.calls
    reopened = FollowupStore(system.repository, system.clock)
    reopened.recover()
    assert view(system).items[0].status == "INTERRUPTED"


def test_expired_logs_preserve_historical_answers_and_accept_new_requests(system):
    submit(system)
    system.clock.value = "2026-10-09T01:00:00.000Z"
    assert not view(system).can_ask and view(system).reason == "BUSY"
    assert complete_historical_task(system)
    assert view(system).can_ask and view(system).reason is None
    submit(system, "later")
    assert complete_historical_task(system)
    assert all(item.status == "COMPLETED" for item in view(system).items)
    assert not system.backend.calls


def test_owner_pagination_sse_cursor_and_old_retry_cannot_move_to_other_run(system):
    for index in range(3):
        submit(system, str(index))
        complete_historical_task(system)
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


def test_historical_chat_is_loaded_without_truncating_answers_or_native_session_work(system):
    answer = "答案：" + "a" * 50_000
    for index in range(4):
        seed_historical_turn(system, str(index), f"追问{index}：" + "q" * 30_000, answer)
    submit(system, "4", "继续讨论。")
    task = system.service.store.claim_task()
    source, history, snapshot = system.service.store.task_context(task)
    assert len(history) == 4
    assert history[0]["text"] == "追问0：" + "q" * 30_000
    assert history[-1]["answer_markdown"] == answer
    assert system.service.store.finish_task(task["followup_id"], answer="历史补充。")
    assert not system.backend.calls
    assert system.service.store.sessions.get(system.cid) is None


def test_requests_enforce_utf8_budget_and_validate_attachment_ids():
    FollowupRequest(request_id="中文", text="问" * 21_845)
    with pytest.raises(ValueError):
        FollowupRequest(request_id="x", text="问" * 21_846)
    with pytest.raises(ValueError):
        FollowupRequest(request_id="x", text=" ")
    assert FollowupRequest(request_id="x", text="问题", attachment_ids=[]).attachment_ids == []
    with pytest.raises(ValueError):
        FollowupRequest(request_id="x", text="问题", attachment_ids=["invalid"])
