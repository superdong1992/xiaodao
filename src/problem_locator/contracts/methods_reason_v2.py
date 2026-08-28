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
            "Specialist 的输出修复一次后仍不符合结构要求。"
        ),
        "REVIEWER_PROTOCOL_REPAIR_EXHAUSTED": (
            "Reviewer 的输出修复一次后仍不符合结构要求。"
        ),
        "SPECIALIST_SEMANTIC_INVALID": (
            "Specialist 的评估结果不符合评估规则。"
        ),
        "REVIEWER_SEMANTIC_INVALID": (
            "Reviewer 的评估结果不符合评估规则。"
        ),
        "SPECIALIST_MODEL_EXECUTION_FAILED": (
            "Specialist 评估未能完成。"
        ),
        "REVIEWER_MODEL_EXECUTION_FAILED": (
            "Reviewer 评估未能完成。"
        ),
        "SPECIALIST_REVIEWER_DISAGREEMENT": (
            "Specialist 与 Reviewer 的判定不一致。"
        ),
        "INCOMPLETE_EVALUATION": "至少一项评估结果仍为 UNKNOWN。",
        "NO_CONFIRMED_METHOD": "没有任何方法获得双方一致的 CONFIRMED 判定。",
        "NO_MATCHING_METHOD_EVIDENCE": "当前日志中没有匹配任何已加载方法的证据。",
        "RESOURCE_SNAPSHOT_DRIFT": (
            "评估完成前，冻结的资源快照已发生变化。"
        ),
        "SERVER_INVARIANT_VIOLATION": (
            "服务端未能保持 Evidence V2 的评估约束。"
        ),
        "AUDIT_ARCHIVE_FAILED": "评估审计记录未能完整归档。",
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
