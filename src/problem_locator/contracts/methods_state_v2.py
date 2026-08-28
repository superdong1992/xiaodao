"""Pure business contracts for the Evidence V2 state and terminal result layer."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from .methods_v2 import (
    MethodConsensusV2,
    MethodEvidenceEventRefV2,
    MethodEvidenceGraphRefV2,
    MethodEvidenceHitRefV2,
    MethodEvaluationPlanRefV2,
    MethodEvaluationRefV2,
    MethodEvaluationRoleV2,
    MethodIdV2,
    MethodRoleEvaluationV2,
)
from .models import ContractModel, NonEmptyText, OpaqueId
from .serialization import canonical_json_sha256


MethodStateStatusV2: TypeAlias = Literal[
    "SPECIALIST_PENDING",
    "REVIEWER_PENDING",
    "RESOLVED",
    "UNRESOLVED",
    "FAILED",
    "INTERRUPTED",
]
MethodTerminalStatusV2: TypeAlias = Literal["RESOLVED", "UNRESOLVED", "FAILED"]
MethodStateReasonCodeV2: TypeAlias = Literal[
    "SPECIALIST_PROTOCOL_REPAIR_EXHAUSTED",
    "REVIEWER_PROTOCOL_REPAIR_EXHAUSTED",
    "SPECIALIST_SEMANTIC_INVALID",
    "REVIEWER_SEMANTIC_INVALID",
    "SPECIALIST_MODEL_EXECUTION_FAILED",
    "REVIEWER_MODEL_EXECUTION_FAILED",
    "SPECIALIST_REVIEWER_DISAGREEMENT",
    "INCOMPLETE_EVALUATION",
    "NO_CONFIRMED_METHOD",
    "NO_MATCHING_METHOD_EVIDENCE",
    "RESOURCE_SNAPSHOT_DRIFT",
    "SERVER_INVARIANT_VIOLATION",
    "AUDIT_ARCHIVE_FAILED",
]
MethodStateRefV2: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^state-[0-9a-f]{64}$", strict=True),
]
MethodDiagnosticIdV2: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^diag-[0-9a-f]{64}$", strict=True),
]
MethodTerminalResultRefV2: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^result-[0-9a-f]{64}$", strict=True),
]
MethodProtocolFailureCountV2: TypeAlias = Annotated[
    int,
    Field(ge=0, le=2, strict=True),
]

_UNRESOLVED_REASONS = frozenset(
    {
        "SPECIALIST_PROTOCOL_REPAIR_EXHAUSTED",
        "REVIEWER_PROTOCOL_REPAIR_EXHAUSTED",
        "SPECIALIST_SEMANTIC_INVALID",
        "REVIEWER_SEMANTIC_INVALID",
        "SPECIALIST_MODEL_EXECUTION_FAILED",
        "REVIEWER_MODEL_EXECUTION_FAILED",
        "SPECIALIST_REVIEWER_DISAGREEMENT",
        "INCOMPLETE_EVALUATION",
        "NO_CONFIRMED_METHOD",
        "NO_MATCHING_METHOD_EVIDENCE",
    }
)
_FAILED_REASONS = frozenset(
    {
        "RESOURCE_SNAPSHOT_DRIFT",
        "SERVER_INVARIANT_VIOLATION",
        "AUDIT_ARCHIVE_FAILED",
    }
)


class _MethodStateContract(ContractModel):
    model_config = ConfigDict(frozen=True)


def method_diagnostic_id_v2(
    *,
    evaluation_id: str,
    plan_ref: str,
    status: str,
    reason_code: str | None,
    evaluation_ref: str | None,
) -> str:
    return "diag-" + canonical_json_sha256(
        {
            "kind": "method-diagnostic-v2",
            "evaluation_id": evaluation_id,
            "plan_ref": plan_ref,
            "status": status,
            "reason_code": reason_code,
            "evaluation_ref": evaluation_ref,
        }
    )


def _role_dump(value: MethodRoleEvaluationV2 | None) -> dict[str, object] | None:
    return None if value is None else value.model_dump(mode="json")


def _consensus_dump(value: MethodConsensusV2 | None) -> dict[str, object] | None:
    return None if value is None else value.model_dump(mode="json")


def method_state_ref_v2(
    *,
    evaluation_id: str,
    plan_ref: str,
    evaluation_refs: tuple[str, ...],
    status: str,
    current_role: str | None,
    specialist_protocol_failures: int,
    reviewer_protocol_failures: int,
    specialist_evaluation: MethodRoleEvaluationV2 | None,
    reviewer_evaluation: MethodRoleEvaluationV2 | None,
    consensus: MethodConsensusV2 | None,
    reason_code: str | None,
    diagnostic_id: str | None,
    diagnostic_evaluation_ref: str | None,
    reasons: tuple[str, ...],
) -> str:
    return "state-" + canonical_json_sha256(
        {
            "kind": "method-state-v2",
            "evaluation_id": evaluation_id,
            "plan_ref": plan_ref,
            "evaluation_refs": list(evaluation_refs),
            "status": status,
            "current_role": current_role,
            "specialist_protocol_failures": specialist_protocol_failures,
            "reviewer_protocol_failures": reviewer_protocol_failures,
            "specialist_evaluation": _role_dump(specialist_evaluation),
            "reviewer_evaluation": _role_dump(reviewer_evaluation),
            "consensus": _consensus_dump(consensus),
            "reason_code": reason_code,
            "diagnostic_id": diagnostic_id,
            "diagnostic_evaluation_ref": diagnostic_evaluation_ref,
            "reasons": list(reasons),
        }
    )


class MethodStateV2(_MethodStateContract):
    state_ref: MethodStateRefV2
    evaluation_id: OpaqueId
    plan_ref: MethodEvaluationPlanRefV2
    evaluation_refs: tuple[MethodEvaluationRefV2, ...]
    status: MethodStateStatusV2
    current_role: MethodEvaluationRoleV2 | None
    specialist_protocol_failures: MethodProtocolFailureCountV2
    reviewer_protocol_failures: MethodProtocolFailureCountV2
    specialist_evaluation: MethodRoleEvaluationV2 | None
    reviewer_evaluation: MethodRoleEvaluationV2 | None
    consensus: MethodConsensusV2 | None
    reason_code: MethodStateReasonCodeV2 | None
    diagnostic_id: MethodDiagnosticIdV2 | None
    diagnostic_evaluation_ref: MethodEvaluationRefV2 | None
    reasons: tuple[NonEmptyText, ...]

    @model_validator(mode="after")
    def validate_state(self) -> "MethodStateV2":
        if len(self.evaluation_refs) != len(set(self.evaluation_refs)):
            raise ValueError("state evaluation refs must be unique")
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError("state reasons must be unique")
        if self.diagnostic_evaluation_ref is not None and (
            self.diagnostic_evaluation_ref not in self.evaluation_refs
        ):
            raise ValueError("diagnostic evaluation must belong to the state plan")
        for value, expected_role in (
            (self.specialist_evaluation, "SPECIALIST"),
            (self.reviewer_evaluation, "REVIEWER"),
        ):
            if value is not None and (
                value.role != expected_role or value.plan_ref != self.plan_ref
            ):
                raise ValueError("role evaluation differs from the state plan or role")
            if value is not None and tuple(
                item.evaluation_ref for item in value.evaluations
            ) != self.evaluation_refs:
                raise ValueError("role evaluation does not exactly cover state evaluations")
            if value is not None:
                failures = getattr(
                    self,
                    f"{expected_role.lower()}_protocol_failures",
                )
                expected_failures = 1 if value.repair_used else 0
                if failures != expected_failures:
                    raise ValueError(
                        "role evaluation repair marker differs from protocol failures"
                    )
        if self.consensus is not None and self.consensus.plan_ref != self.plan_ref:
            raise ValueError("consensus differs from the state plan")
        if self.consensus is not None and any(
            item not in self.evaluation_refs
            for item in self.consensus.confirmed_evaluation_refs
        ):
            raise ValueError("consensus confirms an evaluation outside the state plan")

        if self.status in {"SPECIALIST_PENDING", "REVIEWER_PENDING"}:
            expected_role = (
                "SPECIALIST" if self.status == "SPECIALIST_PENDING" else "REVIEWER"
            )
            if (
                self.current_role != expected_role
                or self.reason_code is not None
                or self.diagnostic_id is not None
                or self.diagnostic_evaluation_ref is not None
                or self.consensus is not None
                or self.reasons
            ):
                raise ValueError("pending state fields are inconsistent")
            if (
                self.specialist_protocol_failures > 1
                or self.reviewer_protocol_failures > 1
            ):
                raise ValueError("pending role may consume at most one protocol repair")
            if self.status == "SPECIALIST_PENDING" and (
                self.specialist_evaluation is not None
                or self.reviewer_evaluation is not None
                or self.specialist_protocol_failures > 1
                or self.reviewer_protocol_failures != 0
            ):
                raise ValueError("specialist pending must not contain role evaluations")
            if self.status == "REVIEWER_PENDING" and (
                self.specialist_evaluation is None
                or self.reviewer_evaluation is not None
                or self.reviewer_protocol_failures > 1
            ):
                raise ValueError("reviewer pending requires only specialist evaluation")
        elif self.status == "INTERRUPTED":
            if (
                self.current_role not in {"SPECIALIST", "REVIEWER"}
                or self.reason_code is not None
                or self.diagnostic_id is not None
                or self.diagnostic_evaluation_ref is not None
                or self.consensus is not None
                or self.reasons
            ):
                raise ValueError("interrupted state must preserve only its pending role")
            if (
                self.specialist_protocol_failures > 1
                or self.reviewer_protocol_failures > 1
            ):
                raise ValueError("interrupted role may retain at most one protocol repair")
            if self.current_role == "SPECIALIST" and (
                self.specialist_evaluation is not None
                or self.reviewer_evaluation is not None
                or self.specialist_protocol_failures > 1
                or self.reviewer_protocol_failures != 0
            ):
                raise ValueError("interrupted specialist state has unexpected evaluations")
            if self.current_role == "REVIEWER" and (
                self.specialist_evaluation is None
                or self.reviewer_evaluation is not None
                or self.reviewer_protocol_failures > 1
            ):
                raise ValueError("interrupted reviewer state must retain specialist evaluation")
        else:
            if self.current_role is not None or self.diagnostic_id is None:
                raise ValueError("terminal state must clear role and expose a diagnostic id")
            if self.status == "RESOLVED":
                if (
                    self.reason_code is not None
                    or self.specialist_evaluation is None
                    or self.reviewer_evaluation is None
                    or self.consensus is None
                    or self.consensus.status != "RESOLVED"
                    or self.reasons
                ):
                    raise ValueError("resolved state requires complete resolved consensus")
            elif self.status == "UNRESOLVED":
                if self.reason_code not in _UNRESOLVED_REASONS:
                    raise ValueError("unresolved state reason code is invalid")
                if self.consensus is not None and self.consensus.status != "UNRESOLVED":
                    raise ValueError("unresolved state cannot retain resolved consensus")
            elif self.status == "FAILED" and self.reason_code not in _FAILED_REASONS:
                raise ValueError("failed state reason code is invalid")
            if self.reason_code == "SPECIALIST_PROTOCOL_REPAIR_EXHAUSTED" and (
                self.specialist_protocol_failures != 2
            ):
                raise ValueError("specialist protocol exhaustion requires two failures")
            if self.reason_code == "REVIEWER_PROTOCOL_REPAIR_EXHAUSTED" and (
                self.reviewer_protocol_failures != 2
            ):
                raise ValueError("reviewer protocol exhaustion requires two failures")

        if self.diagnostic_id is not None:
            expected_diagnostic = method_diagnostic_id_v2(
                evaluation_id=self.evaluation_id,
                plan_ref=self.plan_ref,
                status=self.status,
                reason_code=self.reason_code,
                evaluation_ref=self.diagnostic_evaluation_ref,
            )
            if self.diagnostic_id != expected_diagnostic:
                raise ValueError("diagnostic_id does not match the state diagnosis")

        expected = method_state_ref_v2(
            evaluation_id=self.evaluation_id,
            plan_ref=self.plan_ref,
            evaluation_refs=self.evaluation_refs,
            status=self.status,
            current_role=self.current_role,
            specialist_protocol_failures=self.specialist_protocol_failures,
            reviewer_protocol_failures=self.reviewer_protocol_failures,
            specialist_evaluation=self.specialist_evaluation,
            reviewer_evaluation=self.reviewer_evaluation,
            consensus=self.consensus,
            reason_code=self.reason_code,
            diagnostic_id=self.diagnostic_id,
            diagnostic_evaluation_ref=self.diagnostic_evaluation_ref,
            reasons=self.reasons,
        )
        if self.state_ref != expected:
            raise ValueError("state_ref does not match the method state")
        return self


class MethodConfirmedEvaluationV2(_MethodStateContract):
    evaluation_ref: MethodEvaluationRefV2
    method_id: MethodIdV2
    evidence_event_refs: tuple[MethodEvidenceEventRefV2, ...]
    evidence_hit_refs: tuple[MethodEvidenceHitRefV2, ...]
    verdict: Literal["CONFIRMED"]

    @model_validator(mode="after")
    def validate_evidence_refs(self) -> "MethodConfirmedEvaluationV2":
        if not self.evidence_event_refs or len(self.evidence_event_refs) != len(
            set(self.evidence_event_refs)
        ):
            raise ValueError("outcome evaluation event refs must be unique and non-empty")
        if not self.evidence_hit_refs or len(self.evidence_hit_refs) != len(
            set(self.evidence_hit_refs)
        ):
            raise ValueError("outcome evaluation hit refs must be unique and non-empty")
        return self


def method_terminal_result_ref_v2(
    *,
    evaluation_id: str,
    status: str,
    plan_ref: str,
    evidence_graph_ref: str,
    reason_code: str | None,
    diagnostic_id: str,
    diagnostic_evaluation_ref: str | None,
    evaluations: tuple[MethodConfirmedEvaluationV2, ...],
    confirmed_evaluation_refs: tuple[str, ...],
    confirmed_method_ids: tuple[str, ...],
    confirmed_event_refs: tuple[str, ...],
    confirmed_hit_refs: tuple[str, ...],
    limitations: tuple[str, ...],
    reasons: tuple[str, ...],
) -> str:
    return "result-" + canonical_json_sha256(
        {
            "kind": "method-terminal-result-v2",
            "evaluation_id": evaluation_id,
            "status": status,
            "plan_ref": plan_ref,
            "evidence_graph_ref": evidence_graph_ref,
            "reason_code": reason_code,
            "diagnostic_id": diagnostic_id,
            "diagnostic_evaluation_ref": diagnostic_evaluation_ref,
            "evaluations": [item.model_dump(mode="json") for item in evaluations],
            "confirmed_evaluation_refs": list(confirmed_evaluation_refs),
            "confirmed_method_ids": list(confirmed_method_ids),
            "confirmed_event_refs": list(confirmed_event_refs),
            "confirmed_hit_refs": list(confirmed_hit_refs),
            "limitations": list(limitations),
            "reasons": list(reasons),
        }
    )


class MethodTerminalResultV2(_MethodStateContract):
    result_ref: MethodTerminalResultRefV2
    evaluation_id: OpaqueId
    status: MethodTerminalStatusV2
    plan_ref: MethodEvaluationPlanRefV2
    evidence_graph_ref: MethodEvidenceGraphRefV2
    reason_code: MethodStateReasonCodeV2 | None
    diagnostic_id: MethodDiagnosticIdV2
    diagnostic_evaluation_ref: MethodEvaluationRefV2 | None
    evaluations: tuple[MethodConfirmedEvaluationV2, ...]
    confirmed_evaluation_refs: tuple[MethodEvaluationRefV2, ...]
    confirmed_method_ids: tuple[MethodIdV2, ...]
    confirmed_event_refs: tuple[MethodEvidenceEventRefV2, ...]
    confirmed_hit_refs: tuple[MethodEvidenceHitRefV2, ...]
    limitations: tuple[NonEmptyText, ...]
    reasons: tuple[NonEmptyText, ...]

    @model_validator(mode="after")
    def validate_result(self) -> "MethodTerminalResultV2":
        evaluation_refs = tuple(item.evaluation_ref for item in self.evaluations)
        if len(evaluation_refs) != len(set(evaluation_refs)):
            raise ValueError("terminal evaluation refs must be unique")
        for values, label in (
            (self.confirmed_evaluation_refs, "confirmed evaluations"),
            (self.confirmed_method_ids, "confirmed methods"),
            (self.confirmed_event_refs, "confirmed events"),
            (self.confirmed_hit_refs, "confirmed hits"),
            (self.limitations, "limitations"),
            (self.reasons, "reasons"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if len(self.confirmed_evaluation_refs) != len(self.confirmed_method_ids):
            raise ValueError("confirmed evaluation and method refs must align")
        expected_diagnostic = method_diagnostic_id_v2(
            evaluation_id=self.evaluation_id,
            plan_ref=self.plan_ref,
            status=self.status,
            reason_code=self.reason_code,
            evaluation_ref=self.diagnostic_evaluation_ref,
        )
        if self.diagnostic_id != expected_diagnostic:
            raise ValueError("terminal diagnostic_id is not stable for its identity")
        confirmed_values = (
            self.confirmed_evaluation_refs,
            self.confirmed_method_ids,
            self.confirmed_event_refs,
            self.confirmed_hit_refs,
        )
        if self.status == "RESOLVED":
            if self.reason_code is not None or any(not value for value in confirmed_values):
                raise ValueError("resolved result requires consensus-confirmed refs")
            if evaluation_refs != self.confirmed_evaluation_refs:
                raise ValueError("resolved evaluations must exactly match consensus refs")
            if tuple(item.method_id for item in self.evaluations) != self.confirmed_method_ids:
                raise ValueError("resolved evaluation methods must match consensus methods")
            mapped_event_refs = tuple(
                dict.fromkeys(
                    ref for item in self.evaluations for ref in item.evidence_event_refs
                )
            )
            mapped_hit_refs = tuple(
                dict.fromkeys(
                    ref for item in self.evaluations for ref in item.evidence_hit_refs
                )
            )
            if mapped_event_refs != self.confirmed_event_refs:
                raise ValueError("resolved event refs must come from confirmed evaluations")
            if mapped_hit_refs != self.confirmed_hit_refs:
                raise ValueError("resolved hit refs must come from confirmed evaluations")
        else:
            if self.evaluations or any(confirmed_values):
                raise ValueError(
                    "unresolved and failed results must clear evaluations and confirmed refs"
                )
            if self.status == "UNRESOLVED" and self.reason_code not in _UNRESOLVED_REASONS:
                raise ValueError("unresolved result reason code is invalid")
            if self.status == "FAILED" and self.reason_code not in _FAILED_REASONS:
                raise ValueError("failed result reason code is invalid")
        expected = method_terminal_result_ref_v2(
            evaluation_id=self.evaluation_id,
            status=self.status,
            plan_ref=self.plan_ref,
            evidence_graph_ref=self.evidence_graph_ref,
            reason_code=self.reason_code,
            diagnostic_id=self.diagnostic_id,
            diagnostic_evaluation_ref=self.diagnostic_evaluation_ref,
            evaluations=self.evaluations,
            confirmed_evaluation_refs=self.confirmed_evaluation_refs,
            confirmed_method_ids=self.confirmed_method_ids,
            confirmed_event_refs=self.confirmed_event_refs,
            confirmed_hit_refs=self.confirmed_hit_refs,
            limitations=self.limitations,
            reasons=self.reasons,
        )
        if self.result_ref != expected:
            raise ValueError("result_ref does not match the terminal result")
        return self


__all__ = [
    "MethodDiagnosticIdV2",
    "MethodConfirmedEvaluationV2",
    "MethodProtocolFailureCountV2",
    "MethodStateReasonCodeV2",
    "MethodStateRefV2",
    "MethodStateStatusV2",
    "MethodStateV2",
    "MethodTerminalResultRefV2",
    "MethodTerminalResultV2",
    "MethodTerminalStatusV2",
    "method_diagnostic_id_v2",
    "method_state_ref_v2",
    "method_terminal_result_ref_v2",
]
