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
from problem_locator.runtime.input_profile import (
    builtin_input_profile_sha256,
    expand_profile_requirements,
    load_builtin_input_profile,
)


REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL_ROOT = REPO_ROOT / ".claude" / "skills"
GENERATOR_SKILL = SKILL_ROOT / "wiki-to-diagnosis-skill"
LOGPARSE_SKILL = SKILL_ROOT / "logparse-diagnose"
TAKEOVER_SKILL = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "components"
    / "diagnosis-generator"
    / "diagnose-service-takeover"
)
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


def test_diagnosis_manifest_v6_is_exact_canonical_and_profile_owned() -> None:
    spec = _json(SPEC_ROOT / "rpc-service-takeover.json")
    roles = [
        {key: item[key] for key in ("label", "description", "presence", "source_reference")}
        for item in spec["roles"]
    ]
    requirements = expand_profile_requirements(roles, requires_logparse=True)
    requirements.extend(
        {
            key: value
            for key, value in item.items()
            if key != "confirmed"
        }
        | {"origin": "WIKI", "role": None}
        for item in spec["requirements"]
    )
    requirements = sorted(
        enumerate(requirements),
        key=lambda entry: (
            0 if entry[1]["stage"] == "INITIAL" else 1,
            0 if entry[1]["kind"] == "INPUT" else 1,
            entry[0],
        ),
    )
    requirements = [item for _, item in requirements]
    logparse_plan = {
        "attachment_requirement": "log_archive",
        "problem_time_binding": {"source": "USER_FACT", "name": "problem_time"},
        "anchors": [
            {
                "label": role["label"],
                "module": anchor["module"],
                "slot": {"source": "USER_FACT", "name": f"{role['label']}_slot"},
                "process_name": {
                    "source": "USER_FACT",
                    "name": f"{role['label']}_process_name",
                },
                "pid": {"source": "USER_FACT", "name": f"{role['label']}_pid"},
            }
            for role, anchor in zip(roles, spec["logparse_plan"]["anchors"], strict=True)
        ],
    }
    profile = load_builtin_input_profile()
    expected = {
        key: spec[key]
        for key in (
            "schema_version",
            "id",
            "version",
            "capability",
            "deployment_scope",
            "summary",
            "requires_logparse",
            "logparse_product",
            "verification_contract",
        )
    }
    expected.update(
        {
            "entry_document": "SKILL.md",
            "tool_bundle_id": "tool-bundle/diagnose",
            "input_profile": profile,
            "input_profile_sha256": builtin_input_profile_sha256(profile),
            "roles": roles,
            "requirements": requirements,
            "logparse_plan": logparse_plan,
        }
    )

    manifest_path = TAKEOVER_SKILL / "diagnosis-skill.json"
    payload = manifest_path.read_bytes()
    assert json.loads(payload) == expected
    assert payload == canonical_json_bytes(expected)

    generated = _text(TAKEOVER_SKILL / "SKILL.md")
    embedded = generated.split(
        "<!-- DIAGNOSIS_SKILL_MANIFEST_V6_BEGIN -->\n```json\n", 1
    )[1].split("\n```\n<!-- DIAGNOSIS_SKILL_MANIFEST_V6_END -->", 1)[0]
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
        "order_id",
    }
    for name in rpc_names:
        assert name not in generator_contract
        assert name not in global_contract

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
    assert "当前阶段全部已激活且缺失的 INPUT" in normalized_skill
    assert "NEED_INPUT" in normalized_skill
    assert "ATTACHMENT 并返回 NEED_ATTACHMENT" in normalized_skill
    for requirement in manifest["requirements"]:
        assert f"`{requirement['name']}`" in generated_skill
        assert requirement["prompt"] in generated_skill
        assert requirement["supplement_policy"] in {"NONE", "MISSING_ONLY"}

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
    assert '"client_slot"' in generated_skill and '"server_slot"' in generated_skill


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


def test_agent_candidate_forbids_public_result_artifacts() -> None:
    generator_contract = _text(
        GENERATOR_SKILL / "references" / "generated-skill-contract.md"
    )
    generated_skill = _text(TAKEOVER_SKILL / "SKILL.md")
    generic_values = ("USER_RESULT", "USER_RESULT_ARCHIVE", "result.zip")
    for document in (generator_contract, generated_skill):
        assert "Agent 禁止提出或写入" in document
        assert "独立 Review PASS 后开放公开下载" in document
        assert "problem-locator-pack-result" not in document
        for value in generic_values:
            assert value in document
    assert "服务端立即从已验证的" in generated_skill
    assert "diagnosis-result.json" in generated_skill
    assert "只提交 Candidate" in generated_skill


def test_wiki_conversion_contract_is_self_contained_and_business_neutral() -> None:
    skill = _text(GENERATOR_SKILL / "SKILL.md")
    references = {
        name: GENERATOR_SKILL / "references" / name
        for name in (
            "generation-spec-v6-reference.md",
            "verification-contract-v2-reference.md",
            "neutral-logparse-generation-spec-v6.json",
        )
    }
    for name, path in references.items():
        assert path.is_file(), name
        assert f"references/{name}" in skill
    assert "Base directory for this skill:" in skill
    assert "不得把裸 `references/...` 交给 `Read`" in skill
    assert "不得相对当前工作目录、输入 workspace" in skill
    assert "Role label" in skill and "anchor label" in skill
    assert "递归展开全部 `depends_on`" in skill
    assert "PASS/FAIL/UNKNOWN 条件" in skill

    for document, heading in (
        (skill, "Write 前机器引用闭包检查"),
        (
            _text(references["verification-contract-v2-reference.md"]),
            "最终 Write 前的机械闭包算法",
        ),
    ):
        for token in (
            heading,
            "event_id -> field 名称集",
            "递归遍历",
            "(event, field)",
            "field ∈ event_id -> field 名称集",
            "INPUT Requirement",
            "已见 rule ID",
            "跨 event 借用 field",
            "不得 `Write`",
            "重新完整核对",
        ):
            assert token in document

    generation_reference = _text(references["generation-spec-v6-reference.md"])
    verification_reference = _text(
        references["verification-contract-v2-reference.md"]
    )
    generator = _text(
        GENERATOR_SKILL / "scripts" / "generate_diagnosis_skill.py"
    )
    neutral = _json(references["neutral-logparse-generation-spec-v6.json"])

    assert "`allowed_values` 必须始终是数组" in generation_reference
    assert "空数组 `[]`，绝不能写 JSON `null`" in generation_reference
    for token in (
        "逐引用内部清单",
        "field ∈ fields(event)",
        "request_event.end_ms",
        "即使存在于 `response_event`",
        "NUMERIC_COMPARE.parameters.left/right",
        "不能只检查顶层 Rule",
        '"event": "server_event", "field": "server_request_id"',
        '"event": "server_event", "field": "client_request_id"',
        "两侧字段名不要求相同",
        "不是字段名文本本身",
    ):
        assert token in skill or token in verification_reference

    for document in (skill, verification_reference):
        normalized_document = re.sub(r"\s+", " ", document)
        for token in (
            "正向 witness",
            "稳定日志消息体",
            "实际 `line_pattern`",
            "match_mode",
            "event count",
            "按声明顺序",
            "NOT_APPLICABLE",
            "occurrence tuple",
            "不能只证明 JSON 可加载",
            "不得 `Write`",
        ):
            assert token in normalized_document

    assert "Write 前语义保真检查" in skill
    for document in (skill, generation_reference):
        for token in (
            "`(# ... #)`",
            "`（# ... #）`",
            "整个正文",
            "标记外正文",
            "转换元数据",
            "临时禁止集合仅包含旁注中未由",
            "标记外正文或权威澄清独立支持的实质内容",
            "只能用于排除审计",
            "绝不能用",
            "理解、补全、修正或推断业务语义",
            "复制、改写、概括",
            "GenerationSpec 字段值",
            "语义重叠",
            "只能依据标记外正文或",
            "权威澄清生成并记录具体源映射",
            "不得因旁注重复而删除合法事实",
            "不得借旁注补足外部来源未声明的",
            "限定",
            "旁注标记",
            "旁注独有的逐字或独特片段",
            "未闭合",
            "嵌套",
            "交叉",
            "不得猜测边界",
            "唯一最终 `Write` 前",
            "递归遍历待写 GenerationSpec 的所有对象和数组",
            "检查每一个字符串值",
            "每项语义及限定确认到标记外正文或权威澄清的具体源映射",
            "外部来源未独立支持的旁注内容",
            "立即丢弃整份草稿",
            "最多允许",
            "一次从标记外正文与权威澄清重新构造",
            "不能就地删改命中字段",
            "该次复检仍失败时",
            "立即停止并请求澄清",
            "不得再次重构或 `Write`",
            "源映射独立支持",
            "完整语义及限定",
            "复检通过前不得",
        ):
            assert token in document
        for token in (
            "`judgement_rules`",
            "`output_requirements`",
            "安全判断",
            "最终用户",
            "否定",
            "可能性",
            "风险后果",
            "不要求逐字复制",
        ):
            assert token in document
        assert "同时" in document or "兼具" in document
    assert "解释任何业务语义前" in skill
    assert "可以帮助理解匿名化、简写和特殊边界" not in skill
    assert "A conversion Agent may read these notes as author guidance" not in generator
    for token in (
        "conversion metadata removed before business interpretation",
        "neither author guidance nor diagnosis knowledge",
        "must never enter",
        "a generated product",
    ):
        assert token in generator

    assert "自包含唯一合同" in generation_reference
    assert "自包含唯一合同" in verification_reference
    for token in (
        "observation_policies",
        "event_extractors",
        "NUMERIC_COMPARE",
        "SEMANTIC_CAUSALITY",
        "terminal_paths",
        "COMPLETE",
        "PARTIAL",
        "NONE",
    ):
        assert token in verification_reference
    assert "合取式就绪门槛" in verification_reference
    assert "可达性 witness" in verification_reference
    for forbidden in (
        "scripts/generate_diagnosis_skill.py",
        "scripts/validate_generated_skill.py",
        "src/problem_locator/runtime/verification_contract.py",
        "以该实现为准",
    ):
        assert forbidden not in generation_reference
        assert forbidden not in verification_reference

    assert neutral["requires_logparse"] is True
    contract = neutral["verification_contract"]
    assert set(neutral) == {
        "schema_version",
        "generator_version",
        "id",
        "version",
        "capability",
        "deployment_scope",
        "summary",
        "chinese_title",
        "module_name",
        "problem_scope",
        "roles",
        "requirements",
        "logparse_plan",
        "verification_contract",
        "time_characteristics",
        "analysis_steps",
        "judgement_rules",
        "output_requirements",
        "assumptions",
        "requires_logparse",
    }
    assert set(contract) == {
        "schema_version",
        "observation_policies",
        "event_extractors",
        "rules",
        "terminal_paths",
    }
    assert {item["kind"] for item in contract["observation_policies"]} == {
        "SUPPRESSION",
        "RATE_LIMIT",
    }
    assert any(len(item["members"]) > 1 for item in contract["event_extractors"])
    assert any(item["kind"] == "NUMERIC_COMPARE" for item in contract["rules"])
    assert {item["resolution_status"] for item in contract["terminal_paths"]} == {
        "COMPLETE",
        "PARTIAL",
        "NONE",
    }
    assert max(
        item["parameters"].get("clock_tolerance_ms", 0)
        for item in contract["rules"]
    ) > 0
    duration_rule = next(
        item for item in contract["rules"]
        if item["id"] == "local_duration_over_budget"
    )
    converted_budget = duration_rule["parameters"]["right"]
    assert converted_budget["kind"] == "CONVERT"
    assert converted_budget["unit"] == "MICROSECOND"

    combined = "\n".join(
        [
            generation_reference,
            verification_reference,
            json.dumps(neutral, ensure_ascii=False),
        ]
    ).casefold()
    case_roots = sorted(
        path.parent
        for path in (REPO_ROOT / "tests" / "cases" / "release").glob("*/case.json")
    )
    assert case_roots
    business_canaries = [
        canary
        for case_root in case_roots
        for canary in _json(case_root / "oracle.json")["business_canaries"]
    ]
    for canary in business_canaries:
        assert str(canary).casefold() not in combined


def test_real_wiki_gate_allows_only_inputs_and_declared_skill_references() -> None:
    gate = _text(
        REPO_ROOT / "tests/real/agent/test_real_wiki_skill_generation_gate.py"
    )
    assert "only the references explicitly linked by the loaded Skill" in gate
    assert "actual absolute directory shown after `Base directory for this skill:`" in gate
    assert "Never pass a bare references/... path to Read" in gate
    assert "resolve it against the workspace cwd" in gate
    assert "Do not read repository source" in gate
    assert "generator or validator implementations" in gate
    assert "case oracles" in gate
    assert (
        "Then read inputs/wiki.md, inputs/clarifications.md, references/" not in gate
    )
    assert "Then read only inputs/wiki.md and inputs/clarifications.md" not in gate
    assert "Do not read outside inputs/" not in gate


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
