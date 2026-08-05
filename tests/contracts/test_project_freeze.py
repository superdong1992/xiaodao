from __future__ import annotations

import tomllib
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _load_toml(name: str) -> dict[str, object]:
    return tomllib.loads((REPOSITORY_ROOT / name).read_text(encoding="utf-8"))


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
        "problem-locator-client-proxy": "problem_locator.interfaces.client_proxy:main",
        "problem-locator-logparse": "problem_locator.integrations.logparse.cli:main",
        "problem-locator-pack-result": "problem_locator.integrations.result_archive:main",
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
