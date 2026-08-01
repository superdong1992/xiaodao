from __future__ import annotations

import hashlib
import math

import pytest
from pydantic import ValidationError

from problem_locator.contracts import SCHEMA_MODELS
from problem_locator.contracts.serialization import (
    canonical_json_bytes,
    canonical_json_sha256,
    is_canonical_json_bytes,
    parse_canonical_json_bytes,
)

from tests.contracts._support import FIXTURE_ROOT


def test_canonical_json_has_sorted_keys_compact_utf8_and_one_lf() -> None:
    value = {"😀": "雪", "é": "值", "a": {"z": 2, "b": 1}}
    expected = '{"a":{"b":1,"z":2},"é":"值","😀":"雪"}\n'.encode()
    assert canonical_json_bytes(value) == expected
    assert is_canonical_json_bytes(expected)


def test_canonical_json_sha256_hashes_the_exact_canonical_bytes() -> None:
    value = {"second": [3, 2, 1], "first": True}
    encoded = canonical_json_bytes(value)
    assert canonical_json_sha256(value) == hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_non_json_numbers(value: float) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_json_bytes({"value": value})


@pytest.mark.parametrize(
    "data",
    [
        b'{"a":1}',
        b'{"a": 1}\n',
        b'{"b":1,"a":2}\n',
        b'\xef\xbb\xbf{"a":1}\n',
        b'{"a":1}\n\n',
        b'{"a":NaN}\n',
        b'{"a":1,"a":1}\n',
    ],
)
def test_parse_rejects_noncanonical_or_ambiguous_bytes(data: bytes) -> None:
    assert not is_canonical_json_bytes(data)
    with pytest.raises((TypeError, ValueError, ValidationError)):
        parse_canonical_json_bytes(data)


def test_parse_can_validate_and_return_a_public_model() -> None:
    data = (FIXTURE_ROOT / "positive" / "job-route.json").read_bytes()
    model_type = SCHEMA_MODELS["job.schema.json"]
    parsed = parse_canonical_json_bytes(data, model_type=model_type)
    assert isinstance(parsed, model_type)
    assert canonical_json_bytes(parsed) == data


def test_candidate_hash_preimage_ignores_identity_and_status() -> None:
    mapping = [
        {
            "criterion_index": 0,
            "criterion": "Identify the timed-out request.",
            "evidence_refs": ["00000000-0000-0000-0000-000000000040"],
            "explanation": "The request identifier appears in the parsed log.",
            "satisfied": True,
        }
    ]
    preimage = {
        "completion_criteria_mapping": mapping,
        "statement": "The inventory RPC exceeded its deadline.",
        "supporting_evidence_refs": [
            "00000000-0000-0000-0000-000000000040"
        ],
    }
    baseline = canonical_json_sha256(preimage)

    # Identity/status fields are deliberately outside the preimage.
    for ignored_change in (
        {"conclusion_id": "new"},
        {"revision": 99},
        {"status": "ACCEPTED"},
        {"proposed_by_job_id": "new"},
    ):
        assert canonical_json_sha256(preimage) == baseline
        assert ignored_change

    changed_statement = {**preimage, "statement": preimage["statement"] + "!"}
    changed_evidence = {
        **preimage,
        "supporting_evidence_refs": [
            "00000000-0000-0000-0000-000000000041"
        ],
    }
    changed_mapping = {
        **preimage,
        "completion_criteria_mapping": [
            {**mapping[0], "explanation": "A different explanation."}
        ],
    }
    assert canonical_json_sha256(changed_statement) != baseline
    assert canonical_json_sha256(changed_evidence) != baseline
    assert canonical_json_sha256(changed_mapping) != baseline
