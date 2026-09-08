"""Asynchronous website intake; authoritative diagnosis stays in Case commands."""
from __future__ import annotations

import threading
from typing import Any

from problem_locator.contracts import (
    ApplicationPortError, ApplicationResponse, CancellationReason, CreateCase,
    PrepareAttachment, SubmitSupplement,
)
from problem_locator.contracts.models import ProblemSpecInput

from .intake import (
    ClaudeIntakeEngine, IntakeAttachment, IntakeDecision, IntakeInput, IntakeMessage,
    IntakeRequirement, IntakeValue, build_initial_problem_spec, validate_intake_decision,
)
from .models import AgentStoreError
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

    def create_conversation(self, request_id):
        self._available()
        return self.store.create_conversation(request_id)

    def send_message(self, conversation_id, request_id, text="", attachment_ids=None):
        self._available()
        return self.store.submit_message(conversation_id, request_id, text or "", attachment_ids or [])

    def get_conversation(self, conversation_id):
        return self.store.get_conversation(conversation_id)

    def list_events(self, conversation_id, after_sequence=0, limit=100):
        events = self.store.list_events(conversation_id, after=after_sequence, limit=limit)
        view = self.store.get_conversation(conversation_id)
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
        if self._stop.is_set() or not self._processing.acquire(blocking=False):
            return False
        progressed = False
        try:
            targets = [conversation_id] if conversation_id else self.store.pending_conversations()
            for selected in targets:
                if self._stop.is_set():
                    break
                try:
                    progressed = self._advance(selected) or progressed
                except Exception as error:
                    if not self._stop.is_set():
                        # Public failures never contain raw model, filesystem or tool output.
                        self.store.fail_conversation(selected, getattr(error, "code", "AGENT_EXECUTION_FAILED"))
                    progressed = True
            return progressed
        finally:
            self._processing.release()

    def _execute_command(self, conversation_id, label, command, *, message_id=None):
        """Freeze the full command before dispatch, including its initial revision."""
        dispatch_id = conversation_id + ":" + label
        existing = self.store.get_dispatch(conversation_id, dispatch_id)
        if existing is not None:
            if existing["epoch"] != self.store.runtime_epoch and existing["status"] != "COMPLETED":
                raise AgentStoreError("AGENT_DISPATCH_INTERRUPTED", "任务已经中断，请新建任务。", 409)
            if existing["status"] == "COMPLETED":
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
        return response

    def _retry_pending_commands(self, conversation_id):
        for pending in self.store.pending_dispatches(conversation_id):
            payload = pending["payload"]
            if payload["operation"] not in _COMMANDS:
                continue
            command = _COMMANDS[payload["operation"]].model_validate(payload["command"])
            # Replaying a received command can only redispatch its existing Job.
            response = self.application.execute(command)
            if not response.dispatch_pending:
                self.store.complete_dispatch(pending["dispatch_id"], response.model_dump(mode="json"))

    def _advance(self, conversation_id):
        view = self.store.get_conversation(conversation_id)
        if view.status in _CLOSED or view.case_status in _CASE_DONE:
            return False
        self._retry_pending_commands(conversation_id)
        view = self.store.get_conversation(conversation_id)
        case_view = None
        if view.case_id is not None:
            try:
                case_view = self.application.get_case(view.case_id).case_view
            except ApplicationPortError as error:
                if error.error.code.value == "CASE_NOT_FOUND":
                    self.store.fail_conversation(conversation_id, interrupted=True)
                    return True
                raise
            if case_view.status.value not in {"WAITING_INPUT", "WAITING_ATTACHMENT"}:
                return False
        pending = self.store.pending_messages(conversation_id)
        if not pending:
            if case_view is not None:
                return self._submit_attachments(conversation_id, view, case_view)
            return False
        message = pending[0]
        prefix = []
        for item in view.messages:
            if item.status != "UNUSED":
                prefix.append(item)
            if item.message_id == message.message_id:
                break
        self.store.set_message_status(conversation_id, message.message_id, "PROCESSING")
        draft = self.store.get_draft(conversation_id)
        if case_view is None:
            if not message.text.strip():
                self.store.set_message_status(conversation_id, message.message_id, "APPLIED")
                self.store.update_intake(conversation_id, draft, ["请描述需要定位的问题。"])
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
        request = self._intake_input(view, prefix, draft, case_view)
        operation_id = conversation_id + ":intake:" + message.message_id
        prior = self.store.get_dispatch(conversation_id, operation_id)
        if prior is not None:
            if prior["status"] != "COMPLETED":
                raise AgentStoreError("AGENT_INTAKE_UNCERTAIN", "问题整理已中断，请新建任务。", 409)
            decision = validate_intake_decision(IntakeDecision.model_validate(prior["result"]), request)
        else:
            self.store.record_dispatch(conversation_id, operation_id,
                {"operation": "INTAKE", "input": request.model_dump(mode="json")})
            self.store.append_progress(conversation_id, "INTAKE", dedupe_key="intake:" + message.message_id)
            if not message.text.strip() and case_view is not None and request.attachments:
                decision = IntakeDecision(action="SUBMIT_SUPPLEMENT", message="已收到日志附件。",
                    problem_fields=[], user_facts=[])
            elif isinstance(self.intake_engine, ClaudeIntakeEngine):
                decision = self.intake_engine.intake(request, cancellation=_IntakeShutdown(self._stop))
            else:
                decision = self.intake_engine.intake(request)
            decision = validate_intake_decision(decision, request)
            self.store.complete_dispatch(operation_id, decision.model_dump(mode="json"))
        if self._stop.is_set():
            return False
        if decision.action == "NEW_CASE_REQUIRED":
            self.store.set_message_status(conversation_id, message.message_id, "UNUSED", decision.message)
            self.store.update_intake(conversation_id, draft, [decision.message])
            return True
        known = {item["name"]: item for item in draft.get("user_facts", [])}
        known.update({item.name: item.model_dump(mode="json") for item in decision.user_facts})
        draft = {"problem_fields": [item.model_dump(mode="json") for item in decision.draft],
                 "user_facts": list(known.values())}
        self.store.set_draft(conversation_id, draft)
        if decision.action == "NEED_CLARIFICATION":
            self.store.set_message_status(conversation_id, message.message_id, "APPLIED")
            questions = [item.prompt for item in case_view.pending_requirements if item.status.value == "OPEN"]
            self.store.update_intake(conversation_id, draft, questions)
            return True
        inputs = {item.name: item.value for item in decision.user_facts}
        attachment_ids = self._available_attachments(view, case_view, prefix)
        self._supplement(conversation_id, case_view, inputs, attachment_ids, "message-" + message.message_id,
            message_id=message.message_id)
        return True

    def _intake_input(self, view, messages, draft, case_view):
        sources = [IntakeMessage(message_id=item.message_id, role="USER", text=item.text) for item in messages]
        if view.current_questions:
            sources.append(IntakeMessage(message_id="question-" + messages[-1].message_id,
                role="ASSISTANT", text="\n".join(view.current_questions)))
        references = {key for item in messages for key in item.attachment_ids}
        attachments = [IntakeAttachment(attachment_id=item.attachment_id, file_name=item.name,
            media_type=item.content_type, size_bytes=item.size, sha256=item.sha256)
            for item in view.attachments if item.attachment_id in references and item.status in {"READY", "IMPORTED"}]
        requirements, spec, frozen_facts = [], None, {}
        if case_view is not None:
            spec = ProblemSpecInput.model_validate(case_view.problem_spec.model_dump(exclude={"revision"}))
            frozen_facts = {item.provenance.input_name: item.statement for item in case_view.user_facts
                if item.provenance.input_name is not None and item.status.value == "ACTIVE"}
            requirements = [IntakeRequirement(requirement_id=item.requirement_id, name=item.name,
                description=item.prompt, kind=item.kind.value,
                constraints=item.constraints if item.kind.value == "INPUT" else None)
                for item in case_view.pending_requirements if item.status.value == "OPEN"
                and item.supplement_policy.value == "MISSING_ONLY"]
        return IntakeInput(conversation_id=view.conversation_id, messages=sources,
            draft=[IntakeValue.model_validate(item) for item in draft.get("problem_fields", [])],
            requirements=requirements, attachments=attachments,
            frozen_problem_spec=spec, frozen_user_facts=frozen_facts)

    def _available_attachments(self, view, case_view, messages):
        requirements = [item for item in case_view.pending_requirements
            if item.status.value == "OPEN" and item.kind.value == "ATTACHMENT"
            and item.supplement_policy.value == "MISSING_ONLY"]
        if len(requirements) != 1:
            return []
        constraints = requirements[0].constraints
        selected = {key for item in messages if item.status != "UNUSED" for key in item.attachment_ids}
        used = {key for item in case_view.pending_requirements if item.kind.value == "ATTACHMENT"
            and item.status.value == "FULFILLED" for key in item.fulfilled_by_refs}
        attachments = [item.attachment_id for item in view.attachments if item.attachment_id in selected
            and item.status in {"READY", "IMPORTED"} and item.case_attachment_id not in used
            and item.content_type in constraints.allowed_content_types]
        if not constraints.min_count <= len(attachments) <= constraints.max_count:
            return []
        return attachments

    def _submit_attachments(self, conversation_id, view, case_view):
        applied = [item for item in view.messages if item.status == "APPLIED"]
        attachments = self._available_attachments(view, case_view, applied)
        if not attachments:
            return False
        requirement = next(item for item in case_view.pending_requirements
            if item.status.value == "OPEN" and item.kind.value == "ATTACHMENT")
        self._supplement(conversation_id, case_view, {}, attachments, "attachment-" + requirement.requirement_id)
        return True

    def _supplement(self, conversation_id, case_view, inputs, attachment_ids, label, *, message_id=None):
        targets = [self.uploads.import_into_case(conversation_id, case_view.case_id, item, self._execute_command)
            for item in attachment_ids]
        if not inputs and not targets:
            raise AgentStoreError("AGENT_NO_MATCHING_INPUT", "本次内容不符合当前补充要求。", 409)
        latest = self.application.get_case(case_view.case_id).case_view
        self._execute_command(conversation_id, label, SubmitSupplement(
            idempotency_key="agent-supplement-" + conversation_id + "-" + label,
            case_id=latest.case_id, expected_case_revision=latest.case_revision,
            inputs=inputs, attachment_ids=targets, wait_seconds=0,
        ), message_id=message_id)
