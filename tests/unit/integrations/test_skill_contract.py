from __future__ import annotations

import json
import re
from pathlib import Path

from problem_locator.contracts import (
    AttachmentRequirementConstraints,
    ErrorCode,
    PendingRequirement,
    canonical_json_bytes,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOT = REPO_ROOT / ".claude" / "skills"
GENERATOR_SKILL = SKILL_ROOT / "wiki-to-diagnosis-skill"
LOGPARSE_SKILL = SKILL_ROOT / "logparse-diagnose"
TAKEOVER_SKILL = SKILL_ROOT / "diagnose-service-takeover"

RESULT_TYPES = frozenset(
    {"NEED_INPUT", "NEED_ATTACHMENT", "REROUTE", "COMPLETED"}
)
REQUIREMENT_NAMES = (
    "caller_service",
    "server_service",
    "rpc_method",
    "problem_time",
    "log_archive",
    "order_id",
)
RAW_LOGPARSE_ENV = (
    "LOGPARSE_REPO",
    "LOGPARSE_CONFIG_PATH",
    "LOGPARSE_PYTHON",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = _text(path)
    assert text.startswith("---\n"), f"{path} must start with YAML frontmatter"
    marker = text.find("\n---\n", 4)
    assert marker >= 0, f"{path} has unterminated YAML frontmatter"
    fields: dict[str, str] = {}
    for line in text[4:marker].splitlines():
        assert line and not line[0].isspace(), (
            f"{path} frontmatter must use scalar top-level fields only"
        )
        key, separator, value = line.partition(":")
        assert separator and key and value.strip(), f"invalid frontmatter line: {line!r}"
        assert key not in fields, f"duplicate frontmatter key: {key}"
        fields[key] = value.strip().strip('"').strip("'")
    return fields, text[marker + 5 :]


def _paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]


def _fenced_blocks(text: str) -> list[tuple[str, str]]:
    return [
        (match.group("language").casefold(), match.group("body"))
        for match in re.finditer(
            r"```(?P<language>[^\n]*)\n(?P<body>.*?)```",
            text,
            flags=re.DOTALL,
        )
    ]


def test_all_three_skill_frontmatters_have_only_name_and_description() -> None:
    for skill_dir in (GENERATOR_SKILL, LOGPARSE_SKILL, TAKEOVER_SKILL):
        fields, body = _frontmatter(skill_dir / "SKILL.md")
        assert set(fields) == {"name", "description"}
        assert fields["name"] == skill_dir.name
        assert fields["description"]
        assert body.strip()


def test_diagnosis_manifest_has_the_exact_fields_and_canonical_bytes() -> None:
    manifest_path = TAKEOVER_SKILL / "diagnosis-skill.json"
    expected = {
        "schema_version": 1,
        "id": "diagnose-service-takeover",
        "version": "2.0.0",
        "capability": "service-takeover",
        "summary": "定位合成服务接管场景中的 RPC 超时",
        "entry_document": "SKILL.md",
        "tool_bundle_id": "tool-bundle/diagnose",
        "requires_logparse": True,
        "logparse_product": "compact",
    }

    payload = manifest_path.read_bytes()
    assert json.loads(payload) == expected
    assert payload == canonical_json_bytes(expected)


def test_generator_and_generated_skill_freeze_the_four_business_results() -> None:
    generator_contract = _text(
        GENERATOR_SKILL / "references" / "generated-skill-contract.md"
    )
    generated_skill = _text(TAKEOVER_SKILL / "SKILL.md")

    for document in (generator_contract, generated_skill):
        assert RESULT_TYPES == {
            result_type for result_type in RESULT_TYPES if result_type in document
        }
        assert "output/job_outcome.json" in document
        assert "AgentJobOutcome" in document
    assert "业务性缺参不是 `FAILED`" in generator_contract
    assert "业务性缺参不是执行失败" in generated_skill


def test_requirement_names_and_current_s00_constraint_seam_are_fixed() -> None:
    generator_contract = _text(
        GENERATOR_SKILL / "references" / "generated-skill-contract.md"
    )
    generated_skill = _text(TAKEOVER_SKILL / "SKILL.md")

    assert (
        "- 参数组 A：`caller_service`、`server_service`、`rpc_method`、`problem_time`"
        in generator_contract
    )
    assert "- 唯一日志：`log_archive`" in generator_contract
    assert "- 参数 B：`order_id`" in generator_contract
    for name in REQUIREMENT_NAMES:
        assert f"`{name}`" in generator_contract
        assert f"`{name}`" in generated_skill

    assert "只接受一个 Attachment" in generated_skill

    # S00 owns the DTO shape; S07 only asserts the public fields it consumes.
    assert {
        "allowed_content_types",
        "min_count",
        "max_count",
    } <= set(AttachmentRequirementConstraints.model_fields)
    assert {
        "requirement_id",
        "kind",
        "name",
        "constraints",
        "status",
    } <= set(PendingRequirement.model_fields)


def test_parse_once_and_logparse_run_reuse_are_unambiguous() -> None:
    helper = _text(LOGPARSE_SKILL / "SKILL.md")
    generated_skill = _text(TAKEOVER_SKILL / "SKILL.md")

    assert "Invoke it exactly once" in helper
    assert "do not retry parse in the same Job" in helper
    assert "If the manifest contains any `LOGPARSE_RUN`, `parse-targets` is forbidden" in helper
    assert "Then call only\n`target-logs`" in helper
    assert "Do not alter it and do not parse again" in helper

    assert "仅调用一次 `problem-locator-logparse parse-targets" in generated_skill
    assert "已含任一 `artifact_kind=LOGPARSE_RUN`，严禁调用\n`parse-targets`" in generated_skill
    assert "不得再次 parse" in generated_skill
    assert "新 Job" in generated_skill and "PREVIOUS_OUTCOME" in generated_skill


def test_candidate_requires_one_fixed_user_result_in_the_same_outcome() -> None:
    generator_contract = _text(
        GENERATOR_SKILL / "references" / "generated-skill-contract.md"
    )
    generated_skill = _text(TAKEOVER_SKILL / "SKILL.md")
    fixed_values = (
        "user-result",
        "USER_RESULT",
        "diagnosis-result.json",
        "application/json",
        "FILE",
        "output/proposals/user-result/payload",
        "problem-locator-diagnosis-v1",
        "Diagnosis result",
        "problem_statement",
        "candidate_statement",
        "supporting_evidence_bindings",
        "completion_criteria_mapping",
    )

    for document in (generator_contract, generated_skill):
        assert "恰好" in document and "一个 USER_RESULT" in document
        assert "同一" in document and "Outcome" in document
        for value in fixed_values:
            assert value in document
        assert "REVIEW" in document
        assert "RESOLVED" in document

    assert "没有 Candidate 时禁止 USER_RESULT" in generator_contract
    assert (
        '{"schema_version":1,"format_id":"problem-locator-diagnosis-v1",'
        '"description":"Diagnosis result"}' in generated_skill
    )


def test_skills_define_no_private_errors_direct_cli_or_raw_capabilities() -> None:
    documents = [
        path
        for skill_dir in (GENERATOR_SKILL, LOGPARSE_SKILL, TAKEOVER_SKILL)
        for path in skill_dir.rglob("*.md")
    ]
    combined = "\n".join(_text(path) for path in documents)
    public_error_codes = {code.value for code in ErrorCode}
    error_like_tokens = set(
        re.findall(
            r"\b[A-Z][A-Z0-9_]*_(?:FAILED|INVALID|UNAVAILABLE|VIOLATION|ERROR)\b",
            combined,
        )
    )
    assert error_like_tokens <= public_error_codes

    direct_cli = re.compile(
        r"(?:python(?:3(?:\.\d+)?)?|<[^>]*python[^>]*>)\s+[^\n]*"
        r"(?:^|[/\\])cli\.py\s+(?:parse|mech-target-logs)\b",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    assert direct_cli.search(combined) is None
    assert "subprocess.Popen" not in combined
    assert "subprocess.run" not in combined

    for path in documents:
        text = _text(path)
        for paragraph in _paragraphs(text):
            if not any(name in paragraph for name in RAW_LOGPARSE_ENV):
                continue
            assert re.search(
                r"不得|禁止|不读取|never|do not|must not",
                paragraph,
                flags=re.IGNORECASE,
            ), f"raw logparse configuration is not negated in {path}: {paragraph}"

    helper = _text(LOGPARSE_SKILL / "SKILL.md")
    executable_commands = {
        line.strip().split()[1]
        for language, block in _fenced_blocks(helper)
        if language in {"", "text", "console", "shell", "bash", "sh"}
        for line in block.splitlines()
        if line.strip().startswith("problem-locator-logparse ")
    }
    assert executable_commands == {"parse-targets", "target-logs"}
    assert "No other flags or positional arguments are allowed" in helper
    assert "Do not\nprint, persist, forward, or inspect the endpoint/token" in helper
