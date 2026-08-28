"""Fixed-name persistence helpers for Evidence V2 runtime records."""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal, Mapping, TypeVar

from pydantic import BaseModel

from problem_locator.contracts import (
    ExecutionFileRef,
    ExecutionRecordStore,
    MethodEvidenceGraphV2,
    MethodEvaluationPlanV2,
    MethodEvaluationRoleV2,
    MethodStateV2,
    OpaqueId,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)


MethodEvaluationAttemptV2 = Literal["PRIMARY", "REPAIR"]

METHODS_EVIDENCE_GRAPH_V2_FILENAME = "methods-evidence-graph-v2.json"
METHODS_EVALUATION_PLAN_V2_FILENAME = "methods-evaluation-plan-v2.json"
METHODS_STATE_V2_FILENAME = "methods-state-v2.json"

_PROMPT_FILENAMES: Mapping[
    tuple[MethodEvaluationRoleV2, MethodEvaluationAttemptV2], str
] = MappingProxyType(
    {
        ("SPECIALIST", "PRIMARY"): "methods-specialist-prompt.txt",
        ("SPECIALIST", "REPAIR"): "methods-specialist-repair-prompt.txt",
        ("REVIEWER", "PRIMARY"): "methods-reviewer-prompt.txt",
        ("REVIEWER", "REPAIR"): "methods-reviewer-repair-prompt.txt",
    }
)
_REJECTED_FILENAMES: Mapping[
    tuple[MethodEvaluationRoleV2, MethodEvaluationAttemptV2], str
] = MappingProxyType(
    {
        ("SPECIALIST", "PRIMARY"): "methods-specialist-primary.rejected.json",
        ("SPECIALIST", "REPAIR"): "methods-specialist-repair.rejected.json",
        ("REVIEWER", "PRIMARY"): "methods-reviewer-primary.rejected.json",
        ("REVIEWER", "REPAIR"): "methods-reviewer-repair.rejected.json",
    }
)

_ContractT = TypeVar("_ContractT", bound=BaseModel)


def _attempt_filename(
    filenames: Mapping[
        tuple[MethodEvaluationRoleV2, MethodEvaluationAttemptV2], str
    ],
    *,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
) -> str:
    try:
        return filenames[(role, attempt)]
    except KeyError:
        if role not in {"SPECIALIST", "REVIEWER"}:
            raise ValueError("role must be SPECIALIST or REVIEWER") from None
        raise ValueError("attempt must be PRIMARY or REPAIR") from None


def method_prompt_filename_v2(
    *,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
) -> str:
    return _attempt_filename(_PROMPT_FILENAMES, role=role, attempt=attempt)


def method_rejected_attempt_filename_v2(
    *,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
) -> str:
    return _attempt_filename(_REJECTED_FILENAMES, role=role, attempt=attempt)


def _publish_contract(
    records: ExecutionRecordStore,
    *,
    job_id: OpaqueId,
    filename: str,
    value: _ContractT,
    model_type: type[_ContractT],
) -> ExecutionFileRef:
    if not isinstance(value, model_type):
        raise TypeError(f"value must be {model_type.__name__}")
    payload = canonical_json_bytes(value)
    parse_canonical_json_bytes(payload, model_type)
    return records.publish_audit_bytes(job_id, filename, payload)


def _read_contract(
    records: ExecutionRecordStore,
    *,
    job_id: OpaqueId,
    filename: str,
    model_type: type[_ContractT],
) -> _ContractT | None:
    payload = records.read_audit_bytes(job_id, filename)
    if payload is None:
        return None
    return parse_canonical_json_bytes(payload, model_type)


def publish_method_evidence_graph_v2(
    records: ExecutionRecordStore,
    *,
    job_id: OpaqueId,
    graph: MethodEvidenceGraphV2,
) -> ExecutionFileRef:
    return _publish_contract(
        records,
        job_id=job_id,
        filename=METHODS_EVIDENCE_GRAPH_V2_FILENAME,
        value=graph,
        model_type=MethodEvidenceGraphV2,
    )


def read_method_evidence_graph_v2(
    records: ExecutionRecordStore,
    *,
    job_id: OpaqueId,
) -> MethodEvidenceGraphV2 | None:
    return _read_contract(
        records,
        job_id=job_id,
        filename=METHODS_EVIDENCE_GRAPH_V2_FILENAME,
        model_type=MethodEvidenceGraphV2,
    )


def publish_method_evaluation_plan_v2(
    records: ExecutionRecordStore,
    *,
    job_id: OpaqueId,
    plan: MethodEvaluationPlanV2,
) -> ExecutionFileRef:
    return _publish_contract(
        records,
        job_id=job_id,
        filename=METHODS_EVALUATION_PLAN_V2_FILENAME,
        value=plan,
        model_type=MethodEvaluationPlanV2,
    )


def read_method_evaluation_plan_v2(
    records: ExecutionRecordStore,
    *,
    job_id: OpaqueId,
) -> MethodEvaluationPlanV2 | None:
    return _read_contract(
        records,
        job_id=job_id,
        filename=METHODS_EVALUATION_PLAN_V2_FILENAME,
        model_type=MethodEvaluationPlanV2,
    )


def publish_method_state_v2(
    records: ExecutionRecordStore,
    *,
    job_id: OpaqueId,
    state: MethodStateV2,
) -> ExecutionFileRef:
    return _publish_contract(
        records,
        job_id=job_id,
        filename=METHODS_STATE_V2_FILENAME,
        value=state,
        model_type=MethodStateV2,
    )


def read_method_state_v2(
    records: ExecutionRecordStore,
    *,
    job_id: OpaqueId,
) -> MethodStateV2 | None:
    return _read_contract(
        records,
        job_id=job_id,
        filename=METHODS_STATE_V2_FILENAME,
        model_type=MethodStateV2,
    )


def publish_method_prompt_v2(
    records: ExecutionRecordStore,
    *,
    job_id: OpaqueId,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
    prompt_bytes: bytes,
) -> ExecutionFileRef:
    if type(prompt_bytes) is not bytes:
        raise TypeError("prompt_bytes must be exact bytes")
    return records.publish_audit_bytes(
        job_id,
        method_prompt_filename_v2(role=role, attempt=attempt),
        prompt_bytes,
    )


def read_method_prompt_v2(
    records: ExecutionRecordStore,
    *,
    job_id: OpaqueId,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
) -> bytes | None:
    return records.read_audit_bytes(
        job_id,
        method_prompt_filename_v2(role=role, attempt=attempt),
    )


def publish_method_rejected_attempt_v2(
    records: ExecutionRecordStore,
    *,
    job_id: OpaqueId,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
    raw_bytes: bytes,
) -> ExecutionFileRef:
    if type(raw_bytes) is not bytes:
        raise TypeError("raw_bytes must be exact bytes")
    return records.publish_audit_bytes(
        job_id,
        method_rejected_attempt_filename_v2(role=role, attempt=attempt),
        raw_bytes,
    )


def read_method_rejected_attempt_v2(
    records: ExecutionRecordStore,
    *,
    job_id: OpaqueId,
    role: MethodEvaluationRoleV2,
    attempt: MethodEvaluationAttemptV2,
) -> bytes | None:
    return records.read_audit_bytes(
        job_id,
        method_rejected_attempt_filename_v2(role=role, attempt=attempt),
    )


__all__ = [
    "METHODS_EVALUATION_PLAN_V2_FILENAME",
    "METHODS_EVIDENCE_GRAPH_V2_FILENAME",
    "METHODS_STATE_V2_FILENAME",
    "MethodEvaluationAttemptV2",
    "method_prompt_filename_v2",
    "method_rejected_attempt_filename_v2",
    "publish_method_evaluation_plan_v2",
    "publish_method_evidence_graph_v2",
    "publish_method_prompt_v2",
    "publish_method_rejected_attempt_v2",
    "publish_method_state_v2",
    "read_method_evaluation_plan_v2",
    "read_method_evidence_graph_v2",
    "read_method_prompt_v2",
    "read_method_rejected_attempt_v2",
    "read_method_state_v2",
]
