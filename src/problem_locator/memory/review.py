"""Validate correction attribution against a recorded memory use and checked citations."""
from __future__ import annotations

import hashlib
import json
import uuid

from .extraction import parse_card_json

_FIELDS = {"card_id", "card_sha256", "relation", "evidence_indexes", "scope_terms"}
RELATIONS = frozenset({"GLOBAL_ERROR", "NOT_APPLICABLE", "DIAGNOSIS_OMISSION", "UNRELATED"})
SCOPED_RELATIONS = frozenset({"NOT_APPLICABLE", "DIAGNOSIS_OMISSION"})


def validate_memory_review(value, usage, evidence, card_json):
    """Check identity and citation bindings; the reviewer decides the semantic relation."""
    # Import lazily so recall can share these rules without a module cycle.
    from .retrieval import _ERROR_CODE, keywords

    if not isinstance(value, dict) or set(value) != _FIELDS or not isinstance(usage, dict):
        raise ValueError("经验纠正关联字段无效。")
    card_id, old_raw, old_hash = usage.get("card_id"), usage.get("card_json"), usage.get("card_sha256")
    if (not isinstance(card_id, str) or str(uuid.UUID(card_id)) != card_id
            or value["card_id"] != card_id or value["card_sha256"] != old_hash
            or not isinstance(old_raw, str)):
        raise ValueError("经验纠正未关联本次实际采用的经验。")
    old_card = parse_card_json(old_raw)
    if old_card != old_raw or hashlib.sha256(old_card.encode("utf-8")).hexdigest() != old_hash:
        raise ValueError("实际采用的经验内容与记录不一致。")
    relation, indexes, terms = value["relation"], value["evidence_indexes"], value["scope_terms"]
    if not isinstance(relation, str) or relation not in RELATIONS:
        raise ValueError("经验纠正关系无效。")
    if (not isinstance(evidence, list) or not 1 <= len(evidence) <= 4
            or not isinstance(indexes, list) or len(indexes) > len(evidence)
            or any(type(index) is not int or not 0 <= index < len(evidence) for index in indexes)
            or len(set(indexes)) != len(indexes) or (relation != "UNRELATED" and not indexes)):
        raise ValueError("经验纠正缺少对应的已核验证据。")
    if not isinstance(terms, list) or any(not isinstance(term, str) for term in terms):
        raise ValueError("经验纠正的适用范围无效。")
    canonical = parse_card_json(card_json)
    if relation != "UNRELATED" and canonical == old_card:
        raise ValueError("纠正经验必须修正已有内容，不能原样重新发布。")
    if relation in SCOPED_RELATIONS:
        card = json.loads(canonical)
        features = keywords(" ".join(card["problem_features"] + card["applicability"]))
        single_code = len(terms) == 1 and len(terms[0]) >= 8 and _ERROR_CODE.fullmatch(terms[0])
        if (not (2 <= len(terms) <= 8 or single_code) or len(set(terms)) != len(terms)
                or any(term != term.casefold() or keywords(term) != frozenset({term})
                    or term not in features for term in terms)):
            raise ValueError("经验纠正必须明确最终经验中的适用条件。")
    elif terms:
        raise ValueError("本类经验纠正不能附带局部适用范围。")
    return {"card_id": card_id, "card_sha256": old_hash, "relation": relation,
        "evidence_indexes": list(indexes), "scope_terms": list(terms)}
