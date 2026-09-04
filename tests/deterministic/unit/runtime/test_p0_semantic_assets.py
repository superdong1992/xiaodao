from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from problem_locator.contracts import MethodEvaluationPlanV2
from problem_locator.runtime.catalog import _BUILTIN_SPECS_BY_ID
from problem_locator.runtime.methods_evaluation_v2 import (
    MethodEvaluationResponseError,
    parse_method_evaluation_response_v2,
)
from problem_locator.runtime.methods_evidence_v2 import (
    build_method_evaluation_plan_v2,
    scan_method_evidence_v2,
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

ASSET_ROOT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "problem_locator"
    / "runtime"
    / "assets"
)


def _asset(relative: str) -> tuple[dict[str, object], str]:
    root = ASSET_ROOT / relative
    metadata = json.loads((root / "asset.json").read_text(encoding="utf-8"))
    content = (root / metadata["entry"]).read_text(encoding="utf-8")
    return metadata, content


def _production_plan() -> MethodEvaluationPlanV2:
    role = RuntimeRoleBindingV1("profile", "tools", "policy", "output")
    skill = ResolvedSpecializedSkillV1(
        registration_root=Path("registration"),
        package_root=Path("package"),
        registration=RegistrationTemplateV1(
            registration_id="agent-contract-test",
            version="1.0.0",
            capability="test",
            deployment_scope="PRODUCTION",
            summary="test",
            package_relative_path="package/agent-contract-test",
            skill_name="agent-contract-test",
            source_wiki_sha256="1" * 64,
            diagnose=role,
            review=role,
            preprocessing=PreprocessingBindingV1(False, None, (), None),
        ),
        methods=MethodsManifestV1(
            skill_name="agent-contract-test",
            source_wiki_sha256="1" * 64,
            required_user_inputs=(),
            required_artifacts=(),
            log_derived_fields=("request_id",),
            shared_references=(),
            methods=(
                MethodCardV1(
                    id="timeout",
                    title="Timeout",
                    reference="references/timeout.md",
                    priority=1,
                    evidence_markers=("TIMEOUT",),
                    activation_markers=("TIMEOUT",),
                ),
            ),
        ),
        registration_sha256="2" * 64,
        package_tree_sha256="3" * 64,
        combined_sha256="4" * 64,
    )
    content = b"TIMEOUT request_id=req-1\n"
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
    return build_method_evaluation_plan_v2(skill=skill, evidence=graph)


def test_methods_v1_asset_versions_match_the_builtin_catalog() -> None:
    expected = {
        "tool-bundle/router": "3.0.0",
        "output-contract/route": "5.0.0",
        "agent-profile/specialist": "7.0.0",
        "agent-profile/reviewer": "7.0.0",
        "context-policy/review": "3.0.0",
        "output-contract/diagnose": "10.0.0",
        "output-contract/review": "10.0.0",
    }

    for asset_id, version in expected.items():
        assert _BUILTIN_SPECS_BY_ID[asset_id].version == version


def test_router_writes_one_server_finalized_draft_without_a_tool_round_trip() -> None:
    contract_meta, contract = _asset("output-contracts/route")
    tool_meta, tool_bundle_text = _asset("tool-bundles/router")
    tool_bundle = json.loads(tool_bundle_text)

    assert contract_meta["version"] == "5.0.0"
    assert tool_meta["version"] == "3.0.0"
    assert tool_bundle == {"schema_version": 1, "tools": []}
    assert "then exit without invoking another tool" in contract
    assert "problem-locator-seal-outcome-draft" not in contract
    assert "independently parses, validates" in contract


def test_specialist_assets_require_grounded_methods_v1_output() -> None:
    profile_meta, profile = _asset("profiles/specialist")
    contract_meta, contract = _asset("output-contracts/diagnose")
    tool_meta, tool_bundle = _asset("tool-bundles/diagnose")

    assert profile_meta["version"] == "7.0.0"
    assert contract_meta["version"] == "10.0.0"
    assert tool_meta["version"] == "4.0.0"
    assert "SPECIALIST" in profile
    assert "authoritative target logs" in profile
    assert "output/method-diagnosis.draft.json" in contract
    assert "inputs/request.json" in contract
    assert "inputs/target_logs.json" in contract
    assert "inputs/logparse-receipt.json" in contract
    assert "confirmed_methods" in contract
    assert "candidate_methods" in contract
    assert "identity_tokens" in contract
    assert "line_number" in contract
    assert "CONFIRMED" in contract
    assert "PARTIAL" in contract
    assert "INSUFFICIENT" in contract
    assert "output/job_outcome.draft.json" in contract
    assert "Candidate, Outcome, JSON, or ZIP" in contract
    assert "problem-locator-logparse" not in tool_bundle
    assert "problem-locator-seal-outcome-draft" not in tool_bundle


def test_reviewer_assets_require_independent_methods_v1_review() -> None:
    profile_meta, profile = _asset("profiles/reviewer")
    contract_meta, contract = _asset("output-contracts/review")
    policy_meta, policy = _asset("context-policies/review")
    tool_meta, tool_bundle = _asset("tool-bundles/review")

    assert profile_meta["version"] == "7.0.0"
    assert contract_meta["version"] == "10.0.0"
    assert policy_meta["version"] == "3.0.0"
    assert tool_meta["version"] == "3.0.0"
    assert "REVIEWER" in profile
    assert "Do not continue the Specialist" in profile
    assert "output/method-review.draft.json" in contract
    assert "inputs/method-diagnosis.json" in contract
    assert "inputs/method-grounding-audit.json" in contract
    assert "identity_tokens" in contract
    assert "NEED_MORE_EVIDENCE" in contract
    assert "fixed Candidate review target" in policy
    assert "problem-locator-logparse" not in tool_bundle
    assert "problem-locator-seal-outcome-draft" not in tool_bundle


def test_asset_response_shape_is_accepted_by_production_v2_parser() -> None:
    plan = _production_plan()
    response = [
        {
            "evaluation_ref": item.evaluation_ref,
            "verdict": "CONFIRMED",
            "supporting_event_refs": list(item.evidence_event_refs),
            "reason": "The method confirmation rule is satisfied.",
        }
        for item in plan.evaluations
    ]

    parsed = parse_method_evaluation_response_v2(plan=plan, response=response)

    assert tuple(item.evaluation_ref for item in parsed) == tuple(
        item.evaluation_ref for item in plan.evaluations
    )
    assert all(
        set(item.model_dump(mode="json"))
        == {"evaluation_ref", "verdict", "supporting_event_refs", "reason"}
        for item in parsed
    )


def test_asset_response_rejects_one_added_field_from_production_baseline() -> None:
    plan = _production_plan()
    response = [
        {
            "evaluation_ref": item.evaluation_ref,
            "verdict": "CONFIRMED",
            "supporting_event_refs": list(item.evidence_event_refs),
            "reason": "The method confirmation rule is satisfied.",
        }
        for item in plan.evaluations
    ]
    mutated = copy.deepcopy(response)
    mutated[0]["marker"] = "TIMEOUT"

    with pytest.raises(MethodEvaluationResponseError):
        parse_method_evaluation_response_v2(plan=plan, response=mutated)
