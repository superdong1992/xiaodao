"""Asynchronous website intake; authoritative diagnosis stays in Case commands."""
from __future__ import annotations

import threading
import inspect
import time
import uuid
import weakref
from contextlib import contextmanager, nullcontext
from typing import Any

from problem_locator.contracts import (
    ApplicationPortError, ApplicationResponse, CancellationReason, CreateCase,
    PrepareAttachment, SubmitSupplement, CancelCase, RestartGenericDiagnosis, MarkInitialLogArchiveExpected,
)
from problem_locator.contracts.models import ProblemSpecInput, derive_attachment_filename_suffix
from problem_locator.diagnostics import log_event
from problem_locator.application.reports import result_source_job_id
from problem_locator.dispatch.cancellation import CancellationController

from .intake import (
    ClaudeIntakeEngine, IntakeAttachment, IntakeDecision, IntakeInput, IntakeMessage,
    IntakeRequirement, IntakeValue, build_initial_problem_spec, validate_intake_decision,
    intake_processing_receipt,
)
from .models import (AgentPublicFailure, AgentStoreError, ConversationReportView, PublicArtifactData,
                     ConversationDetail, ConversationArtifact, SendMessageRequest)
from .failures import exception_code, exception_details
from .uploads import ConversationUploads
from .usage import ConversationUsageGuard

_CLOSED = {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}
_CASE_DONE = {"RESOLVED", "PARTIALLY_RESOLVED", "UNRESOLVED", "FAILED", "CANCELLED", "INTERRUPTED"}
_COMMANDS = {item.__name__: item for item in (CreateCase, PrepareAttachment, SubmitSupplement)}


class _RunStopped(Exception):
    """Control flow only: user cancellation is never a diagnosis failure."""


class AgentConversationService:
    """One bounded intake worker; subscribing never starts or repeats model work."""

    def __init__(self, store, application, intake_engine, layout, *, memory_store=None, memory_enabled=False):
        self.store, self.application, self.intake_engine = store, application, intake_engine
        from problem_locator.memory.service import FeedbackService
        self.memory_store = memory_store
        self.memory_enabled = memory_enabled
        store.memory_store = memory_store
        self.feedback = None if memory_store is None else FeedbackService(self, memory_store, enabled=memory_enabled)
        self.uploads = ConversationUploads(store, application, layout)
        self.usage_guard = ConversationUsageGuard()
        self.cleanup = None
        self.dispatcher = None
        self._run_lock = threading.Lock()
        self._message_guard = threading.Lock()
        self._message_locks = weakref.WeakValueDictionary()
        self._active_runs = {}
        self._local = threading.local()
        intake = getattr(intake_engine, "intake", None)
        self._intake_accepts_cancellation = intake is not None and "cancellation" in inspect.signature(intake).parameters
        self._stop, self._wake = threading.Event(), threading.Event()
        self._control_wake = threading.Event()
        self._control_processing = threading.Lock()
        self._processing, self._lifecycle = threading.Lock(), threading.Lock()
        self._thread = None
        self._control_thread = None
        self._failure = None
        self._phase = "AGENT"
        store.on_change = self._changed

    def _changed(self, _conversation_id):
        self._wake.set()
        self._control_wake.set()

    def start(self, runtime_epoch=None):
        with self._lifecycle:
            if self._thread is not None:
                return
            if runtime_epoch is not None:
                self.store.runtime_epoch = runtime_epoch
            self.store.recover()
            self._thread = threading.Thread(target=self._run, name="agent-intake", daemon=True)
            self._control_thread = threading.Thread(target=self._run_control, name="agent-management", daemon=True)
            self._thread.start()
            self._control_thread.start()

    def shutdown(self, timeout_seconds=30):
        self._stop.set()
        self._wake.set()
        self._control_wake.set()
        if self.cleanup is not None:
            self.cleanup.shutdown(0)
        with self._run_lock:
            for signal in self._active_runs.values():
                signal.cancel(CancellationReason.SERVICE_SHUTDOWN)
        deadline = time.monotonic() + timeout_seconds
        for thread in (self._thread, self._control_thread):
            if thread is not None:
                thread.join(max(0.0, deadline - time.monotonic()))
        return all(thread is None or not thread.is_alive() for thread in (self._thread, self._control_thread))

    def _available(self):
        if self._stop.is_set() or self._failure is not None:
            raise AgentStoreError("AGENT_UNAVAILABLE", "会话服务暂时不可用，请稍后重试。", 503)
        operational = getattr(self.application, "operational_state", None)
        if operational is not None:
            operational.require_accepting()

    def create_conversation(self, request_id, *, owner_key=None):
        self._available()
        return self.store.create_conversation(request_id, owner_key=owner_key)

    @contextmanager
    def operation_lease(self, conversation_id, *, owner_key=None):
        with self.usage_guard.acquire(conversation_id):
            self.store.require_owner(conversation_id, owner_key)
            yield

    def send_message(self, conversation_id, request_id, text="", attachment_ids=None, *, owner_key=None):
        self._available()
        with self.operation_lease(conversation_id, owner_key=owner_key), self._message_lock(conversation_id):
            request = SendMessageRequest(request_id=request_id, text=text or "", attachment_ids=attachment_ids or [])
            receipt, pending = self.store.message_request(conversation_id, request)
            if receipt is not None:
                return receipt
            if pending is not None:
                return self._dispatch_generic_restart(pending, request)
            view = self.store.get_status(conversation_id)
            if (request.attachment_ids and view.case_id is not None and view.status not in _CLOSED
                    and view.report_state != "READY"):
                snapshot = self.store.repository.read_snapshot(view.case_id)
                aggregate = snapshot.cases.get(view.case_id)
                if aggregate is None or aggregate.case.status.value in _CASE_DONE:
                    raise AgentStoreError("AGENT_RUN_CHANGED", "本次定位已结束。", 409)
                job = None if aggregate is None else aggregate.jobs.get(aggregate.case.active_job_id)
                if self._is_generic_job(job) or (job is not None and job.job_type.value == "ROUTE"):
                    # Validate the selected archive before accepting or cancelling
                    # anything. Multiple selections must never pick the first.
                    records = [self.store.get_attachment(item) for item in request.attachment_ids]
                    if any(item.conversation_id != conversation_id or item.status not in {"READY", "IMPORTED"} for item in records):
                        raise AgentStoreError("AGENT_ATTACHMENT_NOT_READY", "附件不存在、未上传完成或不属于本会话。", 409)
                    if len(records) != 1 and self._is_generic_job(job):
                        raise AgentStoreError("AGENT_LOG_SELECTION_INVALID", "当前仅支持一份日志归档，请合并后上传，或重新选择一个附件。", 409)
                    for record in records:
                        derive_attachment_filename_suffix(record.name, record.content_type)
                    record = records[0] if len(records) == 1 else None
                    if self._is_generic_job(job) and record is not None and any(aggregate.attachments[item].sha256 == record.sha256 for item in job.attachment_refs
                            if item in aggregate.attachments):
                        raise AgentStoreError("AGENT_LOG_ALREADY_SELECTED", "这份日志已用于当前定位，无需重复提交。", 409)
                    identity = str(uuid.uuid5(uuid.UUID(view.run_id), request_id))
                    fields = dict(case_id=view.case_id, expected_case_revision=aggregate.case.case_revision,
                        source_job_id=job.job_id)
                    command = (RestartGenericDiagnosis(idempotency_key="agent-restart-" + identity,
                        supplement_text=request.text, **fields) if self._is_generic_job(job) else
                        MarkInitialLogArchiveExpected(idempotency_key="agent-route-logs-" + identity, **fields))
                    pending = self.store.freeze_generic_restart(conversation_id, request, command,
                        run_id=view.run_id, archive_sha256=record.sha256 if record is not None else None)
                    return self._dispatch_generic_restart(pending, request)
            return self.store.submit_message(conversation_id, request_id, request.text, request.attachment_ids)

    def _message_lock(self, conversation_id):
        with self._message_guard:
            return self._message_locks.setdefault(conversation_id, threading.RLock())

    @staticmethod
    def _is_generic_job(job):
        return job is not None and job.diagnosis_mode is not None and job.diagnosis_mode.value == "GENERIC"

    def _generic_waiting_job(self, case_view):
        if case_view.selected_skill_ref is not None:
            return None
        snapshot = self.store.repository.read_snapshot(case_view.case_id)
        aggregate = snapshot.cases[case_view.case_id]
        sources = [aggregate.jobs.get(item.requested_by_job_id) for item in case_view.pending_requirements
            if item.status.value == "OPEN" and item.kind.value == "ATTACHMENT"]
        return next((item for item in sources if self._is_generic_job(item)), None)

    def _dispatch_generic_restart(self, pending, request=None, *, route_rechecked=False):
        cid, run_id = pending["conversation_id"], pending["run_id"]
        kind = {"RestartGenericDiagnosis": RestartGenericDiagnosis,
                "MarkInitialLogArchiveExpected": MarkInitialLogArchiveExpected}[pending.get("operation", "RestartGenericDiagnosis")]
        command = kind.model_validate(pending["command"])
        if pending["status"] == "REJECTED":
            raise AgentStoreError("AGENT_RUN_CHANGED", "本次定位已结束或发生变化，请刷新会话。", 409)
        run = self.store.get_run(cid, run_id, deleted=True)
        if run.get("stop_requested") or run.get("_deleted") or run["status"] in _CLOSED:
            if self.dispatcher is not None:
                # A stopped replacement still needs its predecessor killed if
                # the first cancellation signal failed. Never redispatch it.
                self.dispatcher.cancel(command.source_job_id)
            self.store.finish_generic_restart(command.idempotency_key, rejected=True)
            raise AgentStoreError("AGENT_RUN_CHANGED", "本次定位已结束。", 409)
        try:
            with self.store.run_scope(cid, run_id):
                response = self.application.execute(command)
        except ApplicationPortError as error:
            if error.error.code.value in {"REVISION_CONFLICT", "INVALID_CASE_STATE", "CASE_NOT_FOUND", "JOB_NOT_FOUND"}:
                if isinstance(command, MarkInitialLogArchiveExpected):
                    snapshot = self.store.repository.read_snapshot(command.case_id)
                    aggregate = snapshot.cases.get(command.case_id)
                    job = None if aggregate is None else aggregate.jobs.get(aggregate.case.active_job_id)
                    if self._is_generic_job(job) and len(pending["message"]["attachment_ids"]) == 1:
                        replacement = RestartGenericDiagnosis(idempotency_key=command.idempotency_key + "-generic",
                            case_id=command.case_id, expected_case_revision=aggregate.case.case_revision,
                            source_job_id=job.job_id, supplement_text=pending["message"]["text"])
                        retargeted = self.store.retarget_routed_log_request(command.idempotency_key, replacement)
                        return self._dispatch_generic_restart(retargeted, request)
                    if (aggregate is not None and aggregate.case.selected_skill_ref is not None
                            and aggregate.case.status.value not in _CASE_DONE):
                        message = pending["message"]
                        try:
                            return self.store.submit_message(cid, message["request_id"], message["text"],
                                message["attachment_ids"], routed_request_key=command.idempotency_key)
                        except AgentStoreError as changed:
                            if changed.code != "AGENT_ROUTE_CHANGED":
                                raise
                            if route_rechecked:
                                raise AgentStoreError("AGENT_RESTART_PENDING", "定位策略正在更新，请稍后重试同一请求。", 503, retryable=True) from changed
                            return self._dispatch_generic_restart(pending, request, route_rechecked=True)
                    if job is not None and job.job_type.value == "ROUTE":
                        raise AgentStoreError("AGENT_RESTART_PENDING", "定位策略正在更新，请稍后重试同一请求。", 503, retryable=True)
                self.store.finish_generic_restart(command.idempotency_key, rejected=True)
                raise AgentStoreError("AGENT_RUN_CHANGED", "本次定位已结束或发生变化，请刷新会话。", 409) from error
            raise
        if not response.dispatch_pending:
            self.store.finish_generic_restart(command.idempotency_key)
        self._wake.set()
        self._control_wake.set()
        if request is None:
            request = SendMessageRequest(request_id=pending["message"]["request_id"], text=pending["message"]["text"],
                attachment_ids=pending["message"]["attachment_ids"])
        receipt, _ = self.store.message_request(cid, request)
        if receipt is None:
            raise AgentStoreError("AGENT_RESTART_PENDING", "日志接入尚未完成，请稍后重试同一请求。", 503, retryable=True)
        return receipt

    def get_feedback(self, conversation_id, run_id, *, owner_key=None):
        if self.feedback is not None:
            return self.feedback.get_feedback(conversation_id, run_id, owner_key=owner_key)
        from problem_locator.memory.models import FeedbackView
        if owner_key is None:
            raise AgentStoreError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在。", 404)
        with self.operation_lease(conversation_id, owner_key=owner_key):
            self.store.get_run(conversation_id, run_id)
            return FeedbackView(conversation_id=conversation_id, run_id=run_id, can_rate=False)

    def put_feedback(self, conversation_id, run_id, request_id, rating, *, owner_key=None):
        if self.feedback is None:
            self.get_feedback(conversation_id, run_id, owner_key=owner_key)
            raise AgentStoreError("AGENT_FEEDBACK_UNSUPPORTED", "这份报告暂不支持反馈。", 409)
        return self.feedback.put_feedback(conversation_id, run_id, request_id, rating, owner_key=owner_key)

    def list_conversations(self, owner_key, *, cursor=None, limit=20):
        return self.store.list_conversations(owner_key, cursor=cursor, limit=limit)

    def authorize_case(self, case_id, owner_key=None):
        conversation_id = self.store.conversation_for_case(case_id)
        if conversation_id is not None:
            if owner_key is None:
                raise AgentStoreError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在或已删除。", 404)
            self.store.require_owner(conversation_id, owner_key)
        return conversation_id

    def authorize_attachment(self, attachment_id, owner_key=None):
        snapshot = self.store.repository.read_snapshot(attachment_id=attachment_id)
        for case_id in snapshot.cases:
            return self.authorize_case(case_id, owner_key)
        return None

    def rename_conversation(self, conversation_id, title, *, owner_key=None):
        with self.operation_lease(conversation_id, owner_key=owner_key):
            return self.store.rename_conversation(conversation_id, title)

    def stop_conversation(self, conversation_id, request_id, run_id, *, owner_key=None):
        # Do not take _processing or require admission: this must stay usable
        # while INTAKE is blocked or dispatch has paused new work.
        with self.operation_lease(conversation_id, owner_key=owner_key):
            receipt = self.store.request_stop(conversation_id, request_id, run_id)
            self._signal_run(conversation_id, run_id)
        self._control_wake.set()
        return receipt

    def delete_conversation(self, conversation_id, *, owner_key=None):
        # Store performs authorization even for the minimal deletion receipt.
        receipt = self.store.request_delete(conversation_id, owner_key=owner_key)
        with self._run_lock:
            for (cid, _run_id), signal in self._active_runs.items():
                if cid == conversation_id:
                    signal.cancel(CancellationReason.USER_CANCEL)
        self._control_wake.set()
        return receipt

    def get_conversation(self, conversation_id, include=("history", "report", "artifacts"), *,
                         run_id=None, history_before=None, history_limit=50, owner_key=None) -> ConversationDetail:
        with self.operation_lease(conversation_id, owner_key=owner_key):
            result = self._conversation_detail(conversation_id, include, run_id=run_id,
                history_before=history_before, history_limit=history_limit)
            self.store.require_owner(conversation_id, owner_key)
            return result

    def _conversation_detail(self, conversation_id, include, *, run_id, history_before, history_limit):
        choices = ("history", "report", "artifacts")
        if (not isinstance(include, (tuple, list)) or any(item not in choices for item in include)
                or len(include) != len(set(include))):
            raise AgentStoreError("VALIDATION_ERROR", "会话内容选项无效或重复。", 400)
        included = [item for item in choices if item in include]
        needs_case = "report" in included or "artifacts" in included
        try:
            captured = self.store.read_conversation(conversation_id,
                history="history" in included, case_snapshot=needs_case, run_id=run_id,
                history_before=history_before, history_limit=history_limit)
        except ApplicationPortError:
            # Preserve the delivery-uncertainty classification when even the
            # authoritative snapshot cannot be read. No model work is retried.
            with self.store.run_scope(conversation_id, run_id) if run_id is not None else nullcontext():
                view = self.store.get_status(conversation_id)
            operational = getattr(self.application, "operational_state", None)
            error = None if operational is None or view.case_id is None else operational.error_for_case(
                view.case_id, None, include_archive_faults=False)
            if error is not None:
                raise ApplicationPortError(error) from None
            raise
        view = captured.view
        case = published = artifacts = None
        if needs_case and view.case_id is not None:
            try:
                case, published, artifacts = self.application.read_conversation_delivery(
                    view.case_id, captured.snapshot, report="report" in included, artifacts="artifacts" in included)
            except ApplicationPortError as error:
                if not (error.error.code.value == "CASE_NOT_FOUND" and view.status in _CLOSED
                        and view.report_state == "UNAVAILABLE"):
                    raise
        view = self._read_failure(view, case=case)
        values = view.model_dump(mode="python")
        values.update(schema_version=3, included=included, progress=captured.progress,
            history=captured.history, history_next_cursor=captured.history_next_cursor,
            attachments=captured.attachments, title=captured.title,
            current_run=captured.current_run, capabilities=captured.capabilities,
            selected_run_id=captured.selected_run_id)
        if case is not None:
            source = result_source_job_id(case)
            values.update(case_revision=case.case_revision, case_status=case.status.value,
                archive_status=case.archive_status, source_job_id=source,
                report_state="READY" if source is not None else "UNAVAILABLE" if (
                    view.status in _CLOSED or case.status.value in _CASE_DONE) else "PENDING")
        if "report" in included:
            result = self._report_view(view, published)
            values.update(result=result, case_revision=result.case_revision, case_status=result.case_status,
                archive_status=result.archive_status, source_job_id=result.source_job_id,
                report_state=result.report_state, failure=result.failure)
        if "artifacts" in included:
            values["artifacts"] = [ConversationArtifact.model_validate(item.model_dump(mode="json"))
                for item in artifacts or []]
        return ConversationDetail(**values)

    def get_status(self, conversation_id):
        return self._read_failure(self.store.get_status(conversation_id))

    def _read_failure(self, view, *, case=None):
        operational = getattr(self.application, "operational_state", None)
        if (operational is not None and view.case_id is None and view.status not in _CLOSED
                and not operational.accepting and operational.latest_error is not None
                and self.store.has_pending_messages(view.conversation_id, run_id=view.run_id)):
            # This accepted message has no Case whose delivery can be queried.
            # Reveal the pause, never identifiers from the task that stopped us.
            raise AgentStoreError("DISPATCH_REJECTED", "服务异常，已接收的任务暂时无法继续。", 503,
                details=[{"field": "phase", "actual": "DISPATCH_PAUSED"},
                    {"field": "persistence", "actual": "UNKNOWN"}], retryable=True)
        if operational is not None and view.case_id is not None:
            error = operational.error_for_case(view.case_id, None, view.archive_status)
            if error is None:
                return view
            try:
                if case is None:
                    case = self.application.get_case(view.case_id).case_view
            except ApplicationPortError as query_error:
                if query_error.error.code.value == "CASE_NOT_FOUND" and (view.status in _CLOSED or
                    operational.error_for_case(view.case_id, None, view.archive_status) is None):
                    return view
                raise
            error = operational.error_for_case(view.case_id, case.status, case.archive_status)
            if error is not None:
                if any(item.field == "phase" and item.actual == "ARCHIVE_STATUS_COMMIT" for item in error.details):
                    # The report is authoritative; only this process's archive
                    # delivery is unknown. Do not persist or emit a terminal event.
                    return view.model_copy(update={"failure": AgentPublicFailure(code=error.code.value,
                        message="报告已生成，但归档状态暂时无法确认。",
                        details=[item.model_dump(mode="json") for item in error.details], retryable=False)})
                raise ApplicationPortError(error)
        return view

    @staticmethod
    def _report_view(view, result):
        values = dict(conversation_id=view.conversation_id, case_id=view.case_id,
            report_state=view.report_state, case_status=view.case_status,
            archive_status=view.archive_status, failure=view.failure)
        if result is not None:
            case = result.case
            ready = result.format is not None
            values.update(case_revision=case.case_revision, case_status=case.status.value,
                archive_status=case.archive_status,
                report_state="READY" if ready else "UNAVAILABLE" if (
                    view.status in _CLOSED or case.status.value in _CASE_DONE) else "PENDING",
                format=result.format, report=result.report, markdown=result.markdown,
                source_job_id=result.source_job_id,
                artifact=None if result.artifact is None else PublicArtifactData.model_validate(
                    result.artifact.model_dump(mode="json")))
            if not ready and values["report_state"] == "UNAVAILABLE" and values["failure"] is None:
                if case.failure is not None:
                    details = []
                    if case.failure.diagnostic_id is not None:
                        details.append({"field": "diagnostic_id", "actual": case.failure.diagnostic_id})
                    if case.failure.reason_code is not None:
                        details.append({"field": "reason_code", "actual": case.failure.reason_code.value})
                    values["failure"] = AgentPublicFailure(code=case.failure.code.value,
                        message="本次定位未能完成，请重新发起任务。", details=details)
                elif case.status.value == "INTERRUPTED":
                    values["failure"] = AgentPublicFailure(code="AGENT_INTERRUPTED",
                        message="本次任务已结束，未生成诊断报告。")
        return ConversationReportView(**values)

    def list_events(self, conversation_id, after_sequence=0, limit=100, *, owner_key=None):
        with self.operation_lease(conversation_id, owner_key=owner_key):
            events = self.store.list_events(conversation_id, after=after_sequence, limit=limit)
            view = self.get_status(conversation_id)
            delivered = events[-1].sequence if events else after_sequence
            return {"events": events, "stream_closed": view.status in _CLOSED and delivered >= view.last_event_id}

    def prepare_attachment(self, conversation_id, request_id, name, content_type, declared_size, declared_sha256, *, owner_key=None):
        self._available()
        with self.operation_lease(conversation_id, owner_key=owner_key):
            return self.uploads.prepare(conversation_id, request_id, name, content_type, declared_size, declared_sha256)

    def upload_attachment(self, attachment_id, request_id, content_type, content_length, content_sha256, content, *, owner_key=None):
        self._available()
        record = self.store.get_attachment(attachment_id)
        with self.operation_lease(record.conversation_id, owner_key=owner_key):
            return self.uploads.upload(attachment_id, request_id, content_type, content_length, content_sha256, content)

    def _signal_run(self, conversation_id, run_id):
        with self._run_lock:
            signal = self._active_runs.get((conversation_id, run_id))
            if signal is not None:
                signal.cancel(CancellationReason.USER_CANCEL)

    def _check_run(self):
        current = getattr(self._local, "run", None)
        if self._stop.is_set():
            raise _RunStopped()
        if current is not None and self.store.stop_requested(*current):
            raise _RunStopped()

    def _run_control(self):
        while not self._stop.is_set():
            self._control_wake.clear()
            try:
                self.control_once()
            except Exception as error:
                # Intent remains durable; do not lose the management thread
                # after a transient database error or invent a completed stop.
                log_event("agent.management.pending", error_type=type(error).__name__)
            self._control_wake.wait(0.5)

    def control_once(self):
        """Management retries durable intents, never model execution."""
        if self._stop.is_set() or not self._control_processing.acquire(blocking=False):
            return False
        progressed = False
        try:
            for pending in self.store.pending_generic_restarts():
                try:
                    with self.usage_guard.acquire(pending["conversation_id"]):
                        self._dispatch_generic_restart(pending)
                    progressed = True
                except Exception as error:
                    log_event("agent.generic_restart.pending", conversation_id=pending["conversation_id"],
                        run_id=pending["run_id"], error_type=type(error).__name__)
            for pending in self.store.pending_stops():
                cid, run_id = pending["conversation_id"], pending["run_id"]
                self._signal_run(cid, run_id)
                try:
                    progressed = self._settle_stop(cid, run_id) or progressed
                except Exception as error:
                    # Keep the durable intent in CANCELLING. A failed commit
                    # must not be projected as a completed user cancellation.
                    log_event("agent.stop.pending", conversation_id=cid, run_id=run_id,
                        error_type=type(error).__name__)
            if self.cleanup is not None:
                try:
                    progressed = bool(self.cleanup.run_once()) or progressed
                except Exception as error:
                    log_event("agent.cleanup.pending", error_type=type(error).__name__)
            return progressed
        finally:
            self._control_processing.release()

    def _settle_stop(self, conversation_id, run_id):
        body = self.store.get_run(conversation_id, run_id, deleted=True)
        case_id = body.get("case_id")
        if case_id is not None:
            # Internal snapshot remains readable after the public tombstone.
            snapshot = self.store.repository.read_snapshot(case_id)
            aggregate = snapshot.cases.get(case_id)
            if aggregate is not None and aggregate.case.status.value not in _CASE_DONE:
                self.application.execute(CancelCase(idempotency_key="agent-stop-" + run_id,
                    case_id=case_id, expected_case_revision=aggregate.case.case_revision))
            if self.dispatcher is not None and not self.dispatcher.cases_idle({case_id}):
                return False
        with self._run_lock:
            if (conversation_id, run_id) in self._active_runs:
                return False
        # A CreateCase call remains registered as active until its response or
        # exception has returned. Re-read so a just-committed Case cannot orphan.
        latest = self.store.get_run(conversation_id, run_id, deleted=True)
        if latest.get("case_id") != case_id:
            return False
        self.store.finish_stop(conversation_id, run_id)
        return True

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                progressed = self.run_once()
            except Exception as error:
                # A persistent-store error must not turn into an unbounded retry loop.
                self._failure = type(error).__name__
                return
            if not progressed:
                self._wake.wait(0.5)

    def run_once(self, conversation_id=None):
        operational = getattr(self.application, "operational_state", None)
        if self._stop.is_set() or (operational is not None and not operational.accepting) or not self._processing.acquire(blocking=False):
            return False
        progressed = False
        try:
            targets = [conversation_id] if conversation_id else self.store.pending_conversations()
            for selected in targets:
                if self._stop.is_set():
                    break
                try:
                    self._phase = "AGENT"
                    progressed = self._advance_guarded(selected) or progressed
                except Exception as error:
                    if operational is not None and not operational.accepting:
                        # The operational latch reports uncertain delivery over HTTP.
                        # Do not invent a durable failure or replay a queued command.
                        break
                    # Scoped execution records its failure before leaving the
                    # frozen run. Errors here are storage/control failures.
                    raise error
            return progressed
        finally:
            self._processing.release()

    def _advance_guarded(self, conversation_id):
        try:
            with self.usage_guard.acquire(conversation_id):
                body = self.store.get_run(conversation_id)
                run_id = body["run_id"]
                signal = CancellationController()
                with self._run_lock:
                    self._active_runs[(conversation_id, run_id)] = signal
                self._local.run = (conversation_id, run_id)
                self._local.signal = signal
                try:
                    with self.store.run_scope(conversation_id, run_id):
                        try:
                            self._check_run()
                            return self._advance(conversation_id)
                        except _RunStopped:
                            return False
                        except Exception as error:
                            # A deleted/stopped run rejects late writes. These
                            # exceptions must never overwrite its cancellation.
                            if self._stop.is_set() or self.store.stop_requested(conversation_id, run_id):
                                return False
                            operational = getattr(self.application, "operational_state", None)
                            if operational is not None and not operational.accepting:
                                return False
                            self.store.fail_conversation(conversation_id, exception_code(error), phase=self._phase,
                                source_details=exception_details(error))
                            return True
                finally:
                    self._local.run = None
                    self._local.signal = None
                    with self._run_lock:
                        self._active_runs.pop((conversation_id, run_id), None)
                    self._control_wake.set()
        except AgentStoreError as error:
            if error.status_code == 404:
                return False
            raise

    def _execute_command(self, conversation_id, label, command, *, message_id=None, generic_message_ids=None):
        # A first message may already have been read while a second message
        # uploads logs. Serialize only the short Case creation commit against
        # message acceptance; never hold this lock during INTAKE or diagnosis.
        with self._message_lock(conversation_id) if isinstance(command, CreateCase) else nullcontext():
            if isinstance(command, CreateCase):
                latest = self.store.get_conversation(conversation_id)
                expects_logs = any(item.attachment_ids for item in latest.messages if item.status != "UNUSED")
                command = command.model_copy(update={"initial_log_archive_expected": expects_logs})
            return self._execute_frozen_command(conversation_id, label, command, message_id=message_id,
                generic_message_ids=generic_message_ids)

    def _execute_frozen_command(self, conversation_id, label, command, *, message_id=None, generic_message_ids=None):
        """Freeze the full command before dispatch, including its initial revision."""
        previous_phase = self._phase
        self._phase = {CreateCase: "CREATE_CASE", PrepareAttachment: "PREPARE_ATTACHMENT",
            SubmitSupplement: "SUBMIT_SUPPLEMENT"}[type(command)]
        self._check_run()
        run_id = self.store.get_run(conversation_id)["run_id"]
        dispatch_id = run_id + ":" + label
        existing = self.store.get_dispatch(conversation_id, dispatch_id)
        if existing is not None:
            if existing["epoch"] != self.store.runtime_epoch and existing["status"] != "COMPLETED":
                raise AgentStoreError("AGENT_DISPATCH_INTERRUPTED", "任务已经中断，请新建任务。", 409)
            if existing["status"] == "COMPLETED":
                self._phase = previous_phase
                return ApplicationResponse.model_validate(existing["result"])
            payload = existing["payload"]
            command = _COMMANDS[payload["operation"]].model_validate(payload["command"])
        else:
            payload = {"operation": type(command).__name__, "command": command.model_dump(mode="json")}
            if generic_message_ids is not None:
                payload["generic_message_ids"] = generic_message_ids
            self.store.record_dispatch(conversation_id, dispatch_id, payload)
        if message_id is not None:
            self.store.begin_adoption(conversation_id, message_id, dispatch_id)
        try:
            self._check_run()
            response = self.application.execute(command)
        except Exception:
            if message_id is not None:
                self.store.finish_adoption(conversation_id, message_id, accepted=False)
            raise
        if message_id is not None and not self.store.stop_requested(conversation_id, run_id):
            self.store.finish_adoption(conversation_id, message_id, accepted=True)
        if not response.dispatch_pending:
            self.store.complete_dispatch(dispatch_id, response.model_dump(mode="json"))
        self._phase = previous_phase
        return response

    def _retry_pending_commands(self, conversation_id):
        for pending in self.store.pending_dispatches(conversation_id):
            self._check_run()
            payload = pending["payload"]
            if payload["operation"] not in _COMMANDS:
                continue
            command = _COMMANDS[payload["operation"]].model_validate(payload["command"])
            self._phase = {CreateCase: "CREATE_CASE", PrepareAttachment: "PREPARE_ATTACHMENT",
                SubmitSupplement: "SUBMIT_SUPPLEMENT"}[type(command)]
            # Replaying a received command can only redispatch its existing Job.
            response = self.application.execute(command)
            if not response.dispatch_pending:
                self.store.complete_dispatch(pending["dispatch_id"], response.model_dump(mode="json"))

    def _advance(self, conversation_id):
        self._check_run()
        run_id = self.store.get_run(conversation_id)["run_id"]
        self._phase = "CASE_QUERY"
        intake_state = self.store.get_intake_state(conversation_id)
        if not intake_state["pending"] and not intake_state["pending_commands"]:
            return False
        if not intake_state["ready"] and not intake_state["pending_commands"]:
            return False
        if intake_state["pending_commands"]:
            self._retry_pending_commands(conversation_id)
        view = self.store.get_conversation(conversation_id)
        if view.status in _CLOSED or view.case_status in _CASE_DONE:
            return False
        case_view = None
        if view.case_id is not None:
            self._phase = "CASE_QUERY"
            try:
                case_view = self.application.get_case(view.case_id).case_view
            except ApplicationPortError as error:
                if error.error.code.value == "CASE_NOT_FOUND":
                    self.store.fail_conversation(conversation_id, "CASE_NOT_FOUND", interrupted=True, phase="CASE_QUERY")
                    return True
                raise
            if case_view.status.value not in {"WAITING_INPUT", "WAITING_ATTACHMENT"}:
                return False
            if self._generic_waiting_job(case_view) is not None:
                return self._advance_generic_supplement(conversation_id)
        pending = [item for item in view.messages if item.status in {"QUEUED", "PROCESSING"}]
        covered = set(intake_state["covered_message_ids"])
        if not pending:
            # APPLIED means the original message created the Case, not that
            # its Skill-specific facts were extracted. Read it once after ROUTE.
            pending = [item for item in view.messages
                if item.status == "APPLIED" and item.message_id not in covered]
            if not pending or case_view is None:
                self.store.finish_intake(conversation_id, [])
                return False
        message = pending[0]
        prefix = []
        selected_attachment_ids = []
        uncovered_text = False
        for item in view.messages:
            if item.status != "UNUSED":
                prefix.append(item)
                uncovered_text = uncovered_text or (item.message_id not in covered and bool(item.text.strip()))
                if item.attachment_ids:
                    # A later explicit selection replaces an unadopted choice;
                    # older uploads are retained but never silently combined.
                    selected_attachment_ids = item.attachment_ids
            if item.message_id == message.message_id:
                break
        if message.status != "APPLIED":
            self.store.set_message_status(conversation_id, message.message_id, "PROCESSING")
        draft = self.store.get_draft(conversation_id)
        if case_view is None:
            self._phase = "CREATE_CASE"
            if not message.text.strip():
                self.store.set_message_status(conversation_id, message.message_id, "APPLIED")
                self.store.finish_intake(conversation_id, [message.message_id], ["请描述需要定位的问题。"])
                return True
            # Creation is deterministic. Retain the full original message and
            # let the Case produce requirements before extracting any facts.
            spec = build_initial_problem_spec(message.text)
            self.store.update_intake(conversation_id, draft, [], "RUNNING")
            create_key = "agent-create-" + run_id
            self.store.expect_case(conversation_id, create_key)
            self._execute_command(conversation_id, "create", CreateCase(
                idempotency_key=create_key,
                raw_problem_text=message.text, problem_spec=spec,
                initial_log_archive_expected=bool(selected_attachment_ids),
                initial_user_facts=[], wait_seconds=0,
            ), message_id=message.message_id)
            return True
        self._phase = "INTAKE"
        attachments, attachment_ids, attachment_notice = self._attachment_selection(
            view, case_view, selected_attachment_ids)
        request = self._intake_input(view, prefix, draft, case_view, attachments)
        operation_id = run_id + ":intake:" + message.message_id
        prior = self.store.get_dispatch(conversation_id, operation_id)
        if prior is not None:
            if prior["status"] != "COMPLETED":
                raise AgentStoreError("AGENT_INTAKE_UNCERTAIN", "问题整理已中断，请新建任务。", 409)
            # A completed model call belongs to its frozen input, even if a
            # successfully committed supplement has since closed requirements.
            frozen_request = IntakeInput.model_validate(prior["payload"]["input"])
            decision = validate_intake_decision(IntakeDecision.model_validate(prior["result"]), frozen_request)
            if decision.action == "SUBMIT_SUPPLEMENT":
                # A formerly nonempty submission may now be fully adopted,
                # including attachment-only results with no user_facts. Derive
                # any remaining work from the current authoritative requirements.
                decision = decision.model_copy(update={"action": "NEED_CLARIFICATION"})
            decision = validate_intake_decision(decision, request)
        else:
            workspace_id = str(uuid.uuid5(uuid.UUID(run_id), "intake:" + message.message_id))
            self.store.record_dispatch(conversation_id, operation_id,
                {"operation": "INTAKE", "input": request.model_dump(mode="json"), "workspace_id": workspace_id})
            self.store.append_progress(conversation_id, "INTAKE", dedupe_key="intake:" + message.message_id)
            open_inputs = any(item.kind == "INPUT" for item in request.requirements)
            if (not message.text.strip() and not (uncovered_text and open_inputs)) or (
                    message.status == "APPLIED" and not open_inputs):
                # Attachment-only work needs no language model. Actual attachment
                # work may still cover an earlier, as-yet unextracted description.
                # New nonempty messages retain frozen-fact correction checks.
                decision = IntakeDecision(action="SUBMIT_SUPPLEMENT" if request.attachments else "NEED_CLARIFICATION",
                    message="已核对补充要求。",
                    problem_fields=[], user_facts=[])
            elif isinstance(self.intake_engine, ClaudeIntakeEngine):
                self._check_run()
                decision = self.intake_engine.intake(request, cancellation=self._local.signal, workspace_id=workspace_id)
            elif self._intake_accepts_cancellation:
                self._check_run()
                decision = self.intake_engine.intake(request, cancellation=self._local.signal)
            else:
                self._check_run()
                decision = self.intake_engine.intake(request)
            self._check_run()
            decision = validate_intake_decision(decision, request)
            self.store.complete_dispatch(operation_id, decision.model_dump(mode="json"))
            receipt = intake_processing_receipt(decision)
            if receipt is not None:
                log_event("agent.intake.inputs_processed", conversation_id=conversation_id,
                    case_id=view.case_id, operation_id=operation_id, **receipt)
        self._check_run()
        if decision.action == "NEW_CASE_REQUIRED":
            if message.status != "APPLIED":
                self.store.set_message_status(conversation_id, message.message_id, "UNUSED", decision.message)
            self.store.finish_intake(conversation_id, [item.message_id for item in prefix], [decision.message])
            return True
        # The validated decision already merges source-backed drafts with new
        # facts and removes identical facts adopted by the authoritative Case.
        draft = {"problem_fields": [item.model_dump(mode="json") for item in decision.draft],
                 "user_facts": [item.model_dump(mode="json") for item in decision.user_facts]}
        self.store.set_draft(conversation_id, draft)
        inputs = {item.name: item.value for item in decision.user_facts}
        if inputs or attachment_ids:
            self._supplement(conversation_id, case_view, inputs, attachment_ids, "message-" + message.message_id,
                message_id=message.message_id)
        elif decision.action == "SUBMIT_SUPPLEMENT" and attachment_notice is None:
            raise AgentStoreError("AGENT_NO_MATCHING_INPUT", "本次内容不符合当前补充要求。", 409)
        elif message.status != "APPLIED":
            self.store.set_message_status(conversation_id, message.message_id, "APPLIED")
        self.store.finish_intake(conversation_id, [item.message_id for item in prefix],
            attachment_notice=attachment_notice)
        return True

    @staticmethod
    def _generic_message_selection(view):
        messages = [item for item in view.messages if item.status != "UNUSED"]
        selected = next((item.attachment_ids for item in reversed(messages) if item.attachment_ids), [])
        return messages, selected

    def _advance_generic_supplement(self, conversation_id):
        """Import outside the input lock, then freeze the latest accepted batch."""
        run_id = self.store.get_run(conversation_id)["run_id"]
        while True:
            self._check_run()
            with self._message_lock(conversation_id):
                view = self.store.get_conversation(conversation_id)
                case_view = self.application.get_case(view.case_id).case_view
                if case_view.status.value not in {"WAITING_INPUT", "WAITING_ATTACHMENT"}:
                    return False
                messages, selected = self._generic_message_selection(view)
                _, attachment_ids, notice = self._attachment_selection(view, case_view, selected, generic=True)
                if not attachment_ids:
                    for item in messages:
                        if item.status in {"QUEUED", "PROCESSING"}:
                            self.store.set_message_status(conversation_id, item.message_id, "APPLIED")
                    self.store.finish_intake(conversation_id, [item.message_id for item in messages], attachment_notice=notice)
                    return True
            self._phase = "IMPORT_ATTACHMENT"
            targets = [self.uploads.import_into_case(conversation_id, case_view.case_id, item, self._execute_command,
                run_id=run_id, check_cancelled=self._check_run) for item in attachment_ids]
            with self._message_lock(conversation_id):
                self._check_run()
                latest = self.store.get_conversation(conversation_id)
                messages, current_selection = self._generic_message_selection(latest)
                if current_selection != selected:
                    # An imported but superseded archive remains audited; it
                    # must never start a model or replace the newer selection.
                    continue
                case_view = self.application.get_case(latest.case_id).case_view
                if case_view.status.value not in {"WAITING_INPUT", "WAITING_ATTACHMENT"}:
                    return False
                adopted = set(self.store.get_run(conversation_id).get("generic_adopted_message_ids", []))
                supplement_text = "\n\n".join(item.text for item in messages if item.text.strip()
                    and item.message_id not in adopted and item.text != case_view.raw_problem_text)
                for item in messages:
                    if item.status == "QUEUED":
                        self.store.set_message_status(conversation_id, item.message_id, "PROCESSING")
                message_ids = [item.message_id for item in messages]
                label = "message-" + message_ids[-1]
                self._execute_command(conversation_id, label, SubmitSupplement(
                    idempotency_key="agent-supplement-" + run_id + "-" + label,
                    case_id=case_view.case_id, expected_case_revision=case_view.case_revision,
                    inputs={}, attachment_ids=targets, wait_seconds=0,
                    generic_supplement_text=supplement_text), message_id=message_ids[-1],
                    generic_message_ids=message_ids)
                self.store.finish_intake(conversation_id, message_ids, attachment_notice=None)
                return True

    def _intake_input(self, view, messages, draft, case_view, attachments):
        sources = [IntakeMessage(message_id=item.message_id, role="USER", text=item.text) for item in messages]
        if view.current_questions:
            sources.insert(len(sources) - 1, IntakeMessage(message_id="question-" + messages[-1].message_id,
                role="ASSISTANT", text="\n".join(view.current_questions)))
        requirements, frozen_requirements, spec, frozen_facts = [], [], None, {}
        if case_view is not None:
            spec = ProblemSpecInput.model_validate(case_view.problem_spec.model_dump(exclude={"revision"}))
            frozen_facts = {item.provenance.input_name: item.statement for item in case_view.user_facts
                if item.provenance.input_name is not None and item.status.value == "ACTIVE"}
            requirements = [IntakeRequirement(requirement_id=item.requirement_id, name=item.name,
                description=item.prompt, kind=item.kind.value,
                constraints=item.constraints if item.kind.value == "INPUT" else None)
                for item in case_view.pending_requirements if item.status.value == "OPEN"
                and item.supplement_policy.value == "MISSING_ONLY"]
            frozen_requirements = [IntakeRequirement(requirement_id=item.requirement_id, name=item.name,
                description=item.prompt, kind="INPUT", constraints=item.constraints)
                for item in case_view.pending_requirements if item.status.value == "FULFILLED"
                and item.kind.value == "INPUT" and item.supplement_policy.value == "MISSING_ONLY"]
        return IntakeInput(conversation_id=view.conversation_id, messages=sources,
            draft=[IntakeValue.model_validate(item) for item in draft.get("problem_fields", [])],
            draft_user_facts=[IntakeValue.model_validate(item) for item in draft.get("user_facts", [])],
            requirements=requirements, attachments=attachments,
            frozen_problem_spec=spec, frozen_user_facts=frozen_facts,
            frozen_input_requirements=frozen_requirements)

    def _attachment_selection(self, view, case_view, selected_ids, *, generic=False):
        requirements = [item for item in case_view.pending_requirements
            if item.status.value == "OPEN" and item.kind.value == "ATTACHMENT"
            and item.supplement_policy.value == "MISSING_ONLY"]
        if len(requirements) != 1:
            return [], [], None
        requirement = requirements[0]
        constraints = requirements[0].constraints
        selected = set(selected_ids)
        used = set() if generic else {key for item in case_view.pending_requirements if item.kind.value == "ATTACHMENT"
            and item.status.value == "FULFILLED" for key in item.fulfilled_by_refs}
        records = [item for item in view.attachments if item.attachment_id in selected
            and item.status in {"READY", "IMPORTED"} and item.case_attachment_id not in used]
        attachments = [IntakeAttachment(attachment_id=item.attachment_id, file_name=item.name,
            media_type=item.content_type, size_bytes=item.size, sha256=item.sha256) for item in records]
        notice = None
        if len(records) > constraints.max_count:
            notice = ("当前仅支持一份日志归档，请合并后上传，或重新选择一个附件。"
                if constraints.max_count == 1 else "所选日志附件数量超出当前要求，请重新选择附件。")
        elif any(item.content_type not in constraints.allowed_content_types for item in records):
            notice = "所选附件格式不符合要求，请重新上传支持的日志归档。"
        elif records and len(records) < constraints.min_count:
            notice = "日志附件数量不足，请按要求补充后重新选择附件。"
        if notice is not None:
            return attachments, [], {"requirement_id": requirement.requirement_id, "message": notice}
        return attachments, [item.attachment_id for item in records], None

    def _supplement(self, conversation_id, case_view, inputs, attachment_ids, label, *, message_id=None,
                    generic_supplement_text=""):
        self._check_run()
        run_id = self.store.get_run(conversation_id)["run_id"]
        self._phase = "IMPORT_ATTACHMENT"
        targets = [self.uploads.import_into_case(conversation_id, case_view.case_id, item, self._execute_command,
            run_id=run_id, check_cancelled=self._check_run)
            for item in attachment_ids]
        if not inputs and not targets and not generic_supplement_text:
            self._phase = "SUBMIT_SUPPLEMENT"
            raise AgentStoreError("AGENT_NO_MATCHING_INPUT", "本次内容不符合当前补充要求。", 409)
        self._phase = "CASE_QUERY"
        latest = self.application.get_case(case_view.case_id).case_view
        self._execute_command(conversation_id, label, SubmitSupplement(
            idempotency_key="agent-supplement-" + run_id + "-" + label,
            case_id=latest.case_id, expected_case_revision=latest.case_revision,
            inputs=inputs, attachment_ids=targets, wait_seconds=0,
            generic_supplement_text=generic_supplement_text,
        ), message_id=message_id)
