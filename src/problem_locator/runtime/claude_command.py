"""Parse and prepare the external ``CLAUDE_COMMAND`` invocation.

The command is deliberately kept as one configuration string.  This module
reproduces the frozen ``issue-locator`` tokenisation rules without invoking a
shell, then removes every ambient logparse capability before optionally adding
the capability for the current Job.
"""

from __future__ import annotations

import ntpath
import os
import re
import shlex
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from problem_locator.contracts import ErrorCode


_ENVIRONMENT_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RAW_LOGPARSE_KEYS = frozenset(
    {
        "logparse_repo",
        "logparse_config_path",
        "logparse_python",
    }
)
_BROKER_PREFIX = "problem_locator_logparse_"
_BROKER_ENVIRONMENT_KEYS = frozenset(
    {
        "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT",
        "PROBLEM_LOCATOR_LOGPARSE_TOKEN",
    }
)

WhichResolver = Callable[..., str | None]


class ClaudeCommandError(ValueError):
    """A safe, typed configuration error for ``CLAUDE_COMMAND``.

    Messages intentionally never include the command, environment values, or
    broker credentials.
    """

    code = ErrorCode.CONFIG_INVALID


@dataclass(frozen=True, slots=True, repr=False)
class ClaudeCommand:
    """A shell-free argv/environment pair ready for process creation."""

    argv: tuple[str, ...]
    environment: dict[str, str]

    def __repr__(self) -> str:
        return (
            "ClaudeCommand("
            f"argv_tokens={len(self.argv)}, "
            f"environment_variables={len(self.environment)})"
        )


def _is_reserved_logparse_key(name: str) -> bool:
    folded = name.casefold()
    return folded in _RAW_LOGPARSE_KEYS or folded.startswith(_BROKER_PREFIX)


def _strip_one_quote_pair(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        return token[1:-1]
    return token


def _validate_environment_entry(name: object, value: object) -> tuple[str, str]:
    if not isinstance(name, str) or not isinstance(value, str):
        raise ClaudeCommandError("The process environment is invalid.")
    if not name or "\x00" in name or "=" in name or "\x00" in value:
        raise ClaudeCommandError("The process environment is invalid.")
    return name, value


def parse_command_tokens(
    command: str,
    *,
    os_name: str | None = None,
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Return the executable argv and continuous leading env assignments.

    ``shlex.split`` is applied with the platform's frozen ``posix`` flag.  One
    additional matching quote pair is removed from every resulting token,
    matching the historical launcher behaviour.
    """

    if not isinstance(command, str) or "\x00" in command:
        raise ClaudeCommandError("CLAUDE_COMMAND is invalid.")
    target_os = os.name if os_name is None else os_name
    try:
        tokens = [
            _strip_one_quote_pair(token)
            for token in shlex.split(command, posix=target_os != "nt")
        ]
    except (TypeError, ValueError) as exc:
        raise ClaudeCommandError("CLAUDE_COMMAND cannot be parsed.") from exc

    assignments: dict[str, str] = {}
    executable_index = 0
    for token in tokens:
        if "=" not in token:
            break
        name, value = token.split("=", 1)
        if _ENVIRONMENT_ASSIGNMENT.fullmatch(name) is None:
            raise ClaudeCommandError(
                "CLAUDE_COMMAND has an invalid leading environment assignment."
            )
        if _is_reserved_logparse_key(name):
            raise ClaudeCommandError(
                "CLAUDE_COMMAND cannot assign reserved logparse variables."
            )
        if "\x00" in value:
            raise ClaudeCommandError("CLAUDE_COMMAND has an invalid assignment.")
        assignments[name] = _strip_one_quote_pair(value)
        executable_index += 1

    argv = tuple(tokens[executable_index:])
    if not argv or not argv[0]:
        raise ClaudeCommandError("CLAUDE_COMMAND must include an executable.")
    return argv, assignments


def sanitize_environment(
    parent_environment: Mapping[str, str],
    command_environment: Mapping[str, str],
    *,
    broker_environment: Mapping[str, str] | None = None,
    os_name: str | None = None,
) -> dict[str, str]:
    """Merge environments, remove ambient logparse keys, and add one session.

    Leading command assignments are checked before the merge, so an attempted
    reserved-key override cannot be hidden by case differences or by a later
    sanitisation pass.
    """

    target_os = os.name if os_name is None else os_name
    merged: dict[str, str] = {}
    for name, value in parent_environment.items():
        valid_name, valid_value = _validate_environment_entry(name, value)
        merged[valid_name] = valid_value

    for name, value in command_environment.items():
        valid_name, valid_value = _validate_environment_entry(name, value)
        if _ENVIRONMENT_ASSIGNMENT.fullmatch(valid_name) is None:
            raise ClaudeCommandError("CLAUDE_COMMAND has an invalid assignment.")
        if _is_reserved_logparse_key(valid_name):
            raise ClaudeCommandError(
                "CLAUDE_COMMAND cannot assign reserved logparse variables."
            )
        if target_os == "nt":
            for inherited_name in tuple(merged):
                if inherited_name.casefold() == valid_name.casefold():
                    del merged[inherited_name]
        merged[valid_name] = valid_value

    merged = {
        name: value
        for name, value in merged.items()
        if not _is_reserved_logparse_key(name)
    }

    if broker_environment is not None:
        if set(broker_environment) != _BROKER_ENVIRONMENT_KEYS:
            raise ClaudeCommandError("The logparse broker environment is invalid.")
        broker_values: dict[str, str] = {}
        for name, value in broker_environment.items():
            valid_name, valid_value = _validate_environment_entry(name, value)
            if not valid_value:
                raise ClaudeCommandError("The logparse broker environment is invalid.")
            broker_values[valid_name] = valid_value
        merged.update(broker_values)

    return merged


def _environment_path(environment: Mapping[str, str], *, windows: bool) -> str | None:
    if not windows:
        return environment.get("PATH")
    for name, value in environment.items():
        if name.casefold() == "path":
            return value
    return None


def _resolve_windows_shim(
    argv: tuple[str, ...],
    environment: Mapping[str, str],
    resolver: WhichResolver,
) -> tuple[str, ...]:
    executable = argv[0]
    basename = ntpath.basename(executable)
    if "/" in executable or "\\" in executable or "." in basename:
        return argv
    resolved = resolver(
        executable,
        path=_environment_path(environment, windows=True),
    )
    if resolved is None:
        return argv
    if not isinstance(resolved, str) or not resolved or "\x00" in resolved:
        raise ClaudeCommandError("The Windows command shim resolution is invalid.")
    return (resolved, *argv[1:])


def prepare_claude_command(
    command: str,
    *,
    parent_environment: Mapping[str, str] | None = None,
    broker_environment: Mapping[str, str] | None = None,
    os_name: str | None = None,
    which: WhichResolver | None = None,
) -> ClaudeCommand:
    """Prepare a shell-free Agent invocation using the frozen semantics."""

    target_os = os.name if os_name is None else os_name
    argv, command_environment = parse_command_tokens(command, os_name=target_os)
    environment = sanitize_environment(
        os.environ if parent_environment is None else parent_environment,
        command_environment,
        broker_environment=broker_environment,
        os_name=target_os,
    )
    if target_os == "nt":
        argv = _resolve_windows_shim(
            argv,
            environment,
            shutil.which if which is None else which,
        )
    return ClaudeCommand(argv=argv, environment=environment)


__all__ = [
    "ClaudeCommand",
    "ClaudeCommandError",
    "WhichResolver",
    "parse_command_tokens",
    "prepare_claude_command",
    "sanitize_environment",
]
