"""Validation-only replay for persisted Evidence V2 protocol rejections."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeVar

from problem_locator.contracts import (
    ApplicationPortError,
    ExecutionRecordStore,
    InvalidJsonBytesError,
    MethodEvidenceGraphV2,
    MethodEvaluationPlanV2,
    MethodEvaluationRoleV2,
    MethodStateV2,
    OpaqueId,
    bytes_sha256,
)
from problem_locator.integrations.agent_json import parse_agent_json_bytes

from .methods_evaluation_v2 import (
    MethodEvaluationResponseError,
    evaluate_method_role_v2,
)
from .methods_evidence_v2 import validate_method_evaluation_plan_v2
from .methods_records_v2 import (
    MethodEvaluationAttemptV2,
    read_method_evaluation_plan_v2,
    read_method_evidence_graph_v2,
    read_method_rejected_attempt_v2,
    read_method_state_v2,
)


class MethodValidationReplayErrorCodeV2(StrEnum):
    """Stable failure classification for the validation-only replay boundary."""

    RECORD_READ_FAILED = "RECORD_READ_FAILED"
    CORE_RECORD_INVALID = "CORE_RECORD_INVALID"
    STATE_NOT_FOUND = "STATE_NOT_FOUND"
    EVIDENCE_GRAPH_NOT_FOUND = "EVIDENCE_GRAPH_NOT_FOUND"
    EVALUATION_PLAN_NOT_FOUND = "EVALUATION_PLAN_NOT_FOUND"
    WORKFLOW_MISMATCH = "WORKFLOW_MISMATCH"
    REJECTED_ATTEMPT_NOT_FOUND = "REJECTED_ATTEMPT_NOT_FOUND"
    REJECTION_NOT_REPRODUCED = "REJECTION_NOT_REPRODUCED"


class MethodValidationReplayErrorV2(RuntimeError):
    """One typed replay failure without starting a scanner or model session."""

    def __init__(
        self,
        code: MethodValidationReplayErrorCodeV2,
        message: str,
        *,
        job_id: OpaqueId,
        role: MethodEvaluationRoleV2,
        attempt: MethodEvaluationAttemptV2,
    ) -> None:
        self.code = code
        self.job_id = job_id
        self.role = role
        self.attempt = attempt
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class MethodValidationReplayReceiptV2:
    """Receipt proving that the current parser reproduced one stored rejection."""

    status: Literal["REJECTION_REPRODUCED"]
    job_id: OpaqueId
    source_job_id: OpaqueId
    role: MethodEvaluationRoleV2
    attempt: MethodEvaluationAttemptV2
    graph_ref: str
    plan_ref: str
    state_ref: str
    raw_response_sha256: str
    raw_response_size: int
    rejection_reason: str


_RecordT = TypeVar(
    "_RecordT",
    MethodEvidenceGraphV2,
    MethodEvaluationPlanV2,
    MethodStateV2,
)


def _error(
    code: MethodValidationReplayErrorCodeV2,
    message: str,
    *,
    job_id: OpaqueId,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
) -> MethodValidationReplayErrorV2:
    return MethodValidationReplayErrorV2(
        code,
        message,
        job_id=job_id,
        role=role,
        attempt=attempt,
    )


def _read_required_core_record(
    reader: Callable[[], _RecordT | None],
    *,
    missing_code: MethodValidationReplayErrorCodeV2,
    missing_message: str,
    invalid_message: str,
    job_id: OpaqueId,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
) -> _RecordT:
    try:
        value = reader()
    except (ApplicationPortError, OSError) as exc:
        raise _error(
            MethodValidationReplayErrorCodeV2.RECORD_READ_FAILED,
            "Evidence V2 replay could not read an execution record.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        ) from exc
    except (TypeError, ValueError) as exc:
        raise _error(
            MethodValidationReplayErrorCodeV2.CORE_RECORD_INVALID,
            invalid_message,
            job_id=job_id,
            role=role,
            attempt=attempt,
        ) from exc
    if value is None:
        raise _error(
            missing_code,
            missing_message,
            job_id=job_id,
            role=role,
            attempt=attempt,
        )
    return value


def _validate_workflow(
    *,
    graph: MethodEvidenceGraphV2,
    plan: MethodEvaluationPlanV2,
    state: MethodStateV2,
    job_id: OpaqueId,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
) -> None:
    try:
        validate_method_evaluation_plan_v2(evidence=graph, plan=plan)
    except (TypeError, ValueError) as exc:
        raise _error(
            MethodValidationReplayErrorCodeV2.WORKFLOW_MISMATCH,
            "Evidence V2 replay Graph and Plan do not describe one workflow.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        ) from exc

    evaluation_refs = tuple(item.evaluation_ref for item in plan.evaluations)
    if state.plan_ref != plan.plan_ref or state.evaluation_refs != evaluation_refs:
        raise _error(
            MethodValidationReplayErrorCodeV2.WORKFLOW_MISMATCH,
            "Evidence V2 replay State does not match the stored Evaluation Plan.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        )
    if role == "SPECIALIST" and state.source_job_id != job_id:
        raise _error(
            MethodValidationReplayErrorCodeV2.WORKFLOW_MISMATCH,
            "Specialist replay records belong to a different source Job.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        )

    failures = getattr(state, f"{role.lower()}_protocol_failures")
    required_failures = 1 if attempt == "PRIMARY" else 2
    if failures < required_failures:
        raise _error(
            MethodValidationReplayErrorCodeV2.WORKFLOW_MISMATCH,
            "The stored State does not record the selected rejected attempt.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        )


def _rejection_reason(
    raw_bytes: bytes,
    *,
    plan: MethodEvaluationPlanV2,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
) -> str | None:
    try:
        document = parse_agent_json_bytes(raw_bytes)
    except InvalidJsonBytesError:
        return f"{role} model evaluation response is not valid UTF-8 JSON"
    try:
        evaluate_method_role_v2(
            role=role,
            plan=plan,
            response=document.value,
            attempt=attempt,
        )
    except MethodEvaluationResponseError as exc:
        return str(exc)
    return None


def replay_method_rejected_attempt_v2(
    records: ExecutionRecordStore,
    *,
    job_id: OpaqueId,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
) -> MethodValidationReplayReceiptV2:
    """Re-run validation for one stored rejection without scanning or a model call."""

    if role not in {"SPECIALIST", "REVIEWER"}:
        raise ValueError("role must be SPECIALIST or REVIEWER")
    if attempt not in {"PRIMARY", "REPAIR"}:
        raise ValueError("attempt must be PRIMARY or REPAIR")

    state = _read_required_core_record(
        lambda: read_method_state_v2(records, job_id=job_id),
        missing_code=MethodValidationReplayErrorCodeV2.STATE_NOT_FOUND,
        missing_message="Evidence V2 replay State was not found.",
        invalid_message="Evidence V2 replay State is invalid.",
        job_id=job_id,
        role=role,
        attempt=attempt,
    )
    source_job_id = state.source_job_id
    graph = _read_required_core_record(
        lambda: read_method_evidence_graph_v2(records, job_id=source_job_id),
        missing_code=(
            MethodValidationReplayErrorCodeV2.EVIDENCE_GRAPH_NOT_FOUND
        ),
        missing_message="Evidence V2 replay Graph was not found.",
        invalid_message="Evidence V2 replay Graph is invalid.",
        job_id=job_id,
        role=role,
        attempt=attempt,
    )
    plan = _read_required_core_record(
        lambda: read_method_evaluation_plan_v2(records, job_id=source_job_id),
        missing_code=MethodValidationReplayErrorCodeV2.EVALUATION_PLAN_NOT_FOUND,
        missing_message="Evidence V2 replay Evaluation Plan was not found.",
        invalid_message="Evidence V2 replay Evaluation Plan is invalid.",
        job_id=job_id,
        role=role,
        attempt=attempt,
    )
    _validate_workflow(
        graph=graph,
        plan=plan,
        state=state,
        job_id=job_id,
        role=role,
        attempt=attempt,
    )

    try:
        raw_bytes = read_method_rejected_attempt_v2(
            records,
            job_id=job_id,
            role=role,
            attempt=attempt,
        )
    except (ApplicationPortError, OSError) as exc:
        raise _error(
            MethodValidationReplayErrorCodeV2.RECORD_READ_FAILED,
            "Evidence V2 replay could not read the rejected attempt.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        ) from exc
    if raw_bytes is None:
        raise _error(
            MethodValidationReplayErrorCodeV2.REJECTED_ATTEMPT_NOT_FOUND,
            "The selected Evidence V2 rejected attempt was not found.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        )
    if type(raw_bytes) is not bytes:
        raise _error(
            MethodValidationReplayErrorCodeV2.CORE_RECORD_INVALID,
            "The selected Evidence V2 rejected attempt is not exact bytes.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        )

    reason = _rejection_reason(
        raw_bytes,
        plan=plan,
        role=role,
        attempt=attempt,
    )
    if reason is None:
        raise _error(
            MethodValidationReplayErrorCodeV2.REJECTION_NOT_REPRODUCED,
            "The current production parser accepts the stored rejected attempt.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        )
    return MethodValidationReplayReceiptV2(
        status="REJECTION_REPRODUCED",
        job_id=job_id,
        source_job_id=source_job_id,
        role=role,
        attempt=attempt,
        graph_ref=graph.graph_ref,
        plan_ref=plan.plan_ref,
        state_ref=state.state_ref,
        raw_response_sha256=bytes_sha256(raw_bytes),
        raw_response_size=len(raw_bytes),
        rejection_reason=reason,
    )


__all__ = [
    "MethodValidationReplayErrorCodeV2",
    "MethodValidationReplayErrorV2",
    "MethodValidationReplayReceiptV2",
    "replay_method_rejected_attempt_v2",
]
