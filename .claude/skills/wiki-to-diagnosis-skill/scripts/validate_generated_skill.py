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
    "verification_contract",
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
        r"(?ms)<!-- DIAGNOSIS_SKILL_MANIFEST_V3_BEGIN -->\s*```json\s*(.*?)\s*```\s*<!-- DIAGNOSIS_SKILL_MANIFEST_V3_END -->",
        markdown,
    )
    if len(matches) != 1:
        raise ValueError("SKILL.md must embed exactly one manifest v3 block")
    return json.loads(matches[0])


def _validate_verification_contract(
    value: object,
    requirements: list[object],
    logparse_plan: object,
    requires_logparse: object,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "event_extractors",
        "rules",
    }:
        return ["verification_contract field set is invalid"]
    if value.get("schema_version") != 1:
        errors.append("verification_contract schema_version must be 1")
    extractors = value.get("event_extractors")
    rules = value.get("rules")
    if not isinstance(extractors, list):
        return errors + ["event_extractors must be an array"]
    if not isinstance(rules, list) or not rules:
        return errors + ["verification rules must be a non-empty array"]
    if (requires_logparse is True) != bool(extractors):
        errors.append("logparse Skills require extractors; non-logparse Skills forbid them")
    anchors = {
        item.get("label")
        for item in logparse_plan.get("anchors", [])
        if isinstance(item, dict)
    } if isinstance(logparse_plan, dict) else set()
    extractor_by_id: dict[str, dict[str, object]] = {}
    for extractor in extractors:
        if not isinstance(extractor, dict) or set(extractor) != {
            "id",
            "anchor",
            "line_pattern",
            "timestamp_group",
            "timestamp_format",
            "field_groups",
            "match_cardinality",
        }:
            errors.append("event extractor field set is invalid")
            continue
        extractor_id = extractor.get("id")
        pattern = extractor.get("line_pattern")
        fields = extractor.get("field_groups")
        timestamp_group = extractor.get("timestamp_group")
        try:
            compiled = re.compile(pattern) if isinstance(pattern, str) else None
        except re.error:
            compiled = None
        valid_fields = (
            isinstance(fields, list)
            and all(
                isinstance(item, str)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item) is not None
                for item in fields
            )
            and len(fields) == len(set(fields))
        )
        if (
            not isinstance(extractor_id, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", extractor_id) is None
            or extractor_id in extractor_by_id
        ):
            errors.append("event extractor ids must be unique lower-snake names")
            continue
        if extractor.get("anchor") not in anchors:
            errors.append("event extractor anchor must name a logparse anchor")
        if (
            compiled is None
            or not pattern.startswith("^")
            or not pattern.endswith("$")
            or "\n" in pattern
            or "\r" in pattern
            or not valid_fields
            or not isinstance(timestamp_group, str)
            or set(compiled.groupindex) != {timestamp_group, *fields}
        ):
            errors.append("event extractor must be a single-line named-group regex")
        if extractor.get("timestamp_format") != "RFC3339_MILLIS_UTC":
            errors.append("event extractor timestamp_format is invalid")
        if extractor.get("match_cardinality") != "EXACTLY_ONE":
            errors.append("event extractor match_cardinality is invalid")
        extractor_by_id[extractor_id] = extractor
    requirement_by_name = {
        item.get("name"): item for item in requirements if isinstance(item, dict)
    }
    rule_ids: set[str] = set()
    has_semantic = False
    kinds = {
        "EVENT_PRESENT",
        "EVENT_TIME_WINDOW",
        "FACT_FIELD_EQUALS",
        "ROLE_COVERAGE",
        "CROSS_ROLE_CORRELATION",
        "EVENT_ORDER",
        "SEMANTIC_CAUSALITY",
    }
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {
            "id",
            "kind",
            "description",
            "depends_on",
            "remediation_requirements",
            "parameters",
        }:
            errors.append("verification rule field set is invalid")
            continue
        rule_id = rule.get("id")
        dependencies = rule.get("depends_on")
        remediation = rule.get("remediation_requirements")
        if (
            not isinstance(rule_id, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", rule_id) is None
            or rule_id in rule_ids
            or rule.get("kind") not in kinds
            or not isinstance(rule.get("description"), str)
            or not rule["description"]
            or "\n" in rule["description"]
            or "\r" in rule["description"]
        ):
            errors.append("verification rule identity is invalid")
            continue
        if (
            not isinstance(dependencies, list)
            or len(dependencies) != len(set(dependencies))
            or not set(dependencies) <= rule_ids
        ):
            errors.append("verification rule dependencies must name preceding rules")
        if not isinstance(remediation, list) or len(remediation) != len(set(remediation)) or any(
            name not in requirement_by_name
            or requirement_by_name[name].get("supplement_policy") != "MISSING_ONLY"
            for name in remediation
        ):
            errors.append("rule remediation must name MISSING_ONLY requirements")
        parameters = rule.get("parameters")
        if not isinstance(parameters, dict):
            errors.append("verification rule parameters must be an object")
        elif rule.get("kind") == "EVENT_PRESENT":
            if set(parameters) != {"event"}:
                errors.append("EVENT_PRESENT parameters are invalid")
        elif rule.get("kind") == "EVENT_TIME_WINDOW":
            if set(parameters) != {
                "event",
                "reference",
                "before_ms",
                "after_ms",
                "lower_bound",
                "upper_bound",
            } or type(parameters.get("before_ms")) is not int or type(parameters.get("after_ms")) is not int or parameters.get("before_ms", -1) < 0 or parameters.get("after_ms", -1) < 0 or parameters.get("lower_bound") not in {"INCLUSIVE", "EXCLUSIVE"} or parameters.get("upper_bound") not in {"INCLUSIVE", "EXCLUSIVE"}:
                errors.append("event time window must declare both ranges and boundaries")
        elif rule.get("kind") == "FACT_FIELD_EQUALS":
            if set(parameters) != {"event", "field", "fact_name"}:
                errors.append("FACT_FIELD_EQUALS parameters are invalid")
        elif rule.get("kind") == "ROLE_COVERAGE":
            coverage = parameters.get("coverage")
            if set(parameters) != {"coverage"} or not isinstance(coverage, list) or not coverage or any(
                not isinstance(item, dict) or set(item) != {"role", "event"}
                for item in coverage
            ):
                errors.append("ROLE_COVERAGE parameters are invalid")
        elif rule.get("kind") == "CROSS_ROLE_CORRELATION":
            members = parameters.get("members")
            if set(parameters) != {"members"} or not isinstance(members, list) or len(members) < 2 or any(
                not isinstance(item, dict) or set(item) != {"event", "field"}
                for item in members
            ):
                errors.append("CROSS_ROLE_CORRELATION parameters are invalid")
        elif rule.get("kind") == "EVENT_ORDER":
            if set(parameters) != {"before_event", "after_event", "allow_equal"} or type(parameters.get("allow_equal")) is not bool:
                errors.append("EVENT_ORDER parameters are invalid")
        elif rule.get("kind") == "SEMANTIC_CAUSALITY":
            if set(parameters) != {"assertion", "evidence_events"} or not isinstance(parameters.get("assertion"), str) or not isinstance(parameters.get("evidence_events"), list):
                errors.append("SEMANTIC_CAUSALITY parameters are invalid")
        if isinstance(parameters, dict):
            event_ids: list[object] = []
            kind = rule.get("kind")
            if kind in {"EVENT_PRESENT", "EVENT_TIME_WINDOW", "FACT_FIELD_EQUALS"}:
                event_ids.append(parameters.get("event"))
            elif kind == "ROLE_COVERAGE" and isinstance(parameters.get("coverage"), list):
                event_ids.extend(
                    item.get("event") for item in parameters["coverage"] if isinstance(item, dict)
                )
            elif kind == "CROSS_ROLE_CORRELATION" and isinstance(parameters.get("members"), list):
                event_ids.extend(
                    item.get("event") for item in parameters["members"] if isinstance(item, dict)
                )
            elif kind == "EVENT_ORDER":
                event_ids.extend((parameters.get("before_event"), parameters.get("after_event")))
            elif kind == "SEMANTIC_CAUSALITY" and isinstance(parameters.get("evidence_events"), list):
                event_ids.extend(parameters["evidence_events"])
            if any(event_id not in extractor_by_id for event_id in event_ids):
                errors.append("verification rule names an unknown event")
        if rule.get("kind") == "SEMANTIC_CAUSALITY":
            has_semantic = True
        rule_ids.add(rule_id)
    if not has_semantic:
        errors.append("verification_contract requires SEMANTIC_CAUSALITY")
    return errors


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
    if manifest.get("schema_version") != 3:
        errors.append("manifest schema_version must be 3")
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
    if any(
        item.get("supplement_policy") not in {"NONE", "MISSING_ONLY"}
        for item in requirements
        if isinstance(item, dict)
    ):
        errors.append("requirements must declare supplement_policy")
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
    errors.extend(
        _validate_verification_contract(
            manifest.get("verification_contract"),
            requirements,
            manifest.get("logparse_plan"),
            requires_logparse,
        )
    )
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
