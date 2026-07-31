"""UTF-8 dotenv loading with process-environment precedence."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


class EnvFileError(ValueError):
    """A dotenv source could not be read without exposing its values."""


def load_env_file(path: Path) -> dict[str, str]:
    """Load one UTF-8 dotenv file without mutating ``os.environ``."""

    try:
        from dotenv import dotenv_values
        from dotenv.parser import parse_stream
    except ImportError as exc:  # pragma: no cover - exercised by dependency gate
        raise EnvFileError("dotenv support is not installed") from exc

    try:
        if not path.is_file():
            raise EnvFileError("the env file does not exist or is not a regular file")
        with path.open("r", encoding="utf-8") as source:
            if any(binding.error for binding in parse_stream(source)):
                raise EnvFileError("the env file contains invalid dotenv syntax")
        values = dotenv_values(path, encoding="utf-8", interpolate=True)
    except EnvFileError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise EnvFileError("the env file could not be loaded") from exc
    result: dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            raise EnvFileError("the env file contains a key without a value")
        result[key] = value
    return result


def merged_environment(
    env_file: Path | None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return dotenv values overlaid by the already-running process environment."""

    result = {} if env_file is None else load_env_file(env_file)
    result.update(dict(os.environ if environ is None else environ))
    return result


__all__ = ["EnvFileError", "load_env_file", "merged_environment"]
