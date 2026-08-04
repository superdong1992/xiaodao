from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from problem_locator.contracts import (
    AttachmentFilenameSuffix,
    ErrorCode,
    FixtureManifest,
    StateFile,
    canonical_json_bytes,
    derive_attachment_filename_suffix,
    parse_canonical_json_bytes,
    workspace_attachment_relative_path,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "components" / "storage"
MANIFEST_PATH = FIXTURE_ROOT / "fixture-manifest.json"
STATE_SCHEMA_PATH = REPOSITORY_ROOT / "schemas" / "v1" / "state.schema.json"
FIXTURE_MANIFEST_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "v1" / "fixture-manifest.schema.json"
)
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000050"


def _fixture_bytes(relative: str) -> bytes:
    return (FIXTURE_ROOT / relative).read_bytes()


def _fixture_json(relative: str) -> object:
    return json.loads(_fixture_bytes(relative).decode("utf-8"))


def _schema_validator(path: Path) -> Draft202012Validator:
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_storage_fixture_manifest_covers_every_owned_byte() -> None:
    payload = parse_canonical_json_bytes(MANIFEST_PATH.read_bytes())
    _schema_validator(FIXTURE_MANIFEST_SCHEMA_PATH).validate(payload)
    manifest = FixtureManifest.model_validate(payload)
    assert manifest.owner_spec == "S02"
    assert manifest.root == "tests/fixtures/components/storage"

    actual_paths: list[str] = []
    for path in FIXTURE_ROOT.rglob("*"):
        assert not path.is_symlink()
        if path.is_file() and path != MANIFEST_PATH:
            actual_paths.append(path.relative_to(FIXTURE_ROOT).as_posix())
    actual_paths.sort()

    declared_paths = [entry.path for entry in manifest.files]
    assert declared_paths == actual_paths
    assert declared_paths == sorted(declared_paths)
    assert "fixture-manifest.json" not in declared_paths

    for entry in manifest.files:
        relative = PurePosixPath(entry.path)
        assert not relative.is_absolute()
        assert all(part not in {"", ".", ".."} for part in relative.parts)
        data = (FIXTURE_ROOT / relative).read_bytes()
        assert entry.size == len(data)
        assert entry.sha256 == hashlib.sha256(data).hexdigest()
        if entry.schema_ref is not None:
            assert (REPOSITORY_ROOT / entry.schema_ref).is_file()


def test_all_complete_storage_json_fixtures_use_canonical_bytes() -> None:
    for path in sorted(FIXTURE_ROOT.rglob("*.json")):
        if path.name == "invalid-truncated.json":
            continue
        raw = path.read_bytes()
        assert canonical_json_bytes(json.loads(raw.decode("utf-8"))) == raw


def test_valid_empty_r3_state_fixture_is_accepted() -> None:
    payload = parse_canonical_json_bytes(_fixture_bytes("state/valid-empty-r4.json"))
    _schema_validator(STATE_SCHEMA_PATH).validate(payload)
    state = StateFile.model_validate(payload)
    assert state.schema_version == 1
    assert state.contract_revision == "v1-contract-r4"
    assert state.generation == 1
    assert state.runtime_epochs == []
    assert state.recovery_processing_records == {}
    assert state.cases == {}
    assert state.idempotency_records == {}


@pytest.mark.parametrize(
    ("relative", "field_name"),
    [
        ("state/invalid-unknown-schema-version.json", "schema_version"),
        (
            "state/invalid-r2-contract-revision.json",
            "contract_revision",
        ),
    ],
)
def test_unknown_state_envelopes_are_rejected(
    relative: str,
    field_name: str,
) -> None:
    payload = parse_canonical_json_bytes(_fixture_bytes(relative))
    assert list(_schema_validator(STATE_SCHEMA_PATH).iter_errors(payload))
    with pytest.raises(ValidationError) as captured:
        StateFile.model_validate(payload)
    assert field_name in str(captured.value)


def test_truncated_state_fixture_is_rejected_before_model_validation() -> None:
    with pytest.raises(ValueError, match="invalid UTF-8 JSON bytes"):
        parse_canonical_json_bytes(_fixture_bytes("state/invalid-truncated.json"))


def test_dangling_state_reference_is_rejected_by_graph_validation() -> None:
    payload = parse_canonical_json_bytes(
        _fixture_bytes("state/invalid-dangling-active-job.json")
    )
    _schema_validator(STATE_SCHEMA_PATH).validate(payload)
    with pytest.raises(ValidationError, match="active_job_id must resolve"):
        StateFile.model_validate(payload)


def test_resource_hash_mismatch_scenario_has_a_single_deterministic_mismatch() -> None:
    payload = _fixture_json("scenarios/resource-hash-mismatch.json")
    assert isinstance(payload, dict)
    assert set(payload) == {
        "actual_content_utf8",
        "declared_sha256",
        "declared_size",
        "expected_error_code",
        "resource_kind",
        "scenario",
    }
    assert payload["scenario"] == "staged_file_hash_mismatch"
    assert payload["resource_kind"] == "FILE"
    assert payload["expected_error_code"] == ErrorCode.RESOURCE_HASH_MISMATCH

    actual = payload["actual_content_utf8"].encode("utf-8")
    assert payload["declared_size"] == len(actual)
    assert hashlib.sha256(actual).hexdigest() == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e"
        "1b161e5c1fa7425e73043362938b9824"
    )
    assert hashlib.sha256(actual).hexdigest() != payload["declared_sha256"]


def test_workspace_attachment_materialization_scenario_uses_r3_helpers() -> None:
    payload = _fixture_json(
        "scenarios/workspace-attachment-materialization-paths.json"
    )
    assert isinstance(payload, dict)
    assert set(payload) == {
        "directory_workspace_leaf",
        "entries",
        "formal_file_storage_leaf",
        "scenario",
    }
    assert payload["scenario"] == "attachment_workspace_archive_paths"
    assert payload["formal_file_storage_leaf"] == "payload"
    assert payload["directory_workspace_leaf"] == "tree"

    entries = payload["entries"]
    assert isinstance(entries, list)
    assert {
        entry["filename_suffix"]
        for entry in entries
        if isinstance(entry, dict)
    } == {None, *(suffix.value for suffix in AttachmentFilenameSuffix)}
    for entry in entries:
        assert isinstance(entry, dict)
        assert set(entry) == {
            "content_type",
            "filename_suffix",
            "name",
            "workspace_leaf",
        }
        suffix = derive_attachment_filename_suffix(
            entry["name"],
            entry["content_type"],
        )
        assert (None if suffix is None else suffix.value) == entry["filename_suffix"]
        relative = workspace_attachment_relative_path(ATTACHMENT_ID, suffix)
        assert PurePosixPath(relative).name == entry["workspace_leaf"]


def test_atomic_write_fault_scenario_freezes_disk_truth_per_boundary() -> None:
    payload = _fixture_json("scenarios/atomic-write-faults.json")
    assert isinstance(payload, dict)
    assert set(payload) == {
        "phases",
        "scenario",
        "starting_generation",
        "target_generation",
    }
    assert payload["scenario"] == "state_atomic_failure_truth"
    assert payload["starting_generation"] == 1
    assert payload["target_generation"] == 2

    phases = payload["phases"]
    assert isinstance(phases, list)
    assert all(
        isinstance(phase, dict)
        and set(phase)
        == {"authoritative_generation_after_failure", "fault_point"}
        for phase in phases
    )
    assert [phase["fault_point"] for phase in phases] == [
        "state.temp.write",
        "state.temp.file_sync",
        "state.prev.replace",
        "state.current.replace",
        "state.root.directory_sync",
    ]
    assert [phase["authoritative_generation_after_failure"] for phase in phases] == [
        1,
        1,
        1,
        1,
        2,
    ]
