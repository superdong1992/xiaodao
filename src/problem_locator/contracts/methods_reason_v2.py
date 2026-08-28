"""Single-source Evidence V2 terminal reason vocabulary and public text."""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal, Mapping, TypeAlias


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
MethodsTerminalReasonCodeV2: TypeAlias = MethodStateReasonCodeV2

SPECIALIST_UNRESOLVED_REASON_CODES_V2 = frozenset(
    {
        "SPECIALIST_PROTOCOL_REPAIR_EXHAUSTED",
        "SPECIALIST_SEMANTIC_INVALID",
        "SPECIALIST_MODEL_EXECUTION_FAILED",
        "NO_MATCHING_METHOD_EVIDENCE",
    }
)
REVIEWER_UNRESOLVED_REASON_CODES_V2 = frozenset(
    {
        "REVIEWER_PROTOCOL_REPAIR_EXHAUSTED",
        "REVIEWER_SEMANTIC_INVALID",
        "REVIEWER_MODEL_EXECUTION_FAILED",
    }
)
CONSENSUS_UNRESOLVED_REASON_CODES_V2 = frozenset(
    {
        "SPECIALIST_REVIEWER_DISAGREEMENT",
        "INCOMPLETE_EVALUATION",
        "NO_CONFIRMED_METHOD",
    }
)
UNRESOLVED_METHOD_REASON_CODES_V2 = frozenset(
    {
        *SPECIALIST_UNRESOLVED_REASON_CODES_V2,
        *REVIEWER_UNRESOLVED_REASON_CODES_V2,
        *CONSENSUS_UNRESOLVED_REASON_CODES_V2,
    }
)
FAILED_METHOD_REASON_CODES_V2 = frozenset(
    {
        "RESOURCE_SNAPSHOT_DRIFT",
        "SERVER_INVARIANT_VIOLATION",
        "AUDIT_ARCHIVE_FAILED",
    }
)

METHOD_PUBLIC_REASON_TEXT_V2: Mapping[str, str] = MappingProxyType(
    {
        "SPECIALIST_PROTOCOL_REPAIR_EXHAUSTED": (
            "The Specialist response remained structurally invalid after one repair."
        ),
        "REVIEWER_PROTOCOL_REPAIR_EXHAUSTED": (
            "The Reviewer response remained structurally invalid after one repair."
        ),
        "SPECIALIST_SEMANTIC_INVALID": (
            "The Specialist evaluation did not satisfy the evaluation contract."
        ),
        "REVIEWER_SEMANTIC_INVALID": (
            "The Reviewer evaluation did not satisfy the evaluation contract."
        ),
        "SPECIALIST_MODEL_EXECUTION_FAILED": (
            "The Specialist evaluation could not be completed."
        ),
        "REVIEWER_MODEL_EXECUTION_FAILED": (
            "The Reviewer evaluation could not be completed."
        ),
        "SPECIALIST_REVIEWER_DISAGREEMENT": (
            "Specialist and Reviewer verdicts disagree."
        ),
        "INCOMPLETE_EVALUATION": "At least one evaluation remains UNKNOWN.",
        "NO_CONFIRMED_METHOD": "No method received a CONFIRMED consensus verdict.",
        "NO_MATCHING_METHOD_EVIDENCE": "No loaded method has matching evidence.",
        "RESOURCE_SNAPSHOT_DRIFT": (
            "The frozen resource snapshot changed before evaluation completed."
        ),
        "SERVER_INVARIANT_VIOLATION": (
            "The server could not preserve the Evidence V2 evaluation invariant."
        ),
        "AUDIT_ARCHIVE_FAILED": "The evaluation audit archive could not be completed.",
    }
)


__all__ = [
    "CONSENSUS_UNRESOLVED_REASON_CODES_V2",
    "FAILED_METHOD_REASON_CODES_V2",
    "METHOD_PUBLIC_REASON_TEXT_V2",
    "MethodStateReasonCodeV2",
    "MethodsTerminalReasonCodeV2",
    "REVIEWER_UNRESOLVED_REASON_CODES_V2",
    "SPECIALIST_UNRESOLVED_REASON_CODES_V2",
    "UNRESOLVED_METHOD_REASON_CODES_V2",
]
