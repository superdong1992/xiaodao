"""Corrected guidance is selected only inside its validated diagnostic scope."""
from __future__ import annotations

import json
from itertools import permutations
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from problem_locator.memory.retrieval import ExperienceRetriever
from tests.deterministic.unit.runtime.test_experience_memory import Store, card_json, row


def candidate(*, words=("redis", "pool", "timeout"), applies=("queue",), corrected=False,
              identity=None, updated="2026-10-19T00:00:00Z", **metadata):
    raw = card_json(problem_features=list(words), applicability=list(applies),
        steps=["先排除慢查询长期占用，再核对连接释放" if corrected else "核对连接释放"])
    return {**row(raw=raw, identity=identity, updated=updated),
        "source_kind": "CORRECTION" if corrected else "REPORT",
        "correction_verified": corrected, "revision_kind": None,
        "revises_sha256": None, "scope_terms_json": None, **metadata}


def scoped(old, *, scope=("redis", "pool"), **changes):
    return candidate(corrected=True, revision_kind="NOT_APPLICABLE",
        revises_sha256=old["card_sha256"], scope_terms_json=json.dumps(scope), **changes)


def choose(cards, query="redis pool timeout queue"):
    return ExperienceRetriever(Store(cards=cards)).select("generic", query)


def test_verified_correction_wins_equal_overlap_before_recency_or_precision():
    normal = candidate(words=("redis", "pool"), applies=("queue",))
    correction = candidate(corrected=True, words=("redis", "pool", "timeout"),
        applies=("queue",), updated="2026-10-18T00:00:00Z")
    assert choose([normal, correction], "redis pool queue").card_id == correction["card_id"]


@pytest.mark.parametrize("verified", [False, None, 1, "true"])
def test_unverified_correction_label_has_no_ranking_priority(verified):
    normal = candidate()
    correction = candidate(corrected=True, correction_verified=verified,
        updated="2026-10-18T00:00:00Z")
    assert choose([normal, correction]).card_id == normal["card_id"]


def test_verified_correction_does_not_override_stronger_unrelated_guidance():
    normal = candidate(words=("redis", "pool", "timeout", "queue", "wait"))
    correction = candidate(corrected=True, words=("redis", "pool"), applies=("slowquery",))
    assert choose([normal, correction], "redis pool timeout queue wait").card_id == normal["card_id"]


@pytest.mark.parametrize("kind", ["NOT_APPLICABLE", "DIAGNOSIS_OMISSION"])
def test_matching_scope_suppresses_the_revised_content_even_if_it_matches_more_words(kind):
    old = candidate(words=("redis", "pool", "timeout", "queue", "wait"))
    correction = scoped(old, words=("redis", "pool"), applies=("slowquery",))
    correction["revision_kind"] = kind
    assert choose([old, correction], "redis pool timeout queue wait").card_id == correction["card_id"]


def test_nonmatching_scope_preserves_old_guidance_and_excludes_scoped_correction():
    old = candidate()
    correction = scoped(old, scope=("redis", "slowquery"))
    assert choose([old, correction]).card_id == old["card_id"]
    assert choose([correction]) is None


def test_matching_scope_alone_does_not_bypass_normal_relevance():
    old = candidate()
    correction = scoped(old, words=("postgres", "replica"), applies=("lag",))
    assert choose([old, correction]).card_id == old["card_id"]


def test_duplicate_corrected_content_keeps_every_matching_replacement_edge():
    old_a = candidate(words=("redis", "pool", "timeout", "queue", "wait"))
    old_b = candidate(words=("redis", "pool", "timeout", "queue", "wait", "socket"))
    first = scoped(old_a, words=("redis", "pool"), applies=("slowquery",))
    second = scoped(old_b, words=("redis", "pool"), applies=("slowquery",))
    assert first["card_sha256"] == second["card_sha256"]
    for ordering in permutations([old_a, old_b, first, second]):
        selected = choose(ordering, "redis pool timeout queue wait socket")
        assert selected.card_sha256 == first["card_sha256"]


def test_content_dedup_prefers_verified_correction_source_before_newer_ordinary_source():
    correction = candidate(corrected=True, updated="2026-10-18T00:00:00Z")
    normal = {**correction, "card_id": "00000000-0000-0000-0000-000000000099",
        "source_kind": "REPORT", "correction_verified": False,
        "updated_at": "2026-10-19T00:00:00Z"}
    for ordering in ([normal, correction], [correction, normal]):
        assert choose(ordering).card_id == correction["card_id"]


@pytest.mark.parametrize("invalid", [
    {"correction_verified": False}, {"source_kind": "REPORT"},
    {"revision_kind": "unknown"}, {"revises_sha256": "broken"},
    {"scope_terms_json": "null"}, {"scope_terms_json": "[1,2]"},
    {"scope_terms_json": '["redis"]'}, {"scope_terms_json": '["redis","redis"]'},
    {"scope_terms_json": '["REDIS","pool"]'},
    {"scope_terms_json": '["redis pool","queue"]'},
    {"scope_terms_json": '["redis","problem"]'},
    {"scope_terms_json": '["redis","pool","timeout","queue","wait","socket","foo","bar","baz"]'},
])
def test_malformed_revision_cannot_suppress_or_replace_valid_guidance(invalid):
    old = candidate()
    correction = {**scoped(old), **invalid}
    assert choose([old, correction]).card_id == old["card_id"]


def test_single_specific_error_code_can_scope_a_correction():
    old = candidate()
    correction = scoped(old, scope=("econnrefused",), words=("ECONNREFUSED",), applies=("listener",))
    assert choose([correction], "ECONNREFUSED").card_id == correction["card_id"]


def test_over_budget_matching_correction_still_suppresses_old_content():
    old = candidate()
    correction = scoped(old)
    correction["card_json"] = card_json(problem_features=["redis", "pool"],
        applicability=["queue"], steps=["检查" + "队列" * 120] * 5)
    import hashlib
    correction["card_sha256"] = hashlib.sha256(correction["card_json"].encode()).hexdigest()
    # Keep the independently validated old-content target after changing card bytes.
    assert choose([old, correction]) is None


def test_scoped_replacement_chain_does_not_reintroduce_an_earlier_card():
    old = candidate(words=("redis", "pool", "timeout", "queue", "wait"))
    first = scoped(old, words=("redis", "pool", "timeout"), applies=("slowquery",))
    final = scoped(first, words=("redis", "pool"), applies=("latency",))
    assert choose([old, first, final], "redis pool timeout queue wait").card_id == final["card_id"]


def test_recall_adds_no_per_candidate_database_queries():
    old = candidate()
    correction = scoped(old)
    store = SimpleNamespace(iter_active_cards=Mock(return_value=iter([old, correction])),
        is_card_active=Mock(return_value=True))
    result = ExperienceRetriever(store).select("generic", "redis pool timeout queue")
    assert result.card_id == correction["card_id"]
    store.iter_active_cards.assert_called_once_with("generic")
    store.is_card_active.assert_called_once()


def test_usage_recording_is_one_optional_write_and_failure_does_not_escape():
    store = Store(cards=[candidate()])
    store.record_usage = Mock(return_value=True)
    retriever = ExperienceRetriever(store)
    selection = retriever.select("generic", "redis pool")
    job = object()
    retriever.record_usage(job, selection)
    store.record_usage.assert_called_once_with(job, selection)
    store.record_usage.side_effect = RuntimeError("private storage failure")
    retriever.record_usage(job, selection)


def test_scope_terms_must_be_supported_by_the_corrected_card_features():
    old = candidate()
    correction = scoped(old, words=("redis", "timeout"), applies=("queue",))
    assert choose([old, correction]).card_id == old["card_id"]


def test_single_error_code_scope_survives_the_old_and_corrected_card_pair():
    old = candidate(words=("ECONNREFUSED",), applies=("listener",))
    correction = scoped(old, scope=("econnrefused",), words=("ECONNREFUSED",), applies=("socket",))
    assert choose([old, correction], "ECONNREFUSED").card_id == correction["card_id"]


@pytest.mark.parametrize("raw_scope", [None, "[]"])
def test_global_correction_accepts_only_an_empty_scope_projection(raw_scope):
    old = candidate()
    correction = candidate(corrected=True, revision_kind="GLOBAL_ERROR",
        revises_sha256=old["card_sha256"], scope_terms_json=raw_scope,
        updated="2026-10-18T00:00:00Z")
    assert choose([old, correction]).card_id == correction["card_id"]
    correction["scope_terms_json"] = '["redis","pool"]'
    assert choose([old, correction]).card_id == old["card_id"]


def test_cached_card_content_does_not_cache_scope_or_revision_eligibility():
    old = candidate()
    correction = scoped(old, scope=("redis", "pool"), words=("redis", "pool", "timeout"))
    retriever = ExperienceRetriever(Store(cards=[old, correction]))
    assert retriever.select("generic", "redis timeout queue").card_id == old["card_id"]
    assert retriever.select("generic", "redis pool timeout queue").card_id == correction["card_id"]
    correction["scope_terms_json"] = '["redis","slowquery"]'
    assert retriever.select("generic", "redis pool timeout queue").card_id == old["card_id"]
