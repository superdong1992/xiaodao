from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

from problem_locator.contracts import Job, bytes_sha256, canonical_json_bytes
from problem_locator.runtime.route_recall import recall_route_candidates


def _entry(skill_id, *, capability="", summary="", applicability="范围", exclusions=(), keywords=()):
    return {
        "ref": {"id": skill_id, "version": "1.0.0", "content_hash": "b" * 64},
        "capability": capability, "summary": summary,
        "routing": {
            "applicability": [{"id": "scope", "description": applicability}],
            "exclusions": [{"id": f"exclude-{index}", "description": text}
                           for index, text in enumerate(exclusions)],
            **({"direct_match_keywords": list(keywords)} if keywords else {}),
        },
        "required_user_inputs": ["never_rank_this"], "required_artifacts": ["log_archive"],
        "requires_logparse": True, "logparse_product": "never_rank_this",
    }


def _inputs(text, *entries):
    value = json.loads((Path(__file__).parents[3] / "fixtures/contracts/positive/job-route.json").read_bytes())
    value["available_skill_refs"] = [copy.deepcopy(entry["ref"]) for entry in entries]
    value["context_snapshot"]["problem_spec"] = {
        "revision": 1, "statement": text, "actual_behavior": "问题", "expected_behavior": "定位",
        "scope": "问题", "goals": ["定位"], "completion_criteria": ["定位"],
        "constraints": [], "non_goals": [],
    }
    return Job.model_validate(value), {"schema_version": 3, "skills": list(entries)}


def _recall(job, index):
    return recall_route_candidates(job, skill_index=canonical_json_bytes(index).decode())


def _ranked(result):
    return {item["skill_id"]: item for item in result.audit["ranked_candidates"]}


def test_selects_at_most_five_positive_candidates_without_padding():
    entries = [_entry(f"skill-{index}", summary="quartz") for index in range(7)]
    entries.append(_entry("skill-negative", summary="basalt"))
    job, index = _inputs("quartz", *entries)
    result = _recall(job, index)
    assert result.skill_ids == tuple(f"skill-{index}" for index in range(5))
    assert len(result.audit["ranked_candidates"]) == 8
    assert _ranked(result)["skill-negative"] == {
        "skill_id": "skill-negative", "score": 0, "keyword_matches": [], "selected": False,
    }
    job, index = _inputs("basalt", *entries)
    assert _recall(job, index).skill_ids == ("skill-negative",)


@pytest.mark.parametrize("text", [
    "没有匹配的特征", "问题 错误 失败 超时 故障 定位", "问题错误失败超时故障定位",
    "problem error failure timeout fault diagnosis locate", "please check the service logs",
])
def test_no_overlap_or_only_generic_words_returns_no_candidates(text):
    job, index = _inputs(text, _entry("alpha", summary="quartz"), _entry("beta", summary="basalt"))
    result = _recall(job, index)
    assert result.skill_ids == ()
    assert json.loads(result.model_index) == {"schema_version": 1, "skills": []}
    assert all(item["score"] == 0 and not item["selected"] for item in result.audit["ranked_candidates"])


def test_field_weights_use_the_highest_weight_once_per_token():
    entries = [
        _entry("diagnosis-skill/quartz", capability="quartz", applicability="quartz", summary="quartz quartz"),
        _entry("skill-capability", capability="quartz"),
        _entry("skill-applicability", applicability="quartz"),
        _entry("skill-summary", summary="quartz"),
    ]
    job, index = _inputs("quartz quartz quartz", *entries)
    ranked = _ranked(_recall(job, index))
    assert {skill_id: item["score"] for skill_id, item in ranked.items()} == {
        "diagnosis-skill/quartz": 3, "skill-capability": 3,
        "skill-applicability": 2, "skill-summary": 1,
    }


def test_rarer_terms_rank_above_terms_shared_by_more_skills():
    job, index = _inputs("quartz basalt", _entry("a-common", summary="quartz"),
                         _entry("b-common", summary="quartz"), _entry("z-rare", summary="basalt"))
    result = _recall(job, index)
    assert result.skill_ids == ("z-rare", "a-common", "b-common")
    ranked = _ranked(result)
    assert ranked["z-rare"]["score"] == pytest.approx(1 + math.log(4 / 2))
    assert ranked["a-common"]["score"] == pytest.approx(1 + math.log(4 / 3))


def test_repeated_document_and_query_words_do_not_change_scores():
    job, index = _inputs("quartz", _entry("alpha", summary="quartz"))
    first = _recall(job, index)
    index["skills"][0]["summary"] = "quartz " * 100
    value = job.model_dump(mode="json")
    value["context_snapshot"]["problem_spec"]["statement"] = "quartz " * 100
    second = _recall(Job.model_validate(value), index)
    assert first.audit["ranked_candidates"] == second.audit["ranked_candidates"]


def test_equal_scores_use_skill_id_order_regardless_of_catalog_order():
    entries = [_entry(name, summary="quartz basalt") for name in ("zulu", "delta", "alpha")]
    job, index = _inputs("basalt quartz", *entries)
    first = _recall(job, index)
    index["skills"].reverse()
    second = _recall(job, index)
    assert first.skill_ids == second.skill_ids == ("alpha", "delta", "zulu")
    assert first.model_index == second.model_index
    assert first.audit["ranked_candidates"] == second.audit["ranked_candidates"]


@pytest.mark.parametrize("text,description", [
    ("ＲＰＣ_ＱＵＥＵＥ_ＳＴＡＬＬＥＤ", "rpc_queue_stalled"),
    ("rpc_queue_stalled", "stalled"), ("rpc.queue-stalled", "queue"),
    ("RpcQueueStalled", "queue"), ("IO", "io"), ("DB", "db"),
    ("连接池耗尽", "连接池"), ("磁盘写入阻塞", "写入阻塞"),
])
def test_normalized_identifiers_segments_short_terms_and_chinese_bigrams_match(text, description):
    job, index = _inputs(text, _entry("alpha", summary=description))
    assert _recall(job, index).skill_ids == ("alpha",)


def test_complete_error_identifier_ranks_above_a_shared_identifier_segment():
    job, index = _inputs("ERR_QUARTZ_123", _entry("a-partial", summary="quartz"),
                         _entry("z-complete", summary="ERR_QUARTZ_123"))
    assert _recall(job, index).skill_ids == ("z-complete", "a-partial")


def test_explicit_keyword_count_precedes_lexical_score_and_keywords_count_once():
    entries = [
        _entry("a-lexical", capability="quartz basalt granite marble"),
        _entry("b-one", keywords=["ROUTE_SIGNATURE"]),
        _entry("c-two", keywords=["ROUTE_SIGNATURE", "SECOND_SIGNATURE"]),
    ]
    job, index = _inputs("quartz basalt granite marble ROUTE_SIGNATURE ROUTE_SIGNATURE SECOND_SIGNATURE", *entries)
    result = _recall(job, index)
    assert result.skill_ids == ("c-two", "b-one", "a-lexical")
    assert _ranked(result)["b-one"]["keyword_matches"] == ["ROUTE_SIGNATURE"]
    assert _ranked(result)["c-two"]["score"] == 0


@pytest.mark.parametrize("text,matched", [
    ("route_signature", True), ("ＲＯＵＴＥ_ＳＩＧＮＡＴＵＲＥ", True),
    ("出现ROUTE_SIGNATURE故障", True), ("(ROUTE_SIGNATURE)", True),
    ("XROUTE_SIGNATURE", False), ("ROUTE_SIGNATURE_X", False), ("ROUTE_SIGNATURE2", False),
])
def test_explicit_keywords_normalize_case_and_width_but_keep_ascii_boundaries(text, matched):
    job, index = _inputs(text, _entry("alpha", keywords=["ROUTE_SIGNATURE"]))
    result = _recall(job, index)
    assert result.skill_ids == (("alpha",) if matched else ())
    assert _ranked(result)["alpha"]["keyword_matches"] == (["ROUTE_SIGNATURE"] if matched else [])


def test_normalized_keyword_duplicates_do_not_increase_priority():
    job, index = _inputs("route_signature", _entry("alpha", keywords=["ROUTE_SIGNATURE", "route_signature"]))
    assert len(_ranked(_recall(job, index))["alpha"]["keyword_matches"]) == 1


def test_exclusions_and_diagnostic_material_metadata_do_not_add_recall_score():
    job, index = _inputs("quartz never_rank_this log_archive", _entry("alpha", exclusions=["quartz"]))
    assert _recall(job, index).skill_ids == ()


@pytest.mark.parametrize("field,is_list", [
    ("statement", False), ("scope", False), ("actual_behavior", False), ("expected_behavior", False),
    ("goals", True), ("non_goals", True), ("constraints", True), ("completion_criteria", True),
])
def test_current_problem_fields_are_all_searchable(field, is_list):
    job, index = _inputs("问题", _entry("alpha", summary="quartz"))
    value = job.model_dump(mode="json")
    value["context_snapshot"]["problem_spec"][field] = ["quartz"] if is_list else "quartz"
    assert _recall(Job.model_validate(value), index).skill_ids == ("alpha",)


@pytest.mark.parametrize("collection", ["user_facts", "confirmed_facts"])
@pytest.mark.parametrize("status,selected", [("ACTIVE", True), ("SUPERSEDED", False)])
def test_only_active_current_facts_affect_recall(collection, status, selected):
    job, index = _inputs("问题", _entry("alpha", summary="quartz"))
    value = job.model_dump(mode="json")
    fact = {
        "item_id": "00000000-0000-4000-8000-000000000498", "statement": "quartz", "status": status,
        "provenance": {"source_type": "USER_INPUT", "source_ref": "00000000-0000-4000-8000-000000000497", "input_name": "scope"},
        "evidence_refs": [], "created_revision": 1, "supersedes": [],
    }
    if collection == "confirmed_facts":
        fact["provenance"].update(source_type="AGENT_OUTCOME", input_name=None)
        fact["evidence_refs"] = ["00000000-0000-4000-8000-000000000499"]
        value["evidence_refs"] = list(fact["evidence_refs"])
        value["context_snapshot"]["evidence_refs"] = list(fact["evidence_refs"])
    value["context_snapshot"][collection] = [fact]
    assert _recall(Job.model_validate(value), index).skill_ids == (("alpha",) if selected else ())


@pytest.mark.parametrize("collection,status", [("active_hypotheses", "ACTIVE"), ("rejected_hypotheses", "REJECTED")])
def test_hypotheses_and_job_metadata_are_not_query_facts(collection, status):
    job, index = _inputs("问题", _entry("alpha", summary="quartz"))
    value = job.model_dump(mode="json")
    value["goal"] = "quartz"
    value["context_snapshot"][collection] = [{
        "item_id": "00000000-0000-4000-8000-000000000498", "statement": "quartz", "status": status,
        "provenance": {"source_type": "AGENT_OUTCOME", "source_ref": "00000000-0000-4000-8000-000000000497", "input_name": None},
        "evidence_refs": [], "created_revision": 1, "supersedes": [],
    }]
    assert _recall(Job.model_validate(value), index).skill_ids == ()


def test_recall_uses_the_changed_current_problem_even_when_prior_outcomes_exist():
    job, index = _inputs("quartz", _entry("alpha", summary="quartz"), _entry("beta", summary="basalt"))
    assert _recall(job, index).skill_ids == ("alpha",)
    value = job.model_dump(mode="json")
    value["context_snapshot"]["problem_spec"]["statement"] = "basalt"
    value["previous_outcome_refs"] = ["00000000-0000-4000-8000-000000000497"]
    assert _recall(Job.model_validate(value), index).skill_ids == ("beta",)


def test_no_routing_scope_is_excluded_from_candidates_and_rarity_count():
    legacy = _entry("legacy-quartz", capability="quartz")
    legacy["routing"] = None
    job, index = _inputs("quartz", legacy, _entry("alpha", summary="quartz"))
    result = _recall(job, index)
    assert result.skill_ids == ("alpha",)
    assert result.audit["ranked_candidates"] == [{
        "skill_id": "alpha", "score": 1, "keyword_matches": [], "selected": True,
    }]


def test_empty_catalog_produces_an_empty_model_index():
    job, index = _inputs("quartz")
    result = _recall(job, index)
    assert result.skill_ids == () and result.audit["ranked_candidates"] == []


def test_model_index_is_compact_but_preserves_selected_routing_descriptions_and_audit_identity():
    selected = _entry("diagnosis-skill/alpha", capability="quartz", summary="完整能力说明",
                      applicability="完整适用条件", exclusions=["完整排除条件"], keywords=["QUARTZ_SIGNATURE"])
    job, index = _inputs("quartz", selected, _entry("beta", summary="basalt"))
    result = _recall(job, index)
    assert json.loads(result.model_index) == {"schema_version": 1, "skills": [{
        "skill_id": "diagnosis-skill/alpha", "name": "alpha", "capability": "quartz", "summary": "完整能力说明",
        "routing": {"applicability": selected["routing"]["applicability"], "exclusions": selected["routing"]["exclusions"]},
    }]}
    assert result.audit["policy"] == "lexical_top5_v1"
    assert result.audit["candidate_ids"] == ["diagnosis-skill/alpha"]
    assert result.audit["context_snapshot_sha256"] == bytes_sha256(canonical_json_bytes(job.context_snapshot))
    assert result.audit["skill_index_sha256"] == bytes_sha256(canonical_json_bytes(index))
    assert result.audit["model_index_sha256"] == bytes_sha256(result.model_index.encode())


def test_recall_preserves_full_catalog_version_validation():
    job, index = _inputs("quartz", _entry("alpha", summary="quartz"), _entry("beta", summary="basalt"))
    index["skills"][1]["ref"]["content_hash"] = "c" * 64
    with pytest.raises(ValueError, match="冻结"):
        _recall(job, index)
