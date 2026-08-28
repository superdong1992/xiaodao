"""Validation-only replay for persisted Evidence V2 protocol rejections."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeVar

from problem_locator.contracts import (
    ApplicationPortError,
    ExecutionRecordStore,
    InvalidJsonBytesError,
    Job,
    MethodEvidenceGraphV2,
    MethodEvaluationPlanV2,
    MethodEvaluationRoleV2,
    MethodStateV2,
    OpaqueId,
    bytes_sha256,
    canonical_json_bytes,
)
from problem_locator.domain.methods_state_v2 import (
    record_protocol_error_v2,
    resume_method_state_v2,
    start_method_state_v2,
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
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
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


def _read_optional_state(
    records: ExecutionRecordStore,
    *,
    state_job_id: OpaqueId,
    job_id: OpaqueId,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
) -> MethodStateV2 | None:
    try:
        return read_method_state_v2(records, job_id=state_job_id)
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
            "Evidence V2 replay State is invalid.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        ) from exc


def _read_required_job(
    records: ExecutionRecordStore,
    *,
    job_id: OpaqueId,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
) -> Job:
    try:
        receipt = records.read_published_job(job_id)
    except (ApplicationPortError, OSError) as exc:
        raise _error(
            MethodValidationReplayErrorCodeV2.RECORD_READ_FAILED,
            "Evidence V2 replay could not read the immutable Job record.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        ) from exc
    except (TypeError, ValueError) as exc:
        raise _error(
            MethodValidationReplayErrorCodeV2.CORE_RECORD_INVALID,
            "Evidence V2 replay Job is invalid.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        ) from exc
    if receipt is None:
        raise _error(
            MethodValidationReplayErrorCodeV2.JOB_NOT_FOUND,
            "Evidence V2 replay Job was not found.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        )
    return receipt.job


def _derived_evaluation_id(
    *,
    case_id: str,
    source_job_id: str,
    plan_ref: str,
) -> str:
    """Reproduce the frozen ``IdGenerator.derive`` identity without runtime I/O."""

    name = canonical_json_bytes(
        {
            "kind": "methods_evaluation",
            "parts": [case_id, source_job_id, plan_ref],
        }
    )[:-1].decode("utf-8")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _workflow_mismatch(
    message: str,
    *,
    job_id: OpaqueId,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
) -> MethodValidationReplayErrorV2:
    return _error(
        MethodValidationReplayErrorCodeV2.WORKFLOW_MISMATCH,
        message,
        job_id=job_id,
        role=role,
        attempt=attempt,
    )


def _read_rejected_attempt(
    records: ExecutionRecordStore,
    *,
    job_id: OpaqueId,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
) -> bytes | None:
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
    if raw_bytes is not None and type(raw_bytes) is not bytes:
        raise _error(
            MethodValidationReplayErrorCodeV2.CORE_RECORD_INVALID,
            "The selected Evidence V2 rejected attempt is not exact bytes.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        )
    return raw_bytes


def _apply_rejected_attempts(
    *,
    state: MethodStateV2,
    primary: bytes | None,
    repair: bytes | None,
    job_id: OpaqueId,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
) -> MethodStateV2:
    if repair is not None and primary is None:
        raise _workflow_mismatch(
            f"A {role} repair rejection exists without its primary rejection.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        )

    rebuilt = state
    recorded = (primary is not None, repair is not None)
    for expected_failures, exists in enumerate(recorded):
        if not exists:
            continue
        failures = getattr(rebuilt, f"{role.lower()}_protocol_failures")
        if failures > expected_failures:
            continue
        if failures != expected_failures:
            raise _workflow_mismatch(
                f"The {role} rejected-attempt sequence differs from its State.",
                job_id=job_id,
                role=role,
                attempt=attempt,
            )
        try:
            rebuilt = record_protocol_error_v2(
                state=rebuilt,
                role=role,
                reason=f"The persisted {role} response was rejected.",
            )
        except (TypeError, ValueError) as exc:
            raise _workflow_mismatch(
                f"The {role} rejected-attempt sequence cannot advance its State.",
                job_id=job_id,
                role=role,
                attempt=attempt,
            ) from exc
    if getattr(rebuilt, f"{role.lower()}_protocol_failures") != sum(recorded):
        raise _workflow_mismatch(
            f"The {role} rejected-attempt sequence differs from its State.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        )
    return rebuilt


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

    job = _read_required_job(
        records,
        job_id=job_id,
        role=role,
        attempt=attempt,
    )
    if role == "SPECIALIST":
        if job.methods_review_target is not None:
            raise _workflow_mismatch(
                "Specialist replay requires a source diagnosis Job.",
                job_id=job_id,
                role=role,
                attempt=attempt,
            )
        source_job_id = job_id
    else:
        target = job.methods_review_target
        if target is None:
            raise _workflow_mismatch(
                "Reviewer replay requires a Methods review target.",
                job_id=job_id,
                role=role,
                attempt=attempt,
            )
        source_job_id = target.source_job_id

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
    state = _read_optional_state(
        records,
        state_job_id=job_id,
        job_id=job_id,
        role=role,
        attempt=attempt,
    )
    if state is None:
        if role == "SPECIALIST":
            if job.replacement_for_job_id is None:
                state = start_method_state_v2(
                    case_id=job.case_id,
                    source_job_id=job_id,
                    evaluation_id=_derived_evaluation_id(
                        case_id=job.case_id,
                        source_job_id=job_id,
                        plan_ref=plan.plan_ref,
                    ),
                    plan=plan,
                )
            else:
                predecessor_job_id = job.replacement_for_job_id
                assert predecessor_job_id is not None
                predecessor_state = _read_required_core_record(
                    lambda: read_method_state_v2(
                        records,
                        job_id=predecessor_job_id,
                    ),
                    missing_code=MethodValidationReplayErrorCodeV2.STATE_NOT_FOUND,
                    missing_message=(
                        "Evidence V2 replacement predecessor State was not found."
                    ),
                    invalid_message=(
                        "Evidence V2 replacement predecessor State is invalid."
                    ),
                    job_id=job_id,
                    role=role,
                    attempt=attempt,
                )
                try:
                    state = resume_method_state_v2(
                        state=predecessor_state,
                        source_job_id=job_id,
                    )
                except (TypeError, ValueError) as exc:
                    raise _workflow_mismatch(
                        "Specialist replacement State is not resumable.",
                        job_id=job_id,
                        role=role,
                        attempt=attempt,
                    ) from exc
        else:
            state = _read_required_core_record(
                lambda: read_method_state_v2(records, job_id=source_job_id),
                missing_code=MethodValidationReplayErrorCodeV2.STATE_NOT_FOUND,
                missing_message="Evidence V2 Reviewer source State was not found.",
                invalid_message="Evidence V2 Reviewer source State is invalid.",
                job_id=job_id,
                role=role,
                attempt=attempt,
            )

    if state.case_id != job.case_id or state.source_job_id != source_job_id:
        raise _workflow_mismatch(
            "Evidence V2 replay Job and State do not describe one workflow.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        )
    if role == "SPECIALIST":
        if job.replacement_for_job_id is None and state.evaluation_id != (
            _derived_evaluation_id(
                case_id=job.case_id,
                source_job_id=job_id,
                plan_ref=plan.plan_ref,
            )
        ):
            raise _workflow_mismatch(
                "Specialist replay State has a different evaluation identity.",
                job_id=job_id,
                role=role,
                attempt=attempt,
            )
    else:
        assert job.methods_review_target is not None
        target = job.methods_review_target
        if (
            state.evaluation_id != target.evaluation_id
            or target.graph_ref != graph.graph_ref
            or target.plan_ref != plan.plan_ref
        ):
            raise _workflow_mismatch(
                "Reviewer replay target differs from its source workflow.",
                job_id=job_id,
                role=role,
                attempt=attempt,
            )

    primary = _read_rejected_attempt(
        records,
        job_id=job_id,
        role=role,
        attempt="PRIMARY",
    )
    repair = _read_rejected_attempt(
        records,
        job_id=job_id,
        role=role,
        attempt="REPAIR",
    )
    raw_bytes = primary if attempt == "PRIMARY" else repair
    if raw_bytes is None:
        raise _error(
            MethodValidationReplayErrorCodeV2.REJECTED_ATTEMPT_NOT_FOUND,
            "The selected Evidence V2 rejected attempt was not found.",
            job_id=job_id,
            role=role,
            attempt=attempt,
        )
    state = _apply_rejected_attempts(
        state=state,
        primary=primary,
        repair=repair,
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
