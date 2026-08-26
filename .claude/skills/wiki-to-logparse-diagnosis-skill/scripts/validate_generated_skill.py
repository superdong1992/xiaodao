#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from pathlib import Path, PurePosixPath


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
ROOT_FILES = {"SKILL.md", "methods.json", "logparse.json", "references", "scripts"}
SCRIPT_FILES = {"pack_result_zip.py"}
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
LOGPARSE_ROOT_KEYS = {
    "schema_version",
    "helper_skill",
    "module",
    "problem_time_input",
    "artifact_input",
    "roles",
}
ROLE_KEYS = {
    "label",
    "required",
    "slot_input",
    "process_name_input",
    "pid_input",
}
REQUIRED_INPUT_PREFIX = [
    "problem_time",
    "client_slot",
    "client_process_name",
    "server_slot",
    "server_process_name",
]
FORBIDDEN_INPUT_ALIASES = {
    "api_name",
    "client_process",
    "server_process",
    "service_name",
    "slot",
}
EXPECTED_ROLES = [
    {
        "label": "client",
        "required": True,
        "slot_input": "client_slot",
        "process_name_input": "client_process_name",
        "pid_input": "client_pid",
    },
    {
        "label": "server",
        "required": True,
        "slot_input": "server_slot",
        "process_name_input": "server_process_name",
        "pid_input": "server_pid",
    },
]
SOURCE_LOG_TEMPLATES_REFERENCE = "references/source-log-templates.md"
SOURCE_IDENTITY_SCHEMA_VERSION = 2
LOG_TEMPLATE_EXTRACTION_VERSION = 1
REQUIRED_SKILL_PHRASES = (
    "methods.json",
    "logparse.json",
    "required_user_inputs",
    "required_artifacts",
    "client_slot",
    "client_process_name",
    "server_slot",
    "server_process_name",
    "Skill(logparse-diagnose)",
    "target_logs[*].log_path",
    "identity_tokens",
    "result.txt",
    "result.zip",
    "pack_result_zip.py",
    "<label>__<module>__slot_<slot>__<process_name>[__pid_<pid>].log",
)
REQUIRED_SKILL_SEMANTICS = (
    (
        "exactly one Skill(logparse-diagnose) load",
        re.compile(
            r"(?:恰好|仅|只)(?:加载|调用)一次\s*`?Skill\(logparse-diagnose\)`?"
            r"|(?:load|invoke)\s+`?Skill\(logparse-diagnose\)`?\s+exactly once"
            r"|(?:load|invoke)\s+exactly once\s+`?Skill\(logparse-diagnose\)`?",
            re.IGNORECASE,
        ),
    ),
    ("flat result.zip delivery", re.compile(r"扁平|\bflat\b", re.IGNORECASE)),
    ("actually used logs", re.compile(r"实际使用|所用日志|使用日志|\bused logs?\b", re.IGNORECASE)),
    ("direct conclusion", re.compile(r"结论|\bconclusion\b", re.IGNORECASE)),
    ("key evidence", re.compile(r"关键证据|\bkey evidence\b", re.IGNORECASE)),
    ("evidence gaps", re.compile(r"证据缺口|\bevidence gaps?\b", re.IGNORECASE)),
)
FORBIDDEN_SKILL_PATTERNS = (
    (
        "direct problem-locator-logparse command",
        re.compile(
            r"(?m)^[ \t]*(?:\$[ \t]*)?`?problem-locator-logparse(?:[ \t]|`|$)"
        ),
    ),
    (
        "direct legacy cli.py command",
        re.compile(
            r"(?m)^[^\r\n]*\bcli\.py[ \t]+(?:parse|mech-target-logs)\b"
        ),
    ),
    ("SKILL_FIXED binding", re.compile(r"\bSKILL_FIXED\b")),
)


def _ordinary(path: Path, label: str, errors: list[str]) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        errors.append(f"missing {label}: {path}")
        return False
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        errors.append(f"{label} must be one ordinary file: {path}")
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


def _read_json(path: Path, errors: list[str]) -> object | None:
    if not _ordinary(path, path.name, errors):
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"invalid JSON in {path.name}: {exc}")
        return None


def _field_ids(value: object, label: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{label} must be an array")
        return []
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
    in_text_fence = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped == "```text":
            in_text_fence = True
            continue
        if stripped == "```" and in_text_fence:
            in_text_fence = False
            continue
        if in_text_fence and stripped and LOG_PLACEHOLDER_PATTERN.search(stripped):
            templates.append(stripped)
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
    match = LOG_PLACEHOLDER_PATTERN.search(template)
    if match is None:
        return template.strip() or None
    prefix = template[: match.start()].strip()
    if prefix:
        return prefix
    literal_segments = [
        segment.strip()
        for segment in LOG_PLACEHOLDER_PATTERN.split(template)[::2]
        if segment.strip()
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


def _validate_logparse(
    value: object,
    *,
    expected_module: str,
    errors: list[str],
) -> None:
    if not isinstance(value, dict):
        if value is not None:
            errors.append("logparse.json must contain one object")
        return
    if set(value) != LOGPARSE_ROOT_KEYS:
        errors.append("logparse.json root keys do not match the LAN Logparse contract")
    if value.get("schema_version") != 1:
        errors.append("logparse.json schema_version must be 1")
    if value.get("helper_skill") != "logparse-diagnose":
        errors.append("logparse.json helper_skill must be logparse-diagnose")
    module = value.get("module")
    if not _valid_module(module):
        errors.append("logparse.json module must be non-empty canonical ASCII up to 128 bytes")
    elif module != expected_module:
        errors.append("logparse.json module does not match the user-confirmed module")
    if value.get("problem_time_input") != "problem_time":
        errors.append("logparse.json problem_time_input must be problem_time")
    if value.get("artifact_input") != "log_archive":
        errors.append("logparse.json artifact_input must be log_archive")
    roles = value.get("roles")
    if roles != EXPECTED_ROLES:
        errors.append("logparse.json roles must be the exact required client/server input mappings")
    elif any(set(role) != ROLE_KEYS for role in roles):
        errors.append("logparse.json role keys do not match the LAN Logparse contract")


def validate(skill_dir: Path, wiki: Path, module: str) -> dict[str, object]:
    errors: list[str] = []
    skill_dir = skill_dir.resolve()
    wiki = wiki.resolve()
    if not _valid_module(module):
        errors.append("--module must be non-empty canonical ASCII up to 128 bytes")
    if not skill_dir.is_dir() or skill_dir.is_symlink():
        errors.append(f"skill directory is unavailable: {skill_dir}")
        return {"ok": False, "errors": errors}
    if not _ordinary(wiki, "Wiki", errors):
        return {"ok": False, "errors": errors}

    root_names = {entry.name for entry in skill_dir.iterdir()}
    if root_names != ROOT_FILES:
        errors.append(
            "generated root entries must be exactly SKILL.md, methods.json, logparse.json, references, scripts"
        )
    references_dir = skill_dir / "references"
    if not references_dir.is_dir() or references_dir.is_symlink():
        errors.append("references must be one ordinary directory")
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.is_dir() or scripts_dir.is_symlink():
        errors.append("scripts must be one ordinary directory")
    elif {entry.name for entry in scripts_dir.iterdir()} != SCRIPT_FILES:
        errors.append("scripts must contain exactly pack_result_zip.py")

    skill_path = skill_dir / "SKILL.md"
    skill_text = ""
    frontmatter: dict[str, str] = {}
    if _ordinary(skill_path, "SKILL.md", errors):
        try:
            skill_text = skill_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"SKILL.md is not UTF-8: {exc}")
        else:
            frontmatter = _frontmatter(skill_text, errors)
            for phrase in REQUIRED_SKILL_PHRASES:
                if phrase not in skill_text:
                    errors.append(f"SKILL.md must mention {phrase}")
            for label, pattern in REQUIRED_SKILL_SEMANTICS:
                if pattern.search(skill_text) is None:
                    errors.append(f"SKILL.md must require {label}")
            if "sources" not in skill_text and "来源路径" not in skill_text:
                errors.append("SKILL.md must preserve evidence sources or 来源路径")
            for label, pattern in FORBIDDEN_SKILL_PATTERNS:
                if pattern.search(skill_text):
                    errors.append(f"SKILL.md must delegate instead of embedding {label}")

    expected_packer = Path(__file__).resolve().parents[1] / "assets" / "pack_result_zip.py"
    generated_packer = scripts_dir / "pack_result_zip.py"
    if _ordinary(expected_packer, "meta Skill pack_result_zip.py asset", errors) and _ordinary(
        generated_packer, "generated pack_result_zip.py", errors
    ):
        if generated_packer.read_bytes() != expected_packer.read_bytes():
            errors.append("generated pack_result_zip.py must exactly match the meta Skill asset")

    logparse = _read_json(skill_dir / "logparse.json", errors)
    _validate_logparse(logparse, expected_module=module, errors=errors)
    manifest = _read_json(skill_dir / "methods.json", errors)

    wiki_bytes = wiki.read_bytes() if wiki.exists() else b""
    try:
        wiki_text = wiki_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        errors.append(f"Wiki is not UTF-8: {exc}")
        wiki_text = ""
    wiki_sha256 = hashlib.sha256(wiki_bytes).hexdigest()
    wiki_templates = _wiki_log_templates(wiki_text)
    wiki_named_log_fields = _wiki_named_log_fields(wiki_templates)
    wiki_canonical_markers = _wiki_canonical_evidence_markers(wiki_templates)

    method_count = 0
    marker_count = 0
    all_references: set[str] = set()
    if isinstance(manifest, dict):
        if set(manifest) != METHODS_ROOT_KEYS:
            errors.append("methods.json root keys do not match the methods package contract")
        if manifest.get("schema_version") != 1:
            errors.append("methods.json schema_version must be 1")
        skill_name = manifest.get("skill_name")
        if not isinstance(skill_name, str) or not NAME_PATTERN.fullmatch(skill_name):
            errors.append("methods.json skill_name is invalid")
        else:
            if not skill_name.startswith("diagnose-"):
                errors.append("methods.json skill_name must start with diagnose-")
            if skill_name != skill_dir.name:
                errors.append("methods.json skill_name must match the skill directory")
            if frontmatter.get("name") != skill_name:
                errors.append("SKILL.md name must match methods.json skill_name")
        source_sha = manifest.get("source_wiki_sha256")
        if not isinstance(source_sha, str) or not SHA256_PATTERN.fullmatch(source_sha):
            errors.append("source_wiki_sha256 must be lowercase SHA-256")
        elif source_sha != wiki_sha256:
            errors.append("source_wiki_sha256 does not match the supplied Wiki")

        required_user_inputs = _field_ids(
            manifest.get("required_user_inputs"), "required_user_inputs", errors
        )
        if required_user_inputs[: len(REQUIRED_INPUT_PREFIX)] != REQUIRED_INPUT_PREFIX:
            errors.append(
                "required_user_inputs must start with problem_time and the required client/server slot/process inputs"
            )
        aliases = sorted(FORBIDDEN_INPUT_ALIASES.intersection(required_user_inputs))
        if aliases:
            errors.append("required_user_inputs contains forbidden aliases: " + ", ".join(aliases))

        required_artifacts = _field_ids(
            manifest.get("required_artifacts"), "required_artifacts", errors
        )
        if not required_artifacts or required_artifacts[0] != "log_archive":
            errors.append("required_artifacts must start with log_archive")
        log_derived_fields = _field_ids(
            manifest.get("log_derived_fields"), "log_derived_fields", errors
        )
        expected_log_derived_fields = [
            field for field in wiki_named_log_fields if field not in required_user_inputs
        ]
        if log_derived_fields != expected_log_derived_fields:
            errors.append(
                "log_derived_fields must be the named Wiki log fields in first-appearance order, excluding required_user_inputs"
            )
        declared_fields = required_user_inputs + required_artifacts + log_derived_fields
        if len(declared_fields) != len(set(declared_fields)):
            errors.append("input, artifact and log-derived identifiers must be disjoint")

        shared = manifest.get("shared_references")
        if not isinstance(shared, list) or any(_safe_reference(item) is None for item in shared):
            errors.append("shared_references must contain safe references/*.md paths")
            shared = []
        if len(shared) != len(set(shared)):
            errors.append("shared_references must be unique")
        if not shared or shared[0] != SOURCE_LOG_TEMPLATES_REFERENCE:
            errors.append("shared_references must start with references/source-log-templates.md")
        all_references.update(str(item) for item in shared)

        methods = manifest.get("methods")
        if not isinstance(methods, list) or not methods:
            errors.append("methods must be a non-empty array")
            methods = []
        method_count = len(methods)
        ids: set[str] = set()
        priorities: list[int] = []
        method_references: set[str] = set()
        for index, method in enumerate(methods, start=1):
            if not isinstance(method, dict) or set(method) != METHOD_KEYS:
                errors.append(f"method {index} keys do not match the methods package contract")
                continue
            method_id = method.get("id")
            if not isinstance(method_id, str) or not NAME_PATTERN.fullmatch(method_id):
                errors.append(f"method {index} id is invalid")
            elif method_id in ids:
                errors.append(f"method id is duplicated: {method_id}")
            else:
                ids.add(method_id)
            title = method.get("title")
            if not isinstance(title, str) or not title.strip():
                errors.append(f"method {index} title is empty")
            reference = _safe_reference(method.get("reference"))
            if reference is None:
                errors.append(f"method {index} reference is invalid")
            elif reference == SOURCE_LOG_TEMPLATES_REFERENCE:
                errors.append(f"method {index} must not use the fixed template reference")
            elif reference in method_references:
                errors.append(f"method reference is duplicated: {reference}")
            else:
                method_references.add(reference)
                all_references.add(reference)
            priority = method.get("priority")
            if not isinstance(priority, int) or isinstance(priority, bool) or priority < 1:
                errors.append(f"method {index} priority is invalid")
            else:
                priorities.append(priority)
            markers = method.get("evidence_markers")
            if (
                not isinstance(markers, list)
                or not markers
                or any(not isinstance(marker, str) or not marker for marker in markers)
                or len(markers) != len(set(markers))
            ):
                errors.append(f"method {index} evidence_markers are invalid")
                markers = []
            for marker in markers:
                marker_count += 1
                if marker not in wiki_text:
                    errors.append(f"method {index} evidence marker is absent from the Wiki: {marker}")
                if marker not in wiki_canonical_markers:
                    errors.append(
                        f"method {index} evidence marker is not a canonical stable Wiki log marker: {marker}"
                    )
        if priorities != list(range(1, method_count + 1)):
            errors.append("method priorities must be unique and consecutive from 1")

        if references_dir.is_dir():
            actual_references = {
                f"references/{entry.name}"
                for entry in references_dir.iterdir()
                if entry.is_file() and not entry.is_symlink()
            }
            if actual_references != all_references:
                errors.append("references directory does not exactly match methods.json")
            reference_texts: dict[str, str] = {}
            for reference in sorted(all_references):
                path = skill_dir / reference
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
                    "references/source-log-templates.md must exactly match the mechanically extracted Wiki log template inventory"
                )
    elif manifest is not None:
        errors.append("methods.json must contain one object")

    for path in skill_dir.rglob("*"):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            errors.append(f"generated path disappeared during validation: {path}")
            continue
        if stat.S_ISLNK(metadata.st_mode):
            errors.append(f"generated package must not contain symlinks: {path}")
        elif not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            errors.append(f"generated package contains unsupported path type: {path}")

    package_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(skill_dir.rglob("*"))
        if path.is_file() and not path.is_symlink() and path.suffix in {".md", ".json"}
    )
    for template in wiki_templates:
        if template not in package_text:
            errors.append(f"generated package lost Wiki log template: {template}")

    return {
        "ok": not errors,
        "skill_name": frontmatter.get("name"),
        "source_wiki_sha256": wiki_sha256,
        "module": module,
        "method_count": method_count,
        "marker_count": marker_count,
        "template_count": len(wiki_templates),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument("--wiki", type=Path, required=True)
    parser.add_argument("--module", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = validate(args.skill_dir, args.wiki, args.module)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    elif result["ok"]:
        print(
            f"PASS: {result['skill_name']} "
            f"({result['method_count']} methods, {result['marker_count']} markers)"
        )
    else:
        for error in result["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
