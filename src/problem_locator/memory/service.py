"""Authorize feedback against the published V2 Markdown report snapshot."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from problem_locator.agent.models import AgentStoreError
from problem_locator.application.reports import result_source_job_id
from problem_locator.contracts import DiagnosisMode, JobStatus

from .models import FeedbackRequest, FeedbackSource


class FeedbackService:
    def __init__(self, agent_service, memory_store, *, memory_enabled=False):
        self.agent = agent_service
        self.store = memory_store
        self.memory_enabled = memory_enabled

    def _source(self, conversation_id, run_id):
        captured = self.agent.store.read_conversation(
            conversation_id, case_snapshot=True, run_id=run_id)
        if captured.snapshot is None or captured.view.case_id is None:
            return None
        aggregate = captured.snapshot.cases.get(captured.view.case_id)
        if aggregate is None:
            return None
        result = aggregate.case.generic_result_v2
        if result is None or result_source_job_id(aggregate.case) != result.source_job_id:
            return None
        job = aggregate.jobs.get(result.source_job_id)
        if job is None or job.status is not JobStatus.SUCCEEDED:
            return None
        # The captured StateFile already validates Job, Skill, Outcome, artifact
        # and report hash bindings. Feedback does not need another artifact read.
        return FeedbackSource(
            case_id=aggregate.case.case_id, source_job_id=job.job_id,
            skill_name=result.skill_name,
            problem_text=(job.generic_problem_text or "") if job.diagnosis_mode is DiagnosisMode.GENERIC else "",
            report_markdown=result.report_markdown, report_sha256=result.report_sha256)

    @staticmethod
    def _require_owner(owner_key):
        if owner_key is None:
            raise AgentStoreError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在。", 404)

    def get_feedback(self, conversation_id, run_id, *, owner_key):
        self._require_owner(owner_key)
        with self.agent.operation_lease(conversation_id, owner_key=owner_key):
            source = self._source(conversation_id, run_id)
            return self.store.get_feedback(conversation_id, run_id, owner_key=owner_key,
                                           can_rate=source is not None)

    def put_feedback(self, conversation_id, run_id, request_id, rating, *, owner_key):
        self._require_owner(owner_key)
        request = FeedbackRequest(request_id=request_id, rating=rating)
        with self.agent.operation_lease(conversation_id, owner_key=owner_key):
            source = self._source(conversation_id, run_id)
            return self.store.put_feedback(conversation_id, run_id, owner_key=owner_key,
                request_id=request.request_id, rating=request.rating, source=source,
                extract_memory=self.memory_enabled)

    def _followup_source(self, conversation_id, run_id, followup_id, owner_key):
        followups = getattr(self.agent, "followups", None)
        if followups is None:
            raise AgentStoreError("AGENT_FOLLOWUP_NOT_FOUND", "这条追问不存在。", 404)
        source, item, correction, history = followups.store.learning_source(
            conversation_id, run_id, followup_id, owner_key)
        if (source is None or item.status != "COMPLETED" or not item.answer_markdown
                or item.diagnosis_run_id is not None):
            return None
        if not source.skill_name and source.source_kind == "GENERIC":
            original = self._source(conversation_id, run_id)
            if original is not None and (original.case_id, original.source_job_id, original.report_sha256) == (
                    source.case_id, source.source_job_id, source.report_sha256):
                source = replace(source, skill_name=original.skill_name)
        answer = item.answer_markdown or ""
        context = {
            "original_problem": source.problem_text,
            "original_report": source.report_markdown,
            "prior_turns": [{"question": previous["text"], "answer": previous.get("answer_markdown"),
                "status": previous["status"]} for previous in history],
            "question": item.text, "answer": answer, "correction": correction,
        }
        is_correction = correction.get("intent") == "CORRECTION"
        return FeedbackSource(case_id=source.case_id, source_job_id=source.source_job_id,
            skill_name=source.skill_name,
            problem_text=source.problem_text if source.source_kind == "GENERIC" else "",
            report_markdown=answer, report_sha256=hashlib.sha256(answer.encode("utf-8")).hexdigest(),
            answer_key=followup_id, source_kind="CORRECTION" if is_correction else "FOLLOWUP",
            context_json=json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            replaces_answer_key=(correction.get("target_followup_id") or "") if is_correction else None)

    def get_followup_feedback(self, conversation_id, run_id, followup_id, *, owner_key):
        self._require_owner(owner_key)
        with self.agent.operation_lease(conversation_id, owner_key=owner_key):
            source = self._followup_source(conversation_id, run_id, followup_id, owner_key)
            return self.store.get_feedback(conversation_id, run_id, owner_key=owner_key,
                can_rate=source is not None, answer_key=followup_id)

    def put_followup_feedback(self, conversation_id, run_id, followup_id, request_id, rating, *, owner_key):
        self._require_owner(owner_key)
        request = FeedbackRequest(request_id=request_id, rating=rating)
        with self.agent.operation_lease(conversation_id, owner_key=owner_key):
            source = self._followup_source(conversation_id, run_id, followup_id, owner_key)
            return self.store.put_feedback(conversation_id, run_id, owner_key=owner_key,
                request_id=request.request_id, rating=request.rating, source=source,
                extract_memory=self.memory_enabled, answer_key=followup_id)
    def post_feedback_comment(self, conversation_id, run_id, request_id, text=None,
                              analysis_text=None, analysis_reference=None, *, owner_key, followup_id=None):
        self._require_owner(owner_key)
        with self.agent.operation_lease(conversation_id, owner_key=owner_key):
            source = (self._source(conversation_id, run_id) if followup_id is None else
                self._followup_source(conversation_id, run_id, followup_id, owner_key))
            return self.store.post_feedback_comment(conversation_id, run_id, owner_key=owner_key,
                request_id=request_id, text=text, analysis_text=analysis_text,
                analysis_reference=analysis_reference, source=source, answer_key=followup_id or "",
                extract_memory=self.memory_enabled)
