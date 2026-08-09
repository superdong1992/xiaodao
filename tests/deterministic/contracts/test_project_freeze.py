from __future__ import annotations

import json
import tomllib
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
def _load_toml(name: str) -> dict[str, object]:
    return tomllib.loads((REPOSITORY_ROOT / name).read_text(encoding="utf-8"))


def test_test_flow_identity_closure_replaces_the_manual_patch_allowlist() -> None:
    identity_path = REPOSITORY_ROOT / "tools/test-flow/config/identities.v1.json"
    identities = json.loads(identity_path.read_text(encoding="utf-8"))
    groups = identities["groups"]
    product_paths = set(groups["product"]["paths"])
    proof_paths = set(groups["proof"]["paths"])

    assert {"src", "schemas", "pyproject.toml", "uv.lock"} <= product_paths
    assert "tests/deterministic" in product_paths
    assert "tools/test-flow" in proof_paths
    assert "tests" in proof_paths
    assert not (REPOSITORY_ROOT / "tools/test-flow/product-patch-files.txt").exists()


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
