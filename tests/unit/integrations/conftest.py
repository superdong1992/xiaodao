"""Local opt-in switches for S07 integration tests."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("problem-locator-logparse")
    group.addoption(
        "--run-real-logparse",
        action="store_true",
        default=False,
        help="run the S07 test against an explicitly configured real logparse checkout",
    )
