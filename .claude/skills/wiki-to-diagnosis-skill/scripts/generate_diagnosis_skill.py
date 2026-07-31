from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any, Mapping, Sequence


GENERATOR_VERSION = "2.0.0"
SPEC_SCHEMA_VERSION = 1
MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "version",
        "capability",
        "summary",
        "entry_document",
        "tool_bundle_id",
        "requires_logparse",
        "logparse_product",
    }
)
PRODUCT_FILES = ("SKILL.md", "diagnosis-skill.json")
SKILL_ID_PATTERN = re.compile(r"^diagnose-[a-z0-9]+(?:-[a-z0-9]+)*$")
CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
SEMVER_PATTERN = re.compile(r"^(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)$")
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ROLE_LABEL_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
CONTENT_TYPE_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+\-]{0,62}/[a-z0-9][a-z0-9!#$&^_.+\-]{0,62}$"
)
UTC_MILLIS_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"


@dataclass(frozen=True)
class Role:
    label: str
    description: str
    required: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Role":
        _require_exact_keys(value, {"label", "description", "required"}, "role")
        label = _single_line(value["label"], "role.label", maximum=64)
        description = _single_line(value["description"], "role.description", maximum=512)
        required = value["required"]
        if ROLE_LABEL_PATTERN.fullmatch(label) is None:
            raise ValueError("role.label must match ^[a-z][a-z0-9_-]{0,63}$")
        if type(required) is not bool:
            raise ValueError("role.required must be a boolean")
        return cls(label=label, description=description, required=required)


@dataclass(frozen=True)
class CustomParameter:
    name: str
    description: str
    required: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CustomParameter":
        _require_exact_keys(value, {"name", "description", "required"}, "custom parameter")
        name = _single_line(value["name"], "custom_parameter.name", maximum=64)
        description = _single_line(
            value["description"], "custom_parameter.description", maximum=512
        )
        required = value["required"]
        if NAME_PATTERN.fullmatch(name) is None:
            raise ValueError("custom_parameter.name must use S00 lower snake case")
        if type(required) is not bool:
            raise ValueError("custom_parameter.required must be a boolean")
        return cls(name=name, description=description, required=required)


@dataclass(frozen=True)
class GenerationSpec:
    skill_id: str
    version: str
    capability: str
    summary: str
    chinese_title: str
    module_name: str
    problem_scope: str
    roles: tuple[Role, ...]
    custom_parameters: tuple[CustomParameter, ...]
    time_characteristics: tuple[str, ...]
    analysis_steps: tuple[str, ...]
    judgement_rules: tuple[str, ...]
    output_requirements: tuple[str, ...]
    assumptions: tuple[str, ...]
    requires_logparse: bool
    logparse_product: str | None
    allowed_content_types: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GenerationSpec":
        expected = {
            "schema_version",
            "generator_version",
            "id",
            "version",
            "capability",
            "summary",
            "chinese_title",
            "module_name",
            "problem_scope",
            "roles",
            "custom_parameters",
            "time_characteristics",
            "analysis_steps",
            "judgement_rules",
            "output_requirements",
            "assumptions",
            "requires_logparse",
            "logparse_product",
            "allowed_content_types",
        }
        _require_exact_keys(value, expected, "generation spec")
        if value["schema_version"] != SPEC_SCHEMA_VERSION:
            raise ValueError("generation spec schema_version must be 1")
        if value["generator_version"] != GENERATOR_VERSION:
            raise ValueError(f"generator_version must be {GENERATOR_VERSION}")
        requires_logparse = value["requires_logparse"]
        if type(requires_logparse) is not bool:
            raise ValueError("requires_logparse must be a boolean")
        raw_product = value["logparse_product"]
        logparse_product = (
            None
            if raw_product is None
            else _single_line(raw_product, "logparse_product", maximum=65536)
        )
        spec = cls(
            skill_id=_single_line(value["id"], "id", maximum=64),
            version=_single_line(value["version"], "version", maximum=64),
            capability=_single_line(value["capability"], "capability", maximum=64),
            summary=_single_line(value["summary"], "summary", maximum=4096),
            chinese_title=_single_line(value["chinese_title"], "chinese_title", maximum=256),
            module_name=_single_line(value["module_name"], "module_name", maximum=128),
            problem_scope=_text(value["problem_scope"], "problem_scope"),
            roles=tuple(
                Role.from_mapping(item)
                for item in _mapping_list(value["roles"], "roles", minimum=1, maximum=20)
            ),
            custom_parameters=tuple(
                CustomParameter.from_mapping(item)
                for item in _mapping_list(
                    value["custom_parameters"], "custom_parameters", minimum=0, maximum=32
                )
            ),
            time_characteristics=_text_tuple(value["time_characteristics"], "time_characteristics"),
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
            logparse_product=logparse_product,
            allowed_content_types=tuple(
                _single_line(item, "allowed_content_types[]", maximum=127)
                for item in _sequence(value["allowed_content_types"], "allowed_content_types")
            ),
        )
        spec.validate()
        return spec

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SPEC_SCHEMA_VERSION,
            "generator_version": GENERATOR_VERSION,
            "id": self.skill_id,
            "version": self.version,
            "capability": self.capability,
            "summary": self.summary,
            "chinese_title": self.chinese_title,
            "module_name": self.module_name,
            "problem_scope": self.problem_scope,
            "roles": [role.__dict__ for role in self.roles],
            "custom_parameters": [parameter.__dict__ for parameter in self.custom_parameters],
            "time_characteristics": list(self.time_characteristics),
            "analysis_steps": list(self.analysis_steps),
            "judgement_rules": list(self.judgement_rules),
            "output_requirements": list(self.output_requirements),
            "assumptions": list(self.assumptions),
            "requires_logparse": self.requires_logparse,
            "logparse_product": self.logparse_product,
            "allowed_content_types": list(self.allowed_content_types),
        }

    def validate(self) -> None:
        if SKILL_ID_PATTERN.fullmatch(self.skill_id) is None:
            raise ValueError("id must match diagnose-<lower-kebab-capability>")
        match = SEMVER_PATTERN.fullmatch(self.version)
        if match is None or int(match.group("major")) < 2:
            raise ValueError("generated Skill version must be semantic version 2.0.0 or later")
        if CAPABILITY_PATTERN.fullmatch(self.capability) is None:
            raise ValueError("capability must be a stable lower-kebab identifier")
        _unique([role.label for role in self.roles], "role labels")
        if not any(role.required for role in self.roles):
            raise ValueError("at least one role must be required")
        _unique([parameter.name for parameter in self.custom_parameters], "custom parameter names")
        reserved = {
            "caller_service",
            "server_service",
            "rpc_method",
            "problem_time",
            "log_archive",
        }
        if reserved.intersection(parameter.name for parameter in self.custom_parameters):
            raise ValueError("custom parameters must not shadow fixed S07 requirement names")
        if self.requires_logparse:
            if self.logparse_product is None:
                raise ValueError("requires_logparse=true requires a non-empty logparse_product")
            if not self.allowed_content_types:
                raise ValueError("a logparse Skill requires at least one allowed ContentType")
        elif self.logparse_product is not None or self.allowed_content_types:
            raise ValueError(
                "requires_logparse=false requires logparse_product=null and no allowed ContentTypes"
            )
        _unique(list(self.allowed_content_types), "allowed ContentTypes")
        for content_type in self.allowed_content_types:
            validate_content_type(content_type)


@dataclass(frozen=True)
class GenerationResult:
    skill_dir: Path
    product_sha256: str
    created: bool
    replaced: bool


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def validate_content_type(value: str) -> str:
    if not isinstance(value, str) or not value.isascii():
        raise ValueError("ContentType must be ASCII")
    if not 3 <= len(value) <= 127 or CONTENT_TYPE_PATTERN.fullmatch(value) is None:
        raise ValueError("ContentType does not satisfy the S00 Canonical grammar")
    return value


def diagnosis_skill_manifest(spec: GenerationSpec) -> dict[str, Any]:
    spec.validate()
    manifest = {
        "schema_version": 1,
        "id": spec.skill_id,
        "version": spec.version,
        "capability": spec.capability,
        "summary": spec.summary,
        "entry_document": "SKILL.md",
        "tool_bundle_id": "tool-bundle/diagnose",
        "requires_logparse": spec.requires_logparse,
        "logparse_product": spec.logparse_product,
    }
    if set(manifest) != MANIFEST_FIELDS:
        raise AssertionError("internal diagnosis-skill.json field drift")
    return manifest


def render_product(spec: GenerationSpec) -> dict[str, bytes]:
    spec.validate()
    files = {
        "SKILL.md": _render_skill_markdown(spec).encode("utf-8"),
        "diagnosis-skill.json": canonical_json_bytes(diagnosis_skill_manifest(spec)),
    }
    if tuple(sorted(files)) != tuple(sorted(PRODUCT_FILES)):
        raise AssertionError("generated product file-set drift")
    return files


def product_sha256(files: Mapping[str, bytes]) -> str:
    entries = []
    for path in sorted(files):
        payload = files[path]
        if not _safe_relative_posix(path):
            raise ValueError(f"unsafe product path: {path}")
        entries.append(
            {"path": path, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        )
    return hashlib.sha256(canonical_json_bytes({"version": 1, "entries": entries})).hexdigest()


def generate_diagnosis_skill(
    spec: GenerationSpec,
    output_root: str | Path,
    *,
    replace_different_version: bool = False,
) -> GenerationResult:
    """Generate one deterministic product without overwriting changed same-version bytes."""

    spec.validate()
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / spec.skill_id
    desired = render_product(spec)
    desired_hash = product_sha256(desired)
    replaced = False

    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise FileExistsError(f"generated Skill target is not a plain directory: {target}")
        existing = _read_product_tree(target)
        existing_hash = product_sha256(existing)
        existing_manifest = _decode_manifest(existing.get("diagnosis-skill.json"))
        same_identity = (
            existing_manifest is not None
            and existing_manifest.get("id") == spec.skill_id
            and existing_manifest.get("version") == spec.version
        )
        if same_identity and existing_hash != desired_hash:
            raise FileExistsError(
                "refusing to overwrite the same diagnosis Skill id/version with different "
                f"product bytes: existing={existing_hash} requested={desired_hash}"
            )
        if existing_hash == desired_hash:
            return GenerationResult(target, desired_hash, created=False, replaced=False)
        if not replace_different_version:
            raise FileExistsError(
                "target contains a different product version; pass "
                "--replace-different-version only after explicitly increasing version"
            )
        if existing_manifest is None or existing_manifest.get("id") != spec.skill_id:
            raise FileExistsError("refusing to replace a target owned by another Skill id")
        replaced = True

    temp = Path(tempfile.mkdtemp(prefix=f".{spec.skill_id}.", dir=output_root))
    backup: Path | None = None
    try:
        for relative_path, payload in desired.items():
            destination = temp / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        if replaced:
            backup = Path(tempfile.mkdtemp(prefix=f".{spec.skill_id}.backup.", dir=output_root))
            backup.rmdir()
            os.replace(target, backup)
        os.replace(temp, target)
        if backup is not None:
            shutil.rmtree(backup)
    except BaseException:
        if backup is not None and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    finally:
        if temp.exists():
            shutil.rmtree(temp)
        if backup is not None and backup.exists():
            shutil.rmtree(backup)
    return GenerationResult(target, desired_hash, created=not replaced, replaced=replaced)


def normalize_wiki(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("wiki text must be a string")
    text = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip() + "\n"


def build_spec_from_wiki(
    wiki_text: str,
    *,
    capability: str,
    summary: str,
    version: str = "2.0.0",
    requires_logparse: bool = True,
    logparse_product: str | None,
    allowed_content_types: Sequence[str],
    assumptions: Sequence[str] = (),
) -> GenerationSpec:
    wiki = normalize_wiki(wiki_text)
    sections = _wiki_sections(wiki)
    basic = _wiki_basic_info(sections.get("基本信息", ""))
    skill_id = basic.get("skill_name") or f"diagnose-{capability}"
    raw: dict[str, Any] = {
        "schema_version": 1,
        "generator_version": GENERATOR_VERSION,
        "id": skill_id,
        "version": version,
        "capability": capability,
        "summary": summary,
        "chinese_title": basic.get("title") or capability,
        "module_name": basic.get("module_name"),
        "problem_scope": _section_prose(sections.get("问题范围", "")),
        "roles": _wiki_roles(sections.get("目标进程角色", "")),
        "custom_parameters": _wiki_custom_parameters(
            sections.get("自定义定位参数候选", "")
        ),
        "time_characteristics": _section_items(sections.get("时间特征", "")),
        "analysis_steps": _section_items(sections.get("定位步骤", "")),
        "judgement_rules": _section_items(sections.get("判断规则", "")),
        "output_requirements": _section_items(sections.get("输出要求", "")),
        "assumptions": list(assumptions),
        "requires_logparse": requires_logparse,
        "logparse_product": logparse_product,
        "allowed_content_types": list(allowed_content_types),
    }
    return GenerationSpec.from_mapping(raw)


def load_generation_spec(path: str | Path) -> GenerationSpec:
    data = Path(path).read_bytes()
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("generation spec must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("generation spec must be a JSON object")
    return GenerationSpec.from_mapping(value)


def _render_skill_markdown(spec: GenerationSpec) -> str:
    if spec.requires_logparse:
        description = (
            f"用于{spec.summary}；在 Problem Locator DIAGNOSE Job 中遵守 S00 AgentJobOutcome，"
            "缺参或缺日志时正常结束本 Job，日志仅经 logparse-diagnose broker 分析，候选结论等待独立复核。"
        )
    else:
        description = (
            f"用于{spec.summary}；在 Problem Locator DIAGNOSE Job 中遵守 S00 AgentJobOutcome，"
            "通过固定输入和 Evidence 形成候选结论并等待独立复核，不调用 logparse。"
        )
    roles = "\n".join(
        f"| {_cell(role.label)} | {_cell(role.description)} | {'是' if role.required else '否'} |"
        for role in spec.roles
    )
    custom_parameters = (
        "本 Skill 不设置额外自定义定位参数。"
        if not spec.custom_parameters
        else "\n".join(
            ["| 参数名 | 说明 | 是否必需 |", "| --- | --- | --- |"]
            + [
                f"| {_cell(item.name)} | {_cell(item.description)} | {'是' if item.required else '否'} |"
                for item in spec.custom_parameters
            ]
        )
    )
    content_type_contract = (
        "允许日志 Content-Type（逐字匹配 S00 Canonical ContentType，不做大小写或参数归一化）：\n\n"
        + "\n".join(f"- `{item}`" for item in spec.allowed_content_types)
        if spec.requires_logparse
        else "此产品 `requires_logparse=false`，不声明日志 ContentType，也不调用 logparse。"
    )
    time_characteristics = _markdown_list(spec.time_characteristics, fallback="Wiki 未声明额外时间特征。")
    analysis_steps = _markdown_numbered(spec.analysis_steps)
    judgement_rules = _markdown_list(spec.judgement_rules)
    output_requirements = _markdown_list(spec.output_requirements)
    assumptions = _markdown_list(spec.assumptions, fallback="无额外假设。")
    module_literal = _code(spec.module_name)
    product_literal = _code(spec.logparse_product or "null")
    if spec.requires_logparse:
        result_type_details = '''- `NEED_INPUT`：缺少参数组 A，或首次解析后的机器证据仍缺 `order_id`。
- `NEED_ATTACHMENT`：参数组 A 已满足，但尚无本 Job 固定的唯一日志 Attachment。
- `REROUTE`：问题不属于本 capability；不调用 Router，也不选择另一个 Skill。
- `COMPLETED`：当前完成条件均有 Evidence binding，可提出 Candidate；不声称 Case 已
  `RESOLVED`。'''
        input_and_tool_workflow = '''## 参数组 A 与一次日志

先复用 `CONTEXT_SNAPSHOT` 中已有且仍有效的事实和 OPEN requirement。缺少参数时返回
`NEED_INPUT`，只为缺失名称提出当前 S00 定义的 INPUT requirement；已经存在的
requirement 必须复用原 `requirement_id`，不得重复创建。

参数组 A 齐全但没有可用日志时返回 `NEED_ATTACHMENT`。日志 requirement 的 name 固定
为 `log_archive`，只接受一个 Attachment，允许 Content-Type 只能来自上面的固定列表。
上传本身不推进 Case；后续 Job 只能消费 `inputs/manifest.json` 中固定的 READY Attachment。

## 先调用 logparse-diagnose Skill

加载 `logparse-diagnose`，且只调用随服务安装的 `problem-locator-logparse` broker 客户端。
禁止读取 `LOGPARSE_REPO`、`LOGPARSE_CONFIG_PATH`、`LOGPARSE_PYTHON`，禁止直接启动
`cli.py`，禁止打开、枚举、解包或扫描原始归档，也禁止用 grep/rg 代替 logparse。

首次日志 Job 在 manifest 不含 `LOGPARSE_RUN` 时：

1. 用 Canonical JSON 写 `output/proposals/logparse-run/request.json`；request 只含 S07
   `parse-targets` 字段，禁止携带 `logparse_product` 或任意 argv。
2. 仅调用一次 `problem-locator-logparse parse-targets --request ... --result ...`。
3. 读取 broker 生成的 `target_logs.json` 与受控 `parse_manifest.json` 机器结果。
4. 提出 proposal key=`logparse-run` 的 `LOGPARSE_RUN` 目录 Artifact Draft，以及用同一
   artifact proposal key 作为 source binding 的 `LOGPARSE` Evidence Draft。
5. 若仍缺 `order_id`，在同一 `NEED_INPUT` Outcome 中提交中间 StateDelta、Evidence、
   LOGPARSE_RUN 与新 OPEN INPUT requirement；正常结束 Job。

## LOGPARSE_RUN 复用

只要 `inputs/manifest.json` 已含任一 `artifact_kind=LOGPARSE_RUN`，严禁调用
`parse-targets`。验证 manifest 固定的 Artifact kind、目录 hash、parse manifest 相对路径、
源 Attachment、`logparse_tool_ref` 与 product 后，使用其只读
`inputs/artifacts/<artifact_id>/tree` 根调用 `problem-locator-logparse target-logs`。
request 只含 S07 `target-logs` 字段且 `artifact_id` 必须来自 manifest。不得修改物化目录，
不得再次 parse；新 Job 的连续性只来自固定 StateDelta、Evidence、Attachment、
`LOGPARSE_RUN` 与 `PREVIOUS_OUTCOME`。'''
        evidence_source_workflow = '''只把 `target_logs` 返回并解析到受控 output root 内的安全相对 POSIX `log_path` 写入 S00
`LogparseEvidenceLocator.relative_path`。没有匹配、路径歧义、时间无法关联或证据不足时
必须明确保留缺口，不得把假设升级为事实。'''
    else:
        result_type_details = '''- `NEED_INPUT`：缺少 Wiki 所需结构化参数。
- `NEED_ATTACHMENT`：Wiki 明确要求且当前 Job 尚未固定所需非日志附件。
- `REROUTE`：问题不属于本 capability；不调用 Router，也不选择另一个 Skill。
- `COMPLETED`：当前完成条件均有 Evidence binding，可提出 Candidate；不声称 Case 已
  `RESOLVED`。'''
        input_and_tool_workflow = '''## 输入与工具边界

先复用 `CONTEXT_SNAPSHOT` 中已有且仍有效的事实和 OPEN requirement。缺少参数时返回
`NEED_INPUT`，只按当前 S00 requirement 合同提出缺失项；已经存在的 requirement 必须
复用原 `requirement_id`。若 Wiki 明确需要普通附件，可使用 `NEED_ATTACHMENT`，但只能
消费当前 Job manifest 固定的只读资源。

本产品 `requires_logparse=false`：不得加载 `logparse-diagnose`、调用
`problem-locator-logparse`、请求 `log_archive`、提出 `LOGPARSE_RUN`，或读取 raw logparse
环境。所有工具行为只限固定 Tool Bundle 和本 Job 输入。'''
        evidence_source_workflow = '''只引用当前 Job 固定 Evidence，或在同一 Outcome 中按 S00 提出新的 Evidence Draft。
没有可定位证据、证据歧义或时间无法关联时必须明确保留缺口，不得把假设升级为事实。'''
    return f'''---
name: {json.dumps(spec.skill_id, ensure_ascii=False)}
description: {json.dumps(description, ensure_ascii=False)}
---

# {spec.chinese_title}

本产品由 `wiki-to-diagnosis-skill` 生成器 `{GENERATOR_VERSION}` 生成。只消费当前
Problem Locator Job 的固定输入；S00 冻结 DTO、Schema、枚举和错误码是唯一机器合同。
禁止增加私有结果字段、私有错误码或直接修改 Case。Candidate 不是最终结果，必须等待
独立 REVIEW Job 的 `PASS`。

## 产品固定信息

- capability：`{spec.capability}`
- module：{module_literal}
- logparse product：{product_literal}
- generator version：`{GENERATOR_VERSION}`

{content_type_contract}

## 问题范围

{spec.problem_scope}

## 运行时输入

只读取 Runtime 提供的 `JOB_INSTRUCTION`、`CONTEXT_SNAPSHOT`、`OPEN_REQUIREMENTS`、
`PREVIOUS_OUTCOME`、`RESOURCE_MANIFEST` 与只读 `inputs/manifest.json`。不得扫描
`inputs/`、读取 Repository、沿用旧 Session 隐式状态或采用 Job 创建后的输入。

目标角色顺序固定为：

| 标签 | 说明 | 是否必需 |
| --- | --- | --- |
{roles}

每个 broker anchor 只含 `label`、`module`、`slot`、`process_name`、`pid`；其中
`module` 固定为 {module_literal}，`pid` 可以为 null，其余值必须来自本 Job 已验证事实。

## 自定义定位参数

{custom_parameters}

参数组 A 的 requirement name 固定为 `caller_service`、`server_service`、`rpc_method`、
`problem_time`。`problem_time` 必须是毫秒精度 UTC RFC 3339 单值，必须匹配
`{UTC_MILLIS_PATTERN}`；不得接受范围、猜测时区或取中点。参数 B 固定为 `order_id`。

## 四种业务结果

始终按 S00 `agent-job-outcome.schema.json` 生成完整 `AgentJobOutcome`，并在退出前原子
发布为 `output/job_outcome.json`。只使用以下 DIAGNOSE result type：

{result_type_details}

业务性缺参不是执行失败。`DiagnosisStateDelta`、requirement、Evidence/Artifact Draft、
Candidate 和 error 字段全部逐字使用当前 S00 合同；未使用的集合写空数组、无值写 null。
`add_user_facts` 与 `fulfill_requirements` 由应用服务拥有，Agent 必须写空数组。新事实只写
`proposed_facts`，并通过 `add_evidence_bindings` 提案引用 Evidence。

{input_and_tool_workflow}

## Evidence 与 Candidate

{evidence_source_workflow}

形成 Candidate 时，supporting Evidence bindings 和每个 completion criterion mapping
必须完整、按 ProblemSpec 顺序、全部 satisfied 且非空。Candidate 所在 Outcome 必须恰好
同时提出一个 USER_RESULT Draft：

- proposal key：`user-result`
- kind/name/content type/resource kind：`USER_RESULT` / `diagnosis-result.json` /
  `application/json` / `FILE`
- path：`output/proposals/user-result/payload`
- metadata：`{{"schema_version":1,"format_id":"problem-locator-diagnosis-v1","description":"Diagnosis result"}}`

payload 只用 S00 `UserResultPayload`：`problem_statement` 逐字等于 Job 固定 ProblemSpec，
`candidate_statement`、`supporting_evidence_bindings`、`completion_criteria_mapping` 逐字等于
同一 Candidate Draft。使用 S00 Canonical JSON（UTF-8、排序、紧凑、末尾一个 LF）；禁止
写入时间、正式 ID 猜测、Workspace 路径、endpoint、token 或 raw logparse 配置。

## 时间特征

{time_characteristics}

## Wiki 定位步骤

{analysis_steps}

## 判断规则

{judgement_rules}

## 输出要求

{output_requirements}

## 假设

{assumptions}

## 原子交付

先写同目录临时文件、flush 并同步，再原子替换 `output/job_outcome.json`；成功退出后 stdout
和 stderr 只给安全摘要，不能作为业务结果回退。任何 endpoint/token、绝对路径、环境值、
原始日志正文或敏感 Wiki 内容都不得进入 Outcome、proposal、USER_RESULT 或日志。
'''


def _wiki_sections(wiki: str) -> dict[str, str]:
    result: dict[str, list[str]] = {}
    current: str | None = None
    for line in wiki.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            result.setdefault(current, [])
        elif current is not None:
            result[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in result.items()}


def _wiki_basic_info(section: str) -> dict[str, str]:
    values: dict[str, str] = {}
    inside_fence = False
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            inside_fence = not inside_fence
            continue
        if not inside_fence or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in {"title", "skill_name", "module_name"} and value:
            values[key] = _unquote_simple(value)
    return values


def _wiki_roles(section: str) -> list[dict[str, Any]]:
    rows = _markdown_table(section, ["标签", "说明", "是否必需"])
    return [
        {"label": row[0], "description": row[1], "required": _required_bool(row[2])}
        for row in rows
    ]


def _wiki_custom_parameters(section: str) -> list[dict[str, Any]]:
    if not section:
        return []
    rows = _markdown_table(section, ["参数名", "说明", "是否必需"])
    return [
        {"name": row[0], "description": row[1], "required": _required_bool(row[2])}
        for row in rows
    ]


def _markdown_table(section: str, expected_header: list[str]) -> list[list[str]]:
    parsed = []
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            parsed.append([cell.strip() for cell in stripped.strip("|").split("|")])
    try:
        index = parsed.index(expected_header)
    except ValueError as exc:
        raise ValueError(f"wiki is missing table: {' | '.join(expected_header)}") from exc
    rows = []
    for row in parsed[index + 1 :]:
        if len(row) != len(expected_header):
            break
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in row):
            continue
        rows.append(row)
    if not rows:
        raise ValueError(f"wiki table has no data rows: {' | '.join(expected_header)}")
    return rows


def _section_prose(section: str) -> str:
    text = section.strip()
    if not text:
        raise ValueError("wiki 问题范围 must not be empty")
    return text


def _section_items(section: str) -> list[str]:
    items: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("```"):
            continue
        match = re.match(r"^(?:[-*]|[0-9]+[.)])\s+(.*)$", stripped)
        items.append(match.group(1).strip() if match else stripped)
    return items


def _read_product_tree(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names.sort()
        file_names.sort()
        directory_path = Path(directory)
        for name in list(directory_names):
            path = directory_path / name
            if path.is_symlink():
                raise ValueError(f"generated product contains a symlink: {path}")
        for name in file_names:
            path = directory_path / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or path.stat().st_nlink != 1:
                raise ValueError(f"generated product contains a non-plain file: {path}")
            relative = path.relative_to(root).as_posix()
            if not _safe_relative_posix(relative):
                raise ValueError(f"generated product contains an unsafe path: {relative}")
            files[relative] = path.read_bytes()
    return files


def _decode_manifest(payload: bytes | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_relative_posix(path: str) -> bool:
    return bool(path) and not path.startswith("/") and "\\" not in path and all(
        part not in {"", ".", ".."} for part in path.split("/")
    )


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        missing = sorted(expected - set(value)) if isinstance(value, Mapping) else sorted(expected)
        extra = sorted(set(value) - expected) if isinstance(value, Mapping) else []
        raise ValueError(f"{label} fields differ: missing={missing} extra={extra}")


def _single_line(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} must be a non-empty string within {maximum} UTF-8 bytes")
    if any(character in value for character in "\r\n\x00") or value.strip() != value:
        raise ValueError(f"{field} must be an exact non-blank single line")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 65536:
        raise ValueError(f"{field} must be non-empty UTF-8 text")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def _mapping_list(
    value: Any, field: str, *, minimum: int, maximum: int
) -> list[Mapping[str, Any]]:
    items = _sequence(value, field)
    if not minimum <= len(items) <= maximum or any(not isinstance(item, Mapping) for item in items):
        raise ValueError(f"{field} must contain {minimum}..{maximum} objects")
    return list(items)


def _text_tuple(value: Any, field: str, *, minimum: int = 0) -> tuple[str, ...]:
    items = _sequence(value, field)
    if len(items) < minimum:
        raise ValueError(f"{field} must contain at least {minimum} entries")
    result = tuple(_text(item, f"{field}[]") for item in items)
    _unique(list(result), field)
    return result


def _unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique and order-preserving")


def _required_bool(value: str) -> bool:
    if value == "是":
        return True
    if value == "否":
        return False
    raise ValueError("wiki required column must be 是 or 否")


def _unquote_simple(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        decoded = json.loads(value)
        if isinstance(decoded, str):
            return decoded
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    return value


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _code(value: str) -> str:
    return f"`{value.replace('`', '')}`"


def _markdown_list(values: Sequence[str], *, fallback: str | None = None) -> str:
    if not values:
        if fallback is None:
            raise ValueError("required Markdown list is empty")
        return f"- {fallback}"
    return "\n".join(f"- {value}" for value in values)


def _markdown_numbered(values: Sequence[str]) -> str:
    return "\n".join(f"{index}. {value}" for index, value in enumerate(values, start=1))


def _parse_cli_spec(args: argparse.Namespace) -> GenerationSpec:
    if args.spec is not None:
        return load_generation_spec(args.spec)
    if args.wiki is None:
        raise ValueError("provide exactly one of --spec or --wiki")
    required = {
        "--capability": args.capability,
        "--summary": args.summary,
        "--logparse-product": args.logparse_product if not args.no_logparse else "not-required",
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ValueError(f"wiki generation is missing options: {', '.join(missing)}")
    return build_spec_from_wiki(
        args.wiki.read_text(encoding="utf-8"),
        capability=args.capability,
        summary=args.summary,
        version=args.version,
        requires_logparse=not args.no_logparse,
        logparse_product=None if args.no_logparse else args.logparse_product,
        allowed_content_types=[] if args.no_logparse else args.allowed_content_type,
        assumptions=args.assumption,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate one deterministic Problem Locator diagnosis Skill product."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec", type=Path, help="UTF-8 JSON GenerationSpec")
    source.add_argument("--wiki", type=Path, help="Markdown wiki using references/wiki-template.md")
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--capability")
    parser.add_argument("--summary")
    parser.add_argument("--version", default="2.0.0")
    parser.add_argument("--logparse-product")
    parser.add_argument("--allowed-content-type", action="append", default=[])
    parser.add_argument("--assumption", action="append", default=[])
    parser.add_argument("--no-logparse", action="store_true")
    parser.add_argument("--replace-different-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        spec = _parse_cli_spec(args)
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
