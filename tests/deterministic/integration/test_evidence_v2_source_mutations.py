from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPO_ROOT / "src"


@dataclass(frozen=True)
class SourceMutation:
    name: str
    source_path: str
    original: str
    replacement: str
    selector: str
    expected_failures: int
    source_root: str = "src"
    environment_key: str | None = None


MUTATIONS = (
    SourceMutation(
        name="cross-method-marker-index",
        source_path="problem_locator/runtime/methods_evidence_v2.py",
        original=(
            "    for item in evidence.hits:\n"
            "        method_markers = methods_by_id[item.method_id].evidence_markers\n"
            "        if (\n"
            "            item.marker_index > len(method_markers)\n"
            "            or item.marker != method_markers[item.marker_index - 1]\n"
            "        ):\n"
            "            raise ValueError(\"evidence hit marker/index does not belong to its method\")\n"
            "\n"
        ),
        replacement="",
        selector=(
            "tests/deterministic/unit/runtime/test_methods_evidence_v2.py::"
            "test_plan_rejects_rehashed_hit_bound_to_another_methods_marker_index"
        ),
        expected_failures=1,
    ),
    SourceMutation(
        name="specialized-job-restores-v1-path",
        source_path="problem_locator/runtime/diagnosis_runtime.py",
        original=(
            "        job.job_type is JobType.DIAGNOSE\n"
            "        and job.diagnosis_mode is DiagnosisMode.SPECIALIZED\n"
        ),
        replacement="        False\n",
        selector=(
            "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::"
            "test_specialist_scans_once_hard_cuts_logs_and_publishes_handoff"
        ),
        expected_failures=1,
    ),
    SourceMutation(
        name="third-role-call",
        source_path="problem_locator/domain/methods_state_v2.py",
        original=(
            "    if failures == 0:\n"
            "        return _replace(state, **{field: 1})\n"
        ),
        replacement=(
            "    if failures <= 1:\n"
            "        return _replace(state, **{field: 1})\n"
        ),
        selector=(
            "tests/deterministic/unit/domain/test_methods_state_v2.py::"
            "test_each_role_gets_one_protocol_repair_then_exhausts"
        ),
        expected_failures=2,
    ),
    SourceMutation(
        name="case-sensitive-downstream-rematch",
        source_path="problem_locator/runtime/methods_evidence_v2.py",
        original=(
            "    for item in evidence.hits:\n"
            "        method_markers = methods_by_id[item.method_id].evidence_markers\n"
            "        if (\n"
            "            item.marker_index > len(method_markers)\n"
            "            or item.marker != method_markers[item.marker_index - 1]\n"
            "        ):\n"
            "            raise ValueError(\"evidence hit marker/index does not belong to its method\")\n"
            "\n"
        ),
        replacement=(
            "    for item in evidence.hits:\n"
            "        method_markers = methods_by_id[item.method_id].evidence_markers\n"
            "        if (\n"
            "            item.marker_index > len(method_markers)\n"
            "            or item.marker != method_markers[item.marker_index - 1]\n"
            "        ):\n"
            "            raise ValueError(\"evidence hit marker/index does not belong to its method\")\n"
            "    if any(item.marker not in item.line for item in evidence.hits):\n"
            "        raise ValueError(\"mutant restored downstream marker matching\")\n"
            "\n"
        ),
        selector=(
            "tests/deterministic/integration/test_methods_v2_runtime_journey.py::"
            "test_runtime_submission_reviewer_and_public_projection_are_one_v2_journey"
        ),
        expected_failures=1,
    ),
    SourceMutation(
        name="workspace-hardlink-materialization",
        source_path="problem_locator/storage/resource_files.py",
        original=(
            "            if resource_ref.resource_kind is ResourceKind.FILE:\n"
            "                temporary = self._new_materialization_temp(destination)\n"
        ),
        replacement=(
            "            if resource_ref.resource_kind is ResourceKind.FILE:\n"
            "                os.link(source, destination, follow_symlinks=False)\n"
            "                temporary = self._new_materialization_temp(destination)\n"
        ),
        selector=(
            "tests/deterministic/unit/storage/test_resource_files.py::"
            "test_reader_file_materialization_never_attempts_a_hardlink"
        ),
        expected_failures=1,
    ),
    SourceMutation(
        name="package-validator-marker-ownership",
        source_path=(
            ".agents/skills/wiki-to-diagnosis-skill/scripts/"
            "validate_generated_skill.py"
        ),
        original=(
            "            for index, reference, markers in marker_bindings:\n"
            "                method_text = reference_texts.get(reference)\n"
            "                if method_text is None:\n"
            "                    continue\n"
            "                for marker in markers:\n"
            "                    if marker not in method_text:\n"
            "                        errors.append(\n"
            "                            f\"method {index} evidence marker is absent from its method reference: {marker}\"\n"
            "                        )\n"
        ),
        replacement="",
        selector=(
            "tests/deterministic/unit/runtime/test_meta_skill_source_identity.py::"
            "test_validator_rejects_marker_from_another_method_reference"
        ),
        expected_failures=1,
        source_root="repo",
        environment_key="TEST_WIKI_DIAGNOSIS_VALIDATOR",
    ),
    SourceMutation(
        name="registration-validator-marker-ownership",
        source_path=(
            ".claude/skills/wiki-to-logparse-diagnosis-skill/scripts/"
            "validate_generated_skill.py"
        ),
        original=(
            "        for index, reference, markers in marker_bindings:\n"
            "            method_text = reference_texts.get(reference)\n"
            "            if method_text is None:\n"
            "                continue\n"
            "            for marker in markers:\n"
            "                if marker not in method_text:\n"
            "                    errors.append(\n"
            "                        f\"method {index} evidence marker is absent from its method reference: {marker}\"\n"
            "                    )\n"
        ),
        replacement="",
        selector=(
            "tests/deterministic/unit/integrations/test_lan_logparse_meta_skill.py::"
            "test_validator_rejects_marker_from_another_method_reference"
        ),
        expected_failures=1,
        source_root="repo",
        environment_key="TEST_LAN_DIAGNOSIS_VALIDATOR",
    ),
)


def _apply_source_overlay_mutation(
    tmp_path: Path,
    mutation: SourceMutation,
) -> tuple[Path, dict[str, str]]:
    overlay_src = tmp_path / "overlay" / "src"
    shutil.copytree(
        SOURCE_ROOT,
        overlay_src,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    production_root = SOURCE_ROOT if mutation.source_root == "src" else REPO_ROOT
    overlay_root = overlay_src if mutation.source_root == "src" else tmp_path / "overlay"
    production_path = production_root / mutation.source_path
    overlay_path = overlay_root / mutation.source_path
    if mutation.source_root == "repo":
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(production_path, overlay_path)
    production_bytes = production_path.read_bytes()
    source = production_bytes.decode("utf-8")

    assert source.count(mutation.original) == 1, (
        f"mutation anchor drifted for {mutation.name}: {mutation.source_path}"
    )
    mutated = source.replace(mutation.original, mutation.replacement, 1)
    overlay_path.write_text(mutated, encoding="utf-8", newline="\n")

    assert overlay_path.read_text(encoding="utf-8") == mutated
    assert production_path.read_bytes() == production_bytes
    environment = (
        {}
        if mutation.environment_key is None
        else {mutation.environment_key: str(overlay_path)}
    )
    return overlay_src, environment


def _run_exact_regression_test(
    *,
    tmp_path: Path,
    overlay_src: Path,
    overlay_environment: dict[str, str],
    mutation: SourceMutation,
) -> subprocess.CompletedProcess[str]:
    pytest_config = tmp_path / "pytest.ini"
    pytest_config.write_text(
        "[pytest]\naddopts = --strict-config --strict-markers\n",
        encoding="utf-8",
        newline="\n",
    )
    environment = os.environ.copy()
    python_path = [str(overlay_src)]
    if existing := environment.get("PYTHONPATH"):
        python_path.append(existing)
    environment.update(
        {
            "NO_COLOR": "1",
            "PY_COLORS": "0",
            "PYTHONPATH": os.pathsep.join(python_path),
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            **overlay_environment,
        }
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            "-c",
            str(pytest_config),
            "--basetemp",
            str(tmp_path / "child-basetemp"),
            mutation.selector,
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda item: item.name)
def test_source_overlay_mutant_is_killed_by_exact_regression_test(
    tmp_path: Path,
    mutation: SourceMutation,
) -> None:
    overlay_src, overlay_environment = _apply_source_overlay_mutation(
        tmp_path,
        mutation,
    )
    completed = _run_exact_regression_test(
        tmp_path=tmp_path,
        overlay_src=overlay_src,
        overlay_environment=overlay_environment,
        mutation=mutation,
    )
    output = f"{completed.stdout}\n{completed.stderr}".replace("\\", "/")
    failed_lines = re.findall(r"(?m)^FAILED\s+(.+)$", output)
    target_test_name = mutation.selector.split("::", 1)[1]

    assert completed.returncode == 1, output
    assert len(failed_lines) == mutation.expected_failures, output
    assert all(target_test_name in line for line in failed_lines), output
    assert f"{mutation.expected_failures} failed" in output, output
    assert "ERROR collecting" not in output, output
    assert not re.search(r"(?m)^ERROR(?:\s|$)", output), output
    assert "INTERNALERROR" not in output, output
    assert "ImportError" not in output, output
    assert "ModuleNotFoundError" not in output, output
    assert "no tests ran" not in output, output
    assert (
        "E   assert" in output
        or "E   AssertionError" in output
        or "Failed: DID NOT RAISE" in output
    ), output
