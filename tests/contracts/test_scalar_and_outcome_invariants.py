from __future__ import annotations

import copy

import pytest
from pydantic import TypeAdapter, ValidationError

from problem_locator.contracts import SCHEMA_MODELS

from tests.contracts._support import FIXTURE_ROOT, load_json


def _validate(schema_name: str, payload: object):
    return TypeAdapter(SCHEMA_MODELS[schema_name]).validate_python(payload)


@pytest.mark.parametrize(
    "content_type",
    [
        "Application/Gzip",
        "application/json; charset=utf-8",
        "application/*",
        "application/ json",
        "application/json\r\nX-Injected: true",
        "应用/json",
        f"{'a' * 64}/b",
        "application/",
    ],
)
def test_content_type_rejects_noncanonical_mime_spellings(
    content_type: str,
) -> None:
    payload = load_json(
        FIXTURE_ROOT / "positive" / "workspace-input-manifest.json"
    )
    payload["entries"][0]["content_type"] = content_type
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _validate("workspace-input-manifest.schema.json", payload)


def test_content_type_accepts_the_exact_127_ascii_character_boundary() -> None:
    payload = load_json(
        FIXTURE_ROOT / "positive" / "workspace-input-manifest.json"
    )
    content_type = f"{'a' * 63}/{'b' * 63}"
    assert len(content_type) == 127
    payload["entries"][0]["content_type"] = content_type
    _validate("workspace-input-manifest.schema.json", payload)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-07-31T00:00:00Z",
        "2026-07-31T00:00:00.00Z",
        "2026-07-31T00:00:00.000+00:00",
        "2026-07-31 00:00:00.000Z",
        "2026-07-31T00:00:00.000z",
    ],
)
def test_utc_timestamp_requires_millisecond_z_form(timestamp: str) -> None:
    payload = load_json(FIXTURE_ROOT / "positive" / "job-route.json")
    payload["created_at"] = timestamp
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _validate("job.schema.json", payload)


@pytest.mark.parametrize(
    ("field_path", "invalid_value"),
    [
        (("job_id",), "00000000-0000-0000-0000-00000000001A"),
        (("job_id",), "job-1"),
        (("agent_profile_ref", "content_hash"), "A" * 64),
        (("agent_profile_ref", "content_hash"), "a" * 63),
    ],
)
def test_opaque_ids_and_hashes_are_lowercase_and_fixed_width(
    field_path: tuple[str, ...], invalid_value: str
) -> None:
    payload = load_json(FIXTURE_ROOT / "positive" / "job-route.json")
    target = payload
    for part in field_path[:-1]:
        target = target[part]
    target[field_path[-1]] = invalid_value
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _validate("job.schema.json", payload)


def test_problem_spec_preserves_order_but_rejects_exact_duplicates() -> None:
    payload = load_json(FIXTURE_ROOT / "positive" / "job-route.json")
    problem = payload["context_snapshot"]["problem_spec"]
    problem["goals"] = ["Locate the timeout cause.", "Locate the timeout cause."]
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _validate("job.schema.json", payload)


def test_review_verdict_matrix_accepts_each_legal_branch() -> None:
    base = load_json(
        FIXTURE_ROOT / "positive" / "agent-job-outcome-review.json"
    )
    valid_problem_sets = [
        ("PASS", [], [], [], []),
        ("NEED_MORE_EVIDENCE", ["unsupported"], [], [], []),
        ("NEED_MORE_EVIDENCE", [], [], ["missing"], []),
        ("REJECT", ["unsupported"], [], [], []),
        ("REJECT", [], ["conflict"], [], []),
        ("REJECT", [], [], [], ["stale"]),
    ]
    for verdict, unsupported, conflicts, missing, stale in valid_problem_sets:
        payload = copy.deepcopy(base)
        assessment = payload["payload"]
        assessment["verdict"] = verdict
        assessment["unsupported_findings"] = unsupported
        assessment["evidence_conflicts"] = conflicts
        assessment["missing_evidence"] = missing
        assessment["stale_references"] = stale
        _validate("agent-job-outcome.schema.json", payload)


@pytest.mark.parametrize(
    ("verdict", "unsupported", "conflicts", "missing", "stale"),
    [
        ("PASS", ["unsupported"], [], [], []),
        ("PASS", [], ["conflict"], [], []),
        ("PASS", [], [], ["missing"], []),
        ("PASS", [], [], [], ["stale"]),
        ("NEED_MORE_EVIDENCE", [], [], [], []),
        ("REJECT", [], [], [], []),
        ("REJECT", [], [], ["missing alone is insufficient"], []),
    ],
)
def test_review_verdict_matrix_rejects_illegal_combinations(
    verdict: str,
    unsupported: list[str],
    conflicts: list[str],
    missing: list[str],
    stale: list[str],
) -> None:
    payload = load_json(
        FIXTURE_ROOT / "positive" / "agent-job-outcome-review.json"
    )
    assessment = payload["payload"]
    assessment["verdict"] = verdict
    assessment["unsupported_findings"] = unsupported
    assessment["evidence_conflicts"] = conflicts
    assessment["missing_evidence"] = missing
    assessment["stale_references"] = stale
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _validate("agent-job-outcome.schema.json", payload)


def test_failed_outcome_requires_null_payload_and_nonnull_error() -> None:
    base = load_json(
        FIXTURE_ROOT / "positive" / "agent-job-outcome-failure.json"
    )
    _validate("agent-job-outcome.schema.json", base)

    no_error = copy.deepcopy(base)
    no_error["error"] = None
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _validate("agent-job-outcome.schema.json", no_error)

    payload_and_error = copy.deepcopy(base)
    payload_and_error["payload"] = load_json(
        FIXTURE_ROOT / "positive" / "agent-job-outcome-route.json"
    )["payload"]
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _validate("agent-job-outcome.schema.json", payload_and_error)


def test_nonfailed_outcome_requires_typed_payload_and_null_error() -> None:
    base = load_json(FIXTURE_ROOT / "positive" / "agent-job-outcome-route.json")
    _validate("agent-job-outcome.schema.json", base)
    for field, value in (("payload", None), ("error", {"unexpected": True})):
        invalid = copy.deepcopy(base)
        invalid[field] = value
        with pytest.raises((TypeError, ValueError, ValidationError)):
            _validate("agent-job-outcome.schema.json", invalid)


def test_execution_failure_true_retryability_is_allowlisted() -> None:
    invalid = load_json(
        FIXTURE_ROOT / "positive" / "agent-job-outcome-failure.json"
    )
    invalid["error"]["retryable"] = True
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _validate("agent-job-outcome.schema.json", invalid)

    valid = copy.deepcopy(invalid)
    valid["error"].update(
        {
            "code": "BACKEND_TIMEOUT",
            "message": "The backend exceeded the fixed wall time.",
            "stage": "BACKEND_EXECUTE",
        }
    )
    _validate("agent-job-outcome.schema.json", valid)


def test_candidate_requires_exactly_one_user_result_artifact() -> None:
    base = load_json(
        FIXTURE_ROOT / "positive" / "agent-job-outcome-diagnosis.json"
    )
    _validate("agent-job-outcome.schema.json", base)

    missing = copy.deepcopy(base)
    missing["proposed_artifact_drafts"] = []
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _validate("agent-job-outcome.schema.json", missing)

    duplicate = copy.deepcopy(base)
    duplicate["proposed_artifact_drafts"].append(
        copy.deepcopy(duplicate["proposed_artifact_drafts"][0])
    )
    duplicate["proposed_artifact_drafts"][1]["proposal_key"] = "user_result_2"
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _validate("agent-job-outcome.schema.json", duplicate)
