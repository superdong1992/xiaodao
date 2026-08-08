from __future__ import annotations

import copy
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from problem_locator.contracts import SCHEMA_MODELS
from problem_locator.contracts.serialization import (
    canonical_json_bytes,
    is_canonical_json_bytes,
    parse_canonical_json_bytes,
)

from tests.contracts._support import FIXTURE_ROOT, load_json, schema_validator
from tests.contracts.manifest_helpers import (
    FixtureManifestError,
    assert_fixture_manifest_matches,
    build_fixture_manifest,
)


POSITIVE_FIXTURES = {
    "agent-job-outcome-draft-diagnosis.json": "agent-job-outcome-draft.schema.json",
    "agent-job-outcome-draft-failure.json": "agent-job-outcome-draft.schema.json",
    "agent-job-outcome-draft-review.json": "agent-job-outcome-draft.schema.json",
    "agent-job-outcome-draft-route.json": "agent-job-outcome-draft.schema.json",
    "agent-job-outcome-diagnosis.json": "agent-job-outcome.schema.json",
    "agent-job-outcome-failure.json": "agent-job-outcome.schema.json",
    "agent-job-outcome-review.json": "agent-job-outcome.schema.json",
    "agent-job-outcome-route.json": "agent-job-outcome.schema.json",
    "handoff.json": "handoff.schema.json",
    "job-diagnose.json": "job.schema.json",
    "job-outcome-diagnosis.json": "job-outcome.schema.json",
    "job-outcome-failure.json": "job-outcome.schema.json",
    "job-outcome-review.json": "job-outcome.schema.json",
    "job-outcome-route.json": "job-outcome.schema.json",
    "job-review.json": "job.schema.json",
    "job-route.json": "job.schema.json",
    "logparse-parse-claim.json": "logparse-parse-claim.schema.json",
    "state.json": "state.schema.json",
    "user-result.json": "user-result.schema.json",
    "workspace-input-manifest.json": "workspace-input-manifest.schema.json",
    "workspace-input-manifest-review.json": "workspace-input-manifest.schema.json",
}
UNTYPED_POSITIVE_FIXTURES = {"rpc-timeout-continuation.json"}

NEGATIVE_FIXTURES = {
    "model-agent-outcome-payload-error-conflict.json": (
        "agent-job-outcome.schema.json",
        False,
    ),
    "model-job-stale-status.json": ("job.schema.json", False),
    "model-review-pass-with-missing-evidence.json": (
        "agent-job-outcome.schema.json",
        False,
    ),
    "model-workspace-content-type-uppercase.json": (
        "workspace-input-manifest.schema.json",
        False,
    ),
    "model-workspace-archive-path-drift.json": (
        "workspace-input-manifest.schema.json",
        False,
    ),
    "model-workspace-archive-suffix-mismatch.json": (
        "workspace-input-manifest.schema.json",
        False,
    ),
    "schema-fixture-manifest-traversal.json": (
        "fixture-manifest.schema.json",
        True,
    ),
    "schema-handoff-invalid-hash.json": ("handoff.schema.json", True),
    "schema-handoff-nested-extra-field.json": ("handoff.schema.json", True),
    "schema-logparse-claim-extra-field.json": (
        "logparse-parse-claim.schema.json",
        True,
    ),
    "schema-user-result-extra-field.json": ("user-result.schema.json", True),
}


def test_positive_fixture_inventory_is_explicit() -> None:
    actual = {path.name for path in (FIXTURE_ROOT / "positive").glob("*.json")}
    assert actual == set(POSITIVE_FIXTURES) | UNTYPED_POSITIVE_FIXTURES


def test_untyped_scenario_seed_is_canonical_and_structurally_explicit() -> None:
    path = FIXTURE_ROOT / "positive" / "rpc-timeout-continuation.json"
    raw = path.read_bytes()
    payload = load_json(path)

    assert is_canonical_json_bytes(raw)
    assert set(payload) == {
        "scenario",
        "parameter_group_a",
        "log_attachment_requirement",
        "parameter_group_b",
    }
    assert set(payload["parameter_group_a"]) == {
        "caller_service",
        "server_service",
        "rpc_method",
        "problem_time",
    }
    assert payload["log_attachment_requirement"] == "log_archive"
    assert set(payload["parameter_group_b"]) == {"order_id"}


@pytest.mark.parametrize(
    ("fixture_name", "schema_name"), sorted(POSITIVE_FIXTURES.items())
)
def test_positive_fixture_passes_schema_model_and_canonical_bytes(
    fixture_name: str, schema_name: str
) -> None:
    path = FIXTURE_ROOT / "positive" / fixture_name
    raw = path.read_bytes()
    payload = load_json(path)

    assert is_canonical_json_bytes(raw)
    schema_validator(schema_name).validate(payload)
    model_type = SCHEMA_MODELS[schema_name]
    model = TypeAdapter(model_type).validate_python(payload)
    assert canonical_json_bytes(model) == raw
    assert parse_canonical_json_bytes(raw, model_type=model_type) == model


def test_negative_fixture_inventory_is_explicit() -> None:
    actual = {path.name for path in (FIXTURE_ROOT / "negative").glob("*.json")}
    assert actual == set(NEGATIVE_FIXTURES)


@pytest.mark.parametrize(
    ("fixture_name", "case"), sorted(NEGATIVE_FIXTURES.items())
)
def test_negative_fixture_is_rejected(
    fixture_name: str, case: tuple[str, bool]
) -> None:
    schema_name, must_fail_json_schema = case
    path = FIXTURE_ROOT / "negative" / fixture_name
    payload = load_json(path)
    schema_errors = list(schema_validator(schema_name).iter_errors(payload))
    if must_fail_json_schema:
        assert schema_errors, f"{fixture_name} unexpectedly passes JSON Schema"

    model_type = SCHEMA_MODELS[schema_name]
    with pytest.raises((TypeError, ValueError, ValidationError)):
        TypeAdapter(model_type).validate_python(payload)


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_agent_claim",
        "non_pass_agent_claim",
        "server_issues",
        "semantic_marked_mechanical_pass",
        "mechanical_marked_semantic_only",
    ),
)
def test_effective_resolution_requires_a_complete_fail_closed_audit(
    mutation: str,
) -> None:
    payload = load_json(
        FIXTURE_ROOT / "positive" / "job-outcome-diagnosis.json"
    )
    rule = payload["decision_audit"]["rules"][0]
    if mutation == "missing_agent_claim":
        rule["agent_claim"] = None
    elif mutation == "non_pass_agent_claim":
        rule["agent_claim"]["claimed_result"] = "UNKNOWN"
    elif mutation == "server_issues":
        rule["server_evaluation"]["issues"] = ["unresolved server issue"]
    elif mutation == "semantic_marked_mechanical_pass":
        rule["server_evaluation"]["status"] = "VERIFIED_PASS"
    else:
        rule["server_evaluation"]["rule_kind"] = "EVENT_PRESENT"

    with pytest.raises((TypeError, ValueError, ValidationError)):
        TypeAdapter(SCHEMA_MODELS["job-outcome.schema.json"]).validate_python(
            payload
        )


def test_handoff_contract_accepts_every_spec_id() -> None:
    base = load_json(FIXTURE_ROOT / "positive" / "handoff.json")
    validator = schema_validator("handoff.schema.json")
    model_type = SCHEMA_MODELS["handoff.schema.json"]
    for index in range(9):
        payload = copy.deepcopy(base)
        payload["spec_id"] = f"S{index:02d}"
        payload["branch"] = (
            "codex/v1-s00-contract-freeze"
            if index == 0
            else f"codex/v1-s{index:02d}-implementation"
        )
        validator.validate(payload)
        TypeAdapter(model_type).validate_python(payload)


@pytest.mark.parametrize("status", ["PASS", "ok", "", None])
def test_handoff_contract_rejects_unfrozen_test_status(status: object) -> None:
    payload = load_json(FIXTURE_ROOT / "positive" / "handoff.json")
    payload["tests"][0]["status"] = status
    with pytest.raises((TypeError, ValueError, ValidationError)):
        TypeAdapter(SCHEMA_MODELS["handoff.schema.json"]).validate_python(payload)


def test_owned_fixture_manifest_matches_every_byte_on_disk() -> None:
    manifest_path = FIXTURE_ROOT / "fixture-manifest.json"
    raw = manifest_path.read_bytes()
    payload = load_json(manifest_path)
    assert is_canonical_json_bytes(raw)
    schema_validator("fixture-manifest.schema.json").validate(payload)
    TypeAdapter(SCHEMA_MODELS["fixture-manifest.schema.json"]).validate_python(
        payload
    )
    assert_fixture_manifest_matches(FIXTURE_ROOT, payload)


def test_fixture_manifest_builder_requires_reviewed_metadata(tmp_path: Path) -> None:
    root = tmp_path / "fixtures"
    root.mkdir()
    (root / "a.json").write_bytes(b"{}\n")
    with pytest.raises(FixtureManifestError, match="metadata mismatch"):
        build_fixture_manifest(
            root,
            owner_spec="S00",
            repository_relative_root="tests/fixtures/contracts",
            metadata_by_path={},
        )


def test_fixture_manifest_semantics_detect_hash_and_file_set_drift() -> None:
    payload = load_json(FIXTURE_ROOT / "fixture-manifest.json")

    hash_drift = copy.deepcopy(payload)
    hash_drift["files"][0]["sha256"] = "0" * 64
    with pytest.raises(FixtureManifestError, match="sha256 drift"):
        assert_fixture_manifest_matches(FIXTURE_ROOT, hash_drift)

    omitted = copy.deepcopy(payload)
    omitted["files"].pop()
    with pytest.raises(FixtureManifestError, match="file set drift"):
        assert_fixture_manifest_matches(FIXTURE_ROOT, omitted)

    duplicate = copy.deepcopy(payload)
    duplicate["files"].append(copy.deepcopy(duplicate["files"][0]))
    with pytest.raises(FixtureManifestError, match="not unique"):
        assert_fixture_manifest_matches(FIXTURE_ROOT, duplicate)
