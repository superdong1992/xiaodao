from __future__ import annotations

import tomllib
from pathlib import Path

from problem_locator import __version__
from problem_locator.runtime.methods_skill import load_specialized_skill_registration
from tests.platform.distribution.test_installed_distribution_gate import (
    EXPECTED_RUNTIME_VERSIONS,
    RPC_COMBINED_SHA256,
    RPC_PACKAGE_TREE_SHA256,
    RPC_REGISTRATION,
    RPC_REGISTRATION_SHA256,
)


ROOT = Path(__file__).resolve().parents[3]
RELEASE_VERSION = "8.0.0"


def test_runtime_project_and_lock_publish_one_v4_release_version() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_project = [
        package
        for package in lock["package"]
        if package["name"] == "problem-locator"
    ]

    assert __version__ == RELEASE_VERSION
    assert project["project"]["version"] == RELEASE_VERSION
    assert len(locked_project) == 1
    assert locked_project[0]["version"] == RELEASE_VERSION
    assert EXPECTED_RUNTIME_VERSIONS["problem-locator"] == RELEASE_VERSION


def test_installed_distribution_skill_hashes_match_current_fixture() -> None:
    registration = load_specialized_skill_registration(RPC_REGISTRATION)

    assert registration.registration_sha256 == RPC_REGISTRATION_SHA256
    assert registration.package_tree_sha256 == RPC_PACKAGE_TREE_SHA256
    assert registration.combined_sha256 == RPC_COMBINED_SHA256
