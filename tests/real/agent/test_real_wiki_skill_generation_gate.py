from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from pathlib import Path
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


ROOT = Path(__file__).resolve().parents[3]
GENERATOR_PATH = (
    ROOT
    / ".claude/skills/wiki-to-diagnosis-skill/scripts/generate_diagnosis_skill.py"
)
VALIDATOR_PATH = (
    ROOT
    / ".claude/skills/wiki-to-diagnosis-skill/scripts/validate_generated_skill.py"
)


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _release_case_root() -> Path:
    configured = Path(os.environ["S08_RELEASE_CASES_ROOT"])
    candidates = sorted(
        path.parent for path in configured.glob("*/case.json")
    )
    assert len(candidates) == 1
    return candidates[0]


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


def test_real_conversion_agent_builds_an_executable_reviewed_skill_from_plain_wiki(
    tmp_path: Path,
) -> None:
    if os.environ.get("S08_REAL_SKILL_GENERATION_GATE") != "1":
        pytest.skip("requires the explicitly selected real Skill-generation gate")
    command = os.environ.get("S08_REAL_SKILL_GENERATION_AGENT_COMMAND")
    assert command, "S08_REAL_SKILL_GENERATION_AGENT_COMMAND is required"

    case_root = _release_case_root()
    descriptor = json.loads((case_root / "case.json").read_text(encoding="utf-8"))
    workspace = tmp_path / "workspace"
    inputs = workspace / "inputs"
    output = workspace / "output"
    inputs.mkdir(parents=True)
    output.mkdir()
    wiki = (case_root / descriptor["input_wiki"]).read_bytes()
    clarifications = (case_root / descriptor["clarifications"]).read_bytes()
    (inputs / "wiki.md").write_bytes(wiki)
    (inputs / "clarifications.md").write_bytes(clarifications)

    stdout = _Sink()
    stderr = _Sink()
    prompt = """Use the wiki-to-diagnosis-skill Skill to convert this reviewed plain Markdown Wiki into one executable GenerationSpec v5.

Your first action must call the Skill tool with exactly {"skill":"wiki-to-diagnosis-skill"}. Then read only inputs/wiki.md and inputs/clarifications.md. The clarifications are authoritative when they resolve ambiguity. Do not read outside inputs/. Do not ask questions, use the network, or invent a platform log prefix. Treat both (# ... #) and （# ... #） as conversion-only author notes that must not enter any product field.

Write exactly one UTF-8 JSON object to output/generation-spec.json. It must satisfy the loaded Skill's GenerationSpec v5 contract and preserve the Wiki's multiple contributors, lossy observation policies, multiline record, explicit clock tolerance, COMPLETE/PARTIAL/NONE paths, fixed-snapshot boundary, and timeout-not-cancellation safety meaning. Create no other output file or directory, then stop.
"""
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
                wall_time_seconds=600.0,
                stdout_stderr_bytes=8 * 1024 * 1024,
                workspace_bytes=16 * 1024 * 1024,
                poll_interval_seconds=0.02,
                termination_grace_seconds=5.0,
            ),
        )
    except RuntimeExecutionError as exc:
        pytest.fail(
            f"Wiki conversion Agent failed with {exc.failure.code.value}; "
            f"stdout={bytes(stdout.data).decode('utf-8', 'replace')!r}; "
            f"stderr={bytes(stderr.data).decode('utf-8', 'replace')!r}"
        )
    assert execution.returncode == 0
    assert sorted(path.name for path in output.iterdir()) == [
        "generation-spec.json"
    ]
    assert (inputs / "wiki.md").read_bytes() == wiki
    assert (inputs / "clarifications.md").read_bytes() == clarifications

    generator = _load_module(GENERATOR_PATH, "_real_wiki_generator_v5")
    validator = _load_module(VALIDATOR_PATH, "_real_wiki_validator_v5")
    model_spec = generator.load_generation_spec(output / "generation-spec.json")
    compiled_root = tmp_path / "compiled"
    generated = generator.generate_diagnosis_skill(model_spec, compiled_root)
    validation = validator.validate_skill_directory(generated.skill_dir)
    assert validation.ok, validation.errors

    semantic_oracle = json.loads(
        (case_root / descriptor["semantic_oracle"]).read_text(encoding="utf-8")
    )
    expected = semantic_oracle["expected_skill"]
    manifest = json.loads(
        (generated.skill_dir / "diagnosis-skill.json").read_text(encoding="utf-8")
    )
    contract = manifest["verification_contract"]
    assert manifest["id"] == expected["id"]
    assert manifest["version"] == expected["version"]
    assert manifest["capability"] == expected["capability"]
    assert manifest["deployment_scope"] == expected["deployment_scope"]
    assert [item["name"] for item in manifest["requirements"]] == expected[
        "requirement_names"
    ]
    assert sorted({item["kind"] for item in contract["observation_policies"]}) == sorted(
        expected["observation_policy_kinds"]
    )
    assert [item["id"] for item in contract["terminal_paths"]] == expected[
        "terminal_paths"
    ]
    assert any(len(item["members"]) > 1 for item in contract["event_extractors"])
    assert any(item["kind"] == "NUMERIC_COMPARE" for item in contract["rules"])
    assert max(
        item["parameters"].get("clock_tolerance_ms", 0)
        for item in contract["rules"]
    ) == expected["requires_cross_clock_tolerance_ms"]
    product_text = "\n".join(
        (generated.skill_dir / name).read_text(encoding="utf-8")
        for name in ("SKILL.md", "diagnosis-skill.json")
    )
    for marker in semantic_oracle["author_note_markers_forbidden_in_product"]:
        assert marker not in product_text

    approved_spec = generator.load_generation_spec(
        case_root / descriptor["generation_spec"]
    )
    approved_product = generator.render_product(approved_spec)
    approved_root = case_root / descriptor["approved_skill_dir"]
    assert {
        name: (approved_root / name).read_bytes()
        for name in sorted(approved_product)
    } == approved_product
