"""S06 command-line implementation; S08 wires it from package ``__main__``."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from problem_locator.contracts.enums import ErrorCode
from problem_locator.contracts.errors import (
    ApplicationPortError,
    CLI_EXIT_CONFIG_OR_STATE_CORRUPT,
    CLI_EXIT_REQUEST_OR_STATE_CONFLICT,
    CLI_EXIT_SUCCESS,
)
from problem_locator.contracts.models import ApplicationError
from problem_locator.contracts.ports import StateAdminPort
from problem_locator.contracts.serialization import canonical_json_bytes

from problem_locator.interfaces.error_mapping import cli_exit_for, validation_error

from .settings import Settings, SettingsError


class CliUsageError(ValueError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


@dataclass(frozen=True, slots=True)
class CliHooks:
    state_admin_factory: Callable[[Path], StateAdminPort]
    app_factory: Callable[[Settings], Any]
    server_runner: Callable[[Any, str, int, int], None]
    atomic_writer: Callable[[Path, bytes], None] | None = None


_DEFAULT_HOOKS: CliHooks | None = None


def set_default_hooks(hooks: CliHooks) -> None:
    """Allow S08's package entrypoint to install the real composition once."""

    global _DEFAULT_HOOKS
    if _DEFAULT_HOOKS is not None and _DEFAULT_HOOKS is not hooks:
        raise RuntimeError("CLI hooks are already configured")
    _DEFAULT_HOOKS = hooks


def run_uvicorn(app: Any, host: str, port: int, workers: int) -> None:
    if workers != 1:
        raise ValueError("Problem Locator V1 requires exactly one Uvicorn worker")
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - dependency gate
        raise SettingsError("Uvicorn is not installed") from exc
    uvicorn.run(app, host=host, port=port, workers=1)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="problem-locator")
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve = subcommands.add_parser("serve", help="run the single-worker service")
    serve.add_argument("--env-file", type=Path)

    validate = subcommands.add_parser(
        "validate-state",
        help="validate the configured state without modifying it",
    )
    validate.add_argument("--data-root", type=Path, required=True)

    export = subcommands.add_parser(
        "export-state",
        help="atomically export one canonical state snapshot",
    )
    export.add_argument("--data-root", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    return parser


def _write_error(stream: BinaryIO, error: ApplicationError) -> None:
    stream.write(canonical_json_bytes(error))
    stream.flush()


def _config_error(message: str = "Configuration is invalid.") -> ApplicationError:
    return ApplicationError(
        code=ErrorCode.CONFIG_INVALID,
        message=message,
        details=[],
        retryable=False,
    )


def _atomic_write(output: Path, data: bytes) -> None:
    parent = output.parent
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as destination:
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, output)
        try:
            directory_fd = os.open(parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _path_is_within(path: Path, root: Path) -> bool:
    resolved_path = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def main(
    argv: Sequence[str] | None = None,
    *,
    hooks: CliHooks | None = None,
    stdout: BinaryIO | None = None,
    stderr: BinaryIO | None = None,
) -> int:
    output = sys.stdout.buffer if stdout is None else stdout
    errors = sys.stderr.buffer if stderr is None else stderr
    try:
        arguments = _parser().parse_args(argv)
    except CliUsageError:
        _write_error(errors, validation_error("Command-line arguments are invalid."))
        return CLI_EXIT_REQUEST_OR_STATE_CONFLICT

    active_hooks = hooks or _DEFAULT_HOOKS
    if active_hooks is None:
        _write_error(errors, _config_error("CLI composition is not configured."))
        return CLI_EXIT_CONFIG_OR_STATE_CORRUPT

    if arguments.command == "serve":
        try:
            settings = Settings.load(env_file=arguments.env_file)
            app = active_hooks.app_factory(settings)
            active_hooks.server_runner(app, settings.bind_host, settings.port, 1)
        except SettingsError:
            _write_error(errors, _config_error())
            return CLI_EXIT_CONFIG_OR_STATE_CORRUPT
        return CLI_EXIT_SUCCESS

    data_root: Path = arguments.data_root
    if not data_root.is_absolute():
        _write_error(errors, _config_error("DATA_ROOT must be an absolute path."))
        return CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    if arguments.command == "export-state":
        try:
            if _path_is_within(arguments.output, data_root):
                raise ValueError("export output overlaps DATA_ROOT")
        except (OSError, RuntimeError, ValueError):
            _write_error(errors, _config_error("Export output must be outside DATA_ROOT."))
            return CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    admin = active_hooks.state_admin_factory(data_root)

    if arguments.command == "validate-state":
        report = admin.validate_state()
        output.write(canonical_json_bytes(report))
        output.flush()
        return CLI_EXIT_SUCCESS if report.valid else CLI_EXIT_CONFIG_OR_STATE_CORRUPT

    assert arguments.command == "export-state"
    try:
        exported = admin.export_state()
    except ApplicationPortError as exc:
        _write_error(errors, exc.error)
        return cli_exit_for(exc.error)
    try:
        writer = active_hooks.atomic_writer or _atomic_write
        writer(arguments.output, exported)
    except OSError:
        _write_error(errors, _config_error("Export output could not be written."))
        return CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    return CLI_EXIT_SUCCESS


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess acceptance
    raise SystemExit(main())


__all__ = ["CliHooks", "main", "run_uvicorn", "set_default_hooks"]
