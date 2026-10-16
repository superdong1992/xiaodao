"""Deferred imports retain generic selection and continuation run ownership."""
from __future__ import annotations

import threading
from types import SimpleNamespace
import uuid

from problem_locator.contracts import CaseStatus
from tests.deterministic.unit.agent.test_generic_log_messages import generic_web, _ready, JOB_ID
from tests.deterministic.unit.agent.test_report_continuation import system, create, confirm, upload
from tests.deterministic.unit.storage.test_state_repository import CASE_ID


def test_generic_multiple_imports_follow_replaced_selection_without_intake(generic_web, monkeypatch):
    store, service, app, cid, rid = generic_web
    first, second, replacement = [_ready(store, cid, str(i), str(i) * 64) for i in range(3)]
    requirement = SimpleNamespace(requirement_id=str(uuid.uuid4()), name="log_archive", prompt="请上传日志。",
        kind=SimpleNamespace(value="ATTACHMENT"), status=SimpleNamespace(value="OPEN"),
        supplement_policy=SimpleNamespace(value="MISSING_ONLY"), requested_by_job_id=JOB_ID,
        constraints=SimpleNamespace(min_count=1, max_count=2, allowed_content_types=["application/zip"]))
    case = app.aggregate.case
    case.status = CaseStatus.WAITING_ATTACHMENT
    case.active_job_id = None
    case.diagnosis_state = SimpleNamespace(pending_requirements=[requirement])
    case.pending_requirements = [requirement]
    case.selected_skill_ref = None
    case.raw_problem_text = "设备偶发重启"
    with store.repository.database_transaction() as db:
        store._project_case(db, store._load(db, cid), app.aggregate)
    app.get_case = lambda _: SimpleNamespace(case_view=case)
    store.submit_message(cid, "old-selection", "补充客户端和服务端日志", [first, second])
    entered, release = threading.Event(), threading.Event()
    imports, commands, errors, results = [], [], [], []

    def importer(owner, case_id, aid, *args, **kwargs):
        assert owner == cid and kwargs["run_id"] == rid
        imports.append(aid)
        if aid == first:
            entered.set()
            assert release.wait(5)
        kwargs["check_cancelled"]()
        store.bind_attachment(aid, aid, run_id=rid)
        return aid

    def adopt(owner, label, command, **kwargs):
        commands.append(command)
        store.set_message_status(owner, kwargs["message_id"], "APPLIED")

    def run():
        try:
            results.append(service.run_once(cid))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(service.uploads, "import_into_case", importer)
    monkeypatch.setattr(service, "_execute_command", adopt)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(2)
        store.submit_message(cid, "replacement", "改用这一份完整日志", [replacement])
        assert not service.run_once(cid)
        assert len(service._attachment_workers._tasks) == 1
    finally:
        release.set()
        thread.join(3)
        assert service.shutdown(2)
    assert not thread.is_alive() and not errors and results == [True]
    assert imports == [first, second, replacement]
    assert len(commands) == 1 and commands[0].attachment_ids == [replacement]
    assert commands[0].generic_supplement_text == "补充客户端和服务端日志\n\n改用这一份完整日志"
    assert store.get_intake_state(cid)["pending"] is False


def test_continuation_imports_are_bound_to_new_run_while_other_conversation_advances(system, monkeypatch):
    s = system
    aids = [upload(s, "client.zip", b"client"), upload(s, "server.zip", b"server")]
    draft = create(s, effective_problem_text="修正时间并补充两份日志", log_mode="REPLACE", attachment_ids=aids)
    new = confirm(s, draft)
    other = s.store.create_conversation("other").conversation_id
    case = SimpleNamespace(case_id=CASE_ID, case_revision=1)
    monkeypatch.setattr(s.agent.application, "get_case", lambda _: SimpleNamespace(case_view=case), raising=False)
    entered, release, advanced = [threading.Event() for _ in range(3)]
    imports, commands, errors = [], [], []

    def importer(cid, case_id, aid, *args, **kwargs):
        assert kwargs["run_id"] == new.run_id
        assert s.store.get_run(cid)["run_id"] == new.run_id
        if not imports:
            entered.set()
            assert release.wait(5)
        kwargs["check_cancelled"]()
        s.store.bind_attachment(aid, aid, run_id=kwargs["run_id"])
        imports.append(aid)
        return aid

    def advance(cid):
        if cid == s.cid:
            return s.agent._supplement(cid, case, {}, aids, "continuation", selected_attachment_ids=aids)
        advanced.set()
        return True

    def run():
        try:
            s.agent.run_once(s.cid)
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(s.agent, "_advance", advance)
    monkeypatch.setattr(s.agent.uploads, "import_into_case", importer)
    monkeypatch.setattr(s.agent, "_execute_command", lambda cid, label, command, **kwargs: commands.append(command))
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(2)
        assert s.agent.run_once(other) and advanced.is_set()
        assert s.agent.usage_guard.acquire_cleanup_if_idle(s.cid) is None
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and not errors
    assert imports == aids and len(commands) == 1 and commands[0].attachment_ids == aids
    assert all(s.store.get_attachment_import(aid, new.run_id) == aid for aid in aids)
    assert all(s.store.get_attachment_import(aid, s.rid) is None for aid in aids)
    assert s.store.get_run(s.cid, s.rid)["report_available"]
