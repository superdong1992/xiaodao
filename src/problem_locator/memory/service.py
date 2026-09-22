"""Authorize feedback against the exact published Generic V2 source Job."""
from __future__ import annotations

from problem_locator.agent.models import AgentStoreError
from problem_locator.contracts import DiagnosisMode, JobStatus

from .models import FeedbackRequest, FeedbackSource


class FeedbackService:
    def __init__(self, agent_service, memory_store, *, enabled=False):
        self.agent = agent_service
        self.store = memory_store
        self.enabled = enabled

    def _source(self, conversation_id, run_id):
        captured = self.agent.store.read_conversation(
            conversation_id, case_snapshot=True, run_id=run_id)
        if captured.snapshot is None or captured.view.case_id is None:
            return None
        aggregate = captured.snapshot.cases.get(captured.view.case_id)
        if aggregate is None:
            return None
        result = aggregate.case.generic_result_v2
        if result is None:
            return None
        job = aggregate.jobs.get(result.source_job_id)
        if (job is None or job.diagnosis_mode is not DiagnosisMode.GENERIC
                or job.status is not JobStatus.SUCCEEDED
                or job.generic_skill_name != result.skill_name
                or not job.generic_problem_text or not job.generic_problem_text.strip()
                or len(job.generic_problem_text.encode("utf-8")) > 65_536
                or len(result.report_markdown.encode("utf-8")) > 65_536):
            return None
        _, published, _ = self.agent.application.read_conversation_delivery(
            captured.view.case_id, captured.snapshot, report=True, artifacts=False)
        if (published is None or published.format != "markdown"
                or published.source_job_id != job.job_id
                or published.artifact is None
                or published.artifact.artifact_id != result.report_artifact_id
                or published.markdown != result.report_markdown):
            return None
        return FeedbackSource(
            case_id=aggregate.case.case_id, source_job_id=job.job_id,
            skill_name=result.skill_name, problem_text=job.generic_problem_text,
            report_markdown=result.report_markdown, report_sha256=result.report_sha256)

    @staticmethod
    def _require_owner(owner_key):
        if owner_key is None:
            raise AgentStoreError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在。", 404)

    def get_feedback(self, conversation_id, run_id, *, owner_key):
        self._require_owner(owner_key)
        with self.agent.operation_lease(conversation_id, owner_key=owner_key):
            source = self._source(conversation_id, run_id) if self.enabled else None
            return self.store.get_feedback(conversation_id, run_id, owner_key=owner_key,
                                           can_rate=source is not None)

    def put_feedback(self, conversation_id, run_id, request_id, rating, *, owner_key):
        self._require_owner(owner_key)
        request = FeedbackRequest(request_id=request_id, rating=rating)
        with self.agent.operation_lease(conversation_id, owner_key=owner_key):
            source = self._source(conversation_id, run_id) if self.enabled else None
            return self.store.put_feedback(conversation_id, run_id, owner_key=owner_key,
                request_id=request.request_id, rating=request.rating, source=source)
