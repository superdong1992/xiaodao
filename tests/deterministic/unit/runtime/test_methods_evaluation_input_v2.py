from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from problem_locator.contracts import canonical_json_bytes, parse_canonical_json_bytes
from problem_locator.runtime.methods_evaluation_input_v2 import (
    MethodEvaluationInputV2,
    build_method_evaluation_input_v2,
    validate_method_evaluation_input_v2,
)
from problem_locator.runtime.methods_evidence_v2 import (
    build_method_evaluation_plan_v2,
    scan_method_evidence_v2,
)
from problem_locator.runtime.methods_grounding import FrozenTargetLogV1

from tests.deterministic.unit.runtime.methods_v2_test_support import (
    load_test_methods_skill,
)


def _target(source_id: str, text: str) -> FrozenTargetLogV1:
    content = text.encode("utf-8")
    return FrozenTargetLogV1(
        source_id=source_id,
        relative_path=f"logs/{source_id}.log",
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _all_keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_keys(child)


def test_shared_marker_lines_are_catalogued_once_without_losing_relations(
    tmp_path,
) -> None:
    skill = load_test_methods_skill(
        tmp_path,
        name="compact-shared-marker",
        methods=(
            ("first-method", "SHARED_MARKER"),
            ("second-method", "SHARED_MARKER"),
            ("third-method", "SHARED_MARKER"),
        ),
    )
    lines = tuple(
        f"shared_marker request_id=req-{index} unique-payload-{index}"
        for index in range(1, 5)
    )
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(_target("server", "\n".join(lines) + "\n"),),
        limitations=("证据仅覆盖指定日志窗口",),
    )
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)

    model_input = build_method_evaluation_input_v2(evidence=graph, plan=plan)
    validate_method_evaluation_input_v2(
        evidence=graph,
        plan=plan,
        model_input=model_input,
    )
    canonical = canonical_json_bytes(model_input)
    decoded = json.loads(canonical)

    assert parse_canonical_json_bytes(canonical, MethodEvaluationInputV2) == model_input
    assert model_input.evidence_graph_ref == graph.graph_ref
    assert model_input.plan_ref == plan.plan_ref
    assert model_input.limitations == graph.limitations
    assert [(item.source_id, item.relative_path) for item in model_input.sources] == [
        ("server", "logs/server.log")
    ]
    assert len(model_input.observations) == len(lines)
    assert [item.line for item in model_input.observations] == list(lines)
    assert [(item.id, item.literal) for item in model_input.markers] == [
        (1, "SHARED_MARKER")
    ]
    assert [item.evaluation_ref for item in model_input.evaluations] == [
        item.evaluation_ref for item in plan.evaluations
    ]
    assert [item.method_id for item in model_input.evaluations] == [
        "first-method",
        "second-method",
        "third-method",
    ]
    assert sum(
        len(event.matches)
        for evaluation in model_input.evaluations
        for event in evaluation.events
    ) == len(graph.hits) == 12
    assert [
        event.event_ref
        for evaluation in model_input.evaluations
        for event in evaluation.events
    ] == [item.event_ref for item in graph.events]
    assert [
        event.identity_tokens
        for evaluation in model_input.evaluations
        for event in evaluation.events
    ] == [item.identity_tokens for item in graph.events]
    for line in lines:
        assert canonical.decode("utf-8").count(line) == 1
    # The line uses a case-folded spelling, so the declared literal occurs only
    # in the marker catalog rather than being confused with its log occurrence.
    assert canonical.decode("utf-8").count("SHARED_MARKER") == 1

    forbidden_keys = {
        "content_sha256",
        "evidence_hit_refs",
        "hit_ref",
        "hits",
        "loaded_method_ids",
        "skill_sha256",
        "source_ref",
    }
    assert forbidden_keys.isdisjoint(set(_all_keys(decoded)))
    assert json.loads(
        json.dumps(model_input.model_dump(mode="json"), ensure_ascii=False)
    ) == decoded

    observations_by_id = {item.id: item for item in model_input.observations}
    markers_by_id = {item.id: item for item in model_input.markers}
    projected_events = {
        event.event_ref: event
        for evaluation in model_input.evaluations
        for event in evaluation.events
    }
    hits_by_ref = {item.hit_ref: item for item in graph.hits}
    for event in graph.events:
        projected = projected_events[event.event_ref]
        assert projected.identity_tokens == event.identity_tokens
        assert [
            (
                observations_by_id[match.observation_id].source_id,
                observations_by_id[match.observation_id].line_number,
                observations_by_id[match.observation_id].line,
                markers_by_id[match.marker_id].literal,
                match.method_marker_index,
            )
            for match in projected.matches
        ] == [
            (
                hits_by_ref[hit_ref].source_id,
                hits_by_ref[hit_ref].line_number,
                hits_by_ref[hit_ref].line,
                hits_by_ref[hit_ref].marker,
                hits_by_ref[hit_ref].marker_index,
            )
            for hit_ref in event.evidence_hit_refs
        ]


def test_large_shared_marker_graph_projects_below_the_byte_boundary(tmp_path) -> None:
    skill = load_test_methods_skill(
        tmp_path,
        name="compact-capacity-regression",
        methods=(
            ("first-method", "SHARED_MARKER"),
            ("second-method", "SHARED_MARKER"),
            ("third-method", "SHARED_MARKER"),
        ),
    )
    lines = tuple(
        "shared_marker "
        f"request_id=req-{index} row={index} payload="
        + chr(97 + index % 26) * 2048
        for index in range(40)
    )
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(_target("server", "\n".join(lines) + "\n"),),
    )
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)

    model_input = build_method_evaluation_input_v2(evidence=graph, plan=plan)
    graph_and_plan_bytes = canonical_json_bytes(graph) + canonical_json_bytes(plan)
    model_input_bytes = canonical_json_bytes(model_input)

    assert len(graph.hits) == 120
    assert len(graph_and_plan_bytes) > 256 * 1024
    assert len(model_input_bytes) < 128 * 1024
    assert len(model_input_bytes) * 2 < len(graph_and_plan_bytes)
    assert len(model_input.observations) == 40
    assert len(model_input.markers) == 1
    assert sum(
        len(event.matches)
        for evaluation in model_input.evaluations
        for event in evaluation.events
    ) == 120
    for line in lines:
        assert model_input_bytes.decode("utf-8").count(line) == 1


def test_source_catalog_preserves_scanned_source_without_matching_lines(tmp_path) -> None:
    skill = load_test_methods_skill(
        tmp_path,
        name="compact-zero-hit-source",
        methods=(("timeout-method", "TIMEOUT_MARKER"),),
    )
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(
            _target("client", "client noise without a declared marker\n"),
            _target("server", "timeout_marker request_id=req-1\n"),
        ),
    )
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)

    model_input = build_method_evaluation_input_v2(evidence=graph, plan=plan)

    assert [
        (item.id, item.source_id, item.relative_path)
        for item in model_input.sources
    ] == [
        (1, "client", "logs/client.log"),
        (2, "server", "logs/server.log"),
    ]
    assert {item.source_id for item in model_input.observations} == {"server"}
    assert "client noise without a declared marker" not in canonical_json_bytes(
        model_input
    ).decode("utf-8")


def test_projection_is_stable_across_equivalent_target_log_order(tmp_path) -> None:
    skill = load_test_methods_skill(
        tmp_path,
        name="compact-deterministic-order",
        methods=(
            ("first-method", "SHARED_MARKER"),
            ("second-method", "SHARED_MARKER"),
        ),
    )
    first_target = _target(
        "a-source",
        "shared_marker request_id=req-a payload=a\n",
    )
    second_target = _target(
        "z-source",
        "shared_marker request_id=req-z payload=z\n",
    )

    first_graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(second_target, first_target),
    )
    second_graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(first_target, second_target),
    )
    first_plan = build_method_evaluation_plan_v2(skill=skill, evidence=first_graph)
    second_plan = build_method_evaluation_plan_v2(skill=skill, evidence=second_graph)

    first_input = build_method_evaluation_input_v2(
        evidence=first_graph,
        plan=first_plan,
    )
    second_input = build_method_evaluation_input_v2(
        evidence=second_graph,
        plan=second_plan,
    )

    assert first_graph == second_graph
    assert first_plan == second_plan
    assert first_input == second_input
    assert canonical_json_bytes(first_input) == canonical_json_bytes(second_input)
    assert [item.source_id for item in first_input.observations] == [
        "a-source",
        "z-source",
    ]


def test_projection_is_frozen_and_validator_rejects_wrong_identity(tmp_path) -> None:
    skill = load_test_methods_skill(
        tmp_path,
        name="compact-validation",
        methods=(("first-method", "MARKER"),),
    )
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(_target("server", "marker request_id=req-1\n"),),
    )
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)
    model_input = build_method_evaluation_input_v2(evidence=graph, plan=plan)

    with pytest.raises(ValidationError, match="frozen"):
        model_input.plan_ref = plan.plan_ref  # type: ignore[misc]

    wrong_identity = model_input.model_copy(
        update={"plan_ref": "plan-" + "f" * 64}
    )
    with pytest.raises(ValueError, match="does not exactly project"):
        validate_method_evaluation_input_v2(
            evidence=graph,
            plan=plan,
            model_input=wrong_identity,
        )
