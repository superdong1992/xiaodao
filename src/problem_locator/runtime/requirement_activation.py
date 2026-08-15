"""Deterministically activate v6 diagnosis Skill roles and requirements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


ROLE_FIELD_SUFFIXES = ("slot", "process_name", "pid")


@dataclass(frozen=True, slots=True)
class RequirementResolution:
    active_roles: tuple[str, ...]
    inactive_roles: tuple[str, ...]
    active_requirement_names: tuple[str, ...]
    inactive_requirement_names: tuple[str, ...]
    requested_requirements: tuple[dict[str, Any], ...]


def active_role_labels(
    roles: Sequence[Mapping[str, Any]],
    facts: Mapping[str, str],
) -> tuple[str, ...]:
    result: list[str] = []
    for role in roles:
        label = role["label"]
        presence = role["presence"]
        if presence == "REQUIRED" or any(
            f"{label}_{suffix}" in facts for suffix in ROLE_FIELD_SUFFIXES
        ):
            result.append(label)
    return tuple(result)


def activation_condition_matches(
    condition: Mapping[str, Any],
    *,
    facts: Mapping[str, str],
    rule_results: Mapping[str, str],
) -> bool:
    def term_matches(term: Mapping[str, Any]) -> bool:
        source = facts if term["source"] == "USER_FACT" else rule_results
        return source.get(term["name"]) == term["value"]

    return any(
        all(term_matches(term) for term in branch["all_of"])
        for branch in condition["any_of"]
    )


def _requirement_active(
    requirement: Mapping[str, Any],
    *,
    active_roles: set[str],
    facts: Mapping[str, str],
    rule_results: Mapping[str, str],
) -> bool:
    role = requirement["role"]
    if role is not None and role not in active_roles:
        return False
    requiredness = requirement["requiredness"]
    if requiredness == "REQUIRED":
        return True
    if requiredness == "OPTIONAL":
        return False
    if requiredness != "CONDITIONAL" or requirement["activation_condition"] is None:
        raise ValueError("manifest requirement requiredness is invalid")
    return activation_condition_matches(
        requirement["activation_condition"],
        facts=facts,
        rule_results=rule_results,
    )


def resolve_requirements(
    *,
    roles: Sequence[Mapping[str, Any]],
    requirements: Sequence[dict[str, Any]],
    facts: Mapping[str, str],
    attachment_ready: bool,
    after_logparse: bool,
    rule_results: Mapping[str, str] | None = None,
) -> RequirementResolution:
    """Return the exact active and currently requestable requirement set."""

    results = {} if rule_results is None else rule_results
    active_roles = active_role_labels(roles, facts)
    active_role_set = set(active_roles)
    inactive_roles = tuple(
        role["label"] for role in roles if role["label"] not in active_role_set
    )
    active: list[dict[str, Any]] = []
    inactive: list[str] = []
    for requirement in requirements:
        if _requirement_active(
            requirement,
            active_roles=active_role_set,
            facts=facts,
            rule_results=results,
        ):
            active.append(requirement)
        else:
            inactive.append(requirement["name"])

    missing_initial_inputs = [
        item
        for item in active
        if item["stage"] == "INITIAL"
        and item["kind"] == "INPUT"
        and item["name"] not in facts
    ]
    missing_initial_attachments = [
        item
        for item in active
        if item["stage"] == "INITIAL"
        and item["kind"] == "ATTACHMENT"
        and not attachment_ready
    ]
    missing_after_inputs = [
        item
        for item in active
        if item["stage"] == "AFTER_LOGPARSE"
        and item["kind"] == "INPUT"
        and item["name"] not in facts
    ]
    if missing_initial_inputs:
        requested = missing_initial_inputs
    elif missing_initial_attachments:
        requested = missing_initial_attachments
    elif after_logparse:
        requested = missing_after_inputs
    else:
        requested = []
    return RequirementResolution(
        active_roles=active_roles,
        inactive_roles=inactive_roles,
        active_requirement_names=tuple(item["name"] for item in active),
        inactive_requirement_names=tuple(inactive),
        requested_requirements=tuple(requested),
    )


def _collect_user_fact_names(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        if value.get("source") == "USER_FACT" and isinstance(value.get("name"), str):
            result.add(value["name"])
        if isinstance(value.get("fact_name"), str):
            result.add(value["fact_name"])
        if value.get("kind") == "FACT" and isinstance(value.get("name"), str):
            result.add(value["name"])
        for item in value.values():
            result.update(_collect_user_fact_names(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_collect_user_fact_names(item))
    return result


def _collect_event_names(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"event", "before_event", "after_event"} and isinstance(item, str):
                result.add(item)
            elif key == "evidence_events" and isinstance(item, list):
                result.update(entry for entry in item if isinstance(entry, str))
            else:
                result.update(_collect_event_names(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_collect_event_names(item))
    return result


def validate_requirement_activation_contract(
    requirements: Sequence[Mapping[str, Any]],
    verification_contract: Mapping[str, Any],
) -> None:
    """Reject cyclic, semantic or target-dependent activation conditions."""

    requirement_by_name = {item["name"]: item for item in requirements}
    selector_facts = {
        extractor["id"]: _collect_user_fact_names(extractor.get("selectors", []))
        for extractor in verification_contract["event_extractors"]
    }
    rule_by_name = {item["id"]: item for item in verification_contract["rules"]}
    expanded_rule_facts: dict[str, set[str]] = {}
    for rule in verification_contract["rules"]:
        names = _collect_user_fact_names(rule["parameters"])
        for event in _collect_event_names(rule["parameters"]):
            names.update(selector_facts.get(event, set()))
        for dependency in rule["depends_on"]:
            names.update(expanded_rule_facts[dependency])
        expanded_rule_facts[rule["id"]] = names
    for requirement in requirements:
        condition = requirement["activation_condition"]
        if condition is None:
            continue
        for branch in condition["any_of"]:
            for term in branch["all_of"]:
                if term["source"] == "USER_FACT":
                    source = requirement_by_name.get(term["name"])
                    if (
                        source is None
                        or source["kind"] != "INPUT"
                        or source["stage"] != "INITIAL"
                        or source["requiredness"] == "CONDITIONAL"
                        or source["name"] == requirement["name"]
                    ):
                        raise ValueError(
                            "activation USER_FACT must name another non-conditional INITIAL input"
                        )
                else:
                    rule = rule_by_name.get(term["name"])
                    if (
                        requirement["stage"] != "AFTER_LOGPARSE"
                        or rule is None
                        or rule["kind"] == "SEMANTIC_CAUSALITY"
                        or requirement["name"] in expanded_rule_facts[term["name"]]
                    ):
                        raise ValueError(
                            "activation RULE_RESULT must be an independent mechanical AFTER_LOGPARSE rule"
                        )


__all__ = [
    "ROLE_FIELD_SUFFIXES",
    "RequirementResolution",
    "activation_condition_matches",
    "active_role_labels",
    "resolve_requirements",
    "validate_requirement_activation_contract",
]
