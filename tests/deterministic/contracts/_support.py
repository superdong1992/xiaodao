from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_ROOT = REPOSITORY_ROOT / "schemas" / "v2"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "contracts"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def schema_validator(schema_name: str) -> Draft202012Validator:
    schema = load_json(SCHEMA_ROOT / schema_name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def public_value(value: Any, name: str) -> Any:
    """Read a public field from a mapping, dataclass, or Pydantic model."""

    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)


def enum_values(enum_type: type[Any]) -> tuple[str, ...]:
    return tuple(member.value for member in enum_type)
