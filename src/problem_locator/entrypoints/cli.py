"""S06 command-line implementation; S08 wires it from package ``__main__``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from pydantic import TypeAdapter, ValidationError

from problem_locator.contracts.enums import ErrorCode
from problem_locator.contracts.errors import (
    ApplicationPortError,
    CLI_EXIT_CONFIG_OR_STATE_CORRUPT,
    CLI_EXIT_REQUEST_OR_STATE_CONFLICT,
    CLI_EXIT_RUNTIME_FAILURE,
    CLI_EXIT_SUCCESS,
)
from problem_locator.contracts.models import ApplicationError, OpaqueId
from problem_locator.contracts.ports import StateAdminPort
from problem_locator.contracts.serialization import canonical_json_bytes
from problem_locator.diagnostics import configure_diagnostics, log_event
from problem_locator.journey import configure_journey
from problem_locator.journey_renderer import (
    JourneyCaseNotFound,
    JourneyOutputError,
    JourneySourceError,
    render_journey,
)
from problem_locator.interfaces.error_mapping import cli_exit_for, validation_error
from problem_locator.runtime.methods_replay_v2 import (
    MethodValidationReplayErrorCodeV2,
    MethodValidationReplayErrorV2,
    MethodValidationReplayReceiptV2,
)

from .replay import (
    MethodValidationReplayRequestV2,
    ReplayError,
    ReplayMode,
    ReplayRequest,
    ReplayResult,
)
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
    replay_runner: Callable[[ReplayRequest, Settings], ReplayResult] | None = None
    method_validation_replay_runner: (
        Callable[
            [MethodValidationReplayRequestV2],
            MethodValidationReplayReceiptV2,
        ]
        | None
    ) = None


_DEFAULT_HOOKS: CliHooks | None = None


_METHOD_VALIDATION_REPLAY_ERROR_MESSAGES = {
    MethodValidationReplayErrorCodeV2.RECORD_READ_FAILED: (
        "读取 Evidence V2 执行记录失败。"
    ),
    MethodValidationReplayErrorCodeV2.CORE_RECORD_INVALID: (
        "Evidence V2 核心记录无效。"
    ),
    MethodValidationReplayErrorCodeV2.JOB_NOT_FOUND: "找不到指定的 Job。",
    MethodValidationReplayErrorCodeV2.STATE_NOT_FOUND: (
        "找不到校验重放所需的 Methods V2 State。"
    ),
    MethodValidationReplayErrorCodeV2.EVIDENCE_GRAPH_NOT_FOUND: (
        "找不到校验重放所需的 Evidence Graph。"
    ),
    MethodValidationReplayErrorCodeV2.EVALUATION_PLAN_NOT_FOUND: (
        "找不到校验重放所需的 Evaluation Plan。"
    ),
    MethodValidationReplayErrorCodeV2.WORKFLOW_MISMATCH: (
        "已保存的 Evidence V2 记录不属于同一定位流程。"
    ),
    MethodValidationReplayErrorCodeV2.REJECTED_ATTEMPT_NOT_FOUND: (
        "找不到指定的 Evidence V2 被拒响应。"
    ),
    MethodValidationReplayErrorCodeV2.REJECTION_NOT_REPRODUCED: (
        "当前校验器无法复现这条响应原先的拒绝结果。"
    ),
}


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
    uvicorn.run(
        app,
        host=host,
        port=port,
        workers=1,
        log_config=None,
        access_log=False,
    )


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

    render = subcommands.add_parser(
        "render-journey",
        help="render detailed and brief logs for one Case",
    )
    render.add_argument("--case-id", required=True)
    render.add_argument("--log-dir", type=Path)

    replay = subcommands.add_parser(
        "replay-job",
        help="replay one State V8 Job in a new isolated installation",
    )
    replay.add_argument("--source-data-root", type=Path, required=True)
    replay.add_argument("--job-id", required=True)
    replay.add_argument(
        "--mode",
        choices=[item.value for item in ReplayMode],
        required=True,
    )
    replay.add_argument("--output-dir", type=Path, required=True)
    replay.add_argument("--env-file", type=Path)
    replay.add_argument("--skill-dir", type=Path)

    method_validation_replay = subcommands.add_parser(
        "replay-method-rejection",
        help="使用当前校验器重放一条已持久化的 Methods V2 被拒响应",
    )
    method_validation_replay.add_argument("--data-root", type=Path, required=True)
    method_validation_replay.add_argument("--job-id", required=True)
    method_validation_replay.add_argument(
        "--role",
        choices=("SPECIALIST", "REVIEWER"),
        required=True,
    )
    method_validation_replay.add_argument(
        "--attempt",
        choices=("PRIMARY", "REPAIR"),
        required=True,
    )
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


def _request_error(message: str) -> ApplicationError:
    return ApplicationError(
        code=ErrorCode.VALIDATION_ERROR,
        message=message,
        details=[],
        retryable=False,
    )


def _runtime_error(message: str) -> ApplicationError:
    return ApplicationError(
        code=ErrorCode.RESOURCE_PUBLISH_FAILED,
        message=message,
        details=[],
        retryable=False,
    )


def _render_journey_command(
    arguments: argparse.Namespace,
    *,
    output: BinaryIO,
    errors: BinaryIO,
) -> int:
    if "DFX_LOG_FILE" in os.environ:
        _write_error(
            errors,
            _config_error("DFX_LOG_FILE is no longer supported; use DFX_LOG_DIR."),
        )
        return CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    raw_log_dir = arguments.log_dir or os.environ.get("DFX_LOG_DIR")
    if raw_log_dir is None or str(raw_log_dir) == "":
        _write_error(errors, _config_error("DFX_LOG_DIR is required."))
        return CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    log_dir = Path(raw_log_dir)
    if not log_dir.is_absolute():
        _write_error(errors, _config_error("DFX_LOG_DIR must be an absolute path."))
        return CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    try:
        case_id = TypeAdapter(OpaqueId).validate_python(
            arguments.case_id,
            strict=True,
        )
    except (TypeError, ValueError, ValidationError):
        _write_error(errors, _request_error("case_id must be a canonical lowercase UUID."))
        return CLI_EXIT_REQUEST_OR_STATE_CONFLICT

    try:
        receipt = render_journey(log_dir, case_id)
    except JourneyCaseNotFound as exc:
        _write_error(errors, _request_error(str(exc)))
        return CLI_EXIT_REQUEST_OR_STATE_CONFLICT
    except JourneySourceError as exc:
        _write_error(errors, _config_error(str(exc)))
        return CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    except JourneyOutputError as exc:
        _write_error(errors, _runtime_error(str(exc)))
        return CLI_EXIT_RUNTIME_FAILURE
    except Exception:
        _write_error(errors, _runtime_error("Journey rendering failed unexpectedly."))
        return CLI_EXIT_RUNTIME_FAILURE
    output.write(canonical_json_bytes(receipt))
    output.flush()
    return CLI_EXIT_SUCCESS


def _replay_job_command(
    arguments: argparse.Namespace,
    hooks: CliHooks,
    *,
    output: BinaryIO,
    errors: BinaryIO,
) -> int:
    if hooks.replay_runner is None:
        _write_error(errors, _config_error("Replay composition is not configured."))
        return CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    try:
        job_id = TypeAdapter(OpaqueId).validate_python(arguments.job_id, strict=True)
        source_data_root = Path(arguments.source_data_root)
        output_dir = Path(arguments.output_dir)
        if not source_data_root.is_absolute() or not output_dir.is_absolute():
            raise ValueError("replay roots must be absolute")
        environment = dict(os.environ)
        # Explicit replay paths own these two settings; an env file need not
        # repeat DATA_ROOT and --skill-dir is an intentional one-run override.
        environment["DATA_ROOT"] = str(source_data_root)
        if arguments.skill_dir is not None:
            skill_dir = Path(arguments.skill_dir)
            if not skill_dir.is_absolute():
                raise ValueError("skill-dir must be absolute")
            environment["SKILL_DIR"] = str(skill_dir)
        settings = Settings.load(
            env_file=arguments.env_file,
            environ=environment,
        )
        request = ReplayRequest(
            source_data_root=source_data_root,
            job_id=job_id,
            mode=ReplayMode(arguments.mode),
            output_dir=output_dir,
        )
        result = hooks.replay_runner(request, settings)
    except SettingsError:
        _write_error(errors, _config_error("Replay configuration is invalid."))
        return CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    except ReplayError as exc:
        _write_error(errors, exc.error)
        return cli_exit_for(exc.error)
    except ApplicationPortError as exc:
        _write_error(errors, exc.error)
        return cli_exit_for(exc.error)
    except (OSError, TypeError, ValueError, ValidationError):
        _write_error(errors, _request_error("Replay request is invalid."))
        return CLI_EXIT_REQUEST_OR_STATE_CONFLICT
    except Exception:
        error = _runtime_error("Replay failed unexpectedly.")
        _write_error(errors, error)
        return CLI_EXIT_RUNTIME_FAILURE

    output.write(canonical_json_bytes(result))
    output.flush()
    return CLI_EXIT_SUCCESS if result.success else CLI_EXIT_RUNTIME_FAILURE


def _method_validation_replay_command(
    arguments: argparse.Namespace,
    hooks: CliHooks,
    *,
    output: BinaryIO,
    errors: BinaryIO,
) -> int:
    runner = hooks.method_validation_replay_runner
    if runner is None:
        _write_error(
            errors,
            _config_error("Methods V2 校验重放功能尚未配置。"),
        )
        return CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    try:
        job_id = TypeAdapter(OpaqueId).validate_python(arguments.job_id, strict=True)
        data_root = Path(arguments.data_root)
        if not data_root.is_absolute():
            raise ValueError("data-root must be absolute")
        request = MethodValidationReplayRequestV2(
            data_root=data_root,
            job_id=job_id,
            role=arguments.role,
            attempt=arguments.attempt,
        )
        receipt = runner(request)
    except MethodValidationReplayErrorV2 as exc:
        errors.write(
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "status": "ERROR",
                    "code": exc.code.value,
                    "message": _METHOD_VALIDATION_REPLAY_ERROR_MESSAGES[exc.code],
                    "job_id": exc.job_id,
                    "role": exc.role,
                    "attempt": exc.attempt,
                }
            )
        )
        errors.flush()
        if exc.code is MethodValidationReplayErrorCodeV2.RECORD_READ_FAILED:
            return CLI_EXIT_RUNTIME_FAILURE
        if exc.code is MethodValidationReplayErrorCodeV2.CORE_RECORD_INVALID:
            return CLI_EXIT_CONFIG_OR_STATE_CORRUPT
        return CLI_EXIT_REQUEST_OR_STATE_CONFLICT
    except ApplicationPortError as exc:
        _write_error(errors, exc.error)
        return cli_exit_for(exc.error)
    except (OSError, TypeError, ValueError, ValidationError):
        _write_error(
            errors,
            _request_error("Methods V2 校验重放请求无效。"),
        )
        return CLI_EXIT_REQUEST_OR_STATE_CONFLICT
    except Exception:
        _write_error(
            errors,
            _runtime_error("Methods V2 校验重放执行失败。"),
        )
        return CLI_EXIT_RUNTIME_FAILURE

    payload = TypeAdapter(MethodValidationReplayReceiptV2).dump_python(
        receipt,
        mode="json",
    )
    output.write(canonical_json_bytes({"schema_version": 1, **payload}))
    output.flush()
    return CLI_EXIT_SUCCESS


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

    if arguments.command == "render-journey":
        return _render_journey_command(arguments, output=output, errors=errors)

    active_hooks = hooks or _DEFAULT_HOOKS
    if active_hooks is None:
        _write_error(errors, _config_error("CLI composition is not configured."))
        return CLI_EXIT_CONFIG_OR_STATE_CORRUPT

    if arguments.command == "replay-job":
        return _replay_job_command(
            arguments,
            active_hooks,
            output=output,
            errors=errors,
        )

    if arguments.command == "replay-method-rejection":
        return _method_validation_replay_command(
            arguments,
            active_hooks,
            output=output,
            errors=errors,
        )

    if arguments.command == "serve":
        configure_diagnostics("INFO")
        configure_journey()
        try:
            settings = Settings.load(env_file=arguments.env_file)
            try:
                if settings.dfx_log_dir is None:
                    configure_diagnostics(settings.dfx_log_level)
                    configure_journey()
                else:
                    configure_diagnostics(
                        settings.dfx_log_level,
                        log_file=settings.dfx_log_dir / "debug.jsonl",
                    )
                    configure_journey(
                        log_file=settings.dfx_log_dir / "journey.jsonl"
                    )
            except (OSError, ValueError) as exc:
                raise SettingsError("DFX log directory could not be opened") from exc
            log_event(
                "service.configuration.loaded",
                bind_host=settings.bind_host,
                port=settings.port,
                public_base_url=settings.public_base_url,
                dfx_log_level=settings.dfx_log_level,
                dfx_log_dir=(
                    str(settings.dfx_log_dir)
                    if settings.dfx_log_dir is not None
                    else None
                ),
            )
            app = active_hooks.app_factory(settings)
            active_hooks.server_runner(app, settings.bind_host, settings.port, 1)
        except SettingsError as exc:
            log_event(
                "service.configuration.invalid",
                level=logging.ERROR,
                error=exc,
            )
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
