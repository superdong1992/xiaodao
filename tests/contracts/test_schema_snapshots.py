from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterator

from problem_locator.contracts import (
    CONTRACT_REVISION,
    GENERATOR_VERSION,
    SCHEMA_MODELS,
    SCHEMA_VERSION,
)
from problem_locator.contracts.serialization import (
    canonical_json_bytes,
    is_canonical_json_bytes,
    schema_document,
)

from tests.contracts._support import REPOSITORY_ROOT, SCHEMA_ROOT, load_json


EXPECTED_SCHEMA_NAMES = {
    "agent-job-outcome-draft.schema.json",
    "agent-job-outcome.schema.json",
    "fixture-manifest.schema.json",
    "handoff.schema.json",
    "job-outcome.schema.json",
    "job.schema.json",
    "logparse-parse-claim.schema.json",
    "state.schema.json",
    "user-result.schema.json",
    "workspace-input-manifest.schema.json",
}


def _walk_json(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def test_schema_registry_is_the_exact_frozen_public_set() -> None:
    assert set(SCHEMA_MODELS) == EXPECTED_SCHEMA_NAMES
    assert SCHEMA_VERSION == 2
    assert CONTRACT_REVISION == "v2-contract-r1"
    assert isinstance(GENERATOR_VERSION, str)
    assert GENERATOR_VERSION.strip()


def test_generated_schema_snapshots_are_byte_stable() -> None:
    for name, model_type in sorted(SCHEMA_MODELS.items()):
        path = SCHEMA_ROOT / name
        actual = path.read_bytes()
        expected = canonical_json_bytes(schema_document(model_type))
        assert actual == expected, f"regenerate {path.relative_to(REPOSITORY_ROOT)}"
        assert is_canonical_json_bytes(actual)


def test_public_model_objects_reject_undeclared_fields() -> None:
    """Every object with declared DTO properties must freeze its field set.

    Mapping values intentionally use ``additionalProperties`` as a schema, so
    this assertion is limited to object nodes that declare named properties.
    """

    for name in EXPECTED_SCHEMA_NAMES:
        schema = load_json(SCHEMA_ROOT / name)
        for node in _walk_json(schema):
            if isinstance(node, dict) and "properties" in node:
                assert node.get("additionalProperties") is False, (
                    f"{name} contains an extensible public object: {node.get('title')}"
                )


def test_contract_manifest_covers_the_exact_frozen_inputs() -> None:
    path = SCHEMA_ROOT / "contract-manifest.json"
    raw = path.read_bytes()
    manifest = load_json(path)

    assert is_canonical_json_bytes(raw)
    assert set(manifest) == {
        "contract_revision",
        "files",
        "generator_version",
        "schema_version",
    }
    assert manifest["schema_version"] == 2
    assert manifest["contract_revision"] == CONTRACT_REVISION
    assert manifest["generator_version"] == GENERATOR_VERSION

    contract_root = REPOSITORY_ROOT / "src" / "problem_locator" / "contracts"
    included_paths = sorted(
        [
            file.relative_to(REPOSITORY_ROOT).as_posix()
            for file in contract_root.rglob("*.py")
            if file.is_file() and not file.is_symlink()
        ]
        + [
            file.relative_to(REPOSITORY_ROOT).as_posix()
            for file in SCHEMA_ROOT.glob("*.schema.json")
            if file.is_file() and not file.is_symlink()
        ]
    )

    entries = manifest["files"]
    assert [entry["path"] for entry in entries] == included_paths
    assert len(included_paths) == len(set(included_paths))
    for entry in entries:
        assert set(entry) == {"path", "sha256"}
        file_path = REPOSITORY_ROOT / Path(entry["path"])
        assert entry["sha256"] == hashlib.sha256(file_path.read_bytes()).hexdigest()


def test_contract_manifest_excludes_self_and_post_freeze_inputs() -> None:
    paths = {
        entry["path"]
        for entry in load_json(SCHEMA_ROOT / "contract-manifest.json")["files"]
    }
    assert "schemas/v2/contract-manifest.json" not in paths
    assert "pyproject.toml" not in paths
    assert "uv.lock" not in paths
    assert not any(path.startswith("tests/") for path in paths)
    assert not any(path.startswith("handoff/") for path in paths)
