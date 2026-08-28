#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any


NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
FIELD_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
LOG_PLACEHOLDER_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}|%[A-Za-z]")
METHOD_HEADINGS = (
    "## 适用条件",
    "## 所需证据",
    "## 计算与判断",
    "## 确认条件",
    "## 未知边界",
    "## 输出含义",
)
REGISTRATION_ROOT_ENTRIES = {"registration-template.json", "package"}
PACKAGE_ROOT_ENTRIES = {"SKILL.md", "methods.json", "references"}
REGISTRATION_KEYS = {
    "schema_version",
    "registration_id",
    "version",
    "capability",
    "deployment_scope",
    "summary",
    "package",
    "runtime",
}
PACKAGE_BINDING_KEYS = {"relative_path", "skill_name", "source_wiki_sha256"}
RUNTIME_KEYS = {"diagnose", "review", "preprocessing"}
RUNTIME_BINDING_KEYS = {
    "agent_profile_id",
    "tool_bundle_id",
    "context_policy_id",
    "output_contract_id",
}
PREPROCESSING_KEYS = {
    "requires_logparse",
    "logparse_product",
    "roles",
    "logparse_plan",
}
ROLE_KEYS = {"label", "description", "presence", "source_reference"}
PLAN_KEYS = {"attachment_requirement", "problem_time_binding", "anchors"}
ANCHOR_KEYS = {"label", "module", "slot", "process_name", "pid"}
METHODS_ROOT_KEYS = {
    "schema_version",
    "skill_name",
    "source_wiki_sha256",
    "required_user_inputs",
    "required_artifacts",
    "log_derived_fields",
    "shared_references",
    "methods",
}
METHOD_KEYS = {"id", "title", "reference", "priority", "evidence_markers"}
SOURCE_IDENTITY_KEYS = {
    "algorithm",
    "log_template_extraction_version",
    "log_template_inventory_sha256",
    "log_templates",
    "schema_version",
    "sha256",
    "source_path",
}
REQUIRED_INPUT_PREFIX = [
    "problem_time",
    "client_slot",
    "client_process_name",
    "server_slot",
    "server_process_name",
    "client_pid",
    "server_pid",
]
FORBIDDEN_INPUT_ALIASES = {
    "api_name",
    "client_process",
    "server_process",
    "service_name",
    "slot",
}
DIAGNOSE_BINDING = {
    "agent_profile_id": "agent-profile/specialist",
    "tool_bundle_id": "tool-bundle/diagnose",
    "context_policy_id": "context-policy/diagnose",
    "output_contract_id": "output-contract/diagnose",
}
REVIEW_BINDING = {
    "agent_profile_id": "agent-profile/reviewer",
    "tool_bundle_id": "tool-bundle/review",
    "context_policy_id": "context-policy/review",
    "output_contract_id": "output-contract/review",
}
SOURCE_LOG_TEMPLATES_REFERENCE = "references/source-log-templates.md"
SOURCE_IDENTITY_SCHEMA_VERSION = 2
LOG_TEMPLATE_EXTRACTION_VERSION = 2
SERVER_BOUNDARY_SENTENCE = (
    "Logparse 预处理、目标日志冻结、Review 和最终 Artifact 发布由 Server 完成；"
    "诊断阶段不重新执行这些操作。"
)
OPTIONAL_PID_SENTENCE = (
    "`client_pid` 和 `server_pid` 是可选事实；缺失时不请求补充，也不构成证据缺口。"
)
REQUIRED_SKILL_PHRASES = (
    "method-evidence-graph.json",
    "method-evaluation-plan.json",
    "evaluation_ref",
    "verdict",
    "reason",
    "UNKNOWN",
)
OBSOLETE_SKILL_FIELDS = ("target_logs", "identity_tokens", "sources")
REQUIRED_SKILL_SEMANTICS = (
    (
        "consume only the server Evidence Graph and Evaluation Plan",
        re.compile(
            r"(?:只|仅)(?:消费|读取).*method-evidence-graph\.json.*method-evaluation-plan\.json"
            r"|(?:consume|read) only.*method-evidence-graph\.json.*method-evaluation-plan\.json",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "avoid rescanning evidence markers",
        re.compile(
            r"(?:不|不得|不要).*重新扫描.*(?:marker|标记)"
            r"|(?:do not|must not).*(?:rescan).*(?:marker|evidence)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "evaluate every plan reference in plan order",
        re.compile(
            r"(?:按|依照).*Evaluation Plan.*(?:顺序).*(?:全部|所有|每个).*evaluation_ref"
            r"|(?:in).*Evaluation Plan.*(?:order).*(?:all|every).*evaluation_ref",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "avoid stopping after the first confirmation",
        re.compile(
            r"(?:不能|不得|不要).*(?:第一|首个).*(?:确认).*(?:停止|短路)"
            r"|(?:do not|must not).*(?:first).*(?:confirmation).*(?:stop|short-circuit)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "return only evaluation_ref, verdict, and reason",
        re.compile(
            r"(?:只输出|仅输出).*evaluation_ref.*verdict.*reason"
            r"|(?:only).*(?:output|return).*evaluation_ref.*verdict.*reason",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)
ACTION_PATTERN = (
    r"(?:调用|加载|运行|执行|使用|启动|触发|"
    r"(?<![A-Za-z])(?:load|invoke|call|run|execute|use|start|trigger|invocation|execution)"
    r"(?![A-Za-z]))"
)
HELPER_TOKEN_PATTERN = r"(?<![A-Za-z0-9_])Helper(?![A-Za-z0-9_])"
BROKER_TOKEN_PATTERN = r"(?<![A-Za-z0-9_])broker(?![A-Za-z0-9_])"
PREPROCESS_PATTERN = r"(?:preprocess(?:ing)?|预处理)"
FORBIDDEN_PACKAGE_PATTERNS = (
    (
        "logparse-diagnose",
        re.compile(r"logparse-diagnose", re.IGNORECASE),
    ),
    (
        "Skill( tool call",
        re.compile(r"(?<![A-Za-z0-9_])Skill\s*\(", re.IGNORECASE),
    ),
    (
        "Helper invocation",
        re.compile(
            ACTION_PATTERN
            + r"[^\r\n]{0,24}"
            + HELPER_TOKEN_PATTERN
            + r"|"
            + HELPER_TOKEN_PATTERN
            + r"[^\r\n]{0,24}"
            + ACTION_PATTERN,
            re.IGNORECASE,
        ),
    ),
    (
        "broker preprocessing invocation",
        re.compile(
            ACTION_PATTERN
            + r"[^\r\n]{0,80}"
            + BROKER_TOKEN_PATTERN
            + r"[^\r\n]{0,80}"
            + PREPROCESS_PATTERN
            + r"|"
            + ACTION_PATTERN
            + r"[^\r\n]{0,80}"
            + PREPROCESS_PATTERN
            + r"[^\r\n]{0,80}"
            + BROKER_TOKEN_PATTERN,
            re.IGNORECASE,
        ),
    ),
    ("problem-locator-logparse", re.compile(r"problem-locator-logparse", re.IGNORECASE)),
    ("result.zip", re.compile(r"result\.zip", re.IGNORECASE)),
    ("pack_result_zip", re.compile(r"pack_result_zip", re.IGNORECASE)),
    ("logparse.json", re.compile(r"logparse\.json", re.IGNORECASE)),
    ("cli.py", re.compile(r"cli\.py", re.IGNORECASE)),
)


def _ordinary(path: Path, label: str, errors: list[str]) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        errors.append(f"missing {label}: {path}")
        return False
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        errors.append(f"{label} must be one ordinary file: {path}")
        return False
    return True


def _real_directory(path: Path, label: str, errors: list[str]) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        errors.append(f"missing {label}: {path}")
        return False
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        errors.append(f"{label} must be one real directory: {path}")
        return False
    return True


def _safe_reference(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        return None
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or len(path.parts) != 2
        or path.parts[0] != "references"
        or path.suffix != ".md"
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        return None
    return path.as_posix()


def _frontmatter(text: str, errors: list[str]) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        errors.append("SKILL.md must start with YAML frontmatter")
        return {}
    try:
        closing = lines.index("---", 1)
    except ValueError:
        errors.append("SKILL.md frontmatter is not closed")
        return {}
    values: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip():
            continue
        if ":" not in line:
            errors.append(f"unsupported SKILL.md frontmatter line: {line}")
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value and value[:1] == value[-1:] and value[:1] in {'"', "'"}:
            value = value[1:-1]
        if key in values:
            errors.append(f"duplicate SKILL.md frontmatter key: {key}")
        values[key] = value
    if set(values) != {"name", "description"}:
        errors.append("SKILL.md frontmatter must contain exactly name and description")
    if not values.get("description"):
        errors.append("SKILL.md description must not be empty")
    return values


def _read_json(path: Path, label: str, errors: list[str]) -> object | None:
    if not _ordinary(path, label, errors):
        return None

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate field {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite value {value}")

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"invalid JSON in {label}: {exc}")
        return None


def _field_ids(value: object, label: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{label} must be an array")
        return []
    if len(value) > 200:
        errors.append(f"{label} must contain at most 200 identifiers")
    if any(not isinstance(item, str) or not FIELD_PATTERN.fullmatch(item) for item in value):
        errors.append(f"{label} must contain lowercase snake_case identifiers")
        return []
    if len(value) != len(set(value)):
        errors.append(f"{label} must contain unique identifiers")
    return list(value)


def _valid_module(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value.encode("utf-8")) <= 128
        and value == value.strip()
        and value.isascii()
        and not any(character in value for character in "\r\n\x00")
    )


def _wiki_log_templates(text: str) -> list[str]:
    templates: list[str] = []
    in_fence = False
    collect_fence = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if in_fence:
            if stripped == "```":
                in_fence = False
                collect_fence = False
            elif collect_fence and stripped and LOG_PLACEHOLDER_PATTERN.search(stripped):
                templates.append(stripped)
            continue
        if stripped in {"```text", "```"}:
            in_fence = True
            collect_fence = True
        elif stripped.startswith("```"):
            in_fence = True
    return templates


def _render_source_log_templates(templates: list[str]) -> str:
    return "# Source log templates\n\n```text\n" + "\n".join(templates) + "\n```\n"


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _log_template_inventory_sha256(templates: list[str]) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(
            {"version": LOG_TEMPLATE_EXTRACTION_VERSION, "templates": templates}
        )
    ).hexdigest()


def build_source_wiki_identity(wiki_bytes: bytes, source_path: str) -> dict[str, object]:
    wiki_text = wiki_bytes.decode("utf-8")
    templates = _wiki_log_templates(wiki_text)
    return {
        "schema_version": SOURCE_IDENTITY_SCHEMA_VERSION,
        "algorithm": "sha256",
        "source_path": source_path,
        "sha256": hashlib.sha256(wiki_bytes).hexdigest(),
        "log_template_extraction_version": LOG_TEMPLATE_EXTRACTION_VERSION,
        "log_templates": templates,
        "log_template_inventory_sha256": _log_template_inventory_sha256(templates),
    }


def _wiki_named_log_fields(templates: list[str]) -> list[str]:
    fields: list[str] = []
    for template in templates:
        for match in LOG_PLACEHOLDER_PATTERN.finditer(template):
            field = match.group(1)
            if field is not None and field not in fields:
                fields.append(field)
    return fields


def _canonical_evidence_marker(template: str) -> str | None:
    matches = list(LOG_PLACEHOLDER_PATTERN.finditer(template))
    if not matches:
        return template.strip() or None
    prefix = template[: matches[0].start()].strip()
    if prefix:
        return prefix
    literal_segments = [
        template[left.end() : right.start()].strip()
        for left, right in zip(matches, matches[1:])
        if template[left.end() : right.start()].strip()
    ]
    if not literal_segments:
        return None
    return max(enumerate(literal_segments), key=lambda item: (len(item[1]), -item[0]))[1]


def _wiki_canonical_evidence_markers(templates: list[str]) -> list[str]:
    markers: list[str] = []
    for template in templates:
        marker = _canonical_evidence_marker(template)
        if marker is not None and marker not in markers:
            markers.append(marker)
    return markers


def _validate_source_identity(
    path: Path | None,
    *,
    wiki_bytes: bytes,
    wiki_templates: list[str],
    errors: list[str],
) -> None:
    if path is None:
        return
    value = _read_json(path, "source identity", errors)
    if not isinstance(value, dict):
        if value is not None:
            errors.append("source identity must contain one object")
        return
    if set(value) != SOURCE_IDENTITY_KEYS:
        errors.append("source identity keys do not match schema v2")
    if value.get("schema_version") != SOURCE_IDENTITY_SCHEMA_VERSION:
        errors.append("source identity schema_version must be 2")
    if value.get("algorithm") != "sha256":
        errors.append("source identity algorithm must be sha256")
    source_path = value.get("source_path")
    if not isinstance(source_path, str) or not source_path or "\x00" in source_path:
        errors.append("source identity source_path must be non-empty text")
    if value.get("sha256") != hashlib.sha256(wiki_bytes).hexdigest():
        errors.append("source identity sha256 does not match the supplied Wiki")
    if value.get("log_template_extraction_version") != LOG_TEMPLATE_EXTRACTION_VERSION:
        errors.append("source identity log_template_extraction_version must be 2")
    if value.get("log_templates") != wiki_templates:
        errors.append("source identity log_templates do not match extraction version 2")
    if value.get("log_template_inventory_sha256") != _log_template_inventory_sha256(
        wiki_templates
    ):
        errors.append("source identity log_template_inventory_sha256 does not match")


def _validate_business_skill(
    path: Path,
    *,
    expected_name: str | None,
    errors: list[str],
) -> dict[str, str]:
    if not _ordinary(path, "business SKILL.md", errors):
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        errors.append(f"business SKILL.md is not UTF-8: {exc}")
        return {}
    frontmatter = _frontmatter(text, errors)
    if expected_name is not None and frontmatter.get("name") != expected_name:
        errors.append("SKILL.md name must match the package directory")
    for phrase in REQUIRED_SKILL_PHRASES:
        if phrase not in text:
            errors.append(f"SKILL.md must mention {phrase}")
    for obsolete_field in OBSOLETE_SKILL_FIELDS:
        if re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(obsolete_field)}(?![A-Za-z0-9_])",
            text,
        ):
            errors.append(
                f"SKILL.md must not use the V1 runtime field {obsolete_field}"
            )
    for label, pattern in REQUIRED_SKILL_SEMANTICS:
        if pattern.search(text) is None:
            errors.append(f"SKILL.md must require {label}")
    if SERVER_BOUNDARY_SENTENCE not in text:
        errors.append("SKILL.md must contain the fixed Server-owned preprocessing boundary")
    if OPTIONAL_PID_SENTENCE not in text:
        errors.append("SKILL.md must contain the fixed optional PID boundary")
    return frontmatter


def _validate_package_tokens(package_root: Path, errors: list[str]) -> None:
    for path in sorted(package_root.rglob("*")):
        if path.suffix.lower() not in {".md", ".json"}:
            continue
        try:
            metadata = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative = path.relative_to(package_root).as_posix()
        for label, pattern in FORBIDDEN_PACKAGE_PATTERNS:
            if pattern.search(text):
                errors.append(
                    f"business package text must not contain Server-owned token {label}: {relative}"
                )


def _validate_methods(
    package_root: Path,
    *,
    wiki_sha256: str,
    wiki_text: str,
    wiki_templates: list[str],
    expected_skill_name: str | None,
    frontmatter: dict[str, str],
    errors: list[str],
) -> dict[str, object]:
    manifest = _read_json(package_root / "methods.json", "methods.json", errors)
    result: dict[str, object] = {
        "skill_name": None,
        "source_wiki_sha256": None,
        "required_user_inputs": [],
        "method_count": 0,
        "marker_count": 0,
    }
    if not isinstance(manifest, dict):
        if manifest is not None:
            errors.append("methods.json must contain one object")
        return result
    if set(manifest) != METHODS_ROOT_KEYS:
        errors.append("methods.json root keys do not match the Methods package contract")
    if type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1:
        errors.append("methods.json schema_version must be 1")

    skill_name = manifest.get("skill_name")
    result["skill_name"] = skill_name
    if not isinstance(skill_name, str) or not NAME_PATTERN.fullmatch(skill_name):
        errors.append("methods.json skill_name is invalid")
    else:
        if not skill_name.startswith("diagnose-"):
            errors.append("methods.json skill_name must start with diagnose-")
        if expected_skill_name is not None and skill_name != expected_skill_name:
            errors.append("methods.json skill_name must match the package directory")
        if frontmatter.get("name") != skill_name:
            errors.append("SKILL.md name must match methods.json skill_name")

    source_sha = manifest.get("source_wiki_sha256")
    result["source_wiki_sha256"] = source_sha
    if not isinstance(source_sha, str) or not SHA256_PATTERN.fullmatch(source_sha):
        errors.append("methods.json source_wiki_sha256 must be lowercase SHA-256")
    elif source_sha != wiki_sha256:
        errors.append("methods.json source_wiki_sha256 does not match the supplied Wiki")

    required_user_inputs = _field_ids(
        manifest.get("required_user_inputs"), "required_user_inputs", errors
    )
    result["required_user_inputs"] = required_user_inputs
    if required_user_inputs[: len(REQUIRED_INPUT_PREFIX)] != REQUIRED_INPUT_PREFIX:
        errors.append(
            "required_user_inputs must start with the five mandatory anchor facts, then client_pid and server_pid"
        )
    aliases = sorted(FORBIDDEN_INPUT_ALIASES.intersection(required_user_inputs))
    if aliases:
        errors.append("required_user_inputs contains forbidden aliases: " + ", ".join(aliases))

    required_artifacts = _field_ids(
        manifest.get("required_artifacts"), "required_artifacts", errors
    )
    if required_artifacts != ["log_archive"]:
        errors.append("required_artifacts must equal exactly [log_archive]")
    log_derived_fields = _field_ids(
        manifest.get("log_derived_fields"), "log_derived_fields", errors
    )
    expected_log_derived_fields = [
        field
        for field in _wiki_named_log_fields(wiki_templates)
        if field not in required_user_inputs
    ]
    if log_derived_fields != expected_log_derived_fields:
        errors.append(
            "log_derived_fields must be the named Wiki log fields in first-appearance order, excluding required_user_inputs"
        )
    declared_fields = required_user_inputs + required_artifacts + log_derived_fields
    if len(declared_fields) != len(set(declared_fields)):
        errors.append("input, artifact and log-derived identifiers must be disjoint")

    shared_raw = manifest.get("shared_references")
    shared: list[str] = []
    if not isinstance(shared_raw, list):
        errors.append("shared_references must be an array")
    else:
        for item in shared_raw:
            reference = _safe_reference(item)
            if reference is None:
                errors.append("shared_references must contain safe references/*.md paths")
            else:
                shared.append(reference)
        if len(shared) != len(set(shared)):
            errors.append("shared_references must be unique")
    if not shared or shared[0] != SOURCE_LOG_TEMPLATES_REFERENCE:
        errors.append("shared_references must start with references/source-log-templates.md")

    methods = manifest.get("methods")
    if not isinstance(methods, list) or not methods or len(methods) > 100:
        errors.append("methods must be a non-empty array with at most 100 items")
    if not isinstance(methods, list):
        methods = []
    result["method_count"] = len(methods)
    wiki_markers = _wiki_canonical_evidence_markers(wiki_templates)
    method_ids: set[str] = set()
    method_references: set[str] = set()
    priorities: list[int] = []
    marker_count = 0
    for index, method in enumerate(methods, start=1):
        if not isinstance(method, dict) or set(method) != METHOD_KEYS:
            errors.append(f"method {index} keys do not match the Methods package contract")
            continue
        method_id = method.get("id")
        if not isinstance(method_id, str) or not NAME_PATTERN.fullmatch(method_id):
            errors.append(f"method {index} id is invalid")
        elif method_id in method_ids:
            errors.append(f"method id is duplicated: {method_id}")
        else:
            method_ids.add(method_id)
        title = method.get("title")
        if not isinstance(title, str) or not title.strip() or "\n" in title or "\r" in title:
            errors.append(f"method {index} title is invalid")
        reference = _safe_reference(method.get("reference"))
        if reference is None:
            errors.append(f"method {index} reference is invalid")
        elif reference == SOURCE_LOG_TEMPLATES_REFERENCE:
            errors.append(f"method {index} must not use the fixed template reference")
        elif reference in method_references or reference in shared:
            errors.append(f"method reference is duplicated: {reference}")
        else:
            method_references.add(reference)
        priority = method.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 1:
            errors.append(f"method {index} priority is invalid")
        else:
            priorities.append(priority)
        markers = method.get("evidence_markers")
        if (
            not isinstance(markers, list)
            or not markers
            or len(markers) > 100
            or any(
                not isinstance(marker, str)
                or not marker
                or "\n" in marker
                or "\r" in marker
                or len(marker.encode("utf-8")) > 1024
                for marker in markers
            )
            or len(markers) != len(set(markers))
        ):
            errors.append(f"method {index} evidence_markers are invalid")
            markers = []
        for marker in markers:
            marker_count += 1
            if marker not in wiki_text:
                errors.append(f"method {index} evidence marker is absent from the Wiki: {marker}")
            if marker not in wiki_markers:
                errors.append(
                    f"method {index} evidence marker is not a canonical stable Wiki log marker: {marker}"
                )
    result["marker_count"] = marker_count
    if priorities != list(range(1, len(methods) + 1)):
        errors.append("method priorities must be unique and consecutive from 1")

    references_dir = package_root / "references"
    expected_references = {*shared, *method_references}
    if _real_directory(references_dir, "references directory", errors):
        actual_entries = list(references_dir.iterdir())
        actual_references = {
            f"references/{entry.name}"
            for entry in actual_entries
            if entry.is_file() and not entry.is_symlink()
        }
        if len(actual_entries) != len(actual_references) or actual_references != expected_references:
            errors.append("references directory does not exactly match methods.json")
        reference_texts: dict[str, str] = {}
        for reference in sorted(expected_references):
            path = package_root / reference
            if not _ordinary(path, reference, errors):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                errors.append(f"{reference} is not UTF-8: {exc}")
                continue
            reference_texts[reference] = text
            if reference in method_references:
                for heading in METHOD_HEADINGS:
                    if heading not in text:
                        errors.append(f"{reference} is missing heading: {heading}")
        if reference_texts.get(SOURCE_LOG_TEMPLATES_REFERENCE) != _render_source_log_templates(
            wiki_templates
        ):
            errors.append(
                "references/source-log-templates.md must exactly match the version 2 Wiki log template inventory"
            )
    return result


def _validate_registration(
    value: object,
    *,
    registration_id: str,
    expected_module: str,
    package_skill_name: object,
    package_source_sha256: object,
    required_user_inputs: object,
    errors: list[str],
) -> None:
    if not isinstance(value, dict):
        if value is not None:
            errors.append("registration-template.json must contain one object")
        return
    if set(value) != REGISTRATION_KEYS:
        errors.append("registration-template.json root keys do not match the registration contract")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        errors.append("registration schema_version must be 1")
    if value.get("registration_id") != registration_id or not NAME_PATTERN.fullmatch(
        registration_id
    ):
        errors.append("registration_id must be lower kebab-case and match the output directory")
    if value.get("version") != "1.0.0":
        errors.append("registration version must be 1.0.0")
    if value.get("deployment_scope") != "PRODUCTION":
        errors.append("registration deployment_scope must be PRODUCTION")
    capability = value.get("capability")
    if not isinstance(capability, str) or not capability.strip() or "\n" in capability or "\r" in capability:
        errors.append("registration capability must be non-empty single-line text")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("registration summary must be non-empty text")

    package = value.get("package")
    if not isinstance(package, dict):
        errors.append("registration package must be one object")
    else:
        if set(package) != PACKAGE_BINDING_KEYS:
            errors.append("registration package keys do not match the contract")
        if package.get("skill_name") != package_skill_name:
            errors.append("registration package skill_name differs from the Methods package")
        if package.get("relative_path") != f"package/{package_skill_name}":
            errors.append("registration package relative_path must equal package/<skill_name>")
        source_sha = package.get("source_wiki_sha256")
        if not isinstance(source_sha, str) or not SHA256_PATTERN.fullmatch(source_sha):
            errors.append("registration package source_wiki_sha256 is invalid")
        elif source_sha != package_source_sha256:
            errors.append("registration and Methods package Wiki digests differ")

    runtime = value.get("runtime")
    if not isinstance(runtime, dict):
        errors.append("registration runtime must be one object")
        return
    if set(runtime) != RUNTIME_KEYS:
        errors.append("registration runtime keys do not match the contract")
    for label, expected in (("diagnose", DIAGNOSE_BINDING), ("review", REVIEW_BINDING)):
        binding = runtime.get(label)
        if not isinstance(binding, dict) or set(binding) != RUNTIME_BINDING_KEYS or binding != expected:
            errors.append(f"runtime.{label} must use the fixed product binding")

    preprocessing = runtime.get("preprocessing")
    if not isinstance(preprocessing, dict):
        errors.append("runtime.preprocessing must be one object")
        return
    if set(preprocessing) != PREPROCESSING_KEYS:
        errors.append("runtime.preprocessing keys do not match the contract")
    if preprocessing.get("requires_logparse") is not True:
        errors.append("runtime.preprocessing.requires_logparse must be true")
    if preprocessing.get("logparse_product") != "default":
        errors.append("runtime.preprocessing.logparse_product must be default")

    roles = preprocessing.get("roles")
    if not isinstance(roles, list) or len(roles) != 2:
        errors.append("runtime.preprocessing.roles must contain client and server")
    else:
        for index, expected_label in enumerate(("client", "server")):
            role = roles[index]
            if not isinstance(role, dict) or set(role) != ROLE_KEYS:
                errors.append(f"runtime.preprocessing.roles[{index}] keys are invalid")
                continue
            if role.get("label") != expected_label or role.get("presence") != "REQUIRED":
                errors.append("runtime preprocessing roles must be required client then server")
            if not isinstance(role.get("description"), str) or not role["description"].strip():
                errors.append(f"runtime.preprocessing.roles[{index}].description is empty")
            if not isinstance(role.get("source_reference"), str) or not role[
                "source_reference"
            ].strip():
                errors.append(f"runtime.preprocessing.roles[{index}].source_reference is empty")

    plan = preprocessing.get("logparse_plan")
    if not isinstance(plan, dict):
        errors.append("runtime.preprocessing.logparse_plan must be one object")
        return
    if set(plan) != PLAN_KEYS:
        errors.append("runtime.preprocessing.logparse_plan keys do not match the contract")
    if plan.get("attachment_requirement") != "log_archive":
        errors.append("logparse_plan attachment_requirement must be log_archive")
    if plan.get("problem_time_binding") != {
        "source": "USER_FACT",
        "name": "problem_time",
    }:
        errors.append("logparse_plan problem_time_binding must use problem_time USER_FACT")

    expected_anchors = [
        {
            "label": "client",
            "module": {"source": "SKILL_FIXED", "value": expected_module},
            "slot": {"source": "USER_FACT", "name": "client_slot"},
            "process_name": {"source": "USER_FACT", "name": "client_process_name"},
            "pid": {"source": "USER_FACT", "name": "client_pid"},
        },
        {
            "label": "server",
            "module": {"source": "SKILL_FIXED", "value": expected_module},
            "slot": {"source": "USER_FACT", "name": "server_slot"},
            "process_name": {"source": "USER_FACT", "name": "server_process_name"},
            "pid": {"source": "USER_FACT", "name": "server_pid"},
        },
    ]
    anchors = plan.get("anchors")
    if anchors != expected_anchors:
        errors.append(
            "logparse_plan anchors must use one fixed module and exact client/server USER_FACT bindings"
        )
    elif any(not isinstance(anchor, dict) or set(anchor) != ANCHOR_KEYS for anchor in anchors):
        errors.append("logparse_plan anchor keys do not match the contract")
    if isinstance(required_user_inputs, list) and required_user_inputs[:7] != REQUIRED_INPUT_PREFIX:
        errors.append("registration bindings require the fixed seven-input Methods prefix")


def validate(
    registration_dir: Path,
    wiki: Path,
    module: str,
    source_identity: Path | None = None,
) -> dict[str, object]:
    errors: list[str] = []
    if not _valid_module(module):
        errors.append("--module must be non-empty canonical ASCII up to 128 bytes")
    if not _real_directory(registration_dir, "registration directory", errors):
        return {"ok": False, "errors": errors}
    if not _ordinary(wiki, "Wiki", errors):
        return {"ok": False, "errors": errors}
    registration_dir = registration_dir.resolve()
    wiki = wiki.resolve()

    wiki_bytes = wiki.read_bytes()
    try:
        wiki_text = wiki_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        errors.append(f"Wiki is not UTF-8: {exc}")
        wiki_text = ""
    wiki_sha256 = hashlib.sha256(wiki_bytes).hexdigest()
    wiki_templates = _wiki_log_templates(wiki_text)
    _validate_source_identity(
        source_identity,
        wiki_bytes=wiki_bytes,
        wiki_templates=wiki_templates,
        errors=errors,
    )

    root_names = {entry.name for entry in registration_dir.iterdir()}
    if root_names != REGISTRATION_ROOT_ENTRIES:
        errors.append(
            "registration root entries must be exactly registration-template.json and package"
        )
    package_parent = registration_dir / "package"
    package_root: Path | None = None
    if _real_directory(package_parent, "registration package directory", errors):
        package_children = list(package_parent.iterdir())
        if (
            len(package_children) != 1
            or not package_children[0].is_dir()
            or package_children[0].is_symlink()
        ):
            errors.append("registration package must contain exactly one real Skill directory")
        else:
            package_root = package_children[0]

    method_result: dict[str, object] = {
        "skill_name": None,
        "source_wiki_sha256": None,
        "required_user_inputs": [],
        "method_count": 0,
        "marker_count": 0,
    }
    if package_root is not None:
        if not NAME_PATTERN.fullmatch(package_root.name) or not package_root.name.startswith(
            "diagnose-"
        ):
            errors.append("package Skill directory must be lower kebab-case starting with diagnose-")
        package_names = {entry.name for entry in package_root.iterdir()}
        if package_names != PACKAGE_ROOT_ENTRIES:
            errors.append(
                "Methods package entries must be exactly SKILL.md, methods.json, and references"
            )
        frontmatter = _validate_business_skill(
            package_root / "SKILL.md",
            expected_name=package_root.name,
            errors=errors,
        )
        method_result = _validate_methods(
            package_root,
            wiki_sha256=wiki_sha256,
            wiki_text=wiki_text,
            wiki_templates=wiki_templates,
            expected_skill_name=package_root.name,
            frontmatter=frontmatter,
            errors=errors,
        )
        _validate_package_tokens(package_root, errors)

    registration = _read_json(
        registration_dir / "registration-template.json",
        "registration-template.json",
        errors,
    )
    _validate_registration(
        registration,
        registration_id=registration_dir.name,
        expected_module=module,
        package_skill_name=method_result["skill_name"],
        package_source_sha256=method_result["source_wiki_sha256"],
        required_user_inputs=method_result["required_user_inputs"],
        errors=errors,
    )

    for path in registration_dir.rglob("*"):
        try:
            metadata = path.lstat()
        except OSError:
            errors.append(f"generated path disappeared during validation: {path}")
            continue
        if stat.S_ISLNK(metadata.st_mode):
            errors.append(f"generated registration must not contain symlinks: {path}")
        elif not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            errors.append(f"generated registration contains unsupported path type: {path}")

    return {
        "ok": not errors,
        "registration_id": registration_dir.name,
        "skill_name": method_result["skill_name"],
        "source_wiki_sha256": wiki_sha256,
        "module": module,
        "method_count": method_result["method_count"],
        "marker_count": method_result["marker_count"],
        "template_count": len(wiki_templates),
        "log_template_extraction_version": LOG_TEMPLATE_EXTRACTION_VERSION,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registration-dir", type=Path, required=True)
    parser.add_argument("--wiki", type=Path, required=True)
    parser.add_argument("--module", required=True)
    parser.add_argument("--source-identity", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = validate(
        args.registration_dir,
        args.wiki,
        args.module,
        args.source_identity,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    elif result["ok"]:
        print(
            f"PASS: {result['registration_id']} / {result['skill_name']} "
            f"({result['method_count']} methods, {result['marker_count']} markers)"
        )
    else:
        for error in result["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
