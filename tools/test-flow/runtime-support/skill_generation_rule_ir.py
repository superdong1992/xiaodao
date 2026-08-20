"""Deterministic production compiler for one typed ordered-interval rule family.

The compiler is intentionally pure: it accepts an in-memory JSON-compatible
mapping and returns canonical bytes.  It performs no file, clock, random, or
network access; the isolated wrapper owns all I/O and deep validation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


COMPILER_ID = "generation-blueprint-ordered-interval"
COMPILER_VERSION = "1.0.0"
BLUEPRINT_SCHEMA_VERSION = 1
FAMILY_KIND = "ORDERED_INTERVAL"
FAMILY_VERSION = 1

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,127}")
_UNRESOLVED = re.compile(r"\{\{|\}\}|\$\{|<PLACEHOLDER>")

_SPEC_REQUIRED_KEYS = {
    "schema_version",
    "generator_version",
    "id",
    "version",
    "capability",
    "deployment_scope",
    "summary",
    "chinese_title",
    "module_name",
    "problem_scope",
    "roles",
    "requirements",
    "logparse_plan",
    "time_characteristics",
    "analysis_steps",
    "judgement_rules",
    "output_requirements",
    "assumptions",
    "requires_logparse",
}
_SPEC_OPTIONAL_KEYS = {"logparse_product"}
_RULE_KEYS = {
    "id",
    "kind",
    "description",
    "depends_on",
    "remediation_requirements",
    "parameters",
}
_PATH_KEYS = {"id", "resolution_status", "condition"}


class BlueprintError(ValueError):
    """Raised before any compilation result is returned."""


@dataclass(frozen=True)
class CompilationResult:
    compiler_id: str
    compiler_version: str
    blueprint_sha256: str
    spec_sha256: str
    spec_bytes: bytes
    literal_rule_count: int
    mechanical_rule_count: int
    semantic_rule_count: int
    expanded_family_rule_count: int
    total_rule_count: int
    expanded_family_terminal_path_count: int
    total_terminal_path_count: int


def canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BlueprintError("blueprint must contain only finite JSON values") from exc
    return encoded + b"\n"


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise BlueprintError(f"{name} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise BlueprintError(f"{name} keys must be strings")
    return dict(value)


def _exact(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        raise BlueprintError(
            f"{name} keys are invalid: missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}"
        )


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise BlueprintError(f"{name} must be an array")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or _UNRESOLVED.search(value):
        raise BlueprintError(f"{name} must be non-empty text without template residue")
    return value


def _identifier(value: Any, name: str) -> str:
    value = _text(value, name)
    if _IDENTIFIER.fullmatch(value) is None:
        raise BlueprintError(f"{name} must be a safe identifier")
    return value


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise BlueprintError(f"{name} must be an integer >= {minimum}")
    return value


def _scan_for_residue(value: Any, path: str = "blueprint") -> None:
    if isinstance(value, str):
        if _UNRESOLVED.search(value):
            raise BlueprintError(f"{path} contains unresolved template text")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _scan_for_residue(key, f"{path}.<key>")
            _scan_for_residue(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_for_residue(item, f"{path}[{index}]")


def _field(event: str, field_name: str) -> dict[str, Any]:
    return {"kind": "FIELD", "event": event, "field": field_name}


def _subtract(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "SUBTRACT", "left": left, "right": right}


def _add(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "ADD", "left": left, "right": right}


def _convert_microseconds(operand: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "CONVERT", "operand": operand, "unit": "MICROSECOND"}


def _numeric_parameters(
    left: dict[str, Any], operator: str, right: dict[str, Any]
) -> dict[str, Any]:
    return {
        "left": left,
        "operator": operator,
        "right": right,
        "quantifier": "EXISTS",
        "clock_tolerance_ms": 0,
        "joins": [],
    }


def _rule(
    rule_id: str,
    kind: str,
    description: str,
    depends_on: Sequence[str],
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "id": rule_id,
        "kind": kind,
        "description": description,
        "depends_on": list(depends_on),
        "remediation_requirements": [],
        "parameters": dict(parameters),
    }


def _single_pass_branch(*rule_ids: str) -> dict[str, Any]:
    return {
        "all_of": [
            {"rule_id": rule_id, "result": "PASS"} for rule_id in rule_ids
        ]
    }


def _validate_literal_rule(rule: Any, name: str) -> dict[str, Any]:
    rule = _mapping(rule, name)
    _exact(rule, _RULE_KEYS, name)
    _identifier(rule["id"], f"{name}.id")
    _text(rule["kind"], f"{name}.kind")
    _text(rule["description"], f"{name}.description")
    dependencies = _array(rule["depends_on"], f"{name}.depends_on")
    for index, dependency in enumerate(dependencies):
        _identifier(dependency, f"{name}.depends_on[{index}]")
    _array(rule["remediation_requirements"], f"{name}.remediation_requirements")
    _mapping(rule["parameters"], f"{name}.parameters")
    return copy.deepcopy(rule)


def _validate_literal_path(path: Any, name: str) -> dict[str, Any]:
    path = _mapping(path, name)
    _exact(path, _PATH_KEYS, name)
    _identifier(path["id"], f"{name}.id")
    if path["resolution_status"] not in {"COMPLETE", "PARTIAL", "NONE"}:
        raise BlueprintError(f"{name}.resolution_status is invalid")
    _mapping(path["condition"], f"{name}.condition")
    return copy.deepcopy(path)


def _parse_position(value: Any, index: int) -> dict[str, Any]:
    name = f"ordered_interval_family.positions[{index}]"
    value = _mapping(value, name)
    _exact(
        value,
        {"ordinal", "name", "event", "end_field", "cost_field", "queue_field", "timeout_field"},
        name,
    )
    if _integer(value["ordinal"], f"{name}.ordinal", minimum=1) != index + 1:
        raise BlueprintError("position ordinals must be consecutive and match array order")
    result = {"ordinal": index + 1}
    for key in ("name", "event", "end_field", "cost_field", "queue_field", "timeout_field"):
        result[key] = _identifier(value[key], f"{name}.{key}")
    if len({result[key] for key in ("end_field", "cost_field", "queue_field", "timeout_field")}) != 4:
        raise BlueprintError(f"{name} field mappings must be distinct")
    return result


def _parse_family(value: Any) -> dict[str, Any]:
    value = _mapping(value, "ordered_interval_family")
    _exact(
        value,
        {"kind", "version", "namespace", "positions", "shared", "texts", "names", "terminal_paths"},
        "ordered_interval_family",
    )
    if value["kind"] != FAMILY_KIND:
        raise BlueprintError(f"unknown family kind: {value['kind']!r}")
    if value["version"] != FAMILY_VERSION:
        raise BlueprintError(f"unknown family version: {value['version']!r}")
    namespace = _identifier(value["namespace"], "ordered_interval_family.namespace")
    positions = [
        _parse_position(item, index)
        for index, item in enumerate(
            _array(value["positions"], "ordered_interval_family.positions")
        )
    ]
    if len(positions) < 2:
        raise BlueprintError("ordered interval family requires at least two positions")
    if len({item["name"] for item in positions}) != len(positions):
        raise BlueprintError("position names must be unique")
    if len({item["event"] for item in positions}) != len(positions):
        raise BlueprintError("position events must be unique")

    shared = _mapping(value["shared"], "ordered_interval_family.shared")
    _exact(
        shared,
        {
            "call_event",
            "call_timeout_field",
            "call_present_rule_id",
            "detail_event",
            "detail_timeout_field",
            "detail_present_rule_id",
            "base_semantic_dependency_rule_ids",
        },
        "ordered_interval_family.shared",
    )
    for key in (
        "call_event",
        "call_timeout_field",
        "call_present_rule_id",
        "detail_event",
        "detail_timeout_field",
        "detail_present_rule_id",
    ):
        shared[key] = _identifier(shared[key], f"ordered_interval_family.shared.{key}")
    base_dependencies = _array(
        shared["base_semantic_dependency_rule_ids"],
        "ordered_interval_family.shared.base_semantic_dependency_rule_ids",
    )
    shared["base_semantic_dependency_rule_ids"] = [
        _identifier(item, f"ordered_interval_family.shared.base_semantic_dependency_rule_ids[{index}]")
        for index, item in enumerate(base_dependencies)
    ]
    if len(shared["base_semantic_dependency_rule_ids"]) != len(
        set(shared["base_semantic_dependency_rule_ids"])
    ):
        raise BlueprintError("base semantic dependencies must be unique")

    texts = _mapping(value["texts"], "ordered_interval_family.texts")
    _exact(
        texts,
        {
            "present_prefix",
            "present_suffix",
            "timeout_infix",
            "timeout_suffix",
            "core_prefix",
            "core_infix",
            "core_suffix",
            "serial_prefix",
            "serial_infix",
            "serial_suffix",
            "interval_prefix",
            "interval_infix",
            "interval_suffix",
            "unattributed_assertion",
            "overlap_assertion",
            "full_assertion",
            "gap_assertion",
        },
        "ordered_interval_family.texts",
    )
    texts = {
        key: _text(item, f"ordered_interval_family.texts.{key}")
        for key, item in texts.items()
    }

    names = _mapping(value["names"], "ordered_interval_family.names")
    _exact(names, {"unattributed_semantic_suffix"}, "ordered_interval_family.names")
    names = {
        "unattributed_semantic_suffix": _identifier(
            names["unattributed_semantic_suffix"],
            "ordered_interval_family.names.unattributed_semantic_suffix",
        )
    }

    paths = _mapping(value["terminal_paths"], "ordered_interval_family.terminal_paths")
    _exact(paths, {"complete", "unattributed", "mixed"}, "ordered_interval_family.terminal_paths")
    parsed_paths: dict[str, dict[str, str]] = {}
    expected_status = {"complete": "COMPLETE", "unattributed": "PARTIAL", "mixed": "PARTIAL"}
    for key, status in expected_status.items():
        item = _mapping(paths[key], f"ordered_interval_family.terminal_paths.{key}")
        _exact(item, {"id", "resolution_status"}, f"ordered_interval_family.terminal_paths.{key}")
        path_id = _identifier(item["id"], f"ordered_interval_family.terminal_paths.{key}.id")
        if item["resolution_status"] != status:
            raise BlueprintError(f"ordered_interval_family.terminal_paths.{key} status must be {status}")
        parsed_paths[key] = {"id": path_id, "resolution_status": status}

    return {
        "kind": FAMILY_KIND,
        "version": FAMILY_VERSION,
        "namespace": namespace,
        "positions": positions,
        "shared": shared,
        "texts": texts,
        "names": names,
        "terminal_paths": parsed_paths,
    }


def _core_rule_ids(namespace: str, target: dict[str, Any]) -> list[str]:
    prefix = f"{namespace}_{target['name']}"
    return [
        f"{prefix}_present",
        f"{prefix}_timeout_consistent",
        f"{prefix}_total_exceeds_timeout",
        f"{prefix}_execution_within_timeout",
        f"{prefix}_queue_positive",
    ]


def _serial_rule_id(namespace: str, target: str, left: str, right: str) -> str:
    return f"{namespace}_{target}_serial_{left}_{right}"


def _gap_pair_stem(namespace: str, target: str, left: str, right: str) -> str:
    return f"{namespace}_{target}_gap_{left}_{right}"


def _expand_mechanical(family: dict[str, Any]) -> list[dict[str, Any]]:
    namespace = family["namespace"]
    positions = family["positions"]
    shared = family["shared"]
    texts = family["texts"]
    rules: list[dict[str, Any]] = []

    for target_index, target in enumerate(positions):
        prefix = f"{namespace}_{target['name']}"
        present_id, timeout_id, total_id, execution_id, queue_id = _core_rule_ids(
            namespace, target
        )
        rules.append(
            _rule(
                present_id,
                "EVENT_PRESENT",
                f"{texts['present_prefix']}{target['name']}{texts['present_suffix']}",
                [],
                {"event": target["event"]},
            )
        )
        rules.append(
            _rule(
                timeout_id,
                "FIELDS_EQUAL",
                f"{target['name']}{texts['timeout_infix']}{texts['timeout_suffix']}",
                [
                    shared["call_present_rule_id"],
                    shared["detail_present_rule_id"],
                    present_id,
                ],
                {
                    "equalities": [
                        {
                            "members": [
                                {
                                    "event": shared["call_event"],
                                    "field": shared["call_timeout_field"],
                                },
                                {
                                    "event": shared["detail_event"],
                                    "field": shared["detail_timeout_field"],
                                },
                                {
                                    "event": target["event"],
                                    "field": target["timeout_field"],
                                },
                            ]
                        }
                    ],
                    "quantifier": "EXISTS",
                },
            )
        )
        metric_parameters = {
            "total_exceeds_timeout": _numeric_parameters(
                _add(
                    _field(target["event"], target["queue_field"]),
                    _field(target["event"], target["cost_field"]),
                ),
                "GT",
                _convert_microseconds(_field(target["event"], target["timeout_field"])),
            ),
            "execution_within_timeout": _numeric_parameters(
                _field(target["event"], target["cost_field"]),
                "LTE",
                _convert_microseconds(_field(target["event"], target["timeout_field"])),
            ),
            "queue_positive": _numeric_parameters(
                _field(target["event"], target["queue_field"]),
                "GT",
                {"kind": "CONST", "value": 0, "unit": "MICROSECOND"},
            ),
        }
        for rule_id, metric in (
            (total_id, "total_exceeds_timeout"),
            (execution_id, "execution_within_timeout"),
            (queue_id, "queue_positive"),
        ):
            rules.append(
                _rule(
                    rule_id,
                    "NUMERIC_COMPARE",
                    f"{texts['core_prefix']}{target['name']}{texts['core_infix']}{metric}{texts['core_suffix']}",
                    [present_id],
                    metric_parameters[metric],
                )
            )
        for right_index in range(1, target_index + 1):
            left = positions[right_index - 1]
            right = positions[right_index]
            rule_id = _serial_rule_id(
                namespace, target["name"], left["name"], right["name"]
            )
            rules.append(
                _rule(
                    rule_id,
                    "NUMERIC_COMPARE",
                    f"{texts['serial_prefix']}{left['name']}{texts['serial_infix']}{right['name']}{texts['serial_suffix']}",
                    [present_id],
                    _numeric_parameters(
                        _field(target["event"], left["end_field"]),
                        "LTE",
                        _subtract(
                            _field(target["event"], right["end_field"]),
                            _field(target["event"], right["cost_field"]),
                        ),
                    ),
                )
            )

    for target_index in range(1, len(positions)):
        target = positions[target_index]
        target_prefix = f"{namespace}_{target['name']}"
        present_id = f"{target_prefix}_present"
        target_start = _subtract(
            _field(target["event"], target["end_field"]),
            _field(target["event"], target["cost_field"]),
        )
        queue_start = _subtract(
            copy.deepcopy(target_start),
            _field(target["event"], target["queue_field"]),
        )

        def interval_rule(
            suffix: str,
            left: dict[str, Any],
            operator: str,
            right: dict[str, Any],
        ) -> dict[str, Any]:
            rule_id = f"{target_prefix}_{suffix}"
            return _rule(
                rule_id,
                "NUMERIC_COMPARE",
                f"{texts['interval_prefix']}{target['name']}{texts['interval_infix']}{rule_id}{texts['interval_suffix']}",
                [present_id],
                _numeric_parameters(left, operator, right),
            )

        for prior in positions[:target_index]:
            prior_start = _subtract(
                _field(target["event"], prior["end_field"]),
                _field(target["event"], prior["cost_field"]),
            )
            rules.extend(
                [
                    interval_rule(
                        f"overlap_{prior['name']}_starts_before_end",
                        copy.deepcopy(prior_start),
                        "LT",
                        copy.deepcopy(target_start),
                    ),
                    interval_rule(
                        f"overlap_{prior['name']}_ends_after_start",
                        _field(target["event"], prior["end_field"]),
                        "GT",
                        copy.deepcopy(queue_start),
                    ),
                    interval_rule(
                        f"cover_from_{prior['name']}_starts_before_queue",
                        copy.deepcopy(prior_start),
                        "LTE",
                        copy.deepcopy(queue_start),
                    ),
                ]
            )
        first = positions[0]
        latest = positions[target_index - 1]
        first_start = _subtract(
            _field(target["event"], first["end_field"]),
            _field(target["event"], first["cost_field"]),
        )
        rules.extend(
            [
                interval_rule(
                    "cover_ends_after_queue",
                    _field(target["event"], latest["end_field"]),
                    "GTE",
                    copy.deepcopy(target_start),
                ),
                interval_rule(
                    "latest_prior_before_queue",
                    _field(target["event"], latest["end_field"]),
                    "LTE",
                    copy.deepcopy(queue_start),
                ),
                interval_rule(
                    "gap_prefix_open",
                    first_start,
                    "GT",
                    copy.deepcopy(queue_start),
                ),
                interval_rule(
                    "gap_suffix_open",
                    _field(target["event"], latest["end_field"]),
                    "LT",
                    copy.deepcopy(target_start),
                ),
            ]
        )
        for pair_index in range(target_index - 1):
            left = positions[pair_index]
            right = positions[pair_index + 1]
            stem = f"{left['name']}_{right['name']}"
            right_start = _subtract(
                _field(target["event"], right["end_field"]),
                _field(target["event"], right["cost_field"]),
            )
            rules.extend(
                [
                    interval_rule(
                        f"cover_{stem}_no_gap",
                        copy.deepcopy(right_start),
                        "LTE",
                        _field(target["event"], left["end_field"]),
                    ),
                    interval_rule(
                        f"gap_{stem}_open",
                        _field(target["event"], left["end_field"]),
                        "LT",
                        copy.deepcopy(right_start),
                    ),
                    interval_rule(
                        f"gap_{stem}_before_end",
                        _field(target["event"], left["end_field"]),
                        "LT",
                        copy.deepcopy(target_start),
                    ),
                    interval_rule(
                        f"gap_{stem}_after_start",
                        copy.deepcopy(right_start),
                        "GT",
                        copy.deepcopy(queue_start),
                    ),
                ]
            )
    return rules


def _expand_semantic(family: dict[str, Any]) -> list[dict[str, Any]]:
    namespace = family["namespace"]
    positions = family["positions"]
    shared = family["shared"]
    texts = family["texts"]
    rules: list[dict[str, Any]] = []
    for target_index, target in enumerate(positions):
        prefix = f"{namespace}_{target['name']}"
        present_id, timeout_id, total_id, execution_id, queue_id = _core_rule_ids(
            namespace, target
        )
        base = [
            *shared["base_semantic_dependency_rule_ids"],
            present_id,
            timeout_id,
            total_id,
            execution_id,
            queue_id,
            *[
                _serial_rule_id(
                    namespace,
                    target["name"],
                    positions[index - 1]["name"],
                    positions[index]["name"],
                )
                for index in range(1, target_index + 1)
            ],
        ]
        evidence_events = [
            shared["call_event"],
            shared["detail_event"],
            target["event"],
        ]

        def semantic_rule(
            suffix: str, assertion: str, dependencies: Sequence[str]
        ) -> dict[str, Any]:
            return _rule(
                f"{prefix}_{suffix}",
                "SEMANTIC_CAUSALITY",
                assertion,
                [*base, *dependencies],
                {"assertion": assertion, "evidence_events": evidence_events},
            )

        unattributed_dependencies = (
            [] if target_index == 0 else [f"{prefix}_latest_prior_before_queue"]
        )
        rules.append(
            semantic_rule(
                family["names"]["unattributed_semantic_suffix"],
                texts["unattributed_assertion"],
                unattributed_dependencies,
            )
        )
        if target_index == 0:
            continue
        for prior_index, prior in enumerate(positions[:target_index]):
            rules.append(
                semantic_rule(
                    f"overlap_{prior['name']}_confirmed",
                    texts["overlap_assertion"],
                    [
                        f"{prefix}_overlap_{prior['name']}_starts_before_end",
                        f"{prefix}_overlap_{prior['name']}_ends_after_start",
                    ],
                )
            )
        for prior_index, prior in enumerate(positions[:target_index]):
            no_gap_ids = [
                f"{prefix}_cover_{positions[index]['name']}_{positions[index + 1]['name']}_no_gap"
                for index in range(prior_index, target_index - 1)
            ]
            rules.append(
                semantic_rule(
                    f"full_from_{prior['name']}",
                    texts["full_assertion"],
                    [
                        f"{prefix}_cover_from_{prior['name']}_starts_before_queue",
                        f"{prefix}_cover_ends_after_queue",
                        *no_gap_ids,
                    ],
                )
            )
        rules.append(
            semantic_rule(
                "gap_prefix_confirmed",
                texts["gap_assertion"],
                [f"{prefix}_gap_prefix_open"],
            )
        )
        for index in range(target_index - 1):
            left = positions[index]["name"]
            right = positions[index + 1]["name"]
            stem = _gap_pair_stem(namespace, target["name"], left, right)
            rules.append(
                semantic_rule(
                    f"gap_{left}_{right}_confirmed",
                    texts["gap_assertion"],
                    [f"{stem}_open", f"{stem}_before_end", f"{stem}_after_start"],
                )
            )
        rules.append(
            semantic_rule(
                "gap_suffix_confirmed",
                texts["gap_assertion"],
                [f"{prefix}_gap_suffix_open"],
            )
        )
    return rules


def _expand_terminal_paths(family: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    namespace = family["namespace"]
    positions = family["positions"]
    metadata = family["terminal_paths"]
    complete_branches = [
        _single_pass_branch(f"{namespace}_{target['name']}_full_from_{prior['name']}")
        for target_index, target in enumerate(positions[1:], start=1)
        for prior in positions[:target_index]
    ]
    unattributed_branches = [
        _single_pass_branch(
            f"{namespace}_{target['name']}_{family['names']['unattributed_semantic_suffix']}"
        )
        for target in positions
    ]
    mixed_branches: list[dict[str, Any]] = []
    for target_index, target in enumerate(positions[1:], start=1):
        gap_rule_ids = [
            f"{namespace}_{target['name']}_gap_prefix_confirmed",
            *[
                f"{namespace}_{target['name']}_gap_{positions[index]['name']}_{positions[index + 1]['name']}_confirmed"
                for index in range(target_index - 1)
            ],
            f"{namespace}_{target['name']}_gap_suffix_confirmed",
        ]
        for prior in positions[:target_index]:
            overlap = f"{namespace}_{target['name']}_overlap_{prior['name']}_confirmed"
            mixed_branches.extend(
                _single_pass_branch(overlap, gap_rule_id)
                for gap_rule_id in gap_rule_ids
            )

    def path(key: str, branches: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "id": metadata[key]["id"],
            "resolution_status": metadata[key]["resolution_status"],
            "condition": {"any_of": branches},
        }

    return (
        path("complete", complete_branches),
        path("unattributed", unattributed_branches),
        path("mixed", mixed_branches),
    )


def _validate_rule_graph(rules: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        rule_id = _identifier(rule.get("id"), f"compiled.rules[{index}].id")
        if rule_id in seen:
            raise BlueprintError(f"duplicate rule ID: {rule_id}")
        dependencies = rule.get("depends_on")
        if not isinstance(dependencies, list):
            raise BlueprintError(f"compiled rule {rule_id} dependencies are invalid")
        missing = [item for item in dependencies if item not in seen]
        if missing:
            raise BlueprintError(
                f"compiled rule {rule_id} has missing or non-topological dependencies: {missing!r}"
            )
        seen.add(rule_id)


def _validate_terminal_refs(paths: list[dict[str, Any]], rule_ids: set[str]) -> None:
    path_ids: set[str] = set()
    for path_index, path in enumerate(paths):
        path_id = _identifier(path.get("id"), f"compiled.terminal_paths[{path_index}].id")
        if path_id in path_ids:
            raise BlueprintError(f"duplicate terminal path ID: {path_id}")
        path_ids.add(path_id)
        condition = _mapping(path.get("condition"), f"compiled.terminal_paths[{path_index}].condition")
        branches = _array(condition.get("any_of"), f"compiled.terminal_paths[{path_index}].condition.any_of")
        for branch_index, branch in enumerate(branches):
            branch = _mapping(branch, f"compiled.terminal_paths[{path_index}].condition.any_of[{branch_index}]")
            terms = _array(branch.get("all_of"), f"compiled.terminal_paths[{path_index}].condition.any_of[{branch_index}].all_of")
            for term in terms:
                term = _mapping(term, "compiled terminal condition term")
                if set(term) != {"rule_id", "result"} or term["result"] not in {"PASS", "FAIL", "UNKNOWN"}:
                    raise BlueprintError("compiled terminal condition term is invalid")
                if term["rule_id"] not in rule_ids:
                    raise BlueprintError(
                        f"terminal path {path_id} references unknown rule {term['rule_id']!r}"
                    )


def compile_blueprint(value: Mapping[str, Any]) -> CompilationResult:
    """Compile a validated blueprint atomically into canonical GenerationSpec bytes."""

    root = _mapping(value, "blueprint")
    _exact(root, {"schema_version", "compiler", "spec", "verification"}, "blueprint")
    if root["schema_version"] != BLUEPRINT_SCHEMA_VERSION:
        raise BlueprintError("blueprint schema_version is unsupported")
    compiler = _mapping(root["compiler"], "blueprint.compiler")
    _exact(compiler, {"id", "version"}, "blueprint.compiler")
    if compiler != {"id": COMPILER_ID, "version": COMPILER_VERSION}:
        raise BlueprintError("blueprint compiler identity does not match this compiler")
    _scan_for_residue(root)
    blueprint_bytes = canonical_json_bytes(root)

    spec = _mapping(root["spec"], "blueprint.spec")
    spec_keys = set(spec)
    if not _SPEC_REQUIRED_KEYS.issubset(spec_keys) or not spec_keys.issubset(
        _SPEC_REQUIRED_KEYS | _SPEC_OPTIONAL_KEYS
    ):
        raise BlueprintError("blueprint.spec keys are invalid")
    verification = _mapping(root["verification"], "blueprint.verification")
    _exact(
        verification,
        {
            "schema_version",
            "observation_policies",
            "event_extractors",
            "literal_rule_segments",
            "literal_terminal_segments",
            "ordered_interval_family",
            "expected_counts",
        },
        "blueprint.verification",
    )
    if verification["schema_version"] != 2:
        raise BlueprintError("verification schema_version must be 2")

    rule_segments = _mapping(
        verification["literal_rule_segments"], "blueprint.verification.literal_rule_segments"
    )
    _exact(rule_segments, {"prefix", "middle", "suffix"}, "blueprint.verification.literal_rule_segments")
    parsed_rule_segments = {
        key: [
            _validate_literal_rule(item, f"literal_rule_segments.{key}[{index}]")
            for index, item in enumerate(_array(rule_segments[key], f"literal_rule_segments.{key}"))
        ]
        for key in ("prefix", "middle", "suffix")
    }
    path_segments = _mapping(
        verification["literal_terminal_segments"],
        "blueprint.verification.literal_terminal_segments",
    )
    _exact(path_segments, {"after_complete", "after_families"}, "blueprint.verification.literal_terminal_segments")
    parsed_path_segments = {
        key: [
            _validate_literal_path(item, f"literal_terminal_segments.{key}[{index}]")
            for index, item in enumerate(_array(path_segments[key], f"literal_terminal_segments.{key}"))
        ]
        for key in ("after_complete", "after_families")
    }

    family = _parse_family(verification["ordered_interval_family"])
    mechanical = _expand_mechanical(family)
    semantic = _expand_semantic(family)
    family_paths = _expand_terminal_paths(family)

    counts = _mapping(verification["expected_counts"], "blueprint.verification.expected_counts")
    expected_count_keys = {
        "positions",
        "policies",
        "extractors",
        "prefix_rules",
        "mechanical_rules",
        "middle_rules",
        "semantic_rules",
        "suffix_rules",
        "total_rules",
        "family_terminal_paths",
        "literal_terminal_paths",
        "total_terminal_paths",
    }
    _exact(counts, expected_count_keys, "blueprint.verification.expected_counts")
    for key in expected_count_keys:
        counts[key] = _integer(counts[key], f"expected_counts.{key}")
    actual_counts = {
        "positions": len(family["positions"]),
        "policies": len(_array(verification["observation_policies"], "observation_policies")),
        "extractors": len(_array(verification["event_extractors"], "event_extractors")),
        "prefix_rules": len(parsed_rule_segments["prefix"]),
        "mechanical_rules": len(mechanical),
        "middle_rules": len(parsed_rule_segments["middle"]),
        "semantic_rules": len(semantic),
        "suffix_rules": len(parsed_rule_segments["suffix"]),
        "total_rules": sum(
            (
                len(parsed_rule_segments["prefix"]),
                len(mechanical),
                len(parsed_rule_segments["middle"]),
                len(semantic),
                len(parsed_rule_segments["suffix"]),
            )
        ),
        "family_terminal_paths": len(family_paths),
        "literal_terminal_paths": sum(map(len, parsed_path_segments.values())),
        "total_terminal_paths": len(family_paths) + sum(map(len, parsed_path_segments.values())),
    }
    if counts != actual_counts:
        raise BlueprintError(f"compiled counts differ from expected counts: {actual_counts!r}")

    rules = [
        *parsed_rule_segments["prefix"],
        *mechanical,
        *parsed_rule_segments["middle"],
        *semantic,
        *parsed_rule_segments["suffix"],
    ]
    paths = [
        family_paths[0],
        *parsed_path_segments["after_complete"],
        family_paths[1],
        family_paths[2],
        *parsed_path_segments["after_families"],
    ]
    _validate_rule_graph(rules)
    _validate_terminal_refs(paths, {rule["id"] for rule in rules})

    result = copy.deepcopy(spec)
    result["verification_contract"] = {
        "schema_version": 2,
        "observation_policies": copy.deepcopy(verification["observation_policies"]),
        "event_extractors": copy.deepcopy(verification["event_extractors"]),
        "rules": rules,
        "terminal_paths": paths,
    }
    _scan_for_residue(result, "compiled_spec")
    spec_bytes = canonical_json_bytes(result)
    return CompilationResult(
        compiler_id=COMPILER_ID,
        compiler_version=COMPILER_VERSION,
        blueprint_sha256=hashlib.sha256(blueprint_bytes).hexdigest(),
        spec_sha256=hashlib.sha256(spec_bytes).hexdigest(),
        spec_bytes=spec_bytes,
        literal_rule_count=(
            len(parsed_rule_segments["prefix"])
            + len(parsed_rule_segments["middle"])
            + len(parsed_rule_segments["suffix"])
        ),
        mechanical_rule_count=len(mechanical),
        semantic_rule_count=len(semantic),
        expanded_family_rule_count=len(mechanical) + len(semantic),
        total_rule_count=len(rules),
        expanded_family_terminal_path_count=len(family_paths),
        total_terminal_path_count=len(paths),
    )


__all__ = [
    "BLUEPRINT_SCHEMA_VERSION",
    "BlueprintError",
    "COMPILER_ID",
    "COMPILER_VERSION",
    "CompilationResult",
    "compile_blueprint",
]
