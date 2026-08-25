from __future__ import annotations

# Runtime support shared by the first-party CrossJob adapters.

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_explicit_source_root = os.environ.get("TEST_FLOW_SOURCE_ROOT")
if _explicit_source_root:
    explicit_source = Path(_explicit_source_root) / "src"
    if not explicit_source.is_absolute() or not (explicit_source / "problem_locator").is_dir():
        raise RuntimeError("TEST_FLOW_SOURCE_ROOT is invalid")
    sys.path.insert(0, str(explicit_source))

try:
    from problem_locator.bootstrap import _create_test_app, cli_hooks
    from problem_locator.entrypoints.cli import CliHooks, main as cli_main
    from problem_locator.entrypoints.settings import Settings
except ModuleNotFoundError as exc:
    if exc.name != "problem_locator":
        raise
    source_root = Path(__file__).resolve().parents[3] / "src"
    if not (source_root / "problem_locator").is_dir():
        raise
    sys.path.insert(0, str(source_root))
    from problem_locator.bootstrap import _create_test_app, cli_hooks  # noqa: E402
    from problem_locator.entrypoints.cli import (  # noqa: E402
        CliHooks,
        main as cli_main,
    )
    from problem_locator.entrypoints.settings import Settings  # noqa: E402


def _test_app_factory(settings: Settings) -> Any:
    return _create_test_app(settings)


def _test_cli_hooks() -> CliHooks:
    production = cli_hooks()
    return CliHooks(
        state_admin_factory=production.state_admin_factory,
        app_factory=_test_app_factory,
        server_runner=production.server_runner,
        atomic_writer=production.atomic_writer,
        replay_runner=production.replay_runner,
    )


def main(argv: Sequence[str] | None = None) -> int:
    return cli_main(argv, hooks=_test_cli_hooks())


if __name__ == "__main__":
    raise SystemExit(main())
