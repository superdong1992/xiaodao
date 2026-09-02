"""Pure business contracts for the Evidence V2 state and terminal result layer."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from .methods_reason_v2 import (
    CONSENSUS_UNRESOLVED_REASON_CODES_V2,
    FAILED_METHOD_REASON_CODES_V2,
    METHOD_PUBLIC_REASON_TEXT_V2,
    UNRESOLVED_METHOD_REASON_CODES_V2,
    MethodStateReasonCodeV2,
)
from .methods_v2 import (
    MethodConsensusV2,
    MethodEvidenceGraphV2,
    MethodEvidenceEventRefV2,
    MethodEvidenceGraphRefV2,
    MethodEvidenceHitRefV2,
    MethodEvaluationPlanV2,
    MethodEvaluationPlanRefV2,
    MethodEvaluationRefV2,
    MethodEvaluationRoleV2,
    MethodIdV2,
    MethodRoleEvaluationV2,
)
from .models import (
    ContractModel,
    MethodsTerminalProjectionV2,
    NonEmptyText,
    OpaqueId,
)
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

class _MethodStateContract(ContractModel):
    model_config = ConfigDict(frozen=True)


def method_diagnostic_id_v2(
    *,
    case_id: str,
    source_job_id: str,
    evaluation_id: str,
    plan_ref: str,
    status: str,
    reason_code: str | None,
    evaluation_ref: str | None,
) -> str:
    return "diag-" + canonical_json_sha256(
        {
            "kind": "method-diagnostic-v2",
            "case_id": case_id,
            "source_job_id": source_job_id,
            "evaluation_id": evaluation_id,
            "plan_ref": plan_ref,
            "status": status,
            "reason_code": reason_code,
            "evaluation_ref": evaluation_ref,
        }
    )


def method_pre_evaluation_diagnostic_id_v2(
    *,
    case_id: str,
    source_job_id: str,
    reason_code: str,
    source_stage: str,
    source_error_code: str,
) -> str:
    """Derive one stable diagnostic ID before a Graph or Plan exists."""

    if reason_code not in FAILED_METHOD_REASON_CODES_V2:
        raise ValueError("pre-evaluation failure requires a FAILED reason code")
    return "diag-" + canonical_json_sha256(
        {
            "kind": "method-pre-evaluation-diagnostic-v2",
            "case_id": case_id,
            "source_job_id": source_job_id,
            "reason_code": reason_code,
            "source_stage": source_stage,
            "source_error_code": source_error_code,
        }
    )


def _role_dump(value: MethodRoleEvaluationV2 | None) -> dict[str, object] | None:
    return None if value is None else value.model_dump(mode="json")


def _consensus_dump(value: MethodConsensusV2 | None) -> dict[str, object] | None:
    return None if value is None else value.model_dump(mode="json")


def method_state_ref_v2(
    *,
    case_id: str,
    source_job_id: str,
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
            "case_id": case_id,
            "source_job_id": source_job_id,
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
    case_id: OpaqueId
    source_job_id: OpaqueId
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
        if self.consensus is not None:
            if self.specialist_evaluation is None or self.reviewer_evaluation is None:
                raise ValueError("consensus requires both role evaluations")
            specialist_blind = tuple(
                (item.evaluation_ref, item.verdict, item.supporting_event_refs)
                for item in self.specialist_evaluation.evaluations
            )
            reviewer_blind = tuple(
                (item.evaluation_ref, item.verdict, item.supporting_event_refs)
                for item in self.reviewer_evaluation.evaluations
            )
            verdicts = tuple(
                item.verdict for item in self.specialist_evaluation.evaluations
            )
            resolved = (
                specialist_blind == reviewer_blind
                and "UNKNOWN" not in verdicts
                and "CONFIRMED" in verdicts
            )
            expected_status = "RESOLVED" if resolved else "UNRESOLVED"
            expected_evaluation_refs = (
                tuple(
                    item.evaluation_ref
                    for item in self.specialist_evaluation.evaluations
                    if item.verdict == "CONFIRMED"
                )
                if resolved
                else ()
            )
            expected_event_refs = (
                tuple(
                    event_ref
                    for item in self.specialist_evaluation.evaluations
                    if item.verdict == "CONFIRMED"
                    for event_ref in item.supporting_event_refs
                )
                if resolved
                else ()
            )
            if (
                self.consensus.status != expected_status
                or self.consensus.confirmed_evaluation_refs
                != expected_evaluation_refs
                or self.consensus.confirmed_event_refs != expected_event_refs
            ):
                raise ValueError(
                    "consensus event refs differ from the two role evaluations"
                )
        if (self.reviewer_evaluation is None) != (self.consensus is None):
            raise ValueError(
                "Reviewer evaluation and consensus must be present together"
            )

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
                    or self.reasons
                ):
                    raise ValueError("resolved state requires a Specialist evaluation")
                if self.consensus is None:
                    specialist_verdicts = tuple(
                        item.verdict
                        for item in self.specialist_evaluation.evaluations
                    )
                    if (
                        self.reviewer_evaluation is not None
                        or self.reviewer_protocol_failures != 0
                        or "UNKNOWN" in specialist_verdicts
                        or "CONFIRMED" not in specialist_verdicts
                    ):
                        raise ValueError(
                            "Specialist-only resolved state requires a confirmed complete evaluation"
                        )
                elif (
                    self.reviewer_evaluation is None
                    or self.consensus.status != "RESOLVED"
                ):
                    raise ValueError(
                        "reviewed resolved state requires complete resolved consensus"
                    )
            elif self.status == "UNRESOLVED":
                if self.reason_code not in UNRESOLVED_METHOD_REASON_CODES_V2:
                    raise ValueError("unresolved state reason code is invalid")
                if self.consensus is not None and self.consensus.status != "UNRESOLVED":
                    raise ValueError("unresolved state cannot retain resolved consensus")
                if (
                    self.consensus is not None
                    and self.reason_code not in CONSENSUS_UNRESOLVED_REASON_CODES_V2
                ):
                    raise ValueError(
                        "only a consensus reason may retain Reviewer consensus"
                    )
                if self.reason_code in {
                    "INCOMPLETE_EVALUATION",
                    "NO_CONFIRMED_METHOD",
                } and self.consensus is None:
                    if (
                        self.specialist_evaluation is None
                        or self.reviewer_protocol_failures != 0
                    ):
                        raise ValueError(
                            "Specialist-only unresolved state requires its evaluation"
                        )
                    specialist_verdicts = tuple(
                        item.verdict
                        for item in self.specialist_evaluation.evaluations
                    )
                    if self.reason_code == "INCOMPLETE_EVALUATION":
                        valid_specialist_terminal = "UNKNOWN" in specialist_verdicts
                    else:
                        valid_specialist_terminal = bool(specialist_verdicts) and all(
                            verdict == "REJECTED"
                            for verdict in specialist_verdicts
                        )
                    if not valid_specialist_terminal:
                        raise ValueError(
                            "Specialist-only unresolved reason differs from its verdicts"
                        )
                if (
                    self.reason_code == "SPECIALIST_REVIEWER_DISAGREEMENT"
                    and self.consensus is None
                ):
                    raise ValueError(
                        "Specialist/Reviewer disagreement requires Reviewer consensus"
                    )
            elif (
                self.status == "FAILED"
                and self.reason_code not in FAILED_METHOD_REASON_CODES_V2
            ):
                raise ValueError("failed state reason code is invalid")
            if self.status in {"UNRESOLVED", "FAILED"} and self.reasons != (
                METHOD_PUBLIC_REASON_TEXT_V2[self.reason_code],
            ):
                raise ValueError(
                    "terminal state reasons must use the fixed public reason text"
                )
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
                case_id=self.case_id,
                source_job_id=self.source_job_id,
                evaluation_id=self.evaluation_id,
                plan_ref=self.plan_ref,
                status=self.status,
                reason_code=self.reason_code,
                evaluation_ref=self.diagnostic_evaluation_ref,
            )
            if self.diagnostic_id != expected_diagnostic:
                raise ValueError("diagnostic_id does not match the state diagnosis")

        expected = method_state_ref_v2(
            case_id=self.case_id,
            source_job_id=self.source_job_id,
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
    case_id: str,
    source_job_id: str,
    terminal_job_id: str,
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
            "case_id": case_id,
            "source_job_id": source_job_id,
            "terminal_job_id": terminal_job_id,
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
    case_id: OpaqueId
    source_job_id: OpaqueId
    terminal_job_id: OpaqueId
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
            case_id=self.case_id,
            source_job_id=self.source_job_id,
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
                raise ValueError("resolved result requires validated confirmed refs")
            if evaluation_refs != self.confirmed_evaluation_refs:
                raise ValueError("resolved evaluations must exactly match confirmed refs")
            if tuple(item.method_id for item in self.evaluations) != self.confirmed_method_ids:
                raise ValueError("resolved evaluation methods must match confirmed methods")
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
            if (
                self.status == "UNRESOLVED"
                and self.reason_code not in UNRESOLVED_METHOD_REASON_CODES_V2
            ):
                raise ValueError("unresolved result reason code is invalid")
            if (
                self.status == "FAILED"
                and self.reason_code not in FAILED_METHOD_REASON_CODES_V2
            ):
                raise ValueError("failed result reason code is invalid")
        expected = method_terminal_result_ref_v2(
            case_id=self.case_id,
            source_job_id=self.source_job_id,
            terminal_job_id=self.terminal_job_id,
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


def validate_method_terminal_result_v2(
    state: MethodStateV2,
    result: MethodTerminalResultV2,
    plan: MethodEvaluationPlanV2,
    *,
    evidence: MethodEvidenceGraphV2,
) -> MethodTerminalResultV2:
    """Revalidate and bind one terminal result to its exact state, Plan, and Graph."""

    validated_state = MethodStateV2.model_validate(
        state.model_dump(mode="python")
    )
    validated_result = MethodTerminalResultV2.model_validate(
        result.model_dump(mode="python")
    )
    validated_plan = MethodEvaluationPlanV2.model_validate(
        plan.model_dump(mode="python")
    )
    if not isinstance(evidence, MethodEvidenceGraphV2):
        raise TypeError("evidence must be MethodEvidenceGraphV2")
    validated_evidence = MethodEvidenceGraphV2.model_validate(
        evidence.model_dump(mode="python")
    )
    planned_refs = tuple(
        item.evaluation_ref for item in validated_plan.evaluations
    )
    if (
        validated_state.evaluation_refs != planned_refs
        or validated_state.plan_ref != validated_plan.plan_ref
        or validated_result.case_id != validated_state.case_id
        or validated_result.source_job_id != validated_state.source_job_id
        or validated_result.evaluation_id != validated_state.evaluation_id
        or validated_result.status != validated_state.status
        or validated_result.plan_ref != validated_plan.plan_ref
        or validated_result.evidence_graph_ref
        != validated_plan.evidence_graph_ref
        or validated_result.reason_code != validated_state.reason_code
        or validated_result.diagnostic_id != validated_state.diagnostic_id
        or validated_result.diagnostic_evaluation_ref
        != validated_state.diagnostic_evaluation_ref
        or validated_result.reasons != validated_state.reasons
    ):
        raise ValueError("Methods terminal result differs from its production state and Plan")
    if (
        validated_evidence.graph_ref != validated_plan.evidence_graph_ref
        or validated_result.evidence_graph_ref != validated_evidence.graph_ref
    ):
        raise ValueError("Methods terminal result differs from its Evidence Graph")
    if validated_state.status != "RESOLVED":
        return validated_result

    by_ref = {
        item.evaluation_ref: item for item in validated_plan.evaluations
    }
    specialist = validated_state.specialist_evaluation
    reviewer = validated_state.reviewer_evaluation
    consensus = validated_state.consensus
    if specialist is None:
        raise ValueError("resolved terminal result requires a Specialist evaluation")
    if consensus is None:
        if reviewer is not None:
            raise ValueError(
                "Specialist-only terminal result cannot retain a Reviewer evaluation"
            )
        confirmed_evaluation_refs = tuple(
            item.evaluation_ref
            for item in specialist.evaluations
            if item.verdict == "CONFIRMED"
        )
        confirmed_method_ids = tuple(
            by_ref[evaluation_ref].method_id
            for evaluation_ref in confirmed_evaluation_refs
        )
    else:
        if consensus.status != "RESOLVED" or reviewer is None:
            raise ValueError(
                "reviewed terminal result requires resolved state consensus"
            )
        confirmed_evaluation_refs = consensus.confirmed_evaluation_refs
        confirmed_method_ids = consensus.confirmed_method_ids
    try:
        confirmed_plan = tuple(
            by_ref[evaluation_ref]
            for evaluation_ref in confirmed_evaluation_refs
        )
    except KeyError as exc:
        raise ValueError(
            "resolved state references an evaluation outside the Plan"
        ) from exc
    specialist_by_ref = {
        item.evaluation_ref: item for item in specialist.evaluations
    }
    selected_event_refs = tuple(
        specialist_by_ref[item.evaluation_ref].supporting_event_refs
        for item in confirmed_plan
    )
    if reviewer is not None:
        reviewer_by_ref = {
            item.evaluation_ref: item for item in reviewer.evaluations
        }
        if any(
            reviewer_by_ref[item.evaluation_ref].supporting_event_refs != selected
            for item, selected in zip(
                confirmed_plan,
                selected_event_refs,
                strict=True,
            )
        ):
            raise ValueError(
                "resolved role evaluations select different evidence events"
            )
    expected_event_refs = tuple(
        dict.fromkeys(
            event_ref
            for selected in selected_event_refs
            for event_ref in selected
        )
    )
    if consensus is not None and expected_event_refs != consensus.confirmed_event_refs:
        raise ValueError("resolved consensus differs from selected evidence events")

    actual_by_ref = {
        item.evaluation_ref: item for item in validated_result.evaluations
    }
    expected_evaluations: list[MethodConfirmedEvaluationV2] = []
    graph_events = {item.event_ref: item for item in validated_evidence.events}
    for planned, selected in zip(
        confirmed_plan,
        selected_event_refs,
        strict=True,
    ):
        actual = actual_by_ref.get(planned.evaluation_ref)
        if actual is None:
            raise ValueError("resolved result omits a confirmed evaluation")
        if any(event_ref not in planned.evidence_event_refs for event_ref in selected):
            raise ValueError("selected evidence event lies outside its planned evaluation")
        try:
            selected_events = tuple(graph_events[event_ref] for event_ref in selected)
        except KeyError as exc:
            raise ValueError(
                "selected evidence event lies outside the Evidence Graph"
            ) from exc
        if any(event.method_id != planned.method_id for event in selected_events):
            raise ValueError("selected evidence event belongs to another method")
        expected_item_hit_refs = tuple(
            dict.fromkeys(
                hit_ref
                for event in selected_events
                for hit_ref in event.evidence_hit_refs
            )
        )
        expected_evaluations.append(
            MethodConfirmedEvaluationV2(
                evaluation_ref=planned.evaluation_ref,
                method_id=planned.method_id,
                evidence_event_refs=selected,
                evidence_hit_refs=expected_item_hit_refs,
                verdict="CONFIRMED",
            )
        )
    frozen_expected_evaluations = tuple(expected_evaluations)
    expected_hit_refs = tuple(
        dict.fromkeys(
            hit_ref
            for item in frozen_expected_evaluations
            for hit_ref in item.evidence_hit_refs
        )
    )
    if (
        validated_result.evaluations != frozen_expected_evaluations
        or validated_result.confirmed_evaluation_refs
        != confirmed_evaluation_refs
        or validated_result.confirmed_method_ids != confirmed_method_ids
        or validated_result.confirmed_event_refs != expected_event_refs
        or validated_result.confirmed_hit_refs != expected_hit_refs
    ):
        raise ValueError(
            "resolved Methods terminal result differs from its exact consensus evidence"
        )
    return validated_result


def project_method_terminal_result_v2(
    result: MethodTerminalResultV2,
) -> MethodsTerminalProjectionV2:
    """Mechanically remove private role material and bind the source Job."""

    if not isinstance(result, MethodTerminalResultV2):
        raise TypeError("result must be MethodTerminalResultV2")
    validated_result = MethodTerminalResultV2.model_validate(
        result.model_dump(mode="python")
    )
    return MethodsTerminalProjectionV2(
        schema_version=2,
        case_id=validated_result.case_id,
        source_job_id=validated_result.terminal_job_id,
        result_ref=validated_result.result_ref,
        evaluation_id=validated_result.evaluation_id,
        status=validated_result.status,
        plan_ref=validated_result.plan_ref,
        evidence_graph_ref=validated_result.evidence_graph_ref,
        reason_code=validated_result.reason_code,
        diagnostic_id=validated_result.diagnostic_id,
        diagnostic_evaluation_ref=validated_result.diagnostic_evaluation_ref,
        confirmed_evaluation_refs=validated_result.confirmed_evaluation_refs,
        confirmed_method_ids=validated_result.confirmed_method_ids,
        confirmed_event_refs=validated_result.confirmed_event_refs,
        confirmed_hit_refs=validated_result.confirmed_hit_refs,
        limitations=validated_result.limitations,
        reasons=validated_result.reasons,
    )


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
    "method_pre_evaluation_diagnostic_id_v2",
    "method_state_ref_v2",
    "method_terminal_result_ref_v2",
    "project_method_terminal_result_v2",
    "validate_method_terminal_result_v2",
]
