"""Standalone registration v3 validation; requires only the Python standard library."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit


NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
FIELD = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?\Z")
ARCHIVE_TYPES = {"application/gzip", "application/zip", "application/x-tar"}
DIRECT_TYPES = ARCHIVE_TYPES | {"text/plain", "application/json", "application/x-ndjson", "application/jsonl"}
ROOT_KEYS = {"schema_version", "registration_id", "version", "capability", "deployment_scope", "summary", "routing", "source", "package", "inputs", "attachment", "runtime"}
CONSTRAINT_KEYS = {"value_type", "min_utf8_bytes", "max_utf8_bytes", "pattern", "allowed_values"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == keys, f"{label} 的字段不符合 v3 合同。")
    return value


def _text(value: Any, label: str, maximum: int = 65536) -> str:
    _require(isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= maximum, f"{label} 必须是非空文本，且不能超过 {maximum} 个 UTF-8 字节。")
    return value


def _name(value: Any, label: str, *, field: bool = False) -> str:
    pattern = FIELD if field else NAME
    _require(isinstance(value, str) and len(value) <= 64 and pattern.fullmatch(value) is not None, f"{label} 的名称格式不正确。")
    return value


def _directory(path: Path) -> None:
    metadata = path.lstat()
    _require(stat.S_ISDIR(metadata.st_mode) and not path.is_symlink() and not (getattr(metadata, "st_file_attributes", 0) & 0x400), f"目录必须是真实目录，不能使用链接：{path}")


def _file(path: Path) -> bytes:
    metadata = path.lstat()
    maximum_links = 2 if os.name == "nt" else 1
    _require(stat.S_ISREG(metadata.st_mode) and not path.is_symlink() and not (getattr(metadata, "st_file_attributes", 0) & 0x400) and metadata.st_nlink <= maximum_links, f"必须使用普通文件，不能使用链接：{path}")
    return path.read_bytes()


def _json(path: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            _require(key not in result, f"JSON 字段重复：{key}")
            result[key] = value
        return result

    def reject(value: str) -> None:
        raise ValueError(f"JSON 不允许非有限数值：{value}")

    result = json.loads(_file(path).decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject)
    _require(isinstance(result, dict), "registration-template.json 必须包含一个对象。")
    return result


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    _require(bool(lines) and lines[0] == "---", "SKILL.md 缺少 frontmatter。")
    _require("---" in lines[1:], "SKILL.md 的 frontmatter 未闭合。")
    values: dict[str, str] = {}
    for line in lines[1:lines.index("---", 1)]:
        if not line.strip():
            continue
        _require(":" in line, "SKILL.md 的 frontmatter 只能包含 name 和 description。")
        key, value = (part.strip() for part in line.split(":", 1))
        _require(key not in values, f"SKILL.md 的 frontmatter 字段重复：{key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    _object(values, {"name", "description"}, "SKILL.md frontmatter")
    _name(values["name"], "Skill")
    _require(bool(values["description"]), "Skill description 不能为空。")
    return values


def document_files(root: Path, *, expected_name: str | None = None) -> list[dict[str, Any]]:
    """Validate the closed document tree and return the V1 product hash entries."""
    _directory(root)
    _require({item.name for item in root.iterdir()} == {"SKILL.md", "references"}, "文档型 Skill 只能包含 SKILL.md 和 references/。")
    references = root / "references"
    _directory(references)
    paths = [root / "SKILL.md"]
    nodes = list(references.iterdir())
    _require(len(nodes) <= 200, "references/ 最多包含 200 个 Markdown 文件。")
    for item in nodes:
        relative = PurePosixPath("references/" + item.name)
        _require(item.suffix == ".md" and "\\" not in item.name and len(relative.parts) == 2, "references/ 只允许普通 Markdown 文件，不能包含子目录或脚本。")
        paths.append(item)
    seen: set[tuple[int, int]] = set()
    entries = []
    total = 0
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        data = _file(path)
        total += len(data)
        _require(len(data) <= 1_048_576 and total <= 8_388_608, "单个文档不能超过 1 MiB，文档包不能超过 8 MiB。")
        metadata = path.stat()
        identity = (metadata.st_dev, metadata.st_ino)
        _require(identity not in seen, "文档包不能重复引用同一文件或使用硬链接。")
        seen.add(identity)
        text = data.decode("utf-8")
        _require(bool(text.strip()) and "\x00" not in text, f"文档必须是非空 UTF-8 文本，且不能含有 NUL：{path}")
        if path.name == "SKILL.md" and path.parent == root:
            name = _frontmatter(text)["name"]
            _require(name == root.name and (expected_name is None or name == expected_name), "Skill 名称必须与包目录及注册声明一致。")
        entries.append({"path": path.relative_to(root).as_posix(), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    _references(root, [entry["path"] for entry in entries])
    return entries


def _references(root: Path, paths: list[str]) -> None:
    root = root.resolve()
    known = {(root / path).resolve() for path in paths}
    for relative in paths:
        source = root / relative
        text = source.read_text(encoding="utf-8")
        text = re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1\s*$", "", text)
        targets = [match.group(1) for match in re.finditer(r"!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)]+)", text)]
        targets.extend(match.group(1) for match in re.finditer(r"(?m)^\s*\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)", text))
        named = re.findall(r"(?<![A-Za-z0-9_./-])(references/[A-Za-z0-9_./-]+\.md)\b", text)
        for target, base in [*((target, source.parent) for target in targets), *((target, root) for target in named)]:
            target = target.removeprefix("<").removesuffix(">")
            parsed = urlsplit(target)
            if parsed.scheme in {"https", "http", "mailto"}:
                continue
            _require(not parsed.scheme and not parsed.netloc and "\\" not in target, f"{relative} 的本地引用不能使用外部路径或执行协议。")
            path = unquote(parsed.path)
            if not path:
                continue
            _require(not PurePosixPath(path).is_absolute() and "\\" not in path, f"{relative} 的本地引用不能使用绝对路径。")
            destination = (base / path).resolve()
            _require(destination.is_relative_to(root) and destination in known, f"{relative} 的本地引用不存在或超出文档包：{target}")


def _source_files(root: Path) -> list[dict[str, Any]]:
    _directory(root)
    names = {item.name for item in root.iterdir()}
    _require("SKILL.md" in names and not names - {"SKILL.md", "references"}, "原始 Skill 只允许 SKILL.md 和可选的 references/，不能自带脚本或工具。")
    paths = [root / "SKILL.md"]

    def visit(directory: Path) -> None:
        _directory(directory)
        for node in sorted(directory.iterdir(), key=lambda item: item.name):
            _require(not node.is_symlink(), "原始 Skill 不能包含链接。")
            if node.is_dir():
                visit(node)
            else:
                _require(node.suffix == ".md", "原始 references/ 只允许 Markdown 文档。")
                paths.append(node)

    if "references" in names:
        visit(root / "references")
    _require(len(paths) <= 201, "原始 Skill 最多包含 200 个参考文档。")
    entries, total, seen = [], 0, set()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        data = _file(path)
        metadata = path.stat()
        identity = (metadata.st_dev, metadata.st_ino)
        _require(identity not in seen, "原始 Skill 不能重复引用同一文件或使用硬链接。")
        seen.add(identity)
        total += len(data)
        _require(len(data) <= 1_048_576 and total <= 8_388_608, "原始文档不能超过 1 MiB，文档包不能超过 8 MiB。")
        text = data.decode("utf-8")
        _require(bool(text.strip()) and "\x00" not in text, "原始 Skill 必须是非空文本，且不能含有 NUL。")
        relative = path.relative_to(root).as_posix()
        if relative == "SKILL.md":
            _require(re.search(r"(?m)^\s*allowed-tools\s*:", text) is None, "文档型 Skill 不支持声明自带工具权限。")
        entries.append({"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    _references(root, [entry["path"] for entry in entries])
    # Match the existing product-directory hash's ignored bookkeeping names.
    return [entry for entry in entries if not any(
        part in {".DS_Store", "__pycache__", ".pytest_cache", ".managed", ".codex-managed"}
        or part.endswith(".pyc") or part.startswith(".managed.")
        for part in PurePosixPath(entry["path"]).parts
    )]


def source_identity(source: Path) -> dict[str, str]:
    source = Path(source)
    if source.is_dir():
        entries = _source_files(source)
        canonical = json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
        return {"kind": "SKILL", "sha256": hashlib.sha256(canonical).hexdigest()}
    raw = _file(source)
    raw.decode("utf-8")
    return {"kind": "WIKI", "sha256": hashlib.sha256(raw).hexdigest()}


def _routing(raw: Any) -> None:
    value = _object(raw, {"applicability", "exclusions"}, "routing")
    names: set[str] = set()
    for kind, minimum in (("applicability", 1), ("exclusions", 0)):
        items = value[kind]
        _require(isinstance(items, list) and minimum <= len(items) <= 16, f"routing.{kind} 的数量不符合合同。")
        for item in items:
            item = _object(item, {"id", "description"}, f"routing.{kind}")
            name = _name(item["id"], "路由条件")
            _require(name not in names, "路由条件 ID 不能重复。")
            names.add(name)
            description = item["description"]
            _require(isinstance(description, str) and bool(description.strip()) and len(description) <= 1024, "路由说明必须是非空文本，且不能超过 1024 个字符。")


def _constraints(raw: Any) -> None:
    value = _object(raw, CONSTRAINT_KEYS, "用户参数 constraints")
    _require(value["value_type"] == "STRING", "用户参数仅支持 STRING。")
    low, high = value["min_utf8_bytes"], value["max_utf8_bytes"]
    _require(type(low) is int and type(high) is int and 1 <= low <= high <= 65536, "参数长度必须满足 1 <= min <= max <= 65536。")
    pattern = value["pattern"]
    _require(pattern is None or (isinstance(pattern, str) and len(pattern.encode("utf-8")) <= 4096), "pattern 必须是 null 或不超过 4096 字节的字符串。")
    if pattern is not None:
        re.compile(pattern)
    choices = value["allowed_values"]
    _require(isinstance(choices, list) and len(choices) <= 200 and all(isinstance(item, str) and bool(item.strip()) for item in choices), "allowed_values 最多包含 200 个非空字符串，也可以使用空列表。")
    _require(len(choices) == len(set(choices)), "allowed_values 不能重复。")
    for choice in choices:
        _require(low <= len(choice.encode("utf-8")) <= high and (pattern is None or re.fullmatch(pattern, choice) is not None), "allowed_values 中的值必须满足长度与 pattern 约束。")


def _inputs(raw: Any) -> tuple[dict[str, Any], set[str]]:
    value = _object(raw, {"user", "fixed", "derived"}, "inputs")
    _require(all(isinstance(value[key], list) for key in value), "inputs 的三个字段必须是数组。")
    _require(sum(len(items) for items in value.values()) <= 200, "输入声明最多 200 项。")
    names: set[str] = set()
    users = {}
    keys = {"user": {"name", "prompt", "required", "constraints"}, "fixed": {"name", "value"}, "derived": {"name", "description"}}
    for kind, items in value.items():
        for item in items:
            item = _object(item, keys[kind], f"inputs.{kind}")
            name = _name(item["name"], "输入", field=True)
            _require(name not in names, "用户参数、固定值和派生字段不能重名。")
            names.add(name)
            if kind == "user":
                _text(item["prompt"], "参数提示", 4096)
                _require(type(item["required"]) is bool, "用户参数 required 必须是布尔值。")
                _constraints(item["constraints"])
                users[name] = item
            elif kind == "fixed":
                _text(item["value"], "固定值")
            else:
                _text(item["description"], "派生字段说明", 4096)
    return users, names


def _binding(raw: Any, users: dict[str, Any], label: str, *, required: bool = False, nullable: bool = False) -> None:
    if nullable and raw is None:
        return
    _require(isinstance(raw, dict), f"{label} 必须是绑定对象。")
    source = raw.get("source")
    if source == "SKILL_FIXED":
        value = _object(raw, {"source", "value"}, label)
        _require(isinstance(value["value"], str) and bool(value["value"]), f"{label} 的固定绑定必须是非空字符串。")
        return
    value = _object(raw, {"source", "name"}, label)
    _require(source == "USER_FACT" and isinstance(value["name"], str) and value["name"] in users, f"{label} 必须引用已经声明的用户参数。")
    _require(not required or users[value["name"]]["required"], f"{label} 引用的用户参数必须是必填项。")


def _preprocessing(raw: Any, attachment: Any, users: dict[str, Any], names: set[str]) -> str:
    runtime = _object(raw, {"preprocessing"}, "runtime")
    value = _object(runtime["preprocessing"], {"mode", "logparse_product", "roles", "logparse_plan"}, "preprocessing")
    mode = value["mode"]
    _require(mode in ("none", "direct_files", "logparse"), "preprocessing.mode 必须是 none、direct_files 或 logparse。")
    if mode == "none":
        _require(attachment is None, "none 模式不能声明附件。")
    else:
        item = _object(attachment, {"name", "prompt", "required", "allowed_content_types", "min_count", "max_count"}, "attachment")
        name = _name(item["name"], "附件", field=True)
        _require(name not in names, "附件名称不能与输入名称重复。")
        _text(item["prompt"], "附件提示", 4096)
        _require(type(item["required"]) is bool, "附件 required 必须是布尔值。")
        low, high = item["min_count"], item["max_count"]
        _require(type(low) is int and type(high) is int and 1 <= low <= high <= 20, "附件数量必须满足 1 <= min_count <= max_count <= 20。")
        types = item["allowed_content_types"]
        supported = ARCHIVE_TYPES if mode == "logparse" else DIRECT_TYPES
        _require(isinstance(types, list) and bool(types) and all(isinstance(entry, str) and entry in supported for entry in types), "附件格式超出当前输入准备方式支持的范围。")
        _require(len(types) == len(set(types)), "附件格式不能重复。")
    if mode != "logparse":
        _require(value["logparse_product"] is None and value["roles"] == [] and value["logparse_plan"] is None, "非 Logparse 模式必须清空 product、roles 和 plan。")
        return mode
    _require(attachment["name"] == "log_archive" and attachment["required"] is True, "Logparse 必须声明必填 log_archive 附件集合。")
    _text(value["logparse_product"], "logparse_product", 256)
    roles = value["roles"]
    _require(isinstance(roles, list) and 1 <= len(roles) <= 20, "Logparse 必须声明 1 至 20 个角色。")
    labels = []
    for role in roles:
        role = _object(role, {"label", "description", "presence", "source_reference"}, "Logparse role")
        label = _name(role["label"], "角色", field=True)
        _require(label not in labels, "Logparse 角色名称不能重复。")
        labels.append(label)
        _require(role["presence"] in ("REQUIRED", "OPTIONAL"), "角色 presence 必须是 REQUIRED 或 OPTIONAL。")
        _text(role["description"], "角色说明", 4096)
        _text(role["source_reference"], "角色来源说明", 4096)
    _require(any(role["presence"] == "REQUIRED" for role in roles), "Logparse 至少需要一个 REQUIRED 角色。")
    plan = _object(value["logparse_plan"], {"attachment_requirement", "problem_time_binding", "anchors"}, "logparse_plan")
    _require(plan["attachment_requirement"] == "log_archive", "Logparse plan 必须绑定 log_archive。")
    _binding(plan["problem_time_binding"], users, "problem_time_binding", required=True)
    anchors = plan["anchors"]
    _require(isinstance(anchors, list) and len(anchors) == len(roles), "Logparse anchors 必须与角色逐项对应。")
    for role, anchor in zip(roles, anchors):
        anchor = _object(anchor, {"label", "module", "slot", "process_name", "pid"}, "Logparse anchor")
        _require(anchor["label"] == role["label"], "Logparse anchors 顺序必须与角色一致。")
        for field in ("module", "slot", "process_name"):
            _binding(anchor[field], users, f"{role['label']}.{field}", required=role["presence"] == "REQUIRED")
        _binding(anchor["pid"], users, f"{role['label']}.pid", nullable=True)
    return mode


def validate_document(registration_dir: Path, source: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "schema_version": 3, "errors": []}
    try:
        root = Path(registration_dir)
        _directory(root)
        _require({item.name for item in root.iterdir()} == {"registration-template.json", "package"}, "注册目录只能包含 registration-template.json 和 package/。")
        raw = _object(_json(root / "registration-template.json"), ROOT_KEYS, "registration-template.json")
        _require(type(raw["schema_version"]) is int and raw["schema_version"] == 3, "文档型注册的 schema_version 必须是整数 3。")
        name = _name(raw["registration_id"], "registration_id")
        _require(name == root.name, "registration_id 必须与注册目录名一致。")
        _require(isinstance(raw["version"], str) and len(raw["version"]) <= 128 and SEMVER.fullmatch(raw["version"]) is not None, "version 必须使用 SemVer，且不能超过 128 个字符。")
        capability = _text(raw["capability"], "capability", 256)
        _require(not any(character in capability for character in "\r\n"), "capability 必须是单行文本。")
        _text(raw["summary"], "summary", 4096)
        _require(raw["deployment_scope"] in ("PRODUCTION", "TEST_ONLY"), "deployment_scope 不受支持。")
        _routing(raw["routing"])
        identity = _object(raw["source"], {"kind", "sha256"}, "source")
        _require(identity["kind"] in ("WIKI", "SKILL") and isinstance(identity["sha256"], str) and SHA256.fullmatch(identity["sha256"]) is not None, "source 类型或 SHA-256 格式不正确。")
        _require(identity == source_identity(Path(source)), "source 与原始来源的类型或 SHA-256 不一致。")
        package = _object(raw["package"], {"format", "relative_path", "skill_name"}, "package")
        skill_name = _name(package["skill_name"], "skill_name")
        _require(package["format"] == "skill-document-v1", "v3 只支持 skill-document-v1 包。")
        _require(package["relative_path"] == f"package/{skill_name}", "package.relative_path 必须等于 package/<skill_name>。")
        parent = root / "package"
        _directory(parent)
        _require({item.name for item in parent.iterdir()} == {skill_name}, "package/ 必须只包含注册指定的 Skill 目录。")
        document_files(parent / skill_name, expected_name=skill_name)
        users, names = _inputs(raw["inputs"])
        mode = _preprocessing(raw["runtime"], raw["attachment"], users, names)
        result.update(ok=True, registration_id=name, skill_name=skill_name, source=identity, preparation_mode=mode)
    except (OSError, UnicodeError, ValueError, TypeError, re.error) as exc:
        result["errors"].append(str(exc))
    return result
