from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[4]
GATE_PATH = ROOT / "tests/real/agent/test_real_wiki_skill_generation_gate.py"


@pytest.fixture(scope="module")
def gate() -> Any:
    module_name = "_real_wiki_skill_generation_gate_audit_helpers"
    spec = importlib.util.spec_from_file_location(module_name, GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_product_semantic_projection_preserves_only_whitelisted_prose(
    gate: Any,
) -> None:
    generated = SimpleNamespace(
        summary="Generated summary",
        judgement_rules=("First judgement", "Second judgement"),
        verification_contract={"facts": ["never-seal-fact"]},
    )

    assert gate._product_semantic_projection(
        generated,
        ["summary", "judgement_rules"],
    ) == {
        "summary": "Generated summary",
        "judgement_rules": ["First judgement", "Second judgement"],
    }
    with pytest.raises(AssertionError):
        gate._product_semantic_projection(
            generated,
            ["verification_contract"],
        )


def test_product_semantic_failure_audit_is_field_attributed_and_bounded(
    gate: Any,
) -> None:
    generated = SimpleNamespace(
        judgement_rules=("Timeout is not cancellation.",),
        output_requirements=("Report the available evidence.",),
        verification_contract={"facts": ["never-seal-fact"]},
        logparse_plan={"fixture": "never-seal-log"},
    )
    approved = SimpleNamespace(
        judgement_rules=(
            "Timeout is not cancellation.",
            "Later execution may continue.",
        ),
        output_requirements=("never-seal-approved-prose",),
    )
    semantics = [
        {
            "id": "timeout_safety",
            "target_fields": ["judgement_rules", "output_requirements"],
            "all_of_any_patterns": [
                [r"not\s+cancellation"],
                [r"later\s+execution"],
            ],
        }
    ]

    audit = gate._required_product_semantic_audit(
        generated,
        approved,
        semantics,
    )

    assert audit["generated_product_semantic_projection"] == {
        "judgement_rules": ["Timeout is not cancellation."],
        "output_requirements": ["Report the available evidence."],
    }
    assert audit["mismatches"] == [
        {
            "semantic_id": "timeout_safety",
            "unmatched_group_indexes": [1],
            "approved_match": True,
            "model_match": False,
        }
    ]
    evaluation = audit["product_semantic_evaluations"][0]
    assert evaluation["approved_match"] is True
    assert evaluation["generated_match"] is False
    assert evaluation["groups"] == [
        {
            "group_index": 0,
            "approved_match": True,
            "generated_match": True,
            "approved_field_matches": {
                "judgement_rules": True,
                "output_requirements": False,
            },
            "generated_field_matches": {
                "judgement_rules": True,
                "output_requirements": False,
            },
        },
        {
            "group_index": 1,
            "approved_match": True,
            "generated_match": False,
            "approved_field_matches": {
                "judgement_rules": True,
                "output_requirements": False,
            },
            "generated_field_matches": {
                "judgement_rules": False,
                "output_requirements": False,
            },
        },
    ]

    failure_audit = {
        "schema_version": 1,
        "status": "FAIL",
        "diagnostic_kind": "PRODUCT_SEMANTIC_MISMATCH",
        **audit,
    }
    payload = gate._canonical_json_bytes(failure_audit)
    assert len(payload) <= gate.SCENARIO_AUDIT_MAX_BYTES
    decoded = json.loads(payload)
    serialized = json.dumps(decoded, ensure_ascii=False, sort_keys=True)
    for forbidden in (
        "never-seal-fact",
        "never-seal-log",
        "never-seal-approved-prose",
        r"not\s+cancellation",
        r"later\s+execution",
    ):
        assert forbidden not in serialized


def test_terminal_path_mismatch_audit_projects_only_generated_dnf_terms(
    gate: Any,
) -> None:
    terminal_paths = [
        {
            "id": "complete_queue",
            "resolution_status": "COMPLETE",
            "description": "never-seal-generated-prose",
            "condition": {
                "any_of": [
                    {
                        "all_of": [
                            {"rule_id": "queue_wait", "result": "PASS"},
                            {"rule_id": "upstream_cause", "result": "PASS"},
                        ],
                        "comment": "never-seal-branch-prose",
                    }
                ]
            },
        },
        {
            "id": "none",
            "resolution_status": "NONE",
            "condition": {"any_of": [{"all_of": []}]},
        },
    ]

    audit = gate._terminal_path_mismatch_audit(
        scenario_id="complete_case",
        expected_path_id="complete_queue",
        expected_resolution_status="COMPLETE",
        terminal_paths=terminal_paths,
        matching_path_indices=[1],
        results={
            "queue_wait": "PASS",
            "upstream_cause": "FAIL",
            "unreferenced_rule": "never-seal-result",
        },
    )

    assert audit == {
        "scenario_id": "complete_case",
        "failure_reason": "TERMINAL_PATH_ID_MISMATCH",
        "expected_terminal_path_id": "complete_queue",
        "expected_resolution_status": "COMPLETE",
        "actual_terminal_path_id": "none",
        "actual_resolution_status": "NONE",
        "expected_path_exists": True,
        "expected_path_index": 0,
        "actual_path_index": 1,
        "matching_terminal_paths": [
            {
                "path_id": "none",
                "resolution_status": "NONE",
                "path_index": 1,
            }
        ],
        "generated_terminal_paths": [
            {
                "path_id": "complete_queue",
                "resolution_status": "COMPLETE",
                "path_index": 0,
            },
            {
                "path_id": "none",
                "resolution_status": "NONE",
                "path_index": 1,
            },
        ],
        "expected_generated_path_dnf": {
            "condition_sha256": gate.hashlib.sha256(
                gate._canonical_json_bytes(terminal_paths[0]["condition"])
            ).hexdigest(),
            "branch_count": 1,
            "term_count": 2,
            "reported_term_count": 1,
            "projection_complete": False,
            "any_of": [
                {
                    "branch_index": 0,
                    "branch_matched": False,
                    "term_count": 2,
                    "mismatch_count": 1,
                    "condition_terms_sha256": gate.hashlib.sha256(
                        gate._canonical_json_bytes(
                            terminal_paths[0]["condition"]["any_of"][0][
                                "all_of"
                            ]
                        )
                    ).hexdigest(),
                    "reported_terms_kind": "MISMATCHED",
                    "reported_terms": [
                        {
                            "term_index": 1,
                            "rule_id": "upstream_cause",
                            "required_result": "PASS",
                            "actual_result": "FAIL",
                            "matched": False,
                        },
                    ],
                    "reported_terms_truncated": False,
                }
            ]
        },
    }
    serialized = json.dumps(audit, ensure_ascii=False, sort_keys=True)
    for forbidden in (
        "never-seal-generated-prose",
        "never-seal-branch-prose",
        "never-seal-result",
    ):
        assert forbidden not in serialized
    assert len(gate._canonical_json_bytes(audit)) <= gate.SCENARIO_AUDIT_MAX_BYTES


def test_terminal_path_mismatch_audit_handles_no_match_and_validates_ids(
    gate: Any,
) -> None:
    terminal_paths = [
        {
            "id": "complete",
            "resolution_status": "COMPLETE",
            "condition": {
                "any_of": [
                    {
                        "all_of": [
                            {"rule_id": "causal_rule", "result": "PASS"}
                        ]
                    }
                ]
            },
        }
    ]

    audit = gate._terminal_path_mismatch_audit(
        scenario_id="partial_case",
        expected_path_id="missing_expected_path",
        expected_resolution_status="PARTIAL",
        terminal_paths=terminal_paths,
        matching_path_indices=[],
        results={"causal_rule": "FAIL"},
    )

    assert audit["failure_reason"] == "NO_MATCHING_TERMINAL_PATH"
    assert audit["expected_path_exists"] is False
    assert audit["expected_path_index"] is None
    assert audit["actual_terminal_path_id"] is None
    assert audit["actual_resolution_status"] is None
    assert audit["actual_path_index"] is None
    assert audit["matching_terminal_paths"] == []
    assert audit["expected_generated_path_dnf"] is None

    invalid_paths = [dict(terminal_paths[0], id="unsafe path")]
    with pytest.raises(AssertionError):
        gate._terminal_path_mismatch_audit(
            scenario_id="partial_case",
            expected_path_id="missing_expected_path",
            expected_resolution_status="PARTIAL",
            terminal_paths=invalid_paths,
            matching_path_indices=[],
            results={"causal_rule": "FAIL"},
        )


def test_terminal_path_mismatch_audit_handles_status_only_mismatch(
    gate: Any,
) -> None:
    terminal_paths = [
        {
            "id": "complete",
            "resolution_status": "PARTIAL",
            "condition": {"any_of": [{"all_of": []}]},
        }
    ]

    audit = gate._terminal_path_mismatch_audit(
        scenario_id="complete_case",
        expected_path_id="complete",
        expected_resolution_status="COMPLETE",
        terminal_paths=terminal_paths,
        matching_path_indices=[0],
        results={},
    )

    assert audit["failure_reason"] == "TERMINAL_PATH_STATUS_MISMATCH"
    assert audit["actual_terminal_path_id"] == "complete"
    assert audit["actual_resolution_status"] == "PARTIAL"
    assert audit["expected_generated_path_dnf"]["projection_complete"] is True


def test_terminal_path_mismatch_audit_stays_bounded_at_contract_maxima(
    gate: Any,
) -> None:
    results: dict[str, str] = {}
    branches: list[dict[str, Any]] = []
    for branch_index in range(gate.SCENARIO_AUDIT_MAX_PATH_BRANCHES):
        terms = []
        for term_index in range(gate.SCENARIO_AUDIT_MAX_BRANCH_TERMS):
            suffix = f"{branch_index:02d}{term_index:03d}"
            rule_id = "r" + ("x" * (127 - len(suffix))) + suffix
            terms.append({"rule_id": rule_id, "result": "PASS"})
            results[rule_id] = "FAIL"
        branches.append({"all_of": terms})
    terminal_paths = [
        {
            "id": "expected",
            "resolution_status": "COMPLETE",
            "condition": {"any_of": branches},
        },
        *[
            {
                "id": f"candidate_{index}",
                "resolution_status": "PARTIAL",
                "condition": {"any_of": [{"all_of": []}]},
            }
            for index in range(1, gate.SCENARIO_AUDIT_MAX_TERMINAL_PATHS - 1)
        ],
        {
            "id": "none",
            "resolution_status": "NONE",
            "condition": {"any_of": [{"all_of": []}]},
        },
    ]

    mismatch = gate._terminal_path_mismatch_audit(
        scenario_id="maximal_case",
        expected_path_id="expected",
        expected_resolution_status="COMPLETE",
        terminal_paths=terminal_paths,
        matching_path_indices=[len(terminal_paths) - 1],
        results=results,
    )
    audit = {
        "schema_version": 1,
        "status": "FAIL",
        "diagnostic_kind": "SCENARIO_TERMINAL_PATH_MISMATCH",
        "generated_output": {
            "logical_path": "workspace/output/generation-spec.json",
            "sha256": "0" * 64,
            "size_bytes": 1,
            "load_status": "PASS",
            "compile_status": "PASS",
            "validation_status": "PASS",
        },
        "scenario_count": 1,
        "evaluated_scenario_count": 1,
        "mismatches": [],
        "terminal_path_mismatch": mismatch,
    }

    projection = mismatch["expected_generated_path_dnf"]
    assert projection["projection_complete"] is False
    assert projection["branch_count"] == gate.SCENARIO_AUDIT_MAX_PATH_BRANCHES
    assert projection["term_count"] == (
        gate.SCENARIO_AUDIT_MAX_PATH_BRANCHES
        * gate.SCENARIO_AUDIT_MAX_BRANCH_TERMS
    )
    assert projection["reported_term_count"] == gate.SCENARIO_AUDIT_MAX_REPORTED_TERMS
    assert len(gate._canonical_json_bytes(audit)) <= gate.SCENARIO_AUDIT_MAX_BYTES
