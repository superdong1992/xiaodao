"""Small, immutable contracts for the Evidence V2 Methods core.

These DTOs describe business data only.  They deliberately do not carry
execution authority, transport metadata, or persistence/audit concerns.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import ConfigDict, StrictBool, StringConstraints, model_validator

from .models import (
    ContractModel,
    NonEmptyText,
    OpaqueId,
    PositiveInt,
    RelativePosixPath,
    Sha256,
)
from .serialization import canonical_json_sha256


MethodIdV2: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        strict=True,
    ),
]
MethodEvidenceSourceIdV2: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*$",
        strict=True,
    ),
]
MethodEvidenceTextV2: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, strict=True),
]
MethodEvidenceIdentityTokenV2: TypeAlias = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*=[^\s,;]+$",
        strict=True,
    ),
]
MethodEvidenceSourceRefV2: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^source-[0-9a-f]{64}$", strict=True),
]
MethodEvidenceHitRefV2: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^hit-[0-9a-f]{64}$", strict=True),
]
MethodEvidenceGraphRefV2: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^graph-[0-9a-f]{64}$", strict=True),
]
MethodEvidenceEventRefV2: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^event-[0-9a-f]{64}$", strict=True),
]
MethodEvaluationRefV2: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^eval-[0-9a-f]{64}$", strict=True),
]
MethodEvaluationPlanRefV2: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^plan-[0-9a-f]{64}$", strict=True),
]
MethodLimitationsRecordRefV2: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^limitations-[0-9a-f]{64}$", strict=True),
]
MethodEvaluationRoleV2: TypeAlias = Literal["SPECIALIST", "REVIEWER"]
MethodEvaluationVerdictV2: TypeAlias = Literal["CONFIRMED", "REJECTED", "UNKNOWN"]
MethodConsensusStatusV2: TypeAlias = Literal["RESOLVED", "UNRESOLVED"]


class _MethodsV2Contract(ContractModel):
    model_config = ConfigDict(frozen=True)


def method_evidence_source_ref_v2(
    *,
    source_id: str,
    relative_path: str,
    content_sha256: str,
) -> str:
    return "source-" + canonical_json_sha256(
        {
            "kind": "method-evidence-source-v2",
            "source_id": source_id,
            "relative_path": relative_path,
            "content_sha256": content_sha256,
        }
    )


class MethodEvidenceSourceV2(_MethodsV2Contract):
    source_ref: MethodEvidenceSourceRefV2
    source_id: MethodEvidenceSourceIdV2
    relative_path: RelativePosixPath
    content_sha256: Sha256

    @model_validator(mode="after")
    def validate_source_ref(self) -> "MethodEvidenceSourceV2":
        expected = method_evidence_source_ref_v2(
            source_id=self.source_id,
            relative_path=self.relative_path,
            content_sha256=self.content_sha256,
        )
        if self.source_ref != expected:
            raise ValueError("source_ref does not match the evidence source")
        return self


def method_evidence_hit_ref_v2(
    *,
    method_id: str,
    method_priority: int,
    marker_index: int,
    source_ref: str,
    source_id: str,
    line_number: int,
    marker: str,
    line: str,
) -> str:
    return "hit-" + canonical_json_sha256(
        {
            "kind": "method-evidence-hit-v2",
            "method_id": method_id,
            "method_priority": method_priority,
            "marker_index": marker_index,
            "source_ref": source_ref,
            "source_id": source_id,
            "line_number": line_number,
            "marker": marker,
            "line": line,
        }
    )


class MethodEvidenceHitV2(_MethodsV2Contract):
    hit_ref: MethodEvidenceHitRefV2
    method_id: MethodIdV2
    method_priority: PositiveInt
    marker_index: PositiveInt
    source_ref: MethodEvidenceSourceRefV2
    source_id: MethodEvidenceSourceIdV2
    line_number: PositiveInt
    marker: MethodEvidenceTextV2
    line: MethodEvidenceTextV2

    @model_validator(mode="after")
    def validate_hit_ref(self) -> "MethodEvidenceHitV2":
        expected = method_evidence_hit_ref_v2(
            method_id=self.method_id,
            method_priority=self.method_priority,
            marker_index=self.marker_index,
            source_ref=self.source_ref,
            source_id=self.source_id,
            line_number=self.line_number,
            marker=self.marker,
            line=self.line,
        )
        if self.hit_ref != expected:
            raise ValueError("hit_ref does not match the method-qualified evidence hit")
        return self


def method_evidence_event_ref_v2(
    *,
    method_id: str,
    method_priority: int,
    identity_tokens: tuple[str, ...],
    evidence_hit_refs: tuple[str, ...],
) -> str:
    return "event-" + canonical_json_sha256(
        {
            "kind": "method-evidence-event-v2",
            "method_id": method_id,
            "method_priority": method_priority,
            "identity_tokens": list(identity_tokens),
            "evidence_hit_refs": list(evidence_hit_refs),
        }
    )


class MethodEvidenceEventV2(_MethodsV2Contract):
    event_ref: MethodEvidenceEventRefV2
    method_id: MethodIdV2
    method_priority: PositiveInt
    identity_tokens: tuple[MethodEvidenceIdentityTokenV2, ...]
    evidence_hit_refs: tuple[MethodEvidenceHitRefV2, ...]

    @model_validator(mode="after")
    def validate_event_ref(self) -> "MethodEvidenceEventV2":
        if len(self.identity_tokens) != len(set(self.identity_tokens)):
            raise ValueError("event identity_tokens must be unique")
        identity_names = [item.split("=", 1)[0] for item in self.identity_tokens]
        if len(identity_names) != len(set(identity_names)):
            raise ValueError("an event may contain only one value for each identity field")
        if not self.evidence_hit_refs or len(self.evidence_hit_refs) != len(
            set(self.evidence_hit_refs)
        ):
            raise ValueError("an event must reference unique non-empty evidence hits")
        expected = method_evidence_event_ref_v2(
            method_id=self.method_id,
            method_priority=self.method_priority,
            identity_tokens=self.identity_tokens,
            evidence_hit_refs=self.evidence_hit_refs,
        )
        if self.event_ref != expected:
            raise ValueError("event_ref does not match the evidence event")
        return self


def method_evidence_graph_ref_v2(
    *,
    skill_sha256: str,
    sources: tuple[MethodEvidenceSourceV2, ...],
    hits: tuple[MethodEvidenceHitV2, ...],
    events: tuple[MethodEvidenceEventV2, ...],
    loaded_method_ids: tuple[str, ...],
    limitations: tuple[str, ...] = (),
) -> str:
    return "graph-" + canonical_json_sha256(
        {
            "kind": "method-evidence-graph-v2",
            "skill_sha256": skill_sha256,
            "source_refs": [item.source_ref for item in sources],
            "hit_refs": [item.hit_ref for item in hits],
            "event_refs": [item.event_ref for item in events],
            "loaded_method_ids": list(loaded_method_ids),
            "limitations": list(limitations),
        }
    )


class MethodEvidenceGraphV2(_MethodsV2Contract):
    graph_ref: MethodEvidenceGraphRefV2
    skill_sha256: Sha256
    sources: tuple[MethodEvidenceSourceV2, ...]
    hits: tuple[MethodEvidenceHitV2, ...]
    events: tuple[MethodEvidenceEventV2, ...]
    loaded_method_ids: tuple[MethodIdV2, ...]
    limitations: tuple[NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def validate_graph(self) -> "MethodEvidenceGraphV2":
        source_refs = [item.source_ref for item in self.sources]
        source_ids = [item.source_id for item in self.sources]
        if len(source_refs) != len(set(source_refs)) or len(source_ids) != len(set(source_ids)):
            raise ValueError("evidence graph sources must be unique")
        hit_refs = [item.hit_ref for item in self.hits]
        if len(hit_refs) != len(set(hit_refs)):
            raise ValueError("evidence graph hits must be unique")
        sources_by_ref = {item.source_ref: item for item in self.sources}
        if any(item.source_ref not in sources_by_ref for item in self.hits):
            raise ValueError("every evidence hit must reference a graph source")
        if any(
            item.source_id != sources_by_ref[item.source_ref].source_id
            for item in self.hits
        ):
            raise ValueError("evidence hit source_id must match its source_ref")
        expected_hit_order = tuple(
            sorted(
                self.hits,
                key=lambda item: (
                    item.method_priority,
                    item.method_id,
                    item.marker_index,
                    item.source_id,
                    item.line_number,
                ),
            )
        )
        if self.hits != expected_hit_order:
            raise ValueError("evidence hits are not in deterministic business order")
        events_by_ref = {item.event_ref: item for item in self.events}
        if len(events_by_ref) != len(self.events):
            raise ValueError("evidence graph events must be unique")
        hits_by_ref = {item.hit_ref: item for item in self.hits}
        partitioned_hit_refs: list[str] = []
        keyed_event_keys: set[tuple[str, tuple[str, ...]]] = set()
        for event in self.events:
            if event.identity_tokens:
                event_key = (event.method_id, event.identity_tokens)
                if event_key in keyed_event_keys:
                    raise ValueError("one method identity may only produce one event")
                keyed_event_keys.add(event_key)
            elif len(event.evidence_hit_refs) != 1:
                raise ValueError("an unkeyed evidence hit must be its own event")
            event_hits = [hits_by_ref.get(item) for item in event.evidence_hit_refs]
            if any(item is None for item in event_hits):
                raise ValueError("every event hit must belong to the evidence graph")
            if any(
                item.method_id != event.method_id
                or item.method_priority != event.method_priority
                for item in event_hits
                if item is not None
            ):
                raise ValueError("an event may only reference hits from its method")
            partitioned_hit_refs.extend(event.evidence_hit_refs)
        if sorted(partitioned_hit_refs) != sorted(hits_by_ref):
            raise ValueError("evidence events must exactly partition all graph hits")
        hit_order = {item.hit_ref: index for index, item in enumerate(self.hits)}
        expected_event_order = tuple(
            sorted(
                self.events,
                key=lambda item: (
                    item.method_priority,
                    item.method_id,
                    min(hit_order[ref] for ref in item.evidence_hit_refs),
                ),
            )
        )
        if self.events != expected_event_order:
            raise ValueError("evidence events are not in deterministic business order")
        if len(self.loaded_method_ids) != len(set(self.loaded_method_ids)):
            raise ValueError("loaded_method_ids must be unique")
        if len(self.limitations) != len(set(self.limitations)):
            raise ValueError("evidence graph limitations must be unique")
        if set(self.loaded_method_ids) != {item.method_id for item in self.hits}:
            raise ValueError("loaded_method_ids must exactly cover methods with evidence hits")
        priority_by_method: dict[str, int] = {}
        for item in self.hits:
            previous = priority_by_method.setdefault(item.method_id, item.method_priority)
            if previous != item.method_priority:
                raise ValueError("one method must have one stable priority")
        expected_loaded_method_ids = tuple(
            method_id
            for method_id, _ in sorted(
                priority_by_method.items(),
                key=lambda item: (item[1], item[0]),
            )
        )
        if self.loaded_method_ids != expected_loaded_method_ids:
            raise ValueError("loaded_method_ids are not in priority and method-id order")
        expected = method_evidence_graph_ref_v2(
            skill_sha256=self.skill_sha256,
            sources=self.sources,
            hits=self.hits,
            events=self.events,
            loaded_method_ids=self.loaded_method_ids,
            limitations=self.limitations,
        )
        if self.graph_ref != expected:
            raise ValueError("graph_ref does not match the evidence graph")
        return self


def method_evaluation_ref_v2(
    *,
    method_id: str,
    method_priority: int,
    evidence_event_refs: tuple[str, ...],
    evidence_hit_refs: tuple[str, ...],
) -> str:
    return "eval-" + canonical_json_sha256(
        {
            "kind": "method-evaluation-v2",
            "method_id": method_id,
            "method_priority": method_priority,
            "evidence_event_refs": list(evidence_event_refs),
            "evidence_hit_refs": list(evidence_hit_refs),
        }
    )


class MethodEvaluationPlanItemV2(_MethodsV2Contract):
    evaluation_ref: MethodEvaluationRefV2
    method_id: MethodIdV2
    method_priority: PositiveInt
    evidence_event_refs: tuple[MethodEvidenceEventRefV2, ...]
    evidence_hit_refs: tuple[MethodEvidenceHitRefV2, ...]

    @model_validator(mode="after")
    def validate_evaluation_ref(self) -> "MethodEvaluationPlanItemV2":
        if not self.evidence_event_refs or len(self.evidence_event_refs) != len(
            set(self.evidence_event_refs)
        ):
            raise ValueError("an evaluation must reference unique non-empty evidence events")
        if not self.evidence_hit_refs or len(self.evidence_hit_refs) != len(
            set(self.evidence_hit_refs)
        ):
            raise ValueError("an evaluation must reference unique non-empty evidence hits")
        expected = method_evaluation_ref_v2(
            method_id=self.method_id,
            method_priority=self.method_priority,
            evidence_event_refs=self.evidence_event_refs,
            evidence_hit_refs=self.evidence_hit_refs,
        )
        if self.evaluation_ref != expected:
            raise ValueError("evaluation_ref does not match the evaluation item")
        return self


def method_evaluation_plan_ref_v2(
    *,
    skill_sha256: str,
    evidence_graph_ref: str,
    evaluations: tuple[MethodEvaluationPlanItemV2, ...],
) -> str:
    return "plan-" + canonical_json_sha256(
        {
            "kind": "method-evaluation-plan-v2",
            "skill_sha256": skill_sha256,
            "evidence_graph_ref": evidence_graph_ref,
            "evaluation_refs": [item.evaluation_ref for item in evaluations],
        }
    )


class MethodEvaluationPlanV2(_MethodsV2Contract):
    plan_ref: MethodEvaluationPlanRefV2
    skill_sha256: Sha256
    evidence_graph_ref: MethodEvidenceGraphRefV2
    evaluations: tuple[MethodEvaluationPlanItemV2, ...]

    @model_validator(mode="after")
    def validate_plan(self) -> "MethodEvaluationPlanV2":
        evaluation_refs = [item.evaluation_ref for item in self.evaluations]
        method_ids = [item.method_id for item in self.evaluations]
        if len(evaluation_refs) != len(set(evaluation_refs)) or len(method_ids) != len(
            set(method_ids)
        ):
            raise ValueError("evaluation plan items must be unique")
        expected_order = tuple(
            sorted(
                self.evaluations,
                key=lambda item: (item.method_priority, item.method_id),
            )
        )
        if self.evaluations != expected_order:
            raise ValueError("evaluation plan is not in priority and method-id order")
        expected = method_evaluation_plan_ref_v2(
            skill_sha256=self.skill_sha256,
            evidence_graph_ref=self.evidence_graph_ref,
            evaluations=self.evaluations,
        )
        if self.plan_ref != expected:
            raise ValueError("plan_ref does not match the evaluation plan")
        return self


def method_limitations_record_ref_v2(
    *,
    case_id: str,
    source_job_id: str,
    evidence_graph_ref: str,
    plan_ref: str,
    limitations: tuple[str, ...],
) -> str:
    return "limitations-" + canonical_json_sha256(
        {
            "kind": "method-limitations-record-v2",
            "case_id": case_id,
            "source_job_id": source_job_id,
            "evidence_graph_ref": evidence_graph_ref,
            "plan_ref": plan_ref,
            "limitations": list(limitations),
        }
    )


class MethodLimitationsRecordV2(_MethodsV2Contract):
    """Server-owned limitations shared by both role Jobs."""

    schema_version: Literal[2]
    record_ref: MethodLimitationsRecordRefV2
    case_id: OpaqueId
    source_job_id: OpaqueId
    evidence_graph_ref: MethodEvidenceGraphRefV2
    plan_ref: MethodEvaluationPlanRefV2
    limitations: tuple[NonEmptyText, ...]

    @model_validator(mode="after")
    def validate_record(self) -> "MethodLimitationsRecordV2":
        if len(self.limitations) != len(set(self.limitations)):
            raise ValueError("method limitations must be unique")
        expected = method_limitations_record_ref_v2(
            case_id=self.case_id,
            source_job_id=self.source_job_id,
            evidence_graph_ref=self.evidence_graph_ref,
            plan_ref=self.plan_ref,
            limitations=self.limitations,
        )
        if self.record_ref != expected:
            raise ValueError("record_ref does not match the method limitations")
        return self


class MethodEvaluationOutputItemV2(_MethodsV2Contract):
    """The exact object shape accepted from one model evaluation response."""

    evaluation_ref: MethodEvaluationRefV2
    verdict: MethodEvaluationVerdictV2
    reason: NonEmptyText


class MethodRoleEvaluationV2(_MethodsV2Contract):
    role: MethodEvaluationRoleV2
    plan_ref: MethodEvaluationPlanRefV2
    evaluations: tuple[MethodEvaluationOutputItemV2, ...]
    repair_used: StrictBool


class MethodConsensusV2(_MethodsV2Contract):
    plan_ref: MethodEvaluationPlanRefV2
    status: MethodConsensusStatusV2
    confirmed_evaluation_refs: tuple[MethodEvaluationRefV2, ...]
    confirmed_method_ids: tuple[MethodIdV2, ...]

    @model_validator(mode="after")
    def validate_confirmed_mapping(self) -> "MethodConsensusV2":
        if len(self.confirmed_evaluation_refs) != len(self.confirmed_method_ids):
            raise ValueError("confirmed evaluation and method refs must be aligned")
        if len(self.confirmed_evaluation_refs) != len(set(self.confirmed_evaluation_refs)):
            raise ValueError("confirmed evaluation refs must be unique")
        if len(self.confirmed_method_ids) != len(set(self.confirmed_method_ids)):
            raise ValueError("confirmed method ids must be unique")
        if self.status == "UNRESOLVED" and (
            self.confirmed_evaluation_refs or self.confirmed_method_ids
        ):
            raise ValueError("an unresolved consensus must not publish confirmed methods")
        if self.status == "RESOLVED" and not self.confirmed_evaluation_refs:
            raise ValueError("a resolved consensus requires at least one confirmed method")
        return self


__all__ = [
    "MethodConsensusStatusV2",
    "MethodConsensusV2",
    "MethodEvidenceGraphV2",
    "MethodEvidenceGraphRefV2",
    "MethodEvidenceEventRefV2",
    "MethodEvidenceEventV2",
    "MethodEvidenceHitV2",
    "MethodEvidenceHitRefV2",
    "MethodEvidenceIdentityTokenV2",
    "MethodEvidenceSourceIdV2",
    "MethodEvidenceSourceRefV2",
    "MethodEvidenceSourceV2",
    "MethodEvaluationOutputItemV2",
    "MethodEvaluationPlanItemV2",
    "MethodEvaluationPlanRefV2",
    "MethodEvaluationPlanV2",
    "MethodEvaluationRefV2",
    "MethodEvaluationRoleV2",
    "MethodEvaluationVerdictV2",
    "MethodIdV2",
    "MethodLimitationsRecordRefV2",
    "MethodLimitationsRecordV2",
    "MethodRoleEvaluationV2",
    "method_evaluation_plan_ref_v2",
    "method_evaluation_ref_v2",
    "method_evidence_graph_ref_v2",
    "method_evidence_event_ref_v2",
    "method_evidence_hit_ref_v2",
    "method_evidence_source_ref_v2",
    "method_limitations_record_ref_v2",
]
