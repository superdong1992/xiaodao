from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

import problem_locator.runtime.methods_evidence_v2 as methods_evidence_v2
from problem_locator.contracts import (
    MethodEvidenceEventV2,
    MethodEvidenceGraphV2,
    MethodEvidenceHitV2,
    method_evidence_event_ref_v2,
    method_evidence_graph_ref_v2,
    method_evidence_hit_ref_v2,
)
from problem_locator.runtime.methods_evidence_v2 import (
    build_method_evaluation_plan_v2,
    scan_method_evidence_v2,
    validate_method_evaluation_plan_v2,
)
from problem_locator.runtime.methods_grounding import FrozenTargetLogV1
from problem_locator.runtime.methods_skill import (
    MethodCardV1,
    MethodsManifestV1,
    PreprocessingBindingV1,
    RegistrationTemplateV1,
    ResolvedSpecializedSkillV1,
    RuntimeRoleBindingV1,
)


def _skill(
    *methods: tuple[str, tuple[str, ...]],
    log_derived_fields: tuple[str, ...] = (),
) -> ResolvedSpecializedSkillV1:
    role = RuntimeRoleBindingV1("profile", "tools", "policy", "output")
    cards = tuple(
        MethodCardV1(
            id=method_id,
            title=method_id,
            reference=f"references/{method_id}.md",
            priority=index,
            evidence_markers=markers,
        )
        for index, (method_id, markers) in enumerate(methods, start=1)
    )
    return ResolvedSpecializedSkillV1(
        registration_root=Path("registration"),
        package_root=Path("package"),
        registration=RegistrationTemplateV1(
            registration_id="test-methods",
            version="1.0.0",
            capability="test",
            deployment_scope="PRODUCTION",
            summary="test",
            package_relative_path="package/test-methods",
            skill_name="test-methods",
            source_wiki_sha256="1" * 64,
            diagnose=role,
            review=role,
            preprocessing=PreprocessingBindingV1(False, None, (), None),
        ),
        methods=MethodsManifestV1(
            skill_name="test-methods",
            source_wiki_sha256="1" * 64,
            required_user_inputs=(),
            required_artifacts=(),
            log_derived_fields=log_derived_fields,
            shared_references=(),
            methods=cards,
        ),
        registration_sha256="2" * 64,
        package_tree_sha256="3" * 64,
        combined_sha256="4" * 64,
    )


def _target(source_id: str, text: str) -> FrozenTargetLogV1:
    content = text.encode("utf-8")
    return FrozenTargetLogV1(
        source_id=source_id,
        relative_path=f"logs/{source_id}.log",
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def test_shared_literal_emits_one_method_qualified_hit_per_method() -> None:
    skill = _skill(
        ("first-method", ("SHARED_MARKER",)),
        ("second-method", ("SHARED_MARKER",)),
    )

    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(_target("server", "noise\nshared_marker request=42\n"),),
    )

    assert isinstance(graph, MethodEvidenceGraphV2)
    assert graph.loaded_method_ids == ("first-method", "second-method")
    assert [(item.method_id, item.marker, item.line_number) for item in graph.hits] == [
        ("first-method", "SHARED_MARKER", 2),
        ("second-method", "SHARED_MARKER", 2),
    ]
    assert graph.hits[0].hit_ref != graph.hits[1].hit_ref
    assert graph.sources[0].source_ref.startswith("source-")
    assert all(item.hit_ref.startswith("hit-") for item in graph.hits)


def test_scan_casefolds_but_preserves_declared_marker_and_frozen_line() -> None:
    skill = _skill(("unicode-method", ("Straße",)))
    target = _target("server", "STRASSE request=42\n")

    graph = scan_method_evidence_v2(skill=skill, target_logs=(target,))

    assert len(graph.sources) == 1
    assert graph.sources[0].content_sha256 == target.content_sha256
    assert len(graph.hits) == 1
    assert graph.hits[0].marker == "Straße"
    assert graph.hits[0].line == "STRASSE request=42"


def test_complete_plan_has_every_loaded_method_and_all_of_its_hits() -> None:
    skill = _skill(
        ("first-method", ("SHARED", "FIRST_ONLY")),
        ("second-method", ("SHARED",)),
    )
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(
            _target("client", "shared\n"),
            _target("server", "first_only shared\n"),
        ),
    )

    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)

    assert [item.method_id for item in plan.evaluations] == [
        "first-method",
        "second-method",
    ]
    for item in plan.evaluations:
        assert item.evidence_hit_refs == tuple(
            hit.hit_ref for hit in graph.hits if hit.method_id == item.method_id
        )
    assert len(plan.evaluations[0].evidence_hit_refs) == 3
    assert len(plan.evaluations[1].evidence_hit_refs) == 2


def test_plan_consumes_production_graph_refs_without_rescanning_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = _skill(("first-method", ("DECLARED_MARKER",)))
    graph = methods_evidence_v2.scan_method_evidence_v2(
        skill=skill,
        target_logs=(_target("server", "DECLARED_MARKER request_id=req-1\n"),),
    )

    def fail_if_rescanned(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"plan builder crossed the scan boundary: {args!r} {kwargs!r}")

    monkeypatch.setattr(methods_evidence_v2, "scan_method_evidence_v2", fail_if_rescanned)
    monkeypatch.setattr(methods_evidence_v2, "_validated_logs", fail_if_rescanned)
    plan = methods_evidence_v2.build_method_evaluation_plan_v2(
        skill=skill,
        evidence=graph,
    )

    assert plan.evaluations[0].evidence_hit_refs == tuple(
        item.hit_ref for item in graph.hits
    )
    assert plan.evaluations[0].evidence_event_refs == tuple(
        item.event_ref for item in graph.events
    )
    assert plan.evaluations[0].evaluation_ref.startswith("eval-")
    assert plan.plan_ref.startswith("plan-")


def test_plan_rejects_rehashed_hit_bound_to_another_methods_marker_index() -> None:
    skill = _skill(
        ("first-method", ("UNUSED", "FIRST")),
        ("second-method", ("SECOND",)),
    )
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(_target("server", "FIRST SECOND\n"),),
    )
    first_hit, second_hit = graph.hits

    forged_hit_ref = method_evidence_hit_ref_v2(
        method_id=first_hit.method_id,
        method_priority=first_hit.method_priority,
        marker_index=second_hit.marker_index,
        source_ref=first_hit.source_ref,
        source_id=first_hit.source_id,
        line_number=first_hit.line_number,
        marker=second_hit.marker,
        line=first_hit.line,
    )
    forged_hit = MethodEvidenceHitV2(
        hit_ref=forged_hit_ref,
        method_id=first_hit.method_id,
        method_priority=first_hit.method_priority,
        marker_index=second_hit.marker_index,
        source_ref=first_hit.source_ref,
        source_id=first_hit.source_id,
        line_number=first_hit.line_number,
        marker=second_hit.marker,
        line=first_hit.line,
    )
    first_event = graph.events[0]
    forged_event_ref = method_evidence_event_ref_v2(
        method_id=first_event.method_id,
        method_priority=first_event.method_priority,
        identity_tokens=first_event.identity_tokens,
        evidence_hit_refs=(forged_hit_ref,),
    )
    forged_event = MethodEvidenceEventV2(
        event_ref=forged_event_ref,
        method_id=first_event.method_id,
        method_priority=first_event.method_priority,
        identity_tokens=first_event.identity_tokens,
        evidence_hit_refs=(forged_hit_ref,),
    )
    forged_hits = (forged_hit, second_hit)
    forged_events = (forged_event, graph.events[1])
    forged_graph_ref = method_evidence_graph_ref_v2(
        skill_sha256=graph.skill_sha256,
        sources=graph.sources,
        hits=forged_hits,
        events=forged_events,
        loaded_method_ids=graph.loaded_method_ids,
        limitations=graph.limitations,
    )
    forged_graph = MethodEvidenceGraphV2(
        graph_ref=forged_graph_ref,
        skill_sha256=graph.skill_sha256,
        sources=graph.sources,
        hits=forged_hits,
        events=forged_events,
        loaded_method_ids=graph.loaded_method_ids,
        limitations=graph.limitations,
    )

    with pytest.raises(ValueError, match="marker/index does not belong to its method"):
        build_method_evaluation_plan_v2(skill=skill, evidence=forged_graph)


def test_evidence_refs_are_stable_for_the_same_frozen_inputs() -> None:
    skill = _skill(("first-method", ("MARKER",)))
    logs = (_target("server", "marker\n"),)

    first = scan_method_evidence_v2(skill=skill, target_logs=logs)
    second = scan_method_evidence_v2(skill=skill, target_logs=logs)

    assert first == second
    assert build_method_evaluation_plan_v2(
        skill=skill,
        evidence=first,
    ) == build_method_evaluation_plan_v2(skill=skill, evidence=second)


def test_hits_and_plan_use_business_order_not_manifest_or_log_order() -> None:
    skill = _skill(
        ("first-priority", ("MARKER_ONE", "MARKER_TWO")),
        ("second-priority", ("MARKER_ONE",)),
    )
    skill = replace(
        skill,
        methods=replace(skill.methods, methods=tuple(reversed(skill.methods.methods))),
    )

    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(
            _target("z-source", "marker_one marker_two\n"),
            _target("a-source", "marker_one marker_two\n"),
        ),
    )
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)

    hit_keys = [
        (
            item.method_priority,
            item.method_id,
            item.marker_index,
            item.source_id,
            item.line_number,
        )
        for item in graph.hits
    ]
    assert hit_keys == sorted(hit_keys)
    assert graph.loaded_method_ids == ("first-priority", "second-priority")
    assert [
        (item.method_priority, item.method_id) for item in plan.evaluations
    ] == [(1, "first-priority"), (2, "second-priority")]


def test_request_identity_tokens_create_distinct_events_without_losing_hits() -> None:
    skill = _skill(
        ("request-method", ("REQUEST_DONE",)),
        log_derived_fields=("request_id",),
    )
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(
            _target(
                "server",
                "REQUEST_DONE request_id=req-1\n"
                "REQUEST_DONE request_id=req-2\n"
                "REQUEST_DONE request_id=req-1\n",
            ),
        ),
    )

    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)

    assert [item.identity_tokens for item in graph.events] == [
        ("request_id=req-1",),
        ("request_id=req-2",),
    ]
    assert [len(item.evidence_hit_refs) for item in graph.events] == [2, 1]
    assert len(plan.evaluations) == 1
    assert plan.evaluations[0].evidence_event_refs == tuple(
        item.event_ref for item in graph.events
    )
    assert plan.evaluations[0].evidence_hit_refs == tuple(
        item.hit_ref for item in graph.hits
    )


def test_hits_without_identity_tokens_each_form_their_own_event() -> None:
    skill = _skill(
        ("request-method", ("REQUEST_DONE",)),
        log_derived_fields=("request_id",),
    )

    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(_target("server", "REQUEST_DONE\nREQUEST_DONE\n"),),
    )

    assert len(graph.hits) == 2
    assert len(graph.events) == 2
    assert all(item.identity_tokens == () for item in graph.events)
    assert [item.evidence_hit_refs for item in graph.events] == [
        (graph.hits[0].hit_ref,),
        (graph.hits[1].hit_ref,),
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-method",
        "missing-hit",
        "cross-method",
    ],
)
def test_plan_graph_validation_rejects_incomplete_or_cross_method_refs(
    mutation: str,
) -> None:
    skill = _skill(
        ("first-method", ("FIRST",)),
        ("second-method", ("SECOND",)),
    )
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(_target("server", "first\nfirst\nsecond\n"),),
    )
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)

    if mutation == "missing-method":
        forged = plan.model_copy(update={"evaluations": plan.evaluations[:-1]})
    elif mutation == "missing-hit":
        first = plan.evaluations[0].model_copy(
            update={"evidence_hit_refs": plan.evaluations[0].evidence_hit_refs[:-1]}
        )
        forged = plan.model_copy(update={"evaluations": (first, plan.evaluations[1])})
    else:
        first = plan.evaluations[0].model_copy(
            update={"evidence_hit_refs": plan.evaluations[1].evidence_hit_refs}
        )
        forged = plan.model_copy(update={"evaluations": (first, plan.evaluations[1])})

    with pytest.raises(ValueError):
        validate_method_evaluation_plan_v2(evidence=graph, plan=forged)


@pytest.mark.parametrize("mutation", ["omit-request-event", "merge-request-events"])
def test_plan_graph_validation_rejects_omitted_or_merged_request_events(
    mutation: str,
) -> None:
    skill = _skill(
        ("request-method", ("REQUEST_DONE",)),
        log_derived_fields=("request_id",),
    )
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(
            _target(
                "server",
                "REQUEST_DONE request_id=req-1\nREQUEST_DONE request_id=req-2\n",
            ),
        ),
    )
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)
    item = plan.evaluations[0]
    if mutation == "omit-request-event":
        event_refs = item.evidence_event_refs[:-1]
    else:
        event_refs = (
            method_evidence_event_ref_v2(
                method_id=item.method_id,
                method_priority=item.method_priority,
                identity_tokens=("request_id=req-1", "request_id=req-2"),
                evidence_hit_refs=item.evidence_hit_refs,
            ),
        )
    forged_item = item.model_copy(update={"evidence_event_refs": event_refs})
    forged = plan.model_copy(update={"evaluations": (forged_item,)})

    with pytest.raises(ValueError, match="evidence events"):
        validate_method_evaluation_plan_v2(evidence=graph, plan=forged)
