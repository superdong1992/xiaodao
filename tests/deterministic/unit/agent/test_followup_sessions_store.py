"""Native follow-up sessions keep their own durable, transactional checkpoint."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from types import SimpleNamespace
import uuid

import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.agent.store import AgentStore
from problem_locator.followup.models import FollowupRequest, FollowupSource
from problem_locator.followup.sessions import FollowupSessions
from problem_locator.followup.store import FollowupStore
from tests.deterministic.unit.storage.test_state_repository import _open

NOW = "2026-09-24T01:00:00.000Z"
OWNER = "a" * 64
SESSION = "00000000-0000-0000-0000-000000000101"


@pytest.fixture
def system(tmp_path):
    repository = _open(tmp_path)
    clock = SimpleNamespace(now=lambda: NOW)
    agent = AgentStore(repository, clock=clock, runtime_epoch="test")
    created = agent.create_conversation("create", owner_key=OWNER)
    store = FollowupStore(repository, clock=clock)
    report = "# 定位报告\n连接池出现排队。\n"
    source = FollowupSource(str(uuid.uuid4()), str(uuid.uuid4()), "为什么超时？", report,
        hashlib.sha256(report.encode()).hexdigest(), "GENERIC", False, NOW)
    yield SimpleNamespace(repository=repository, agent=agent, store=store, sessions=store.sessions,
        source=source, cid=created.conversation_id, rid=created.run_id, clock=clock)
    repository.close()


def start(system, request="first", *, cid=None, rid=None, source=None):
    receipt = system.store.submit(cid or system.cid, rid or system.rid,
        FollowupRequest(request_id=request, text=request), source or system.source, OWNER)
    task = system.store.claim_task()
    assert task["followup_id"] == receipt.followup_id
    return task


def complete(system, task):
    assert system.store.finish_task(task["followup_id"], answer="结合前文，原因是连接池排队。")


def task_status(system, task):
    with system.repository.database_read() as db:
        return db.execute("SELECT status FROM agent_followup_tasks WHERE followup_id=?",
            (task["followup_id"],)).fetchone()[0]


def test_session_schema_is_additive_and_does_not_create_config_directories(system):
    with system.repository.database_read() as db:
        assert db.execute("SELECT value FROM agent_followup_metadata WHERE key='storage_version'").fetchone()[0] == "2"
        assert db.execute("SELECT value FROM agent_followup_session_metadata WHERE key='storage_version'").fetchone()[0] == "1"
        original = db.execute("SELECT * FROM agent_conversation_runs").fetchall()
    FollowupSessions(system.store)
    assert system.sessions.get(system.cid) is None
    assert not (system.repository.layout.resources / "followup-sessions").exists()
    with system.repository.database_read() as db:
        assert db.execute("SELECT * FROM agent_conversation_runs").fetchall() == original


@pytest.mark.parametrize("damage", ["version", "missing_table", "missing_column"])
def test_session_schema_damage_is_reported_without_rewriting_old_storage(system, damage):
    with system.repository.database_transaction() as db:
        if damage == "version":
            db.execute("UPDATE agent_followup_session_metadata SET value='999'")
        elif damage == "missing_table":
            db.execute("DROP TABLE agent_followup_sessions")
        else:
            db.execute("ALTER TABLE agent_followup_sessions DROP COLUMN report_sha256")
    with pytest.raises(AgentStoreError) as caught:
        FollowupSessions(system.store)
    assert caught.value.code == "STATE_SCHEMA_UNSUPPORTED"
    with system.repository.database_read() as db:
        assert db.execute("SELECT value FROM agent_followup_metadata WHERE key='storage_version'").fetchone()[0] == "2"


def test_completed_turn_checkpoints_once_and_resumes_after_store_restart(system):
    task = start(system)
    previous = system.sessions.begin(task)
    assert previous["session_id"] is None and previous["last_order"] == 0
    system.sessions.capture_session(task, SESSION)
    system.sessions.capture_session(task, SESSION)
    complete(system, task)
    saved = system.sessions.get(system.cid)
    assert saved["last_followup_id"] == task["followup_id"]
    assert saved["last_order"] > 0
    assert saved["report_run_id"] == system.rid
    assert saved["report_sha256"] == system.source.report_sha256
    system.store = FollowupStore(system.repository, clock=system.clock)
    system.sessions = system.store.sessions
    second = start(system, "second")
    previous = system.sessions.begin(second)
    assert previous["session_id"] == SESSION
    assert previous["previous_status"] == "COMPLETED"
    assert system.sessions.history(second, previous["last_order"]) == []
    assert [item["followup_id"] for item in system.sessions.history(second)] == [task["followup_id"]]


def test_legacy_turn_between_native_turns_preserves_checkpoint_and_is_loaded_as_increment(system):
    first = start(system)
    system.sessions.begin(first)
    system.sessions.capture_session(first, SESSION)
    complete(system, first)
    saved = system.sessions.get(system.cid)
    legacy = start(system, "legacy")
    complete(system, legacy)
    assert system.sessions.get(system.cid) == saved
    resumed = start(system, "resumed")
    previous = system.sessions.begin(resumed)
    assert previous["session_id"] == SESSION
    assert [item["followup_id"] for item in system.sessions.history(resumed, previous["last_order"])] == [legacy["followup_id"]]


@pytest.mark.parametrize("terminal", ["FAILED", "CANCELLED", "INTERRUPTED"])
def test_bound_session_survives_unsuccessful_turn_and_reports_previous_status(system, terminal):
    task = start(system)
    system.sessions.begin(task)
    system.sessions.capture_session(task, SESSION)
    if terminal == "FAILED":
        system.store.finish_task(task["followup_id"], code="AGENT_FOLLOWUP_FAILED")
    elif terminal == "CANCELLED":
        system.store.stop(system.cid, system.rid, task["followup_id"], "stop", OWNER)
        system.store.finish_task(task["followup_id"], answer="迟到的回答")
    else:
        system.store.recover()
    second = start(system, "second")
    previous = system.sessions.begin(second)
    assert previous["session_id"] == SESSION and previous["previous_status"] == terminal
    assert previous["last_order"] == 0 and previous["last_followup_id"] is None
    assert system.sessions.history(second)[0]["status"] == terminal
    system.sessions.capture_session(second, SESSION)
    complete(system, second)


def test_unknown_initial_session_is_not_silently_recreated(system):
    first = start(system)
    system.sessions.begin(first)
    system.store.finish_task(first["followup_id"], interrupted=True)
    second = start(system, "second")
    with pytest.raises(AgentStoreError, match="未能保存会话标识"):
        system.sessions.begin(second)
    assert system.sessions.get(system.cid)["active_followup_id"] == first["followup_id"]
    assert system.sessions.get(system.cid)["session_id"] is None


def test_duplicate_begin_cannot_launch_the_same_turn_twice(system):
    task = start(system)
    def begin():
        try:
            system.sessions.begin(task)
            return "started"
        except AgentStoreError:
            return "rejected"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: begin(), range(2))) == ["rejected", "started"]


@pytest.mark.parametrize("change", ["cancel", "delete", "epoch", "task"])
def test_late_init_callback_is_rejected(system, change):
    task = start(system)
    system.sessions.begin(task)
    if change == "cancel":
        system.store.stop(system.cid, system.rid, task["followup_id"], "stop", OWNER)
    elif change == "delete":
        with system.repository.database_transaction() as db:
            db.execute("UPDATE agent_conversations SET deleted_at=? WHERE conversation_id=?", (NOW, system.cid))
    elif change == "epoch":
        with system.repository.database_transaction() as db:
            db.execute("UPDATE agent_followup_tasks SET epoch='new-process' WHERE followup_id=?", (task["followup_id"],))
    else:
        with system.repository.database_transaction() as db:
            db.execute("UPDATE agent_followup_sessions SET active_followup_id=? WHERE conversation_id=?",
                (str(uuid.uuid4()), system.cid))
    with pytest.raises(AgentStoreError):
        system.sessions.capture_session(task, SESSION)
    assert system.sessions.get(system.cid)["session_id"] is None


def test_capture_rejects_invalid_or_changed_session_identity(system):
    task = start(system)
    system.sessions.begin(task)
    with pytest.raises(AgentStoreError, match="会话标识无效"):
        system.sessions.capture_session(task, "not-a-session")
    system.sessions.capture_session(task, SESSION)
    with pytest.raises(AgentStoreError, match="不一致"):
        system.sessions.capture_session(task, str(uuid.uuid4()))
    assert system.sessions.get(system.cid)["session_id"] == SESSION


def test_answer_and_checkpoint_roll_back_together_on_database_failure(system, monkeypatch):
    task = start(system)
    system.sessions.begin(task)
    system.sessions.capture_session(task, SESSION)
    checkpoint = system.sessions.checkpoint
    def fail_after_checkpoint(db, row):
        checkpoint(db, row)
        raise OSError("injected database write failure")
    monkeypatch.setattr(system.sessions, "checkpoint", fail_after_checkpoint)
    with pytest.raises(OSError):
        complete(system, task)
    assert task_status(system, task) == "RUNNING"
    assert system.sessions.get(system.cid)["last_order"] == 0
    assert system.sessions.get(system.cid)["session_id"] == SESSION
    monkeypatch.setattr(system.sessions, "checkpoint", checkpoint)
    complete(system, task)
    assert system.sessions.get(system.cid)["last_followup_id"] == task["followup_id"]


def test_completed_answer_requires_captured_session_before_checkpoint(system):
    task = start(system)
    system.sessions.begin(task)
    with pytest.raises(AgentStoreError, match="未能保存会话"):
        complete(system, task)
    assert task_status(system, task) == "RUNNING"
    assert system.sessions.get(system.cid)["last_order"] == 0


def test_history_uses_conversation_storage_order_across_reports_and_excludes_other_conversations(system):
    first = start(system)
    system.sessions.begin(first)
    system.sessions.capture_session(first, SESSION)
    complete(system, first)
    checkpoint = system.sessions.get(system.cid)["last_order"]
    other = system.agent.create_conversation("other", owner_key=OWNER)
    unrelated = start(system, "unrelated", cid=other.conversation_id, rid=other.run_id)
    system.store.finish_task(unrelated["followup_id"], code="AGENT_FOLLOWUP_FAILED")
    with system.repository.database_transaction() as db:
        next_run = system.agent._new_run(db, system.agent._load(db, system.cid))
    second = start(system, "second", rid=next_run["run_id"])
    previous = system.sessions.begin(second)
    assert previous["report_run_id"] == system.rid
    assert previous["session_id"] == SESSION
    system.store.finish_task(second["followup_id"], code="AGENT_FOLLOWUP_FAILED")
    third = start(system, "third", rid=next_run["run_id"])
    assert [item["followup_id"] for item in system.sessions.history(third)] == [first["followup_id"], second["followup_id"]]
    assert [item["followup_id"] for item in system.sessions.history(third, checkpoint)] == [second["followup_id"]]
    assert [item["ordinal"] for item in system.sessions.history(third)] == [1, 1]


def test_session_state_and_files_survive_log_expiry_but_conversation_purge_removes_mapping(system):
    task = start(system)
    system.sessions.begin(task)
    system.sessions.capture_session(task, SESSION)
    complete(system, task)
    config = system.sessions.config_dir(system.cid)
    session_file = config / "native-session.jsonl"
    session_file.write_text("durable session", encoding="utf-8")
    with system.repository.database_transaction() as db:
        system.store.expire_resources(db, system.cid, system.rid)
    assert system.sessions.get(system.cid)["session_id"] == SESSION
    assert session_file.read_text(encoding="utf-8") == "durable session"
    with system.repository.database_transaction() as db:
        system.store.purge(db, system.cid, run_id=system.rid)
    assert system.sessions.get(system.cid)["session_id"] == SESSION
    with system.repository.database_transaction() as db:
        system.store.purge(db, system.cid)
    assert system.sessions.get(system.cid) is None


def test_config_roots_are_stable_isolated_and_reject_path_traversal(system):
    first = system.sessions.config_dir(system.cid)
    second_cid = str(uuid.uuid4())
    second = system.sessions.config_dir(second_cid)
    assert first == system.repository.layout.data_root / system.sessions.relative_root(system.cid) / "config"
    assert first != second and first.is_dir() and second.is_dir()
    assert system.sessions.config_dir(system.cid) == first
    if os.name == "posix":
        assert all(path.stat().st_mode & 0o777 == 0o700 for path in (first.parent.parent, first.parent, first))
    with pytest.raises(ValueError):
        system.sessions.config_dir("../other")


def test_config_accepts_cli_debug_latest_symlink_across_repeated_turns(system):
    config = system.sessions.config_dir(system.cid)
    debug = config / "debug"
    debug.mkdir()
    log = debug / f"{SESSION}.txt"
    log.write_text("CLI debug log", encoding="utf-8")
    latest = debug / "latest"
    try:
        latest.symlink_to(log)
    except OSError:
        pytest.skip("当前系统不允许创建测试符号链接。")
    assert system.sessions.config_dir(system.cid) == config
    assert system.sessions.config_dir(system.cid) == config
    assert latest.is_symlink() and log.read_text(encoding="utf-8") == "CLI debug log"


def test_config_rejects_symlink_root(system, tmp_path):
    root = system.repository.layout.data_root / system.sessions.relative_root(system.cid)
    root.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "unrelated-directory"
    outside.mkdir()
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前系统不允许创建测试符号链接。")
    with pytest.raises(ValueError):
        system.sessions.config_dir(system.cid)
    assert not (outside / "config").exists()
