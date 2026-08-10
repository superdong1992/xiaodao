"""Repository-wide pytest fixtures shared by every deterministic platform."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pytest import FixtureRequest, TempPathFactory


@pytest.fixture
def tmp_path(request: FixtureRequest, tmp_path_factory: TempPathFactory) -> Path:
    """Allocate a unique bounded path without embedding the full test name.

    Deep storage and quarantine tests append contract-owned UUID and SHA-256
    segments.  Keeping the pytest-owned segment bounded makes those same tests
    runnable on standard Windows hosts where long paths are not enabled.
    The nodeid remains visible in JUnit and the digest prevents collisions.
    """

    node_digest = hashlib.sha256(request.node.nodeid.encode("utf-8")).hexdigest()[:12]
    return tmp_path_factory.mktemp(f"t-{node_digest}", numbered=False)
