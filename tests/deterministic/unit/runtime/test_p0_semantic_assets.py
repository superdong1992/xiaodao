from __future__ import annotations

import json
from pathlib import Path


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


def test_specialist_assets_require_skill_and_raw_evidence_checks() -> None:
    profile_meta, profile = _asset("profiles/specialist")
    contract_meta, contract = _asset("output-contracts/diagnose")

    assert profile_meta["version"] == "1.0.1"
    assert contract_meta["version"] == "4.0.1"
    for material in (profile, contract):
        assert "problem_time" in material
        assert "user_facts" in material
        assert "raw" in material.lower() or "原始" in material
        assert "causal" in material.lower() or "因果" in material
        assert "role" in material.lower() or "角色" in material
    assert "Agent 禁止提出或写入 `USER_RESULT`" in contract
    assert "problem-locator-pack-result" not in contract
    assert "`artifact_proposal_key=K`" in contract
    assert "`parse-1-run`" in contract
    assert "every other rule kind" in contract
    assert "`SEMANTIC_CAUSALITY`, has `fact_refs=[]`" in contract
    assert "`problem_time - before_ms`" in contract
    assert "`problem_time + after_ms`" in contract
    assert "2026-07-30T23:59:59.500Z" in contract
    assert "禁止跳过本 Job 的 `target-logs`" in contract
    assert "空 audit、重复成功操作或错误操作" in contract


def test_reviewer_assets_require_independent_rule_replay_and_full_evidence_union() -> None:
    profile_meta, profile = _asset("profiles/reviewer")
    contract_meta, contract = _asset("output-contracts/review")

    assert profile_meta["version"] == "1.0.1"
    assert contract_meta["version"] == "2.0.0"
    for material in (profile, contract):
        assert "problem_time" in material
        assert "raw" in material.lower()
        assert "causal" in material.lower()
        assert "completion" in material
        assert "consumed_evidence_refs" in material
    assert "must equal" in contract
    assert "forbids PASS" in contract
    assert "rule_claims" in contract
    assert "every other rule kind" in contract
    assert "`SEMANTIC_CAUSALITY`, has `fact_refs=[]`" in contract
    assert "`problem_time - before_ms`" in contract
    assert "`problem_time + after_ms`" in contract
    assert "2026-07-30T23:59:59.500Z" in contract
    assert "problem-locator-seal-outcome-draft" in contract
