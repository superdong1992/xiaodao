from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
E2E_BASE_COMMIT = "c31cc03848155d03b9a35776555e413f26b264ad"
E2E_PRODUCT_SCOPES = ("schemas/v2", "src/problem_locator")
E2E_TEST_SCOPES = ("tests",)


def _load_toml(name: str) -> dict[str, object]:
    return tomllib.loads((REPOSITORY_ROOT / name).read_text(encoding="utf-8"))


def _git_paths(*arguments: str) -> set[str]:
    completed = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return {line for line in completed.stdout.splitlines() if line}


def test_e2e_patch_allowlist_covers_current_product_schema_and_test_delta() -> None:
    allowed = {
        line
        for line in (
            REPOSITORY_ROOT / "tools/e2e/product-patch-files.txt"
        ).read_text(encoding="utf-8").splitlines()
        if line
    }
    tracked = _git_paths(
        "diff",
        "--name-only",
        E2E_BASE_COMMIT,
        "--",
        *E2E_PRODUCT_SCOPES,
    )
    untracked = _git_paths(
        "ls-files",
        "--others",
        "--exclude-standard",
        "--",
        *E2E_PRODUCT_SCOPES,
    )
    tracked_tests = _git_paths(
        "diff",
        "--name-only",
        E2E_BASE_COMMIT,
        "--",
        *E2E_TEST_SCOPES,
    )
    untracked_tests = _git_paths(
        "ls-files",
        "--others",
        "--exclude-standard",
        "--",
        *E2E_TEST_SCOPES,
    )
    assert tracked | untracked | tracked_tests | untracked_tests <= allowed


def test_python_and_dependency_baseline_is_frozen() -> None:
    project = _load_toml("pyproject.toml")
    metadata = project["project"]
    assert isinstance(metadata, dict)
    assert metadata["requires-python"] == ">=3.12,<3.13"
    assert "pydantic==2.13.4" in metadata["dependencies"]
    groups = project["dependency-groups"]
    assert isinstance(groups, dict)
    assert {"jsonschema==4.25.1", "pytest==9.0.2"}.issubset(groups["dev"])


def test_public_console_entries_are_pre_registered() -> None:
    project = _load_toml("pyproject.toml")
    metadata = project["project"]
    assert isinstance(metadata, dict)
    scripts = metadata["scripts"]
    assert scripts == {
        "problem-locator-logparse": "problem_locator.integrations.logparse.cli:main",
        "problem-locator-seal-outcome-draft": (
            "problem_locator.runtime.outcome_finalizer:main"
        ),
    }


def test_uv_lock_matches_the_frozen_direct_dependencies() -> None:
    lock = _load_toml("uv.lock")
    assert lock["version"] == 1
    assert lock["requires-python"] == "==3.12.*"
    packages = {
        package["name"]: package
        for package in lock["package"]
        if isinstance(package, dict)
    }
    assert packages["pydantic"]["version"] == "2.13.4"
    assert packages["jsonschema"]["version"] == "4.25.1"
    assert packages["pytest"]["version"] == "9.0.2"
