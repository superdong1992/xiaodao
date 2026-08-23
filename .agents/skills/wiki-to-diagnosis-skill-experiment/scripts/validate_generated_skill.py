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
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
METHOD_HEADINGS = (
    "## 适用条件",
    "## 所需证据",
    "## 计算与判断",
    "## 确认条件",
    "## 未知边界",
    "## 输出含义",
)
ROOT_FILES = {"SKILL.md", "methods.json", "references"}
ROOT_KEYS = {
    "schema_version",
    "skill_name",
    "source_wiki_sha256",
    "shared_references",
    "methods",
}
METHOD_KEYS = {"id", "title", "reference", "priority", "evidence_markers"}


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


def validate(skill_dir: Path, wiki: Path) -> dict[str, object]:
    errors: list[str] = []
    skill_dir = skill_dir.resolve()
    wiki = wiki.resolve()
    if not skill_dir.is_dir() or skill_dir.is_symlink():
        errors.append(f"skill directory is unavailable: {skill_dir}")
        return {"ok": False, "errors": errors}
    if not _ordinary(wiki, "Wiki", errors):
        return {"ok": False, "errors": errors}

    root_names = {entry.name for entry in skill_dir.iterdir()}
    if root_names != ROOT_FILES:
        errors.append(
            "generated root entries must be exactly SKILL.md, methods.json, references"
        )
    references_dir = skill_dir / "references"
    if not references_dir.is_dir() or references_dir.is_symlink():
        errors.append("references must be one ordinary directory")

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
            for required_phrase in ("methods.json", "target_logs", "Logparse"):
                if required_phrase not in skill_text:
                    errors.append(f"SKILL.md must mention {required_phrase}")

    manifest = _read_json(skill_dir / "methods.json", errors)
    wiki_bytes = wiki.read_bytes() if wiki.exists() else b""
    try:
        wiki_text = wiki_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        errors.append(f"Wiki is not UTF-8: {exc}")
        wiki_text = ""
    wiki_sha256 = hashlib.sha256(wiki_bytes).hexdigest()

    method_count = 0
    marker_count = 0
    all_references: set[str] = set()
    if isinstance(manifest, dict):
        if set(manifest) != ROOT_KEYS:
            errors.append("methods.json root keys do not match the experiment contract")
        if manifest.get("schema_version") != 1:
            errors.append("methods.json schema_version must be 1")
        skill_name = manifest.get("skill_name")
        if not isinstance(skill_name, str) or not NAME_PATTERN.fullmatch(skill_name):
            errors.append("methods.json skill_name is invalid")
        else:
            if skill_name != skill_dir.name:
                errors.append("methods.json skill_name must match the skill directory")
            if frontmatter.get("name") != skill_name:
                errors.append("SKILL.md name must match methods.json skill_name")
        source_sha = manifest.get("source_wiki_sha256")
        if not isinstance(source_sha, str) or not SHA256_PATTERN.fullmatch(source_sha):
            errors.append("source_wiki_sha256 must be lowercase SHA-256")
        elif source_sha != wiki_sha256:
            errors.append("source_wiki_sha256 does not match the supplied Wiki")

        shared = manifest.get("shared_references")
        if not isinstance(shared, list) or any(_safe_reference(item) is None for item in shared):
            errors.append("shared_references must contain safe references/*.md paths")
            shared = []
        if len(shared) != len(set(shared)):
            errors.append("shared_references must be unique")
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
                errors.append(f"method {index} keys do not match the experiment contract")
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
                    errors.append(
                        f"method {index} evidence marker is absent from the Wiki: {marker}"
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
            for reference in sorted(all_references):
                path = skill_dir / reference
                if not _ordinary(path, reference, errors):
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError as exc:
                    errors.append(f"{reference} is not UTF-8: {exc}")
                    continue
                if reference in method_references:
                    for heading in METHOD_HEADINGS:
                        if heading not in text:
                            errors.append(f"{reference} is missing heading: {heading}")
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

    return {
        "ok": not errors,
        "skill_name": frontmatter.get("name"),
        "source_wiki_sha256": wiki_sha256,
        "method_count": method_count,
        "marker_count": marker_count,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument("--wiki", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = validate(args.skill_dir, args.wiki)
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
