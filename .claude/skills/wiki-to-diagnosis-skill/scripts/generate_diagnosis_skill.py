from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any, Mapping, Sequence

from problem_locator.runtime.verification_contract import (
    MANIFEST_SCHEMA_VERSION,
    validate_verification_contract,
)


GENERATOR_VERSION = "5.0.0"
SPEC_SCHEMA_VERSION = 5
PRODUCT_FILES = ("SKILL.md", "diagnosis-skill.json")
DEPLOYMENT_SCOPES = frozenset({"PRODUCTION", "TEST_ONLY"})
LOG_ARCHIVE_CONTENT_TYPES = (
    "application/gzip",
    "application/zip",
    "application/x-tar",
)
SKILL_ID_PATTERN = re.compile(r"^diagnose-[a-z0-9]+(?:-[a-z0-9]+)*$")
CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)$"
)
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ROLE_LABEL_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
def _require_exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{name} fields are invalid; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}"
        )


def _single_line(value: Any, name: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\n" in value
        or "\r" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise ValueError(f"{name} must be a non-empty single line")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 65_536:
        raise ValueError(f"{name} must be non-empty UTF-8 text")
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _sequence(value: Any, name: str, *, maximum: int = 100) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{name} must be an array with at most {maximum} items")
    return value


def _text_tuple(value: Any, name: str, *, minimum: int = 0) -> tuple[str, ...]:
    result = tuple(
        _text(item, f"{name}[]") for item in _sequence(value, name)
    )
    if len(result) < minimum or len(result) != len(set(result)):
        raise ValueError(f"{name} cardinality or uniqueness is invalid")
    return result


@dataclass(frozen=True)
class Role:
    label: str
    description: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Role":
        _require_exact_keys(value, {"label", "description"}, "role")
        label = _single_line(value["label"], "role.label", maximum=64)
        if ROLE_LABEL_PATTERN.fullmatch(label) is None:
            raise ValueError("role.label is invalid")
        return cls(
            label=label,
            description=_single_line(
                value["description"], "role.description", maximum=512
            ),
        )


@dataclass(frozen=True)
class Requirement:
    name: str
    kind: str
    stage: str
    fulfillment_source: str
    prompt: str
    constraints: dict[str, Any]
    supplement_policy: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Requirement":
        _require_exact_keys(
            value,
            {
                "name",
                "kind",
                "stage",
                "fulfillment_source",
                "prompt",
                "constraints",
                "supplement_policy",
            },
            "requirement",
        )
        name = _single_line(value["name"], "requirement.name", maximum=64)
        if NAME_PATTERN.fullmatch(name) is None:
            raise ValueError("requirement.name must use S00 lower snake case")
        kind = value["kind"]
        stage = value["stage"]
        source = value["fulfillment_source"]
        if kind not in {"INPUT", "ATTACHMENT"}:
            raise ValueError("requirement.kind must be INPUT or ATTACHMENT")
        if stage not in {"INITIAL", "AFTER_LOGPARSE"}:
            raise ValueError("requirement.stage is invalid")
        if (kind, source) not in {
            ("INPUT", "USER_FACT"),
            ("ATTACHMENT", "READY_ATTACHMENT"),
        }:
            raise ValueError("requirement fulfillment source does not match kind")
        if stage == "AFTER_LOGPARSE" and kind != "INPUT":
            raise ValueError("AFTER_LOGPARSE supports INPUT requirements only")
        constraints = value["constraints"]
        if not isinstance(constraints, dict):
            raise ValueError("requirement.constraints must be an object")
        if kind == "INPUT":
            _require_exact_keys(
                constraints,
                {
                    "value_type",
                    "min_utf8_bytes",
                    "max_utf8_bytes",
                    "pattern",
                    "allowed_values",
                },
                "INPUT constraints",
            )
            minimum = constraints["min_utf8_bytes"]
            maximum = constraints["max_utf8_bytes"]
            if (
                constraints["value_type"] != "STRING"
                or type(minimum) is not int
                or type(maximum) is not int
                or not 1 <= minimum <= maximum <= 65_536
            ):
                raise ValueError("INPUT byte constraints are invalid")
            pattern = constraints["pattern"]
            if pattern is not None:
                if not isinstance(pattern, str):
                    raise ValueError("INPUT pattern must be a string or null")
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError("INPUT pattern is invalid") from exc
            allowed = constraints["allowed_values"]
            if (
                not isinstance(allowed, list)
                or any(not isinstance(item, str) or not item for item in allowed)
                or len(allowed) != len(set(allowed))
            ):
                raise ValueError("INPUT allowed_values are invalid")
        else:
            _require_exact_keys(
                constraints,
                {"allowed_content_types", "min_count", "max_count"},
                "ATTACHMENT constraints",
            )
            allowed = constraints["allowed_content_types"]
            minimum = constraints["min_count"]
            maximum = constraints["max_count"]
            if (
                not isinstance(allowed, list)
                or any(not isinstance(item, str) or not item for item in allowed)
                or len(allowed) != len(set(allowed))
                or type(minimum) is not int
                or type(maximum) is not int
                or not 1 <= minimum <= maximum
            ):
                raise ValueError("ATTACHMENT constraints are invalid")
        supplement_policy = value["supplement_policy"]
        if supplement_policy not in {"NONE", "MISSING_ONLY"}:
            raise ValueError(
                "requirement.supplement_policy must be NONE or MISSING_ONLY"
            )
        return cls(
            name=name,
            kind=kind,
            stage=stage,
            fulfillment_source=source,
            prompt=_single_line(value["prompt"], "requirement.prompt", maximum=4096),
            constraints=dict(constraints),
            supplement_policy=supplement_policy,
        )

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


def _binding(value: Any, name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    source = value.get("source")
    if source == "USER_FACT":
        _require_exact_keys(value, {"source", "name"}, name)
        bound = _single_line(value["name"], f"{name}.name", maximum=64)
        if NAME_PATTERN.fullmatch(bound) is None:
            raise ValueError(f"{name}.name is invalid")
        return {"source": source, "name": bound}
    if source == "SKILL_FIXED":
        _require_exact_keys(value, {"source", "value"}, name)
        return {
            "source": source,
            "value": _single_line(value["value"], f"{name}.value", maximum=4096),
        }
    raise ValueError(f"{name}.source must be USER_FACT or SKILL_FIXED")


def _logparse_plan(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("logparse_plan must be an object or null")
    _require_exact_keys(
        value,
        {"attachment_requirement", "problem_time_binding", "anchors"},
        "logparse_plan",
    )
    attachment = value["attachment_requirement"]
    if attachment is not None:
        attachment = _single_line(
            attachment, "logparse_plan.attachment_requirement", maximum=64
        )
    anchors = []
    labels: set[str] = set()
    for index, raw in enumerate(_sequence(value["anchors"], "logparse_plan.anchors", maximum=20)):
        if not isinstance(raw, dict):
            raise ValueError("logparse anchor must be an object")
        _require_exact_keys(
            raw,
            {"label", "module", "slot", "process_name", "pid"},
            "logparse anchor",
        )
        label = _single_line(raw["label"], "anchor.label", maximum=64)
        if label in labels:
            raise ValueError("anchor labels must be unique")
        labels.add(label)
        anchors.append(
            {
                "label": label,
                "module": _binding(raw["module"], f"anchors[{index}].module"),
                "slot": _binding(raw["slot"], f"anchors[{index}].slot"),
                "process_name": _binding(
                    raw["process_name"], f"anchors[{index}].process_name"
                ),
                "pid": None
                if raw["pid"] is None
                else _binding(raw["pid"], f"anchors[{index}].pid"),
            }
        )
    if not anchors:
        raise ValueError("logparse_plan requires at least one anchor")
    return {
        "attachment_requirement": attachment,
        "problem_time_binding": _binding(
            value["problem_time_binding"], "problem_time_binding"
        ),
        "anchors": anchors,
    }


def _normalize_requirement_mappings(
    value: Any,
    logparse_plan: dict[str, Any] | None,
) -> list[Mapping[str, Any]]:
    """Inject platform-owned constraints without turning them into author inputs."""

    raw_requirements = _sequence(value, "requirements", maximum=64)
    logparse_attachment = (
        None
        if logparse_plan is None
        else logparse_plan["attachment_requirement"]
    )
    normalized: list[Mapping[str, Any]] = []
    for raw in raw_requirements:
        if not isinstance(raw, Mapping):
            raise ValueError("requirement must be an object")
        requirement = dict(raw)
        constraints = requirement.get("constraints")
        if (
            logparse_attachment is not None
            and requirement.get("name") == logparse_attachment
            and requirement.get("kind") == "ATTACHMENT"
            and isinstance(constraints, Mapping)
        ):
            normalized_constraints = dict(constraints)
            normalized_constraints.setdefault(
                "allowed_content_types",
                list(LOG_ARCHIVE_CONTENT_TYPES),
            )
            requirement["constraints"] = normalized_constraints
        normalized.append(requirement)
    return normalized


@dataclass(frozen=True)
class GenerationSpec:
    skill_id: str
    version: str
    capability: str
    deployment_scope: str
    summary: str
    chinese_title: str
    module_name: str | None
    problem_scope: str
    roles: tuple[Role, ...]
    requirements: tuple[Requirement, ...]
    logparse_plan: dict[str, Any] | None
    verification_contract: dict[str, Any]
    time_characteristics: tuple[str, ...]
    analysis_steps: tuple[str, ...]
    judgement_rules: tuple[str, ...]
    output_requirements: tuple[str, ...]
    assumptions: tuple[str, ...]
    requires_logparse: bool
    logparse_product: str | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GenerationSpec":
        required = {
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
        optional = {"logparse_product"}
        actual = set(value)
        if not required <= actual or actual - required - optional:
            raise ValueError("generation spec field set is invalid")
        if value["schema_version"] != SPEC_SCHEMA_VERSION:
            raise ValueError("generation spec schema_version must be 5")
        if value["generator_version"] != GENERATOR_VERSION:
            raise ValueError(f"generator_version must be {GENERATOR_VERSION}")
        requires_logparse = value["requires_logparse"]
        if type(requires_logparse) is not bool:
            raise ValueError("requires_logparse must be a boolean")
        module_name = value["module_name"]
        if module_name is not None:
            module_name = _single_line(module_name, "module_name", maximum=128)
        product = value.get("logparse_product")
        if product is not None:
            product = _single_line(product, "logparse_product", maximum=4096)
        logparse_plan = _logparse_plan(value["logparse_plan"])
        requirements = tuple(
            Requirement.from_mapping(item)
            for item in _normalize_requirement_mappings(
                value["requirements"],
                logparse_plan,
            )
        )
        roles = tuple(
            Role.from_mapping(item)
            for item in _sequence(value["roles"], "roles", maximum=20)
        )
        verification_contract = validate_verification_contract(
            value["verification_contract"],
            requirements=tuple(item.to_mapping() for item in requirements),
            anchor_labels=(
                set()
                if logparse_plan is None
                else {item["label"] for item in logparse_plan["anchors"]}
            ),
            role_labels={item.label for item in roles},
            requires_logparse=requires_logparse,
        )
        spec = cls(
            skill_id=_single_line(value["id"], "id", maximum=64),
            version=_single_line(value["version"], "version", maximum=64),
            capability=_single_line(value["capability"], "capability", maximum=64),
            deployment_scope=_single_line(
                value["deployment_scope"], "deployment_scope", maximum=32
            ),
            summary=_single_line(value["summary"], "summary", maximum=4096),
            chinese_title=_single_line(
                value["chinese_title"], "chinese_title", maximum=256
            ),
            module_name=module_name,
            problem_scope=_text(value["problem_scope"], "problem_scope"),
            roles=roles,
            requirements=requirements,
            logparse_plan=logparse_plan,
            verification_contract=verification_contract,
            time_characteristics=_text_tuple(
                value["time_characteristics"], "time_characteristics"
            ),
            analysis_steps=_text_tuple(
                value["analysis_steps"], "analysis_steps", minimum=1
            ),
            judgement_rules=_text_tuple(
                value["judgement_rules"], "judgement_rules", minimum=1
            ),
            output_requirements=_text_tuple(
                value["output_requirements"], "output_requirements", minimum=1
            ),
            assumptions=_text_tuple(value["assumptions"], "assumptions"),
            requires_logparse=requires_logparse,
            logparse_product=product,
        )
        spec.validate()
        return spec

    def validate(self) -> None:
        if SKILL_ID_PATTERN.fullmatch(self.skill_id) is None:
            raise ValueError("id must match diagnose-<lower-kebab-capability>")
        match = SEMVER_PATTERN.fullmatch(self.version)
        if match is None or int(match.group("major")) < 5:
            raise ValueError("generated Skill version must be semantic version 5.0.0 or later")
        if CAPABILITY_PATTERN.fullmatch(self.capability) is None:
            raise ValueError("capability is invalid")
        if self.deployment_scope not in DEPLOYMENT_SCOPES:
            raise ValueError("deployment_scope must be PRODUCTION or TEST_ONLY")
        names = [item.name for item in self.requirements]
        if len(names) != len(set(names)):
            raise ValueError("requirement names must be unique")
        stages = [item.stage for item in self.requirements]
        if stages != sorted(stages, key={"INITIAL": 0, "AFTER_LOGPARSE": 1}.get):
            raise ValueError("requirements must be ordered by stage")
        for stage in {"INITIAL", "AFTER_LOGPARSE"}:
            if sum(
                item.kind == "ATTACHMENT" and item.stage == stage
                for item in self.requirements
            ) > 1:
                raise ValueError("at most one ATTACHMENT is supported per stage")
        if self.requires_logparse:
            if self.logparse_plan is None:
                raise ValueError("requires_logparse=true requires logparse_plan")
            if self.logparse_product == "default":
                raise ValueError("omit logparse_product to select the upstream default")
            attachment = self.logparse_plan["attachment_requirement"]
            if attachment is not None:
                requirement = next(
                    (item for item in self.requirements if item.name == attachment),
                    None,
                )
                if requirement is None or requirement.kind != "ATTACHMENT":
                    raise ValueError("logparse attachment must name an ATTACHMENT requirement")
                if tuple(requirement.constraints["allowed_content_types"]) != LOG_ARCHIVE_CONTENT_TYPES:
                    raise ValueError("logparse archive ContentTypes are platform-fixed")
            input_names = {
                item.name for item in self.requirements if item.kind == "INPUT"
            }
            bindings = [self.logparse_plan["problem_time_binding"]]
            bindings.extend(
                anchor[field]
                for anchor in self.logparse_plan["anchors"]
                for field in ("module", "slot", "process_name", "pid")
                if anchor[field] is not None
            )
            if any(
                binding["source"] == "USER_FACT"
                and binding["name"] not in input_names
                for binding in bindings
            ):
                raise ValueError("USER_FACT tool bindings must name INPUT requirements")
        else:
            if self.logparse_plan is not None or self.logparse_product is not None:
                raise ValueError("non-logparse Skill forbids logparse plan/product")
            if any(item.stage == "AFTER_LOGPARSE" for item in self.requirements):
                raise ValueError("non-logparse Skill forbids AFTER_LOGPARSE requirements")
        self._validate_verification_contract()

    def _validate_verification_contract(self) -> None:
        normalized = validate_verification_contract(
            self.verification_contract,
            requirements=tuple(item.to_mapping() for item in self.requirements),
            anchor_labels=(
                set()
                if self.logparse_plan is None
                else {item["label"] for item in self.logparse_plan["anchors"]}
            ),
            role_labels={item.label for item in self.roles},
            requires_logparse=self.requires_logparse,
        )
        if normalized != self.verification_contract:
            raise ValueError("verification contract is not canonically normalized")

@dataclass(frozen=True)
class GenerationResult:
    skill_dir: Path
    product_sha256: str
    created: bool
    replaced: bool


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def diagnosis_skill_manifest(spec: GenerationSpec) -> dict[str, Any]:
    spec.validate()
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "id": spec.skill_id,
        "version": spec.version,
        "capability": spec.capability,
        "deployment_scope": spec.deployment_scope,
        "summary": spec.summary,
        "entry_document": "SKILL.md",
        "tool_bundle_id": "tool-bundle/diagnose",
        "requires_logparse": spec.requires_logparse,
        "requirements": [item.to_mapping() for item in spec.requirements],
        "logparse_plan": spec.logparse_plan,
        "verification_contract": spec.verification_contract,
    }
    if spec.logparse_product is not None:
        manifest["logparse_product"] = spec.logparse_product
    return manifest


def _markdown_list(values: Sequence[str], fallback: str) -> str:
    return "\n".join(f"- {value}" for value in values) if values else f"- {fallback}"


def _render_skill_markdown(spec: GenerationSpec) -> str:
    manifest = diagnosis_skill_manifest(spec)
    embedded = canonical_json_bytes(manifest).decode("utf-8").strip()
    rows = "\n".join(
        f"| `{item.name}` | {item.kind} | {item.stage} | {item.fulfillment_source} | {item.supplement_policy} | {item.prompt.replace('|', '\\|')} | `{json.dumps(item.constraints, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}` |"
        for item in spec.requirements
    ) or "| — | — | — | — | 本 Skill 不请求业务输入或附件 | `{}` |"
    roles = "\n".join(
        f"- `{role.label}`：{role.description}" for role in spec.roles
    ) or "- 未声明业务角色。"
    initial_workflow = """按声明顺序执行阶段算法：先复用当前快照中有效事实和同名 OPEN requirement；请求当前
阶段全部缺失 INPUT 并返回 NEED_INPUT；INPUT 齐全后才请求该阶段 ATTACHMENT 并返回
NEED_ATTACHMENT。"""
    has_after_logparse = any(
        item.stage == "AFTER_LOGPARSE" for item in spec.requirements
    )
    if has_after_logparse:
        stage_workflow = initial_workflow + """
INITIAL 齐全后才可进入工具/分析；parse 成功后再检查 AFTER_LOGPARSE。缺少后补输入时，
必须提出必要 LOGPARSE Evidence，并把每个需要跨 Job 保留的 Evidence proposal 写入
`state_delta.add_evidence_bindings`：`existing_evidence_id=null`，
`evidence_proposal_key` 等于对应 proposal key。仅写 proposal、Finding 或说明文字不会
触发接收。每个新 Evidence 还必须用 `artifact_proposal_key` 绑定 broker 返回的同一
Outcome `LOGPARSE_RUN` proposal，使平台共同接收 Evidence 与运行产物；完成这些绑定后
才返回 NEED_INPUT。续跑必须复用正式 Evidence 与 LOGPARSE_RUN，并调用 `target-logs`，
禁止再次 `parse-targets`。工具输出只可形成 Evidence、Finding 或 proposed fact，绝不能
满足 USER_FACT requirement。"""
    elif spec.requires_logparse:
        stage_workflow = initial_workflow + """
INITIAL 齐全后才可进入 Logparse 工具与分析。本 Skill 不声明 parse 后补参；工具输出只可
形成 Evidence、Finding 或 proposed fact，绝不能满足 USER_FACT requirement。"""
    else:
        stage_workflow = initial_workflow + """
INITIAL 齐全后直接执行人工分析。本 Skill 不存在 Logparse 或 parse 后补参阶段。"""
    if spec.requires_logparse:
        product = spec.logparse_product or "default（使用 Logparse 上游默认值）"
        logparse_section = f"""## Logparse 业务映射

本 Skill 需要 Logparse；有效 product 为 `{product}`。产品省略时 Runtime 不向上游传
`--product`，但运行 metadata 仍记录 `default`。加载 `logparse-diagnose` 并严格执行其
broker、Canonical request、parse-once、LOGPARSE_RUN 复用及路径安全规则。

形成 LOGPARSE Evidence 时，`workspace_relative_path` 必须为 null；目标日志位置只写在
`locator.relative_path`，并通过同一 Outcome 的 `artifact_proposal_key` 或已有 Artifact
ID 绑定 LOGPARSE_RUN。不得把 LOGPARSE_RUN tree 内路径填成 Evidence 自己的 proposal
路径；任何非 null workspace path 都必须位于该 proposal key 的独立目录下。
构造 broker anchor 时，`label/module/slot/process_name` 必须保持 JSON string 并逐字复制
已解析 binding；即使值看起来像数字也禁止改变 JSON 类型。
新 `LOGPARSE_RUN.metadata` 必须严格且仅含 `tree_manifest_sha256`、
`logparse_version_ref`、`parse_manifest_relative_path`、`source_attachment_id`、
`source_attachment_sha256`、`parse_parameters` 六个字段；`parse_parameters` 仅含有效
`product`。禁止添加 `schema_version`、`format_id`、`description` 或其他通用字段。
Artifact draft 外壳固定为 `artifact_kind=LOGPARSE_RUN`、
`content_type=application/vnd.problem-locator.logparse-run+directory`、
`resource_kind=DIRECTORY`，且 `declared_size`、`declared_sha256` 均为 null；禁止自行猜测
MIME type 或计算 broker 受控树的 size/hash。
`parse-targets` 成功后必须把结果中的 `logparse_run_artifact_draft` 对象逐字段原样放入
`proposed_artifact_drafts`；禁止自行构造、扩展版本字符串或修改任何值。

业务映射的机器事实如下，不得改名、猜值或从日志反向满足 USER_FACT requirement：

```json
{json.dumps(spec.logparse_plan, ensure_ascii=False, sort_keys=True, indent=2)}
```

归档附件只接受平台固定后缀映射：`.gz/.tar.gz/.tgz -> application/gzip`、
`.zip -> application/zip`、`.tar -> application/x-tar`。Content-Type 不是生成参数。
"""
    else:
        logparse_section = """## 工具边界

本 Skill `requires_logparse=false`：禁止加载 `logparse-diagnose`、调用
`problem-locator-logparse`、请求日志归档、提出 LOGPARSE_RUN，或读取 raw Logparse 配置。
"""
    return f"""---
name: {spec.skill_id}
description: {json.dumps(spec.summary, ensure_ascii=False)}
---

# {spec.chinese_title}

由 `wiki-to-diagnosis-skill` generator `{GENERATOR_VERSION}` 生成。公共 DIAGNOSE output
contract 只定义通用 Schema、安全、Evidence/Candidate 与原子输出；本文件独占业务
requirements、阶段、工具映射和判定规则。

<!-- DIAGNOSIS_SKILL_MANIFEST_V5_BEGIN -->
```json
{embedded}
```
<!-- DIAGNOSIS_SKILL_MANIFEST_V5_END -->

## 范围与角色

{spec.problem_scope}

{roles}

## Requirements

所有声明均为必需项；空数组表示不添加任何默认参数。
INPUT 只能由 `USER_FACT` 满足，ATTACHMENT 只能由 `READY_ATTACHMENT` 满足。

| 名称 | 类型 | 阶段 | 满足来源 | 补充策略 | 用户提示 | S00 constraints |
| --- | --- | --- | --- | --- | --- | --- |
{rows}

{stage_workflow}

{logparse_section}

## 机器验证合同

以下 `verification_contract` 是候选结论的机器门禁，不得用叙述、摘要或 Agent 自报结论替代。
逐条提交同一 rule ID 的证据声明；事件由服务端在对应 anchor 的 UTF-8 原始日志中重新组装、
筛选和计数。单行/多行成员、字段类型、单位、clock domain、选择器、基数、关联、时间窗、
数值表达式及终态路径均以合同明示值为准，不存在默认容差。`observation_policies` 声明的
抑制或限流可以让缺失/上界判断成为 UNKNOWN，但不能削弱已观测到的正向证据。

```json
{json.dumps(spec.verification_contract, ensure_ascii=False, sort_keys=True, indent=2)}
```

## 分析步骤

{_markdown_list(spec.analysis_steps, '无额外步骤。')}

## 时间特征

{_markdown_list(spec.time_characteristics, '无额外时间特征。')}

## 判定规则

{_markdown_list(spec.judgement_rules, '证据不足时保留缺口。')}

## 输出要求

{_markdown_list(spec.output_requirements, '遵守公共输出合同。')}

## 假设

{_markdown_list(spec.assumptions, '无额外假设。')}

## Candidate 与服务端用户结果

先按声明顺序重算全部规则，再选择第一条匹配的 `terminal_paths`。`COMPLETE` 或 `PARTIAL`
路径可以提出 Candidate，且 `resolution_status` 与 `terminal_path_id` 必须逐字绑定该路径；
`NONE` 路径禁止提出 Candidate。COMPLETE 的每个 completion criterion 都必须为
`SATISFIED`；PARTIAL 必须保留已证实进展，并把未完成 criterion 标成
`PARTIALLY_SATISFIED|UNSATISFIED|UNKNOWN`，不得伪装成完整结论。

Candidate 用 `causal_factors`、`candidate_factors` 和 `excluded_factors` 分别表达已证实因素、
仍待区分因素和已排除因素。每个 factor 必须绑定原始 Evidence 及实际支持它的 rule IDs；
允许多个共同贡献因素，不得为了给出单一根因而丢弃并发贡献或 UNKNOWN。

`supporting_evidence_bindings` 必须去重并保持当前快照 `evidence_refs` 的相对顺序；同一
Outcome 新接收的 Evidence 只按 `state_delta.add_evidence_bindings` 顺序追加。禁止按业务
角色、日志时间或叙述习惯重排。completion mapping 重复这些 binding 时也保持同一顺序；
这是 Coordinator 的固定子序列合同。

Agent 禁止提出或写入 `USER_RESULT`、`USER_RESULT_ARCHIVE`、`diagnosis-result.json`、
`result.zip` 或任何归档请求，也禁止自行调用 zip/tar。Agent draft 只提交 Candidate、
Evidence、rule claims 与合同允许的内部 Artifact proposal。Agent 进程退出后，Runtime
重读权威证据并完成机器验证；DIAGNOSE 草稿通过服务端验证后，服务端立即从已验证的
权威结果生成并持久化用户产物，但仅在独立 Review PASS 后开放公开下载。Agent 不得预先
构造、摘要或替代这些服务端产物。

## 原子交付

最终先写 `output/job_outcome.draft.json`，再把
`problem-locator-seal-outcome-draft` 作为最后一个修改 Workspace 的命令；成功后不得继续
写入 `output/`。sealer 只封存 Agent draft，不生成正式 Outcome、ID、时间或服务端验证结果。
Agent 进程退出后，Runtime 重新读取原始证据并按 manifest 重算机械规则，再生成唯一权威的
`output/job_outcome.json`。stdout/stderr、隐藏思维过程和部分文件不是业务结果。
"""


def render_product(spec: GenerationSpec) -> dict[str, bytes]:
    files = {
        "SKILL.md": _render_skill_markdown(spec).encode("utf-8"),
        "diagnosis-skill.json": canonical_json_bytes(diagnosis_skill_manifest(spec)),
    }
    if tuple(sorted(files)) != tuple(sorted(PRODUCT_FILES)):
        raise AssertionError("generated product file-set drift")
    return files


def product_sha256(files: Mapping[str, bytes]) -> str:
    entries = [
        {
            "path": path,
            "size": len(files[path]),
            "sha256": hashlib.sha256(files[path]).hexdigest(),
        }
        for path in sorted(files)
    ]
    return hashlib.sha256(
        canonical_json_bytes({"version": 1, "entries": entries})
    ).hexdigest()


def _write_product(directory: Path, files: Mapping[str, bytes]) -> None:
    directory.mkdir()
    for name in PRODUCT_FILES:
        path = directory / name
        with path.open("xb") as stream:
            stream.write(files[name])
            stream.flush()
            os.fsync(stream.fileno())


def _read_existing(directory: Path) -> dict[str, bytes]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("existing Skill product must be a real directory")
    names = sorted(item.name for item in directory.iterdir())
    if names != sorted(PRODUCT_FILES):
        raise ValueError("existing Skill product has an unexpected file set")
    result = {}
    for name in PRODUCT_FILES:
        path = directory / name
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("existing Skill product contains an unsafe file")
        result[name] = path.read_bytes()
    return result


def generate_diagnosis_skill(
    spec: GenerationSpec,
    output_root: str | Path,
    *,
    replace_different_version: bool = False,
) -> GenerationResult:
    files = render_product(spec)
    desired_hash = product_sha256(files)
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("output_root must be a real directory")
    target = root / spec.skill_id
    if target.exists():
        existing = _read_existing(target)
        if existing == files:
            return GenerationResult(target, desired_hash, created=False, replaced=False)
        try:
            old_manifest = json.loads(existing["diagnosis-skill.json"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("existing diagnosis-skill.json is invalid") from exc
        if old_manifest.get("version") == spec.version:
            raise ValueError("same Skill version cannot be overwritten with different semantics")
        if not replace_different_version:
            raise ValueError("different Skill version requires --replace-different-version")

    temporary = Path(tempfile.mkdtemp(prefix=f".{spec.skill_id}.", dir=root))
    backup: Path | None = None
    replaced = target.exists()
    try:
        temporary.rmdir()
        _write_product(temporary, files)
        if replaced:
            backup = root / f".{spec.skill_id}.backup"
            if backup.exists():
                raise ValueError("stale replacement backup exists")
            os.replace(target, backup)
        os.replace(temporary, target)
        if backup is not None:
            shutil.rmtree(backup)
    except Exception:
        if backup is not None and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return GenerationResult(target, desired_hash, created=not replaced, replaced=replaced)


def normalize_wiki(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("wiki text must be a string")
    text = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip() + "\n"


_AUTHOR_NOTE = re.compile(r"(?s)(?:\(#.*?#\)|（#.*?#）)")


def strip_author_notes(wiki_text: str) -> str:
    """Remove the two author-note forms from material copied into products.

    A conversion Agent may read these notes as author guidance.  They are not
    diagnosis knowledge and must never be copied into a generated product.
    """

    wiki = normalize_wiki(wiki_text)
    stripped = _AUTHOR_NOTE.sub("", wiki)
    if "(#" in stripped or "（#" in stripped or "#)" in stripped or "#）" in stripped:
        raise ValueError("wiki contains an unterminated author note")
    return normalize_wiki(stripped)


def build_spec_from_wiki(wiki_text: str, **overrides: Any) -> GenerationSpec:
    """Read one Agent-authored fenced GenerationSpec v5 from a wiki document.

    Natural-language Wiki interpretation belongs to the conversion Agent.  The
    deterministic renderer consumes the Agent's explicit spec and never guesses
    business rules with heuristic NLP.
    """

    wiki = normalize_wiki(wiki_text)
    matches = re.findall(
        r"(?ms)^## GenerationSpec v5\s*$.*?^```json\s*$\n(.*?)^```\s*$",
        wiki,
    )
    if len(matches) != 1:
        raise ValueError(
            "deterministic rendering requires exactly one Agent-authored "
            "'## GenerationSpec v5' JSON fence"
        )
    try:
        value = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise ValueError("wiki GenerationSpec v5 JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("wiki GenerationSpec v5 must be an object")
    allowed_overrides = {"capability", "summary", "version"}
    unknown = set(overrides) - allowed_overrides
    if unknown:
        raise ValueError(f"unsupported wiki overrides: {sorted(unknown)!r}")
    key_by_override = {"capability": "capability", "summary": "summary", "version": "version"}
    for name, override in overrides.items():
        if override is not None:
            value[key_by_override[name]] = override
    return GenerationSpec.from_mapping(value)


def load_generation_spec(path: str | Path) -> GenerationSpec:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("generation spec must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("generation spec must be an object")
    return GenerationSpec.from_mapping(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate one deterministic Problem Locator Diagnosis Skill v5."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec", type=Path)
    source.add_argument("--wiki", type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--replace-different-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        spec = (
            load_generation_spec(args.spec)
            if args.spec is not None
            else build_spec_from_wiki(args.wiki.read_text(encoding="utf-8"))
        )
        result = generate_diagnosis_skill(
            spec,
            args.output_root,
            replace_different_version=args.replace_different_version,
        )
    except (OSError, TypeError, ValueError) as exc:
        parser.exit(2, f"generate_diagnosis_skill: {exc}\n")
    print(
        canonical_json_bytes(
            {
                "created": result.created,
                "path": result.skill_dir.as_posix(),
                "product_sha256": result.product_sha256,
                "replaced": result.replaced,
            }
        ).decode("utf-8"),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
