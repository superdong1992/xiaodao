from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import zipfile


PRODUCT_FILES = {"SKILL.md", "diagnosis-skill.json"}
MANIFEST_REQUIRED = {
    "schema_version",
    "id",
    "version",
    "capability",
    "summary",
    "entry_document",
    "tool_bundle_id",
    "requires_logparse",
    "requirements",
    "logparse_plan",
}
MANIFEST_OPTIONAL = {"logparse_product"}


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def _embedded_manifest(markdown: str) -> object:
    matches = re.findall(
        r"(?ms)<!-- DIAGNOSIS_SKILL_MANIFEST_V2_BEGIN -->\s*```json\s*(.*?)\s*```\s*<!-- DIAGNOSIS_SKILL_MANIFEST_V2_END -->",
        markdown,
    )
    if len(matches) != 1:
        raise ValueError("SKILL.md must embed exactly one manifest v2 block")
    return json.loads(matches[0])


def validate_skill_directory(skill_dir: str | Path) -> ValidationResult:
    root = Path(skill_dir)
    errors: list[str] = []
    if not root.is_dir() or root.is_symlink():
        return ValidationResult(("Skill product must be a real directory",))
    actual = {path.name for path in root.iterdir()}
    if actual != PRODUCT_FILES:
        errors.append(
            f"generated files must be exactly {sorted(PRODUCT_FILES)!r}; got {sorted(actual)!r}"
        )
        return ValidationResult(tuple(errors))
    try:
        raw_manifest = (root / "diagnosis-skill.json").read_bytes()
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ValidationResult(("diagnosis-skill.json must be UTF-8 JSON",))
    if not isinstance(manifest, dict):
        return ValidationResult(("diagnosis-skill.json must be an object",))
    if raw_manifest != _canonical_json_bytes(manifest):
        errors.append("diagnosis-skill.json must be Canonical JSON")
    actual_fields = set(manifest)
    if not MANIFEST_REQUIRED <= actual_fields or actual_fields - MANIFEST_REQUIRED - MANIFEST_OPTIONAL:
        errors.append("diagnosis-skill.json field set is invalid")
    if manifest.get("schema_version") != 2:
        errors.append("manifest schema_version must be 2")
    if manifest.get("entry_document") != "SKILL.md":
        errors.append("entry_document must be SKILL.md")
    if manifest.get("tool_bundle_id") != "tool-bundle/diagnose":
        errors.append("tool_bundle_id must be tool-bundle/diagnose")
    version = manifest.get("version")
    match = re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version or "")
    if match is None or int(match.group(1)) < 3:
        errors.append("manifest version must be 3.0.0 or later")
    requirements = manifest.get("requirements")
    if not isinstance(requirements, list):
        errors.append("requirements must be an array")
        requirements = []
    names = [item.get("name") for item in requirements if isinstance(item, dict)]
    if len(names) != len(requirements) or len(names) != len(set(names)):
        errors.append("requirements must contain unique named objects")
    if any("required" in item for item in requirements if isinstance(item, dict)):
        errors.append("requirements are intrinsically required and forbid a required field")
    requires_logparse = manifest.get("requires_logparse")
    if type(requires_logparse) is not bool:
        errors.append("requires_logparse must be boolean")
    if requires_logparse:
        if not isinstance(manifest.get("logparse_plan"), dict):
            errors.append("logparse Skill requires logparse_plan")
        if manifest.get("logparse_product") == "default":
            errors.append("default logparse_product must be omitted")
    else:
        if manifest.get("logparse_plan") is not None:
            errors.append("non-logparse Skill requires logparse_plan=null")
        if "logparse_product" in manifest:
            errors.append("non-logparse Skill must omit logparse_product")
        if any(item.get("stage") == "AFTER_LOGPARSE" for item in requirements if isinstance(item, dict)):
            errors.append("non-logparse Skill forbids AFTER_LOGPARSE")
    try:
        markdown = (root / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ValidationResult(tuple(errors + ["SKILL.md must be UTF-8 text"]))
    frontmatter = re.match(r"(?s)^---\nname: ([^\n]+)\ndescription: .*?\n---\n", markdown)
    if frontmatter is None or frontmatter.group(1) != manifest.get("id"):
        errors.append("SKILL.md frontmatter name must equal manifest id")
    try:
        embedded = _embedded_manifest(markdown)
        if embedded != manifest:
            errors.append("SKILL.md embedded manifest must equal diagnosis-skill.json")
    except (ValueError, json.JSONDecodeError) as exc:
        errors.append(str(exc))
    for required_phrase in (
        "USER_RESULT_ARCHIVE",
        "problem-locator-pack-result",
        "result.zip",
        "固定子序列",
        "USER_FACT",
        "READY_ATTACHMENT",
    ):
        if required_phrase not in markdown:
            errors.append(f"SKILL.md is missing {required_phrase}")
    if any(
        item.get("stage") == "AFTER_LOGPARSE"
        for item in requirements
        if isinstance(item, dict)
    ):
        for required_phrase in (
            "AFTER_LOGPARSE",
            "state_delta.add_evidence_bindings",
            "evidence_proposal_key",
            "target-logs",
        ):
            if required_phrase not in markdown:
                errors.append(f"SKILL.md is missing {required_phrase}")
    return ValidationResult(tuple(errors))


def validate_result_zip(zip_path: str | Path) -> ValidationResult:
    path = Path(zip_path)
    if not path.is_file():
        return ValidationResult((f"missing result.zip: {path}",))
    errors: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            expected = ["result.txt"] + [
                f"target-log-{index:03d}.log"
                for index in range(1, len(names))
            ]
            if names != expected or len(names) != len(set(names)):
                errors.append("result.zip entries are not the deterministic flat sequence")
            if any(item.date_time != (1980, 1, 1, 0, 0, 0) for item in infos):
                errors.append("result.zip timestamps are not deterministic")
            if infos and not archive.read("result.txt").decode("utf-8").strip():
                errors.append("result.txt must be non-empty UTF-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError):
        errors.append("result.zip is unreadable or invalid")
    return ValidationResult(tuple(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a generated Diagnosis Skill v3.")
    parser.add_argument("skill_dir", type=Path)
    parser.add_argument("--result-zip", type=Path)
    args = parser.parse_args(argv)
    results = [("skill", validate_skill_directory(args.skill_dir))]
    if args.result_zip is not None:
        results.append(("result.zip", validate_result_zip(args.result_zip)))
    for label, result in results:
        if result.ok:
            print(f"{label}: OK")
        else:
            for error in result.errors:
                print(f"{label}: {error}", file=sys.stderr)
    return 0 if all(result.ok for _, result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
