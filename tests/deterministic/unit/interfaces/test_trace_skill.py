from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
SKILL_ROOT = ROOT / ".claude/skills/render-problem-locator-trace"


def test_trace_skill_is_a_thin_product_cli_wrapper() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    metadata = (SKILL_ROOT / "agents/openai.yaml").read_text(encoding="utf-8")
    normalized_skill = " ".join(skill.split())

    assert "name: render-problem-locator-trace" in skill
    assert (
        "python -m problem_locator render-journey --case-id <case-id> "
        "--log-dir <absolute-log-dir>"
    ) in skill
    assert "Do not parse `journey.jsonl`" in normalized_skill
    assert "fall back to `debug.jsonl`" in normalized_skill
    assert "Exit 2" in skill and "Exit 3" in skill and "Exit 4" in skill
    assert "detailed.log" in skill and "brief.log" in skill
    assert "$render-problem-locator-trace" in metadata
    assert not (SKILL_ROOT / "scripts").exists()


def test_readme_documents_journey_generation_and_snapshots() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "DFX_LOG_DIR" in readme
    assert "<dir>/debug.jsonl" in readme
    assert "<dir>/journey.jsonl" in readme
    assert "render-journey" in readme
    assert "detailed.log" in readme and "brief.log" in readme
    assert "当前快照" in readme
