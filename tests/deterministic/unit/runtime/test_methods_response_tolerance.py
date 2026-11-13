from __future__ import annotations

import copy
import json

import pytest

from problem_locator.runtime.methods_advisory import accept_method_diagnosis_advisory, parse_advisory_review
from problem_locator.runtime.methods_evaluation_v2 import parse_method_evaluation_response_v2
from problem_locator.runtime.methods_grounding import (
    MethodDiagnosisDraftV1, MethodReviewV1, scan_method_markers, verify_method_diagnosis, verify_method_review,
)
from problem_locator.runtime.methods_selection import select_method_diagnosis
from tests.deterministic.unit.runtime.test_methods_evaluation_v2 import _plan, _response
from tests.deterministic.unit.runtime.test_methods_selection import _input
from tests.deterministic.unit.runtime.test_methods_output_pipeline import _target_log


@pytest.mark.parametrize("parser", [MethodDiagnosisDraftV1.from_mapping, lambda value: accept_method_diagnosis_advisory(**{**_input(), "draft": value}).draft])
@pytest.mark.parametrize("variation", ["extra_fields", "optional_arrays", "duplicate_labels", "status_metadata"])
def test_method_diagnosis_format_differences_preserve_findings(parser, variation, caplog):
    value = _input()["draft"]
    if variation == "extra_fields":
        value["explanation"] = "额外说明"
        value["evidence"][0]["confidence"] = 0.99
        value["evidence"][0]["sources"][0]["description"] = "引用说明"
    elif variation == "optional_arrays":
        for field in ("candidate_methods", "limitations", "safety_notes"):
            value.pop(field)
    elif variation == "duplicate_labels":
        value["confirmed_methods"] *= 2
        value["evidence"][0]["identity_tokens"] *= 2
    else:
        value["status"] = "INSUFFICIENT"
    parsed = parser(value)
    assert len(parsed.evidence) == 1
    assert parsed.evidence[0].summary == value["evidence"][0]["summary"]
    assert not caplog.records


@pytest.mark.parametrize("parser", [MethodReviewV1.from_mapping, parse_advisory_review])
def test_method_review_ignores_extra_fields_and_optional_metadata(parser, caplog):
    parsed = parser({"verdict": "PASS", "explanation": "说明", "findings": [{
        "method_id": "rpc-call-timeout", "verdict": "PASS", "confidence": 0.99,
    }]})
    assert parsed.verdict == "PASS"
    assert parsed.findings[0].identity_tokens == ()
    assert not caplog.records


@pytest.mark.parametrize("accept", [accept_method_diagnosis_advisory, select_method_diagnosis])
def test_unusable_findings_become_an_insufficient_result(accept, caplog):
    args = _input()
    args["draft"]["evidence"] = [None, {"method_id": "rpc-call-timeout"}]
    result = accept(**args)
    assert result.draft.status == "INSUFFICIENT"
    assert not result.draft.evidence
    assert not caplog.records


@pytest.mark.parametrize("variation", ["extra_fields", "order", "duplicates", "missing", "optional_metadata"])
def test_method_evaluations_use_plan_references_instead_of_model_format(variation, caplog):
    plan = _plan()
    response = _response(plan)
    if variation == "extra_fields":
        response[0]["confidence"] = 0.99
    elif variation == "order":
        response.reverse()
    elif variation == "duplicates":
        response[0]["supporting_event_refs"] *= 2
        response.append(copy.deepcopy(response[0]))
    elif variation == "missing":
        response.pop()
    else:
        response[1].pop("reason")
        response[1].pop("supporting_event_refs")
    result = parse_method_evaluation_response_v2(plan=plan, response=response)
    assert len(result) == len(plan.evaluations)
    assert result[0].verdict == "CONFIRMED"
    assert result[0].supporting_event_refs == plan.evaluations[0].evidence_event_refs
    assert result[1].verdict == ("UNKNOWN" if variation == "missing" else "REJECTED")
    assert not caplog.records


@pytest.mark.parametrize("variation", ["unknown_confirmed", "missing_evidence", "one_bad_finding"])
def test_strict_diagnosis_keeps_usable_findings_without_whole_response_failure(variation, caplog):
    args = _input()
    if variation == "unknown_confirmed":
        args["draft"]["confirmed_methods"].append("unknown")
    elif variation == "missing_evidence":
        args["draft"]["evidence"] = []
    else:
        bad = copy.deepcopy(args["draft"]["evidence"][0])
        bad["identity_tokens"] = ["other-request"]
        bad["sources"][0]["line"] = "not the real log line"
        args["draft"]["evidence"].append(bad)
    result = verify_method_diagnosis(**args)
    assert result.draft.status == ("INSUFFICIENT" if variation == "missing_evidence" else "PARTIAL")
    assert len(result.draft.evidence) == (0 if variation == "missing_evidence" else 1)
    assert not caplog.records


@pytest.mark.parametrize("variation", ["missing_findings", "contradicting_verdict", "extra_findings"])
def test_strict_review_aligns_with_known_findings_without_response_failure(variation, caplog):
    diagnosis = verify_method_diagnosis(**_input())
    finding = {"method_id": "rpc-call-timeout", "identity_tokens": ["request_id=42"], "verdict": "PASS"}
    value = {"verdict": "PASS", "findings": [finding]}
    if variation == "missing_findings":
        value["findings"] = []
    elif variation == "contradicting_verdict":
        finding["verdict"] = "REJECT"
    else:
        value["findings"].append({**finding, "method_id": "unknown"})
    result = verify_method_review(diagnosis, value)
    assert len(result.findings) == 1
    assert result.findings[0].method_id == "rpc-call-timeout"
    assert result.verdict == {"missing_findings": "NEED_MORE_EVIDENCE", "contradicting_verdict": "REJECT", "extra_findings": "PASS"}[variation]
    assert not caplog.records


@pytest.mark.parametrize("field", ["confirmed_methods", "identity_tokens"])
def test_strict_diagnosis_derives_summary_lists_from_grounded_findings(field):
    args = _input()
    if field == "confirmed_methods":
        args["draft"].pop(field)
    else:
        args["draft"]["evidence"][0].pop(field)
    result = verify_method_diagnosis(**args)
    assert len(result.draft.evidence) == 1
    assert result.draft.confirmed_methods == ("rpc-call-timeout",)
    assert result.draft.status in {"CONFIRMED", "PARTIAL"}


def test_strict_diagnosis_without_tokens_keeps_findings_with_distinct_sources(caplog):
    args = _input()
    lines = [f"rpc deadline exceeded request_id={number}" for number in (42, 43)]
    target = _target_log(("\n".join(lines) + "\n").encode())
    args["target_logs"] = (target,)
    args["skill_load"] = scan_method_markers(skill=args["skill"], target_logs=(target,))
    args["draft"]["evidence"] = [{"method_id": "rpc-call-timeout", "summary": f"finding {index}",
        "sources": [{"source_id": "client", "line_number": index + 1,
            "marker": "rpc deadline exceeded", "line": line}]}
        for index, line in enumerate(lines)]
    result = verify_method_diagnosis(**args)
    assert len(result.draft.evidence) == 2
    assert [item.identity_tokens for item in result.draft.evidence] == [(), ()]
    assert not caplog.records


@pytest.mark.parametrize("wrapper", ["bom", "fence", "bom_fence"])
def test_method_evaluation_array_accepts_display_wrappers(wrapper, caplog):
    plan = _plan()
    response = json.dumps(_response(plan))
    if "fence" in wrapper:
        response = "```json\n" + response + "\n```"
    if "bom" in wrapper:
        response = "\ufeff" + response
    result = parse_method_evaluation_response_v2(plan=plan, response=response)
    assert result[0].verdict == "CONFIRMED"
    assert result[1].verdict == "REJECTED"
    assert not caplog.records
