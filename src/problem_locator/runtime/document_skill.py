"""Version 3 registrations for document-only specialized diagnosis Skills.

The package contains authored instructions, while this registration owns inputs,
material preparation and immutable product bindings. It is not a Methods package.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from problem_locator.contracts import (
    GENERIC_DIRECT_LOG_CONTENT_TYPES,
    GENERIC_LEGACY_LOG_CONTENT_TYPES,
    SPECIALIZED_DIRECT_AGENT_PROFILE_ID,
    SPECIALIZED_DIRECT_OUTPUT_CONTRACT_ID,
    canonical_json_sha256,
)

from .asset_snapshot import snapshot_asset
from .catalog_hash import hash_product_directory
from .methods_skill import (
    RoutingScopeV1,
    RuntimeRoleBindingV1,
    _binding,
    _frontmatter_name,
    _json_object,
    _ordinary_file,
    _real_directory,
    _routing_scope,
    _safe_reference,
)


_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?\Z"
)


@dataclass(frozen=True, slots=True)
class DocumentSource:
    kind: Literal["WIKI", "SKILL"]
    sha256: str


@dataclass(frozen=True, slots=True)
class DocumentUserInput:
    name: str
    prompt: str
    required: bool
    constraints: dict[str, Any]

    @property
    def requiredness(self) -> str:
        return "REQUIRED" if self.required else "OPTIONAL"


@dataclass(frozen=True, slots=True)
class DocumentFixedInput:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class DocumentDerivedField:
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class DocumentInputs:
    user: tuple[DocumentUserInput, ...]
    fixed: tuple[DocumentFixedInput, ...]
    derived: tuple[DocumentDerivedField, ...]


@dataclass(frozen=True, slots=True)
class DocumentAttachment:
    name: str
    prompt: str
    required: bool
    allowed_content_types: tuple[str, ...]
    min_count: int
    max_count: int

    @property
    def constraints(self) -> dict[str, Any]:
        return {
            "allowed_content_types": list(self.allowed_content_types),
            "min_count": self.min_count,
            "max_count": self.max_count,
        }


@dataclass(frozen=True, slots=True)
class DocumentPreprocessing:
    mode: Literal["none", "direct_files", "logparse"]
    logparse_product: str | None
    roles: tuple[dict[str, Any], ...]
    logparse_plan: dict[str, Any] | None

    @property
    def requires_logparse(self) -> bool:
        return self.mode == "logparse"


@dataclass(frozen=True, slots=True)
class DocumentRegistration:
    registration_id: str
    version: str
    capability: str
    deployment_scope: str
    summary: str
    package_relative_path: str
    skill_name: str
    source: DocumentSource
    inputs: DocumentInputs
    attachment: DocumentAttachment | None
    preprocessing: DocumentPreprocessing
    routing: RoutingScopeV1

    @property
    def input_requirements(self) -> tuple[DocumentUserInput, ...]:
        return self.inputs.user

    @property
    def diagnose(self) -> RuntimeRoleBindingV1:
        return RuntimeRoleBindingV1(
            agent_profile_id=SPECIALIZED_DIRECT_AGENT_PROFILE_ID,
            tool_bundle_id="tool-bundle/diagnose",
            context_policy_id="context-policy/diagnose",
            output_contract_id=SPECIALIZED_DIRECT_OUTPUT_CONTRACT_ID,
        )


@dataclass(frozen=True, slots=True)
class ResolvedDocumentSkill:
    registration_root: Path
    package_root: Path
    registration: DocumentRegistration
    document_paths: tuple[str, ...]
    registration_sha256: str
    package_tree_sha256: str
    combined_sha256: str

    @property
    def registration_id(self) -> str:
        return self.registration.registration_id


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} 的字段不符合文档型 Skill 注册格式。")
    return value


def _text(value: Any, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} 必须是非空文本，且不能超过 {maximum} 个 UTF-8 字节。")
    return value


def _name(value: Any, label: str, *, skill: bool = False) -> str:
    pattern = _SKILL_NAME if skill else _NAME
    if not isinstance(value, str) or len(value) > 64 or pattern.fullmatch(value) is None:
        style = "kebab-case" if skill else "snake-case"
        raise ValueError(f"{label} 必须使用小写 {style}，且不能超过 64 个字符。")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} 必须是布尔值。")
    return value


def _array(value: Any, label: str, maximum: int = 200) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{label} 必须是数组，且不能超过 {maximum} 项。")
    return value


def _constraints(value: Any) -> dict[str, Any]:
    raw = _object(value, {"value_type", "min_utf8_bytes", "max_utf8_bytes", "pattern", "allowed_values"}, "输入约束")
    minimum, maximum = raw["min_utf8_bytes"], raw["max_utf8_bytes"]
    if (raw["value_type"] != "STRING" or type(minimum) is not int or type(maximum) is not int
            or not 1 <= minimum <= maximum <= 65_536):
        raise ValueError("输入仅支持 STRING，字节范围必须满足 1 <= min <= max <= 65536。")
    pattern = raw["pattern"]
    if pattern is not None:
        if not isinstance(pattern, str) or len(pattern.encode("utf-8")) > 4096:
            raise ValueError("输入 pattern 必须为 null 或不超过 4096 字节的正则表达式。")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError("输入 pattern 不是有效的正则表达式。") from exc
    allowed = _array(raw["allowed_values"], "allowed_values")
    for item in allowed:
        _text(item, "allowed_values 值", 65_536)
        if not minimum <= len(item.encode("utf-8")) <= maximum or (
            pattern is not None and re.fullmatch(pattern, item) is None
        ):
            raise ValueError("allowed_values 的值必须符合该输入的长度和 pattern 约束。")
    if len(allowed) != len(set(allowed)):
        raise ValueError("allowed_values 不得重复。")
    return deepcopy(raw)


def _inputs(value: Any) -> DocumentInputs:
    raw = _object(value, {"user", "fixed", "derived"}, "inputs")
    user, fixed, derived = [], [], []
    for item in _array(raw["user"], "inputs.user"):
        item = _object(item, {"name", "prompt", "required", "constraints"}, "用户输入")
        user.append(DocumentUserInput(_name(item["name"], "用户输入名称"),
            _text(item["prompt"], "用户输入提示"), _boolean(item["required"], "required"),
            _constraints(item["constraints"])))
    for item in _array(raw["fixed"], "inputs.fixed"):
        item = _object(item, {"name", "value"}, "固定输入")
        fixed.append(DocumentFixedInput(_name(item["name"], "固定输入名称"),
            _text(item["value"], "固定输入值", 65_536)))
    for item in _array(raw["derived"], "inputs.derived"):
        item = _object(item, {"name", "description"}, "派生字段")
        derived.append(DocumentDerivedField(_name(item["name"], "派生字段名称"),
            _text(item["description"], "派生字段说明")))
    names = [item.name for item in (*user, *fixed, *derived)]
    if len(names) > 200 or len(names) != len(set(names)):
        raise ValueError("用户输入、固定输入和派生字段的名称不得重复，总数不能超过 200。")
    return DocumentInputs(tuple(user), tuple(fixed), tuple(derived))


def _attachment(value: Any) -> DocumentAttachment | None:
    if value is None:
        return None
    raw = _object(value, {"name", "prompt", "required", "allowed_content_types", "min_count", "max_count"}, "attachment")
    types = _array(raw["allowed_content_types"], "allowed_content_types", 7)
    if (not types or any(not isinstance(item, str) or item not in GENERIC_DIRECT_LOG_CONTENT_TYPES for item in types)
            or len(types) != len(set(types))):
        raise ValueError("附件类型必须是受支持且不重复的文本、JSON 或日志压缩包类型。")
    minimum, maximum = raw["min_count"], raw["max_count"]
    if type(minimum) is not int or type(maximum) is not int or not 1 <= minimum <= maximum <= 20:
        raise ValueError("附件数量必须满足 1 <= min_count <= max_count <= 20。")
    return DocumentAttachment(_name(raw["name"], "附件集合名称"), _text(raw["prompt"], "附件提示"),
        _boolean(raw["required"], "attachment.required"), tuple(types), minimum, maximum)


def _preprocessing(value: Any, inputs: DocumentInputs, attachment: DocumentAttachment | None) -> DocumentPreprocessing:
    raw = _object(value, {"mode", "logparse_product", "roles", "logparse_plan"}, "runtime.preprocessing")
    mode = raw["mode"]
    if mode not in ("none", "direct_files", "logparse"):
        raise ValueError("预处理模式必须是 none、direct_files 或 logparse。")
    if (mode == "none") != (attachment is None):
        raise ValueError("none 模式不能声明附件；direct_files 和 logparse 必须声明一个附件集合。")
    if mode != "logparse":
        if raw["logparse_product"] is not None or raw["roles"] != [] or raw["logparse_plan"] is not None:
            raise ValueError("不使用 logparse 时，product、roles 和 plan 必须清空。")
        return DocumentPreprocessing(mode, None, (), None)
    assert attachment is not None
    if (attachment.name != "log_archive" or not attachment.required
            or not set(attachment.allowed_content_types) <= GENERIC_LEGACY_LOG_CONTENT_TYPES):
        raise ValueError("logparse 必须声明必填的 log_archive，且只接受日志压缩包。")
    product = _text(raw["logparse_product"], "logparse_product", 256)
    roles_raw = _array(raw["roles"], "roles", 20)
    if not roles_raw:
        raise ValueError("logparse 至少需要一个角色。")
    roles, labels = [], set()
    for item in roles_raw:
        item = _object(item, {"label", "description", "presence", "source_reference"}, "logparse 角色")
        label = _name(item["label"], "角色 label")
        if label in labels or item["presence"] not in ("REQUIRED", "OPTIONAL"):
            raise ValueError("logparse 角色不得重名，presence 必须是 REQUIRED 或 OPTIONAL。")
        labels.add(label)
        _text(item["description"], "角色说明")
        _text(item["source_reference"], "角色来源")
        roles.append(deepcopy(item))
    if not any(item["presence"] == "REQUIRED" for item in roles):
        raise ValueError("logparse 至少需要一个 REQUIRED 角色。")
    plan = _object(raw["logparse_plan"], {"attachment_requirement", "problem_time_binding", "anchors"}, "logparse_plan")
    if plan["attachment_requirement"] != attachment.name:
        raise ValueError("logparse_plan 必须引用 log_archive 附件集合。")
    user = {item.name: item for item in inputs.user}

    def binding(value: Any, label: str, *, mandatory: bool, nullable: bool = False) -> None:
        bound = _binding(value, input_names=set(user), label=label, nullable=nullable)
        if bound is not None and bound["source"] == "USER_FACT" and mandatory and not user[bound["name"]].required:
            raise ValueError(f"{label} 引用的用户输入必须声明 required=true。")

    binding(plan["problem_time_binding"], "problem_time_binding", mandatory=True)
    anchors = _array(plan["anchors"], "anchors", 20)
    if len(anchors) != len(roles):
        raise ValueError("logparse anchors 必须与 roles 一一对应。")
    for index, (role, anchor) in enumerate(zip(roles, anchors, strict=True)):
        anchor = _object(anchor, {"label", "module", "slot", "process_name", "pid"}, f"anchors[{index}]")
        if anchor["label"] != role["label"]:
            raise ValueError("logparse anchors 的顺序和 label 必须与 roles 一致。")
        for field in ("module", "slot", "process_name"):
            binding(anchor[field], f"anchors[{index}].{field}", mandatory=role["presence"] == "REQUIRED")
        binding(anchor["pid"], f"anchors[{index}].pid", mandatory=False, nullable=True)
    return DocumentPreprocessing("logparse", product, tuple(roles), deepcopy(plan))


def document_package_paths(package_root: Path, *, expected_name: str | None = None) -> tuple[str, ...]:
    """Validate a closed Markdown package and return all files in stable order."""
    root = Path(package_root)
    _real_directory(root, label="文档型 Skill 包")
    if {item.name for item in root.iterdir()} != {"SKILL.md", "references"}:
        raise ValueError("文档型 Skill 包只能包含 SKILL.md 和 references，不接受脚本、工具或 methods.json。")
    references = root / "references"
    _real_directory(references, label="references")
    paths = ["SKILL.md"]
    nodes = sorted(references.iterdir(), key=lambda item: item.name)
    if len(nodes) > 200:
        raise ValueError("references 不能超过 200 个 Markdown 文件。")
    for node in nodes:
        relative = _safe_reference("references/" + node.name, label="文档引用")
        _ordinary_file(node, label=relative)
        paths.append(relative)
    total = 0
    for relative in paths:
        raw = _ordinary_file(root / relative, label=relative)
        total += len(raw)
        if len(raw) > 1_048_576 or total > 8_388_608:
            raise ValueError("文档文件不能超过 1 MiB，文档包总大小不能超过 8 MiB。")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{relative} 必须使用 UTF-8。") from exc
        if not text.strip() or "\x00" in text:
            raise ValueError(f"{relative} 必须是非空文本，且不能含有 NUL 字符。")
        if relative == "SKILL.md":
            name = _name(_frontmatter_name(text), "SKILL.md name", skill=True)
            if name != root.name or (expected_name is not None and name != expected_name):
                raise ValueError("Skill 名称必须与包目录和注册声明一致。")
    # Validate file identities as well as the textual allowlist.
    hash_product_directory(root)
    _validate_document_references(root, tuple(paths))
    return tuple(paths)


def _validate_document_references(root: Path, paths: tuple[str, ...]) -> None:
    """Reject missing or escaping local Markdown links before registration."""
    root = root.resolve()
    known = {root.joinpath(*PurePosixPath(path).parts).resolve() for path in paths}
    for relative in paths:
        source = root.joinpath(*PurePosixPath(relative).parts)
        text = source.read_text(encoding="utf-8")
        # Fenced examples are data, not resource dependencies.
        text = re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1\s*$", "", text)
        targets = [match.group(1) for match in re.finditer(r"!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)]+)", text)]
        targets.extend(match.group(1) for match in re.finditer(r"(?m)^\s*\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)", text))
        # Authors often name a reference in backticks rather than using a link.
        named_references = re.findall(r"(?<![A-Za-z0-9_./-])(references/[A-Za-z0-9_./-]+\.md)\b", text)
        for target, base in [*((target, source.parent) for target in targets),
                             *((target, root) for target in named_references)]:
            target = target.removeprefix("<").removesuffix(">")
            try:
                parsed = urlsplit(target)
            except ValueError as exc:
                raise ValueError(f"{relative} 包含无效的文档引用。") from exc
            if parsed.scheme in {"https", "http", "mailto"}:
                continue
            if parsed.scheme or parsed.netloc or "\\" in target:
                raise ValueError(f"{relative} 的本地引用不能使用外部路径或执行协议。")
            path = unquote(parsed.path)
            if not path:
                continue
            if PurePosixPath(path).is_absolute() or "\\" in path:
                raise ValueError(f"{relative} 的本地引用不能使用绝对路径。")
            destination = (base / path).resolve()
            if not destination.is_relative_to(root) or destination not in known:
                raise ValueError(f"{relative} 的本地引用不存在或超出文档包：{target}")


def document_source_paths(source_root: Path) -> tuple[str, ...]:
    """Accept existing document Skills before conversion to the target layout."""
    root = Path(source_root)
    _real_directory(root, label="原始 Skill 目录")
    names = {item.name for item in root.iterdir()}
    if "SKILL.md" not in names or names - {"SKILL.md", "references"}:
        raise ValueError("原始 Skill 只能包含 SKILL.md 和可选的 references 文档目录，不接受自带脚本或工具。")
    paths = ["SKILL.md"]
    if "references" in names:
        references = root / "references"
        _real_directory(references, label="原始 references")

        def visit(directory: Path) -> None:
            for node in sorted(directory.iterdir(), key=lambda item: item.name):
                if node.is_symlink():
                    raise ValueError("原始 Skill 不接受符号链接。")
                if node.is_dir():
                    _real_directory(node, label="原始文档目录")
                    visit(node)
                else:
                    if node.suffix != ".md":
                        raise ValueError("原始 references 只能包含 Markdown 文档。")
                    _ordinary_file(node, label="原始文档")
                    paths.append(node.relative_to(root).as_posix())

        visit(references)
    if len(paths) > 201:
        raise ValueError("原始 Skill 的引用文档不能超过 200 个。")
    total = 0
    for relative in paths:
        raw = _ordinary_file(root / relative, label=relative)
        total += len(raw)
        if len(raw) > 1_048_576 or total > 8_388_608:
            raise ValueError("原始文档文件不能超过 1 MiB，文档包总大小不能超过 8 MiB。")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("原始 Skill 必须使用 UTF-8。") from exc
        if not content.strip() or "\x00" in content:
            raise ValueError("原始 Skill 必须是非空文本，且不能含有 NUL 字符。")
        if relative == "SKILL.md" and re.search(r"(?m)^\s*allowed-tools\s*:", content):
            raise ValueError("文档型 Skill 不支持声明自带工具权限。")
    hash_product_directory(root)
    _validate_document_references(root, tuple(paths))
    return tuple(paths)


def document_source_sha256(source: Path, kind: Literal["WIKI", "SKILL"]) -> str:
    """Hash the author-provided bytes, independently from the generated package."""
    if kind == "WIKI":
        return hashlib.sha256(_ordinary_file(Path(source), label="Wiki 来源")).hexdigest()
    if kind == "SKILL":
        document_source_paths(Path(source))
        return hash_product_directory(Path(source))
    raise ValueError("来源类型必须是 WIKI 或 SKILL。")


def load_document_skill_registration(registration_root: Path) -> ResolvedDocumentSkill:
    root = Path(registration_root)
    frozen = snapshot_asset(root)
    if frozen is not None and frozen[1] is not None:
        if not isinstance(frozen[1], ResolvedDocumentSkill):
            raise ValueError("固定的注册不是文档型 Skill。")
        return frozen[1]
    _real_directory(root, label="文档型 Skill 注册目录")
    if {item.name for item in root.iterdir()} != {"registration-template.json", "package"}:
        raise ValueError("注册目录只能包含 registration-template.json 和 package。")
    raw, registration_bytes = _json_object(root / "registration-template.json", label="registration-template.json")
    _object(raw, {"schema_version", "registration_id", "version", "capability", "deployment_scope", "summary", "routing", "source", "package", "inputs", "attachment", "runtime"}, "registration v3")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 3:
        raise ValueError("文档型 Skill 的 registration schema_version 必须是整数 3。")
    registration_id = _name(raw["registration_id"], "registration_id", skill=True)
    if root.name != registration_id:
        raise ValueError("registration_id 必须与注册目录名称一致。")
    version = raw["version"]
    if not isinstance(version, str) or len(version) > 128 or _SEMVER.fullmatch(version) is None:
        raise ValueError("version 必须是三段式 SemVer。")
    capability = _text(raw["capability"], "capability", 256)
    if "\n" in capability or "\r" in capability:
        raise ValueError("capability 必须是单行文本。")
    summary = _text(raw["summary"], "summary")
    scope = raw["deployment_scope"]
    if scope not in ("PRODUCTION", "TEST_ONLY"):
        raise ValueError("deployment_scope 必须是 PRODUCTION 或 TEST_ONLY。")
    source = _object(raw["source"], {"kind", "sha256"}, "source")
    if (source["kind"] not in ("WIKI", "SKILL") or not isinstance(source["sha256"], str)
            or _SHA256.fullmatch(source["sha256"]) is None):
        raise ValueError("source 必须声明 WIKI 或 SKILL，以及原始来源的 SHA-256。")
    package = _object(raw["package"], {"format", "relative_path", "skill_name"}, "package")
    skill_name = _name(package["skill_name"], "package.skill_name", skill=True)
    if (package["format"] != "skill-document-v1"
            or package["relative_path"] != f"package/{skill_name}"):
        raise ValueError("package 必须使用 skill-document-v1，且路径必须与 Skill 名称一致。")
    parent = root / "package"
    _real_directory(parent, label="package")
    if {item.name for item in parent.iterdir()} != {skill_name}:
        raise ValueError("package 只能包含已声明的一个 Skill 目录。")
    package_root = parent / skill_name
    paths = document_package_paths(package_root, expected_name=skill_name)
    inputs = _inputs(raw["inputs"])
    attachment = _attachment(raw["attachment"])
    if attachment is not None and attachment.name in {item.name for item in (*inputs.user, *inputs.fixed, *inputs.derived)}:
        raise ValueError("附件集合名称不得与输入或派生字段名称重复。")
    runtime = _object(raw["runtime"], {"preprocessing"}, "runtime")
    preprocessing = _preprocessing(runtime["preprocessing"], inputs, attachment)
    registration = DocumentRegistration(registration_id, version, capability, scope, summary,
        package["relative_path"], skill_name, DocumentSource(**source), inputs, attachment,
        preprocessing, _routing_scope(raw["routing"]))
    registration_sha256 = hashlib.sha256(registration_bytes).hexdigest()
    package_tree_sha256 = hash_product_directory(package_root)
    combined_sha256 = canonical_json_sha256({"schema_version": 1, "registration_id": registration_id,
        "registration_sha256": registration_sha256, "package_tree_sha256": package_tree_sha256})
    return ResolvedDocumentSkill(root.resolve(), package_root.resolve(), registration, paths,
        registration_sha256, package_tree_sha256, combined_sha256)


__all__ = ["DocumentSource", "DocumentUserInput", "DocumentFixedInput", "DocumentDerivedField",
    "DocumentInputs", "DocumentAttachment", "DocumentPreprocessing", "DocumentRegistration",
    "ResolvedDocumentSkill", "document_package_paths", "document_source_paths", "document_source_sha256", "load_document_skill_registration"]
