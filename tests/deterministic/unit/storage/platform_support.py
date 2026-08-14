from __future__ import annotations

import os
from pathlib import Path

import pytest


def try_symlink(
    link: Path,
    target: Path,
    *,
    target_is_directory: bool = False,
) -> bool:
    """Create a test symlink and report an unavailable platform capability."""

    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except NotImplementedError:
        if os.name == "nt":
            return False
        raise
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            return False
        raise
    return True


def symlink_or_skip(
    link: Path,
    target: Path,
    *,
    target_is_directory: bool = False,
) -> None:
    """Create a test symlink or skip when Windows lacks the required privilege."""

    if not try_symlink(link, target, target_is_directory=target_is_directory):
        pytest.skip("symbolic link creation requires Windows developer mode")
