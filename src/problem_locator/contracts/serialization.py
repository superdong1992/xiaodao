"""Canonical serialization and deterministic schema/manifest generation.

The functions in this module are deliberately side-effect free.  Callers may
write the returned bytes to disk, while contract tests can regenerate the
entire bundle in memory and compare it byte-for-byte with the frozen files.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter

from .limits import CONTRACT_REVISION, GENERATOR_VERSION, SCHEMA_VERSION

_T = TypeVar("_T")


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is forbidden: {key!r}")
        result[key] = value
    return result


def _json_compatible(value: Any) -> Any:
    """Return the Pydantic JSON-mode representation used by every hash."""

    if isinstance(value, BaseModel):
        return value.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
            exclude_unset=False,
        )
    # Plain JSON-like values must go directly to ``json.dumps`` so its
    # ``allow_nan=False`` guard observes NaN/Infinity instead of a framework
    # serializer first normalizing them to null.
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Encode *value* using the V1 Canonical JSON profile.

    The result is UTF-8 without a BOM, uses code-point-sorted object keys and
    compact separators, rejects NaN/Infinity, and ends in exactly one LF.
    """

    text = json.dumps(
        _json_compatible(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return text.encode("utf-8") + b"\n"


def canonical_json_sha256(value: Any) -> str:
    """Return the lowercase SHA-256 of :func:`canonical_json_bytes`."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def hash_excluded_fields(model_type: type[BaseModel]) -> tuple[str, ...]:
    """Return the frozen request-hash exclusions declared by a command Schema."""

    schema = model_type.model_json_schema(mode="validation")
    value = schema.get("hash_excluded_fields")
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"{model_type.__name__} does not declare hash_excluded_fields")
    if value != sorted(set(value)):
        raise ValueError("hash_excluded_fields must be sorted and unique")
    unknown = set(value) - set(model_type.model_fields)
    if unknown:
        raise ValueError(f"hash exclusions name unknown fields: {sorted(unknown)!r}")
    return tuple(value)


def business_request_preimage(command: BaseModel) -> dict[str, Any]:
    """Project an external write command to its canonical idempotency preimage."""

    if not isinstance(command, BaseModel):
        raise TypeError("command must be a Pydantic contract model")
    excluded = set(hash_excluded_fields(type(command)))
    return command.model_dump(
        mode="json",
        by_alias=True,
        exclude=excluded,
        exclude_none=False,
        exclude_unset=False,
    )


def business_request_sha256(command: BaseModel) -> str:
    """Hash a command after applying its Schema-declared exclusions."""

    return canonical_json_sha256(business_request_preimage(command))


def bytes_sha256(data: bytes) -> str:
    """Return the lowercase SHA-256 of immutable bytes."""

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    return hashlib.sha256(data).hexdigest()


def parse_canonical_json_bytes(
    data: bytes,
    model_type: type[_T] | None = None,
) -> _T | Any:
    """Parse canonical bytes and optionally validate them as ``model_type``.

    Semantically valid JSON with any non-canonical spelling is rejected before
    model validation, which keeps execution records and manifest hashes stable.
    """

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if data.startswith(b"\xef\xbb\xbf"):
        raise ValueError("a UTF-8 BOM is forbidden")
    try:
        parsed = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid UTF-8 JSON bytes") from exc
    if canonical_json_bytes(parsed) != data:
        raise ValueError("JSON bytes are not canonical")
    if model_type is None:
        return parsed
    return TypeAdapter(model_type).validate_python(parsed)


def is_canonical_json_bytes(data: bytes) -> bool:
    """Return whether *data* is valid V1 Canonical JSON."""

    try:
        parse_canonical_json_bytes(data)
    except (TypeError, ValueError):
        return False
    return True


def schema_document(model_type: Any) -> dict[str, Any]:
    """Generate the deterministic Draft 2020-12 schema for a public root."""

    schema = TypeAdapter(model_type).json_schema(
        mode="validation",
        ref_template="#/$defs/{model}",
    )
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}


def schema_bundle_bytes(
    schema_models: Mapping[str, Any] | None = None,
) -> dict[str, bytes]:
    """Return ``*.schema.json`` names mapped to stable canonical bytes."""

    if schema_models is None:
        from problem_locator.contracts import SCHEMA_MODELS

        schema_models = SCHEMA_MODELS
    expected_names = sorted(schema_models)
    if any(not name.endswith(".schema.json") for name in expected_names):
        raise ValueError("schema bundle keys must end with .schema.json")
    return {
        name: canonical_json_bytes(schema_document(schema_models[name]))
        for name in expected_names
    }


def contract_manifest(repo_root: Path) -> dict[str, Any]:
    """Build the S00 manifest from the exact frozen include/exclude rules."""

    root = repo_root.resolve()
    contracts_root = root / "src" / "problem_locator" / "contracts"
    schemas_root = root / "schemas" / "v1"
    paths = [
        path
        for path in contracts_root.rglob("*.py")
        if path.is_file() and not path.is_symlink()
    ]
    paths.extend(
        path
        for path in schemas_root.glob("*.schema.json")
        if path.is_file()
        and not path.is_symlink()
        and path.name != "contract-manifest.json"
    )
    entries: list[dict[str, str]] = []
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        entries.append({"path": relative, "sha256": bytes_sha256(path.read_bytes())})
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "generator_version": GENERATOR_VERSION,
        "files": entries,
    }


def contract_manifest_bytes(repo_root: Path) -> bytes:
    """Return stable bytes for ``schemas/v1/contract-manifest.json``."""

    return canonical_json_bytes(contract_manifest(repo_root))


__all__ = [
    "CONTRACT_REVISION",
    "GENERATOR_VERSION",
    "SCHEMA_VERSION",
    "bytes_sha256",
    "business_request_preimage",
    "business_request_sha256",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "contract_manifest",
    "contract_manifest_bytes",
    "is_canonical_json_bytes",
    "hash_excluded_fields",
    "parse_canonical_json_bytes",
    "schema_bundle_bytes",
    "schema_document",
]
