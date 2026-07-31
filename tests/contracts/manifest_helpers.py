from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


class FixtureManifestError(AssertionError):
    """Raised when a fixture manifest disagrees with its owned subtree."""


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise FixtureManifestError(f"unsafe fixture path: {value!r}")
    return path


def fixture_files(root: Path) -> dict[str, Path]:
    """Return all owned ordinary files except the self-hashing manifest."""

    result: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise FixtureManifestError(f"fixture links are forbidden: {path}")
        if not path.is_file() or path == root / "fixture-manifest.json":
            continue
        relative = path.relative_to(root).as_posix()
        _safe_relative_path(relative)
        result[relative] = path
    return dict(sorted(result.items()))


def build_fixture_manifest(
    root: Path,
    *,
    owner_spec: str,
    repository_relative_root: str,
    metadata_by_path: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build deterministic manifest data without writing to disk.

    Callers must supply purpose/schema metadata for every ordinary file. This
    prevents an unreviewed fixture from being silently assigned generic intent.
    """

    actual = fixture_files(root)
    if set(metadata_by_path) != set(actual):
        missing = sorted(set(actual) - set(metadata_by_path))
        extra = sorted(set(metadata_by_path) - set(actual))
        raise FixtureManifestError(
            f"fixture metadata mismatch; missing={missing!r}, extra={extra!r}"
        )

    files: list[dict[str, Any]] = []
    for relative, path in actual.items():
        metadata = metadata_by_path[relative]
        if set(metadata) != {"purpose", "schema_ref"}:
            raise FixtureManifestError(
                f"metadata for {relative!r} must contain purpose and schema_ref"
            )
        data = path.read_bytes()
        files.append(
            {
                "path": relative,
                "purpose": metadata["purpose"],
                "schema_ref": metadata["schema_ref"],
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )

    return {
        "files": files,
        "owner_spec": owner_spec,
        "root": repository_relative_root,
        "schema_version": 1,
    }


def assert_fixture_manifest_matches(root: Path, manifest: Mapping[str, Any]) -> None:
    """Check path safety, exact coverage, order, size, and content hashes."""

    if manifest.get("root") != "tests/fixtures/contracts":
        raise FixtureManifestError("contract fixture root is not canonical")
    if manifest.get("owner_spec") != "S00":
        raise FixtureManifestError("contract fixtures must be owned by S00")

    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise FixtureManifestError("manifest files must be an array")

    paths = [entry.get("path") for entry in entries]
    if any(not isinstance(path, str) for path in paths):
        raise FixtureManifestError("every fixture path must be a string")
    if len(paths) != len(set(paths)):
        raise FixtureManifestError("fixture paths are not unique")
    if paths != sorted(paths):
        raise FixtureManifestError("fixture paths are not Unicode-codepoint sorted")

    actual = fixture_files(root)
    if paths != list(actual):
        raise FixtureManifestError(
            f"fixture file set drift; manifest={paths!r}, actual={list(actual)!r}"
        )

    for entry in entries:
        relative = entry["path"]
        _safe_relative_path(relative)
        path = actual[relative]
        data = path.read_bytes()
        if entry.get("size") != len(data):
            raise FixtureManifestError(f"size drift for {relative}")
        if entry.get("sha256") != hashlib.sha256(data).hexdigest():
            raise FixtureManifestError(f"sha256 drift for {relative}")
        purpose = entry.get("purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            raise FixtureManifestError(f"empty purpose for {relative}")
        schema_ref = entry.get("schema_ref")
        if schema_ref is not None:
            schema_path = _safe_relative_path(schema_ref)
            if not (root.parents[2] / schema_path).is_file():
                raise FixtureManifestError(
                    f"schema_ref for {relative} does not exist: {schema_ref}"
                )
