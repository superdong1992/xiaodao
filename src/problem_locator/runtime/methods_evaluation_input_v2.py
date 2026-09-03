"""Compact, lossless model input for one Evidence V2 evaluation plan.

The Evidence Graph and Evaluation Plan remain the authoritative audit records.
This module only projects their model-relevant content so a physical log line
and a declared marker literal are not copied into every method-qualified hit.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from problem_locator.contracts.methods_v2 import (
    MethodEvidenceEventRefV2,
    MethodEvidenceGraphRefV2,
    MethodEvidenceGraphV2,
    MethodEvidenceIdentityTokenV2,
    MethodEvidenceSourceIdV2,
    MethodEvidenceTextV2,
    MethodEvaluationPlanRefV2,
    MethodEvaluationPlanV2,
    MethodEvaluationRefV2,
    MethodIdV2,
)
from problem_locator.contracts.models import RelativePosixPath

from .methods_evidence_v2 import validate_method_evaluation_plan_v2


_PositiveIndex = Annotated[int, Field(gt=0, strict=True)]


class _FrozenEvaluationInputModel(BaseModel):
    """Strict scalar values with immutable nested model containers."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=False,
        str_strip_whitespace=False,
    )


class MethodEvaluationObservationV2(_FrozenEvaluationInputModel):
    """One physical frozen log line, emitted exactly once."""

    id: _PositiveIndex
    source_id: MethodEvidenceSourceIdV2
    line_number: _PositiveIndex
    line: MethodEvidenceTextV2


class MethodEvaluationSourceV2(_FrozenEvaluationInputModel):
    """One frozen target source, including sources with no matching line."""

    id: _PositiveIndex
    source_id: MethodEvidenceSourceIdV2
    relative_path: RelativePosixPath


class MethodEvaluationMarkerV2(_FrozenEvaluationInputModel):
    """One exact declared marker literal shared by all matching methods."""

    id: _PositiveIndex
    literal: MethodEvidenceTextV2


class MethodEvaluationMatchV2(_FrozenEvaluationInputModel):
    """A compact replacement for one method-qualified Evidence Graph hit."""

    observation_id: _PositiveIndex
    marker_id: _PositiveIndex
    method_marker_index: _PositiveIndex


class MethodEvaluationEventInputV2(_FrozenEvaluationInputModel):
    """One server-issued event and every hit relation assigned to it."""

    event_ref: MethodEvidenceEventRefV2
    identity_tokens: tuple[MethodEvidenceIdentityTokenV2, ...]
    matches: tuple[MethodEvaluationMatchV2, ...]

    @model_validator(mode="after")
    def validate_event(self) -> "MethodEvaluationEventInputV2":
        if not self.matches:
            raise ValueError("a compact evidence event requires at least one match")
        if len(self.matches) != len(set(self.matches)):
            raise ValueError("compact evidence event matches must be unique")
        if len(self.identity_tokens) != len(set(self.identity_tokens)):
            raise ValueError("compact evidence event identity tokens must be unique")
        identity_names = [item.split("=", 1)[0] for item in self.identity_tokens]
        if len(identity_names) != len(set(identity_names)):
            raise ValueError(
                "a compact evidence event may contain one value per identity field"
            )
        return self


class MethodEvaluationItemInputV2(_FrozenEvaluationInputModel):
    """All model-visible evidence for one method-qualified evaluation."""

    evaluation_ref: MethodEvaluationRefV2
    method_id: MethodIdV2
    method_priority: _PositiveIndex
    events: tuple[MethodEvaluationEventInputV2, ...]

    @model_validator(mode="after")
    def validate_events(self) -> "MethodEvaluationItemInputV2":
        if not self.events:
            raise ValueError("a compact method evaluation requires evidence events")
        event_refs = tuple(item.event_ref for item in self.events)
        if len(event_refs) != len(set(event_refs)):
            raise ValueError("compact method evaluation event refs must be unique")
        return self


class MethodEvaluationInputV2(_FrozenEvaluationInputModel):
    """Canonical model projection bound to one authoritative Graph and Plan."""

    schema_version: Literal[2]
    evidence_graph_ref: MethodEvidenceGraphRefV2
    plan_ref: MethodEvaluationPlanRefV2
    limitations: tuple[MethodEvidenceTextV2, ...]
    sources: tuple[MethodEvaluationSourceV2, ...]
    observations: tuple[MethodEvaluationObservationV2, ...]
    markers: tuple[MethodEvaluationMarkerV2, ...]
    evaluations: tuple[MethodEvaluationItemInputV2, ...]

    @model_validator(mode="after")
    def validate_projection_shape(self) -> "MethodEvaluationInputV2":
        if len(self.limitations) != len(set(self.limitations)):
            raise ValueError("compact evaluation limitations must be unique")

        source_ids = tuple(item.id for item in self.sources)
        if not source_ids:
            raise ValueError("compact evaluation input requires frozen sources")
        if source_ids != tuple(range(1, len(self.sources) + 1)):
            raise ValueError("compact sources must use consecutive ids from one")
        source_order = tuple(
            (item.source_id, item.relative_path) for item in self.sources
        )
        if source_order != tuple(sorted(source_order)):
            raise ValueError("compact sources are not in deterministic order")
        source_names = tuple(item.source_id for item in self.sources)
        if len(source_names) != len(set(source_names)):
            raise ValueError("compact source ids must be unique")

        observation_ids = tuple(item.id for item in self.observations)
        if observation_ids != tuple(range(1, len(self.observations) + 1)):
            raise ValueError("compact observations must use consecutive ids from one")
        observation_order = tuple(
            (item.source_id, item.line_number, item.line) for item in self.observations
        )
        if observation_order != tuple(sorted(observation_order)):
            raise ValueError("compact observations are not in deterministic order")
        if len(observation_order) != len(set(observation_order)):
            raise ValueError("compact observations must be unique")
        physical_lines = tuple(
            (item.source_id, item.line_number) for item in self.observations
        )
        if len(physical_lines) != len(set(physical_lines)):
            raise ValueError("each physical source line must appear exactly once")
        if not {item.source_id for item in self.observations}.issubset(
            set(source_names)
        ):
            raise ValueError("compact observations must name a frozen source")

        marker_ids = tuple(item.id for item in self.markers)
        if marker_ids != tuple(range(1, len(self.markers) + 1)):
            raise ValueError("compact markers must use consecutive ids from one")
        marker_literals = tuple(item.literal for item in self.markers)
        if marker_literals != tuple(sorted(marker_literals)):
            raise ValueError("compact markers are not in deterministic order")
        if len(marker_literals) != len(set(marker_literals)):
            raise ValueError("compact marker literals must be unique")

        evaluation_keys = tuple(
            (item.method_priority, item.method_id) for item in self.evaluations
        )
        if evaluation_keys != tuple(sorted(evaluation_keys)):
            raise ValueError("compact evaluations are not in deterministic order")
        evaluation_refs = tuple(item.evaluation_ref for item in self.evaluations)
        method_ids = tuple(item.method_id for item in self.evaluations)
        if len(evaluation_refs) != len(set(evaluation_refs)) or len(method_ids) != len(
            set(method_ids)
        ):
            raise ValueError("compact evaluations must be unique")

        known_observation_ids = set(observation_ids)
        known_marker_ids = set(marker_ids)
        used_observation_ids: set[int] = set()
        used_marker_ids: set[int] = set()
        qualified_matches: set[tuple[str, int, int, int]] = set()
        event_refs: list[str] = []
        for evaluation in self.evaluations:
            for event in evaluation.events:
                event_refs.append(event.event_ref)
                for match in event.matches:
                    if match.observation_id not in known_observation_ids:
                        raise ValueError("compact match names an unknown observation")
                    if match.marker_id not in known_marker_ids:
                        raise ValueError("compact match names an unknown marker")
                    qualified_match = (
                        evaluation.method_id,
                        match.observation_id,
                        match.marker_id,
                        match.method_marker_index,
                    )
                    if qualified_match in qualified_matches:
                        raise ValueError(
                            "compact method-qualified matches must be globally unique"
                        )
                    qualified_matches.add(qualified_match)
                    used_observation_ids.add(match.observation_id)
                    used_marker_ids.add(match.marker_id)
        if len(event_refs) != len(set(event_refs)):
            raise ValueError("compact event refs must be globally unique")
        if used_observation_ids != known_observation_ids:
            raise ValueError("compact observations must exactly cover evidence matches")
        if used_marker_ids != known_marker_ids:
            raise ValueError("compact markers must exactly cover evidence matches")
        if bool(self.evaluations) != bool(self.observations) or bool(
            self.evaluations
        ) != bool(self.markers):
            raise ValueError(
                "compact catalogs and evaluations must be empty or populated together"
            )
        return self


def _project_method_evaluation_input_v2(
    *,
    evidence: MethodEvidenceGraphV2,
    plan: MethodEvaluationPlanV2,
) -> MethodEvaluationInputV2:
    sources = tuple(
        MethodEvaluationSourceV2(
            id=index,
            source_id=source.source_id,
            relative_path=source.relative_path,
        )
        for index, source in enumerate(
            sorted(
                evidence.sources,
                key=lambda item: (item.source_id, item.relative_path),
            ),
            start=1,
        )
    )
    line_by_physical_key: dict[tuple[str, str, int], str] = {}
    for hit in evidence.hits:
        physical_key = (hit.source_ref, hit.source_id, hit.line_number)
        previous = line_by_physical_key.setdefault(physical_key, hit.line)
        if previous != hit.line:
            raise ValueError("one physical source line has conflicting evidence text")
    observation_keys = tuple(
        (*physical_key, line)
        for physical_key, line in sorted(
            line_by_physical_key.items(),
            key=lambda item: (item[0][1], item[0][2], item[1], item[0][0]),
        )
    )
    observation_id_by_key = {
        key: index for index, key in enumerate(observation_keys, start=1)
    }
    observations = tuple(
        MethodEvaluationObservationV2(
            id=index,
            source_id=source_id,
            line_number=line_number,
            line=line,
        )
        for index, (_, source_id, line_number, line) in enumerate(
            observation_keys,
            start=1,
        )
    )

    marker_literals = tuple(sorted({hit.marker for hit in evidence.hits}))
    marker_id_by_literal = {
        literal: index for index, literal in enumerate(marker_literals, start=1)
    }
    markers = tuple(
        MethodEvaluationMarkerV2(id=index, literal=literal)
        for index, literal in enumerate(marker_literals, start=1)
    )

    hits_by_ref = {hit.hit_ref: hit for hit in evidence.hits}
    events_by_ref = {event.event_ref: event for event in evidence.events}
    evaluations: list[MethodEvaluationItemInputV2] = []
    for planned in plan.evaluations:
        projected_events: list[MethodEvaluationEventInputV2] = []
        for event_ref in planned.evidence_event_refs:
            event = events_by_ref[event_ref]
            projected_events.append(
                MethodEvaluationEventInputV2(
                    event_ref=event.event_ref,
                    identity_tokens=event.identity_tokens,
                    matches=tuple(
                        MethodEvaluationMatchV2(
                            observation_id=observation_id_by_key[
                                (
                                    hit.source_ref,
                                    hit.source_id,
                                    hit.line_number,
                                    hit.line,
                                )
                            ],
                            marker_id=marker_id_by_literal[hit.marker],
                            method_marker_index=hit.marker_index,
                        )
                        for hit_ref in event.evidence_hit_refs
                        for hit in (hits_by_ref[hit_ref],)
                    ),
                )
            )
        evaluations.append(
            MethodEvaluationItemInputV2(
                evaluation_ref=planned.evaluation_ref,
                method_id=planned.method_id,
                method_priority=planned.method_priority,
                events=tuple(projected_events),
            )
        )

    return MethodEvaluationInputV2(
        schema_version=2,
        evidence_graph_ref=evidence.graph_ref,
        plan_ref=plan.plan_ref,
        limitations=evidence.limitations,
        sources=sources,
        observations=observations,
        markers=markers,
        evaluations=tuple(evaluations),
    )


def build_method_evaluation_input_v2(
    *,
    evidence: MethodEvidenceGraphV2,
    plan: MethodEvaluationPlanV2,
) -> MethodEvaluationInputV2:
    """Return the deterministic, lossless model projection for Graph and Plan."""

    if not isinstance(evidence, MethodEvidenceGraphV2):
        raise TypeError("evidence must be MethodEvidenceGraphV2")
    if not isinstance(plan, MethodEvaluationPlanV2):
        raise TypeError("plan must be MethodEvaluationPlanV2")
    validate_method_evaluation_plan_v2(evidence=evidence, plan=plan)
    return _project_method_evaluation_input_v2(evidence=evidence, plan=plan)


def validate_method_evaluation_input_v2(
    *,
    evidence: MethodEvidenceGraphV2,
    plan: MethodEvaluationPlanV2,
    model_input: MethodEvaluationInputV2,
) -> None:
    """Verify that a compact input exactly projects its Graph and Plan."""

    if not isinstance(evidence, MethodEvidenceGraphV2):
        raise TypeError("evidence must be MethodEvidenceGraphV2")
    if not isinstance(plan, MethodEvaluationPlanV2):
        raise TypeError("plan must be MethodEvaluationPlanV2")
    if not isinstance(model_input, MethodEvaluationInputV2):
        raise TypeError("model_input must be MethodEvaluationInputV2")
    validate_method_evaluation_plan_v2(evidence=evidence, plan=plan)
    expected = _project_method_evaluation_input_v2(evidence=evidence, plan=plan)
    if model_input != expected:
        raise ValueError(
            "compact method evaluation input does not exactly project its Graph and Plan"
        )


__all__ = [
    "MethodEvaluationEventInputV2",
    "MethodEvaluationInputV2",
    "MethodEvaluationItemInputV2",
    "MethodEvaluationMarkerV2",
    "MethodEvaluationMatchV2",
    "MethodEvaluationObservationV2",
    "MethodEvaluationSourceV2",
    "build_method_evaluation_input_v2",
    "validate_method_evaluation_input_v2",
]
