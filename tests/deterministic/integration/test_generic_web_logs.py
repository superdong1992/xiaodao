"""Actual Web/core/dispatcher journeys for generic log attachment adoption."""
from __future__ import annotations

import threading
import hashlib
import io
import zipfile

import pytest

from problem_locator.contracts import (
    CreateCase, DiagnosisMode, ErrorCode, ExecutionFailure, ExecutionStage, JobType, MarkInitialLogArchiveExpected,
    SubmitSupplement,
)
from tests.deterministic.integration.test_website_agent import OWNER_KEY, _preupload, website


def _hold_generic(stack, monkeypatch):
    original = stack.runtime.execute
    entered, cancelled, allow_exit = threading.Event(), threading.Event(), threading.Event()
    jobs, all_started = [], []
    monkeypatch.setattr(stack.catalog, "_route_skill_refs", [])
    def execute(job, cancellation):
        all_started.append(job.job_id)
        if job.diagnosis_mode is not DiagnosisMode.GENERIC or (job.generic_log_archive_expected and not job.attachment_refs):
            return original(job, cancellation)
        jobs.append(job)
        entered.set()
        assert cancellation.wait(15), "测试必须显式停止受控诊断"
        cancelled.set()
        assert allow_exit.wait(15), "旧执行退出前不能启动同Case替换任务"
        return stack.runtime._publisher.publish_failure(job, ExecutionFailure(
            code=ErrorCode.BACKEND_CANCELLED, stage=ExecutionStage.BACKEND_EXECUTE,
            message="测试诊断已取消。", retryable=False, details=[]))
    monkeypatch.setattr(stack.runtime, "execute", execute)
    return entered, cancelled, allow_exit, jobs, all_started


def _stop(service, stack, cid, allow_exit):
    allow_exit.set()
    run_id = service.store.get_run(cid)["run_id"]
    service.stop_conversation(cid, "test-stop", run_id)
    service.control_once()
    assert stack.scheduler.wait_until_idle(15)
    service.control_once()


def test_first_web_archive_reaches_generic_job_without_time_or_process_inputs(website, monkeypatch):
    stack, store, engine, service, client = website
    entered, _, allow_exit, jobs, _ = _hold_generic(stack, monkeypatch)
    cid = service.create_conversation("generic-first", owner_key=OWNER_KEY).conversation_id
    attachment = _preupload(client, f"/api/v1/agent/conversations/{cid}")
    service.send_message(cid, "problem", "交换机偶发重启", [attachment])
    try:
        assert service.run_once(cid)
        assert stack.scheduler.wait_until_idle(15)
        waiting = store.get_status(cid)
        assert waiting.case_status == "WAITING_ATTACHMENT", waiting
        case = stack.application.get_case(waiting.case_id).case_view
        assert [(item.kind.value, item.name) for item in case.pending_requirements if item.status.value == "OPEN"] == [
            ("ATTACHMENT", "log_archive")]
        assert engine.calls == [] and jobs == []
        assert service.run_once(cid)
        assert entered.wait(15)
        assert engine.calls == [] and len(jobs) == 1
        assert jobs[0].generic_problem_text == "交换机偶发重启"
        assert len(jobs[0].attachment_refs) == 1
        assert jobs[0].attachment_refs == [store.get_attachment_import(attachment, waiting.run_id)]
        assert jobs[0].generic_log_archive_expected
        assert store.get_conversation(cid).messages[0].status == "APPLIED"
    finally:
        _stop(service, stack, cid, allow_exit)


def test_running_web_log_restart_cancels_and_waits_for_old_worker_before_replacement(website, monkeypatch):
    stack, store, engine, service, client = website
    entered, cancelled, allow_exit, jobs, all_started = _hold_generic(stack, monkeypatch)
    created = service.create_conversation("generic-restart", owner_key=OWNER_KEY)
    cid = created.conversation_id
    service.send_message(cid, "problem", "设备偶发重启")
    try:
        assert service.run_once(cid)
        assert entered.wait(15)
        before = store.get_status(cid)
        old_job = jobs[0]
        attachment = _preupload(client, f"/api/v1/agent/conversations/{cid}")
        receipt = service.send_message(cid, "logs", "补充：掉电前出现异常告警", [attachment])
        assert cancelled.wait(5)
        restarted = store.get_status(cid)
        assert restarted.case_id == before.case_id and restarted.run_id == before.run_id
        assert restarted.job_id != old_job.job_id
        assert restarted.job_id not in all_started
        entered.clear()
        allow_exit.set()
        assert stack.scheduler.wait_until_idle(15)
        assert store.get_status(cid).case_status == "WAITING_ATTACHMENT"
        assert service.run_once(cid)
        assert entered.wait(15)
        assert len(jobs) == 2 and engine.calls == []
        current = jobs[-1]
        assert current.generic_problem_text == old_job.generic_problem_text == "设备偶发重启"
        assert current.generic_supplement_texts == ["补充：掉电前出现异常告警"]
        assert current.attachment_refs == [store.get_attachment_import(attachment, created.run_id)]
        state = stack.repository.read_snapshot(before.case_id)
        aggregate = state.cases[before.case_id]
        assert aggregate.jobs[old_job.job_id].status.value == "CANCELLED"
        assert len([job for job in aggregate.jobs.values() if job.job_type is JobType.ROUTE]) == 1
        assert service.send_message(cid, "logs", "补充：掉电前出现异常告警", [attachment]) == receipt
        assert len([event for event in store.list_events(cid, limit=500)
            if event.type == "message.accepted" and event.data["request_id"] == "logs"]) == 1
    finally:
        _stop(service, stack, cid, allow_exit)


def test_logs_arriving_during_route_are_adopted_before_first_generic_analysis(website, monkeypatch):
    stack, store, engine, service, client = website
    entered, _, allow_exit, jobs, _ = _hold_generic(stack, monkeypatch)
    route_entered, route_release = threading.Event(), threading.Event()
    execute = stack.runtime.execute
    def hold_route(job, cancellation):
        if job.job_type is JobType.ROUTE:
            route_entered.set()
            assert route_release.wait(15)
        return execute(job, cancellation)
    monkeypatch.setattr(stack.runtime, "execute", hold_route)
    created = service.create_conversation("logs-during-route", owner_key=OWNER_KEY)
    cid = created.conversation_id
    service.send_message(cid, "problem", "设备重启")
    try:
        assert service.run_once(cid)
        assert route_entered.wait(15)
        before = store.get_status(cid)
        attachment = _preupload(client, f"/api/v1/agent/conversations/{cid}")
        receipt = service.send_message(cid, "logs", "附加观察：指示灯闪烁", [attachment])
        snapshot = stack.repository.read_snapshot(before.case_id)
        assert snapshot.cases[before.case_id].case.initial_log_archive_expected
        assert store.get_status(cid).job_id == before.job_id
        route_release.set()
        assert stack.scheduler.wait_until_idle(15)
        assert store.get_status(cid).case_status == "WAITING_ATTACHMENT" and jobs == []
        assert service.run_once(cid)
        assert entered.wait(15)
        assert len(jobs) == 1 and jobs[0].attachment_refs
        assert jobs[0].generic_supplement_texts == ["附加观察：指示灯闪烁"]
        assert engine.calls == []
        assert service.send_message(cid, "logs", "附加观察：指示灯闪烁", [attachment]) == receipt
    finally:
        route_release.set()
        _stop(service, stack, cid, allow_exit)


def test_route_finishing_during_log_submission_retargets_same_message_to_generic_restart(website, monkeypatch):
    stack, store, _, service, client = website
    entered, cancelled, allow_exit, jobs, _ = _hold_generic(stack, monkeypatch)
    route_entered, route_release = threading.Event(), threading.Event()
    execute = stack.runtime.execute
    def hold_route(job, cancellation):
        if job.job_type is JobType.ROUTE:
            route_entered.set()
            assert route_release.wait(15)
        return execute(job, cancellation)
    monkeypatch.setattr(stack.runtime, "execute", hold_route)
    core_execute = type(stack.application).execute
    def finish_route_before_marker(app, command):
        if isinstance(command, MarkInitialLogArchiveExpected):
            route_release.set()
            assert entered.wait(15)
        return core_execute(app, command)
    monkeypatch.setattr(type(stack.application), "execute", finish_route_before_marker)
    cid = service.create_conversation("route-race", owner_key=OWNER_KEY).conversation_id
    service.send_message(cid, "problem", "设备重启")
    try:
        assert service.run_once(cid)
        assert route_entered.wait(15)
        attachment = _preupload(client, f"/api/v1/agent/conversations/{cid}")
        receipt = service.send_message(cid, "logs", "路由结束时补交日志", [attachment])
        assert cancelled.wait(5)
        assert len(jobs) == 1
        assert len([event for event in store.list_events(cid, limit=500)
            if event.type == "message.accepted" and event.data["request_id"] == "logs"]) == 1
        assert service.send_message(cid, "logs", "路由结束时补交日志", [attachment]) == receipt
        allow_exit.set()
        assert stack.scheduler.wait_until_idle(15)
        assert store.get_status(cid).case_status == "WAITING_ATTACHMENT"
    finally:
        route_release.set()
        _stop(service, stack, cid, allow_exit)


@pytest.mark.parametrize("boundary", ["before_freeze", "during_commit"])
def test_upload_message_cannot_fall_between_create_selection_and_case_binding(website, monkeypatch, boundary):
    stack, store, engine, service, client = website
    entered, _, allow_exit, jobs, _ = _hold_generic(stack, monkeypatch)
    create_entered, create_release, route_release = threading.Event(), threading.Event(), threading.Event()
    sent, sending = threading.Event(), threading.Event()
    errors = []
    runtime_execute = stack.runtime.execute
    def hold_route(job, cancellation):
        if job.job_type is JobType.ROUTE:
            assert route_release.wait(15)
        return runtime_execute(job, cancellation)
    monkeypatch.setattr(stack.runtime, "execute", hold_route)
    if boundary == "before_freeze":
        execute_command = service._execute_command
        def hold_selected(cid, label, command, **kwargs):
            if isinstance(command, CreateCase):
                create_entered.set()
                assert create_release.wait(15)
            return execute_command(cid, label, command, **kwargs)
        monkeypatch.setattr(service, "_execute_command", hold_selected)
    else:
        execute = type(stack.application).execute
        def hold_committing(app, command):
            if isinstance(command, CreateCase):
                create_entered.set()
                assert create_release.wait(15)
            return execute(app, command)
        monkeypatch.setattr(type(stack.application), "execute", hold_committing)
    cid = service.create_conversation("create-race", owner_key=OWNER_KEY).conversation_id
    attachment = _preupload(client, f"/api/v1/agent/conversations/{cid}")
    service.send_message(cid, "problem", "设备重启")
    def advance():
        try:
            assert service.run_once(cid)
        except BaseException as error:
            errors.append(error)
    def send():
        sending.set()
        try:
            service.send_message(cid, "logs", "创建任务时补交日志", [attachment])
            sent.set()
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=advance)
    sender = threading.Thread(target=send)
    worker.start()
    try:
        assert create_entered.wait(5)
        assert store.get_status(cid).case_id is None
        sender.start()
        assert sending.wait(5)
        if boundary == "before_freeze":
            assert sent.wait(5)
        else:
            assert not sent.wait(0.1)
        create_release.set()
        worker.join(15)
        sender.join(15)
        assert not worker.is_alive() and not sender.is_alive() and errors == []
        assert sent.is_set()
        case_id = store.get_status(cid).case_id
        assert stack.repository.read_snapshot(case_id).cases[case_id].case.initial_log_archive_expected
        route_release.set()
        assert stack.scheduler.wait_until_idle(15)
        assert jobs == [] and store.get_status(cid).case_status == "WAITING_ATTACHMENT"
        assert service.run_once(cid)
        assert entered.wait(15)
        assert len(jobs) == 1 and jobs[0].attachment_refs and engine.calls == []
        assert jobs[0].generic_supplement_texts == ["创建任务时补交日志"]
    finally:
        create_release.set()
        route_release.set()
        worker.join(15)
        if sender.ident is not None:
            sender.join(15)
        _stop(service, stack, cid, allow_exit)


def _upload_choice(service, cid, key):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("device.log", key)
    data = archive.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    attachment = service.prepare_attachment(cid, key, "logs.zip", "application/zip", len(data), digest)
    service.upload_attachment(attachment.attachment_id, attachment.attachment_id,
        "application/zip", len(data), digest, io.BytesIO(data))
    return attachment.attachment_id


@pytest.mark.parametrize("boundary", ["already_queued", "during_import_replace", "during_import_text"])
def test_generic_waiting_submission_adopts_latest_logs_and_all_accepted_text(website, monkeypatch, boundary):
    stack, store, engine, service, _ = website
    entered, _, allow_exit, jobs, _ = _hold_generic(stack, monkeypatch)
    cid = service.create_conversation("waiting-handoff", owner_key=OWNER_KEY).conversation_id
    first = _upload_choice(service, cid, "archive-A")
    second = _upload_choice(service, cid, "archive-B")
    third = _upload_choice(service, cid, "archive-C")
    service.send_message(cid, "problem", "设备偶发重启", [first])
    assert service.run_once(cid)
    assert stack.scheduler.wait_until_idle(15)
    import_entered, import_release = threading.Event(), threading.Event()
    errors = []
    importer = service.uploads.import_into_case
    def hold_import(cid, case_id, attachment_id, *args, **kwargs):
        result = importer(cid, case_id, attachment_id, *args, **kwargs)
        if attachment_id == first:
            import_entered.set()
            assert import_release.wait(15)
        return result
    def add_messages():
        service.send_message(cid, "observation", "重启前指示灯闪烁")
        if boundary != "during_import_text":
            service.send_message(cid, "choice-B", "第二份日志包含启动过程", [second])
            service.send_message(cid, "choice-C", "请以最后这份日志为准", [third])
        service.send_message(cid, "last-text", "重启后业务自行恢复")
    if boundary == "already_queued":
        add_messages()
        import_release.set()
    else:
        monkeypatch.setattr(service.uploads, "import_into_case", hold_import)
    def advance():
        try:
            assert service.run_once(cid)
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=advance)
    worker.start()
    try:
        if boundary != "already_queued":
            assert import_entered.wait(5)
            add_messages()
            assert jobs == []
            import_release.set()
        worker.join(15)
        assert not worker.is_alive() and errors == []
        assert entered.wait(15)
        assert len(jobs) == 1 and engine.calls == []
        view = store.get_conversation(cid)
        expected = first if boundary == "during_import_text" else third
        assert jobs[0].attachment_refs == [store.get_attachment_import(expected, view.run_id)]
        supplements = "\n\n".join(jobs[0].generic_supplement_texts)
        for message in view.messages[1:]:
            assert message.text in supplements
            assert message.status == "APPLIED"
        assert store.get_attachment_import(second, view.run_id) is None
    finally:
        import_release.set()
        worker.join(15)
        _stop(service, stack, cid, allow_exit)


def test_message_after_generic_supplement_freeze_waits_then_restarts(website, monkeypatch):
    stack, store, _, service, _ = website
    entered, cancelled, allow_exit, jobs, _ = _hold_generic(stack, monkeypatch)
    allow_exit.set()
    cid = service.create_conversation("supplement-commit-handoff", owner_key=OWNER_KEY).conversation_id
    first = _upload_choice(service, cid, "archive-A")
    second = _upload_choice(service, cid, "archive-B")
    service.send_message(cid, "problem", "设备偶发重启", [first])
    assert service.run_once(cid)
    assert stack.scheduler.wait_until_idle(15)
    commit_entered, commit_release, sent = threading.Event(), threading.Event(), threading.Event()
    errors = []
    execute = type(stack.application).execute
    def hold_commit(app, command):
        if isinstance(command, SubmitSupplement):
            commit_entered.set()
            assert commit_release.wait(15)
        return execute(app, command)
    monkeypatch.setattr(type(stack.application), "execute", hold_commit)
    def advance():
        try:
            service.run_once(cid)
        except BaseException as error:
            errors.append(error)
    def send():
        try:
            service.send_message(cid, "new-logs", "提交边界后的新日志", [second])
            sent.set()
        except BaseException as error:
            errors.append(error)
    worker, sender = threading.Thread(target=advance), threading.Thread(target=send)
    worker.start()
    try:
        assert commit_entered.wait(5)
        sender.start()
        assert not sent.wait(0.1)
        commit_release.set()
        worker.join(15)
        sender.join(15)
        assert not worker.is_alive() and not sender.is_alive() and errors == []
        assert sent.is_set()
        assert stack.scheduler.wait_until_idle(15)
        assert store.get_status(cid).case_status == "WAITING_ATTACHMENT"
        entered.clear()
        assert service.run_once(cid)
        assert entered.wait(15)
        current = jobs[-1]
        assert current.attachment_refs == [store.get_attachment_import(second, store.get_status(cid).run_id)]
        assert "提交边界后的新日志" in current.generic_supplement_texts
    finally:
        commit_release.set()
        worker.join(15)
        if sender.ident is not None:
            sender.join(15)
        _stop(service, stack, cid, allow_exit)
