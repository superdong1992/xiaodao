"""Asynchronous website intake; authoritative diagnosis stays in Case commands."""
from __future__ import annotations

import threading
from typing import Any

from problem_locator.contracts import (
    ApplicationPortError, ApplicationResponse, CancellationReason, CreateCase,
    PrepareAttachment, SubmitSupplement,
)
from problem_locator.contracts.models import ProblemSpecInput
from problem_locator.diagnostics import log_event

from .intake import (
    ClaudeIntakeEngine, IntakeAttachment, IntakeDecision, IntakeInput, IntakeMessage,
    IntakeRequirement, IntakeValue, build_initial_problem_spec, validate_intake_decision,
    intake_processing_receipt,
)
from .models import AgentPublicFailure, AgentStoreError
from .failures import exception_code, exception_details
from .uploads import ConversationUploads

_CLOSED = {"COMPLETED", "FAILED", "INTERRUPTED"}
_CASE_DONE = {"RESOLVED", "PARTIALLY_RESOLVED", "UNRESOLVED", "FAILED", "CANCELLED", "INTERRUPTED"}
_COMMANDS = {item.__name__: item for item in (CreateCase, PrepareAttachment, SubmitSupplement)}


class _IntakeShutdown:
    def __init__(self, event):
        self.event = event

    @property
    def reason(self):
        return CancellationReason.SERVICE_SHUTDOWN if self.event.is_set() else None

    def is_cancelled(self):
        return self.event.is_set()

    def wait(self, timeout_seconds):
        return self.event.wait(timeout_seconds)


class AgentConversationService:
    """One bounded intake worker; subscribing never starts or repeats model work."""

    def __init__(self, store, application, intake_engine, layout):
        self.store, self.application, self.intake_engine = store, application, intake_engine
        self.uploads = ConversationUploads(store, application, layout)
        self._stop, self._wake = threading.Event(), threading.Event()
        self._processing, self._lifecycle = threading.Lock(), threading.Lock()
        self._thread = None
        self._failure = None
        self._phase = "AGENT"
        store.on_change = lambda _conversation_id: self._wake.set()

    def start(self, runtime_epoch=None):
        with self._lifecycle:
            if self._thread is not None:
                return
            if runtime_epoch is not None:
                self.store.runtime_epoch = runtime_epoch
            self.store.recover()
            self._thread = threading.Thread(target=self._run, name="agent-intake", daemon=True)
            self._thread.start()

    def shutdown(self, timeout_seconds=30):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(max(0.0, timeout_seconds))
            return not self._thread.is_alive()
        return True

    def _available(self):
        if self._stop.is_set() or self._failure is not None:
            raise AgentStoreError("AGENT_UNAVAILABLE", "会话服务暂时不可用，请稍后重试。", 503)
        operational = getattr(self.application, "operational_state", None)
        if operational is not None:
            operational.require_accepting()

    def create_conversation(self, request_id):
        self._available()
        return self.store.create_conversation(request_id)

    def send_message(self, conversation_id, request_id, text="", attachment_ids=None):
        self._available()
        return self.store.submit_message(conversation_id, request_id, text or "", attachment_ids or [])

    def get_conversation(self, conversation_id):
        view = self.store.get_conversation(conversation_id)
        operational = getattr(self.application, "operational_state", None)
        if operational is not None and view.case_id is None and view.status not in _CLOSED and any(
            message.status in {"QUEUED", "PROCESSING"} for message in view.messages
        ) and not operational.accepting and operational.latest_error is not None:
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

    def list_events(self, conversation_id, after_sequence=0, limit=100):
        events = self.store.list_events(conversation_id, after=after_sequence, limit=limit)
        view = self.get_conversation(conversation_id)
        delivered = events[-1].sequence if events else after_sequence
        return {"events": events, "stream_closed": view.status in _CLOSED and delivered >= view.last_event_id}

    def prepare_attachment(self, conversation_id, request_id, name, content_type, declared_size, declared_sha256):
        self._available()
        return self.uploads.prepare(conversation_id, request_id, name, content_type, declared_size, declared_sha256)

    def upload_attachment(self, attachment_id, request_id, content_type, content_length, content_sha256, content):
        self._available()
        return self.uploads.upload(attachment_id, request_id, content_type, content_length, content_sha256, content)

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
                    progressed = self._advance(selected) or progressed
                except Exception as error:
                    if operational is not None and not operational.accepting:
                        # The operational latch reports uncertain delivery over HTTP.
                        # Do not invent a durable failure or replay a queued command.
                        break
                    if not self._stop.is_set():
                        # Public failures never contain raw model, filesystem or tool output.
                        self.store.fail_conversation(selected, exception_code(error), phase=self._phase,
                            source_details=exception_details(error))
                    progressed = True
            return progressed
        finally:
            self._processing.release()

    def _execute_command(self, conversation_id, label, command, *, message_id=None):
        """Freeze the full command before dispatch, including its initial revision."""
        previous_phase = self._phase
        self._phase = {CreateCase: "CREATE_CASE", PrepareAttachment: "PREPARE_ATTACHMENT",
            SubmitSupplement: "SUBMIT_SUPPLEMENT"}[type(command)]
        dispatch_id = conversation_id + ":" + label
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
            self.store.record_dispatch(conversation_id, dispatch_id,
                {"operation": type(command).__name__, "command": command.model_dump(mode="json")})
        if message_id is not None:
            self.store.begin_adoption(conversation_id, message_id, dispatch_id)
        try:
            response = self.application.execute(command)
        except Exception:
            if message_id is not None:
                self.store.finish_adoption(conversation_id, message_id, accepted=False)
            raise
        if message_id is not None:
            self.store.finish_adoption(conversation_id, message_id, accepted=True)
        if not response.dispatch_pending:
            self.store.complete_dispatch(dispatch_id, response.model_dump(mode="json"))
        self._phase = previous_phase
        return response

    def _retry_pending_commands(self, conversation_id):
        for pending in self.store.pending_dispatches(conversation_id):
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
            self.store.expect_case(conversation_id, "agent-create-" + conversation_id)
            self._execute_command(conversation_id, "create", CreateCase(
                idempotency_key="agent-create-" + conversation_id,
                raw_problem_text=message.text, problem_spec=spec,
                initial_user_facts=[], wait_seconds=0,
            ), message_id=message.message_id)
            return True
        self._phase = "INTAKE"
        attachments, attachment_ids, attachment_notice = self._attachment_selection(
            view, case_view, selected_attachment_ids)
        request = self._intake_input(view, prefix, draft, case_view, attachments)
        operation_id = conversation_id + ":intake:" + message.message_id
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
            self.store.record_dispatch(conversation_id, operation_id,
                {"operation": "INTAKE", "input": request.model_dump(mode="json")})
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
                decision = self.intake_engine.intake(request, cancellation=_IntakeShutdown(self._stop))
            else:
                decision = self.intake_engine.intake(request)
            decision = validate_intake_decision(decision, request)
            self.store.complete_dispatch(operation_id, decision.model_dump(mode="json"))
            receipt = intake_processing_receipt(decision)
            if receipt is not None:
                log_event("agent.intake.inputs_processed", conversation_id=conversation_id,
                    case_id=view.case_id, operation_id=operation_id, **receipt)
        if self._stop.is_set():
            return False
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

    def _attachment_selection(self, view, case_view, selected_ids):
        requirements = [item for item in case_view.pending_requirements
            if item.status.value == "OPEN" and item.kind.value == "ATTACHMENT"
            and item.supplement_policy.value == "MISSING_ONLY"]
        if len(requirements) != 1:
            return [], [], None
        requirement = requirements[0]
        constraints = requirements[0].constraints
        selected = set(selected_ids)
        used = {key for item in case_view.pending_requirements if item.kind.value == "ATTACHMENT"
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

    def _supplement(self, conversation_id, case_view, inputs, attachment_ids, label, *, message_id=None):
        self._phase = "IMPORT_ATTACHMENT"
        targets = [self.uploads.import_into_case(conversation_id, case_view.case_id, item, self._execute_command)
            for item in attachment_ids]
        if not inputs and not targets:
            self._phase = "SUBMIT_SUPPLEMENT"
            raise AgentStoreError("AGENT_NO_MATCHING_INPUT", "本次内容不符合当前补充要求。", 409)
        self._phase = "CASE_QUERY"
        latest = self.application.get_case(case_view.case_id).case_view
        self._execute_command(conversation_id, label, SubmitSupplement(
            idempotency_key="agent-supplement-" + conversation_id + "-" + label,
            case_id=latest.case_id, expected_case_revision=latest.case_revision,
            inputs=inputs, attachment_ids=targets, wait_seconds=0,
        ), message_id=message_id)
