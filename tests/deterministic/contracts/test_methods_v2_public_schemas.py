from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import TypeAdapter, ValidationError as PydanticValidationError

from problem_locator.application.formalization import (
    build_methods_reviewer_outcome_v2,
)
from problem_locator.contracts import (
    SCHEMA_MODELS,
    Job,
    JobOutcome,
    MethodConsensusV2,
    MethodEvidenceGraphV2,
    MethodEvaluationPlanV2,
    MethodLimitationsRecordV2,
    MethodRoleEvaluationV2,
    MethodStateV2,
    MethodTerminalResultV2,
    MethodsReviewerResultV2,
    MethodsTerminalProjectionV2,
    OutcomeDisposition,
    StateFile,
    WorkspaceInputManifest,
    project_method_terminal_result_v2,
    validate_methods_reviewer_terminal_v2,
    validate_workspace_manifest_for_job,
)
from problem_locator.runtime.context_builder import (
    build_methods_reviewer_manifest_v2,
)
from problem_locator.domain.methods_state_v2 import (
    accept_specialist_evaluation_v2,
    fail_method_state_v2,
    finalize_reviewer_consensus_v2,
    interrupt_method_state_v2,
    start_method_state_v2,
)
from problem_locator.runtime.methods_evaluation_v2 import (
    MethodEvaluationResponseError,
    evaluate_method_role_v2,
    parse_method_evaluation_response_v2,
    resolve_method_consensus_v2,
)
from problem_locator.runtime.methods_evidence_v2 import (
    build_method_evaluation_plan_v2,
    scan_method_evidence_v2,
    validate_method_evaluation_plan_v2,
)
from problem_locator.runtime.methods_grounding import FrozenTargetLogV1
from problem_locator.runtime.methods_outcome_v2 import (
    build_method_terminal_result_v2,
)
from problem_locator.runtime.methods_records_v2 import (
    build_method_limitations_record_v2,
)
from tests.deterministic.contracts._support import schema_validator
from tests.deterministic.integration.test_methods_v2_terminal_submission import (
    _running_review_state,
    _submit,
)
from tests.deterministic.unit.domain.test_methods_v2_blind_review_seam import (
    EVALUATION_ID,
    REVIEW_OUTCOME_ID,
    _flow_inputs,
    _plan_and_review_job,
)
from tests.deterministic.unit.runtime.methods_v2_test_support import (
    load_test_methods_skill,
)


METHODS_V2_PUBLIC_SCHEMAS = frozenset(
    {
        "method-consensus.schema.json",
        "method-evaluation-plan.schema.json",
        "method-evaluation-response.schema.json",
        "method-evidence-graph.schema.json",
        "method-limitations-record.schema.json",
        "method-role-evaluation.schema.json",
        "method-state.schema.json",
        "method-terminal-result.schema.json",
        "methods-reviewer-result.schema.json",
        "methods-terminal-projection.schema.json",
    }
)


@dataclass(frozen=True)
class _ProductionChain:
    source_job: Job
    review_job: Job
    graph: MethodEvidenceGraphV2
    plan: MethodEvaluationPlanV2
    specialist: MethodRoleEvaluationV2
    reviewer: MethodRoleEvaluationV2
    consensus: MethodConsensusV2
    reviewer_pending: MethodStateV2
    terminal_state: MethodStateV2
    limitations: MethodLimitationsRecordV2
    terminal_result: MethodTerminalResultV2
    reviewer_result: MethodsReviewerResultV2
    projection: MethodsTerminalProjectionV2
    outcome: JobOutcome
    state_file: StateFile
    workspace_manifest: WorkspaceInputManifest


def _role_response(
    plan: MethodEvaluationPlanV2,
    *,
    role: str,
    verdicts: tuple[str, ...],
) -> list[dict[str, str]]:
    return [
        {
            "evaluation_ref": item.evaluation_ref,
            "verdict": verdict,
            "reason": f"private {role.lower()} reason {index}",
        }
        for index, (item, verdict) in enumerate(
            zip(plan.evaluations, verdicts, strict=True),
            start=1,
        )
    ]


def _role_evaluation(
    plan: MethodEvaluationPlanV2,
    *,
    role: str,
    verdicts: tuple[str, ...],
) -> MethodRoleEvaluationV2:
    return evaluate_method_role_v2(
        role=role,  # type: ignore[arg-type]
        plan=plan,
        response=_role_response(plan, role=role, verdicts=verdicts),
        attempt="PRIMARY",
    )


@pytest.fixture(scope="module")
def production_chain(
    tmp_path_factory: pytest.TempPathFactory,
) -> _ProductionChain:
    tmp_path = tmp_path_factory.mktemp("methods-v2-public-schemas")
    inputs = _flow_inputs(tmp_path)
    source_job = inputs[0]
    _, _, _, _, review_job, graph, plan = _plan_and_review_job(inputs)
    verdicts = tuple(
        "CONFIRMED" if index == 0 else "REJECTED"
        for index, _ in enumerate(plan.evaluations)
    )
    specialist = _role_evaluation(
        plan,
        role="SPECIALIST",
        verdicts=verdicts,
    )
    reviewer = _role_evaluation(
        plan,
        role="REVIEWER",
        verdicts=verdicts,
    )
    reviewer_pending = accept_specialist_evaluation_v2(
        state=start_method_state_v2(
            case_id=source_job.case_id,
            source_job_id=source_job.job_id,
            evaluation_id=EVALUATION_ID,
            plan=plan,
        ),
        evaluation=specialist,
    )
    consensus = resolve_method_consensus_v2(
        plan=plan,
        first=specialist,
        second=reviewer,
    )
    terminal_state = finalize_reviewer_consensus_v2(
        state=reviewer_pending,
        plan=plan,
        reviewer_evaluation=reviewer,
        consensus=consensus,
    )
    limitations = build_method_limitations_record_v2(
        case_id=source_job.case_id,
        source_job_id=source_job.job_id,
        graph=graph,
        plan=plan,
        limitations=("server-observed limitation",),
    )
    terminal_result = build_method_terminal_result_v2(
        state=terminal_state,
        plan=plan,
        evidence=graph,
        terminal_job_id=review_job.job_id,
        limitations=limitations.limitations,
    )
    outcome = build_methods_reviewer_outcome_v2(
        review_job,
        outcome_id=REVIEW_OUTCOME_ID,
        terminal_state=terminal_state,
        terminal_result=terminal_result,
        plan=plan,
        produced_at="2026-07-31T00:03:30.000Z",
    )
    assert outcome.methods_reviewer_result is not None
    assert outcome.methods_terminal_projection is not None
    workspace_manifest = build_methods_reviewer_manifest_v2(
        review_job,
        method_ids=tuple(item.method_id for item in plan.evaluations),
    )
    validate_workspace_manifest_for_job(workspace_manifest, review_job)

    initial_state, persisted_outcome, persisted_projection = _running_review_state(
        tmp_path / "state-file-flow",
        verdicts,
        verdicts,
    )
    assert persisted_outcome == outcome
    assert persisted_projection == outcome.methods_terminal_projection
    receipt, repository, _ = _submit(initial_state, persisted_outcome)
    assert receipt.disposition is OutcomeDisposition.APPLIED
    state_file = repository.read_snapshot()
    return _ProductionChain(
        source_job=source_job,
        review_job=review_job,
        graph=graph,
        plan=plan,
        specialist=specialist,
        reviewer=reviewer,
        consensus=consensus,
        reviewer_pending=reviewer_pending,
        terminal_state=terminal_state,
        limitations=limitations,
        terminal_result=terminal_result,
        reviewer_result=outcome.methods_reviewer_result,
        projection=outcome.methods_terminal_projection,
        outcome=outcome,
        state_file=state_file,
        workspace_manifest=workspace_manifest,
    )


def _public_values(chain: _ProductionChain) -> dict[str, object]:
    return {
        "method-consensus.schema.json": chain.consensus,
        "method-evaluation-plan.schema.json": chain.plan,
        "method-evaluation-response.schema.json": chain.specialist.evaluations,
        "method-evidence-graph.schema.json": chain.graph,
        "method-limitations-record.schema.json": chain.limitations,
        "method-role-evaluation.schema.json": chain.specialist,
        "method-state.schema.json": chain.terminal_state,
        "method-terminal-result.schema.json": chain.terminal_result,
        "methods-reviewer-result.schema.json": chain.reviewer_result,
        "methods-terminal-projection.schema.json": chain.projection,
    }


def _json_payload(schema_name: str, value: object) -> Any:
    return TypeAdapter(SCHEMA_MODELS[schema_name]).dump_python(
        value,
        mode="json",
    )


def _leaf_differences(left: Any, right: Any) -> int:
    if type(left) is not type(right):
        return 1
    if isinstance(left, dict):
        if left.keys() != right.keys():
            return 1
        return sum(_leaf_differences(left[key], right[key]) for key in left)
    if isinstance(left, list):
        if len(left) != len(right):
            return 1
        return sum(
            _leaf_differences(first, second)
            for first, second in zip(left, right, strict=True)
        )
    return int(left != right)


def _invalid_public_field(schema_name: str, payload: Any) -> Any:
    mutated = copy.deepcopy(payload)
    if schema_name == "method-consensus.schema.json":
        mutated["status"] = "PARTIALLY_RESOLVED"
    elif schema_name == "method-evaluation-plan.schema.json":
        mutated["plan_ref"] = "not-a-plan-ref"
    elif schema_name == "method-evaluation-response.schema.json":
        mutated[0]["verdict"] = "MAYBE"
    elif schema_name == "method-evidence-graph.schema.json":
        mutated["hits"][0]["marker"] = ""
    elif schema_name == "method-limitations-record.schema.json":
        mutated["schema_version"] = 1
    elif schema_name == "method-role-evaluation.schema.json":
        mutated["role"] = "ARBITER"
    elif schema_name == "method-state.schema.json":
        mutated["status"] = "PARTIALLY_RESOLVED"
    elif schema_name == "method-terminal-result.schema.json":
        mutated["status"] = "PARTIALLY_RESOLVED"
    elif schema_name == "methods-reviewer-result.schema.json":
        mutated["role"] = "SPECIALIST"
    elif schema_name == "methods-terminal-projection.schema.json":
        mutated["status"] = "PARTIALLY_RESOLVED"
    else:  # pragma: no cover - the exact public set is asserted below
        raise AssertionError(f"missing invalid mutation for {schema_name}")
    return mutated


def test_all_ten_public_schema_roots_accept_production_objects(
    production_chain: _ProductionChain,
) -> None:
    values = _public_values(production_chain)

    assert set(values) == METHODS_V2_PUBLIC_SCHEMAS
    assert METHODS_V2_PUBLIC_SCHEMAS <= set(SCHEMA_MODELS)
    for schema_name, value in values.items():
        payload = _json_payload(schema_name, value)
        schema_validator(schema_name).validate(payload)
        TypeAdapter(SCHEMA_MODELS[schema_name]).validate_python(payload)


@pytest.mark.parametrize("schema_name", sorted(METHODS_V2_PUBLIC_SCHEMAS))
def test_each_public_root_rejects_one_invalid_field_in_schema_and_pydantic(
    production_chain: _ProductionChain,
    schema_name: str,
) -> None:
    value = _public_values(production_chain)[schema_name]
    baseline = _json_payload(schema_name, value)
    mutated = _invalid_public_field(schema_name, baseline)

    assert _leaf_differences(baseline, mutated) == 1
    with pytest.raises(JsonSchemaValidationError):
        schema_validator(schema_name).validate(mutated)
    with pytest.raises(PydanticValidationError):
        TypeAdapter(SCHEMA_MODELS[schema_name]).validate_python(mutated)


def test_production_scan_keeps_shared_marker_hits_method_qualified(
    tmp_path: Path,
) -> None:
    skill = load_test_methods_skill(
        tmp_path,
        name="method-qualified-schema-test",
        methods=(
            ("first-method", "SHARED_MARKER"),
            ("second-method", "SHARED_MARKER"),
        ),
    )
    content = b"shared_marker request_id=req-1\n"
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(
            FrozenTargetLogV1(
                source_id="server",
                relative_path="logs/server.log",
                content_sha256=hashlib.sha256(content).hexdigest(),
                content=content,
            ),
        ),
    )
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)

    assert [(hit.method_id, hit.marker) for hit in graph.hits] == [
        ("first-method", "SHARED_MARKER"),
        ("second-method", "SHARED_MARKER"),
    ]
    assert graph.hits[0].hit_ref != graph.hits[1].hit_ref
    assert [(event.method_id, event.evidence_hit_refs) for event in graph.events] == [
        ("first-method", (graph.hits[0].hit_ref,)),
        ("second-method", (graph.hits[1].hit_ref,)),
    ]
    validate_method_evaluation_plan_v2(evidence=graph, plan=plan)

    mutated = graph.model_dump(mode="json")
    mutated["hits"][0]["method_id"] = "second-method"
    assert _leaf_differences(graph.model_dump(mode="json"), mutated) == 1
    with pytest.raises(PydanticValidationError, match="method-qualified|hit_ref"):
        MethodEvidenceGraphV2.model_validate(mutated)


@pytest.mark.parametrize("mutation", ["coverage", "order", "ref"])
def test_evaluation_response_requires_exact_plan_coverage_order_and_refs(
    production_chain: _ProductionChain,
    mutation: str,
) -> None:
    baseline = [
        item.model_dump(mode="json")
        for item in production_chain.specialist.evaluations
    ]
    assert len(baseline) >= 2
    if mutation == "coverage":
        changed = baseline[:-1]
    elif mutation == "order":
        changed = list(reversed(baseline))
    else:
        changed = copy.deepcopy(baseline)
        changed[0]["evaluation_ref"] = baseline[1]["evaluation_ref"]

    with pytest.raises(MethodEvaluationResponseError, match="cover|order"):
        parse_method_evaluation_response_v2(
            plan=production_chain.plan,
            response=changed,
        )

    state_payload = production_chain.terminal_state.model_dump(mode="json")
    assert state_payload["specialist_evaluation"] is not None
    state_payload["specialist_evaluation"]["evaluations"] = changed
    with pytest.raises(
        PydanticValidationError,
        match="exactly cover state evaluations",
    ):
        MethodStateV2.model_validate(state_payload)


def _finalized_state(
    chain: _ProductionChain,
    *,
    verdicts: tuple[str, ...],
) -> MethodStateV2:
    specialist = _role_evaluation(
        chain.plan,
        role="SPECIALIST",
        verdicts=verdicts,
    )
    reviewer = _role_evaluation(
        chain.plan,
        role="REVIEWER",
        verdicts=verdicts,
    )
    pending = accept_specialist_evaluation_v2(
        state=start_method_state_v2(
            case_id=chain.source_job.case_id,
            source_job_id=chain.source_job.job_id,
            evaluation_id=EVALUATION_ID,
            plan=chain.plan,
        ),
        evaluation=specialist,
    )
    consensus = resolve_method_consensus_v2(
        plan=chain.plan,
        first=specialist,
        second=reviewer,
    )
    return finalize_reviewer_consensus_v2(
        state=pending,
        plan=chain.plan,
        reviewer_evaluation=reviewer,
        consensus=consensus,
    )


def test_state_and_result_status_truth_tables_use_production_transitions(
    production_chain: _ProductionChain,
) -> None:
    specialist_pending = start_method_state_v2(
        case_id=production_chain.source_job.case_id,
        source_job_id=production_chain.source_job.job_id,
        evaluation_id=EVALUATION_ID,
        plan=production_chain.plan,
    )
    interrupted = interrupt_method_state_v2(state=specialist_pending)
    rejected_verdicts = tuple("REJECTED" for _ in production_chain.plan.evaluations)
    unresolved = _finalized_state(
        production_chain,
        verdicts=rejected_verdicts,
    )
    failed = fail_method_state_v2(
        state=specialist_pending,
        reason_code="SERVER_INVARIANT_VIOLATION",
        reason="The production state invariant failed.",
    )
    states = (
        specialist_pending,
        production_chain.reviewer_pending,
        production_chain.terminal_state,
        unresolved,
        failed,
        interrupted,
    )

    assert {state.status for state in states} == {
        "SPECIALIST_PENDING",
        "REVIEWER_PENDING",
        "RESOLVED",
        "UNRESOLVED",
        "FAILED",
        "INTERRUPTED",
    }
    for state in states:
        payload = state.model_dump(mode="json")
        schema_validator("method-state.schema.json").validate(payload)
        assert MethodStateV2.model_validate(payload) == state

    unresolved_result = build_method_terminal_result_v2(
        state=unresolved,
        plan=production_chain.plan,
        evidence=production_chain.graph,
        terminal_job_id=production_chain.review_job.job_id,
        limitations=production_chain.limitations.limitations,
    )
    failed_result = build_method_terminal_result_v2(
        state=failed,
        plan=production_chain.plan,
        evidence=production_chain.graph,
        terminal_job_id=production_chain.review_job.job_id,
        limitations=production_chain.limitations.limitations,
    )
    results = (
        production_chain.terminal_result,
        unresolved_result,
        failed_result,
    )
    assert {result.status for result in results} == {
        "RESOLVED",
        "UNRESOLVED",
        "FAILED",
    }
    for result in results:
        result_payload = result.model_dump(mode="json")
        schema_validator("method-terminal-result.schema.json").validate(result_payload)
        assert MethodTerminalResultV2.model_validate(result_payload) == result
        projection = project_method_terminal_result_v2(result)
        projection_payload = projection.model_dump(mode="json")
        schema_validator("methods-terminal-projection.schema.json").validate(
            projection_payload
        )
        assert MethodsTerminalProjectionV2.model_validate(
            projection_payload
        ) == projection


@pytest.mark.parametrize(
    ("schema_name", "model_type"),
    [
        ("method-state.schema.json", MethodStateV2),
        ("method-terminal-result.schema.json", MethodTerminalResultV2),
        ("methods-terminal-projection.schema.json", MethodsTerminalProjectionV2),
    ],
)
def test_valid_but_inconsistent_terminal_status_is_rejected_by_pydantic_truth_table(
    production_chain: _ProductionChain,
    schema_name: str,
    model_type: type[Any],
) -> None:
    value = {
        "method-state.schema.json": production_chain.terminal_state,
        "method-terminal-result.schema.json": production_chain.terminal_result,
        "methods-terminal-projection.schema.json": production_chain.projection,
    }[schema_name]
    baseline = value.model_dump(mode="json")
    mutated = copy.deepcopy(baseline)
    mutated["status"] = "FAILED"

    assert _leaf_differences(baseline, mutated) == 1
    with pytest.raises(PydanticValidationError):
        model_type.model_validate(mutated)


def test_reviewer_result_exactly_covers_plan_and_terminal_projection(
    production_chain: _ProductionChain,
) -> None:
    expected_refs = tuple(
        item.evaluation_ref for item in production_chain.plan.evaluations
    )

    assert tuple(
        item.evaluation_ref for item in production_chain.reviewer_result.evaluations
    ) == expected_refs
    validate_methods_reviewer_terminal_v2(
        production_chain.reviewer_result,
        production_chain.projection,
        review_job_id=production_chain.review_job.job_id,
        reviewed_state_revision=(
            production_chain.review_job.methods_review_target.reviewed_state_revision
        ),
        expected_target=production_chain.review_job.methods_review_target,
    )

    mutated = production_chain.reviewer_result.model_dump(mode="json")
    mutated["evaluations"][0]["evaluation_ref"] = expected_refs[1]
    with pytest.raises(PydanticValidationError, match="unique"):
        MethodsReviewerResultV2.model_validate(mutated)


def _composite_public_values(
    chain: _ProductionChain,
) -> dict[str, object]:
    return {
        "job.schema.json": chain.review_job,
        "job-outcome.schema.json": chain.outcome,
        "state.schema.json": chain.state_file,
        "workspace-input-manifest.schema.json": chain.workspace_manifest,
    }


def test_existing_composite_public_schemas_accept_the_production_methods_branch(
    production_chain: _ProductionChain,
) -> None:
    aggregate = production_chain.state_file.cases[
        production_chain.source_job.case_id
    ]
    persisted_job = aggregate.jobs[production_chain.review_job.job_id]
    persisted_outcome = aggregate.outcomes[production_chain.outcome.outcome_id]

    assert production_chain.review_job.methods_review_target is not None
    assert production_chain.workspace_manifest.methods_reviewer_input is not None
    assert persisted_job.methods_review_target == (
        production_chain.review_job.methods_review_target
    )
    assert persisted_outcome.methods_reviewer_result == (
        production_chain.reviewer_result
    )
    assert persisted_outcome.methods_terminal_projection == (
        production_chain.projection
    )
    assert aggregate.case.methods_result == production_chain.projection

    for schema_name, value in _composite_public_values(production_chain).items():
        payload = _json_payload(schema_name, value)
        schema_validator(schema_name).validate(payload)
        TypeAdapter(SCHEMA_MODELS[schema_name]).validate_python(payload)


@pytest.mark.parametrize(
    "schema_name",
    [
        "job.schema.json",
        "job-outcome.schema.json",
        "state.schema.json",
        "workspace-input-manifest.schema.json",
    ],
)
def test_composite_methods_branch_rejects_one_invalid_nested_field(
    production_chain: _ProductionChain,
    schema_name: str,
) -> None:
    baseline = _json_payload(
        schema_name,
        _composite_public_values(production_chain)[schema_name],
    )
    mutated = copy.deepcopy(baseline)
    if schema_name == "job.schema.json":
        mutated["methods_review_target"]["plan_ref"] = "not-a-plan-ref"
    elif schema_name == "job-outcome.schema.json":
        mutated["methods_reviewer_result"]["evaluations"][0]["verdict"] = "MAYBE"
    elif schema_name == "state.schema.json":
        case = mutated["cases"][production_chain.source_job.case_id]
        outcome = case["outcomes"][production_chain.outcome.outcome_id]
        outcome["methods_terminal_projection"]["result_ref"] = "not-a-result-ref"
    else:
        reviewer_input = mutated["methods_reviewer_input"]
        reviewer_input["target"]["graph_ref"] = "not-a-graph-ref"

    assert _leaf_differences(baseline, mutated) == 1
    with pytest.raises(JsonSchemaValidationError):
        schema_validator(schema_name).validate(mutated)
    with pytest.raises(PydanticValidationError):
        TypeAdapter(SCHEMA_MODELS[schema_name]).validate_python(mutated)


def test_workspace_manifest_target_must_match_its_production_review_job(
    production_chain: _ProductionChain,
) -> None:
    baseline = production_chain.workspace_manifest.model_dump(mode="json")
    mutated = copy.deepcopy(baseline)
    mutated["methods_reviewer_input"]["target"]["graph_ref"] = (
        "graph-" + "f" * 64
    )
    parsed = WorkspaceInputManifest.model_validate(mutated)

    assert _leaf_differences(baseline, mutated) == 1
    with pytest.raises(ValueError, match="must match"):
        validate_workspace_manifest_for_job(parsed, production_chain.review_job)
