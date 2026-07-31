from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import sys
import zipfile


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from generate_diagnosis_skill import (  # noqa: E402
    GENERATOR_VERSION,
    MANIFEST_FIELDS,
    PRODUCT_FILES,
    SEMVER_PATTERN,
    SKILL_ID_PATTERN,
    canonical_json_bytes,
    product_sha256,
    validate_content_type,
)


REQUIRED_FRONTMATTER_FIELDS = frozenset({"name", "description"})
REQUIRED_RESULT_TYPES = ("NEED_INPUT", "NEED_ATTACHMENT", "REROUTE", "COMPLETED")
REQUIRED_PHRASES = (
    "S00",
    "AgentJobOutcome",
    "DiagnosisStateDelta",
    "output/job_outcome.json",
    "inputs/manifest.json",
    "PREVIOUS_OUTCOME",
    "Candidate",
    "USER_RESULT",
    "diagnosis-result.json",
    "output/proposals/user-result/payload",
    "problem-locator-diagnosis-v1",
    "application/json",
    "REVIEW",
    "Canonical JSON",
)
LOGPARSE_REQUIRED_PHRASES = (
    "logparse-diagnose",
    "problem-locator-logparse",
    "parse-targets",
    "target-logs",
    "LOGPARSE_RUN",
)
SERVICE_TAKEOVER_REQUIRED_PHRASES = (
    "caller_service",
    "server_service",
    "rpc_method",
    "problem_time",
    "log_archive",
    "order_id",
)


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_skill_dir(skill_dir: str | Path) -> ValidationResult:
    root = Path(skill_dir)
    errors: list[str] = []
    files = _read_plain_product_files(root, errors)
    if set(files) != set(PRODUCT_FILES):
        errors.append(
            "generated product files must be exactly SKILL.md and diagnosis-skill.json; "
            f"actual={sorted(files)}"
        )

    frontmatter: dict[str, object] = {}
    body = ""
    skill_payload = files.get("SKILL.md")
    if skill_payload is None:
        errors.append("missing SKILL.md")
    else:
        frontmatter, body, read_errors = _read_skill(skill_payload)
        errors.extend(read_errors)

    manifest: dict[str, object] | None = None
    manifest_payload = files.get("diagnosis-skill.json")
    if manifest_payload is None:
        errors.append("missing diagnosis-skill.json")
    else:
        manifest, manifest_errors = _read_manifest(manifest_payload)
        errors.extend(manifest_errors)

    _validate_frontmatter(frontmatter, root, manifest, errors)
    if manifest is not None:
        _validate_manifest(manifest, root, errors)
    _validate_body(body, manifest, errors)
    return ValidationResult(tuple(errors))


def validate_result_zip(zip_path: str | Path) -> ValidationResult:
    """Retain the 1.x flat-zip validator for copied-source compatibility."""

    path = Path(zip_path)
    if not path.is_file():
        return ValidationResult((f"missing result.zip: {path}",))
    errors: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = [entry.filename for entry in archive.infolist()]
    except (OSError, zipfile.BadZipFile):
        return ValidationResult(("result.zip is unreadable or invalid",))
    if "result.txt" not in names:
        errors.append("result.zip must contain result.txt at the root")
    for name in names:
        normalized = name.replace("\\", "/")
        if normalized.endswith("/") or "/" in normalized:
            errors.append(f"result.zip must be flat: {name}")
        if Path(normalized).name == "manifest.txt":
            errors.append("result.zip must not contain manifest.txt")
    return ValidationResult(tuple(errors))


def skill_product_sha256(skill_dir: str | Path) -> str:
    errors: list[str] = []
    files = _read_plain_product_files(Path(skill_dir), errors)
    if errors:
        raise ValueError("; ".join(errors))
    return product_sha256(files)


def _read_plain_product_files(root: Path, errors: list[str]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    if root.is_symlink() or not root.is_dir():
        errors.append(f"Skill path must be a plain directory: {root}")
        return files
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names.sort()
        file_names.sort()
        directory_path = Path(directory)
        for name in list(directory_names):
            path = directory_path / name
            if path.is_symlink():
                errors.append(f"product directory must not contain symlinks: {path}")
                directory_names.remove(name)
        for name in file_names:
            path = directory_path / name
            try:
                metadata = path.lstat()
            except OSError as exc:
                errors.append(f"cannot stat product file {path}: {exc}")
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                errors.append(f"product entries must be single-link plain files: {path}")
                continue
            relative = path.relative_to(root).as_posix()
            try:
                files[relative] = path.read_bytes()
            except OSError as exc:
                errors.append(f"cannot read product file {path}: {exc}")
    return files


def _read_skill(payload: bytes) -> tuple[dict[str, object], str, list[str]]:
    errors: list[str] = []
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return {}, "", ["SKILL.md must be UTF-8"]
    if "\r" in text or not text.endswith("\n"):
        errors.append("SKILL.md must use LF line endings and end with one LF")
    match = re.match(r"\A---\n(?P<frontmatter>.*?)\n---\n", text, re.DOTALL)
    if match is None:
        return {}, text, errors + ["SKILL.md must start with bounded frontmatter"]
    frontmatter: dict[str, object] = {}
    for line_number, line in enumerate(match.group("frontmatter").splitlines(), start=2):
        if not line or ":" not in line:
            errors.append(f"frontmatter line {line_number} must be key: JSON-scalar")
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        raw = raw.strip()
        if key in frontmatter:
            errors.append(f"duplicate frontmatter key: {key}")
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        frontmatter[key] = value
    return frontmatter, text[match.end() :], errors


def _read_manifest(payload: bytes) -> tuple[dict[str, object] | None, list[str]]:
    errors: list[str] = []
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, ["diagnosis-skill.json must be UTF-8 JSON"]
    if not isinstance(value, dict):
        return None, ["diagnosis-skill.json must be an object"]
    if canonical_json_bytes(value) != payload:
        errors.append("diagnosis-skill.json must be exact S00 Canonical JSON bytes")
    return value, errors


def _validate_frontmatter(
    frontmatter: dict[str, object],
    root: Path,
    manifest: dict[str, object] | None,
    errors: list[str],
) -> None:
    if set(frontmatter) != REQUIRED_FRONTMATTER_FIELDS:
        errors.append("SKILL.md frontmatter must contain exactly name and description")
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or SKILL_ID_PATTERN.fullmatch(name) is None:
        errors.append("frontmatter name must match diagnose-<lower-kebab-capability>")
    elif name != root.name:
        errors.append("frontmatter name must equal the Skill directory name")
    if not isinstance(description, str) or not description.strip():
        errors.append("frontmatter description must be non-empty")
    if manifest is not None and name != manifest.get("id"):
        errors.append("frontmatter name must equal diagnosis-skill.json id")


def _validate_manifest(manifest: dict[str, object], root: Path, errors: list[str]) -> None:
    if set(manifest) != MANIFEST_FIELDS:
        errors.append(
            "diagnosis-skill.json must contain the exact S04 field set; "
            f"actual={sorted(manifest)}"
        )
        return
    skill_id = manifest.get("id")
    version = manifest.get("version")
    capability = manifest.get("capability")
    summary = manifest.get("summary")
    requires_logparse = manifest.get("requires_logparse")
    logparse_product = manifest.get("logparse_product")
    if manifest.get("schema_version") != 1:
        errors.append("manifest schema_version must be 1")
    if not isinstance(skill_id, str) or SKILL_ID_PATTERN.fullmatch(skill_id) is None:
        errors.append("manifest id is invalid")
    elif skill_id != root.name:
        errors.append("manifest id must equal directory name")
    if not isinstance(version, str):
        errors.append("manifest version must be a semantic-version string")
    else:
        match = SEMVER_PATTERN.fullmatch(version)
        if match is None or int(match.group("major")) < 2:
            errors.append("manifest version must be 2.0.0 or later")
    if not isinstance(capability, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", capability):
        errors.append("manifest capability must be a stable lower-kebab id")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("manifest summary must be non-empty")
    if manifest.get("entry_document") != "SKILL.md":
        errors.append("manifest entry_document must be SKILL.md")
    if manifest.get("tool_bundle_id") != "tool-bundle/diagnose":
        errors.append("manifest tool_bundle_id must be tool-bundle/diagnose")
    if type(requires_logparse) is not bool:
        errors.append("manifest requires_logparse must be boolean")
    elif requires_logparse:
        if not isinstance(logparse_product, str) or not logparse_product.strip():
            errors.append("requires_logparse=true requires a non-empty logparse_product")
    elif logparse_product is not None:
        errors.append("requires_logparse=false requires logparse_product=null")


def _validate_body(
    body: str, manifest: dict[str, object] | None, errors: list[str]
) -> None:
    if f"生成器 `{GENERATOR_VERSION}`" not in body:
        errors.append(f"SKILL.md must identify generator version {GENERATOR_VERSION}")
    for result_type in REQUIRED_RESULT_TYPES:
        if f"`{result_type}`" not in body:
            errors.append(f"SKILL.md must describe result type {result_type}")
    for phrase in REQUIRED_PHRASES:
        if phrase not in body:
            errors.append(f"SKILL.md is missing required contract phrase: {phrase}")
    for forbidden in (
        "result_type = RESOLVED",
        "artifact_id = user-result",
        "允许直接调用 Router",
        "允许直接修改 Case",
        "允许扫描 inputs/",
    ):
        if forbidden in body:
            errors.append(f"SKILL.md contains a forbidden behavior: {forbidden}")
    content_types = _body_content_types(body)
    if len(content_types) != len(set(content_types)):
        errors.append("allowed ContentTypes must be byte-for-byte unique")
    for content_type in content_types:
        try:
            validate_content_type(content_type)
        except ValueError as exc:
            errors.append(f"invalid allowed ContentType {content_type!r}: {exc}")
    if manifest is not None:
        requires_logparse = manifest.get("requires_logparse")
        if requires_logparse is True and not content_types:
            errors.append("a logparse Skill must declare at least one allowed ContentType")
        if requires_logparse is True:
            for phrase in LOGPARSE_REQUIRED_PHRASES:
                if phrase not in body:
                    errors.append(f"logparse Skill is missing required phrase: {phrase}")
        if requires_logparse is False and content_types:
            errors.append("a non-logparse Skill must not declare log archive ContentTypes")
        if requires_logparse is False and "problem-locator-logparse parse-targets" in body:
            errors.append("a non-logparse Skill must not instruct a broker parse")
        if manifest.get("capability") == "service-takeover":
            for phrase in SERVICE_TAKEOVER_REQUIRED_PHRASES:
                if phrase not in body:
                    errors.append(
                        f"service-takeover Skill is missing scenario phrase: {phrase}"
                    )


def _body_content_types(body: str) -> list[str]:
    marker = "允许日志 Content-Type（逐字匹配"
    start = body.find(marker)
    if start < 0:
        return []
    end = body.find("\n## ", start)
    section = body[start : len(body) if end < 0 else end]
    return re.findall(r"(?m)^- `([^`]+)`$", section)


def _print(label: str, result: ValidationResult) -> None:
    print(f"{label}: {'OK' if result.ok else 'FAILED'}")
    for error in result.errors:
        print(f"- {error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"Validate a generated diagnosis Skill product ({GENERATOR_VERSION})."
    )
    parser.add_argument("skill_dir", type=Path)
    parser.add_argument("--expected-product-sha256")
    parser.add_argument("--result-zip", type=Path)
    args = parser.parse_args(argv)
    skill_result = validate_skill_dir(args.skill_dir)
    errors = list(skill_result.errors)
    if args.expected_product_sha256 is not None and not errors:
        actual = skill_product_sha256(args.skill_dir)
        if actual != args.expected_product_sha256:
            errors.append(
                "product SHA-256 mismatch: "
                f"expected={args.expected_product_sha256} actual={actual}"
            )
    skill_result = ValidationResult(tuple(errors))
    _print("skill", skill_result)
    zip_result = ValidationResult()
    if args.result_zip is not None:
        zip_result = validate_result_zip(args.result_zip)
        _print("result.zip", zip_result)
    return 0 if skill_result.ok and zip_result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
