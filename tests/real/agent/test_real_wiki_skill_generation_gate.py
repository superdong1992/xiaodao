from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from problem_locator.contracts import (
    ExecutionLogSinks,
    JOB_STDOUT_STDERR_BYTES,
    JobType,
    default_resource_limits,
)
from problem_locator.runtime.agent_backend import AgentBackend, BackendExecutionLimits
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.methods_skill import (
    ResolvedSpecializedSkillV1,
    load_methods_package,
    load_specialized_skill_registration,
)


ROOT = Path(__file__).resolve().parents[3]
META_SKILL_ROOT = ROOT / ".agents/skills/wiki-to-diagnosis-skill"
VALIDATOR_PATH = META_SKILL_ROOT / "scripts/validate_generated_skill.py"
SCENARIO_AUDIT_FILE = "scenario-evaluation-audit.json"
SOURCE_WIKI_IDENTITY_FILE = "source-wiki-identity.json"
NON_CANONICAL_EVENT_NAMES = (
    "API_COMPLETE",
    "DEADLOOP_DETECTED",
    "LATE_RESPONSE",
    "QUEUE_HISTORY",
)


class _Signal:
    reason = None

    def __init__(self) -> None:
        self._event = threading.Event()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout_seconds: float | None) -> bool:
        return self._event.wait(timeout_seconds)


class _Sink:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, chunk: bytes) -> None:
        assert not self.closed
        self.data.extend(chunk)

    def flush(self) -> None:
        assert not self.closed

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def isolated_agent_workspace_root() -> Path:
    with tempfile.TemporaryDirectory(prefix="xiaodao-methods-skill-") as temporary:
        root = Path(temporary).resolve()
        repository = ROOT.resolve()
        assert root != repository
        assert repository not in root.parents
        assert root not in repository.parents
        yield root


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_release_methods_skill_validator", VALIDATOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _generation_prompt(
    *,
    requested_skill_name: str,
    canonical_marker_checklist: list[str],
) -> str:
    assert canonical_marker_checklist
    checklist_json = json.dumps(
        canonical_marker_checklist,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    shorthand_json = json.dumps(
        list(NON_CANONICAL_EVENT_NAMES),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"""Use the wiki-to-diagnosis-skill Skill to convert inputs/wiki.md into one Methods Skill named {requested_skill_name}.

Your first action must call the Skill tool with exactly {{"skill":"wiki-to-diagnosis-skill"}}. After that call succeeds, read inputs/wiki.md and runtime/source-wiki-identity.json in full. The closed source identity v2 was generated mechanically from the exact Wiki bytes before this invocation. Copy its `sha256` value verbatim into methods.json as `source_wiki_sha256`; use its `log_templates` as the complete, ordered, duplicate-preserving checklist for `references/source-log-templates.md`; never calculate, guess, normalize, reorder, deduplicate, or replace those values. The Wiki remains the only source of business meaning. From the Skill result, take the exact absolute `Base directory for this skill:` and read only its linked references/output-contract.md in full. Do not read clarifications, repository files, registration metadata, tests, oracles, validators, or any other path. Do not call Bash, Edit, Glob, Grep, or any tool other than the available Skill, Read, and Write tools. Do not call any tool with missing or invalid input.

The Gate mechanically derived this ordered canonical marker checklist from source identity `log_templates` with the same function used by the canonical validator: {checklist_json}. Every item in methods.json `evidence_markers` must be copied byte-for-byte from this checklist. The checklist does not assign markers to methods and adds no business meaning; use the authored Wiki to choose which listed markers belong to each cause. Do not invent, shorten, or extend a marker. These bare event names are shorthand, not valid markers unless the exact whole string itself appears in the checklist: {shorthand_json}.

Generate the complete package directly under output/{requested_skill_name}. Its files must be exactly output/{requested_skill_name}/SKILL.md, output/{requested_skill_name}/methods.json, and the output-contract references, including the mandatory output/{requested_skill_name}/references/source-log-templates.md. Put that fixed reference first in methods.json `shared_references` and never use it as a method reference. Do not emit GenerationSpec, diagnosis-skill.json, registration metadata, copied Wiki, README, scripts, or tests. Use exactly one successful Write call per final package file, with both file_path and complete non-empty content in the same call. Finish every required Read before the first Write; before writing, check every source identity `log_templates` item against the complete fixed-reference content one-for-one and in order. After writing starts, perform only the contiguous sequence of package Write calls. Never overwrite a path, never write outside this one package, and stop after the final Write succeeds.

Preserve the authored Wiki literally where the loaded output contract requires exact log templates, markers, fields, thresholds, units, safety meaning, and observation boundaries. Do not add author experience or infer defaults. The Gate will run the meta Skill's canonical validator after generation; do not attempt to read or invoke it yourself. Do not include package JSON or Markdown in the final response.
"""


def _release_case_root() -> Path:
    configured = Path(os.environ["S08_RELEASE_CASES_ROOT"]).resolve()
    candidates = sorted(path.parent for path in configured.glob("*/case.json"))
    assert len(candidates) == 1
    return candidates[0]


def _load_object(path: Path) -> dict[str, Any]:
    assert path.is_file() and not path.is_symlink()
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_new(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(_canonical_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


def _scenario_audit_path() -> Path:
    configured = os.environ.get("S08_REAL_SKILL_GENERATION_AUDIT_PATH")
    assert configured, "S08_REAL_SKILL_GENERATION_AUDIT_PATH is required"
    destination = Path(configured).resolve()
    assert destination.name == SCENARIO_AUDIT_FILE
    assert destination.parent.is_dir() and not destination.exists()
    return destination


def _package_files(package_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(package_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        payload = path.read_bytes()
        records.append(
            {
                "path": path.relative_to(package_root).as_posix(),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return records


def _gate_only_oracle_audit(
    *,
    package_root: Path,
    manifest: Any,
    oracle: dict[str, Any],
) -> dict[str, Any]:
    assert oracle["schema_version"] == 2
    assert oracle["oracle_visibility"] == "GATE_ONLY"
    expected = oracle["expected_package"]
    mismatches: list[str] = []

    def expect_equal(label: str, actual: object, wanted: object) -> None:
        if actual != wanted:
            mismatches.append(label)

    expect_equal("skill_name", manifest.skill_name, expected["skill_name"])
    expect_equal(
        "source_wiki_sha256",
        manifest.source_wiki_sha256,
        expected["source_wiki_sha256"],
    )
    expect_equal(
        "required_user_inputs",
        list(manifest.required_user_inputs),
        expected["required_user_inputs"],
    )
    expect_equal(
        "required_artifacts",
        list(manifest.required_artifacts),
        expected["required_artifacts"],
    )
    expect_equal(
        "log_derived_fields",
        list(manifest.log_derived_fields),
        expected["required_log_derived_fields"],
    )

    generated_methods = [
        {
            "method_id": method.id,
            "markers": frozenset(method.evidence_markers),
        }
        for method in manifest.methods
    ]
    expected_marker_sets = expected["method_marker_sets"]
    if len(generated_methods) != len(expected_marker_sets):
        mismatches.append("method_count")

    method_marker_coverage: list[dict[str, Any]] = []
    mapped_method_ids: list[str] = []
    for required in expected_marker_sets:
        markers = frozenset(required["all_markers"])
        matched_method_ids = [
            generated["method_id"]
            for generated in generated_methods
            if generated["markers"] == markers
        ]
        covered = len(matched_method_ids) == 1
        method_marker_coverage.append(
            {
                "semantic_id": required["semantic_id"],
                "covered": covered,
                "exact_match_count": len(matched_method_ids),
                "matched_method_ids": matched_method_ids,
            }
        )
        if not covered:
            mismatches.append(f"method_marker_set:{required['semantic_id']}")
        else:
            mapped_method_ids.append(matched_method_ids[0])

    generated_method_ids = [item["method_id"] for item in generated_methods]
    mapped_method_id_set = set(mapped_method_ids)
    method_mapping_is_bijective = (
        len(mapped_method_ids) == len(expected_marker_sets)
        and len(mapped_method_id_set) == len(mapped_method_ids)
        and mapped_method_id_set == set(generated_method_ids)
    )
    if not method_mapping_is_bijective:
        mismatches.append("method_marker_mapping")
    unmapped_method_ids = [
        method_id
        for method_id in generated_method_ids
        if method_id not in mapped_method_id_set
    ]

    shared_text = "\n".join(
        (package_root / relative).read_text(encoding="utf-8")
        for relative in manifest.shared_references
    )
    missing_shared_markers = [
        marker
        for marker in expected["required_shared_markers"]
        if marker not in shared_text
    ]
    if missing_shared_markers:
        mismatches.append("required_shared_markers")

    package_paths = {
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*")
        if path.is_file()
    }
    forbidden_paths = [
        forbidden
        for forbidden in expected["forbidden_paths"]
        if forbidden in package_paths
        or any(path.endswith(f"/{forbidden}") for path in package_paths)
    ]
    if forbidden_paths:
        mismatches.append("forbidden_paths")

    product_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(package_root.rglob("*"))
        if path.is_file() and path.suffix in {".md", ".json"}
    )
    forbidden_note_markers = [
        marker
        for marker in oracle["author_note_markers_forbidden_in_product"]
        if marker in product_text
    ]
    if forbidden_note_markers:
        mismatches.append("author_note_markers")
    leaked_canary_count = sum(
        marker in product_text for marker in oracle["business_canaries"]
    )
    if leaked_canary_count:
        mismatches.append("gate_only_canary_leak")

    return {
        "status": "PASS" if not mismatches else "FAIL",
        "mismatches": mismatches,
        "expected_method_count": len(expected_marker_sets),
        "generated_method_count": len(generated_methods),
        "method_mapping_is_bijective": method_mapping_is_bijective,
        "unmapped_method_ids": unmapped_method_ids,
        "method_marker_coverage": method_marker_coverage,
        "missing_shared_marker_count": len(missing_shared_markers),
        "forbidden_path_count": len(forbidden_paths),
        "forbidden_note_marker_count": len(forbidden_note_markers),
        "gate_only_canary_leak_count": leaked_canary_count,
    }


def _install_product_registration(
    *,
    case_root: Path,
    descriptor: dict[str, Any],
    generated_package: Path,
    output_root: Path,
) -> ResolvedSpecializedSkillV1:
    assert not output_root.exists()
    registration_template = case_root / descriptor["registration_template"]
    registration = _load_object(registration_template)
    registration_id = registration["registration_id"]
    skill_name = registration["package"]["skill_name"]
    registration_root = output_root / registration_id
    package_root = registration_root / "package" / skill_name
    package_root.parent.mkdir(parents=True, mode=0o700)
    shutil.copytree(generated_package, package_root, symlinks=False)
    template_destination = registration_root / "registration-template.json"
    with template_destination.open("xb") as stream:
        stream.write(registration_template.read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    return load_specialized_skill_registration(registration_root)


def _write_generated_skill_receipt(
    *,
    case_id: str,
    resolved: ResolvedSpecializedSkillV1,
) -> None:
    configured = os.environ.get("S08_REAL_SKILL_GENERATION_RECEIPT_PATH")
    assert configured, "S08_REAL_SKILL_GENERATION_RECEIPT_PATH is required"
    destination = Path(configured).resolve()
    assert destination.parent.is_dir() and not destination.exists()
    _write_new(
        destination,
        {
            "schema_version": 1,
            "status": "PASS",
            "case_id": case_id,
            "registration_id": resolved.registration_id,
            "runtime_ref_id": f"diagnosis-skill/{resolved.registration_id}",
            "version": resolved.registration.version,
            "skill_name": resolved.methods.skill_name,
            "source_wiki_sha256": resolved.methods.source_wiki_sha256,
            "registration_sha256": resolved.registration_sha256,
            "package_tree_sha256": resolved.package_tree_sha256,
            "combined_sha256": resolved.combined_sha256,
        },
    )


def _gate_oracle_test_baseline(
    tmp_path: Path,
) -> tuple[Path, Any, dict[str, Any]]:
    case_root = ROOT / "tests/cases/release/rpc-timeout-anonymized"
    oracle = _load_object(case_root / "oracle.json")
    expected = oracle["expected_package"]
    validator = _load_validator()
    source_identity = validator.build_source_wiki_identity(
        (case_root / "input/wiki.md").read_bytes(),
        "inputs/wiki.md",
    )
    package_root = tmp_path / expected["skill_name"]
    references_root = package_root / "references"
    references_root.mkdir(parents=True)
    (package_root / "SKILL.md").write_text(
        f"""---
name: {expected['skill_name']}
description: Test-only Methods package for the generation Gate oracle.
---

Read request.json, method-evidence-graph.json, and method-evaluation-plan.json.
Return evaluation_ref, verdict, and reason for every item. Use UNKNOWN when
the frozen evidence cannot decide a method.
""",
        encoding="utf-8",
    )
    methods: list[dict[str, Any]] = []
    for index, semantic in enumerate(expected["method_marker_sets"], start=1):
        reference = f"references/method-{index}.md"
        marker_text = "\n".join(semantic["all_markers"])
        (package_root / reference).write_text(
            f"""# Test method

## 适用条件
Test-only condition.
## 所需证据
{marker_text}
## 计算与判断
Evaluate the declared markers.
## 确认条件
All declared conditions hold.
## 未知边界
Missing evidence remains UNKNOWN.
## 输出含义
Return one evaluation verdict.
""",
            encoding="utf-8",
        )
        methods.append(
            {
                "id": f"method-{index}",
                "title": f"Method {index}",
                "reference": reference,
                "priority": index,
                "evidence_markers": list(semantic["all_markers"]),
            }
        )
    shared_reference = "references/source-log-templates.md"
    (package_root / shared_reference).write_text(
        validator._render_source_log_templates(source_identity["log_templates"]),
        encoding="utf-8",
    )
    (package_root / "methods.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skill_name": expected["skill_name"],
                "source_wiki_sha256": expected["source_wiki_sha256"],
                "required_user_inputs": expected["required_user_inputs"],
                "required_artifacts": expected["required_artifacts"],
                "log_derived_fields": expected["required_log_derived_fields"],
                "shared_references": [shared_reference],
                "methods": methods,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return package_root, load_methods_package(package_root), oracle


def test_generation_prompt_uses_validator_canonical_marker_checklist() -> None:
    validator = _load_validator()
    wiki_bytes = (
        ROOT / "tests/cases/release/rpc-timeout-anonymized/input/wiki.md"
    ).read_bytes()
    source_identity = validator.build_source_wiki_identity(
        wiki_bytes,
        "inputs/wiki.md",
    )

    checklist = validator.canonical_evidence_markers(
        source_identity["log_templates"]
    )
    expected = [
        "rpc call",
        "call unsuccess, reqid(",
        "LATE_RESPONSE service=",
        "API_COMPLETE service=",
        "QUEUE_HISTORY print_time_ms=",
        "DEADLOOP_DETECTED service=",
    ]
    prompt = _generation_prompt(
        requested_skill_name="diagnose-rpc-timeout",
        canonical_marker_checklist=checklist,
    )

    assert checklist == expected
    assert json.dumps(expected, ensure_ascii=False, separators=(",", ":")) in prompt
    assert all(shorthand not in checklist for shorthand in NON_CANONICAL_EVENT_NAMES)
    assert json.dumps(
        list(NON_CANONICAL_EVENT_NAMES),
        ensure_ascii=False,
        separators=(",", ":"),
    ) in prompt
    contract = (META_SKILL_ROOT / "references/output-contract.md").read_text(
        encoding="utf-8"
    )
    assert "`API_COMPLETE service=`，不是 `API_COMPLETE`" in contract
    assert "`QUEUE_HISTORY print_time_ms=`，不是 `QUEUE_HISTORY`" in contract


def test_generation_validator_rejects_all_observed_shorthand_markers(
    tmp_path: Path,
) -> None:
    package_root, _, _ = _gate_oracle_test_baseline(tmp_path)
    validator = _load_validator()
    wiki = ROOT / "tests/cases/release/rpc-timeout-anonymized/input/wiki.md"
    assert validator.validate(package_root, wiki)["ok"] is True
    shorthand_by_marker = {
        "API_COMPLETE service=": "API_COMPLETE",
        "DEADLOOP_DETECTED service=": "DEADLOOP_DETECTED",
        "LATE_RESPONSE service=": "LATE_RESPONSE",
        "QUEUE_HISTORY print_time_ms=": "QUEUE_HISTORY",
    }
    methods_path = package_root / "methods.json"
    manifest = json.loads(methods_path.read_text(encoding="utf-8"))
    for method in manifest["methods"]:
        method["evidence_markers"] = [
            shorthand_by_marker[marker] for marker in method["evidence_markers"]
        ]
    methods_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    result = validator.validate(package_root, wiki)

    assert result["ok"] is False
    assert result["errors"] == [
        "method 1 evidence marker is not a canonical stable Wiki log marker: LATE_RESPONSE",
        "method 1 evidence marker is not a canonical stable Wiki log marker: API_COMPLETE",
        "method 1 evidence marker is not a canonical stable Wiki log marker: DEADLOOP_DETECTED",
        "method 2 evidence marker is not a canonical stable Wiki log marker: LATE_RESPONSE",
        "method 2 evidence marker is not a canonical stable Wiki log marker: QUEUE_HISTORY",
        "method 3 evidence marker is not a canonical stable Wiki log marker: LATE_RESPONSE",
    ]


def test_generation_gate_oracle_accepts_exact_one_to_one_package(
    tmp_path: Path,
) -> None:
    package_root, manifest, oracle = _gate_oracle_test_baseline(tmp_path)
    validator = _load_validator()
    wiki = ROOT / "tests/cases/release/rpc-timeout-anonymized/input/wiki.md"

    audit = _gate_only_oracle_audit(
        package_root=package_root,
        manifest=manifest,
        oracle=oracle,
    )

    assert validator.validate(package_root, wiki)["ok"] is True
    assert audit["status"] == "PASS"
    assert audit["mismatches"] == []
    assert audit["generated_method_count"] == audit["expected_method_count"] == 3
    assert audit["method_mapping_is_bijective"] is True
    assert audit["unmapped_method_ids"] == []
    assert all(
        item["exact_match_count"] == 1 and len(item["matched_method_ids"]) == 1
        for item in audit["method_marker_coverage"]
    )


def test_generation_gate_oracle_rejects_extra_marker(tmp_path: Path) -> None:
    package_root, manifest, oracle = _gate_oracle_test_baseline(tmp_path)
    first = manifest.methods[0]
    mutant = replace(
        manifest,
        methods=(
            replace(
                first,
                evidence_markers=(*first.evidence_markers, "UNEXPECTED_MARKER"),
            ),
            *manifest.methods[1:],
        ),
    )

    audit = _gate_only_oracle_audit(
        package_root=package_root,
        manifest=mutant,
        oracle=oracle,
    )

    assert audit["status"] == "FAIL"
    assert "method_marker_set:api_execution_overrun" in audit["mismatches"]
    assert "method_marker_mapping" in audit["mismatches"]
    assert audit["generated_method_count"] == audit["expected_method_count"]
    assert audit["unmapped_method_ids"] == [first.id]


def test_generation_gate_oracle_rejects_extra_method(tmp_path: Path) -> None:
    package_root, manifest, oracle = _gate_oracle_test_baseline(tmp_path)
    last = manifest.methods[-1]
    mutant = replace(
        manifest,
        methods=(
            *manifest.methods,
            replace(
                last,
                id="unexpected-method",
                title="Unexpected method",
                reference="references/unexpected-method.md",
                priority=len(manifest.methods) + 1,
                evidence_markers=("UNEXPECTED_METHOD_MARKER",),
            ),
        ),
    )

    audit = _gate_only_oracle_audit(
        package_root=package_root,
        manifest=mutant,
        oracle=oracle,
    )

    assert audit["status"] == "FAIL"
    assert "method_count" in audit["mismatches"]
    assert "method_marker_mapping" in audit["mismatches"]
    assert audit["unmapped_method_ids"] == ["unexpected-method"]


def test_generation_gate_oracle_rejects_duplicate_ambiguous_mapping(
    tmp_path: Path,
) -> None:
    package_root, manifest, oracle = _gate_oracle_test_baseline(tmp_path)
    first = manifest.methods[0]
    second = manifest.methods[1]
    mutant = replace(
        manifest,
        methods=(
            replace(first, evidence_markers=second.evidence_markers),
            *manifest.methods[1:],
        ),
    )

    audit = _gate_only_oracle_audit(
        package_root=package_root,
        manifest=mutant,
        oracle=oracle,
    )

    assert audit["status"] == "FAIL"
    assert "method_marker_set:api_execution_overrun" in audit["mismatches"]
    assert "method_marker_set:server_receive_queueing" in audit["mismatches"]
    assert "method_marker_mapping" in audit["mismatches"]
    assert audit["method_marker_coverage"][1]["exact_match_count"] == 2
    assert audit["method_mapping_is_bijective"] is False


def test_claude_2_1_89_pinned_model_generates_registered_methods_package(
    isolated_agent_workspace_root: Path,
) -> None:
    if os.environ.get("S08_REAL_SKILL_GENERATION_GATE") != "1":
        pytest.skip("requires the explicitly selected real Methods-generation gate")
    command = os.environ.get("S08_REAL_SKILL_GENERATION_AGENT_COMMAND")
    assert command, "S08_REAL_SKILL_GENERATION_AGENT_COMMAND is required"
    scenario_audit_path = _scenario_audit_path()

    case_root = _release_case_root()
    descriptor = _load_object(case_root / "case.json")
    assert descriptor["schema_version"] == 2
    assert "clarifications" not in descriptor
    assert "generation_spec" not in descriptor
    assert "approved_skill_dir" not in descriptor
    registration = _load_object(case_root / descriptor["registration_template"])
    requested_skill_name = registration["package"]["skill_name"]

    workspace = isolated_agent_workspace_root / "workspace"
    inputs = workspace / "inputs"
    runtime = workspace / "runtime"
    output = workspace / "output"
    inputs.mkdir(parents=True)
    runtime.mkdir()
    output.mkdir()
    wiki_source = case_root / descriptor["input_wiki"]
    wiki_bytes = wiki_source.read_bytes()
    wiki_sha256 = hashlib.sha256(wiki_bytes).hexdigest()
    validator = _load_validator()
    source_identity = validator.build_source_wiki_identity(
        wiki_bytes, "inputs/wiki.md"
    )
    assert source_identity["schema_version"] == 2
    assert source_identity["sha256"] == wiki_sha256
    canonical_marker_checklist = validator.canonical_evidence_markers(
        source_identity["log_templates"]
    )
    (inputs / "wiki.md").write_bytes(wiki_bytes)
    _write_new(
        runtime / SOURCE_WIKI_IDENTITY_FILE,
        source_identity,
    )

    stdout = _Sink()
    stderr = _Sink()
    prompt = _generation_prompt(
        requested_skill_name=requested_skill_name,
        canonical_marker_checklist=canonical_marker_checklist,
    )
    try:
        execution = AgentBackend(command).execute(
            prompt=prompt,
            workspace_root=workspace,
            cancellation=_Signal(),
            log_sinks=ExecutionLogSinks(
                stdout=stdout,
                stderr=stderr,
                combined_limit_bytes=JOB_STDOUT_STDERR_BYTES,
            ),
            resource_limits=default_resource_limits(JobType.DIAGNOSE),
            test_limits=BackendExecutionLimits(
                wall_time_seconds=float(
                    os.environ["TEST_FLOW_AGENT_BACKEND_WALL_TIME_SECONDS"]
                ),
                stdout_stderr_bytes=8 * 1024 * 1024,
                workspace_bytes=16 * 1024 * 1024,
                poll_interval_seconds=0.02,
                termination_grace_seconds=5.0,
            ),
        )
    except RuntimeExecutionError as exc:
        pytest.fail(
            f"Methods generation Agent failed with {exc.failure.code.value}; "
            f"stdout={bytes(stdout.data).decode('utf-8', 'replace')!r}; "
            f"stderr={bytes(stderr.data).decode('utf-8', 'replace')!r}"
        )
    assert execution.returncode == 0
    assert (inputs / "wiki.md").read_bytes() == wiki_bytes
    assert sorted(path.name for path in output.iterdir()) == [requested_skill_name]
    generated_package = output / requested_skill_name

    validator_result = validator.validate(generated_package, wiki_source)
    oracle = _load_object(case_root / descriptor["semantic_oracle"])
    if not validator_result["ok"]:
        _write_new(
            scenario_audit_path,
            {
                "schema_version": 2,
                "status": "FAIL",
                "oracle_visibility": "GATE_ONLY",
                "diagnostic_kind": "CANONICAL_VALIDATOR_FAILED",
                "canonical_validator": validator_result,
                "oracle_sha256": hashlib.sha256(
                    (case_root / descriptor["semantic_oracle"]).read_bytes()
                ).hexdigest(),
            },
        )
        pytest.fail("canonical Methods validator rejected generated package")
    assert validator_result["source_wiki_sha256"] == wiki_sha256

    manifest = load_methods_package(generated_package)
    oracle_audit = _gate_only_oracle_audit(
        package_root=generated_package,
        manifest=manifest,
        oracle=oracle,
    )
    if oracle_audit["status"] != "PASS":
        _write_new(
            scenario_audit_path,
            {
                "schema_version": 2,
                "status": "FAIL",
                "oracle_visibility": "GATE_ONLY",
                "diagnostic_kind": "SEMANTIC_ORACLE_MISMATCH",
                "canonical_validator": validator_result,
                "semantic_oracle": oracle_audit,
                "oracle_sha256": hashlib.sha256(
                    (case_root / descriptor["semantic_oracle"]).read_bytes()
                ).hexdigest(),
            },
        )
        pytest.fail(f"Methods semantic oracle mismatch: {oracle_audit['mismatches']!r}")

    output_root_value = os.environ.get("S08_REAL_SKILL_GENERATION_OUTPUT_ROOT")
    assert output_root_value, "S08_REAL_SKILL_GENERATION_OUTPUT_ROOT is required"
    output_root = Path(output_root_value).resolve()
    resolved = _install_product_registration(
        case_root=case_root,
        descriptor=descriptor,
        generated_package=generated_package,
        output_root=output_root,
    )
    assert resolved.methods == manifest
    assert resolved.registration.skill_name == requested_skill_name
    assert resolved.registration.source_wiki_sha256 == wiki_sha256
    _write_generated_skill_receipt(case_id=descriptor["case_id"], resolved=resolved)

    package_files = _package_files(generated_package)
    _write_new(
        scenario_audit_path,
        {
            "schema_version": 2,
            "status": "PASS",
            "oracle_visibility": "GATE_ONLY",
            "diagnostic_kind": "NONE",
            "case_id": descriptor["case_id"],
            "canonical_validator": {
                "ok": True,
                "skill_name": validator_result["skill_name"],
                "source_wiki_sha256": validator_result["source_wiki_sha256"],
                "method_count": validator_result["method_count"],
                "marker_count": validator_result["marker_count"],
                "template_count": validator_result["template_count"],
            },
            "semantic_oracle": oracle_audit,
            "oracle_sha256": hashlib.sha256(
                (case_root / descriptor["semantic_oracle"]).read_bytes()
            ).hexdigest(),
            "generated_package": {
                "skill_name": manifest.skill_name,
                "file_count": len(package_files),
                "files": package_files,
                "package_tree_sha256": resolved.package_tree_sha256,
            },
            "product_registration": {
                "registration_id": resolved.registration_id,
                "version": resolved.registration.version,
                "registration_sha256": resolved.registration_sha256,
                "combined_sha256": resolved.combined_sha256,
            },
        },
    )
