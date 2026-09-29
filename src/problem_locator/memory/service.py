"""Authorize feedback against the published V2 Markdown report snapshot."""
from __future__ import annotations

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
