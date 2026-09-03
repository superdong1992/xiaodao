"""One-pass server evidence scan and deterministic Methods evaluation planning."""

from __future__ import annotations

import re
from collections.abc import Sequence

from problem_locator.contracts import (
    MethodEvidenceEventV2,
    MethodEvidenceGraphV2,
    MethodEvidenceHitV2,
    MethodEvidenceSourceV2,
    MethodEvaluationPlanItemV2,
    MethodEvaluationPlanV2,
    method_evaluation_plan_ref_v2,
    method_evaluation_ref_v2,
    method_evidence_event_ref_v2,
    method_evidence_graph_ref_v2,
    method_evidence_hit_ref_v2,
    method_evidence_source_ref_v2,
)

from .methods_grounding import FrozenTargetLogV1
from .methods_skill import ResolvedSpecializedSkillV1


def _line_contains_marker(folded_line: str, folded_marker: str) -> bool:
    """Return whether one normalized marker occurs in one normalized log line."""

    return folded_marker in folded_line


def _validated_logs(
    target_logs: Sequence[FrozenTargetLogV1],
) -> tuple[FrozenTargetLogV1, ...]:
    logs = tuple(target_logs)
    if not logs:
        raise ValueError("evidence scan requires at least one frozen target log")
    source_ids: set[str] = set()
    for item in logs:
        if not isinstance(item, FrozenTargetLogV1):
            raise TypeError("target_logs must contain FrozenTargetLogV1")
        if item.source_id in source_ids:
            raise ValueError("target log source ids must be unique")
        source_ids.add(item.source_id)
    return logs


def scan_method_evidence_v2(
    *,
    skill: ResolvedSpecializedSkillV1,
    target_logs: Sequence[FrozenTargetLogV1],
    limitations: Sequence[str] = (),
) -> MethodEvidenceGraphV2:
    """Scan each frozen log once and emit method-qualified evidence hits.

    Case-insensitive matching belongs exclusively to this scan boundary.  The
    original marker and exact frozen line are retained in every hit.
    """

    if not isinstance(skill, ResolvedSpecializedSkillV1):
        raise TypeError("skill must be a resolved specialized Skill")
    logs = _validated_logs(target_logs)
    if isinstance(limitations, (str, bytes)):
        raise TypeError("limitations must be a sequence of strings")
    frozen_limitations = tuple(dict.fromkeys(limitations))
    if any(
        not isinstance(item, str) or not item or item.isspace()
        for item in frozen_limitations
    ):
        raise ValueError("limitations must contain non-blank strings")

    sources = tuple(
        sorted(
            (
                MethodEvidenceSourceV2(
                    source_ref=method_evidence_source_ref_v2(
                        source_id=target.source_id,
                        relative_path=target.relative_path,
                        content_sha256=target.content_sha256,
                    ),
                    source_id=target.source_id,
                    relative_path=target.relative_path,
                    content_sha256=target.content_sha256,
                )
                for target in logs
            ),
            key=lambda item: item.source_id,
        )
    )
    source_ref_by_id = {item.source_id: item.source_ref for item in sources}
    marker_bindings_by_folded_literal: dict[
        str,
        list[tuple[int, str, int, str, bool]],
    ] = {}
    marker_bindings = (
        (
            marker.casefold(),
            (
                method.priority,
                method.id,
                marker_index,
                marker,
                marker in method.activation_markers,
            ),
        )
        for method in sorted(
            skill.methods.methods,
            key=lambda item: (item.priority, item.id),
        )
        for marker_index, marker in enumerate(method.evidence_markers, start=1)
    )
    for folded_marker, binding in marker_bindings:
        marker_bindings_by_folded_literal.setdefault(folded_marker, []).append(binding)
    indexed_markers = tuple(
        (folded_marker, tuple(bindings))
        for folded_marker, bindings in marker_bindings_by_folded_literal.items()
    )

    hits: list[MethodEvidenceHitV2] = []
    activated_method_ids: set[str] = set()
    for target in logs:
        # Decode and split once per source, then walk every line exactly once.
        lines = target.content.decode("utf-8").splitlines()
        source_ref = source_ref_by_id[target.source_id]
        for line_number, line in enumerate(lines, start=1):
            folded_line = line.casefold()
            for folded_marker, bindings in indexed_markers:
                if not _line_contains_marker(folded_line, folded_marker):
                    continue
                for (
                    method_priority,
                    method_id,
                    marker_index,
                    marker,
                    is_activation_marker,
                ) in bindings:
                    hit_ref = method_evidence_hit_ref_v2(
                        method_id=method_id,
                        method_priority=method_priority,
                        marker_index=marker_index,
                        source_ref=source_ref,
                        source_id=target.source_id,
                        line_number=line_number,
                        marker=marker,
                        line=line,
                    )
                    hits.append(
                        MethodEvidenceHitV2(
                            hit_ref=hit_ref,
                            method_id=method_id,
                            method_priority=method_priority,
                            marker_index=marker_index,
                            source_ref=source_ref,
                            source_id=target.source_id,
                            line_number=line_number,
                            marker=marker,
                            line=line,
                        )
                    )
                    if is_activation_marker:
                        activated_method_ids.add(method_id)

    frozen_hits = tuple(
        sorted(
            (
                hit
                for hit in hits
                if hit.method_id in activated_method_ids
            ),
            key=lambda item: (
                item.method_priority,
                item.method_id,
                item.marker_index,
                item.source_id,
                item.line_number,
            ),
        )
    )
    identity_patterns = tuple(
        (
            field,
            re.compile(rf"(?<![A-Za-z0-9_])({re.escape(field)}=[^\s,;]+)"),
        )
        for field in skill.methods.log_derived_fields
    )
    identity_by_line: dict[tuple[str, int], tuple[str, ...]] = {}
    event_groups: dict[
        tuple[str, tuple[str, ...], str | None],
        list[MethodEvidenceHitV2],
    ] = {}
    for hit in frozen_hits:
        line_key = (hit.source_ref, hit.line_number)
        identity_tokens = identity_by_line.get(line_key)
        if identity_tokens is None:
            identity_tokens = tuple(
                match.group(1)
                for _, pattern in identity_patterns
                if (match := pattern.search(hit.line)) is not None
            )
            identity_by_line[line_key] = identity_tokens
        # An unkeyed hit is deliberately its own event.  It must never merge
        # with another line merely because both lack identity.
        unkeyed_hit_ref = None if identity_tokens else hit.hit_ref
        group_key = (hit.method_id, identity_tokens, unkeyed_hit_ref)
        event_groups.setdefault(group_key, []).append(hit)

    events: list[MethodEvidenceEventV2] = []
    for (method_id, identity_tokens, _), event_hits in event_groups.items():
        method_priority = event_hits[0].method_priority
        evidence_hit_refs = tuple(item.hit_ref for item in event_hits)
        event_ref = method_evidence_event_ref_v2(
            method_id=method_id,
            method_priority=method_priority,
            identity_tokens=identity_tokens,
            evidence_hit_refs=evidence_hit_refs,
        )
        events.append(
            MethodEvidenceEventV2(
                event_ref=event_ref,
                method_id=method_id,
                method_priority=method_priority,
                identity_tokens=identity_tokens,
                evidence_hit_refs=evidence_hit_refs,
            )
        )
    frozen_events = tuple(events)
    loaded_method_ids = tuple(
        method.id
        for method in sorted(
            skill.methods.methods,
            key=lambda item: (item.priority, item.id),
        )
        if method.id in activated_method_ids
    )
    graph_ref = method_evidence_graph_ref_v2(
        skill_sha256=skill.combined_sha256,
        sources=sources,
        hits=frozen_hits,
        events=frozen_events,
        loaded_method_ids=loaded_method_ids,
        limitations=frozen_limitations,
    )
    return MethodEvidenceGraphV2(
        graph_ref=graph_ref,
        skill_sha256=skill.combined_sha256,
        sources=sources,
        hits=frozen_hits,
        events=frozen_events,
        loaded_method_ids=loaded_method_ids,
        limitations=frozen_limitations,
    )


def build_method_evaluation_plan_v2(
    *,
    skill: ResolvedSpecializedSkillV1,
    evidence: MethodEvidenceGraphV2,
) -> MethodEvaluationPlanV2:
    """Build one ordered evaluation per loaded method from frozen hit refs.

    This boundary consumes the evidence graph as-is.  It intentionally does not
    inspect line text or match markers a second time.
    """

    if not isinstance(skill, ResolvedSpecializedSkillV1):
        raise TypeError("skill must be a resolved specialized Skill")
    if not isinstance(evidence, MethodEvidenceGraphV2):
        raise TypeError("evidence must be MethodEvidenceGraphV2")
    if evidence.skill_sha256 != skill.combined_sha256:
        raise ValueError("evidence graph belongs to a different resolved Skill")

    methods_by_id = {method.id: method for method in skill.methods.methods}
    known_method_ids = set(methods_by_id)
    hit_method_ids = {item.method_id for item in evidence.hits}
    if hit_method_ids - known_method_ids:
        raise ValueError("evidence graph names an unknown method")
    expected_loaded_method_ids = tuple(
        method.id
        for method in sorted(
            skill.methods.methods,
            key=lambda item: (item.priority, item.id),
        )
        if method.id in hit_method_ids
    )
    if evidence.loaded_method_ids != expected_loaded_method_ids:
        raise ValueError("evidence graph loaded methods are not in business order")
    if any(
        item.method_priority != methods_by_id[item.method_id].priority
        for item in evidence.hits
    ):
        raise ValueError("evidence hit method priority differs from the Skill")
    for item in evidence.hits:
        method_markers = methods_by_id[item.method_id].evidence_markers
        if (
            item.marker_index > len(method_markers)
            or item.marker != method_markers[item.marker_index - 1]
        ):
            raise ValueError("evidence hit marker/index does not belong to its method")
    for method_id in evidence.loaded_method_ids:
        activation_markers = methods_by_id[method_id].activation_markers
        if not any(
            item.method_id == method_id and item.marker in activation_markers
            for item in evidence.hits
        ):
            raise ValueError(
                "evidence graph loaded method has no activation marker hit"
            )

    evaluations: list[MethodEvaluationPlanItemV2] = []
    for method_id in evidence.loaded_method_ids:
        method_priority = methods_by_id[method_id].priority
        evidence_event_refs = tuple(
            item.event_ref for item in evidence.events if item.method_id == method_id
        )
        evidence_hit_refs = tuple(
            item.hit_ref for item in evidence.hits if item.method_id == method_id
        )
        evaluation_ref = method_evaluation_ref_v2(
            method_id=method_id,
            method_priority=method_priority,
            evidence_event_refs=evidence_event_refs,
            evidence_hit_refs=evidence_hit_refs,
        )
        evaluations.append(
            MethodEvaluationPlanItemV2(
                evaluation_ref=evaluation_ref,
                method_id=method_id,
                method_priority=method_priority,
                evidence_event_refs=evidence_event_refs,
                evidence_hit_refs=evidence_hit_refs,
            )
        )

    frozen_evaluations = tuple(evaluations)
    plan_ref = method_evaluation_plan_ref_v2(
        skill_sha256=skill.combined_sha256,
        evidence_graph_ref=evidence.graph_ref,
        evaluations=frozen_evaluations,
    )
    plan = MethodEvaluationPlanV2(
        plan_ref=plan_ref,
        skill_sha256=skill.combined_sha256,
        evidence_graph_ref=evidence.graph_ref,
        evaluations=frozen_evaluations,
    )
    validate_method_evaluation_plan_v2(evidence=evidence, plan=plan)
    return plan


def validate_method_evaluation_plan_v2(
    *,
    evidence: MethodEvidenceGraphV2,
    plan: MethodEvaluationPlanV2,
) -> None:
    """Mechanically bind a complete plan to its graph without marker matching."""

    if not isinstance(evidence, MethodEvidenceGraphV2):
        raise TypeError("evidence must be MethodEvidenceGraphV2")
    if not isinstance(plan, MethodEvaluationPlanV2):
        raise TypeError("plan must be MethodEvaluationPlanV2")
    if plan.skill_sha256 != evidence.skill_sha256:
        raise ValueError("evaluation plan belongs to a different Skill")
    if plan.evidence_graph_ref != evidence.graph_ref:
        raise ValueError("evaluation plan belongs to a different evidence graph")

    planned_method_ids = tuple(item.method_id for item in plan.evaluations)
    if planned_method_ids != evidence.loaded_method_ids:
        raise ValueError("evaluation plan must contain each loaded method exactly once")
    all_planned_hit_refs: list[str] = []
    all_planned_event_refs: list[str] = []
    for item in plan.evaluations:
        method_events = tuple(
            event for event in evidence.events if event.method_id == item.method_id
        )
        method_hits = tuple(
            hit for hit in evidence.hits if hit.method_id == item.method_id
        )
        expected_hit_refs = tuple(hit.hit_ref for hit in method_hits)
        expected_event_refs = tuple(event.event_ref for event in method_events)
        if item.evidence_event_refs != expected_event_refs:
            raise ValueError(
                "evaluation plan must reference every and only its method evidence events"
            )
        if item.evidence_hit_refs != expected_hit_refs:
            raise ValueError(
                "evaluation plan must reference every and only its method evidence hits"
            )
        if any(hit.method_priority != item.method_priority for hit in method_hits):
            raise ValueError("evaluation priority differs from its evidence hits")
        if any(event.method_priority != item.method_priority for event in method_events):
            raise ValueError("evaluation priority differs from its evidence events")
        all_planned_event_refs.extend(item.evidence_event_refs)
        all_planned_hit_refs.extend(item.evidence_hit_refs)
    if tuple(all_planned_event_refs) != tuple(event.event_ref for event in evidence.events):
        raise ValueError("evaluation plan does not exactly cover the evidence events")
    if tuple(all_planned_hit_refs) != tuple(hit.hit_ref for hit in evidence.hits):
        raise ValueError("evaluation plan does not exactly partition the evidence graph")


__all__ = [
    "build_method_evaluation_plan_v2",
    "scan_method_evidence_v2",
    "validate_method_evaluation_plan_v2",
]
