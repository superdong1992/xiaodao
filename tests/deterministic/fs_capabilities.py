from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest


_UNAVAILABLE_ERRNOS = {
    errno.EACCES,
    errno.EPERM,
    errno.ENOSYS,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}


def _skip_if_capability_unavailable(operation: str, error: BaseException) -> None:
    if isinstance(error, (AttributeError, NotImplementedError)):
        pytest.skip(f"{operation} capability unavailable: {error}")
    if isinstance(error, OSError) and (
        error.errno in _UNAVAILABLE_ERRNOS
        or getattr(error, "winerror", None) == 1314
    ):
        pytest.skip(f"{operation} capability unavailable: {error}")
    raise error


def symlink_or_skip(
    link: Path,
    target: Path,
    *,
    target_is_directory: bool = False,
) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as error:
        _skip_if_capability_unavailable("symbolic-link", error)


def mkfifo_or_skip(path: Path) -> None:
    try:
        os.mkfifo(path)
    except (AttributeError, NotImplementedError, OSError) as error:
        _skip_if_capability_unavailable("FIFO", error)
