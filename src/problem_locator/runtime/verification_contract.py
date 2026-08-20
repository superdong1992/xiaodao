"""Strict validation helpers for Diagnosis Skill verification contract v2.

The contract is deliberately data-only.  Business event names, message text,
thresholds, clock tolerances, and observation windows remain inside an
individual Diagnosis Skill; this module only validates the generic language.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


MANIFEST_SCHEMA_VERSION = 6
VERIFICATION_CONTRACT_SCHEMA_VERSION = 2

_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_RULE_KINDS = frozenset(
    {
        "EVENT_COUNT",
        "EVENT_PRESENT",
        "EVENT_TIME_WINDOW",
        "FACT_FIELD_EQUALS",
        "FACT_IN",
        "FIELDS_EQUAL",
        "ROLE_COVERAGE",
        "CROSS_ROLE_CORRELATION",
        "EVENT_ORDER",
        "NUMERIC_COMPARE",
        "SEMANTIC_CAUSALITY",
    }
)
_TIME_UNITS = frozenset(
    {"NANOSECOND", "MICROSECOND", "MILLISECOND", "SECOND", "MINUTE"}
)
_INTEGER_UNITS = _TIME_UNITS | {"COUNT", "BYTE"}


def _exact(value: Mapping[str, Any], fields: set[str], name: str) -> None:
    actual = set(value)
    if actual != fields:
        raise ValueError(
            f"{name} fields are invalid; missing={sorted(fields - actual)!r}, "
            f"extra={sorted(actual - fields)!r}"
        )


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _array(value: Any, name: str, *, maximum: int, minimum: int = 0) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(
            f"{name} must be an array with {minimum}..{maximum} items"
        )
    return value


def _name(value: Any, name: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise ValueError(f"{name} must be lower snake case")
    return value


def _text(value: Any, name: str, *, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\r" in value
        or "\n" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise ValueError(f"{name} must be canonical single-line UTF-8 text")
    return value


def _names(
    value: Any,
    name: str,
    *,
    maximum: int = 100,
    minimum: int = 0,
) -> list[str]:
    result = [
        _name(item, f"{name}[]")
        for item in _array(value, name, maximum=maximum, minimum=minimum)
    ]
    if len(result) != len(set(result)):
        raise ValueError(f"{name} entries must be unique")
    return result


def _non_negative_int(value: Any, name: str, *, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer in 0..{maximum}")
    return value


def _positive_int(value: Any, name: str, *, maximum: int) -> int:
    result = _non_negative_int(value, name, maximum=maximum)
    if result == 0:
        raise ValueError(f"{name} must be positive")
    return result


def _binding(value: Any, name: str) -> dict[str, str]:
    value = _mapping(value, name)
    source = value.get("source")
    if source == "USER_FACT":
        _exact(value, {"source", "name"}, name)
        return {"source": source, "name": _name(value["name"], f"{name}.name")}
    if source == "SKILL_FIXED":
        _exact(value, {"source", "value"}, name)
        return {
            "source": source,
            "value": _text(value["value"], f"{name}.value"),
        }
    raise ValueError(f"{name}.source must be USER_FACT or SKILL_FIXED")


def _event_field(value: Any, name: str) -> dict[str, str]:
    value = _mapping(value, name)
    _exact(value, {"event", "field"}, name)
    return {
        "event": _name(value["event"], f"{name}.event"),
        "field": _name(value["field"], f"{name}.field"),
    }


def _normalize_expression(value: Any, name: str, *, depth: int = 0) -> dict[str, Any]:
    if depth > 8:
        raise ValueError("numeric expression nesting is too deep")
    value = _mapping(value, name)
    kind = value.get("kind")
    if kind == "FIELD":
        _exact(value, {"kind", "event", "field"}, name)
        return {
            "kind": kind,
            **_event_field(
                {"event": value["event"], "field": value["field"]},
                name,
            ),
        }
    if kind == "FACT":
        _exact(
            value,
            {"kind", "name", "value_type", "unit", "clock_domain"},
            name,
        )
        value_type = value["value_type"]
        unit = value["unit"]
        clock = value["clock_domain"]
        if value_type == "INTEGER":
            if unit not in _INTEGER_UNITS or (
                clock is not None and not isinstance(clock, str)
            ):
                raise ValueError(f"{name} INTEGER type metadata is invalid")
        elif value_type == "TIMESTAMP":
            if unit is not None or not isinstance(clock, str) or not clock:
                raise ValueError(f"{name} TIMESTAMP type metadata is invalid")
        else:
            raise ValueError(f"{name}.value_type is invalid")
        return {
            "kind": kind,
            "name": _name(value["name"], f"{name}.name"),
            "value_type": value_type,
            "unit": unit,
            "clock_domain": clock,
        }
    if kind == "CONST":
        _exact(value, {"kind", "value", "unit"}, name)
        if type(value["value"]) is not int or value["unit"] not in _INTEGER_UNITS:
            raise ValueError(f"{name} constant is invalid")
        return {"kind": kind, "value": value["value"], "unit": value["unit"]}
    if kind in {"ADD", "SUBTRACT"}:
        _exact(value, {"kind", "left", "right"}, name)
        return {
            "kind": kind,
            "left": _normalize_expression(
                value["left"], f"{name}.left", depth=depth + 1
            ),
            "right": _normalize_expression(
                value["right"], f"{name}.right", depth=depth + 1
            ),
        }
    if kind == "MULTIPLY_CONST":
        _exact(value, {"kind", "operand", "multiplier"}, name)
        multiplier = value["multiplier"]
        if type(multiplier) is not int or not -1_000_000 <= multiplier <= 1_000_000:
            raise ValueError(f"{name}.multiplier is invalid")
        return {
            "kind": kind,
            "operand": _normalize_expression(
                value["operand"], f"{name}.operand", depth=depth + 1
            ),
            "multiplier": multiplier,
        }
    if kind == "CONVERT":
        _exact(value, {"kind", "operand", "unit"}, name)
        if value["unit"] not in _INTEGER_UNITS:
            raise ValueError(f"{name}.unit is invalid")
        return {
            "kind": kind,
            "operand": _normalize_expression(
                value["operand"], f"{name}.operand", depth=depth + 1
            ),
            "unit": value["unit"],
        }
    raise ValueError(f"{name}.kind is invalid")


def _expression_event_fields(expression: Mapping[str, Any]) -> list[tuple[str, str]]:
    kind = expression["kind"]
    if kind == "FIELD":
        return [(expression["event"], expression["field"])]
    if kind in {"ADD", "SUBTRACT"}:
        return [
            *_expression_event_fields(expression["left"]),
            *_expression_event_fields(expression["right"]),
        ]
    if kind in {"MULTIPLY_CONST", "CONVERT"}:
        return _expression_event_fields(expression["operand"])
    return []


def _expression_fact_names(expression: Mapping[str, Any]) -> list[str]:
    kind = expression["kind"]
    if kind == "FACT":
        return [expression["name"]]
    if kind in {"ADD", "SUBTRACT"}:
        return [
            *_expression_fact_names(expression["left"]),
            *_expression_fact_names(expression["right"]),
        ]
    if kind in {"MULTIPLY_CONST", "CONVERT"}:
        return _expression_fact_names(expression["operand"])
    return []


def _equalities(value: Any, name: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(
        _array(value, name, maximum=20, minimum=1)
    ):
        item_name = f"{name}[{index}]"
        raw = _mapping(raw, item_name)
        _exact(raw, {"members"}, item_name)
        members = [
            _event_field(member, f"{item_name}.members[{member_index}]")
            for member_index, member in enumerate(
                _array(raw["members"], f"{item_name}.members", maximum=20, minimum=2)
            )
        ]
        keys = [(item["event"], item["field"]) for item in members]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{item_name}.members must be unique")
        result.append({"members": members})
    return result


def _rule_parameters(
    kind: str,
    value: Any,
    name: str,
) -> dict[str, Any]:
    value = _mapping(value, name)
    if kind == "EVENT_PRESENT":
        _exact(value, {"event"}, name)
        return {"event": _name(value["event"], f"{name}.event")}
    if kind == "EVENT_COUNT":
        _exact(value, {"event", "min_count", "max_count"}, name)
        minimum = _non_negative_int(value["min_count"], f"{name}.min_count", maximum=1_000_000)
        maximum = value["max_count"]
        if maximum is not None:
            maximum = _non_negative_int(maximum, f"{name}.max_count", maximum=1_000_000)
            if maximum < minimum:
                raise ValueError(f"{name}.max_count must be >= min_count")
        return {
            "event": _name(value["event"], f"{name}.event"),
            "min_count": minimum,
            "max_count": maximum,
        }
    if kind == "EVENT_TIME_WINDOW":
        _exact(
            value,
            {
                "event",
                "reference",
                "before_ms",
                "after_ms",
                "lower_bound",
                "upper_bound",
                "quantifier",
                "clock_tolerance_ms",
            },
            name,
        )
        if value["lower_bound"] not in {"INCLUSIVE", "EXCLUSIVE"} or value[
            "upper_bound"
        ] not in {"INCLUSIVE", "EXCLUSIVE"}:
            raise ValueError(f"{name} boundary semantics are invalid")
        if value["quantifier"] not in {"ANY", "ALL"}:
            raise ValueError(f"{name}.quantifier is invalid")
        return {
            "event": _name(value["event"], f"{name}.event"),
            "reference": _binding(value["reference"], f"{name}.reference"),
            "before_ms": _non_negative_int(value["before_ms"], f"{name}.before_ms", maximum=604_800_000),
            "after_ms": _non_negative_int(value["after_ms"], f"{name}.after_ms", maximum=604_800_000),
            "lower_bound": value["lower_bound"],
            "upper_bound": value["upper_bound"],
            "quantifier": value["quantifier"],
            "clock_tolerance_ms": _non_negative_int(
                value["clock_tolerance_ms"],
                f"{name}.clock_tolerance_ms",
                maximum=86_400_000,
            ),
        }
    if kind == "FACT_FIELD_EQUALS":
        _exact(value, {"event", "field", "fact_name", "quantifier"}, name)
        if value["quantifier"] not in {"ANY", "ALL"}:
            raise ValueError(f"{name}.quantifier is invalid")
        return {
            **_event_field(
                {"event": value["event"], "field": value["field"]},
                name,
            ),
            "fact_name": _name(value["fact_name"], f"{name}.fact_name"),
            "quantifier": value["quantifier"],
        }
    if kind == "FACT_IN":
        _exact(value, {"fact_name", "allowed_values"}, name)
        allowed = [
            _text(item, f"{name}.allowed_values[]")
            for item in _array(value["allowed_values"], f"{name}.allowed_values", maximum=100, minimum=1)
        ]
        if len(allowed) != len(set(allowed)):
            raise ValueError(f"{name}.allowed_values must be unique")
        return {
            "fact_name": _name(value["fact_name"], f"{name}.fact_name"),
            "allowed_values": allowed,
        }
    if kind == "FIELDS_EQUAL":
        _exact(value, {"equalities", "quantifier"}, name)
        if value["quantifier"] != "EXISTS":
            raise ValueError(f"{name}.quantifier must be EXISTS")
        return {
            "equalities": _equalities(value["equalities"], f"{name}.equalities"),
            "quantifier": "EXISTS",
        }
    if kind == "CROSS_ROLE_CORRELATION":
        _exact(value, {"members"}, name)
        return {
            "members": [
                _event_field(item, f"{name}.members[{index}]")
                for index, item in enumerate(
                    _array(value["members"], f"{name}.members", maximum=20, minimum=2)
                )
            ]
        }
    if kind == "ROLE_COVERAGE":
        _exact(value, {"coverage"}, name)
        coverage: list[dict[str, str]] = []
        for index, raw in enumerate(
            _array(value["coverage"], f"{name}.coverage", maximum=20, minimum=1)
        ):
            raw = _mapping(raw, f"{name}.coverage[{index}]")
            _exact(raw, {"role", "event"}, f"{name}.coverage[{index}]")
            coverage.append(
                {
                    "role": _name(raw["role"], f"{name}.coverage[{index}].role"),
                    "event": _name(raw["event"], f"{name}.coverage[{index}].event"),
                }
            )
        if len({item["role"] for item in coverage}) != len(coverage):
            raise ValueError(f"{name}.coverage roles must be unique")
        return {"coverage": coverage}
    if kind == "EVENT_ORDER":
        _exact(
            value,
            {
                "before_event",
                "after_event",
                "allow_equal",
                "quantifier",
                "clock_tolerance_ms",
                "joins",
            },
            name,
        )
        if type(value["allow_equal"]) is not bool or value["quantifier"] != "EXISTS":
            raise ValueError(f"{name} order options are invalid")
        joins = [] if not value["joins"] else _equalities(value["joins"], f"{name}.joins")
        return {
            "before_event": _name(value["before_event"], f"{name}.before_event"),
            "after_event": _name(value["after_event"], f"{name}.after_event"),
            "allow_equal": value["allow_equal"],
            "quantifier": "EXISTS",
            "clock_tolerance_ms": _non_negative_int(
                value["clock_tolerance_ms"],
                f"{name}.clock_tolerance_ms",
                maximum=86_400_000,
            ),
            "joins": joins,
        }
    if kind == "NUMERIC_COMPARE":
        _exact(
            value,
            {
                "left",
                "operator",
                "right",
                "quantifier",
                "joins",
                "clock_tolerance_ms",
            },
            name,
        )
        if value["operator"] not in {"LT", "LTE", "EQ", "GTE", "GT"}:
            raise ValueError(f"{name}.operator is invalid")
        if value["quantifier"] not in {"EXISTS", "ALL"}:
            raise ValueError(f"{name}.quantifier is invalid")
        return {
            "left": _normalize_expression(value["left"], f"{name}.left"),
            "operator": value["operator"],
            "right": _normalize_expression(value["right"], f"{name}.right"),
            "quantifier": value["quantifier"],
            "joins": [] if not value["joins"] else _equalities(value["joins"], f"{name}.joins"),
            "clock_tolerance_ms": _non_negative_int(
                value["clock_tolerance_ms"],
                f"{name}.clock_tolerance_ms",
                maximum=86_400_000,
            ),
        }
    if kind == "SEMANTIC_CAUSALITY":
        _exact(value, {"assertion", "evidence_events"}, name)
        return {
            "assertion": _text(value["assertion"], f"{name}.assertion"),
            "evidence_events": _names(value["evidence_events"], f"{name}.evidence_events"),
        }
    raise ValueError(f"{name} has unsupported rule kind {kind!r}")


def _rule_event_fields(rule: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    kind = rule["kind"]
    parameters = rule["parameters"]
    if kind in {"EVENT_COUNT", "EVENT_PRESENT", "EVENT_TIME_WINDOW"}:
        return [(parameters["event"], None)]
    if kind == "FACT_FIELD_EQUALS":
        return [(parameters["event"], parameters["field"])]
    if kind == "FIELDS_EQUAL":
        return [
            (member["event"], member["field"])
            for equality in parameters["equalities"]
            for member in equality["members"]
        ]
    if kind == "CROSS_ROLE_CORRELATION":
        return [(item["event"], item["field"]) for item in parameters["members"]]
    if kind == "ROLE_COVERAGE":
        return [(item["event"], None) for item in parameters["coverage"]]
    if kind == "EVENT_ORDER":
        return [
            (parameters["before_event"], None),
            (parameters["after_event"], None),
            *(
                (member["event"], member["field"])
                for equality in parameters["joins"]
                for member in equality["members"]
            ),
        ]
    if kind == "NUMERIC_COMPARE":
        return [
            *_expression_event_fields(parameters["left"]),
            *_expression_event_fields(parameters["right"]),
            *(
                (member["event"], member["field"])
                for equality in parameters["joins"]
                for member in equality["members"]
            ),
        ]
    if kind == "SEMANTIC_CAUSALITY":
        return [(event, None) for event in parameters["evidence_events"]]
    return []


def _rule_fact_names(rule: Mapping[str, Any]) -> list[str]:
    kind = rule["kind"]
    parameters = rule["parameters"]
    if kind == "FACT_FIELD_EQUALS":
        return [parameters["fact_name"]]
    if kind == "FACT_IN":
        return [parameters["fact_name"]]
    if kind == "EVENT_TIME_WINDOW" and parameters["reference"]["source"] == "USER_FACT":
        return [parameters["reference"]["name"]]
    if kind == "NUMERIC_COMPARE":
        return [
            *_expression_fact_names(parameters["left"]),
            *_expression_fact_names(parameters["right"]),
        ]
    return []


def validate_verification_contract(
    value: Any,
    *,
    requirements: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    anchor_labels: set[str],
    role_labels: set[str],
    requires_logparse: bool,
) -> dict[str, Any]:
    """Validate and normalize one verification contract v2 mapping."""

    value = _mapping(value, "verification_contract")
    _exact(
        value,
        {
            "schema_version",
            "observation_policies",
            "event_extractors",
            "rules",
            "terminal_paths",
        },
        "verification_contract",
    )
    if value["schema_version"] != VERIFICATION_CONTRACT_SCHEMA_VERSION:
        raise ValueError("verification_contract schema_version must equal integer 2")

    policies: list[dict[str, Any]] = []
    policy_by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(
        _array(value["observation_policies"], "observation_policies", maximum=100)
    ):
        name = f"verification_contract.observation_policies[{index}]"
        raw = _mapping(raw, name)
        _exact(
            raw,
            {"id", "kind", "scope", "key_fields", "window_ms", "max_observed", "boundary"},
            name,
        )
        policy_id = _name(raw["id"], f"{name}.id")
        if policy_id in policy_by_id:
            raise ValueError("observation policy IDs must be unique")
        kind = raw["kind"]
        if kind not in {"SUPPRESSION", "RATE_LIMIT"}:
            raise ValueError(f"{name}.kind is invalid")
        maximum = raw["max_observed"]
        if kind == "SUPPRESSION":
            if maximum is not None:
                raise ValueError("SUPPRESSION max_observed must be null")
        else:
            maximum = _positive_int(maximum, f"{name}.max_observed", maximum=1_000_000)
        if raw["boundary"] not in {"CLOSED_OPEN", "CLOSED_CLOSED"}:
            raise ValueError(f"{name}.boundary is invalid")
        policy = {
            "id": policy_id,
            "kind": kind,
            "scope": _name(raw["scope"], f"{name}.scope"),
            "key_fields": _names(raw["key_fields"], f"{name}.key_fields"),
            "window_ms": _positive_int(raw["window_ms"], f"{name}.window_ms", maximum=604_800_000),
            "max_observed": maximum,
            "boundary": raw["boundary"],
        }
        policy_by_id[policy_id] = policy
        policies.append(policy)

    extractors: list[dict[str, Any]] = []
    extractor_by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(
        _array(value["event_extractors"], "event_extractors", maximum=100)
    ):
        name = f"verification_contract.event_extractors[{index}]"
        raw = _mapping(raw, name)
        _exact(
            raw,
            {
                "id",
                "anchor",
                "members",
                "fields",
                "timestamp_field",
                "group_by",
                "selectors",
                "max_gap_lines",
                "min_matches",
                "max_matches",
                "observation_policy_ids",
            },
            name,
        )
        event_id = _name(raw["id"], f"{name}.id")
        if event_id in extractor_by_id:
            raise ValueError("event extractor IDs must be unique")
        anchor = _name(raw["anchor"], f"{name}.anchor")
        if anchor not in anchor_labels:
            raise ValueError("event extractor anchor must name a Logparse anchor")
        members: list[dict[str, str]] = []
        captured_names: set[str] = set()
        for member_index, member in enumerate(
            _array(raw["members"], f"{name}.members", maximum=16, minimum=1)
        ):
            member_name = f"{name}.members[{member_index}]"
            member = _mapping(member, member_name)
            _exact(member, {"line_pattern", "match_mode"}, member_name)
            pattern = _text(member["line_pattern"], f"{member_name}.line_pattern", maximum=8192)
            mode = member["match_mode"]
            if mode not in {"FULL_LINE", "SEARCH"}:
                raise ValueError(f"{member_name}.match_mode is invalid")
            if mode == "FULL_LINE" and not (pattern.startswith("^") and pattern.endswith("$")):
                raise ValueError("FULL_LINE patterns must be anchored")
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"{member_name}.line_pattern is invalid") from exc
            if any(_NAME.fullmatch(item) is None for item in compiled.groupindex):
                raise ValueError("event capture groups must be lower snake case")
            captured_names.update(compiled.groupindex)
            members.append({"line_pattern": pattern, "match_mode": mode})

        fields: list[dict[str, Any]] = []
        field_by_name: dict[str, dict[str, Any]] = {}
        for field_index, field in enumerate(
            _array(raw["fields"], f"{name}.fields", maximum=100, minimum=1)
        ):
            field_name = f"{name}.fields[{field_index}]"
            field = _mapping(field, field_name)
            _exact(field, {"name", "type", "unit", "clock_domain"}, field_name)
            capture_name = _name(field["name"], f"{field_name}.name")
            if capture_name in field_by_name:
                raise ValueError("event field names must be unique")
            field_type = field["type"]
            unit = field["unit"]
            clock = field["clock_domain"]
            if field_type == "STRING":
                if unit is not None or clock is not None:
                    raise ValueError("STRING fields forbid unit and clock_domain")
            elif field_type == "INTEGER":
                if unit not in _INTEGER_UNITS or (
                    clock is not None and (not isinstance(clock, str) or not clock)
                ):
                    raise ValueError("INTEGER field metadata is invalid")
            elif field_type == "TIMESTAMP":
                if unit is not None or not isinstance(clock, str) or not clock:
                    raise ValueError("TIMESTAMP fields require clock_domain and no unit")
            else:
                raise ValueError("event field type is invalid")
            normalized_field = {
                "name": capture_name,
                "type": field_type,
                "unit": unit,
                "clock_domain": clock,
            }
            field_by_name[capture_name] = normalized_field
            fields.append(normalized_field)
        if captured_names != set(field_by_name):
            raise ValueError("event named captures must exactly equal declared fields")
        timestamp_field = raw["timestamp_field"]
        if timestamp_field is not None:
            timestamp_field = _name(timestamp_field, f"{name}.timestamp_field")
            timestamp_spec = field_by_name.get(timestamp_field)
            if timestamp_spec is None or timestamp_spec["type"] not in {"TIMESTAMP", "INTEGER"} or timestamp_spec["clock_domain"] is None:
                raise ValueError("timestamp_field must name a clocked timestamp field")
        group_by = _names(raw["group_by"], f"{name}.group_by")
        if not set(group_by) <= set(field_by_name):
            raise ValueError("event group_by names unknown fields")
        selectors: list[dict[str, Any]] = []
        for selector_index, selector in enumerate(
            _array(raw["selectors"], f"{name}.selectors", maximum=20)
        ):
            selector_name = f"{name}.selectors[{selector_index}]"
            selector = _mapping(selector, selector_name)
            _exact(selector, {"field", "operator", "value"}, selector_name)
            field = _name(selector["field"], f"{selector_name}.field")
            if field not in field_by_name or selector["operator"] != "EQUALS":
                raise ValueError("event selector is invalid")
            selectors.append(
                {
                    "field": field,
                    "operator": "EQUALS",
                    "value": _binding(selector["value"], f"{selector_name}.value"),
                }
            )
        minimum = _non_negative_int(raw["min_matches"], f"{name}.min_matches", maximum=1_000_000)
        maximum = raw["max_matches"]
        if maximum is not None:
            maximum = _non_negative_int(maximum, f"{name}.max_matches", maximum=1_000_000)
            if maximum < minimum:
                raise ValueError("event max_matches must be >= min_matches")
        policy_ids = _names(raw["observation_policy_ids"], f"{name}.observation_policy_ids")
        if not set(policy_ids) <= set(policy_by_id):
            raise ValueError("event names an unknown observation policy")
        for policy_id in policy_ids:
            if not set(policy_by_id[policy_id]["key_fields"]) <= set(field_by_name):
                raise ValueError("observation policy key_fields must name event fields")
        extractor = {
            "id": event_id,
            "anchor": anchor,
            "members": members,
            "fields": fields,
            "timestamp_field": timestamp_field,
            "group_by": group_by,
            "selectors": selectors,
            "max_gap_lines": _non_negative_int(raw["max_gap_lines"], f"{name}.max_gap_lines", maximum=10_000),
            "min_matches": minimum,
            "max_matches": maximum,
            "observation_policy_ids": policy_ids,
        }
        extractor_by_id[event_id] = extractor
        extractors.append(extractor)
    if requires_logparse != bool(extractors):
        raise ValueError("Logparse Skills require extractors; non-Logparse Skills forbid them")

    requirement_by_name = {item["name"]: item for item in requirements}
    for extractor in extractors:
        for selector in extractor["selectors"]:
            binding = selector["value"]
            if binding["source"] != "USER_FACT":
                continue
            requirement = requirement_by_name.get(binding["name"])
            if requirement is None or requirement["kind"] != "INPUT":
                raise ValueError(
                    "event selector USER_FACT must name a declared INPUT requirement"
                )
            if requirement["requiredness"] == "OPTIONAL":
                raise ValueError(
                    "event selector USER_FACT must not name an OPTIONAL input"
                )
    input_names = {
        item["name"] for item in requirements if item["kind"] == "INPUT"
    }
    rules: list[dict[str, Any]] = []
    rule_by_id: dict[str, dict[str, Any]] = {}
    semantic_ids: set[str] = set()
    for index, raw in enumerate(_array(value["rules"], "rules", maximum=300, minimum=1)):
        name = f"verification_contract.rules[{index}]"
        raw = _mapping(raw, name)
        _exact(raw, {"id", "kind", "description", "depends_on", "remediation_requirements", "parameters"}, name)
        rule_id = _name(raw["id"], f"{name}.id")
        kind = raw["kind"]
        if rule_id in rule_by_id or kind not in _RULE_KINDS:
            raise ValueError("verification rule identity is invalid")
        dependencies = _names(raw["depends_on"], f"{name}.depends_on")
        if not set(dependencies) <= set(rule_by_id):
            raise ValueError("rule dependencies must name preceding rules")
        remediation = _names(raw["remediation_requirements"], f"{name}.remediation_requirements")
        if any(
            item not in requirement_by_name
            or requirement_by_name[item]["supplement_policy"] != "MISSING_ONLY"
            for item in remediation
        ):
            raise ValueError("rule remediation must name MISSING_ONLY requirements")
        parameters = _rule_parameters(kind, raw["parameters"], f"{name}.parameters")
        rule = {
            "id": rule_id,
            "kind": kind,
            "description": _text(raw["description"], f"{name}.description"),
            "depends_on": dependencies,
            "remediation_requirements": remediation,
            "parameters": parameters,
        }
        for reference_index, (event, field) in enumerate(_rule_event_fields(rule)):
            extractor = extractor_by_id.get(event)
            if extractor is None:
                raise ValueError(
                    f"{name} kind {kind} reference[{reference_index}] "
                    "names an unknown event"
                )
            if field is not None and field not in {
                item["name"] for item in extractor["fields"]
            }:
                raise ValueError(
                    f"{name} kind {kind} reference[{reference_index}] "
                    "names an unknown event field"
                )
        if kind == "ROLE_COVERAGE" and any(
            item["role"] not in role_labels
            or extractor_by_id[item["event"]]["anchor"] != item["role"]
            for item in parameters["coverage"]
        ):
            raise ValueError("ROLE_COVERAGE must bind declared role anchors")
        for fact_name in _rule_fact_names(rule):
            if fact_name not in input_names:
                raise ValueError("verification rule names an unknown INPUT fact")
        if kind == "NUMERIC_COMPARE":
            for expression in (parameters["left"], parameters["right"]):
                for event, field in _expression_event_fields(expression):
                    spec = next(
                        item
                        for item in extractor_by_id[event]["fields"]
                        if item["name"] == field
                    )
                    if spec["type"] == "STRING":
                        raise ValueError("numeric expressions cannot use STRING fields")
        if kind == "SEMANTIC_CAUSALITY":
            semantic_ids.add(rule_id)
        rule_by_id[rule_id] = rule
        rules.append(rule)
    if not semantic_ids:
        raise ValueError("verification contract requires SEMANTIC_CAUSALITY")

    terminal_paths: list[dict[str, Any]] = []
    path_ids: set[str] = set()
    raw_paths = _array(value["terminal_paths"], "terminal_paths", maximum=50, minimum=1)
    for index, raw in enumerate(raw_paths):
        name = f"verification_contract.terminal_paths[{index}]"
        raw = _mapping(raw, name)
        _exact(raw, {"id", "resolution_status", "condition"}, name)
        path_id = _name(raw["id"], f"{name}.id")
        if path_id in path_ids:
            raise ValueError("terminal path IDs must be unique")
        resolution = raw["resolution_status"]
        if resolution not in {"COMPLETE", "PARTIAL", "NONE"}:
            raise ValueError("terminal path resolution_status is invalid")
        condition = _mapping(raw["condition"], f"{name}.condition")
        _exact(condition, {"any_of"}, f"{name}.condition")
        branches: list[dict[str, Any]] = []
        for branch_index, branch in enumerate(
            _array(condition["any_of"], f"{name}.condition.any_of", maximum=50, minimum=1)
        ):
            branch_name = f"{name}.condition.any_of[{branch_index}]"
            branch = _mapping(branch, branch_name)
            _exact(branch, {"all_of"}, branch_name)
            terms: list[dict[str, str]] = []
            for term_index, term in enumerate(
                _array(branch["all_of"], f"{branch_name}.all_of", maximum=100)
            ):
                term_name = f"{branch_name}.all_of[{term_index}]"
                term = _mapping(term, term_name)
                _exact(term, {"rule_id", "result"}, term_name)
                rule_id = _name(term["rule_id"], f"{term_name}.rule_id")
                if rule_id not in rule_by_id or term["result"] not in {"PASS", "FAIL", "UNKNOWN"}:
                    raise ValueError("terminal path term is invalid")
                terms.append({"rule_id": rule_id, "result": term["result"]})
            if len({item["rule_id"] for item in terms}) != len(terms):
                raise ValueError("terminal path branch repeats a rule")
            if resolution in {"COMPLETE", "PARTIAL"} and not any(
                item["rule_id"] in semantic_ids and item["result"] == "PASS"
                for item in terms
            ):
                raise ValueError("candidate terminal paths require a semantic PASS term")
            branches.append({"all_of": terms})
        unconditional = any(not item["all_of"] for item in branches)
        if unconditional and (resolution != "NONE" or index != len(raw_paths) - 1):
            raise ValueError("only the final NONE path may be unconditional")
        terminal_paths.append(
            {
                "id": path_id,
                "resolution_status": resolution,
                "condition": {"any_of": branches},
            }
        )
        path_ids.add(path_id)
    if terminal_paths[-1]["resolution_status"] != "NONE" or not any(
        not branch["all_of"]
        for branch in terminal_paths[-1]["condition"]["any_of"]
    ):
        raise ValueError("terminal paths require a final unconditional NONE fallback")

    return {
        "schema_version": VERIFICATION_CONTRACT_SCHEMA_VERSION,
        "observation_policies": policies,
        "event_extractors": extractors,
        "rules": rules,
        "terminal_paths": terminal_paths,
    }


def terminal_path_matches(
    path: Mapping[str, Any],
    results: Mapping[str, str],
) -> bool:
    """Return whether one normalized terminal path matches aligned rule results."""

    return any(
        all(results.get(term["rule_id"]) == term["result"] for term in branch["all_of"])
        for branch in path["condition"]["any_of"]
    )


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "VERIFICATION_CONTRACT_SCHEMA_VERSION",
    "terminal_path_matches",
    "validate_verification_contract",
]
