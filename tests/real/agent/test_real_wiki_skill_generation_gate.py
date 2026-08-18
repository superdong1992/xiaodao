from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from problem_locator.contracts import (
    EvidenceBinding,
    ExecutionLogSinks,
    JOB_STDOUT_STDERR_BYTES,
    JobType,
    ServerRuleStatus,
    default_resource_limits,
)
from problem_locator.runtime.agent_backend import AgentBackend, BackendExecutionLimits
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.server_verifier import (
    _ResolvedLine,
    _evaluate_rule,
    _extract_events,
)
from problem_locator.runtime.verification_contract import terminal_path_matches


ROOT = Path(__file__).resolve().parents[3]
GENERATOR_PATH = (
    ROOT
    / ".claude/skills/wiki-to-diagnosis-skill/scripts/generate_diagnosis_skill.py"
)
VALIDATOR_PATH = (
    ROOT
    / ".claude/skills/wiki-to-diagnosis-skill/scripts/validate_generated_skill.py"
)
SCENARIO_AUDIT_FILE = "scenario-evaluation-audit.json"
SCENARIO_AUDIT_MAX_BYTES = 64 * 1024
SCENARIO_AUDIT_MAX_MISMATCHES = 128
SCENARIO_AUDIT_MAX_TERMINAL_PATHS = 50
SCENARIO_AUDIT_MAX_PATH_BRANCHES = 50
SCENARIO_AUDIT_MAX_BRANCH_TERMS = 100
SCENARIO_AUDIT_MAX_REPORTED_TERMS = 64
SCENARIO_AUDIT_MAX_REPORTED_TERMS_PER_BRANCH = 4
_AUDIT_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
_PRODUCT_SEMANTIC_TARGET_FIELDS = {
    "analysis_steps",
    "assumptions",
    "chinese_title",
    "judgement_rules",
    "output_requirements",
    "problem_scope",
    "summary",
    "time_characteristics",
}


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _release_case_root() -> Path:
    configured = Path(os.environ["S08_RELEASE_CASES_ROOT"])
    candidates = sorted(
        path.parent for path in configured.glob("*/case.json")
    )
    assert len(candidates) == 1
    return candidates[0]


class _Signal:
    reason = None

    def __init__(self) -> None:
        self._event = threading.Event()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout_seconds: float | None) -> bool:
        return self._event.wait(timeout_seconds)


class _Sink:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, chunk: bytes) -> None:
        assert not self.closed
        self.data.extend(chunk)

    def flush(self) -> None:
        assert not self.closed

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def isolated_agent_workspace_root() -> Path:
    with tempfile.TemporaryDirectory(prefix="xiaodao-wiki-skill-") as temporary:
        root = Path(temporary).resolve()
        repository = ROOT.resolve()
        assert root != repository
        assert repository not in root.parents
        assert root not in repository.parents
        yield root


def _product_semantic_projection(
    spec: Any,
    target_fields: list[str],
) -> dict[str, str | list[str]]:
    assert target_fields
    assert len(target_fields) == len(set(target_fields))
    assert set(target_fields) <= _PRODUCT_SEMANTIC_TARGET_FIELDS
    projection: dict[str, str | list[str]] = {}
    for field in target_fields:
        value = getattr(spec, field)
        if isinstance(value, str):
            projection[field] = value
            continue
        assert isinstance(value, tuple)
        assert all(isinstance(item, str) for item in value)
        projection[field] = list(value)
    return projection


def _product_semantic_field_text(value: str | list[str]) -> str:
    if isinstance(value, str):
        return value
    assert all(isinstance(item, str) for item in value)
    return "\n".join(value)


def _product_semantic_projection_text(
    projection: dict[str, str | list[str]],
) -> str:
    return "\n".join(
        _product_semantic_field_text(value) for value in projection.values()
    )


def _product_semantic_group_field_matches(
    projection: dict[str, str | list[str]],
    alternatives: list[str],
) -> dict[str, bool]:
    assert alternatives
    assert all(isinstance(pattern, str) and pattern for pattern in alternatives)
    return {
        field: any(
            re.search(
                pattern,
                _product_semantic_field_text(value),
                re.IGNORECASE,
            )
            for pattern in alternatives
        )
        for field, value in projection.items()
    }


def _required_product_semantic_audit(
    model_spec: Any,
    approved_spec: Any,
    semantics: list[dict[str, Any]],
) -> dict[str, Any]:
    semantic_ids = [item["id"] for item in semantics]
    assert semantic_ids
    assert len(semantic_ids) == len(set(semantic_ids))
    generated_projection = _product_semantic_projection(
        model_spec,
        sorted(
            {
                field
                for semantic in semantics
                for field in semantic["target_fields"]
            }
        ),
    )
    mismatches: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []
    for semantic in semantics:
        semantic_id = _audit_identifier(semantic["id"])
        target_fields = semantic["target_fields"]
        approved_projection = _product_semantic_projection(
            approved_spec,
            target_fields,
        )
        model_projection = _product_semantic_projection(model_spec, target_fields)
        approved_text = _product_semantic_projection_text(approved_projection)
        model_text = _product_semantic_projection_text(model_projection)
        approved_groups: list[bool] = []
        model_groups: list[bool] = []
        groups: list[dict[str, Any]] = []
        for group_index, alternatives in enumerate(
            semantic["all_of_any_patterns"]
        ):
            approved_group = any(
                re.search(pattern, approved_text, re.IGNORECASE)
                for pattern in alternatives
            )
            model_group = any(
                re.search(pattern, model_text, re.IGNORECASE)
                for pattern in alternatives
            )
            approved_groups.append(approved_group)
            model_groups.append(model_group)
            groups.append(
                {
                    "group_index": group_index,
                    "approved_match": approved_group,
                    "generated_match": model_group,
                    "approved_field_matches": (
                        _product_semantic_group_field_matches(
                            approved_projection,
                            alternatives,
                        )
                    ),
                    "generated_field_matches": (
                        _product_semantic_group_field_matches(
                            model_projection,
                            alternatives,
                        )
                    ),
                }
            )
        approved_match = all(approved_groups)
        model_match = all(model_groups)
        evaluations.append(
            {
                "semantic_id": semantic_id,
                "target_fields": list(target_fields),
                "approved_match": approved_match,
                "generated_match": model_match,
                "groups": groups,
            }
        )
        if approved_match and model_match:
            continue
        mismatches.append(
            {
                "semantic_id": semantic_id,
                "unmatched_group_indexes": [
                    index
                    for index, (approved_group, model_group) in enumerate(
                        zip(approved_groups, model_groups, strict=True)
                    )
                    if not approved_group or not model_group
                ],
                "approved_match": approved_match,
                "model_match": model_match,
            }
        )
    return {
        "generated_product_semantic_projection": generated_projection,
        "product_semantic_evaluations": evaluations,
        "mismatches": mismatches,
    }


def _assert_business_invariants(
    spec: Any,
    manifest: dict[str, Any],
    expected: dict[str, Any],
    generated_spec_oracle: dict[str, Any],
) -> None:
    """Check reviewed outcomes without prescribing the generated implementation."""

    contract = manifest["verification_contract"]
    assert {
        "id": manifest["id"],
        "version": manifest["version"],
        "capability": manifest["capability"],
        "deployment_scope": manifest["deployment_scope"],
    } == {
        "id": expected["id"],
        "version": expected["version"],
        "capability": expected["capability"],
        "deployment_scope": expected["deployment_scope"],
    }
    assert [item["name"] for item in manifest["requirements"]] == expected[
        "requirement_names"
    ]
    assert {
        item["label"]: item["presence"] for item in manifest["roles"]
    } == expected["role_presence"]
    assert {
        item["name"]: item["requiredness"]
        for item in manifest["requirements"]
    } == expected["requirement_requiredness"]
    policies = contract["observation_policies"]
    assert sorted({item["kind"] for item in policies}) == sorted(
        expected["observation_policy_kinds"]
    )
    referenced_policy_ids = {
        policy_id
        for extractor in contract["event_extractors"]
        for policy_id in extractor["observation_policy_ids"]
    }
    assert {item["id"] for item in policies} <= referenced_policy_ids

    def normalized_policies(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            (
                {
                    **item,
                    "key_fields": sorted(item["key_fields"]),
                }
                for item in values
            ),
            key=lambda item: item["id"],
        )

    def normalized_bindings(
        values: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return sorted(
            (
                {
                    "event_id": item["event_id"],
                    "observation_policy_ids": sorted(
                        item["observation_policy_ids"]
                    ),
                }
                for item in values
            ),
            key=lambda item: item["event_id"],
        )

    expected_policies = normalized_policies(
        generated_spec_oracle["observation_policies"]
    )
    expected_bindings = normalized_bindings(
        generated_spec_oracle["event_policy_bindings"]
    )
    actual_bindings = normalized_bindings(
        [
            {
                "event_id": item["id"],
                "observation_policy_ids": item["observation_policy_ids"],
            }
            for item in contract["event_extractors"]
        ]
    )
    assert normalized_policies(policies) == expected_policies
    assert actual_bindings == expected_bindings
    assert [item["id"] for item in contract["terminal_paths"]] == expected[
        "terminal_paths"
    ]
    terminal_paths = contract["terminal_paths"]
    assert sum(
        path["resolution_status"] == "COMPLETE" for path in terminal_paths
    ) >= 2
    assert any(
        path["resolution_status"] == "PARTIAL" for path in terminal_paths
    )
    fallback = terminal_paths[-1]
    assert fallback["resolution_status"] == "NONE"
    assert any(not branch["all_of"] for branch in fallback["condition"]["any_of"])

    rules = contract["rules"]
    rule_by_id = {rule["id"]: rule for rule in rules}
    rule_position = {rule["id"]: index for index, rule in enumerate(rules)}

    def assert_rule_reachable(rule_id: str, visiting: set[str]) -> None:
        assert rule_id in rule_by_id
        assert rule_id not in visiting
        rule = rule_by_id[rule_id]
        for dependency in rule["depends_on"]:
            assert dependency in rule_by_id
            assert rule_position[dependency] < rule_position[rule_id]
            assert_rule_reachable(dependency, {*visiting, rule_id})

    prior_branches: list[frozenset[tuple[str, str]]] = []
    for path in terminal_paths[:-1]:
        branches = path["condition"]["any_of"]
        branch_terms = [
            frozenset((term["rule_id"], term["result"]) for term in branch["all_of"])
            for branch in branches
        ]
        if path["resolution_status"] not in {"COMPLETE", "PARTIAL"}:
            prior_branches.extend(branch_terms)
            continue
        assert any(
            not any(prior <= current for prior in prior_branches)
            for current in branch_terms
        )
        for branch in branches:
            terms = branch["all_of"]
            assert terms
            for term in terms:
                assert_rule_reachable(term["rule_id"], set())
            assert any(
                term["result"] == "PASS"
                and rule_by_id[term["rule_id"]]["kind"]
                == "SEMANTIC_CAUSALITY"
                for term in terms
            )
        prior_branches.extend(branch_terms)

    assert spec.requires_logparse
    assert len(spec.roles) >= 2
    role_labels = {item.label for item in spec.roles}
    anchor_labels = {item["label"] for item in spec.logparse_plan["anchors"]}
    assert role_labels <= anchor_labels

    multiline_extractors = [
        item for item in contract["event_extractors"] if len(item["members"]) > 1
    ]
    if expected["requires_multiline_event"]:
        assert multiline_extractors
    numeric_rules = [
        item for item in contract["rules"] if item["kind"] == "NUMERIC_COMPARE"
    ]
    if expected["requires_numeric_compare"]:
        assert numeric_rules
    assert max(
        (
            item["parameters"].get("clock_tolerance_ms", 0)
            for item in numeric_rules
        ),
        default=0,
    ) == expected["requires_cross_clock_tolerance_ms"]


def _rule_result(status: ServerRuleStatus, *, semantic_result: str | None) -> str:
    if status is ServerRuleStatus.VERIFIED_PASS:
        return "PASS"
    if status is ServerRuleStatus.VERIFIED_FAIL:
        return "FAIL"
    if status is ServerRuleStatus.SEMANTIC_ONLY and semantic_result is not None:
        return semantic_result
    return "UNKNOWN"


def _audit_identifier(value: Any) -> str:
    assert isinstance(value, str) and _AUDIT_IDENTIFIER.fullmatch(value)
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _numeric_expression_audit(expression: dict[str, Any]) -> dict[str, Any]:
    kind = _audit_identifier(expression["kind"])
    if kind == "FIELD":
        return {
            "kind": kind,
            "event": _audit_identifier(expression["event"]),
            "field": _audit_identifier(expression["field"]),
        }
    if kind == "FACT":
        return {
            "kind": kind,
            "name": _audit_identifier(expression["name"]),
            "value_type": _audit_identifier(expression["value_type"]),
            "unit": (
                None
                if expression["unit"] is None
                else _audit_identifier(expression["unit"])
            ),
            "clock_domain": (
                None
                if expression["clock_domain"] is None
                else _audit_identifier(expression["clock_domain"])
            ),
        }
    if kind == "CONST":
        assert isinstance(expression["value"], int)
        return {
            "kind": kind,
            "value": expression["value"],
            "unit": _audit_identifier(expression["unit"]),
        }
    if kind in {"ADD", "SUBTRACT"}:
        return {
            "kind": kind,
            "left": _numeric_expression_audit(expression["left"]),
            "right": _numeric_expression_audit(expression["right"]),
        }
    if kind == "MULTIPLY_CONST":
        assert isinstance(expression["multiplier"], int)
        return {
            "kind": kind,
            "operand": _numeric_expression_audit(expression["operand"]),
            "multiplier": expression["multiplier"],
        }
    if kind == "CONVERT":
        return {
            "kind": kind,
            "operand": _numeric_expression_audit(expression["operand"]),
            "unit": _audit_identifier(expression["unit"]),
        }
    raise AssertionError("unsupported numeric expression in diagnostic audit")


def _join_audit(joins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "members": [
                {
                    "event": _audit_identifier(member["event"]),
                    "field": _audit_identifier(member["field"]),
                }
                for member in join["members"]
            ]
        }
        for join in joins
    ]


def _rule_structure_audit(rule: dict[str, Any]) -> dict[str, Any]:
    parameters = rule["parameters"]
    result: dict[str, Any] = {
        "kind": _audit_identifier(rule["kind"]),
        "depends_on": [
            _audit_identifier(item) for item in rule["depends_on"]
        ],
        "parameter_keys": sorted(
            _audit_identifier(item) for item in parameters
        ),
        "parameters_sha256": hashlib.sha256(
            _canonical_json_bytes(parameters)
        ).hexdigest(),
    }
    if rule["kind"] == "NUMERIC_COMPARE":
        result.update(
            {
                "left": _numeric_expression_audit(parameters["left"]),
                "operator": _audit_identifier(parameters["operator"]),
                "right": _numeric_expression_audit(parameters["right"]),
                "quantifier": _audit_identifier(parameters["quantifier"]),
                "joins": _join_audit(parameters["joins"]),
                "clock_tolerance_ms": parameters["clock_tolerance_ms"],
            }
        )
    return result


def _evaluation_issue_codes(
    rule: dict[str, Any], evaluation: Any
) -> list[str]:
    if not evaluation.issues:
        return []
    if evaluation.status is ServerRuleStatus.NOT_APPLICABLE:
        return ["DEPENDENCY_NOT_APPLICABLE"]
    if rule["kind"] == "NUMERIC_COMPARE":
        return ["NUMERIC_COMPARISON_UNSATISFIED_OR_UNCERTAIN"]
    if rule["kind"] == "SEMANTIC_CAUSALITY":
        return ["SEMANTIC_PREREQUISITES_MISSING"]
    if evaluation.status is ServerRuleStatus.UNVERIFIABLE:
        return ["RULE_EVALUATION_UNVERIFIABLE"]
    if evaluation.status is ServerRuleStatus.VERIFIED_FAIL:
        return ["RULE_EVALUATION_VERIFIED_FAIL"]
    return ["UNMAPPED_EVALUATION_ISSUE"]


def _evaluation_diagnostic_audit(
    rule: dict[str, Any],
    evaluation: Any,
    *,
    evaluated: dict[str, Any],
    rule_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    blocked_dependencies: list[dict[str, Any]] = []
    for dependency_id in rule["depends_on"]:
        dependency = evaluated[dependency_id]
        if (
            dependency.status
            in {
                ServerRuleStatus.VERIFIED_PASS,
                ServerRuleStatus.SEMANTIC_ONLY,
            }
            and not dependency.issues
        ):
            continue
        blocked_dependencies.append(
            {
                "rule_id": _audit_identifier(dependency_id),
                "status": _audit_identifier(dependency.status.value),
                "issues": _evaluation_issue_codes(
                    rule_by_id[dependency_id], dependency
                ),
            }
        )
    return {
        "blocked_dependencies": blocked_dependencies,
        "event_observations": [
            {
                "event_id": _audit_identifier(item.event_id),
                "observed_count": item.observed_count,
                "count_is_lower_bound": item.count_is_lower_bound,
            }
            for item in evaluation.event_observations
        ],
    }


def _scenario_audit_path() -> Path:
    configured = os.environ.get("S08_REAL_SKILL_GENERATION_AUDIT_PATH")
    assert configured, "S08_REAL_SKILL_GENERATION_AUDIT_PATH is required"
    destination = Path(configured).resolve()
    assert destination.name == SCENARIO_AUDIT_FILE
    assert destination.parent.is_dir()
    assert not destination.exists()
    return destination


def _write_scenario_audit(destination: Path, audit: dict[str, Any]) -> None:
    payload = _canonical_json_bytes(audit)
    assert len(payload) <= SCENARIO_AUDIT_MAX_BYTES
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{SCENARIO_AUDIT_FILE}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        assert not destination.exists()
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _terminal_path_condition_audit(
    path: dict[str, Any],
    results: dict[str, str],
) -> dict[str, Any]:
    branches = path["condition"]["any_of"]
    assert isinstance(branches, list)
    assert 1 <= len(branches) <= SCENARIO_AUDIT_MAX_PATH_BRANCHES
    branch_states: list[dict[str, Any]] = []
    total_term_count = 0
    for branch_index, branch in enumerate(branches):
        terms = branch["all_of"]
        assert isinstance(terms, list)
        assert len(terms) <= SCENARIO_AUDIT_MAX_BRANCH_TERMS
        audited_terms: list[dict[str, Any]] = []
        for term_index, term in enumerate(terms):
            rule_id = _audit_identifier(term["rule_id"])
            required_result = term["result"]
            assert required_result in {"PASS", "FAIL", "UNKNOWN"}
            actual_result = results.get(rule_id, "MISSING")
            assert actual_result in {"PASS", "FAIL", "UNKNOWN", "MISSING"}
            audited_terms.append(
                {
                    "term_index": term_index,
                    "rule_id": rule_id,
                    "required_result": required_result,
                    "actual_result": actual_result,
                    "matched": actual_result == required_result,
                }
            )
        mismatched_terms = [item for item in audited_terms if not item["matched"]]
        report_candidates = mismatched_terms or audited_terms
        branch_states.append(
            {
                "branch_index": branch_index,
                "branch_matched": not mismatched_terms,
                "term_count": len(audited_terms),
                "mismatch_count": len(mismatched_terms),
                "condition_terms_sha256": hashlib.sha256(
                    _canonical_json_bytes(terms)
                ).hexdigest(),
                "reported_terms_kind": (
                    "MISMATCHED"
                    if mismatched_terms
                    else ("MATCHED" if audited_terms else "EMPTY")
                ),
                "_report_candidates": report_candidates,
                "_reported_terms": [],
            }
        )
        total_term_count += len(audited_terms)

    remaining = SCENARIO_AUDIT_MAX_REPORTED_TERMS
    for candidate_index in range(SCENARIO_AUDIT_MAX_REPORTED_TERMS_PER_BRANCH):
        for state in branch_states:
            candidates = state["_report_candidates"]
            if remaining and candidate_index < len(candidates):
                state["_reported_terms"].append(candidates[candidate_index])
                remaining -= 1

    audited_branches: list[dict[str, Any]] = []
    for state in branch_states:
        candidates = state.pop("_report_candidates")
        reported_terms = state.pop("_reported_terms")
        audited_branches.append(
            {
                **state,
                "reported_terms": reported_terms,
                "reported_terms_truncated": len(reported_terms) < len(candidates),
            }
        )
    return {
        "condition_sha256": hashlib.sha256(
            _canonical_json_bytes(path["condition"])
        ).hexdigest(),
        "branch_count": len(branches),
        "term_count": total_term_count,
        "reported_term_count": (
            SCENARIO_AUDIT_MAX_REPORTED_TERMS - remaining
        ),
        "projection_complete": total_term_count
        == sum(len(item["reported_terms"]) for item in audited_branches),
        "any_of": audited_branches,
    }


def _terminal_path_mismatch_audit(
    *,
    scenario_id: str,
    expected_path_id: str,
    expected_resolution_status: str,
    terminal_paths: list[dict[str, Any]],
    matching_path_indices: list[int],
    results: dict[str, str],
) -> dict[str, Any]:
    scenario_id = _audit_identifier(scenario_id)
    expected_path_id = _audit_identifier(expected_path_id)
    expected_resolution_status = _audit_identifier(
        expected_resolution_status
    )
    assert 1 <= len(terminal_paths) <= SCENARIO_AUDIT_MAX_TERMINAL_PATHS
    assert len(matching_path_indices) <= SCENARIO_AUDIT_MAX_TERMINAL_PATHS
    assert matching_path_indices == sorted(set(matching_path_indices))
    assert all(0 <= index < len(terminal_paths) for index in matching_path_indices)

    generated_terminal_paths = [
        {
            "path_id": _audit_identifier(path["id"]),
            "resolution_status": _audit_identifier(path["resolution_status"]),
            "path_index": path_index,
        }
        for path_index, path in enumerate(terminal_paths)
    ]
    expected_path_indices = [
        item["path_index"]
        for item in generated_terminal_paths
        if item["path_id"] == expected_path_id
    ]
    assert len(expected_path_indices) <= 1
    expected_path_index = (
        expected_path_indices[0] if expected_path_indices else None
    )
    actual_path_index = (
        matching_path_indices[0] if matching_path_indices else None
    )
    actual_path = (
        terminal_paths[actual_path_index]
        if actual_path_index is not None
        else None
    )
    actual_path_id = (
        None if actual_path is None else _audit_identifier(actual_path["id"])
    )
    actual_resolution_status = (
        None
        if actual_path is None
        else _audit_identifier(actual_path["resolution_status"])
    )
    if actual_path is None:
        failure_reason = "NO_MATCHING_TERMINAL_PATH"
    elif actual_path_id != expected_path_id:
        failure_reason = "TERMINAL_PATH_ID_MISMATCH"
    else:
        assert actual_resolution_status != expected_resolution_status
        failure_reason = "TERMINAL_PATH_STATUS_MISMATCH"

    return {
        "scenario_id": scenario_id,
        "failure_reason": failure_reason,
        "expected_terminal_path_id": expected_path_id,
        "expected_resolution_status": expected_resolution_status,
        "actual_terminal_path_id": actual_path_id,
        "actual_resolution_status": actual_resolution_status,
        "expected_path_exists": expected_path_index is not None,
        "expected_path_index": expected_path_index,
        "actual_path_index": actual_path_index,
        "matching_terminal_paths": [
            generated_terminal_paths[index]
            for index in matching_path_indices
        ],
        "generated_terminal_paths": generated_terminal_paths,
        "expected_generated_path_dnf": (
            None
            if expected_path_index is None
            else _terminal_path_condition_audit(
                terminal_paths[expected_path_index], results
            )
        ),
    }


def _scenario_lines(
    case_root: Path,
    scenario_ref: dict[str, Any],
    driver: dict[str, Any],
) -> list[_ResolvedLine]:
    result: list[_ResolvedLine] = []
    scenario_root = (case_root / str(scenario_ref["driver"])).parent
    attachments = list(driver["attachment_files"])
    anchors = list(driver["attachment_anchor_names"])
    assert len(attachments) == len(anchors)
    for source_index, (relative, anchor) in enumerate(
        zip(attachments, anchors, strict=True),
        start=1,
    ):
        source = scenario_root / str(relative)
        binding = EvidenceBinding(
            existing_evidence_id=(
                f"00000000-0000-0000-0000-{source_index:012d}"
            ),
            evidence_proposal_key=None,
        )
        for line_number, raw_line in enumerate(
            source.read_bytes().splitlines(keepends=True),
            start=1,
        ):
            result.append(
                _ResolvedLine(
                    binding=binding,
                    anchor=str(anchor),
                    source_key=source.as_posix(),
                    relative_path=str(relative),
                    line_number=line_number,
                    raw_line=raw_line,
                    text=raw_line.rstrip(b"\r\n").decode("utf-8"),
                )
            )
    return result


def _assert_scenario_oracles(
    case_root: Path,
    descriptor: dict[str, Any],
    contract: dict[str, Any],
    expected_skill: dict[str, Any],
    *,
    audit_path: Path,
    generated_output: dict[str, Any],
) -> None:
    extractors = contract["event_extractors"]
    extractor_by_id = {item["id"]: item for item in extractors}
    multiline_ids = {
        item["id"] for item in extractors if len(item["members"]) > 1
    }
    multiline_matched = False
    mismatches: list[dict[str, Any]] = []
    evaluated_scenario_count = 0
    for scenario_ref in descriptor["scenarios"]:
        scenario_id = _audit_identifier(scenario_ref["scenario_id"])
        driver = json.loads(
            (case_root / scenario_ref["driver"]).read_text(encoding="utf-8")
        )
        oracle = json.loads(
            (case_root / scenario_ref["oracle"]).read_text(encoding="utf-8")
        )
        facts = {
            name: [
                SimpleNamespace(
                    item_id=f"00000000-0000-0000-0001-{index:012d}",
                    statement=value,
                )
            ]
            for index, (name, value) in enumerate(
                zip(
                    driver["initial_user_fact_names"]
                    + driver["supplement_input_names"],
                    driver["initial_user_fact_values"]
                    + driver["supplement_input_values"],
                    strict=True,
                )
            )
        }
        events, scan_complete = _extract_events(
            extractors,
            _scenario_lines(case_root, scenario_ref, driver),
            set(),
            facts,
        )
        assert set(events) == set(extractor_by_id)
        assert set(scan_complete) == set(extractor_by_id)
        assert all(scan_complete.values())
        evaluated: dict[str, Any] = {}
        results: dict[str, str] = {}
        required = oracle["required_rule_results"]
        for rule in contract["rules"]:
            evaluation = _evaluate_rule(
                rule,
                events=events,
                event_scan_complete=scan_complete,
                extractor_by_id=extractor_by_id,
                facts=facts,
                prior=evaluated,
            )
            evaluated[rule["id"]] = evaluation
            semantic_result = (
                required.get(rule["id"])
                if evaluation.status is ServerRuleStatus.SEMANTIC_ONLY
                and not evaluation.issues
                else None
            )
            results[rule["id"]] = _rule_result(
                evaluation.status,
                semantic_result=semantic_result,
            )
        evaluated_scenario_count += 1
        scenario_mismatches: list[dict[str, Any]] = []
        rule_by_id = {rule["id"]: rule for rule in contract["rules"]}
        for rule_id, expected_result in required.items():
            rule_id = _audit_identifier(rule_id)
            actual_result = results.get(rule_id)
            if actual_result == expected_result:
                continue
            if rule_id not in evaluated:
                scenario_mismatches.append(
                    {
                        "scenario_id": scenario_id,
                        "rule_id": rule_id,
                        "expected": _audit_identifier(expected_result),
                        "actual": "MISSING",
                        "evaluation_status": "MISSING",
                        "issues": ["RULE_NOT_GENERATED"],
                        "rule_structure": None,
                    }
                )
                continue
            evaluation = evaluated[rule_id]
            scenario_mismatches.append(
                {
                    "scenario_id": scenario_id,
                    "rule_id": rule_id,
                    "expected": _audit_identifier(expected_result),
                    "actual": _audit_identifier(actual_result),
                    "evaluation_status": _audit_identifier(
                        evaluation.status.value
                    ),
                    "issues": _evaluation_issue_codes(
                        rule_by_id[rule_id], evaluation
                    ),
                    "rule_structure": _rule_structure_audit(
                        rule_by_id[rule_id]
                    ),
                    "evaluation_diagnostic": _evaluation_diagnostic_audit(
                        rule_by_id[rule_id],
                        evaluation,
                        evaluated=evaluated,
                        rule_by_id=rule_by_id,
                    ),
                }
            )
        mismatches.extend(scenario_mismatches)
        assert len(mismatches) <= SCENARIO_AUDIT_MAX_MISMATCHES
        if scenario_mismatches:
            _write_scenario_audit(
                audit_path,
                {
                    "schema_version": 1,
                    "status": "FAIL",
                    "diagnostic_kind": "SCENARIO_RULE_MISMATCH",
                    "generated_output": generated_output,
                    "scenario_count": len(descriptor["scenarios"]),
                    "evaluated_scenario_count": evaluated_scenario_count,
                    "mismatches": mismatches,
                },
            )
            assert not scenario_mismatches, (
                f"scenario rule mismatch; audit={SCENARIO_AUDIT_FILE}; "
                f"mismatch_count={len(mismatches)}"
            )
        terminal_paths = contract["terminal_paths"]
        matching_path_indices = [
            path_index
            for path_index, path in enumerate(terminal_paths)
            if terminal_path_matches(path, results)
        ]
        matching_paths = [terminal_paths[index] for index in matching_path_indices]
        selected = matching_paths[0] if matching_paths else None
        expected_path_id = _audit_identifier(oracle["terminal_path_id"])
        expected_resolution_status = _audit_identifier(
            oracle["resolution_status"]
        )
        terminal_path_mismatch = (
            selected is None
            or selected["id"] != expected_path_id
            or selected["resolution_status"] != expected_resolution_status
        )
        if terminal_path_mismatch:
            terminal_path_diagnostic = _terminal_path_mismatch_audit(
                scenario_id=scenario_id,
                expected_path_id=expected_path_id,
                expected_resolution_status=expected_resolution_status,
                terminal_paths=terminal_paths,
                matching_path_indices=matching_path_indices,
                results=results,
            )
            _write_scenario_audit(
                audit_path,
                {
                    "schema_version": 1,
                    "status": "FAIL",
                    "diagnostic_kind": "SCENARIO_TERMINAL_PATH_MISMATCH",
                    "generated_output": generated_output,
                    "scenario_count": len(descriptor["scenarios"]),
                    "evaluated_scenario_count": evaluated_scenario_count,
                    "mismatches": mismatches,
                    "terminal_path_mismatch": terminal_path_diagnostic,
                },
            )
            raise AssertionError(
                f"terminal path mismatch; audit={SCENARIO_AUDIT_FILE}; "
                f"reason={terminal_path_diagnostic['failure_reason']}"
            )
        assert selected is not None

        for event_id, matches in events.items():
            if extractor_by_id[event_id]["observation_policy_ids"] and matches:
                audits = [
                    observation
                    for evaluation in evaluated.values()
                    for observation in evaluation.event_observations
                    if observation.event_id == event_id
                ]
                assert audits
                assert all(item.count_is_lower_bound for item in audits)

        multiline_matched = multiline_matched or any(
            len(match.lines) > 1
            for event_id in multiline_ids
            for match in events[event_id]
        )

        if oracle["resolution_status"] in {"PARTIAL", "NONE"}:
            lossy_missing_events = {
                event_id
                for event_id, matches in events.items()
                if not matches
                and extractor_by_id[event_id]["observation_policy_ids"]
            }
            assert lossy_missing_events
            lossy_unknown_audits = []
            for rule in contract["rules"]:
                if rule["kind"] not in {"EVENT_COUNT", "EVENT_PRESENT"}:
                    continue
                event_id = rule["parameters"]["event"]
                evaluation = evaluated[rule["id"]]
                if (
                    event_id in lossy_missing_events
                    and evaluation.status is ServerRuleStatus.UNVERIFIABLE
                    and results[rule["id"]] == "UNKNOWN"
                ):
                    lossy_unknown_audits.extend(
                        observation
                        for observation in evaluation.event_observations
                        if observation.event_id == event_id
                        and observation.count_is_lower_bound
                    )
            assert lossy_unknown_audits

    audit = {
        "schema_version": 1,
        "status": "PASS",
        "diagnostic_kind": "NONE",
        "generated_output": generated_output,
        "scenario_count": len(descriptor["scenarios"]),
        "evaluated_scenario_count": evaluated_scenario_count,
        "mismatches": mismatches,
    }
    if expected_skill["requires_multiline_event"]:
        assert multiline_matched
    _write_scenario_audit(audit_path, audit)


def test_real_conversion_agent_builds_an_executable_reviewed_skill_from_plain_wiki(
    tmp_path: Path,
    isolated_agent_workspace_root: Path,
) -> None:
    if os.environ.get("S08_REAL_SKILL_GENERATION_GATE") != "1":
        pytest.skip("requires the explicitly selected real Skill-generation gate")
    command = os.environ.get("S08_REAL_SKILL_GENERATION_AGENT_COMMAND")
    assert command, "S08_REAL_SKILL_GENERATION_AGENT_COMMAND is required"
    scenario_audit_path = _scenario_audit_path()

    case_root = _release_case_root()
    descriptor = json.loads((case_root / "case.json").read_text(encoding="utf-8"))
    workspace = isolated_agent_workspace_root / "workspace"
    inputs = workspace / "inputs"
    runtime = workspace / "runtime"
    output = workspace / "output"
    inputs.mkdir(parents=True)
    (runtime / "tool-state").mkdir(parents=True)
    output.mkdir()
    resolved_workspace = workspace.resolve()
    resolved_repo = ROOT.resolve()
    assert resolved_workspace != resolved_repo
    assert resolved_repo not in resolved_workspace.parents
    wiki = (case_root / descriptor["input_wiki"]).read_bytes()
    clarifications = (case_root / descriptor["clarifications"]).read_bytes()
    (inputs / "wiki.md").write_bytes(wiki)
    (inputs / "clarifications.md").write_bytes(clarifications)

    stdout = _Sink()
    stderr = _Sink()
    prompt = """Use the wiki-to-diagnosis-skill Skill to convert this reviewed plain Markdown Wiki into one executable GenerationSpec v6.

Your first action must call the Skill tool with exactly {"skill":"wiki-to-diagnosis-skill"}. After it succeeds, take the actual absolute directory shown after `Base directory for this skill:` in the Skill result. Read inputs/wiki.md and inputs/clarifications.md from the current workspace. For each required Skill reference, join that returned absolute directory with references/generation-spec-v6-reference.md or references/verification-contract-v2-reference.md before calling Read. Never pass a bare references/... path to Read or resolve it against the workspace cwd. Among Skill resources, read only the references explicitly linked by the loaded Skill. The clarifications are authoritative author confirmations for every role and Wiki parameter definition. Do not read repository source, generator or validator implementations, tests, case oracles, or any other path. Do not ask questions, use the network, or invent a platform log prefix. Treat both (# ... #) and （# ... #） as conversion-only author notes that must not enter any product field.

Construct the complete JSON object before starting any file mutation. Only after every required Read has completed, call Write exactly once with both `file_path` and non-empty `content` in that same tool input; set `file_path` to output/generation-spec.json and put the entire JSON object in `content`. Do not call Write with missing or empty arguments, do not split the object across writes, and do not use Bash or another file-writing tool. Write no JSON in the final response.

    Before Write, perform the exact per-reference event-field inventory required by section 9.1 of the loaded verification reference. Recursively enumerate every rule and numeric-expression (event, field) pair, and verify the field belongs to that exact event; a field existing only on another event is invalid. For every FIELDS_EQUAL member, use the field actually declared by that member's own event; the two sides may and often do use different field names, so never copy the first member's field name onto the second event merely to express equality. Keep this inventory internal and do not write it as a second artifact.

    Also perform section 9.2's internal positive-witness evaluation before Write. For every non-fallback COMPLETE or PARTIAL path, use only stable log message bodies and confirmed facts from the Wiki/clarifications; apply the actual final line_pattern, match_mode, multiline grouping and selectors, require nonzero events, then evaluate the required mechanical dependency closure in declaration order. No required dependency may become FAIL, UNKNOWN or NOT_APPLICABLE, and every FIELDS_EQUAL/join must have one occurrence tuple whose member values actually match. Do not write if a positive witness cannot be constructed from the supplied source material; do not invent logs, read tests/oracles, or emit the witness as another artifact.

The one UTF-8 JSON object must satisfy the loaded Skill's GenerationSpec v6 contract and preserve the Wiki's multiple contributors, lossy observation policies, multiline record, explicit clock tolerance, COMPLETE/PARTIAL/NONE paths, fixed-snapshot boundary, and timeout-not-cancellation safety meaning. Create no other output file or directory. After the single Write succeeds, stop.
"""
    try:
        execution = AgentBackend(command).execute(
            prompt=prompt,
            workspace_root=workspace,
            cancellation=_Signal(),
            log_sinks=ExecutionLogSinks(
                stdout=stdout,
                stderr=stderr,
                combined_limit_bytes=JOB_STDOUT_STDERR_BYTES,
            ),
            resource_limits=default_resource_limits(JobType.DIAGNOSE),
            test_limits=BackendExecutionLimits(
                wall_time_seconds=float(
                    os.environ["TEST_FLOW_AGENT_BACKEND_WALL_TIME_SECONDS"]
                ),
                stdout_stderr_bytes=8 * 1024 * 1024,
                workspace_bytes=16 * 1024 * 1024,
                poll_interval_seconds=0.02,
                termination_grace_seconds=5.0,
            ),
        )
    except RuntimeExecutionError as exc:
        pytest.fail(
            f"Wiki conversion Agent failed with {exc.failure.code.value}; "
            f"stdout={bytes(stdout.data).decode('utf-8', 'replace')!r}; "
            f"stderr={bytes(stderr.data).decode('utf-8', 'replace')!r}"
        )
    assert execution.returncode == 0
    assert sorted(path.name for path in output.iterdir()) == [
        "generation-spec.json"
    ]
    assert (inputs / "wiki.md").read_bytes() == wiki
    assert (inputs / "clarifications.md").read_bytes() == clarifications

    generator = _load_module(GENERATOR_PATH, "_real_wiki_generator_v6")
    validator = _load_module(VALIDATOR_PATH, "_real_wiki_validator_v6")
    model_spec = generator.load_generation_spec(output / "generation-spec.json")
    compiled_root = tmp_path / "compiled"
    generated = generator.generate_diagnosis_skill(model_spec, compiled_root)
    validation = validator.validate_skill_directory(generated.skill_dir)
    assert validation.ok, validation.errors

    semantic_oracle = json.loads(
        (case_root / descriptor["semantic_oracle"]).read_text(encoding="utf-8")
    )
    expected = semantic_oracle["expected_skill"]
    manifest = json.loads(
        (generated.skill_dir / "diagnosis-skill.json").read_text(encoding="utf-8")
    )
    contract = manifest["verification_contract"]
    generated_spec_oracle = semantic_oracle["generated_spec_oracle"]
    assert generated_spec_oracle["projection_version"] == 4
    _assert_business_invariants(
        model_spec,
        manifest,
        expected,
        generated_spec_oracle,
    )
    product_text = "\n".join(
        (generated.skill_dir / name).read_text(encoding="utf-8")
        for name in ("SKILL.md", "diagnosis-skill.json")
    )
    for marker in semantic_oracle["author_note_markers_forbidden_in_product"]:
        assert marker not in product_text

    approved_spec = generator.load_generation_spec(
        case_root / descriptor["generation_spec"]
    )
    approved_product = generator.render_product(approved_spec)
    generated_spec_path = output / "generation-spec.json"
    generated_output = {
        "logical_path": "workspace/output/generation-spec.json",
        "sha256": hashlib.sha256(generated_spec_path.read_bytes()).hexdigest(),
        "size_bytes": generated_spec_path.stat().st_size,
        "load_status": "PASS",
        "compile_status": "PASS",
        "validation_status": "PASS",
    }
    semantic_audit = _required_product_semantic_audit(
        model_spec,
        approved_spec,
        generated_spec_oracle["required_product_semantics"],
    )
    semantic_mismatches = semantic_audit["mismatches"]
    if semantic_mismatches:
        assert len(semantic_mismatches) <= SCENARIO_AUDIT_MAX_MISMATCHES
        _write_scenario_audit(
            scenario_audit_path,
            {
                "schema_version": 1,
                "status": "FAIL",
                "diagnostic_kind": "PRODUCT_SEMANTIC_MISMATCH",
                "generated_output": generated_output,
                "scenario_count": len(descriptor["scenarios"]),
                "evaluated_scenario_count": 0,
                **semantic_audit,
            },
        )
        raise AssertionError(
            f"product semantic mismatch; audit={SCENARIO_AUDIT_FILE}"
        )
    _assert_scenario_oracles(
        case_root,
        descriptor,
        contract,
        expected,
        audit_path=scenario_audit_path,
        generated_output=generated_output,
    )
    approved_root = case_root / descriptor["approved_skill_dir"]
    approved_files = {
        name: (approved_root / name).read_bytes()
        for name in sorted(approved_product)
    }
    assert approved_files == approved_product
