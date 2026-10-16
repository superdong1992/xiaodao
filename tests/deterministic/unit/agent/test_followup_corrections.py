"""Correction targets, durable learning and checked log evidence stay independent of votes."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from problem_locator.agent.models import AgentStoreError
from problem_locator.dispatch.cancellation import CancellationController
from problem_locator.followup.models import FollowupRequest
from problem_locator.followup.store import FollowupStore
from problem_locator.memory.corrections import CorrectionMemoryHandler, validate_evidence
from problem_locator.memory.store import MemoryStore
from tests.deterministic.unit.agent.test_report_followup import (Backend, CASE, OWNER, OTHER,
    make_logs, submit, system, view)

CARD = {"problem_features": ["连接池请求排队"], "applicability": ["出现队列溢出错误"],
    "steps": ["先核对队列溢出错误是否与超时同时出现"], "limitations": ["需要当前日志支持，不能直接沿用历史根因"]}
LOG = b"incident_code=QUEUE_OVERFLOW\ntrace_key=one\n"


def enable_memory(system):
    system.source = replace(system.source, skill_name="generic-test")
    system.memory = MemoryStore(system.repository, system.clock)
    system.service.store.memory_store = system.memory
    with system.repository.database_transaction() as db:
        db.execute("UPDATE agent_conversation_runs SET case_id=? WHERE run_id=?", (CASE, system.rid))
    return system.memory


def correct(system, request="correction-1", target=None):
    return system.service.submit(system.cid, system.rid, request,
        "原回答忽略了队列溢出错误，请先核实排队。", owner_key=OWNER,
        intent="CORRECTION", target_followup_id=target)


def finish(system, receipt, answer="日志显示队列溢出，应先核对排队与超时的时间关系。"):
    task = system.service.store.claim_task()
    assert task["followup_id"] == receipt.followup_id
    assert system.service.store.finish_task(task["followup_id"], answer=answer)


def test_correction_input_and_idempotency_are_separate_from_ordinary_questions(system):
    ordinary = submit(system)
    finish(system, ordinary)
    assert submit(system) == ordinary
    with pytest.raises(ValidationError):
        FollowupRequest(request_id="bad", text="说明", target_followup_id=ordinary.followup_id)
    correction = correct(system, target=ordinary.followup_id)
    assert correct(system, target=ordinary.followup_id) == correction
    with pytest.raises(AgentStoreError) as conflict:
        correct(system, target=None)
    assert conflict.value.code == "AGENT_IDEMPOTENCY_CONFLICT"
    source, item, metadata, history = system.service.store.learning_source(system.cid, system.rid, correction.followup_id, OWNER)
    assert metadata["target_followup_id"] == ordinary.followup_id
    assert metadata["target_answer_markdown"] == history[0]["answer_markdown"]
    assert "intent" not in item.model_dump() and "correction" not in item.model_dump()
    with pytest.raises(AgentStoreError) as denied:
        system.service.store.learning_source(system.cid, system.rid, correction.followup_id, OTHER)
    assert denied.value.status_code == 404


def test_correction_target_must_belong_to_completed_answer_of_exact_run(system):
    ordinary = submit(system)
    with pytest.raises(AgentStoreError):
        correct(system, target=ordinary.followup_id)
    finish(system, ordinary)
    with pytest.raises(AgentStoreError) as missing:
        correct(system, target="00000000-0000-0000-0000-000000000099")
    assert missing.value.code == "AGENT_FOLLOWUP_NOT_FOUND"
    other = system.agent.store.create_conversation("another", owner_key=OWNER)
    with pytest.raises(AgentStoreError) as wrong_run:
        system.service.submit(other.conversation_id, other.run_id, "correction-other", "这个解释有误。",
            owner_key=OWNER, intent="CORRECTION", target_followup_id=ordinary.followup_id)
    assert wrong_run.value.code == "AGENT_FOLLOWUP_NOT_FOUND"


def test_correction_keeps_original_history_and_binds_precise_target_in_prompt(system):
    ordinary = submit(system)
    finish(system, ordinary, "原解释认为没有队列问题。")
    before = view(system).items[0].model_dump()
    correction = correct(system, target=ordinary.followup_id)
    assert system.service.worker.run_once()
    call = system.backend.calls[-1]
    context = json.loads((call["workspace_root"] / "inputs/context.json").read_text("utf-8"))
    assert context["correction"]["target_answer_markdown"] == before["answer_markdown"]
    assert context["original_report"] == system.source.report_markdown
    assert context["question"] == view(system).items[-1].text
    assert "不是日志证据" in call["prompt"]
    assert view(system).items[0].model_dump() == before
    assert view(system).items[-1].followup_id == correction.followup_id


def test_correction_completion_atomically_queues_once_without_a_like(system):
    memory = enable_memory(system)
    receipt = correct(system)
    finish(system, receipt)
    assert correct(system) == receipt
    task = memory.claim_task()
    assert task["answer_key"] == receipt.followup_id and task["source_kind"] == "CORRECTION"
    assert task["replaces_answer_key"] == ""
    assert json.loads(task["context_json"])["correction"]["target_answer_markdown"] == system.source.report_markdown
    assert memory.claim_task() is None
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_feedback").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM memory_tasks").fetchone()[0] == 1
    assert system.service.store.workspace_in_use(task["task_id"])


def test_specialized_correction_and_disabled_learning_never_queue_memory(system):
    memory = enable_memory(system)
    system.source = replace(system.source, source_kind="SKILL_DIRECT")
    finish(system, correct(system))
    assert memory.claim_task() is None
    system.service.store.memory_store = None
    system.source = replace(system.source, source_kind="GENERIC")
    finish(system, correct(system, request="disabled"))
    assert memory.claim_task() is None


def test_missing_logs_decline_shared_correction_without_model_call(system):
    memory = enable_memory(system)
    finish(system, correct(system))
    task = memory.claim_task()
    backend = Backend()
    with pytest.raises(ValueError, match="原始日志"):
        CorrectionMemoryHandler(system.service, "claude", backend=backend).extract(task, cancellation=CancellationController())
    assert backend.calls == [] and view(system).items[0].status == "COMPLETED"
    assert memory.active_cards("generic-test") == []


def test_legacy_followup_schema_upgrades_and_old_request_replays(system):
    receipt = submit(system)
    with system.repository.database_transaction() as db:
        db.execute("ALTER TABLE agent_followup_tasks DROP COLUMN correction_json")
        db.execute("UPDATE agent_followup_metadata SET value='1' WHERE key='storage_version'")
        body = db.execute("SELECT source_json FROM agent_followup_snapshots").fetchone()[0]
        legacy = json.loads(body)
        legacy.pop("skill_name", None)
        db.execute("UPDATE agent_followup_snapshots SET source_json=?", (json.dumps(legacy),))
    upgraded = FollowupStore(system.repository, system.clock)
    assert upgraded.request(system.cid, system.rid, "question-1", "为什么得出这个结论？", OWNER) == receipt
    assert upgraded._source(upgraded.snapshot(system.rid)).skill_name == ""
    with system.repository.database_read() as db:
        assert db.execute("SELECT value FROM agent_followup_metadata WHERE key='storage_version'").fetchone()[0] == "2"
        assert db.execute("SELECT correction_json FROM agent_followup_tasks").fetchone()[0] == "{}"


def response(*, quote="incident_code=QUEUE_OVERFLOW", path="inputs/logs/log-000001.log"):
    return json.dumps({"adopt": True, "card": CARD, "evidence": [{"path": path,
        "sha256": hashlib.sha256(LOG).hexdigest(), "start_line": 1, "end_line": 1, "quote": quote}]}, ensure_ascii=False)


def prepare_logged_task(system):
    memory = enable_memory(system)
    make_logs(system, content=LOG)
    assert system.service.observe_report(system.cid, system.rid)
    assert system.service.snapshot_worker.run_once()
    finish(system, correct(system))
    return memory.claim_task()


def test_correction_memory_requires_verified_log_citations_and_keeps_private_receipt(system):
    task = prepare_logged_task(system)
    backend = Backend(response())
    card, receipt = CorrectionMemoryHandler(system.service, "claude", backend=backend).extract(task, cancellation=CancellationController())
    assert json.loads(card) == CARD
    proof = json.loads(receipt)
    assert proof["answer_sha256"] == task["report_sha256"]
    assert "quote" not in proof["evidence"][0]
    assert "incident_code" not in receipt and "trace_key" not in card
    assert len(backend.calls) == 1
    assert backend.calls[0]["backend_phase"] == "MEMORY_CORRECTION"
    assert backend.calls[0]["file_access"] == "read-search"
    assert system.memory.finish_task(task["task_id"], card, evidence_json=receipt)
    assert len(system.memory.active_cards("generic-test")) == 1


@pytest.mark.parametrize("output", [response(quote="the model invented this"), response(path="inputs/report.md"),
    json.dumps({"adopt": False, "card": None, "evidence": []}),
    json.dumps({"adopt": True, "card": CARD, "evidence": []})])
def test_unsupported_or_fabricated_correction_never_publishes(system, output):
    task = prepare_logged_task(system)
    backend = Backend(output)
    with pytest.raises(ValueError):
        CorrectionMemoryHandler(system.service, "claude", backend=backend).extract(task, cancellation=CancellationController())
    system.memory.fail_task(task["task_id"])
    assert system.memory.claim_task() is None and len(backend.calls) == 1
    assert system.memory.active_cards("generic-test") == []


def test_correction_input_drift_rejects_result_and_unknown_launcher_skips_model(system):
    task = prepare_logged_task(system)
    service = system.service
    service.read_search_supported = False
    backend = Backend(response())
    with pytest.raises(ValueError, match="核对能力"):
        CorrectionMemoryHandler(service, "unknown", backend=backend).extract(task, cancellation=CancellationController())
    assert backend.calls == []
    service.read_search_supported = True
    def mutate(call):
        path = call["workspace_root"] / "inputs/logs/log-000001.log"
        path.chmod(0o644)
        path.write_text("changed", encoding="utf-8")
    backend.action = mutate
    with pytest.raises(ValueError):
        CorrectionMemoryHandler(service, "claude", backend=backend).extract(task, cancellation=CancellationController())
    assert len(backend.calls) == 1 and system.memory.active_cards("generic-test") == []


def test_memory_enqueue_failure_preserves_completed_answer_and_removes_partial_task(system):
    memory = enable_memory(system)
    enqueue = memory.enqueue_correction_in_transaction
    def broken(*args):
        assert enqueue(*args)
        raise OSError("injected storage failure after task insertion")
    memory.enqueue_correction_in_transaction = broken
    receipt = correct(system)
    finish(system, receipt)
    assert view(system).items[0].status == "COMPLETED"
    assert memory.claim_task() is None
    assert submit(system, request="next-question").status == "ACCEPTED"
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_tasks").fetchone()[0] == 0
    assert not system.service.worker._active


def test_current_followup_marker_with_missing_column_is_rejected(system):
    with system.repository.database_transaction() as db:
        db.execute("ALTER TABLE agent_followup_tasks DROP COLUMN correction_json")
    with pytest.raises(AgentStoreError) as mismatch:
        FollowupStore(system.repository, system.clock)
    assert mismatch.value.code == "STATE_SCHEMA_UNSUPPORTED"


def test_old_snapshot_skill_is_filled_only_from_matching_published_report(system):
    submit(system)
    with system.repository.database_transaction() as db:
        saved = json.loads(db.execute("SELECT source_json FROM agent_followup_snapshots").fetchone()[0])
        saved.pop("skill_name", None)
        db.execute("UPDATE agent_followup_snapshots SET source_json=?", (json.dumps(saved),))
    source = replace(system.source, skill_name="original-skill")
    assert system.service.store.enqueue_snapshot(system.cid, system.rid, source)
    upgraded = system.service.store._source(system.service.store.snapshot(system.rid))
    assert upgraded.skill_name == "original-skill"
    wrong = replace(source, source_job_id="00000000-0000-0000-0000-000000000099", skill_name="unrelated-skill")
    with pytest.raises(AgentStoreError):
        system.service.store.enqueue_snapshot(system.cid, system.rid, wrong)
    assert system.service.store._source(system.service.store.snapshot(system.rid)).skill_name == "original-skill"


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "READY", "FAILED", "DELETED"])
def test_deletion_manifest_includes_private_correction_workspaces_in_every_state(system, status):
    memory = enable_memory(system)
    system.agent.store.followup_store = system.service.store
    system.agent.store.memory_store = memory
    finish(system, correct(system))
    task = memory.claim_task()
    private = system.repository.layout.workspaces / task["task_id"] / "inputs"
    private.mkdir(parents=True)
    (private / "correction.json").write_text("private user correction", encoding="utf-8")
    with system.repository.database_transaction() as db:
        db.execute("UPDATE memory_tasks SET status=? WHERE task_id=?", (status, task["task_id"]))
        assert task["task_id"] in system.service.store.workspace_ids(db, system.cid, system.rid)
        assert task["task_id"] not in system.service.store.workspace_ids(db, system.cid, "00000000-0000-0000-0000-000000000099")
        assert task["task_id"] not in system.service.store.workspace_ids(db, "00000000-0000-0000-0000-000000000099")
    assert task["task_id"] in system.agent.store.cleanup_context(system.cid)["workspace_ids"]
    system.agent.store.request_delete(system.cid, owner_key=OWNER)
    with system.repository.database_read() as db:
        manifest = json.loads(db.execute("SELECT manifest FROM agent_cleanup_jobs WHERE conversation_id=?", (system.cid,)).fetchone()[0])
    assert task["task_id"] in manifest["workspace_ids"]


class CountingCancellation:
    def __init__(self, cancel_at=None):
        self.calls, self.cancel_at = 0, cancel_at

    def is_cancelled(self):
        self.calls += 1
        return self.cancel_at is not None and self.calls >= self.cancel_at


def deep_log_evidence(tmp_path, lines):
    relative = "inputs/logs/deep.log"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    raw = ("ordinary log line\n" * (lines - 1) + "QUEUE_OVERFLOW\n").encode()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    rows = [{"path": relative, "size": len(raw), "sha256": digest}]
    evidence = [{"path": relative, "sha256": digest, "start_line": lines, "end_line": lines, "quote": "QUEUE_OVERFLOW"}]
    return rows, evidence


def test_deep_log_evidence_batches_database_cancellation_checks(tmp_path):
    rows, evidence = deep_log_evidence(tmp_path, 100_000)
    signal = CountingCancellation()
    result = validate_evidence(evidence, tmp_path, rows, signal)
    assert result[0]["start_line"] == 100_000
    assert 2 <= signal.calls <= 100  # Start/end plus one durable check per 1024 lines.


@pytest.mark.parametrize("cancel_at", [1, 2, 4])
def test_evidence_rejects_cancellation_at_start_during_scan_and_at_end(tmp_path, cancel_at):
    rows, evidence = deep_log_evidence(tmp_path, 2049)
    signal = CountingCancellation(cancel_at)
    with pytest.raises(ValueError, match="已停止"):
        validate_evidence(evidence, tmp_path, rows, signal)
    assert signal.calls == cancel_at
