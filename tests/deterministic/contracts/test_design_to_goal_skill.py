from __future__ import annotations

import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOT = REPOSITORY_ROOT / ".agents" / "skills" / "design-to-goal"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _frontmatter(markdown: str) -> tuple[str, str]:
    match = re.fullmatch(r"---\n(?P<header>.*?)\n---\n(?P<body>.*)", markdown, re.DOTALL)
    assert match is not None, "SKILL.md must contain one YAML frontmatter block"
    return match.group("header"), match.group("body")


def test_design_to_goal_skill_package_contract() -> None:
    required = {
        "SKILL.md",
        "agents/openai.yaml",
        "references/artifact-contract.md",
        "assets/conversation.md",
        "assets/design.md",
        "assets/goal.md",
    }
    assert required == {
        path.relative_to(SKILL_ROOT).as_posix()
        for path in SKILL_ROOT.rglob("*")
        if path.is_file()
    }

    header, body = _frontmatter(_text(SKILL_ROOT / "SKILL.md"))
    assert "name: design-to-goal" in header
    assert "description:" in header
    for trigger in ("design", "architecture", "schema", "Test Flow", "implementation", "Goal"):
        assert trigger in header
    assert "TODO" not in header + body

    metadata = _text(SKILL_ROOT / "agents" / "openai.yaml")
    assert 'display_name: "Design to Goal"' in metadata
    assert 'default_prompt: "Use $design-to-goal' in metadata
    assert "allow_implicit_invocation: true" in metadata


def test_design_to_goal_skill_enforces_design_and_goal_gates() -> None:
    skill = _text(SKILL_ROOT / "SKILL.md")
    contract = _text(SKILL_ROOT / "references" / "artifact-contract.md")
    agents = _text(REPOSITORY_ROOT / "AGENTS.md")
    combined = skill + contract

    for required in (
        "work-items/YYYYMMDD-<kebab-name>/",
        "normal/default mode",
        "Plan mode",
        "conversation.md",
        "design.md",
        "goal.md",
        "SHA-256",
        "unresolved conflict",
        "separate Goal-start",
        "unfinished Goal",
        "get_goal",
        "verdict.json",
        "dev.default",
    ):
        assert required in combined

    for forbidden_design_write in (
        "source code",
        "tests",
        "AGENTS files",
        "Skills",
        "Git metadata",
        "external systems",
    ):
        assert forbidden_design_write in skill

    assert "$design-to-goal" in agents
    assert ".agents/skills/design-to-goal/SKILL.md" in agents
    assert "已按 SHA-256 明确批准" in agents
    assert "规范 work-item 路径" in agents
    assert "冻结的 goal SHA-256" in agents
    assert "conversation/design/goal 三个 SHA-256" in agents
    assert "不得递归启动新的设计流程" in agents
    assert "批准设计与启动 Codex Goal 必须是两条独立用户指令" in agents
    assert "存在未解决冲突时不得批准设计" in agents
    assert "Goal 创建失败均不解除该边界" in agents
    assert "要求用户取消/清除当前 Goal" in agents
    assert "带前驱摘要的 `-r2` 后继" in agents


def test_design_to_goal_templates_define_required_artifacts() -> None:
    conversation = _text(SKILL_ROOT / "assets" / "conversation.md")
    design = _text(SKILL_ROOT / "assets" / "design.md")
    goal = _text(SKILL_ROOT / "assets" / "goal.md")

    for heading in ("# Conversation Archive", "## Entries"):
        assert heading in conversation
    for marker in (
        "Append-only",
        "Role: user|assistant",
        "Kind: message|commentary|question|options|final|correction",
        "[REDACTED:<type>]",
    ):
        assert marker in conversation

    for heading in (
        "## Goal",
        "## Non-goals",
        "## Current facts and evidence",
        "## Applicable repository authority",
        "## Conflict assessment",
        "## Proposed design",
        "## Interfaces and data flow",
        "## Implementation scope",
        "## Authoritative documentation impact",
        "## Testing impact",
        "## Acceptance criteria",
        "## Stop and return-to-design conditions",
        "## Approval eligibility",
    ):
        assert heading in design
    assert "Unresolved conflicts" in design
    assert "Resolution and resulting scope/docs" in design
    assert "Exact path | Why authoritative | Required synchronization | Verification" in design
    assert "NONE|PROTECTED_CHANGE" in design
    assert "Separate authorization entry" in design
    assert "NONE` requires exact changes `NONE`" in design
    assert "Never use a hand-written boolean" in design

    for heading in (
        "## Binding",
        "## Objective and stopping condition",
        "## Read first and reread on uncertainty",
        "## Allowed changes",
        "## Forbidden changes",
        "## Implementation and ordinary tests",
        "## Authoritative documentation synchronization",
        "## Test Flow execution",
        "## Stop and require Goal replacement conditions",
        "## Immutability after Goal creation",
    ):
        assert heading in goal
    assert "Approved design SHA-256" in goal
    assert "Frozen conversation SHA-256" in goal
    assert "Predecessor work item chain" in goal
    assert "Separate Goal-start conversation entry" in goal
    assert "verified, successful `verdict.json`" in goal
    assert "Execution arguments are byte-for-byte" in goal
    assert "GENESIS; empty DATA_ROOT; source-drift rejection" in goal
    assert "Do not modify any work-item file" in goal
    assert "update_goal(status=complete)" in goal


def test_design_to_goal_keeps_test_flow_authorization_separate() -> None:
    skill = _text(SKILL_ROOT / "SKILL.md")
    contract = _text(SKILL_ROOT / "references" / "artifact-contract.md")

    assert "tools/test-flow/**" in contract
    assert "design/test-flow-architecture.md" in contract
    assert "测试活动约束" in contract
    assert "tests/**" in contract
    assert "General approval of the design never implies Test Flow change authorization" in skill
    assert "“Approve the design” never authorizes a protected Test Flow change" in contract
    assert "Do not run `pytest`" in contract
    assert "--track dev --goal dev.default --plan-only" in contract
    assert "--track dev --goal dev.default" in contract


def test_design_to_goal_freezes_source_and_preserves_plan_identity() -> None:
    skill = _text(SKILL_ROOT / "SKILL.md")
    contract = _text(SKILL_ROOT / "references" / "artifact-contract.md")
    goal = _text(SKILL_ROOT / "assets" / "goal.md")

    assert "Freeze a candidate `conversation.md`, `design.md`, and `goal.md`" in skill
    assert "Once confirmed, all three work-item files are permanently frozen" in skill
    assert "objective contains the exact canonical work-item path plus all three digests" in skill
    assert "If creation explicitly fails and `get_goal` confirms there is no unfinished Goal" in skill
    assert "If creation outcome or confirmation is ambiguous, keep the files frozen" in skill
    assert "If the capability is unavailable or its result is ambiguous, fail closed" in skill
    assert "create a linked successor work item such as `<original>-r2`" in skill
    assert "execution arguments and source/identity bindings match the inspected plan" in contract
    assert "new `reason`, `hypothesis`, and `expected-evidence` values" in contract
    for release_invariant in (
        "Git-visible tracked bytes and unignored untracked files",
        "Client, Server, Logparse, MCP, Skill, runtime, and model-context identities",
        "GENESIS",
        "empty `DATA_ROOT`",
        "drift between planning and `verdict.json`",
    ):
        assert release_invariant in contract
    assert "keep progress in Codex Goal state" in goal
