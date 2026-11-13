from __future__ import annotations

import copy
import json
import logging
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
        "skill_id": job.available_skill_refs[0].id,
        "reason": "问题属于专用 Skill 的适用范围。", "confidence": 0.99,
    }
    return job, index, response


def _parse(job, index, response):
    return parse_route_response(json.dumps(response, ensure_ascii=False), job,
                                skill_index=json.dumps(index, ensure_ascii=False))


def test_short_route_result_does_not_require_condition_assessments(caplog):
    job, index, response = _inputs(2)
    assert set(response) == {"skill_id", "reason", "confidence"}
    result = _parse(job, index, response)
    assert result.draft.payload.kind is RouteKind.MATCHED
    assert not caplog.records


def test_explicit_unique_match_admits_even_when_diagnostic_inputs_and_logs_are_missing():
    job, index, response = _inputs()
    assert not job.context_snapshot.user_facts and not job.attachment_refs
    result = _parse(job, index, response)
    assert result.draft.payload.kind is RouteKind.MATCHED
    assert result.draft.payload.skill_ref == job.available_skill_refs[0]
    assert result.route_admission["policy"] == "shortlisted_route_v1"
    assert result.route_admission["reason_code"] == "ADMITTED"
    assert result.route_admission["draft_sha256"] == bytes_sha256(result.canonical_bytes)
    assert result.route_admission["input_hashes"] == {
        "context_snapshot_sha256": bytes_sha256(canonical_json_bytes(job.context_snapshot)),
        "skill_index_sha256": bytes_sha256(json.dumps(index, ensure_ascii=False).encode()),
    }
    assert result.route_admission["retrieval"]["candidate_ids"] == [response["skill_id"]]


def test_other_four_candidates_do_not_require_separate_refutations():
    job, index, response = _inputs(5)
    result = _parse(job, index, response)
    assert len(result.route_admission["retrieval"]["candidate_ids"]) == 5
    assert result.draft.payload.kind is RouteKind.MATCHED
    assert "assessments" not in result.route_admission
    assert "candidate_results" not in result.route_admission


@pytest.mark.parametrize("assessments", [
    None, True, 7, "[]", {}, [], [None],
    [{"skill_id": "unknown", "applicability": "wrong", "exclusions": [{"id": "not-rpc", "verdict": "REFUTED"}]}],
    [{"skill_id": "rpc-timeout", "applicability": [{"condition_id": "rpc-timeout", "verdict": "REFUTED", "evidence": []}],
      "exclusions": [{"id": "not-rpc", "verdict": "SUPPORTED", "reason": None, "evidence": {}}]}],
])
def test_legacy_assessments_and_evidence_are_ignored_without_exception(caplog, assessments):
    caplog.set_level(logging.DEBUG)
    job, index, response = _inputs()
    response.update(assessments=assessments, extra={"description": "附加说明"})
    result = _parse(job, index, response)
    assert result.draft.payload.kind is RouteKind.MATCHED
    assert result.draft.error is None and result.route_recovery is None
    assert not caplog.records


@pytest.mark.parametrize("confidence,matched", [
    (0, False), (0.94, False), (0.949999, False), (0.95, True), (1, True),
    ("0.94", False), ("0.95", True), ("1", True),
])
def test_confidence_threshold_accepts_json_numbers_and_numeric_strings(confidence, matched):
    job, index, response = _inputs()
    response["confidence"] = confidence
    result = _parse(job, index, response)
    assert (result.draft.payload.kind is RouteKind.MATCHED) is matched
    assert result.draft.payload.confidence == float(confidence)
    assert result.route_admission["reason_code"] == ("ADMITTED" if matched else "LOW_CONFIDENCE")


@pytest.mark.parametrize("confidence", [None, True, False, "", "invalid", "NaN", "Infinity", -1, 1.1, 10 ** 500, [], {}])
def test_unusable_confidence_uses_generic_without_exception(caplog, confidence):
    job, index, response = _inputs()
    response["confidence"] = confidence
    result = _parse(job, index, response)
    assert result.draft.payload.kind is RouteKind.NO_CAPABILITY
    assert result.draft.payload.confidence == 0
    assert result.route_admission["reason_code"] == "LOW_CONFIDENCE"
    assert result.draft.error is None and not caplog.records


def test_missing_confidence_does_not_admit_the_selected_skill():
    job, index, response = _inputs()
    del response["confidence"]
    result = _parse(job, index, response)
    assert result.draft.payload.kind is RouteKind.NO_CAPABILITY
    assert result.draft.payload.confidence == 0


@pytest.mark.parametrize("skill_id", [None, "unknown", "", True, 42, [], {}])
def test_absent_or_unknown_choice_is_not_promoted_to_a_recalled_skill(skill_id):
    job, index, response = _inputs()
    response["skill_id"] = skill_id
    result = _parse(job, index, response)
    assert result.route_admission["retrieval"]["candidate_ids"] == [job.available_skill_refs[0].id]
    assert result.draft.payload.kind is RouteKind.NO_CAPABILITY
    assert result.draft.payload.skill_ref is None
    assert result.route_admission["reason_code"] == "MODEL_NO_MATCH"


def test_known_sixth_candidate_is_not_admitted_but_a_recalled_candidate_is():
    job, index, response = _inputs(6)
    response["skill_id"] = job.available_skill_refs[-1].id
    outside = _parse(job, index, response)
    candidates = outside.route_admission["retrieval"]["candidate_ids"]
    assert len(candidates) == 5 and response["skill_id"] not in candidates
    assert len(job.available_skill_refs) == len(index["skills"]) == 6
    assert outside.draft.payload.kind is RouteKind.NO_CAPABILITY
    assert outside.route_admission["reason_code"] == "OUTSIDE_CANDIDATES"
    response["skill_id"] = candidates[-1]
    admitted = _parse(job, index, response)
    assert admitted.draft.payload.kind is RouteKind.MATCHED
    assert admitted.draft.payload.skill_ref.id == candidates[-1]


def test_no_lexical_match_cannot_be_overridden_by_a_known_skill_choice():
    job, index, response = _inputs()
    value = job.model_dump(mode="json")
    value["context_snapshot"]["problem_spec"].update(
        statement="问题", actual_behavior="错误", expected_behavior="定位", scope="问题",
        goals=["定位"], completion_criteria=["定位"],
    )
    result = _parse(Job.model_validate(value), index, response)
    assert result.draft.payload.kind is RouteKind.NO_CAPABILITY
    assert result.route_admission["reason_code"] == "NO_RECALL_MATCH"
    assert result.route_admission["retrieval"]["candidate_ids"] == []


def test_legacy_skill_without_routing_is_not_recalled():
    job, index, response = _inputs(legacy=True)
    result = _parse(job, index, response)
    assert result.draft.payload.kind is RouteKind.NO_CAPABILITY
    assert result.route_admission["reason_code"] == "NO_ELIGIBLE_SKILL"
    assert result.route_admission["retrieval"]["candidate_ids"] == []


@pytest.mark.parametrize("mutation", ["wrong_hash", "wrong_version", "missing_ref", "duplicate_ref", "unknown_ref"])
def test_frozen_catalog_mismatch_is_still_rejected(mutation):
    job, index, response = _inputs(2)
    other = index["skills"][-1]
    if mutation == "wrong_hash":
        other["ref"]["content_hash"] = "0" * 64
    elif mutation == "wrong_version":
        other["ref"]["version"] = "99.0.0"
    elif mutation == "missing_ref":
        index["skills"].pop()
    elif mutation == "duplicate_ref":
        index["skills"].append(copy.deepcopy(other))
    else:
        other["ref"]["id"] = "unknown-skill"
    with pytest.raises(RuntimeExecutionError):
        _parse(job, index, response)


@pytest.mark.parametrize("keywords", [None, "RPC request deadline exceeded", ["RPC request deadline exceeded"] * 2,
    ["timeout"], ["bad\nkeyword"], ["x" * 129]])
def test_model_route_rejects_invalid_indexed_direct_match_keywords(keywords):
    job, index, response = _inputs()
    index["skills"][0]["routing"]["direct_match_keywords"] = keywords
    with pytest.raises(RuntimeExecutionError):
        _parse(job, index, response)


@pytest.mark.parametrize("reason", [None, "", [], "说明" * 1024])
def test_route_reason_is_display_metadata_not_an_admission_requirement(reason):
    job, index, response = _inputs()
    response["reason"] = reason
    result = _parse(job, index, response)
    assert result.draft.payload.kind is RouteKind.MATCHED
    assert 0 < len(result.draft.payload.reason) <= 1024


def test_missing_reason_uses_a_nonempty_display_default():
    job, index, response = _inputs()
    del response["reason"]
    result = _parse(job, index, response)
    assert result.draft.payload.kind is RouteKind.MATCHED and result.draft.payload.reason


@pytest.mark.parametrize("raw", [
    None, "", "{broken", "null", "[]", "{}", "\ud800",
    '{"skill_id":"rpc-timeout","reason":"选择 "RPC" 定位","confidence":0.99}',
    '{"skill_id":"rpc-timeout","reason":"a","confidence":0.99,"confidence":1}',
])
def test_unusable_json_and_unencodable_text_use_generic_without_exception(raw, caplog):
    job, index, _ = _inputs()
    result = parse_route_response(raw, job, skill_index=json.dumps(index))
    assert result.draft.payload.kind is RouteKind.NO_CAPABILITY
    assert result.draft.error is None and result.route_recovery is None
    assert not caplog.records


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "路由结果如下。\n{}"])
def test_valid_escaped_reason_and_original_response_hash_survive_display_wrappers(wrapper):
    job, index, response = _inputs()
    response["reason"] = '选择 "RPC" 定位；路径 C:\\logs\\new，保留字面量 \\n。'
    raw = wrapper.format(json.dumps(response, ensure_ascii=False))
    result = parse_route_response(raw, job, skill_index=json.dumps(index))
    assert result.draft.payload.kind is RouteKind.MATCHED
    assert result.draft.payload.reason == response["reason"]
    assert result.route_admission["model_reason"] == response["reason"]
    assert result.route_admission["raw_response_sha256"] == bytes_sha256(raw.encode())
    assert result.route_recovery is None
