from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from problem_locator.contracts import EvidenceBinding, ServerRuleStatus
from problem_locator.runtime.server_verifier import (
    _ResolvedLine,
    _evaluate_rule,
    _extract_events,
)
from problem_locator.runtime.verification_contract import terminal_path_matches


ROOT = Path(__file__).resolve().parents[4]
CASE_ROOT = ROOT / "tests" / "cases" / "release"


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_bytes())


def _release_cases() -> list[Path]:
    return sorted(path.parent for path in CASE_ROOT.glob("*/case.json"))


def _rule_result(status: ServerRuleStatus, *, semantic_result: str | None) -> str:
    if status is ServerRuleStatus.VERIFIED_PASS:
        return "PASS"
    if status is ServerRuleStatus.VERIFIED_FAIL:
        return "FAIL"
    if status is ServerRuleStatus.SEMANTIC_ONLY and semantic_result is not None:
        return semantic_result
    return "UNKNOWN"


def _scenario_lines(
    case_root: Path,
    scenario_ref: dict[str, object],
    driver: dict[str, object],
) -> list[_ResolvedLine]:
    result: list[_ResolvedLine] = []
    scenario_root = (case_root / str(scenario_ref["driver"])).parent
    attachments = list(driver["attachment_files"])
    anchors = list(driver["attachment_anchor_names"])
    assert len(attachments) == len(anchors)
    for source_index, (relative, anchor) in enumerate(
        zip(attachments, anchors, strict=True),
        start=1,
    ):
        source = scenario_root / str(relative)
        binding = EvidenceBinding(
            existing_evidence_id=(
                f"00000000-0000-0000-0000-{source_index:012d}"
            ),
            evidence_proposal_key=None,
        )
        for line_number, raw_line in enumerate(
            source.read_bytes().splitlines(keepends=True),
            start=1,
        ):
            result.append(
                _ResolvedLine(
                    binding=binding,
                    anchor=str(anchor),
                    source_key=source.as_posix(),
                    relative_path=str(relative),
                    line_number=line_number,
                    raw_line=raw_line,
                    text=raw_line.rstrip(b"\r\n").decode("utf-8"),
                )
            )
    return result


@pytest.mark.parametrize("case_root", _release_cases(), ids=lambda path: path.name)
def test_release_case_scenarios_select_the_reviewed_terminal_paths(
    case_root: Path,
) -> None:
    descriptor = _json(case_root / "case.json")
    spec = _json(case_root / str(descriptor["generation_spec"]))
    semantic_oracle = _json(case_root / str(descriptor["semantic_oracle"]))
    contract = spec["verification_contract"]
    extractors = contract["event_extractors"]
    extractor_by_id = {item["id"]: item for item in extractors}
    multiline_matched = False

    for scenario_ref in descriptor["scenarios"]:
        driver = _json(case_root / scenario_ref["driver"])
        oracle = _json(case_root / scenario_ref["oracle"])
        facts = {
            name: [
                SimpleNamespace(
                    item_id=f"00000000-0000-0000-0001-{index:012d}",
                    statement=value,
                )
            ]
            for index, (name, value) in enumerate(
                zip(
                    driver["initial_user_fact_names"]
                    + driver["supplement_input_names"],
                    driver["initial_user_fact_values"]
                    + driver["supplement_input_values"],
                    strict=True,
                )
            )
        }
        events, scan_complete = _extract_events(
            extractors,
            _scenario_lines(case_root, scenario_ref, driver),
            set(),
            facts,
        )
        evaluated = {}
        results: dict[str, str] = {}
        required = oracle["required_rule_results"]
        for rule in contract["rules"]:
            evaluation = _evaluate_rule(
                rule,
                events=events,
                event_scan_complete=scan_complete,
                extractor_by_id=extractor_by_id,
                facts=facts,
                prior=evaluated,
            )
            evaluated[rule["id"]] = evaluation
            semantic_result = (
                required.get(rule["id"])
                if evaluation.status is ServerRuleStatus.SEMANTIC_ONLY
                and not evaluation.issues
                else None
            )
            results[rule["id"]] = _rule_result(
                evaluation.status,
                semantic_result=semantic_result,
            )

        assert {key: results[key] for key in required} == required
        selected = next(
            path
            for path in contract["terminal_paths"]
            if terminal_path_matches(path, results)
        )
        assert selected["id"] == oracle["terminal_path_id"]
        assert selected["resolution_status"] == oracle["resolution_status"]
        if oracle["resolution_status"] == "PARTIAL":
            assert any(
                status != "SATISFIED" for status in oracle["criterion_statuses"]
            )
        if oracle["resolution_status"] == "NONE":
            assert oracle["causal_factor_ids"] == []
            assert oracle["candidate_factor_ids"] == []
            assert set(oracle["criterion_statuses"]) == {"UNKNOWN"}

        for event_id, matches in events.items():
            if extractor_by_id[event_id]["observation_policy_ids"] and matches:
                audits = [
                    observation
                    for evaluation in evaluated.values()
                    for observation in evaluation.event_observations
                    if observation.event_id == event_id
                ]
                assert audits
                assert all(item.count_is_lower_bound for item in audits)

        expected_skill = semantic_oracle["expected_skill"]
        if expected_skill["requires_multiline_event"]:
            multiline_ids = {
                item["id"] for item in extractors if len(item["members"]) > 1
            }
            assert multiline_ids
            multiline_matched = multiline_matched or any(
                len(match.lines) > 1
                for event_id in multiline_ids
                for match in events[event_id]
            )
        if expected_skill["requires_numeric_compare"]:
            assert any(item["kind"] == "NUMERIC_COMPARE" for item in contract["rules"])
        assert max(
            item["parameters"].get("clock_tolerance_ms", 0)
            for item in contract["rules"]
        ) == expected_skill["requires_cross_clock_tolerance_ms"]

    if semantic_oracle["expected_skill"]["requires_multiline_event"]:
        assert multiline_matched


@pytest.mark.parametrize("case_root", _release_cases(), ids=lambda path: path.name)
def test_release_case_ordered_selector_families_cover_each_member_position(
    case_root: Path,
) -> None:
    descriptor = _json(case_root / "case.json")
    spec = _json(case_root / str(descriptor["generation_spec"]))
    families: dict[tuple[object, ...], list[tuple[dict[str, object], int]]] = {}

    def pattern_shape(pattern: str) -> str:
        return re.sub(r"\(\?P<[^>]+>([^()]*)\)", r"\1", pattern)

    for extractor in spec["verification_contract"]["event_extractors"]:
        selectors = extractor["selectors"]
        members = extractor["members"]
        if len(members) < 2 or not selectors:
            continue
        selector_fields = [selector["field"] for selector in selectors]
        selected_positions = [
            index
            for index, member in enumerate(members)
            if all(
                f"(?P<{field}>" in member["line_pattern"]
                for field in selector_fields
            )
        ]
        assert len(selected_positions) == 1
        key = (
            extractor["anchor"],
            tuple(pattern_shape(member["line_pattern"]) for member in members),
            tuple(
                (
                    selector["operator"],
                    selector["value"]["source"],
                    selector["value"].get("name"),
                )
                for selector in selectors
            ),
            tuple(extractor["observation_policy_ids"]),
        )
        families.setdefault(key, []).append((extractor, selected_positions[0]))

    for family in families.values():
        if len(family) == 1:
            continue
        member_count = len(family[0][0]["members"])
        assert len(family) == member_count
        assert sorted(position for _, position in family) == list(range(member_count))


@pytest.mark.parametrize("case_root", _release_cases(), ids=lambda path: path.name)
def test_release_case_policy_projection_matches_the_approved_contract(
    case_root: Path,
) -> None:
    descriptor = _json(case_root / "case.json")
    spec = _json(case_root / str(descriptor["generation_spec"]))
    semantic_oracle = _json(case_root / str(descriptor["semantic_oracle"]))
    projection = semantic_oracle["generated_spec_oracle"]
    assert projection["projection_version"] == 4

    def policies(values: list[dict[str, object]]) -> list[dict[str, object]]:
        return sorted(
            ({**item, "key_fields": sorted(item["key_fields"])} for item in values),
            key=lambda item: str(item["id"]),
        )

    def bindings(values: list[dict[str, object]]) -> list[dict[str, object]]:
        return sorted(
            (
                {
                    "event_id": item["event_id"],
                    "observation_policy_ids": sorted(
                        item["observation_policy_ids"]
                    ),
                }
                for item in values
            ),
            key=lambda item: str(item["event_id"]),
        )

    contract = spec["verification_contract"]
    actual_bindings = [
        {
            "event_id": extractor["id"],
            "observation_policy_ids": extractor["observation_policy_ids"],
        }
        for extractor in contract["event_extractors"]
    ]
    assert policies(contract["observation_policies"]) == policies(
        projection["observation_policies"]
    )
    assert bindings(actual_bindings) == bindings(
        projection["event_policy_bindings"]
    )

    allowed_target_fields = {
        "analysis_steps",
        "assumptions",
        "chinese_title",
        "judgement_rules",
        "output_requirements",
        "problem_scope",
        "summary",
        "time_characteristics",
    }
    semantics = projection["required_product_semantics"]
    assert semantics
    assert len({item["id"] for item in semantics}) == len(semantics)
    for semantic in semantics:
        target_fields = semantic["target_fields"]
        assert target_fields
        assert len(target_fields) == len(set(target_fields))
        assert set(target_fields) <= allowed_target_fields
        segments: list[str] = []
        for field in target_fields:
            value = spec[field]
            if isinstance(value, str):
                segments.append(value)
            else:
                assert isinstance(value, list)
                assert all(isinstance(item, str) for item in value)
                segments.extend(value)
        approved_spec_text = "\n".join(segments)
        for alternatives in semantic["all_of_any_patterns"]:
            assert any(
                re.search(pattern, approved_spec_text, re.IGNORECASE)
                for pattern in alternatives
            ), (semantic["id"], alternatives)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("不针对用户问题时间启动等待或监控。", True),
        ("不得根据用户问题时间启动监控。", True),
        ("不启动监控。", True),
        ("针对用户问题时间启动等待或监控。", False),
        ("根据用户问题时间启动监控。", False),
        ("不影响诊断，根据用户问题时间启动监控。", False),
        ("不得不根据用户问题时间启动监控。", False),
    ],
)
def test_release_case_monitoring_semantic_requires_an_explicit_prohibition(
    text: str,
    expected: bool,
) -> None:
    semantics = [
        semantic
        for case_root in _release_cases()
        for semantic in _json(case_root / "oracle.json")["generated_spec_oracle"][
            "required_product_semantics"
        ]
        if semantic["id"] == "fixed_snapshot_boundary"
    ]
    assert len(semantics) == 1
    monitoring_patterns = semantics[0]["all_of_any_patterns"][-1]
    assert any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in monitoring_patterns
    ) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("不等待未来日志。", True),
        ("不针对用户问题时间启动等待或监控。", True),
        ("不得根据用户问题时间启动等待。", True),
        ("禁止启动等待。", True),
        ("针对用户问题时间启动等待或监控。", False),
        ("根据用户问题时间启动等待。", False),
        ("不影响诊断，根据用户问题时间启动等待。", False),
        ("不得不根据用户问题时间启动等待。", False),
    ],
)
def test_release_case_waiting_semantic_requires_an_explicit_prohibition(
    text: str,
    expected: bool,
) -> None:
    semantics = [
        semantic
        for case_root in _release_cases()
        for semantic in _json(case_root / "oracle.json")["generated_spec_oracle"][
            "required_product_semantics"
        ]
        if semantic["id"] == "fixed_snapshot_boundary"
    ]
    assert len(semantics) == 1
    waiting_patterns = semantics[0]["all_of_any_patterns"][2]
    assert any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in waiting_patterns
    ) is expected
