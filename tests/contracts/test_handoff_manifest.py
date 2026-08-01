from __future__ import annotations

import copy
from pathlib import PurePosixPath

import pytest
from pydantic import TypeAdapter, ValidationError

from problem_locator.contracts import SCHEMA_MODELS

from tests.contracts._support import FIXTURE_ROOT, load_json, schema_validator
from tests.contracts.manifest_helpers import assert_fixture_manifest_matches


HANDOFF_FIELDS = {
    "branch",
    "changed_files",
    "contract_base_commit",
    "contract_change_requests",
    "contract_revision",
    "dependency_requests",
    "executor",
    "fixtures_consumed",
    "fixtures_produced",
    "forbidden_scope_touched",
    "head_commit",
    "integration_notes",
    "known_limitations",
    "risks",
    "scope_completed",
    "spec_id",
    "tests",
    "title",
}


def _validate_handoff(payload: object) -> None:
    schema_validator("handoff.schema.json").validate(payload)
    TypeAdapter(SCHEMA_MODELS["handoff.schema.json"]).validate_python(payload)


def test_handoff_fixture_freezes_every_required_top_level_field() -> None:
    payload = load_json(FIXTURE_ROOT / "positive" / "handoff.json")
    assert set(payload) == HANDOFF_FIELDS
    assert payload["executor"] == {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "ultra",
    }
    assert payload["contract_revision"] == "v1-contract-r3"
    assert payload["branch"].startswith("codex/")
    assert payload["forbidden_scope_touched"] is False
    _validate_handoff(payload)


def test_handoff_path_arrays_are_repository_relative_posix_paths() -> None:
    payload = load_json(FIXTURE_ROOT / "positive" / "handoff.json")
    for field in ("changed_files", "fixtures_consumed", "fixtures_produced"):
        for value in payload[field]:
            path = PurePosixPath(value)
            assert not path.is_absolute()
            assert "\\" not in value
            assert all(part not in {"", ".", ".."} for part in path.parts)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spec_id", "S09"),
        ("branch", "feature/no-codex-prefix"),
        ("contract_revision", "v1-contract-r1"),
        ("contract_base_commit", "A" * 40),
        ("head_commit", "1" * 39),
    ],
)
def test_handoff_rejects_non_release_candidate_values(
    field: str, value: object
) -> None:
    payload = load_json(FIXTURE_ROOT / "positive" / "handoff.json")
    payload[field] = value
    with pytest.raises((TypeError, ValueError, ValidationError)):
        TypeAdapter(SCHEMA_MODELS["handoff.schema.json"]).validate_python(payload)


def test_handoff_rejects_extra_fields_in_every_nested_record() -> None:
    base = load_json(FIXTURE_ROOT / "positive" / "handoff.json")

    mutations: list[dict[str, object]] = []
    executor_extra = copy.deepcopy(base)
    executor_extra["executor"]["secret"] = "forbidden"
    mutations.append(executor_extra)

    test_extra = copy.deepcopy(base)
    test_extra["tests"][0]["duration"] = 1
    mutations.append(test_extra)

    dependency_extra = copy.deepcopy(base)
    dependency_extra["dependency_requests"] = [
        {
            "license_impact": "none",
            "package": "example",
            "purpose": "exercise nested strictness",
            "version": "1.0.0",
            "extra": "forbidden",
        }
    ]
    mutations.append(dependency_extra)

    change_extra = copy.deepcopy(base)
    change_extra["contract_change_requests"] = [
        {
            "affected_specs": ["S01"],
            "affected_types_or_codes": ["Job"],
            "compatibility": "breaking",
            "current_contract_revision": "v1-contract-r3",
            "fixture_and_test_changes": ["update fixture"],
            "problem": "example",
            "proposed_change": "example",
            "request_id": "contract-change-1",
            "requesting_spec": "S01",
            "extra": "forbidden",
        }
    ]
    mutations.append(change_extra)

    model_type = SCHEMA_MODELS["handoff.schema.json"]
    for payload in mutations:
        with pytest.raises((TypeError, ValueError, ValidationError)):
            TypeAdapter(model_type).validate_python(payload)


def test_fixture_manifest_is_complete_sorted_and_self_excluding() -> None:
    payload = load_json(FIXTURE_ROOT / "fixture-manifest.json")
    assert_fixture_manifest_matches(FIXTURE_ROOT, payload)
    paths = [entry["path"] for entry in payload["files"]]
    assert paths == sorted(paths)
    assert "fixture-manifest.json" not in paths
    assert all(
        entry["schema_ref"] is None
        or entry["schema_ref"].startswith("schemas/v1/")
        for entry in payload["files"]
    )


def test_fixture_manifest_rejects_duplicate_paths_even_if_metadata_differs() -> None:
    payload = load_json(FIXTURE_ROOT / "fixture-manifest.json")
    duplicate = copy.deepcopy(payload["files"][0])
    duplicate["purpose"] = "A different description cannot legalize a duplicate path."
    payload["files"].append(duplicate)
    with pytest.raises((TypeError, ValueError, ValidationError)):
        TypeAdapter(SCHEMA_MODELS["fixture-manifest.schema.json"]).validate_python(
            payload
        )
