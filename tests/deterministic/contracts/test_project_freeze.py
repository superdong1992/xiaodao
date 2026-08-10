from __future__ import annotations

import json
import tomllib
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
def _load_toml(name: str) -> dict[str, object]:
    return tomllib.loads((REPOSITORY_ROOT / name).read_text(encoding="utf-8"))


def test_test_flow_identity_closure_replaces_the_manual_patch_allowlist() -> None:
    identity_path = REPOSITORY_ROOT / "tools/test-flow/config/identities.v2.json"
    identities = json.loads(identity_path.read_text(encoding="utf-8"))
    components = identities["components"]
    identity_sets = identities["sets"]
    product_paths = set(components["product.source"]["paths"])
    deterministic_paths = set(components["proof.deterministic"]["paths"])
    framework_paths = set(components["framework.runner"]["paths"])

    assert {"src", "schemas", "pyproject.toml", "uv.lock"} <= product_paths
    assert "tests/deterministic" not in product_paths
    assert deterministic_paths == {"tests/deterministic"}
    assert "tools/test-flow/lib" in framework_paths
    assert "product.source" in identity_sets["deterministic"]["producer"]
    assert "proof.deterministic" in identity_sets["deterministic"]["proof"]
    assert "framework.runner" in identity_sets["deterministic"]["proof"]
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
