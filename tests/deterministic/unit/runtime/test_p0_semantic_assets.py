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


def test_specialist_assets_require_methods_only_grounded_output() -> None:
    profile_meta, profile = _asset("profiles/specialist")
    contract_meta, contract = _asset("output-contracts/diagnose")
    tool_meta, tool_bundle = _asset("tool-bundles/diagnose")

    assert profile_meta["version"] == "3.0.0"
    assert contract_meta["version"] == "6.0.0"
    assert tool_meta["version"] == "4.0.0"
    assert "raw" in profile.lower()
    assert "output/method-diagnosis.draft.json" in contract
    assert "inputs/logparse-receipt.json" in contract
    assert "identity_tokens" in contract
    assert "exact complete frozen log line" in contract
    assert "do not run an\noutcome sealer" in contract
    assert "output/job_outcome.draft.json" in contract
    assert "problem-locator-logparse" not in tool_bundle
    assert "problem-locator-seal-outcome-draft" not in tool_bundle


def test_reviewer_assets_require_exact_methods_identity_coverage() -> None:
    profile_meta, profile = _asset("profiles/reviewer")
    contract_meta, contract = _asset("output-contracts/review")
    tool_meta, tool_bundle = _asset("tool-bundles/review")

    assert profile_meta["version"] == "3.0.0"
    assert contract_meta["version"] == "4.0.0"
    assert tool_meta["version"] == "3.0.0"
    assert "Independently" in profile
    assert "output/method-review.draft.json" in contract
    assert "inputs/method-diagnosis.json" in contract
    assert "inputs/method-grounding-audit.json" in contract
    assert "exact set" in contract
    assert "(method_id, identity_tokens)" in contract
    assert "problem-locator-logparse" not in tool_bundle
    assert "problem-locator-seal-outcome-draft" not in tool_bundle
