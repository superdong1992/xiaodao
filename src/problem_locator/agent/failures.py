"""Explicit, bounded public diagnostics, independent of exception messages."""
from __future__ import annotations

import re
import uuid

from problem_locator.contracts.enums import CaseStatus, ErrorCode, ExecutionStage, JobStatus, OutcomeDisposition
from problem_locator.contracts.models import ApplicationErrorDetail, CaseAggregate, ExecutionFailure

from .models import AgentPublicFailure

_AGENT_CODES = frozenset({
    "AGENT_EXECUTION_FAILED", "AGENT_INTERRUPTED", "AGENT_DISPATCH_INTERRUPTED",
    "AGENT_INTAKE_UNCERTAIN", "AGENT_NO_MATCHING_INPUT", "AGENT_UNAVAILABLE",
    "INTAKE_OUTPUT_INVALID", "INTAKE_INPUT_INVALID", "INTAKE_CONTEXT_LIMIT",
    "INTAKE_EXECUTION_FAILED", "INTAKE_ASSET_UNAVAILABLE",
})
_CODES = frozenset(item.value for item in ErrorCode) | _AGENT_CODES
_PHASE_MESSAGES = {
    "AGENT": "本次定位未能完成，请重新发起任务。",
    "CREATE_CASE": "定位任务创建失败，请重新发起任务。",
    "INTAKE": "补充信息整理失败，本次任务已结束。请核对输入后新建任务。",
    "CASE_QUERY": "定位状态读取失败，本次任务已结束。",
    "PREPARE_ATTACHMENT": "日志附件预约失败，本次任务已结束。",
    "IMPORT_ATTACHMENT": "日志附件未能用于诊断，本次任务已结束。",
    "SUBMIT_SUPPLEMENT": "补充内容未能提交，本次任务已结束。",
    "RESTART": "服务已重启，本次任务已中断，请重新发起。",
}
_PHASES = frozenset(_PHASE_MESSAGES) | frozenset(item.value for item in ExecutionStage)
_IDENTIFIER = re.compile(r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|diag-[A-Za-z0-9_-]{1,128})\Z")
_REASON = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_LOCATION = re.compile(
    r"(?:inputs|input_names|input_values|initial_user_fact_names|initial_user_fact_values|problem_spec|"
    r"attachment_ids|attachments|declared_size|declared_sha256|content_type|expected_case_revision|idempotency_key|"
    r"methods_result|verification_result|findings|evidence|evidence_refs|matched_rules|rules)"
    r"(?:(?:\.[a-z][a-z0-9_]{0,63})|(?:\[[0-9]{1,5}\])){0,8}\Z"
)


def interrupted_execution_failure(aggregate: CaseAggregate) -> ExecutionFailure | None:
    """Resolve a unique current interruption without borrowing a historical error.

    Replacement links identify superseded interruptions even when clocks tie.
    An applied Outcome finishes its Job at produced_at; an infrastructure
    failure record uses recorded_at. Case.updated_at can advance for late audit
    submissions, so it is not a reliable link to either source.
    """
    if aggregate.case.status is not CaseStatus.INTERRUPTED or aggregate.case.failure is not None:
        return None
    replaced = {job.replacement_for_job_id for job in aggregate.jobs.values()}
    candidates = [job for job in aggregate.jobs.values()
        if job.status is JobStatus.INTERRUPTED and job.job_id not in replaced]
    if len(candidates) != 1:
        return None
    job = candidates[0]
    failures = []
    for outcome in aggregate.outcomes.values():
        processing = aggregate.outcome_processing_records.get(outcome.outcome_id)
        if (
            outcome.job_id == job.job_id and outcome.error is not None
            and outcome.produced_at == job.finished_at
            and processing is not None and processing.job_id == job.job_id
            and processing.disposition is OutcomeDisposition.APPLIED
        ):
            failures.append(outcome.error)
    failures.extend(record.failure for record in aggregate.execution_failure_records.values()
        if record.job_id == job.job_id and record.runtime_epoch == job.runtime_epoch
        and record.recorded_at == job.finished_at)
    return failures[0] if len(failures) == 1 else None


def public_failure(code=None, *, phase="AGENT", diagnostic_id=None, reason_code=None, source_details=()):
    """Only trusted vocabulary/identifiers survive; arbitrary error text never does."""
    code = getattr(code, "value", code)
    code = code if isinstance(code, str) and code in _CODES else "AGENT_EXECUTION_FAILED"
    phase = getattr(phase, "value", phase)
    phase = phase if isinstance(phase, str) and phase in _PHASES else "AGENT"
    diagnostic_id = diagnostic_id if isinstance(diagnostic_id, str) and _IDENTIFIER.fullmatch(diagnostic_id) else str(uuid.uuid4())
    details = [{"field": "phase", "actual": phase}, {"field": "diagnostic_id", "actual": diagnostic_id}]
    reason_code = getattr(reason_code, "value", reason_code)
    if isinstance(reason_code, str) and _REASON.fullmatch(reason_code):
        details.append({"field": "reason_code", "actual": reason_code})
    locations = set()
    for item in source_details:
        # A field path can identify rejected input without publishing its value.
        # Never infer locations from messages or include expected/actual content.
        location = item.field if isinstance(item, ApplicationErrorDetail) else None
        if isinstance(location, str) and len(location) <= 160 and _LOCATION.fullmatch(location) and location not in locations:
            details.append({"field": "location", "actual": location})
            locations.add(location)
            if len(locations) == 8:
                break
    return AgentPublicFailure(code=code, message=_PHASE_MESSAGES.get(phase, _PHASE_MESSAGES["AGENT"]), details=details)


def exception_code(error):
    application_error = getattr(error, "error", None)
    return getattr(application_error, "code", None) or getattr(error, "code", None)


def exception_details(error):
    return getattr(getattr(error, "error", None), "details", ())
