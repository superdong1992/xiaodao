from __future__ import annotations

import json
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
    driver: dict[str, object],
) -> list[_ResolvedLine]:
    result: list[_ResolvedLine] = []
    scenario_root = case_root / "scenarios" / str(driver["scenario_id"])
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
            _scenario_lines(case_root, driver),
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
            assert any(
                len(match.lines) > 1
                for event_id in multiline_ids
                for match in events[event_id]
            ) or oracle["resolution_status"] == "PARTIAL"
        if expected_skill["requires_numeric_compare"]:
            assert any(item["kind"] == "NUMERIC_COMPARE" for item in contract["rules"])
        assert max(
            item["parameters"].get("clock_tolerance_ms", 0)
            for item in contract["rules"]
        ) == expected_skill["requires_cross_clock_tolerance_ms"]
