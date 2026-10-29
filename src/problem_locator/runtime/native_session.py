"""Explicit native CLI session binding, owned by one application conversation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID


def validated_session_id(value: object) -> str:
    """Accept a canonical UUID without including an invalid value in errors."""
    if not isinstance(value, str):
        raise ValueError("Agent 原生会话标识无效。")
    try:
        canonical = str(UUID(value))
    except ValueError:
        raise ValueError("Agent 原生会话标识无效。") from None
    if value.lower() != canonical:
        raise ValueError("Agent 原生会话标识无效。")
    return canonical


@dataclass(frozen=True, slots=True)
class NativeSession:
    config_dir: Path = field(repr=False)
    resume_id: str | None = field(default=None, repr=False)
    on_session_id: Callable[[str], None] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.resume_id is not None:
            object.__setattr__(self, "resume_id", validated_session_id(self.resume_id))
