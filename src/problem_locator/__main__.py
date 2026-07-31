"""Side-effect-free package entrypoint."""

from __future__ import annotations

from collections.abc import Sequence
from typing import BinaryIO


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: BinaryIO | None = None,
    stderr: BinaryIO | None = None,
) -> int:
    from problem_locator.bootstrap import main as bootstrap_main

    return bootstrap_main(argv, stdout=stdout, stderr=stderr)


if __name__ == "__main__":  # pragma: no cover - covered by subprocess gates
    raise SystemExit(main())


__all__ = ["main"]
