from __future__ import annotations

import ast
import copy
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[4]
PROTOTYPE_ROOT = ROOT / "prototypes" / "generation_blueprint"
PRODUCTION_ROOT = ROOT / "tools" / "test-flow" / "runtime-support"
BLUEPRINT_CANDIDATES = tuple(sorted((PROTOTYPE_ROOT / "fixtures").glob("*_blueprint.json")))
assert len(BLUEPRINT_CANDIDATES) == 1
BLUEPRINT_PATH = BLUEPRINT_CANDIDATES[0]
CASE_CANDIDATES = tuple(sorted((ROOT / "tests" / "cases" / "release").glob("*/case.json")))
assert len(CASE_CANDIDATES) == 1
CASE_ROOT = CASE_CANDIDATES[0].parent
APPROVED_SPEC_PATH = CASE_ROOT / "input" / "generation-spec.json"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compiler = _load_module(
    "production_skill_generation_rule_ir",
    PRODUCTION_ROOT / "skill_generation_rule_ir.py",
)


def _blueprint() -> dict[str, object]:
    return json.loads(BLUEPRINT_PATH.read_bytes())


def _compiled_mapping() -> dict[str, object]:
    return json.loads(compiler.compile_blueprint(_blueprint()).spec_bytes)


def test_blueprint_compiles_deterministically_with_bound_identity_and_counts(
    record_property: pytest.RecordProperty,
) -> None:
    blueprint = _blueprint()
    canonical_ir = compiler.canonical_json_bytes(blueprint)
    first = compiler.compile_blueprint(blueprint)
    second = compiler.compile_blueprint(copy.deepcopy(blueprint))

    assert first == second
    assert first.compiler_id == compiler.COMPILER_ID
    assert first.compiler_version == compiler.COMPILER_VERSION
    assert first.spec_bytes.endswith(b"\n")
    assert first.spec_bytes == compiler.canonical_json_bytes(
        json.loads(first.spec_bytes)
    )
    assert len(canonical_ir) == 39_514
    assert len(canonical_ir) <= 48 * 1024
    assert len(canonical_ir) < len(first.spec_bytes)
    assert (
        first.literal_rule_count,
        first.mechanical_rule_count,
        first.semantic_rule_count,
        first.expanded_family_rule_count,
        first.total_rule_count,
        first.expanded_family_terminal_path_count,
        first.total_terminal_path_count,
    ) == (21, 105, 39, 144, 165, 3, 9)
    compiled = json.loads(first.spec_bytes)
    assert len(compiled["verification_contract"]["observation_policies"]) == 2
    assert len(compiled["verification_contract"]["event_extractors"]) == 10

    record_property("rule_ir_compiler_id", first.compiler_id)
    record_property("rule_ir_compiler_version", first.compiler_version)
    record_property("rule_ir_blueprint_sha256", first.blueprint_sha256)
    record_property("rule_ir_spec_sha256", first.spec_sha256)
    record_property("rule_ir_canonical_bytes", str(len(canonical_ir)))
    record_property("rule_ir_compiled_bytes", str(len(first.spec_bytes)))


def test_compiled_spec_matches_the_approved_product_only_as_a_post_hoc_oracle() -> None:
    result = compiler.compile_blueprint(_blueprint())
    approved = json.loads(APPROVED_SPEC_PATH.read_bytes())
    assert json.loads(result.spec_bytes) == approved
    assert result.spec_bytes == compiler.canonical_json_bytes(approved)


def test_optional_logparse_product_is_source_driven_not_a_required_ir_key() -> None:
    blueprint = _blueprint()
    assert blueprint["spec"].pop("logparse_product")
    compiled = json.loads(compiler.compile_blueprint(blueprint).spec_bytes)
    assert "logparse_product" not in compiled


def test_compiled_spec_passes_existing_loader_and_verification_validator() -> None:
    generator = _load_module(
        "prototype_generation_blueprint_generator_contract",
        ROOT
        / ".claude"
        / "skills"
        / "wiki-to-diagnosis-skill"
        / "scripts"
        / "generate_diagnosis_skill.py",
    )
    compiled = _compiled_mapping()
    loaded = generator.GenerationSpec.from_mapping(compiled)
    contract = compiled["verification_contract"]
    normalized = generator.validate_verification_contract(
        contract,
        requirements=loaded.manifest_requirements(),
        anchor_labels={item["label"] for item in loaded.logparse_plan["anchors"]},
        role_labels={item.label for item in loaded.roles},
        requires_logparse=loaded.requires_logparse,
    )
    assert normalized == contract


def test_trusted_adapter_binds_ir_compiler_and_deep_validated_output_seals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(PRODUCTION_ROOT))
    adapter = _load_module(
        "production_skill_generation_rule_ir_adapter",
        PRODUCTION_ROOT / "compile_skill_generation_rule_ir.py",
    )
    canonical_ir = compiler.canonical_json_bytes(_blueprint())
    envelope = json.loads(adapter.compile_and_validate(canonical_ir, ROOT))
    compiled = compiler.compile_blueprint(_blueprint())

    assert envelope["schema_version"] == 1
    assert envelope["compiler"] == {
        "id": compiler.COMPILER_ID,
        "version": compiler.COMPILER_VERSION,
        "blueprint_schema_version": compiler.BLUEPRINT_SCHEMA_VERSION,
        "family_kind": compiler.FAMILY_KIND,
        "family_version": compiler.FAMILY_VERSION,
    }
    assert envelope["ir"] == {
        "size_bytes": len(canonical_ir),
        "sha256": compiled.blueprint_sha256,
    }
    assert envelope["output"] == {
        "size_bytes": len(compiled.spec_bytes),
        "sha256": compiled.spec_sha256,
    }
    assert compiler.canonical_json_bytes(envelope["spec"]) == compiled.spec_bytes
    with pytest.raises(ValueError, match="IR_CANONICAL_BOUNDED"):
        adapter.compile_and_validate(canonical_ir.rstrip(b"\n"), ROOT)


def test_compiled_spec_passes_existing_business_and_nine_scenario_oracles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_tests = _load_module(
        "prototype_generation_blueprint_release_oracles",
        ROOT
        / "tests"
        / "deterministic"
        / "unit"
        / "runtime"
        / "test_release_case_verification.py",
    )
    compiled = _compiled_mapping()
    original_json = release_tests._json

    def compiled_generation_spec(path: Path) -> dict[str, object]:
        if path.resolve() == APPROVED_SPEC_PATH.resolve():
            return copy.deepcopy(compiled)
        return original_json(path)

    monkeypatch.setattr(release_tests, "_json", compiled_generation_spec)
    release_tests.test_release_case_scenarios_select_the_reviewed_terminal_paths(
        CASE_ROOT
    )
    release_tests.test_release_case_ordered_selector_families_cover_each_member_position(
        CASE_ROOT
    )
    release_tests.test_release_case_policy_projection_matches_the_approved_contract(
        CASE_ROOT
    )
    descriptor = original_json(CASE_ROOT / "case.json")
    assert len(descriptor["scenarios"]) == 9


def test_blueprint_contains_literals_but_not_expanded_family_rule_objects() -> None:
    blueprint = _blueprint()
    verification = blueprint["verification"]
    literal_segments = verification["literal_rule_segments"]
    assert sum(len(items) for items in literal_segments.values()) == 21
    assert "rules" not in verification["ordered_interval_family"]
    assert sum(
        len(items)
        for items in verification["literal_terminal_segments"].values()
    ) == 6
    assert "condition" not in verification["ordered_interval_family"]["terminal_paths"]["complete"]


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (
            lambda value: value["verification"]["ordered_interval_family"].update(
                kind="UNKNOWN_FAMILY"
            ),
            "unknown family kind",
        ),
        (
            lambda value: value["verification"]["ordered_interval_family"].update(
                version=2
            ),
            "unknown family version",
        ),
        (
            lambda value: value["verification"]["ordered_interval_family"]["positions"][1].update(
                name=value["verification"]["ordered_interval_family"]["positions"][0]["name"]
            ),
            "position names must be unique",
        ),
        (
            lambda value: value["verification"]["literal_rule_segments"]["prefix"][1].update(
                id=value["verification"]["literal_rule_segments"]["prefix"][0]["id"]
            ),
            "duplicate rule ID",
        ),
        (
            lambda value: value["verification"]["ordered_interval_family"]["positions"][1].update(
                ordinal=3
            ),
            "position ordinals",
        ),
        (
            lambda value: value["verification"]["ordered_interval_family"]["positions"][0].update(
                cost_field=value["verification"]["ordered_interval_family"]["positions"][0]["end_field"]
            ),
            "field mappings must be distinct",
        ),
        (
            lambda value: value["verification"]["ordered_interval_family"]["shared"][
                "base_semantic_dependency_rule_ids"
            ].append("missing_dependency"),
            "missing or non-topological dependencies",
        ),
        (
            lambda value: value["verification"]["ordered_interval_family"]["texts"].update(
                gap_assertion="{{unresolved}}"
            ),
            "unresolved template",
        ),
        (
            lambda value: value["verification"]["expected_counts"].update(
                mechanical_rules=104
            ),
            "compiled counts differ",
        ),
        (
            lambda value: value["verification"]["ordered_interval_family"].update(
                unexpected=True
            ),
            "keys are invalid",
        ),
    ],
)
def test_invalid_blueprints_fail_closed_without_partial_output(
    mutate: object,
    expected: str,
) -> None:
    blueprint = _blueprint()
    mutate(blueprint)
    before = copy.deepcopy(blueprint)
    with pytest.raises(compiler.BlueprintError, match=expected):
        compiler.compile_blueprint(blueprint)
    assert blueprint == before


def test_compiler_source_has_no_case_answers_or_impure_capabilities() -> None:
    source = (PRODUCTION_ROOT / "skill_generation_rule_ir.py").read_text(
        encoding="utf-8"
    )
    folded = source.casefold()
    assert "q_first" not in folded
    assert "rpc" not in folded
    tree = ast.parse(source)
    forbidden_imports = {
        "pathlib",
        "random",
        "secrets",
        "socket",
        "subprocess",
        "time",
        "urllib",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not ({alias.name.split(".")[0] for alias in node.names} & forbidden_imports)
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or node.module.split(".")[0] not in forbidden_imports
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"eval", "exec", "open", "compile", "__import__"}


def test_compiler_runtime_and_mechanical_payload_reduction(
    record_property: pytest.RecordProperty,
) -> None:
    blueprint = _blueprint()
    started = time.perf_counter()
    results = [compiler.compile_blueprint(copy.deepcopy(blueprint)) for _ in range(20)]
    elapsed = time.perf_counter() - started
    assert len({item.spec_sha256 for item in results}) == 1
    assert elapsed < 5.0
    compiled_bytes = len(results[0].spec_bytes)
    ir_bytes = len(compiler.canonical_json_bytes(blueprint))
    reduction_basis_points = ((compiled_bytes - ir_bytes) * 10_000) // compiled_bytes
    assert reduction_basis_points >= 7_000
    record_property("rule_ir_compile_iterations", "20")
    record_property("rule_ir_compile_elapsed_seconds", f"{elapsed:.6f}")
    record_property("rule_ir_payload_reduction_basis_points", str(reduction_basis_points))


def test_compiler_adapter_emits_only_a_fixed_content_free_constraint() -> None:
    blueprint = _blueprint()
    blueprint["verification"]["ordered_interval_family"]["positions"][1][
        "ordinal"
    ] = 1
    raw = compiler.canonical_json_bytes(blueprint)
    result = subprocess.run(
        [
            sys.executable,
            os.fspath(PRODUCTION_ROOT / "compile_skill_generation_rule_ir.py"),
            "--source-root",
            os.fspath(ROOT),
        ],
        input=raw,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert json.loads(result.stderr) == {
        "schema_version": 1,
        "phase": "COMPILER",
        "constraint_id": "POSITION_ORDINALS",
    }
    assert b"positions[1]" not in result.stderr
    assert b"ordinal" not in result.stderr
