"""Trusted stdin adapter: compile GenerationBlueprint IR and deeply validate v6 output."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import skill_generation_rule_ir as rule_ir


MAX_IR_CANONICAL_BYTES = 48 * 1024
GENERATOR_RELATIVE = Path(
    ".claude/skills/wiki-to-diagnosis-skill/scripts/generate_diagnosis_skill.py"
)


class _RuleIrDiagnosticError(ValueError):
    def __init__(self, phase: str, constraint_id: str) -> None:
        super().__init__(constraint_id)
        self.phase = phase
        self.constraint_id = constraint_id


def _blueprint_constraint(error: rule_ir.BlueprintError) -> str:
    message = str(error)
    checks = (
        ("blueprint must contain only finite JSON values", "FINITE_JSON"),
        ("blueprint.spec keys are invalid", "SPEC_KEYS"),
        ("keys are invalid", "EXACT_KEYS"),
        ("must be an object", "OBJECT_REQUIRED"),
        ("keys must be strings", "STRING_KEYS_REQUIRED"),
        ("must be an array", "ARRAY_REQUIRED"),
        ("must be non-empty text without template residue", "NONEMPTY_TEXT_REQUIRED"),
        ("must be a safe identifier", "SAFE_IDENTIFIER_REQUIRED"),
        ("must be an integer >=", "INTEGER_RANGE"),
        ("contains unresolved template text", "TEMPLATE_RESIDUE"),
        ("resolution_status is invalid", "PATH_RESOLUTION_STATUS"),
        ("position ordinals must be consecutive", "POSITION_ORDINALS"),
        ("field mappings must be distinct", "POSITION_FIELDS_DISTINCT"),
        ("unknown family kind", "FAMILY_KIND"),
        ("unknown family version", "FAMILY_VERSION"),
        ("requires at least two positions", "FAMILY_POSITION_COUNT"),
        ("position names must be unique", "POSITION_NAMES_UNIQUE"),
        ("position events must be unique", "POSITION_EVENTS_UNIQUE"),
        ("base semantic dependencies must be unique", "BASE_DEPENDENCIES_UNIQUE"),
        (" status must be ", "FAMILY_PATH_STATUS"),
        ("duplicate rule ID", "RULE_ID_DUPLICATE"),
        (" dependencies are invalid", "RULE_DEPENDENCY_SHAPE"),
        ("missing or non-topological dependencies", "RULE_DEPENDENCY_TOPOLOGY"),
        ("duplicate terminal path ID", "PATH_ID_DUPLICATE"),
        ("compiled terminal condition term is invalid", "TERMINAL_TERM_SHAPE"),
        ("references unknown rule", "TERMINAL_RULE_REFERENCE"),
        ("blueprint schema_version is unsupported", "BLUEPRINT_SCHEMA_VERSION"),
        ("blueprint compiler identity does not match", "COMPILER_IDENTITY"),
        ("verification schema_version must be 2", "VERIFICATION_SCHEMA_VERSION"),
        ("compiled counts differ from expected counts", "EXPECTED_COUNTS"),
    )
    for fragment, constraint_id in checks:
        if fragment in message:
            return constraint_id
    return "COMPILER_PROCESS"


def _diagnostic_bytes(error: _RuleIrDiagnosticError) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "phase": error.phase,
                "constraint_id": error.constraint_id,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )


def _load_generator(source_root: Path) -> ModuleType:
    script = source_root / GENERATOR_RELATIVE
    if not script.is_file() or script.is_symlink():
        raise ValueError("generator script is unavailable")
    source_path = source_root / "src"
    if not source_path.is_dir() or source_path.is_symlink():
        raise ValueError("source package root is unavailable")
    sys.path.insert(0, os.fspath(source_path))
    module_name = "skill_generation_rule_ir_deep_validator"
    specification = importlib.util.spec_from_file_location(module_name, script)
    if specification is None or specification.loader is None:
        raise ValueError("generator module cannot be loaded")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    specification.loader.exec_module(module)
    return module


def compile_and_validate(raw: bytes, source_root: Path) -> bytes:
    if not raw or len(raw) > MAX_IR_CANONICAL_BYTES:
        raise _RuleIrDiagnosticError("ADAPTER", "IR_SIZE_INVALID")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _RuleIrDiagnosticError("ADAPTER", "IR_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise _RuleIrDiagnosticError("ADAPTER", "IR_ROOT_OBJECT")
    try:
        canonical_ir = rule_ir.canonical_json_bytes(value)
    except rule_ir.BlueprintError as exc:
        raise _RuleIrDiagnosticError("COMPILER", _blueprint_constraint(exc)) from exc
    if raw != canonical_ir or len(canonical_ir) > MAX_IR_CANONICAL_BYTES:
        raise _RuleIrDiagnosticError("ADAPTER", "IR_CANONICAL_BOUNDED")

    try:
        compilation = rule_ir.compile_blueprint(value)
    except rule_ir.BlueprintError as exc:
        raise _RuleIrDiagnosticError("COMPILER", _blueprint_constraint(exc)) from exc
    except Exception as exc:
        raise _RuleIrDiagnosticError("COMPILER", "COMPILER_PROCESS") from exc
    expanded = json.loads(compilation.spec_bytes)
    try:
        generator = _load_generator(source_root)
    except Exception as exc:
        raise _RuleIrDiagnosticError("ADAPTER", "GENERATOR_LOAD") from exc
    try:
        generator.GenerationSpec.from_mapping(expanded)
        if generator.canonical_json_bytes(expanded) != compilation.spec_bytes:
            raise ValueError("canonical output mismatch")
    except Exception as exc:
        raise _RuleIrDiagnosticError("DEEP_VALIDATOR", "DEEP_VALIDATOR_REJECTED") from exc

    envelope = {
        "schema_version": 1,
        "compiler": {
            "id": rule_ir.COMPILER_ID,
            "version": rule_ir.COMPILER_VERSION,
            "blueprint_schema_version": rule_ir.BLUEPRINT_SCHEMA_VERSION,
            "family_kind": rule_ir.FAMILY_KIND,
            "family_version": rule_ir.FAMILY_VERSION,
        },
        "ir": {
            "size_bytes": len(canonical_ir),
            "sha256": hashlib.sha256(canonical_ir).hexdigest(),
        },
        "output": {
            "size_bytes": len(compilation.spec_bytes),
            "sha256": hashlib.sha256(compilation.spec_bytes).hexdigest(),
        },
        "spec": expanded,
    }
    return rule_ir.canonical_json_bytes(envelope)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    arguments = parser.parse_args()
    try:
        source_root = Path(arguments.source_root).resolve(strict=True)
        if not source_root.is_dir():
            raise ValueError("source root must be a directory")
        output = compile_and_validate(sys.stdin.buffer.read(), source_root)
    except _RuleIrDiagnosticError as exc:
        sys.stderr.buffer.write(_diagnostic_bytes(exc))
        return 1
    except Exception:
        sys.stderr.buffer.write(
            _diagnostic_bytes(_RuleIrDiagnosticError("ADAPTER", "COMPILER_PROCESS"))
        )
        return 1
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
