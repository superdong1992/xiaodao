"""Slow attachment adoption must leave unrelated conversations runnable."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.agent.models import AgentStoreError
from tests.deterministic.unit.storage.test_state_repository import CASE_ID, _open


def test_blocked_attachment_import_does_not_hold_the_intake_worker(tmp_path, monkeypatch):
    repository = _open(tmp_path)
    store = AgentStore(repository)
    case = SimpleNamespace(case_id=CASE_ID, case_revision=1)
    application = SimpleNamespace(get_case=lambda _: SimpleNamespace(case_view=case))
    service = AgentConversationService(store, application, SimpleNamespace(), repository.layout)
    first = store.create_conversation("large-upload").conversation_id
    second = store.create_conversation("unrelated").conversation_id
    entered, release, advanced = threading.Event(), threading.Event(), threading.Event()
    errors, submissions = [], []

    def importer(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        kwargs["check_cancelled"]()
        return "00000000-0000-4000-8000-000000000703"

    def advance(cid):
        if cid == first:
            return service._supplement(cid, case, {}, ["selected"], "attachment")
        advanced.set()
        return True

    def first_call():
        try:
            service.run_once(first)
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(service.uploads, "import_into_case", importer)
    monkeypatch.setattr(service, "_advance", advance)
    monkeypatch.setattr(service, "_execute_command", lambda *args, **kwargs: submissions.append(args[2]))
    worker = threading.Thread(target=first_call)
    worker.start()
    try:
        assert entered.wait(2)
        assert service.run_once(second), "会话 A 导入附件期间，会话 B 必须仍能推进"
        assert advanced.is_set() and not release.is_set()
    finally:
        release.set()
        worker.join(2)
        assert service.shutdown(2)
        repository.close()
    assert not worker.is_alive() and not errors
    assert len(submissions) == 1


def test_manual_batch_releases_unsubmitted_task_when_first_storage_write_fails(tmp_path, monkeypatch):
    repository = _open(tmp_path)
    store = AgentStore(repository)
    case = SimpleNamespace(case_id=CASE_ID, case_revision=1)
    service = AgentConversationService(store, SimpleNamespace(get_case=lambda _: SimpleNamespace(case_view=case)),
        SimpleNamespace(), repository.layout)
    cids = [store.create_conversation(key).conversation_id for key in ("first", "second")]
    monkeypatch.setattr(store, "pending_conversations", lambda: cids)
    monkeypatch.setattr(service, "_advance", lambda cid: service._supplement(cid, case, {}, ["selected"], "attachment"))

    def failed(*args, **kwargs):
        raise RuntimeError("database failed")

    monkeypatch.setattr(service.uploads, "import_into_case", failed)
    monkeypatch.setattr(store, "fail_conversation", failed)
    try:
        with pytest.raises(RuntimeError, match="database failed"):
            service.run_once()
        assert service._active_runs == {}
        assert service._attachment_workers._tasks == {}
        assert all(not service.usage_guard.active(cid) for cid in cids)
    finally:
        assert service.shutdown(2)
        repository.close()


@pytest.fixture
def scheduling(tmp_path):
    repository = _open(tmp_path)
    store = AgentStore(repository)
    case = SimpleNamespace(case_id=CASE_ID, case_revision=1)
    service = AgentConversationService(store, SimpleNamespace(get_case=lambda _: SimpleNamespace(case_view=case)),
        SimpleNamespace(), repository.layout)
    yield SimpleNamespace(store=store, service=service, case=case)
    assert service.shutdown(2)
    repository.close()


def _idle(service):
    workers = service._attachment_workers
    with workers._condition:
        assert workers._condition.wait_for(lambda: not workers._tasks, 2)


def test_background_handoff_precedes_later_blocked_intake(scheduling, monkeypatch):
    s = scheduling
    first, second = [s.store.create_conversation(key).conversation_id for key in ("attachment", "intake")]
    imported, intake_entered, release = [threading.Event() for _ in range(3)]
    completed = set()
    monkeypatch.setattr(s.store, "pending_conversations", lambda: [cid for cid in (first, second) if cid not in completed])

    def advance(cid):
        if cid == first:
            return s.service._supplement(cid, s.case, {}, ["selected"], "attachment")
        intake_entered.set()
        assert release.wait(5)
        completed.add(cid)
        return True

    def importer(*args, **kwargs):
        imported.set()
        return "00000000-0000-4000-8000-000000000703"

    monkeypatch.setattr(s.service, "_advance", advance)
    monkeypatch.setattr(s.service.uploads, "import_into_case", importer)
    monkeypatch.setattr(s.service, "_execute_command", lambda cid, *args, **kwargs: completed.add(cid))
    s.service.start()
    try:
        assert intake_entered.wait(2)
        assert imported.wait(1), "后续会话的 INTAKE 不应阻止已准备的附件任务入队"
        assert not release.is_set()
    finally:
        release.set()
        assert s.service.shutdown(2)


def test_background_has_two_slots_and_still_advances_an_unrelated_conversation(scheduling, monkeypatch):
    s = scheduling
    cids = [s.store.create_conversation(str(i)).conversation_id for i in range(4)]
    messages = {cid: s.store.submit_message(cid, "input", "附件补充").message_id for cid in cids[:3]}
    advances = {cid: 0 for cid in cids[:3]}
    entered = {cid: threading.Event() for cid in cids[:3]}
    releases = {cid: threading.Event() for cid in cids[:3]}
    ordinary = threading.Event()
    completed = set()
    imports = []
    monkeypatch.setattr(s.store, "pending_conversations", lambda: [cid for cid in cids if cid not in completed])

    def advance(cid):
        if cid == cids[-1]:
            completed.add(cid)
            ordinary.set()
            return True
        advances[cid] += 1
        # The real specialized intake writes PROCESSING before it reaches
        # attachment reservation. That write wakes the ordinary worker.
        s.store.set_message_status(cid, messages[cid], "PROCESSING")
        return s.service._supplement(cid, s.case, {}, ["selected"], "attachment")

    def importer(cid, *args, **kwargs):
        imports.append(cid)
        entered[cid].set()
        assert releases[cid].wait(5)
        kwargs["check_cancelled"]()
        return "00000000-0000-4000-8000-000000000703"

    monkeypatch.setattr(s.service, "_advance", advance)
    monkeypatch.setattr(s.service.uploads, "import_into_case", importer)
    monkeypatch.setattr(s.service, "_execute_command", lambda cid, *args, **kwargs: completed.add(cid))
    s.service.start()
    try:
        assert entered[cids[0]].wait(2) and entered[cids[1]].wait(2)
        assert ordinary.wait(2)
        assert not entered[cids[2]].is_set()
        assert len(s.service._attachment_workers._tasks) == 2
        assert len(s.service._attachment_workers._threads) == 2
        signal = s.service._active_runs[(cids[0], s.store.get_run(cids[0])["run_id"])]
        for _ in range(4):
            assert not s.service.run_once(cids[0])
            s.service.run_once(cids[2])
        assert advances[cids[2]] == 1, "槽满后不能反复写入并唤醒自身"
        assert s.service._active_runs[(cids[0], s.store.get_run(cids[0])["run_id"])] is signal
        assert s.service.usage_guard.acquire_cleanup_if_idle(cids[0]) is None
        releases[cids[0]].set()
        assert entered[cids[2]].wait(2)
        assert s.store.get_run(cids[2])["status"] != "FAILED"
    finally:
        for release in releases.values():
            release.set()
        assert s.service.shutdown(2)
    assert len(imports) == len(set(imports)) == 3
    assert s.service._active_runs == {}
    assert all(not s.service.usage_guard.active(cid) for cid in cids)


@pytest.mark.parametrize("action", ["stop", "delete", "shutdown"])
def test_active_import_keeps_lease_and_rejects_late_submission(scheduling, monkeypatch, action):
    s = scheduling
    created = s.store.create_conversation("import")
    cid, rid = created.conversation_id, created.run_id
    entered, release = threading.Event(), threading.Event()
    submissions, results = [], []

    def importer(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        kwargs["check_cancelled"]()
        return "00000000-0000-4000-8000-000000000703"

    monkeypatch.setattr(s.service, "_advance", lambda cid: s.service._supplement(cid, s.case, {}, ["selected"], "attachment"))
    monkeypatch.setattr(s.service.uploads, "import_into_case", importer)
    monkeypatch.setattr(s.service, "_execute_command", lambda *args, **kwargs: submissions.append(args))
    thread = threading.Thread(target=lambda: results.append(s.service.run_once(cid)))
    thread.start()
    try:
        assert entered.wait(2)
        if action == "stop":
            assert s.service.stop_conversation(cid, "stop", rid).status == "CANCELLING"
            s.service.control_once()
            assert s.store.get_run(cid)["status"] == "CANCELLING"
        elif action == "delete":
            assert s.service.delete_conversation(cid).status == "DELETING"
            with pytest.raises(AgentStoreError) as missing:
                s.service.get_conversation(cid)
            assert missing.value.status_code == 404
        else:
            assert not s.service.shutdown(0)
        assert s.service.usage_guard.active(cid)
        assert s.service.usage_guard.acquire_cleanup_if_idle(cid) is None
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and results == [False] and not submissions
    _idle(s.service)
    assert s.service._active_runs == {} and not s.service.usage_guard.active(cid)
    if action == "stop":
        s.service.control_once()
        assert s.store.get_run(cid)["status"] == "CANCELLED"
    assert s.store.get_run(cid, rid, deleted=True)["status"] != "FAILED"


def test_shutdown_between_reserve_and_submit_releases_handoff(scheduling, monkeypatch):
    s = scheduling
    cid = s.store.create_conversation("reserved").conversation_id
    entered, release = threading.Event(), threading.Event()
    submit = s.service._attachment_workers.submit
    imported, results = [], []

    def paused(task):
        entered.set()
        assert release.wait(5)
        return submit(task)

    monkeypatch.setattr(s.service._attachment_workers, "submit", paused)
    monkeypatch.setattr(s.service, "_advance", lambda cid: s.service._supplement(cid, s.case, {}, ["selected"], "attachment"))
    monkeypatch.setattr(s.service.uploads, "import_into_case", lambda *args, **kwargs: imported.append(args))
    thread = threading.Thread(target=lambda: results.append(s.service.run_once(cid)))
    thread.start()
    try:
        assert entered.wait(2)
        assert s.service.usage_guard.active(cid) and s.service._active_runs
        assert s.service.shutdown(0)
        assert not s.service.usage_guard.active(cid) and not s.service._active_runs
    finally:
        release.set()
        thread.join(2)
    assert results == [False] and not imported and not thread.is_alive()


def test_import_failure_phase_is_not_overwritten_by_other_intake(scheduling, monkeypatch):
    s = scheduling
    first, second = [s.store.create_conversation(key).conversation_id for key in ("import", "ordinary")]
    entered, release = threading.Event(), threading.Event()

    def importer(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        raise OSError("controlled import error")

    def advance(cid):
        if cid == first:
            return s.service._supplement(cid, s.case, {}, ["selected"], "attachment")
        s.service._phase = "INTAKE"
        return True

    monkeypatch.setattr(s.service, "_advance", advance)
    monkeypatch.setattr(s.service.uploads, "import_into_case", importer)
    thread = threading.Thread(target=lambda: s.service.run_once(first))
    thread.start()
    try:
        assert entered.wait(2)
        assert s.service.run_once(second)
    finally:
        release.set()
        thread.join(2)
    failure = s.store.get_run(first)["failure"]
    assert next(item["actual"] for item in failure["details"] if item["field"] == "phase") == "IMPORT_ATTACHMENT"
    assert not thread.is_alive()


def test_phase_remains_available_to_minimal_service_fakes():
    service = object.__new__(AgentConversationService)
    assert service._phase == "AGENT"
    service._phase = "CASE_QUERY"
    assert service._phase == "CASE_QUERY"
