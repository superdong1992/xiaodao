from __future__ import annotations

import copy
import json

import pytest

from problem_locator.contracts import Job, RouteKind, bytes_sha256, canonical_json_bytes
from problem_locator.runtime.keyword_route import try_direct_keyword_route
from problem_locator.runtime.routing_keywords import parse_direct_match_keywords
from tests.deterministic.unit.runtime.test_route_admission import _inputs


KEYWORD = "RPC_QUEUE_STALLED"


def keyword_inputs(count=1):
    job, index, _ = _inputs(count)
    for item in index["skills"]:
        item["routing"]["exclusions"] = []
        item["routing"]["direct_match_keywords"] = [KEYWORD]
    return job, index


def route(job, index, text=f"当前服务报错 {KEYWORD}，请求无法完成。"):
    return try_direct_keyword_route(job, skill_index=json.dumps(index, ensure_ascii=False), raw_problem_text=text)


@pytest.mark.parametrize("value", [[], ["队列阻塞码"], ["Token", "token"], [f"fault-{i}" for i in range(16)]])
def test_keyword_parser_preserves_valid_literal_order(value):
    assert parse_direct_match_keywords(value) == tuple(value)


@pytest.mark.parametrize("value", [
    None, "token", ("token",), [f"fault-{i}" for i in range(17)], [1], [True],
    ["ab"], ["a" * 129], [" token"], ["token "], ["tok\nen"], ["tok\x00en"], ["tok\x7fen"],
    ["tok\x85en"], ["token", "token"], ["timeout"], ["TIMEOUT"], ["Failure"], ["slow"], ["RPC"],
])
def test_keyword_parser_rejects_ambiguous_or_invalid_declarations(value):
    with pytest.raises(ValueError):
        parse_direct_match_keywords(value)


def test_unique_literal_match_uses_raw_text_and_exact_frozen_ref_without_diagnostic_inputs():
    job, index = keyword_inputs()
    text = f"🙂当前故障 {KEYWORD}，错误标记再次出现 {KEYWORD}。"
    result = route(job, index, text)
    assert result is not None
    assert result.decision.kind is RouteKind.MATCHED
    assert result.decision.skill_ref == job.available_skill_refs[0]
    assert result.decision.confidence == 1
    assert not job.attachment_refs and not job.context_snapshot.user_facts
    assert KEYWORD not in job.context_snapshot.problem_spec.statement
    audit = result.audit
    assert audit["policy"] == "direct_keyword_route_v1"
    assert audit["reason_code"] == "DIRECT_KEYWORD_MATCH"
    assert audit["model_called"] is False and audit["model_skill_id"] is None
    assert audit["input_hashes"] == {
        "context_snapshot_sha256": bytes_sha256(canonical_json_bytes(job.context_snapshot)),
        "skill_index_sha256": bytes_sha256(json.dumps(index, ensure_ascii=False).encode()),
        "raw_problem_text_sha256": bytes_sha256(text.encode()),
    }
    assert audit["matches"] == [{"keyword": KEYWORD, "start": text.index(KEYWORD), "end": text.index(KEYWORD) + len(KEYWORD)}]
    assert audit["match_offset_unit"] == "unicode_codepoint"
    assert all(text[item["start"]:item["end"]] == item["keyword"] == KEYWORD for item in audit["matches"])


@pytest.mark.parametrize("text,matched", [
    (KEYWORD, True), (f"出现{KEYWORD}故障", True), (f"({KEYWORD})", True),
    (KEYWORD.lower(), False), (f"X{KEYWORD}", False), (f"{KEYWORD}X", False),
    (f"_{KEYWORD}", False), (f"{KEYWORD}_2", False), ("普通故障，未提供特征", False),
])
def test_matching_is_case_sensitive_and_does_not_accept_longer_ascii_identifiers(text, matched):
    job, index = keyword_inputs()
    assert (route(job, index, text) is not None) is matched


@pytest.mark.parametrize("text", [
    f"不是 {KEYWORD}", f"没有出现 {KEYWORD}", f"无{KEYWORD}", f"未{KEYWORD}", f"非{KEYWORD}",
    f"疑似 {KEYWORD}", f"可能是 {KEYWORD}", f"是否属于 {KEYWORD}",
    f"历史上出现过 {KEYWORD}", f"之前发生过 {KEYWORD}",
    f"示例：{KEYWORD}", f"比如 {KEYWORD}", f"对比 {KEYWORD}",
    f"not {KEYWORD}", f"Possible {KEYWORD}", f"previous {KEYWORD}",
    f"example: {KEYWORD}", f"compare {KEYWORD}", f'"{KEYWORD}"', f"`{KEYWORD}`",
    f"> {KEYWORD}", f"```\n{KEYWORD}\n```",
    f"假设 {KEYWORD}", f"假如 {KEYWORD}", f"如果 {KEYWORD}",
    f"if {KEYWORD}", f"hypothetical {KEYWORD}",
    f"{KEYWORD}；非{KEYWORD}", f'{KEYWORD}；"{KEYWORD}"',
])
def test_obvious_uncertain_or_quoted_context_defers_to_semantic_routing(text):
    job, index = keyword_inputs()
    assert route(job, index, text) is None


@pytest.mark.parametrize("text", [
    f"This isn't {KEYWORD}.", f"We didn't observe {KEYWORD}.",
    f"当前未再出现 {KEYWORD}。", f"{KEYWORD} 仅是举例，实际是另一个故障。",
])
def test_common_negation_and_examples_cannot_be_admitted_as_literal_incidents(text):
    job, index = keyword_inputs()
    assert route(job, index, text) is None


@pytest.mark.parametrize("apostrophe", ["'", "’"])
@pytest.mark.parametrize("prefix", [
    "isn", "aren", "wasn", "weren", "don", "doesn", "didn", "hasn", "haven", "hadn", "couldn",
    "wouldn", "shouldn", "mustn", "needn", "mightn", "can", "won", "shan", "ain", "oughtn",
])
def test_common_english_negative_contractions_defer_with_straight_or_curly_apostrophes(prefix, apostrophe):
    job, index = keyword_inputs()
    assert route(job, index, f"{prefix}{apostrophe}t observe {KEYWORD}.") is None


@pytest.mark.parametrize("phrase", [
    "未再出现", "未再次出现", "未曾出现", "不曾出现", "不再出现", "未再发生", "未再次发生",
    "不曾发生", "不再发生", "未观察到", "未检测到", "未发现", "未复现", "举例", "例子", "举个例",
])
def test_explicit_chinese_negation_and_example_phrases_defer(phrase):
    job, index = keyword_inputs()
    assert route(job, index, f"{phrase} {KEYWORD}。") is None


@pytest.mark.parametrize("mutation", ["missing", "empty", "legacy", "exclusion", "multiple", "prior"])
def test_only_one_declared_initial_route_without_exclusions_can_skip_model(mutation):
    job, index = keyword_inputs(2 if mutation == "multiple" else 1)
    routing = index["skills"][0]["routing"]
    if mutation == "missing":
        del routing["direct_match_keywords"]
    elif mutation == "empty":
        routing["direct_match_keywords"] = []
    elif mutation == "legacy":
        index["skills"][0]["routing"] = None
    elif mutation == "exclusion":
        routing["exclusions"] = [{"id": "other-product", "description": "另一产品不适用。"}]
    elif mutation == "prior":
        data = job.model_dump(mode="json")
        data["previous_outcome_refs"] = ["00000000-0000-4000-8000-000000000799"]
        job = Job.model_validate(data)
    assert route(job, index) is None


def test_unmatched_candidate_does_not_make_explicit_keyword_hit_ambiguous():
    job, index = keyword_inputs(2)
    index["skills"][1]["routing"]["direct_match_keywords"] = ["OTHER_PRODUCT_SIGNATURE"]
    assert route(job, index).decision.skill_ref == job.available_skill_refs[0]


def test_repeated_keywords_in_long_description_have_at_most_one_receipt_per_keyword():
    job, index = keyword_inputs()
    keywords = [f"SIGNATURE_{number:02d}" for number in range(16)]
    index["skills"][0]["routing"]["direct_match_keywords"] = keywords
    text = (" ".join(keywords) + "\n") * 300
    result = route(job, index, text)
    assert result is not None
    assert len(result.audit["matches"]) == 16
    assert all(item["start"] == text.index(item["keyword"]) for item in result.audit["matches"])
    assert len(canonical_json_bytes(result.audit)) < 4096


def test_projected_problem_text_is_not_used_instead_of_the_original_description():
    job, index = keyword_inputs()
    value = job.model_dump(mode="json")
    value["context_snapshot"]["problem_spec"]["statement"] = KEYWORD
    job = Job.model_validate(value)
    assert route(job, index, "当前请求失败。") is None


@pytest.mark.parametrize("mutation", ["hash", "version", "missing", "extra", "duplicate", "invalid-keyword"])
def test_every_frozen_candidate_is_validated_even_when_it_does_not_match(mutation):
    job, index = keyword_inputs(2)
    other = index["skills"][1]
    other["routing"]["direct_match_keywords"] = ["UNRELATED_SIGNATURE"]
    if mutation in {"hash", "version"}:
        other["ref"]["content_hash" if mutation == "hash" else "version"] = "0" * 64 if mutation == "hash" else "99.0.0"
    elif mutation == "missing":
        index["skills"].pop()
    elif mutation == "extra":
        extra = copy.deepcopy(other)
        extra["ref"]["id"] = "unknown-skill"
        index["skills"].append(extra)
    elif mutation == "duplicate":
        index["skills"].append(copy.deepcopy(other))
    else:
        other["routing"]["direct_match_keywords"] = ["error"]
    with pytest.raises(ValueError):
        route(job, index)
