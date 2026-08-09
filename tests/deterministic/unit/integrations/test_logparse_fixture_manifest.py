from __future__ import annotations

import hashlib
import json
from pathlib import Path

from problem_locator.contracts import FixtureManifest
from problem_locator.contracts.serialization import is_canonical_json_bytes


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests/fixtures/components/logparse"
MANIFEST_PATH = FIXTURE_ROOT / "fixture-manifest.json"
SCHEMA_PATH = REPOSITORY_ROOT / "schemas/v2/fixture-manifest.schema.json"


def _ordinary_fixture_files() -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in FIXTURE_ROOT.rglob("*"):
        assert not path.is_symlink(), f"fixture links are forbidden: {path}"
        if not path.is_file() or path == MANIFEST_PATH:
            continue
        files[path.relative_to(FIXTURE_ROOT).as_posix()] = path
    return dict(sorted(files.items()))


def _validate_json_schema_if_available(
    schema: dict[str, object], payload: dict[str, object]
) -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ModuleNotFoundError:
        # The S07 acceptance interpreter intentionally has no jsonschema
        # dependency. DTO and byte-for-byte fixture checks below remain
        # mandatory; environments that provide jsonschema also enforce the
        # frozen Draft 2020-12 schema here.
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["title"] == "FixtureManifest"
        return
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


def test_logparse_fixture_manifest_matches_schema_dto_and_disk() -> None:
    raw = MANIFEST_PATH.read_bytes()
    payload = json.loads(raw)
    schema = json.loads(SCHEMA_PATH.read_bytes())

    assert is_canonical_json_bytes(raw)
    _validate_json_schema_if_available(schema, payload)
    manifest = FixtureManifest.model_validate(payload)

    assert manifest.schema_version == 1
    assert manifest.owner_spec == "S07"
    assert manifest.root == "tests/fixtures/components/logparse"

    paths = [entry.path for entry in manifest.files]
    actual = _ordinary_fixture_files()
    assert paths == sorted(paths)
    assert len(paths) == len(set(paths))
    assert paths == list(actual)
    assert "fixture-manifest.json" not in paths

    for entry in manifest.files:
        data = actual[entry.path].read_bytes()
        assert entry.purpose.strip()
        assert entry.size == len(data)
        assert entry.sha256 == hashlib.sha256(data).hexdigest()
        if entry.schema_ref is not None:
            assert (REPOSITORY_ROOT / entry.schema_ref).is_file()
