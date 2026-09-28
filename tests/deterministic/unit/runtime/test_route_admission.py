from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from problem_locator.contracts import Job, RouteKind, bytes_sha256, canonical_json_bytes
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.final_response import parse_route_response


def _inputs(count=1, *, legacy=False):
    job_value = json.loads((Path(__file__).parents[3] / "fixtures/contracts/positive/job-route.json").read_bytes())
    for index in range(1, count):
        ref = dict(job_value["available_skill_refs"][0], id=f"alternative-{index}")
        job_value["available_skill_refs"].append(ref)
    job = Job.model_validate(job_value)
    routing = {
        "applicability": [{"id": "rpc-timeout", "description": "服务间 RPC 调用超时。"}],
        "exclusions": [{"id": "not-rpc", "description": "问题不属于服务间 RPC。"}],
    }
    index = {"schema_version": 3, "skills": [{
        "ref": ref.model_dump(mode="json"), "routing": None if legacy else copy.deepcopy(routing),
        "required_user_inputs": ["request_id"], "required_artifacts": ["log_archive"],
    } for ref in job.available_skill_refs]}
    response = {
        "skill_id": job.available_skill_refs[0].id, "reason": "已逐项确认适用条件并排除其他候选。",
        "confidence": 0.99,
        "assessments": [] if legacy else [_assessment(ref.id, job) for ref in job.available_skill_refs],
    }
    return job, index, response


def _assessment(skill_id, job):
    quote = {"pointer": "/problem_spec/scope", "quote": job.context_snapshot.problem_spec.scope}
    return {"skill_id": skill_id, "applicability": [{
        "condition_id": "rpc-timeout", "verdict": "SUPPORTED", "reason": "已明确 RPC 调用范围。",
        "evidence": [dict(quote)],
    }], "exclusions": [{
        "condition_id": "not-rpc", "verdict": "REFUTED", "reason": "已明确这是服务间 RPC。",
        "evidence": [dict(quote)],
    }]}


def _parse(job, index, response):
    return parse_route_response(json.dumps(response, ensure_ascii=False), job,
                                skill_index=json.dumps(index, ensure_ascii=False))


def test_explicit_unique_match_admits_even_when_diagnostic_inputs_and_logs_are_missing():
    job, index, response = _inputs()
    assert not job.context_snapshot.user_facts and not job.attachment_refs
    result = _parse(job, index, response)
    assert result.draft.payload.kind is RouteKind.MATCHED
    assert result.draft.payload.skill_ref == job.available_skill_refs[0]
    assert result.route_admission["reason_code"] == "ADMITTED"
    assert result.route_admission["draft_sha256"] == bytes_sha256(result.canonical_bytes)
    assert result.route_admission["input_hashes"] == {
        "context_snapshot_sha256": bytes_sha256(canonical_json_bytes(job.context_snapshot)),
        "skill_index_sha256": bytes_sha256(json.dumps(index, ensure_ascii=False).encode()),
    }


@pytest.mark.parametrize("confidence,matched", [(0, False), (0.94, False), (0.949999, False), (0.95, True), (1, True)])
def test_confidence_is_a_required_additional_gate(confidence, matched):
    job, index, response = _inputs()
    response["confidence"] = confidence
    result = _parse(job, index, response)
    assert (result.draft.payload.kind is RouteKind.MATCHED) is matched
    assert result.draft.payload.confidence == confidence
    if not matched:
        assert result.route_admission["reason_code"] == "LOW_CONFIDENCE"
        assert "通用定位" in result.draft.payload.reason


@pytest.mark.parametrize("kind,verdict,expected", [
    ("applicability", "REFUTED", "ruled_out"),
    ("applicability", "UNKNOWN", "uncertain"),
    ("exclusions", "SUPPORTED", "ruled_out"),
    ("exclusions", "UNKNOWN", "uncertain"),
])
def test_high_confidence_cannot_override_negative_or_unknown_assessments(kind, verdict, expected):
    job, index, response = _inputs()
    response["confidence"] = 1
    response["assessments"][0][kind][0]["verdict"] = verdict
    result = _parse(job, index, response)
    assert result.draft.payload.kind is RouteKind.NO_CAPABILITY
    assert result.route_admission["candidate_results"][0]["status"] == expected


@pytest.mark.parametrize("evidence", [
    [], [{"pointer": "/problem_spec/scope", "quote": ""}],
    [{"pointer": "/problem_spec/scope", "quote": "   "}],
    [{"pointer": "/problem_spec/scope", "quote": "并不存在的系统名称"}],
    [{"pointer": "/active_hypotheses/0/statement", "quote": "payment-to-inventory RPC"}],
    [{"pointer": "/previous_outcomes/0/payload/reason", "quote": "payment-to-inventory RPC"}],
    [{"pointer": "/skills/0/routing/applicability/0/description", "quote": "服务间 RPC 调用超时。"}],
    [{"pointer": "/problem_spec/scope/0", "quote": "p"}],
    [{"pointer": "/problem_spec/revision", "quote": "1"}],
    [{"pointer": "/problem_spec/goals/00", "quote": "Locate the timeout cause."}],
    [{"pointer": "/problem_spec/scope", "quote": "payment-to-inventory RPC"},
     {"pointer": "/invented", "quote": "bad"}],
])
def test_ungrounded_evidence_falls_back_without_protocol_failure(evidence):
    job, index, response = _inputs()
    response["assessments"][0]["applicability"][0]["evidence"] = evidence
    result = _parse(job, index, response)
    assert result.draft.payload.kind is RouteKind.NO_CAPABILITY
    assert result.route_admission["reason_code"] == "UNCERTAIN_CANDIDATE"
    assert result.route_admission["candidate_results"][0]["conditions"][0]["effective_verdict"] == "UNKNOWN"


def test_literal_problem_list_item_is_a_supported_source():
    job, index, response = _inputs()
    response["assessments"][0]["applicability"][0]["evidence"] = [{
        "pointer": "/problem_spec/goals/0", "quote": job.context_snapshot.problem_spec.goals[0],
    }]
    assert _parse(job, index, response).draft.payload.kind is RouteKind.MATCHED


@pytest.mark.parametrize("collection", ["user_facts", "confirmed_facts"])
def test_actual_active_fact_statement_is_supported_but_metadata_is_not(collection):
    job, index, response = _inputs()
    data = job.model_dump(mode="json")
    fact = {
        "item_id": "00000000-0000-4000-8000-000000000498",
        "statement": "payment-to-inventory RPC", "status": "ACTIVE",
        "provenance": {"source_type": "USER_INPUT", "source_ref": "00000000-0000-4000-8000-000000000497", "input_name": "scope"},
        "evidence_refs": [], "created_revision": 1, "supersedes": [],
    }
    if collection == "confirmed_facts":
        fact["provenance"] = {"source_type": "AGENT_OUTCOME", "source_ref": "00000000-0000-4000-8000-000000000497", "input_name": None}
        fact["evidence_refs"] = ["00000000-0000-4000-8000-000000000499"]
        data["context_snapshot"]["evidence_refs"] = list(fact["evidence_refs"])
        data["evidence_refs"] = list(fact["evidence_refs"])
    data["context_snapshot"][collection] = [fact]
    job = Job.model_validate(data)
    evidence = response["assessments"][0]["applicability"][0]["evidence"][0]
    evidence.update(pointer=f"/{collection}/0/statement", quote=fact["statement"])
    assert _parse(job, index, response).draft.payload.kind is RouteKind.MATCHED
    evidence.update(pointer=f"/{collection}/0/provenance/source_ref", quote=fact["provenance"]["source_ref"])
    assert _parse(job, index, response).draft.payload.kind is RouteKind.NO_CAPABILITY


@pytest.mark.parametrize("other_state,matched", [("SUPPORTED", False), ("UNKNOWN", False), ("REFUTED", True)])
def test_selected_match_requires_every_other_candidate_to_be_ruled_out(other_state, matched):
    job, index, response = _inputs(2)
    response["assessments"][1]["applicability"][0]["verdict"] = other_state
    result = _parse(job, index, response)
    assert (result.draft.payload.kind is RouteKind.MATCHED) is matched


def test_bad_quote_cannot_rule_out_an_alternative():
    job, index, response = _inputs(2)
    alternative = response["assessments"][1]["applicability"][0]
    alternative.update(verdict="REFUTED", evidence=[])
    assert _parse(job, index, response).draft.payload.kind is RouteKind.NO_CAPABILITY


def test_legacy_skill_without_routing_cannot_be_selected_automatically():
    job, index, response = _inputs(legacy=True)
    result = _parse(job, index, response)
    assert result.draft.payload.kind is RouteKind.NO_CAPABILITY
    assert result.route_admission["reason_code"] == "NO_ROUTING_DECLARATION"


def test_null_selection_is_never_promoted_even_when_one_skill_passes():
    job, index, response = _inputs()
    response["skill_id"] = None
    result = _parse(job, index, response)
    assert result.draft.payload.kind is RouteKind.NO_CAPABILITY
    assert result.route_admission["reason_code"] == "MODEL_NO_MATCH"


@pytest.mark.parametrize("mutation", [
    "three_fields", "unknown_skill", "missing_candidate", "duplicate_candidate", "extra_candidate",
    "missing_condition", "duplicate_condition", "extra_condition", "wrong_assessments_type",
    "wrong_evidence_type", "wrong_pointer_type", "extra_evidence_field", "wrong_verdict",
    "wrong_condition_reason", "extra_root_field", "bool_confidence", "string_confidence", "huge_confidence",
])
def test_protocol_failures_never_turn_into_accepted_routes(mutation):
    job, index, response = _inputs()
    condition = response["assessments"][0]["applicability"][0]
    if mutation == "three_fields":
        del response["assessments"]
    elif mutation == "unknown_skill":
        response["skill_id"] = "unknown"
    elif mutation == "missing_candidate":
        response["assessments"] = []
    elif mutation == "duplicate_candidate":
        response["assessments"] *= 2
    elif mutation == "extra_candidate":
        response["assessments"].append(_assessment("unknown", job))
    elif mutation == "missing_condition":
        response["assessments"][0]["applicability"] = []
    elif mutation == "duplicate_condition":
        response["assessments"][0]["applicability"] *= 2
    elif mutation == "extra_condition":
        condition["condition_id"] = "unknown"
    elif mutation == "wrong_assessments_type":
        response["assessments"] = "[]"
    elif mutation == "wrong_evidence_type":
        condition["evidence"] = {}
    elif mutation == "wrong_pointer_type":
        condition["evidence"][0]["pointer"] = None
    elif mutation == "extra_evidence_field":
        condition["evidence"][0]["trusted"] = True
    elif mutation == "wrong_verdict":
        condition["verdict"] = True
    elif mutation == "wrong_condition_reason":
        condition["reason"] = None
    elif mutation == "extra_root_field":
        response["trusted"] = True
    elif mutation == "bool_confidence":
        response["confidence"] = True
    elif mutation == "string_confidence":
        response["confidence"] = "1"
    elif mutation == "huge_confidence":
        response["confidence"] = 10 ** 500
    with pytest.raises(RuntimeExecutionError):
        _parse(job, index, response)


def test_frozen_skill_version_mismatch_is_rejected():
    job, index, response = _inputs()
    index["skills"][0]["ref"]["content_hash"] = "0" * 64
    with pytest.raises(RuntimeExecutionError):
        _parse(job, index, response)


@pytest.mark.parametrize("assessment_first", [False, True])
def test_malformed_root_reason_is_rejected_in_every_field_order(assessment_first):
    job, index, response = _inputs()
    if assessment_first:
        response = {"assessments": response["assessments"], **response}
    raw = json.dumps(response, ensure_ascii=False).replace(json.dumps(response["reason"], ensure_ascii=False), '"选择 "RPC" 定位"')
    with pytest.raises(RuntimeExecutionError):
        parse_route_response(raw, job, skill_index=json.dumps(index))


def test_valid_escaped_quotes_preserve_original_reason_and_raw_hash():
    job, index, response = _inputs()
    response["reason"] = '选择 "RPC" 定位'
    raw = json.dumps(response, ensure_ascii=False)
    parsed = parse_route_response(raw, job, skill_index=json.dumps(index))
    assert parsed.draft.payload.kind is RouteKind.MATCHED
    assert parsed.route_recovery is None
    assert parsed.route_admission["model_reason"] == response["reason"]
    assert parsed.route_admission["raw_response_sha256"] == bytes_sha256(raw.encode())


@pytest.mark.parametrize("root_broken", [False, True])
@pytest.mark.parametrize("field", ["reason", "quote", "verdict"])
def test_nested_assessment_strings_cannot_be_repaired_or_swallowed(field, root_broken):
    job, index, response = _inputs()
    condition = response["assessments"][0]["applicability"][0]
    target = condition["evidence"][0] if field == "quote" else condition
    target[field] = 'broken "nested" value'
    raw = json.dumps(response, ensure_ascii=False).replace('broken \\"nested\\" value', 'broken "nested" value')
    if root_broken:
        raw = raw.replace(json.dumps(response["reason"], ensure_ascii=False), '"选择 "RPC" 定位"')
    with pytest.raises(RuntimeExecutionError):
        parse_route_response(raw, job, skill_index=json.dumps(index))
