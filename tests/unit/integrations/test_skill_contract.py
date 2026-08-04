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
SPEC_ROOT = (
    REPO_ROOT / "tests" / "fixtures" / "components" / "diagnosis-generator" / "specs"
)
RAW_LOGPARSE_ENV = (
    "LOGPARSE_REPO",
    "LOGPARSE_CONFIG_PATH",
    "LOGPARSE_PYTHON",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


def _frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = _text(path)
    assert text.startswith("---\n"), f"{path} must start with YAML frontmatter"
    marker = text.find("\n---\n", 4)
    assert marker >= 0, f"{path} has unterminated YAML frontmatter"
    fields: dict[str, str] = {}
    for line in text[4:marker].splitlines():
        assert line and not line[0].isspace()
        key, separator, value = line.partition(":")
        assert separator and key and value.strip()
        assert key not in fields
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


def test_diagnosis_manifest_v2_is_exact_canonical_and_spec_owned() -> None:
    spec = _json(SPEC_ROOT / "rpc-service-takeover.json")
    expected = {
        key: spec[key]
        for key in (
            "schema_version",
            "id",
            "version",
            "capability",
            "summary",
            "requires_logparse",
            "requirements",
            "logparse_plan",
            "logparse_product",
        )
    }
    expected.update(
        {
            "entry_document": "SKILL.md",
            "tool_bundle_id": "tool-bundle/diagnose",
        }
    )

    manifest_path = TAKEOVER_SKILL / "diagnosis-skill.json"
    payload = manifest_path.read_bytes()
    assert json.loads(payload) == expected
    assert payload == canonical_json_bytes(expected)

    generated = _text(TAKEOVER_SKILL / "SKILL.md")
    embedded = generated.split(
        "<!-- DIAGNOSIS_SKILL_MANIFEST_V2_BEGIN -->\n```json\n", 1
    )[1].split("\n```\n<!-- DIAGNOSIS_SKILL_MANIFEST_V2_END -->", 1)[0]
    assert embedded.encode("utf-8") + b"\n" == payload


def test_global_generated_contract_is_generic_and_business_fields_are_isolated() -> None:
    generator_contract = _text(
        GENERATOR_SKILL / "references" / "generated-skill-contract.md"
    )
    global_contract = _text(
        REPO_ROOT
        / "src/problem_locator/runtime/assets/output-contracts/diagnose/output-contract.md"
    )
    rpc_names = {
        "caller_service",
        "server_service",
        "rpc_method",
        "problem_time",
        "log_archive",
        "order_id",
    }
    for name in rpc_names - {"problem_time", "log_archive"}:
        assert name not in generator_contract
        assert name not in global_contract
    # problem_time is also the generic upstream Logparse request field, so its
    # mechanical name may appear globally; RPC-only requirement names may not.
    for document in (generator_contract, global_contract):
        assert "`log_archive`" not in document

    specs = {
        path.stem: _json(path)
        for path in sorted(SPEC_ROOT.glob("*.json"))
        if path.name != "fixture-manifest.json"
    }
    assert set(specs) == {
        "database-deadlock",
        "manual-triage",
        "rpc-service-takeover",
    }
    names = {
        key: {item["name"] for item in value["requirements"]}
        for key, value in specs.items()
    }
    assert rpc_names == names["rpc-service-takeover"]
    assert names["database-deadlock"] == {
        "database_instance",
        "database_process",
        "incident_time",
        "database_logs",
        "victim_transaction_id",
    }
    assert names["manual-triage"] == {
        "affected_component",
        "observed_symptom",
        "reproduction_steps",
    }
    assert names["manual-triage"].isdisjoint(rpc_names)
    assert names["database-deadlock"].isdisjoint(rpc_names)


def test_requirements_drive_need_outcomes_and_use_public_s00_constraints() -> None:
    generated_skill = _text(TAKEOVER_SKILL / "SKILL.md")
    normalized_skill = re.sub(r"\s+", " ", generated_skill)
    manifest = _json(TAKEOVER_SKILL / "diagnosis-skill.json")
    assert "阶段全部缺失 INPUT 并返回 NEED_INPUT" in normalized_skill
    assert "ATTACHMENT 并返回 NEED_ATTACHMENT" in normalized_skill
    for requirement in manifest["requirements"]:
        assert f"`{requirement['name']}`" in generated_skill
        assert requirement["prompt"] in generated_skill

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


def test_parse_continuation_and_candidate_evidence_order_are_explicit() -> None:
    output_contract = _text(
        REPO_ROOT
        / "src/problem_locator/runtime/assets/output-contracts/diagnose/output-contract.md"
    )
    generated_skill = _text(TAKEOVER_SKILL / "SKILL.md")
    helper = _text(LOGPARSE_SKILL / "SKILL.md")

    for document in (output_contract, generated_skill, helper):
        assert "state_delta.add_evidence_bindings" in document
        assert "evidence_proposal_key" in document
    for document in (output_contract, generated_skill):
        assert "evidence_refs" in document
        assert "固定子序列" in document
        assert "禁止按业务" in document


def test_logparse_default_and_parse_once_rules_have_one_owner() -> None:
    helper = _text(LOGPARSE_SKILL / "SKILL.md")
    generated_skill = _text(TAKEOVER_SKILL / "SKILL.md")

    assert "Invoke it exactly once" in helper
    assert "do not retry parse in the same Job" in helper
    assert "If the manifest contains any `LOGPARSE_RUN`, `parse-targets` is forbidden" in helper
    assert "Then call only\n`target-logs`" in helper
    assert "Do not alter it and do not parse again" in helper
    assert "without\n`--product`" in helper
    assert "metadata" in helper and "`default`" in helper
    assert "always JSON strings" in helper
    assert "without converting numeric-looking" in helper

    assert "加载 `logparse-diagnose` 并严格执行" in generated_skill
    assert "parse-once" in generated_skill
    assert "LOGPARSE_RUN 复用" in generated_skill
    assert "product 为 `compact`" in generated_skill
    assert '"slot_1"' in generated_skill and '"slot_2"' in generated_skill


def test_logparse_evidence_paths_are_locator_owned_not_proposal_owned() -> None:
    output_contract = _text(
        REPO_ROOT
        / "src/problem_locator/runtime/assets/output-contracts/diagnose/output-contract.md"
    )
    generator = _text(
        GENERATOR_SKILL / "scripts" / "generate_diagnosis_skill.py"
    )
    helper = _text(LOGPARSE_SKILL / "SKILL.md")
    for document in (output_contract, generator, helper):
        assert "workspace_relative_path" in document
        assert "locator.relative_path" in document
    assert "`workspace_relative_path` 必须为 `null`" in output_contract
    assert "`workspace_relative_path` to null" in helper


def test_logparse_run_metadata_has_one_strict_field_set() -> None:
    output_contract = _text(
        REPO_ROOT
        / "src/problem_locator/runtime/assets/output-contracts/diagnose/output-contract.md"
    )
    generator = _text(
        GENERATOR_SKILL / "scripts" / "generate_diagnosis_skill.py"
    )
    generator_contract = _text(
        GENERATOR_SKILL / "references" / "generated-skill-contract.md"
    )
    helper = _text(LOGPARSE_SKILL / "SKILL.md")
    required = (
        "tree_manifest_sha256",
        "logparse_version_ref",
        "parse_manifest_relative_path",
        "source_attachment_id",
        "source_attachment_sha256",
        "parse_parameters",
    )
    for document in (output_contract, generator, generator_contract, helper):
        for field in required:
            assert field in document
        assert "schema_version" in document
        assert "format_id" in document
        assert "description" in document
    assert "contains exactly these six fields" in helper
    assert "严格且仅含" in generator
    assert "恰好包含" in generator_contract
    for document in (output_contract, generator, generator_contract, helper):
        assert "application/vnd.problem-locator.logparse-run+directory" in document
        assert "declared_size" in document
        assert "declared_sha256" in document
        assert "logparse_run_artifact_draft" in document


def test_candidate_requires_exact_json_and_result_archive_pair() -> None:
    generator_contract = _text(
        GENERATOR_SKILL / "references" / "generated-skill-contract.md"
    )
    generated_skill = _text(TAKEOVER_SKILL / "SKILL.md")
    generic_values = (
        "USER_RESULT",
        "USER_RESULT_ARCHIVE",
        "result.zip",
        "result.txt",
        "target-log-001.log",
    )
    assert "唯一 `USER_RESULT`" in generator_contract
    assert "唯一\n`USER_RESULT_ARCHIVE`" in generator_contract
    assert "必须恰好提出以下两个 FILE Artifact" in generated_skill
    for document in (generator_contract, generated_skill):
        assert "REVIEW PASS" in document.upper()
        for value in generic_values:
            assert value in document
    for value in (
        "diagnosis-result.json",
        "application/zip",
        "problem-locator-result-archive-v1",
    ):
        assert value in generated_skill
    assert "problem-locator-pack-result" in generated_skill
    assert "无日志场景传空数组" in generated_skill
    assert "禁止直接复制或沿用 `target-logs`" in generated_skill
    assert "禁止直接沿用 broker anchor 顺序" in generator_contract


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
        for paragraph in _paragraphs(_text(path)):
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
