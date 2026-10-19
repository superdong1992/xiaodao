"""Correction attribution is bound to the actual historical card and checked evidence."""
from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from problem_locator.memory.extraction import parse_card_json

OLD = {"problem_features": ["connection pool timeout"], "applicability": ["database load"],
    "steps": ["Check the connection release path"], "limitations": ["Verify with current observations"]}
NEW = {"problem_features": ["connection pool timeout ERR_POOL_1024"], "applicability": ["slow query database load"],
    "steps": ["Compare query duration and connection occupancy before deciding"],
    "limitations": ["Pool exhaustion alone does not prove a connection leak"]}


def usage():
    raw = parse_card_json(json.dumps(OLD))
    return {"job_id": str(uuid.uuid4()), "case_id": str(uuid.uuid4()), "skill_name": "generic-test",
        "card_id": str(uuid.uuid4()), "card_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "card_json": raw, "source_kind": "REPORT"}


def review(source, relation="GLOBAL_ERROR"):
    return {"card_id": source["card_id"], "card_sha256": source["card_sha256"], "relation": relation,
        "evidence_indexes": [] if relation == "UNRELATED" else [0],
        "scope_terms": ["slow", "query"] if relation in {"NOT_APPLICABLE", "DIAGNOSIS_OMISSION"} else []}


def validate(value, source, *, card=NEW, evidence=None):
    from problem_locator.memory.review import validate_memory_review
    return validate_memory_review(value, source, [{"quote_sha256": "f" * 64}] if evidence is None else evidence,
        parse_card_json(json.dumps(card)))


@pytest.mark.parametrize("relation", ["GLOBAL_ERROR", "NOT_APPLICABLE", "DIAGNOSIS_OMISSION", "UNRELATED"])
def test_review_binds_supported_relation_to_used_card_and_checked_evidence(relation):
    source = usage()
    value = review(source, relation)
    assert validate(value, source) == value


@pytest.mark.parametrize("field,value", [
    ("card_id", "00000000-0000-0000-0000-000000000001"), ("card_sha256", "0" * 64),
    ("relation", "CORRECT"), ("relation", None), ("evidence_indexes", [True]),
    ("evidence_indexes", [0, 0]), ("evidence_indexes", [-1]), ("evidence_indexes", [1]),
    ("evidence_indexes", "0"), ("evidence_indexes", []), ("scope_terms", ["slow", "query"]),
])
def test_review_rejects_unbound_identity_relation_or_evidence(field, value):
    source = usage()
    value_to_check = review(source)
    value_to_check[field] = value
    with pytest.raises(ValueError):
        validate(value_to_check, source)


@pytest.mark.parametrize("terms", [[], ["slow"], ["slow", "slow"], ["slow query", "pool"],
    ["Slow", "query"], ["unrelated", "pool"], ["service", "query"], ["pool"] * 9, "slow query", [1, "query"]])
def test_scoped_review_rejects_ambiguous_unnormalized_or_missing_conditions(terms):
    source = usage()
    value = review(source, "NOT_APPLICABLE")
    value["scope_terms"] = terms
    with pytest.raises(ValueError):
        validate(value, source)


def test_one_distinct_error_code_can_bound_a_scoped_correction():
    source = usage()
    value = review(source, "NOT_APPLICABLE")
    value["scope_terms"] = ["err_pool_1024"]
    assert validate(value, source) == value


@pytest.mark.parametrize("relation", ["GLOBAL_ERROR", "NOT_APPLICABLE", "DIAGNOSIS_OMISSION"])
def test_attributed_correction_cannot_republish_identical_historical_content(relation):
    source = usage()
    value = review(source, relation)
    if value["scope_terms"]:
        value["scope_terms"] = ["connection", "pool"]
    with pytest.raises(ValueError):
        validate(value, source, card=OLD)


def test_unrelated_review_may_preserve_same_content_but_cannot_add_scope():
    source = usage()
    value = review(source, "UNRELATED")
    assert validate(value, source, card=OLD) == value
    value["scope_terms"] = ["connection", "pool"]
    with pytest.raises(ValueError):
        validate(value, source, card=OLD)


@pytest.mark.parametrize("mutation", ["extra", "missing", "missing_usage", "missing_evidence", "bad_usage_hash"])
def test_review_requires_exact_contract_and_trustworthy_usage(mutation):
    source = usage()
    value = review(source)
    evidence = None
    if mutation == "extra":
        value["target_job_id"] = source["job_id"]
    elif mutation == "missing":
        value.pop("scope_terms")
    elif mutation == "missing_usage":
        source = None
    elif mutation == "missing_evidence":
        evidence = []
    else:
        source["card_sha256"] = value["card_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        validate(value, source, evidence=evidence)


def test_validation_returns_an_independent_receipt_without_source_contents():
    source = usage()
    value = review(source, "NOT_APPLICABLE")
    validated = validate(value, source)
    value["evidence_indexes"].append(10)
    value["scope_terms"].append("other")
    assert validated["evidence_indexes"] == [0]
    assert validated["scope_terms"] == ["slow", "query"]
    assert "card_json" not in validated and "job_id" not in validated
