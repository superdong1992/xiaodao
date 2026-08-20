from __future__ import annotations

import ast
import hashlib
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
            "wiki-template.md",
        )
    }
    for name, path in references.items():
        assert path.is_file(), name
        assert f"references/{name}" in skill
    linked_references = re.findall(
        r"\[[^\]]+\]\((references/[^)]+)\)",
        skill,
    )
    assert linked_references == [
        "references/generation-spec-v6-reference.md",
        "references/verification-contract-v2-reference.md",
        "references/checkpoints/01-begin-repeated-families-and-paths.md",
        "references/checkpoints/02-begin-9-1-inventory.md",
        "references/checkpoints/03-begin-9-2-witnesses.md",
        "references/checkpoints/04-write-now.md",
    ]
    assert "不得 `Read` 这两项可选示例" in skill
    assert "Base directory for this skill:" in skill
    assert "不得把裸 `references/...` 交给 `Read`" in skill
    assert "不得相对当前工作目录、输入 workspace" in skill
    assert "Role label" in skill and "anchor label" in skill
    assert "递归展开全部 `depends_on`" in skill
    assert "PASS/FAIL/UNKNOWN 条件" in skill

    for document, heading, blocked_submission in (
        (
            skill,
            "StructuredOutput 前机器引用闭包检查",
            "不得调用 `StructuredOutput`",
        ),
        (
            _text(references["verification-contract-v2-reference.md"]),
            "最终 Write 前的机械闭包算法",
            "不得 `Write`",
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
            "重新完整核对",
        ):
            assert token in document
        assert blocked_submission in document

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
    normalized_bounded_skill = re.sub(r"\s+", " ", skill)
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
        ):
            assert token in normalized_document
    assert "不得调用 `StructuredOutput`" in skill
    assert "不得 `Write`" in verification_reference

    for token in (
        "有序多成员事件",
        "顺序方向",
        "每个允许位置",
        "不得把测试样例中的位置当成业务不变量",
        "区间并集覆盖目标起点、终点",
        "未解释空隙",
        "无法安全表达并集覆盖",
        "禁止用",
        "近似替代",
    ):
        assert token in normalized_bounded_skill

    for token in (
        "有界构造与通过即提交",
        "有界单遍状态机",
        "只记录非 verification",
        "读取 verification reference 结束",
        "不得先构造",
        "`checkpoint 01` 返回后",
        "版本化 ordered-interval family",
        "不得逐规则复述",
        "第 9.1 节的逐引用闭包检查且仅执行一遍",
        "第 9.2 节的正向 witness 检查且仅执行",
        "blueprint 足以在一次",
        "不得构造 JSON 字符串或手工序列化",
        "`checkpoint 04` 返回后",
        "第十个且最后一个工具调用",
        "唯一一次 `StructuredOutput`",
        "第一次且唯一一次 materialization",
        "Test Flow CLI 的冻结 workflow schema 对协议已解析 IR 的四个根字段",
        "literal segments",
        "family kind/version",
        "声明的精确 cardinality 做机械校验",
        "可信 wrapper 对 IR 与 terminal 回显做",
        "在内存中确定性展开后调用原 loader/validator 深验",
        "递归 key 排序的 canonical JSON",
        "create-only 原子写入",
        "封存 size 与 SHA-256",
        "禁止重读任何合同或 checkpoint",
        "不得只修正引用后再从第 1 步重新完整核对",
        "必须等待这次 `StructuredOutput` 的 tool result 明确成功",
        "完整内容必须是精确 ASCII sentinel `DONE`",
        "尤其不得第二次调用 `StructuredOutput`",
    ):
        assert token in normalized_bounded_skill
    assert re.search(
        r"不得\s*调用\s*`Write`、\s*`Edit`、\s*`Bash`",
        skill,
    )

    bounded_state_machine = skill[
        skill.index("### 有界构造与通过即提交") :
        skill.index("### StructuredOutput 前语义保真检查")
    ]
    for retired_string_protocol in (
        "parse-equivalent",
        "grammar pass",
        "冻结字符串",
        "frozen string",
        "byte-for-byte",
    ):
        assert retired_string_protocol not in bounded_state_machine
    assert "冻结 workflow schema" in bounded_state_machine
    assert bounded_state_machine.index("第 9.2 节的正向 witness 检查") < (
        bounded_state_machine.index("再以读取 `checkpoint 04` 结束")
    )
    assert bounded_state_machine.index("`checkpoint 04` 返回后") < (
        bounded_state_machine.index("第十个且最后一个工具调用")
    )

    assert "StructuredOutput 前语义保真检查" in skill
    for document, submission_boundary, recursive_scan, string_scan, blocked_submission in (
        (
            skill,
            "唯一最终 `StructuredOutput` 前",
            "递归遍历待提交 GenerationBlueprint 的 literal `spec`、literal verification segments、family text/name slots 及其确定性最终投影",
            "检查每一个业务字符串值",
            "不得再次重构或调用 `StructuredOutput`",
        ),
        (
            generation_reference,
            "唯一最终 `Write` 前",
            "递归遍历待写 GenerationSpec 的所有对象和数组",
            "检查每一个字符串值",
            "不得再次重构或 `Write`",
        ),
    ):
        layout_insensitive_document = re.sub(r"\s+", "", document)
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
            submission_boundary,
            recursive_scan,
            string_scan,
            "每项语义及限定确认到标记外正文或权威澄清的具体源映射",
            "外部来源未独立支持的旁注内容",
            "立即丢弃整份草稿",
            "最多允许",
            "一次从标记外正文与权威澄清重新构造",
            "不能就地删改命中字段",
            "该次复检仍失败时",
            "立即停止并请求澄清",
            blocked_submission,
            "源映射独立支持",
            "完整语义及限定",
            "复检通过前不得",
        ):
            assert re.sub(r"\s+", "", token) in layout_insensitive_document
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
            assert re.sub(r"\s+", "", token) in layout_insensitive_document
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


def test_wiki_conversion_checkpoints_are_control_only_and_forward_only() -> None:
    checkpoint_root = GENERATOR_SKILL / "references" / "checkpoints"
    expected = {
        "01-begin-repeated-families-and-paths.md": (
            "begin_repeated_families_and_paths",
            "references/checkpoints/02-begin-9-1-inventory.md",
        ),
        "02-begin-9-1-inventory.md": (
            "begin_9_1_inventory",
            "references/checkpoints/03-begin-9-2-witnesses.md",
        ),
        "03-begin-9-2-witnesses.md": (
            "begin_9_2_witnesses",
            "references/checkpoints/04-write-now.md",
        ),
        "04-write-now.md": ("write_now", "next and only tool call"),
    }
    assert {path.name for path in checkpoint_root.glob("*.md")} == set(expected)

    checkpoint_texts: dict[str, str] = {}
    for name, (checkpoint_id, next_control) in expected.items():
        text = _text(checkpoint_root / name)
        checkpoint_texts[name] = text
        assert "checkpoint_schema_version: 1" in text
        assert f"checkpoint_id: {checkpoint_id}" in text
        assert "control_only: true" in text
        assert "must_not_enter_generation_spec: true" in text
        assert next_control in text
        assert (
            "Do not restate" in text
            or "Do not think further" in text
            or "Your next and only tool call" in text
        )

    assert "Do not call `StructuredOutput`" in checkpoint_texts[
        "01-begin-repeated-families-and-paths.md"
    ]
    for name in (
        "02-begin-9-1-inventory.md",
        "03-begin-9-2-witnesses.md",
    ):
        assert "If it fails, stop without reading the next checkpoint or submitting" in re.sub(
            r"\s+", " ", checkpoint_texts[name]
        )
    final_checkpoint = re.sub(
        r"\s+", " ", checkpoint_texts["04-write-now.md"]
    )
    for token in (
        "next and only tool call must be `StructuredOutput`",
        "complete compact IR root plain object itself",
        "first and only materialization of the `GenerationBlueprint` v1 IR",
        "Do not wrap it in another field",
        "or turn it into a JSON string",
        "CLI schema validates the protocol-parsed IR",
        "trusted wrapper binds the IR input to the terminal IR",
        "versioned deterministic compiler in memory",
        "existing deep GenerationSpec loader and verification validator",
        "create-only atomic output",
        "at most 48 KiB",
        "does not authorize `Write`",
        "Do not call `Write`, `Edit`, or `Bash`",
        "do not manually",
        "Wait for this unique `StructuredOutput` result",
        "complete content is exact ASCII `DONE`",
        "never call `StructuredOutput` a second time",
    ):
        assert token in final_checkpoint
    for disproven in (
        "parse-equivalent",
        "grammar pass",
        "string was frozen",
        "byte-for-byte identical",
    ):
        assert disproven not in final_checkpoint

    combined = "\n".join(checkpoint_texts.values()).casefold()
    for forbidden in (
        "q_{target}",
        "five-position",
        "oracle",
    ):
        assert forbidden not in combined


def test_release_case_business_canaries_do_not_leak_into_generic_runtime() -> None:
    case_roots = sorted(
        path.parent
        for path in (REPO_ROOT / "tests/cases/release").glob("*/case.json")
    )
    canaries = {
        str(canary).casefold()
        for case_root in case_roots
        for canary in _json(case_root / "oracle.json")["business_canaries"]
    }
    generic_paths = [
        GENERATOR_SKILL / "SKILL.md",
        *(GENERATOR_SKILL / "references").rglob("*"),
        *(REPO_ROOT / "src/problem_locator/runtime").rglob("*.py"),
    ]
    combined = "\n".join(
        _text(path)
        for path in generic_paths
        if path.is_file() and path.suffix in {".md", ".json", ".py"}
    ).casefold()
    for canary in canaries:
        assert canary not in combined


def test_real_wiki_gate_allows_only_inputs_and_declared_skill_references() -> None:
    gate = _text(
        REPO_ROOT / "tests/real/agent/test_real_wiki_skill_generation_gate.py"
    )
    assert "exact seven-stage state machine" in gate
    assert "call Read exactly eight times across it" in gate
    assert "These eight named files are the only Read calls permitted" in gate
    assert "actual absolute directory shown after `Base directory for this skill:`" in gate
    assert "Every Skill reference named below must be resolved by joining" in gate
    assert "Never pass a bare references/... path to Read" in gate
    assert "resolve it against the workspace cwd" in gate
    assert "Do not Read the optional references/wiki-template.md" in gate
    assert "references/neutral-logparse-generation-spec-v6.json examples" in gate
    assert "Do not read repository source" in gate
    assert "generator or validator implementations" in gate
    assert "case oracles" in gate
    assert (
        "Then read inputs/wiki.md, inputs/clarifications.md, references/" not in gate
    )
    assert "Then read only inputs/wiki.md and inputs/clarifications.md" not in gate
    assert "Do not read outside inputs/" not in gate
    for token in (
        "Stage 1 — load the bounded sources",
        "Stage 2 — record only the non-verification blueprint",
        "Stage 3 — cross the serial verification boundary",
        "Stage 4 — record only the bounded verification-core blueprint",
        "Stage 5 — complete only the repeated-family and path blueprint",
        "Stage 6 — run sections 9.1 and 9.2 exactly once and finalize the compact IR",
        "Stage 7 — submit the compact IR exactly once",
        "exactly two concurrent Read tool-use blocks",
        "references/checkpoints/01-begin-repeated-families-and-paths.md",
        "references/checkpoints/02-begin-9-1-inventory.md",
        "references/checkpoints/03-begin-9-2-witnesses.md",
        "references/checkpoints/04-write-now.md",
        "one `GenerationBlueprint` v1 IR",
        "does not explicitly contain the 144 ordered-interval family rule objects",
        "root must have exactly `schema_version`, `compiler`, `spec`, and `verification`",
        "`spec` has the 19 required final GenerationSpec fields other than `verification_contract`",
        "`logparse_product` is the only optional key",
        "Literal rule segments are exactly prefix=7 final rules 0..6, middle=9 final rules 112..120, suffix=5 final rules 160..164",
        "Expected counts must bind 5 positions, 105 mechanical family rules, 39 semantic family rules, 165 total rules",
        "IR canonical payload must be at most 48 KiB",
        "tenth and final tool call is one StructuredOutput invocation",
        "trusted wrapper binds tool input to terminal IR",
        "versioned pure compiler",
        "deep-validates the resulting full GenerationSpec v6",
        "final file must still contain 2 policies, 10 extractors, 165 rules, and 9 paths",
        "Do not call Write, Edit, or Bash",
        "On tool error, stop without retry",
        "complete content is ASCII `DONE`",
        "All four checkpoints are control-only",
        "Do not repeatedly `reconsider`",
    ):
        assert token in gate
    for generated_contract_invariant in (
        'assert len(contract["event_extractors"]) == 10',
        'assert len(contract["rules"]) == 165',
        'assert len(contract["terminal_paths"]) == 9',
    ):
        assert generated_contract_invariant in gate

    stage_6 = gate[
        gate.index("Stage 6 — run sections 9.1 and 9.2 exactly once and finalize the compact IR") :
        gate.index("Stage 7 — submit the compact IR exactly once")
    ]
    assert stage_6.index("perform section 9.1's exact per-reference event-field inventory") < stage_6.index(
        "perform section 9.2's positive-witness evaluation"
    )
    assert stage_6.index("perform section 9.2's positive-witness evaluation") < stage_6.index(
        "references/checkpoints/04-write-now.md"
    )
    stage_7 = gate[
        gate.index("Stage 7 — submit the compact IR exactly once") :
    ]
    assert "StructuredOutput" in stage_7
    assert "Do not call Write, Edit, or Bash" in stage_7
    for disproven in (
        "parse-equivalent RFC 8259 grammar pass",
        "freeze the entire string",
        "write the frozen string",
        "call Write immediately",
        "`file_path`",
        "materialize the complete GenerationSpec v6 root plain object exactly once inside StructuredOutput",
    ):
        assert disproven not in gate


def test_release_verification_core_reaches_checkpoint_before_any_rule_construction() -> None:
    raw_skill = _text(GENERATOR_SKILL / "SKILL.md")
    compact_skill = re.sub(r"\s+", "", raw_skill)
    checkpoint_1 = re.sub(
        r"\s+",
        " ",
        _text(
            GENERATOR_SKILL
            / "references/checkpoints/01-begin-repeated-families-and-paths.md"
        ),
    )
    checkpoint_2 = re.sub(
        r"\s+",
        " ",
        _text(GENERATOR_SKILL / "references/checkpoints/02-begin-9-1-inventory.md"),
    )
    checkpoint_3 = re.sub(
        r"\s+",
        " ",
        _text(GENERATOR_SKILL / "references/checkpoints/03-begin-9-2-witnesses.md"),
    )
    gate = _text(
        REPO_ROOT / "tests/real/agent/test_real_wiki_skill_generation_gate.py"
    )
    stage_3 = gate[
        gate.index("Stage 3 — cross the serial verification boundary") :
        gate.index("Stage 4 — record only the bounded verification-core blueprint")
    ]
    stage_4 = gate[
        gate.index("Stage 4 — record only the bounded verification-core blueprint") :
        gate.index("Stage 5 — complete only the repeated-family and path blueprint")
    ]
    stage_5 = gate[
        gate.index("Stage 5 — complete only the repeated-family and path blueprint") :
        gate.index("Stage 6 — run sections 9.1 and 9.2 exactly once and finalize the compact IR")
    ]
    stage_6 = gate[
        gate.index("Stage 6 — run sections 9.1 and 9.2 exactly once and finalize the compact IR") :
        gate.index("Stage 7 — submit the compact IR exactly once")
    ]

    assert "verificationreference的`Read`必须独占其assistantresponse" in compact_skill
    assert "不得先构造任何verification字段" in compact_skill
    assert "下一条独立assistantresponse必须且只能读取`checkpoint01`" in compact_skill
    assert "只记录恰好两个observationpolicies、十个eventextractors与全部固定非重复rules的紧凑blueprint" in compact_skill
    assert "不得物化IRroot" in compact_skill
    assert "不得开始重复ordered-intervalfamily或terminalpaths" in compact_skill
    assert "checkpoint02`返回后" in compact_skill
    assert "先执行verificationreference第9.1节" in compact_skill
    assert "再执行第9.2节" in compact_skill
    assert "no verification object has been materialized" in checkpoint_1
    assert "Record compact blueprint entries for exactly two observation policies" in checkpoint_1
    assert "Do not materialize those rule objects" in checkpoint_1
    assert "one versioned ordered-interval family plus the literal rule/path segments" in checkpoint_2
    assert "without carrying, serializing, or narrating the 144 expanded rule objects" in checkpoint_2
    assert "Do not execute section 9.1 or 9.2 yet" in checkpoint_2
    assert "section 9.1 check exactly once and then section 9.2 exactly once" in checkpoint_3
    assert "verification-reference Read must be the only tool call" in stage_3
    assert "do not construct any verification field" in stage_3
    assert "next independent assistant response" in stage_3
    assert "with no other content or tool call" in stage_3
    assert "record compact blueprint entries for exactly two observation policies and ten event extractors" in stage_4
    assert "then every fixed non-queue rule" in stage_4
    assert "Do not materialize rule objects" in stage_4
    assert "Do not materialize rule objects, start the repeated `q_{target}` families or terminal paths" in stage_4
    assert "Record one compact internal row-by-column expansion blueprint" in stage_5
    assert "do not redesign, collapse, approximate, narrate, or materialize" in stage_5
    assert "Do not execute section 9.1 or 9.2 in this stage" in stage_5
    assert stage_6.index("perform section 9.1's exact per-reference event-field inventory") < stage_6.index(
        "perform section 9.2's positive-witness evaluation"
    )


def test_real_wiki_gate_failure_report_is_bounded_and_content_free() -> None:
    gate_path = REPO_ROOT / "tests/real/agent/test_real_wiki_skill_generation_gate.py"
    gate = _text(gate_path)
    parsed = ast.parse(gate, filename=str(gate_path))
    helper = next(
        node
        for node in parsed.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_bounded_stream_receipt"
    )
    isolated_helper = ast.fix_missing_locations(
        ast.Module(body=[helper], type_ignores=[])
    )
    namespace: dict[str, object] = {"hashlib": hashlib}
    exec(compile(isolated_helper, str(gate_path), "exec"), namespace)
    summarize = namespace["_bounded_stream_receipt"]
    assert callable(summarize)

    payload = b"private-model-thinking-and-tool-content" * 32768
    summary = summarize("stdout", payload)
    assert summary == (
        f"stdout_bytes={len(payload)} "
        f"stdout_sha256={hashlib.sha256(payload).hexdigest()}"
    )
    assert len(summary) < 128
    assert "private-model-thinking" not in summary

    handler = gate[
        gate.index("except RuntimeExecutionError as exc:") :
        gate.index("assert execution.returncode == 0")
    ]
    assert handler.count("_bounded_stream_receipt(") == 2
    assert "failure_code={exc.failure.code.value}" in handler
    assert "pytrace=False" in handler
    assert ".decode(" not in handler
    assert "!r" not in handler


def test_skill_generation_batches_only_the_two_authority_reads() -> None:
    skill = _text(GENERATOR_SKILL / "SKILL.md")
    gate = _text(
        REPO_ROOT / "tests/real/agent/test_real_wiki_skill_generation_gate.py"
    )
    stage_1 = gate[
        gate.index("Stage 1 — load the bounded sources") :
        gate.index("Stage 2 — record only the non-verification blueprint")
    ]
    later_stages = gate[gate.index("Stage 2 — record only the non-verification blueprint") :]

    assert "exactly two concurrent Read tool-use blocks" in stage_1
    assert "first must read inputs/wiki.md" in stage_1
    assert "second must read inputs/clarifications.md" in stage_1
    assert "only batched or concurrent tool-use response" in stage_1
    assert "Wait until both corresponding tool results have returned" in stage_1
    assert stage_1.index("inputs/wiki.md") < stage_1.index("inputs/clarifications.md")
    assert stage_1.index("both corresponding tool results") < stage_1.index(
        "references/generation-spec-v6-reference.md"
    )
    assert "tool ordinals 1, 2, and 3 in exactly that order" in stage_1
    assert "every Read must be strictly serial" in stage_1
    assert "emit exactly one tool call in an assistant response" in stage_1
    assert "concurrent tool-use response" not in later_stages

    normalized_skill = re.sub(r"\s+", " ", skill)
    assert "下一条 assistant response 必须且只能按顺序并发发出两个 `Read` tool-use block" in normalized_skill
    assert "先读 workspace 的 `inputs/wiki.md`，再读 `inputs/clarifications.md`" in normalized_skill
    assert "必须等待这两个 `Read` 的 tool result 都返回" in normalized_skill
    assert "这是全流程唯一允许批量或并发 工具调用的 response" in normalized_skill
    assert "GenerationSpec reference 及之后的每个 `Read` 都必须严格串行" in normalized_skill
    assert "ordinal 保持为 0=`Skill`、1=Wiki `Read`、2=clarifications `Read`" in normalized_skill


def test_structured_output_success_requires_exact_done_terminal_response() -> None:
    skill = _text(GENERATOR_SKILL / "SKILL.md")
    checkpoint = _text(
        GENERATOR_SKILL / "references/checkpoints/04-write-now.md"
    )
    gate = _text(
        REPO_ROOT / "tests/real/agent/test_real_wiki_skill_generation_gate.py"
    )
    stage_7 = gate[
        gate.index("Stage 7 — submit the compact IR exactly once") :
    ]
    normalized_skill = re.sub(r"\s+", " ", skill)
    normalized_checkpoint = re.sub(r"\s+", " ", checkpoint)
    normalized_stage_7 = re.sub(r"\s+", " ", stage_7)

    assert "必须等待这次 `StructuredOutput` 的 tool result 明确成功" in normalized_skill
    assert "完整内容必须是精确 ASCII sentinel `DONE`" in normalized_skill
    assert "`DONE` 不得进入 tool input" in normalized_skill
    assert "尤其不得第二次调用 `StructuredOutput`" in normalized_skill
    assert "If and only if it reports success" in normalized_checkpoint
    assert "complete content is exact ASCII `DONE`" in normalized_checkpoint
    assert "`DONE` is control-only and must not enter the IR or generated output" in normalized_checkpoint
    assert "never call `StructuredOutput` a second time" in normalized_checkpoint
    assert "On success, emit exactly one terminal response" in normalized_stage_7
    assert "complete content is ASCII `DONE`" in normalized_stage_7
    assert "then emit no further text, turn, or tool" in normalized_stage_7

    for document in (skill, checkpoint, stage_7):
        assert "minimal completion" not in document
        assert "invocation itself is the terminal action" not in document
        assert "调用本身就是 terminal action" not in document


def test_release_first_structured_output_is_one_complete_bounded_ir() -> None:
    skill = _text(GENERATOR_SKILL / "SKILL.md")
    checkpoint = _text(
        GENERATOR_SKILL / "references/checkpoints/04-write-now.md"
    )
    gate = _text(
        REPO_ROOT / "tests/real/agent/test_real_wiki_skill_generation_gate.py"
    )
    stage_6 = gate[
        gate.index("Stage 6 — run sections 9.1 and 9.2 exactly once and finalize the compact IR") :
        gate.index("Stage 7 — submit the compact IR exactly once")
    ]
    stage_7 = gate[
        gate.index("Stage 7 — submit the compact IR exactly once") :
    ]

    normalized_skill = re.sub(r"\s+", " ", skill)
    compact_skill = re.sub(r"\s+", "", skill)
    normalized_checkpoint = re.sub(r"\s+", " ", checkpoint)
    compact_checkpoint = re.sub(r"\s+", "", checkpoint)
    normalized_stage_6 = re.sub(r"\s+", " ", stage_6)
    normalized_stage_7 = re.sub(r"\s+", " ", stage_7)
    assert "`StructuredOutput` 不是 schema discovery 或 validation probe" in normalized_skill
    assert "禁止用零属性 root、partial object、trial input 或 probe input" in normalized_skill
    assert "若该调用返回错误，立即停止，不得修补后重试或第二次调用`StructuredOutput`" in compact_skill
    assert "完整 `GenerationBlueprint` v1 根 plain object 的第一次且唯一一次 materialization" in normalized_skill
    assert "不得把 144 条 family rules 或三条 family paths 显式展开进 tool input" in normalized_skill
    assert "不得先在 thinking/正文中 生成第二份对象" in normalized_skill
    assert "必须先在同一 assistant response 内把完整 tool arguments 组装好" in normalized_skill
    assert "`StructuredOutput` 不是用来打开待填充容器的交互步骤" in normalized_skill
    assert "绝对不得先发占位调用再提交完整对象" in normalized_skill
    assert "`checkpoint 04` 提供与 provider schema 等价的 typed argument frame" in normalized_skill
    assert "大写占位符只表示从 blueprint 机械填入的值" in normalized_skill
    assert "`StructuredOutput` is not a schema-discovery or validation probe" in normalized_checkpoint
    assert "Never submit a zero-property root, partial IR, expanded 144-rule family, trial input, or probe input" in normalized_checkpoint
    assert "If the tool reports an error, stop immediately without repairing or retrying" in normalized_checkpoint
    assert "first and only materialization of the `GenerationBlueprint` v1 IR" in normalized_checkpoint
    assert "Use this provider-equivalent typed frame" in normalized_checkpoint
    assert "No token, angle bracket, explanatory label" in normalized_checkpoint
    assert "EXACTLY_7_FINAL_RULES_0_TO_6" in normalized_checkpoint
    assert "EXACTLY_9_FINAL_RULES_112_TO_120" in normalized_checkpoint
    assert "EXACTLY_5_FINAL_RULES_160_TO_164" in normalized_checkpoint
    assert "zero-property, partial, trial, expanded-family, or probe submission is forbidden" in normalized_stage_7
    assert "On tool error, stop without retry" in normalized_stage_7
    assert "complete four-key GenerationBlueprint root" in normalized_stage_7
    assert "do not include a GenerationSpec `verification_contract`" in normalized_stage_7
    assert "do not expand the 144 family rules or three generated paths" in normalized_stage_7

    ir_root_properties = ("schema_version", "compiler", "spec", "verification")
    assert "IR root has exactly four required keys" in normalized_checkpoint
    assert "root must have exactly `schema_version`, `compiler`, `spec`, and `verification`" in normalized_stage_6
    for property_name in ir_root_properties:
        assert f"`{property_name}`" in normalized_checkpoint
        assert f"`{property_name}`" in normalized_stage_6

    spec_required_properties = (
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
        "time_characteristics",
        "analysis_steps",
        "judgement_rules",
        "output_requirements",
        "assumptions",
        "requires_logparse",
    )
    assert len(spec_required_properties) == 19
    assert "19 required final GenerationSpec fields other than `verification_contract`" in normalized_checkpoint
    assert "`logparse_product` is its only optional key" in normalized_checkpoint
    assert "`logparse_product` is the only optional key" in normalized_stage_6
    for property_name in spec_required_properties:
        assert f"`{property_name}`" in normalized_checkpoint
    assert "spec` has 19 or 20 keys" in normalized_checkpoint
    assert "expanded GenerationSpec has 20 required keys plus that same optional key" in normalized_checkpoint

    for exact_cardinality in (
        "roles=2",
        "requirements=5",
        "anchors=2",
        "time_characteristics=4",
        "analysis_steps=5",
        "judgement_rules=6",
        "output_requirements=5",
        "assumptions=3",
    ):
        assert exact_cardinality in stage_6
    assert "merging compatible output semantics into exactly five items" in normalized_stage_6
    assert "Preserve every condition, scope, limitation, warning, and risk" in normalized_stage_6
    assert "literal_rule_segments" in normalized_checkpoint
    assert "literal_terminal_segments" in normalized_checkpoint
    assert "ordered_interval_family" in normalized_checkpoint
    assert "7+105+9+39+5=165" in compact_checkpoint
    assert "preserving all nine paths' first-match order" in normalized_checkpoint
    assert "at most 48 KiB" in normalized_checkpoint
    assert "versioned deterministic compiler in memory" in normalized_checkpoint
    assert "existing deep GenerationSpec loader and verification validator" in normalized_checkpoint


def test_release_conversion_keeps_declared_authoritative_matrices_complete() -> None:
    checked = 0
    for case_root in sorted(
        path.parent
        for path in (REPO_ROOT / "tests/cases/release").glob("*/case.json")
    ):
        descriptor = _json(case_root / "case.json")
        clarifications = _text(case_root / descriptor["clarifications"])
        if "权威机械矩阵" not in clarifications:
            continue
        spec = _json(case_root / descriptor["generation_spec"])
        contract = spec["verification_contract"]
        assert isinstance(contract, dict)

        declared_rules = re.search(r"保留[^\n]*?(\d+) 条 rule", clarifications)
        declared_paths = re.search(r"和(\d+|[一二三四五六七八九十]+)条 terminal path", clarifications)
        assert declared_rules is not None
        assert declared_paths is not None
        chinese_counts = {
            "一": 1,
            "二": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
        }
        expected_path_count = (
            int(declared_paths.group(1))
            if declared_paths.group(1).isdigit()
            else chinese_counts[declared_paths.group(1)]
        )
        extractors = contract["event_extractors"]
        rules = contract["rules"]
        paths = contract["terminal_paths"]
        assert isinstance(extractors, list)
        assert isinstance(rules, list)
        assert isinstance(paths, list)
        assert len(rules) == int(declared_rules.group(1))
        assert len(paths) == expected_path_count

        declared_views = set(
            re.findall(
                r"^\| `[^`]+` \| `([^`]+)` \| `[^`]+` \|",
                clarifications,
                flags=re.MULTILINE,
            )
        )
        assert declared_views
        extractor_ids = {
            item["id"] for item in extractors if isinstance(item, dict)
        }
        assert declared_views <= extractor_ids
        assert any(
            isinstance(item, dict) and str(item.get("id", "")).startswith("q_")
            for item in rules
        )
        assert "预生成 JSON" in clarifications
        assert "场景答案" in clarifications
        checked += 1
    assert checked > 0


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
