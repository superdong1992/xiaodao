from __future__ import annotations

import tomllib
from pathlib import Path

from problem_locator import __version__


ROOT = Path(__file__).resolve().parents[3]
RELEASE_VERSION = "2.0.0"


def test_runtime_project_and_lock_publish_one_v2_release_version() -> None:
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
