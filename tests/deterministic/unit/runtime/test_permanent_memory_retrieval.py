"""Permanent experience keeps independent sources and deduplicates recall content."""
from __future__ import annotations

from copy import deepcopy
from itertools import permutations

import pytest

from problem_locator.memory.retrieval import ExperienceRetriever
from tests.deterministic.unit.runtime.test_experience_memory import Store, card_json, row

LOW = "00000000-0000-0000-0000-000000000001"
HIGH = "00000000-0000-0000-0000-000000000002"
NEW = "00000000-0000-0000-0000-000000000003"


def rare_row(**kwargs):
    return row(raw=card_json(problem_features=["ECONNREFUSED"], applicability=["网络连接"]), **kwargs)


def test_identical_experience_sources_do_not_suppress_a_rare_error_match():
    earlier = rare_row(identity=LOW)
    later = rare_row(identity=HIGH, updated="2026-09-22T12:00:00Z")
    selected = ExperienceRetriever(Store(cards=[earlier, later])).select("generic", "ECONNREFUSED")
    assert selected is not None
    assert selected.card_id == HIGH


def test_duplicate_representative_is_stable_for_every_source_order():
    rows = [rare_row(identity=LOW), rare_row(identity=HIGH),
        rare_row(identity=NEW, updated="2026-09-20T12:00:00Z")]
    for order in permutations(rows):
        selected = ExperienceRetriever(Store(cards=order)).select("generic", "ECONNREFUSED")
        assert selected is not None and selected.card_id == HIGH


def test_deduplication_does_not_merge_sources_or_prevent_remaining_source_recall():
    earlier, later = rare_row(identity=LOW), rare_row(identity=HIGH)
    later["source_kind"] = "CORRECTION"
    store = Store(cards=[earlier, later])
    before = deepcopy(store.cards)
    retriever = ExperienceRetriever(store)
    selected = retriever.select("generic", "ECONNREFUSED")
    assert selected is not None and selected.card_id == HIGH and selected.source_kind == "CORRECTION"
    assert store.cards == before
    # Storage controls each source independently; removing one leaves the other usable.
    store.cards.remove(later)
    remaining = retriever.select("generic", "ECONNREFUSED")
    assert remaining is not None and remaining.card_id == LOW and remaining.source_kind == "REPORT"
    store.cards.clear()
    assert retriever.select("generic", "ECONNREFUSED") is None


def test_distinct_experiences_sharing_an_error_code_are_not_merged():
    original = rare_row(identity=LOW)
    distinct = row(identity=HIGH, raw=card_json(problem_features=["ECONNREFUSED"],
        applicability=["网络连接"], steps=["先检查服务监听状态"]))
    assert original["card_sha256"] != distinct["card_sha256"]
    assert ExperienceRetriever(Store(cards=[original, distinct])).select("generic", "ECONNREFUSED") is None


def test_other_skill_duplicates_do_not_affect_matching_or_source_selection():
    wanted = rare_row(identity=LOW)
    other = rare_row(identity=HIGH, skill="other")
    retriever = ExperienceRetriever(Store(cards=[other, wanted]))
    assert retriever.select("generic", "ECONNREFUSED").card_id == LOW
    assert retriever.select("other", "ECONNREFUSED").card_id == HIGH


@pytest.mark.parametrize("invalid", [
    {"card_sha256": "0" * 64},
    {"card_json": "not a card"},
    {"card_id": "not-a-uuid"},
    {"source_kind": "unrecognized"},
])
@pytest.mark.parametrize("invalid_first", [False, True])
def test_invalid_source_never_swallows_valid_identical_experience(invalid, invalid_first):
    good = rare_row(identity=LOW)
    bad = {**rare_row(identity=HIGH, updated="2026-09-22T12:00:00Z"), **invalid}
    rows = [bad, good] if invalid_first else [good, bad]
    selected = ExperienceRetriever(Store(cards=rows)).select("generic", "ECONNREFUSED")
    assert selected is not None and selected.card_id == LOW


def test_over_budget_duplicate_does_not_hide_an_eligible_source(monkeypatch):
    from problem_locator.memory import retrieval
    good = rare_row(identity=LOW)
    reference = ExperienceRetriever(Store(cards=[good])).select("generic", "ECONNREFUSED")
    monkeypatch.setattr(retrieval, "MAX_CARD_BYTES", len(reference.reference_text.encode("utf-8")))
    longer = {**rare_row(identity=HIGH), "source_kind": "CORRECTION"}
    selected = ExperienceRetriever(Store(cards=[longer, good])).select("generic", "ECONNREFUSED")
    assert selected is not None and selected.card_id == LOW


def test_recall_accepts_a_single_pass_source_iterator():
    rows = [rare_row(identity=LOW), rare_row(identity=HIGH)]
    class StreamingStore:
        def active_cards(self, skill_name):
            return iter(rows)
    selected = ExperienceRetriever(StreamingStore()).select("generic", "ECONNREFUSED")
    assert selected is not None and selected.card_id == HIGH


def test_paged_source_reader_is_preferred_over_eager_compatibility_reader():
    rows = [rare_row(identity=LOW), rare_row(identity=HIGH)]
    class PagedStore:
        def active_cards(self, skill_name):
            pytest.fail("生产召回应使用分批读取入口")
        def iter_active_cards(self, skill_name):
            assert skill_name == "generic"
            yield from rows
    selected = ExperienceRetriever(PagedStore()).select("generic", "ECONNREFUSED")
    assert selected is not None and selected.card_id == HIGH
