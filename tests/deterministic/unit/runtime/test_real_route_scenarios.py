from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from problem_locator.contracts import (
    RouteKind, WorkspaceInputManifest, bytes_sha256, canonical_json_bytes,
)
from problem_locator.runtime.final_response import parse_route_response
from problem_locator.runtime.route_admission import ROUTE_ADMISSION_POLICY
from problem_locator.runtime.route_recall import recall_route_candidates


ROOT = Path(__file__).resolve().parents[4]
GATE = runpy.run_path(str(ROOT / "tests/real/agent/test_real_route_agent_contract_gate.py"))


@pytest.mark.parametrize("scenario", GATE["_SCENARIOS"])
def test_real_route_scenarios_use_the_shortlisted_contract_without_model_calls(scenario, monkeypatch):
    def forbid_model_call(*args, **kwargs):
        pytest.fail("场景合同检查不得调用真实模型。")

    monkeypatch.setattr(GATE["AgentBackend"], "execute", forbid_model_call)
    gate_config = json.loads((ROOT / "tools/test-flow/config/gates.v2.json").read_bytes())
    route_gate = gate_config["gates"]["real.agent.route"]
    assert len(GATE["_SCENARIOS"]) == route_gate["min_passed"] == route_gate["isolated_agent_invocations"] == 5

    job, skill_index, expected_kind = GATE["_route_scenario"](scenario)
    candidates = recall_route_candidates(job, skill_index=skill_index)
    assert 1 <= len(candidates.skill_ids) <= 5
    model_index = json.loads(candidates.model_index)
    assert model_index["schema_version"] == 1
    assert [item["skill_id"] for item in model_index["skills"]] == list(candidates.skill_ids)
    for item in model_index["skills"]:
        assert set(item) == {"skill_id", "name", "capability", "summary", "routing"}
    if scenario == "ambiguous-candidates":
        assert len(candidates.skill_ids) == 2

    manifest = WorkspaceInputManifest(
        schema_version=2, job_id=job.job_id, case_id=job.case_id, job_type=job.job_type,
        logparse_tool_ref=None, logparse_product=None, entries=[],
        resolved_logparse_plan=None, review_subject=None,
    )
    context = GATE["_route_context"](job, candidates, manifest)
    assert candidates.model_index in context.body
    assert skill_index not in context.body

    selected_id = job.available_skill_refs[0].id if expected_kind is RouteKind.MATCHED else None
    response = canonical_json_bytes({
        "skill_id": selected_id, "reason": "按本场景选择定位能力。", "confidence": 0.99,
    }).decode("utf-8")
    validated = parse_route_response(response, job, skill_index=skill_index, candidates=candidates)
    assert validated.draft.payload.kind is expected_kind
    assert validated.route_admission["policy"] == ROUTE_ADMISSION_POLICY
    assert validated.route_admission["effective_skill_id"] == selected_id
    assert validated.route_admission["retrieval"] == candidates.audit
    assert candidates.audit["model_index_sha256"] == bytes_sha256(candidates.model_index.encode("utf-8"))
    assert validated.route_admission["reason_code"] == (
        "ADMITTED" if expected_kind is RouteKind.MATCHED else "MODEL_NO_MATCH"
    )
    assert "candidate_results" not in validated.route_admission
    assert "assessments" not in validated.route_admission
