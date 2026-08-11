from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from problem_locator.contracts import is_canonical_json_bytes


ROOT = Path(__file__).resolve().parents[4]
SKILL_ROOT = (
    ROOT
    / "tests"
    / "fixtures"
    / "components"
    / "generic-problem-locator-smoke"
)
EXPECTED_INPUT = (
    "订单支付成功后页面仍显示“处理中”。\n"
    "request-id: 订单-α-42\n"
    "已确认：刷新三次仍复现"
)


def test_generic_locator_smoke_skill_has_standard_test_only_shape() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    opening, body = text.split("---\n", 2)[1:]
    frontmatter = opening.strip().splitlines()

    assert frontmatter[0] == "name: generic-problem-locator-smoke"
    assert frontmatter[1].startswith("description: ")
    assert len(frontmatter) == 2
    assert re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*",
        SKILL_ROOT.name,
    )
    assert f"```text\n{EXPECTED_INPUT}\n```" in body
    assert "generic-skill-input-contract-ok" in body
    assert "STATUS: RESOLVED" in body
    assert "STATUS: UNRESOLVED" in body
    assert "output/generic_diagnosis_result.txt" in body

    metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(
        encoding="utf-8"
    )
    assert 'display_name: "Generic Problem Locator Smoke"' in metadata
    assert "$generic-problem-locator-smoke" in metadata
    assert "allow_implicit_invocation: false" in metadata


def test_generic_locator_smoke_fixture_manifest_is_exact_and_canonical() -> None:
    manifest_path = SKILL_ROOT / "fixture-manifest.json"
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    assert is_canonical_json_bytes(raw)
    assert manifest["root"] == (
        "tests/fixtures/components/generic-problem-locator-smoke"
    )
    assert manifest["owner_spec"] == "S08"

    actual = {
        path.relative_to(SKILL_ROOT).as_posix(): path
        for path in SKILL_ROOT.rglob("*")
        if path.is_file() and path != manifest_path
    }
    assert [entry["path"] for entry in manifest["files"]] == sorted(actual)
    for entry in manifest["files"]:
        data = actual[entry["path"]].read_bytes()
        assert entry["size"] == len(data)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
