from __future__ import annotations

import copy
import gc
import json
import tracemalloc
import weakref
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from problem_locator.contracts import (
    EvidenceBinding, Job, ServerRuleStatus, WorkspaceInputManifest,
    bytes_sha256, canonical_json_bytes,
)
from problem_locator.runtime.authoritative_targets import AuthoritativeTargetLog, AuthoritativeTargetSet
from problem_locator.runtime.methods_advisory import (
    ADVISORY_LIMITATION, accept_method_diagnosis_advisory,
    parse_advisory_diagnosis, parse_advisory_review,
)
from problem_locator.runtime.methods_grounding import (
    FrozenTargetLogV1, GroundedEvidenceSourceV1, MethodEvidenceV1,
    verify_method_diagnosis, verify_method_review,
)
from problem_locator.runtime.methods_outcome import (
    _RequestedPhysicalLines, _physical_line, map_verified_methods_draft, method_evidence_rule_ids,
)
from problem_locator.runtime.methods_selection import diagnosis_mapping
from problem_locator.runtime.output_reader import ValidatedMethodsPreprocessing
from problem_locator.runtime.result_types import CapturedTargetLog
from problem_locator.runtime.user_results import build_server_result_bundle
from tests.deterministic.unit.runtime.test_methods_output_pipeline import _contract
from tests.deterministic.unit.runtime.test_methods_selection import _input


def _accept(**kwargs):
    return accept_method_diagnosis_advisory(**kwargs)


@pytest.mark.parametrize("change", [
    {"line": "rpc deadline exceeded  request_id=42"},
    {"line_number": 2},
    {"marker": "rpc deadline"},
    {"source_id": "../other-case"},
])
def test_quote_location_and_marker_metadata_do_not_remove_a_judgment(change):
    args = _input()
    args["draft"]["evidence"][0]["sources"][0].update(change)
    with pytest.raises(ValueError):
        verify_method_diagnosis(**args)
    accepted = _accept(**args)
    assert accepted.audit.validation_mode == "advisory"
    assert accepted.audit.checked_source_count == 0
    assert accepted.selection is None
    assert accepted.draft.status == "PARTIAL"
    assert accepted.draft.evidence[0].summary == args["draft"]["evidence"][0]["summary"]
    assert ADVISORY_LIMITATION in accepted.draft.limitations


@pytest.mark.parametrize("missing", ["identity_tokens", "sources"])
def test_missing_evidence_metadata_keeps_the_recognizable_finding(missing):
    args = _input()
    args["draft"]["evidence"][0].pop(missing)
    accepted = _accept(**args)
    assert len(accepted.draft.evidence) == 1
    assert getattr(accepted.draft.evidence[0], missing) == ()
    assert parse_advisory_diagnosis(json.loads(canonical_json_bytes(diagnosis_mapping(accepted.draft)))) == accepted.draft


def test_identity_mismatch_and_no_marker_hit_do_not_trigger_another_scan(monkeypatch):
    args = _input()
    raw = b"request_id=42 reports delayed completion\n"
    target = args["target_logs"][0]
    args["target_logs"] = (replace(target, content=raw, content_sha256=bytes_sha256(raw)),)
    args["skill_load"] = replace(args["skill_load"], marker_hits=(), loaded_method_ids=())
    args["draft"]["evidence"][0]["identity_tokens"] = ["request_id:42"]

    def forbidden(**kwargs):
        raise AssertionError("advisory must not rescan the target logs")

    monkeypatch.setattr("problem_locator.runtime.methods_grounding.scan_method_markers", forbidden)
    assert len(_accept(**args).draft.evidence) == 1


def test_duplicate_identities_keep_distinct_judgments_without_invented_tokens():
    args = _input()
    original = args["draft"]["evidence"][0]
    args["draft"]["evidence"].extend([copy.deepcopy(original), {**copy.deepcopy(original), "summary": "另一种模型解释。"}])
    accepted = _accept(**args)
    assert len(accepted.draft.evidence) == 2
    assert accepted.draft.evidence[0].identity_tokens == accepted.draft.evidence[1].identity_tokens
    assert len(set(method_evidence_rule_ids(accepted))) == 2
    assert accepted.advisory["merged"] == [{"index": 1, "retained_index": 0}]


def test_unknown_method_is_a_labeled_lead_without_fabricated_skill_confirmation():
    args = _input()
    args["draft"]["confirmed_methods"] = ["not-in-this-skill"]
    args["draft"]["evidence"][0]["method_id"] = "not-in-this-skill"
    accepted = _accept(**args)
    assert accepted.draft.status == "PARTIAL"
    assert accepted.draft.evidence[0].summary == args["draft"]["evidence"][0]["summary"]
    assert accepted.draft.evidence[0].method_id == "not-in-this-skill"
    assert any("不在当前 Skill" in text for text in accepted.draft.limitations)


def test_empty_evidence_is_a_deliverable_inconclusive_result():
    args = _input()
    args["draft"]["evidence"] = []
    accepted = _accept(**args)
    assert accepted.draft.status == "INSUFFICIENT"
    assert accepted.draft.confirmed_methods == accepted.draft.evidence == ()


def test_effective_metadata_limits_allow_reloading_normalized_findings():
    args = _input()
    original = args["draft"]["evidence"][0]
    args["draft"]["limitations"] = [f"模型说明 {index}" for index in range(200)]
    args["draft"]["evidence"] = [
        {**copy.deepcopy(original), "method_id": f"unknown-method-{index}"}
        for index in range(201)
    ]
    accepted = _accept(**args)
    effective = json.loads(canonical_json_bytes(diagnosis_mapping(accepted.draft)))
    assert parse_advisory_diagnosis(effective) == accepted.draft


@pytest.mark.parametrize("field,value", [("status", "PASS"), ("evidence", "[]"), ("confirmed_methods", {})])
def test_top_level_protocol_type_and_status_errors_remain_fatal(field, value):
    args = _input()
    args["draft"][field] = value
    with pytest.raises((TypeError, ValueError)):
        _accept(**args)


def test_unrecognizable_findings_and_frozen_input_identity_drift_remain_fatal():
    args = _input()
    args["draft"]["evidence"] = [{"method_id": "rpc-call-timeout"}]
    with pytest.raises(ValueError, match="recognizable"):
        _accept(**args)
    args = _input()
    args["skill_load"] = replace(args["skill_load"], package_tree_sha256="b" * 64)
    with pytest.raises(ValueError, match="input identities"):
        _accept(**args)
    with pytest.raises(ValueError, match="content_sha256"):
        FrozenTargetLogV1("client", "inputs/client.log", "a" * 64, b"actual\n")
    with pytest.raises(ValueError, match="unsafe"):
        FrozenTargetLogV1("client", "../other-case.log", bytes_sha256(b"actual\n"), b"actual\n")


def _mapped(args):
    accepted = _accept(**args)
    job = _contract("job-diagnose.json", Job)
    skill = args["skill"]
    job = Job.model_validate({**job.model_dump(mode="json"), "skill_ref": {
        "id": f"diagnosis-skill/{skill.registration_id}", "version": skill.registration.version,
        "content_hash": skill.combined_sha256,
    }})
    manifest = _contract("workspace-input-manifest.json", WorkspaceInputManifest)
    artifact_id = job.artifact_refs[0]
    target = AuthoritativeTargetLog(ordinal=1, label="client", requested_module="payment",
        requested_slot="request", requested_process_name="payment-service", requested_pid=None,
        module_key="payment", module_name="payment", slot="request", process_name="payment-service",
        pid=None, match_status="exact", board_cycle=None, cpu_id=None, cpu_cycle=None, caveats=(),
        source_kind="INPUT_ARTIFACT", source_ref=artifact_id,
        source_root=f"inputs/artifacts/{artifact_id}/tree", log_path="client.log",
        archive_name="client__payment__slot_request__payment-service.log")
    binding = EvidenceBinding(existing_evidence_id=None, evidence_proposal_key="methods-target-1")
    captured = CapturedTargetLog(target, args["target_logs"][0].content, (binding,))
    validated = ValidatedMethodsPreprocessing(request_bytes=canonical_json_bytes({"schema_version": 1}),
        broker_audit_bytes=canonical_json_bytes({"schema_version": 1}),
        authoritative_targets=AuthoritativeTargetSet(problem_time="2026-07-31T00:00:00.000Z",
            targets=(target,), source_size=512, source_sha256="1" * 64),
        target_logs=(captured,), proposal_resources=())
    mapped = map_verified_methods_draft(job=job, manifest=manifest,
        source_draft_bytes=canonical_json_bytes(args["draft"]), verified_diagnosis=accepted,
        preprocessing=SimpleNamespace(validated=validated))
    return job, accepted, mapped


@pytest.mark.parametrize("location", ["text_differs", "line_out_of_range", "unknown_source", "absent_sources"])
def test_unverified_findings_survive_report_and_archive_plan_without_forged_excerpts(location):
    args = _input()
    item = args["draft"]["evidence"][0]
    if location == "text_differs":
        item["sources"][0]["line"] = "MODEL INVENTED EXCERPT"
    elif location == "line_out_of_range":
        item["sources"][0]["line_number"] = 9000
    elif location == "unknown_source":
        item["sources"][0]["source_id"] = "../other-case"
    else:
        item.pop("sources")
    job, accepted, mapped = _mapped(args)
    assert mapped.verification.evidence_validation_mode == "advisory"
    assert all(rule.server_evaluation.status is ServerRuleStatus.SEMANTIC_ONLY
               for rule in mapped.verification.audit.rules)
    assert all(not rule.server_evaluation.issues for rule in mapped.verification.audit.rules)
    bundle = build_server_result_bundle(job=job, result_type=mapped.draft.result_type,
        payload=mapped.draft.payload, verification=mapped.verification,
        authoritative_targets=mapped.authoritative_targets, captured_logs=mapped.target_logs)
    report = bundle.report
    assert report.status == "PARTIAL" and report.root_cause is None
    assert report.findings[0].statement == item["summary"]
    assert report.findings[0].confidence != 1.0
    assert all(criterion.status.value == "UNKNOWN" for criterion in report.completion_criteria_mapping)
    assert report.evidence_gaps and ADVISORY_LIMITATION in report.limitations
    citation = report.findings[0].citations[0]
    if location == "text_differs":
        assert citation.excerpt == "rpc deadline exceeded request_id=42"
        assert citation.raw_bytes_sha256 == bytes_sha256(args["target_logs"][0].content)
    else:
        assert citation.archive_name is citation.line_start is citation.excerpt is None
    assert b"MODEL INVENTED EXCERPT" not in canonical_json_bytes(report)
    assert method_evidence_rule_ids(accepted) == mapped.verification.audit.required_rule_ids


def test_advisory_review_accepts_whole_result_verdict_and_keeps_strict_history_strict():
    args = _input()
    advisory = _accept(**args)
    review = {"schema_version": 1, "verdict": "PASS", "findings": [{
        "method_id": "rpc-call-timeout", "verdict": "PASS", "reason": "整份结果可作为待核对判断。",
    }], "limitations": []}
    parsed = parse_advisory_review(review)
    assert parsed.findings[0].identity_tokens == ()
    assert verify_method_review(advisory, review) == parsed
    strict = verify_method_diagnosis(**args)
    assert strict.audit.validation_mode == "strict"
    with pytest.raises(ValueError):
        verify_method_review(strict, review)
    assert "validation_mode" in asdict(advisory.audit)
    with pytest.raises(ValueError):
        parse_advisory_review({**review, "verdict": "APPROVED"})


def test_unknown_method_punctuation_does_not_collide_in_persisted_factor_ids():
    args = _input()
    original = args["draft"]["evidence"][0]
    args["draft"]["confirmed_methods"] = ["model-method", "model_method"]
    args["draft"]["evidence"] = [
        {**copy.deepcopy(original), "method_id": method}
        for method in args["draft"]["confirmed_methods"]
    ]
    _, _, mapped = _mapped(args)
    factors = mapped.draft.payload.candidate_conclusion_draft.causal_factors
    assert len(factors) == len({item.factor_id for item in factors}) == 2


class _TrackedLineList(list):
    pass


class _SplitCountingBytes(bytes):
    def __new__(cls, value):
        result = super().__new__(cls, value)
        result.split_count = 0
        result.line_lists = []
        return result

    def splitlines(self, keepends=False):
        self.split_count += 1
        result = _TrackedLineList(super().splitlines(keepends=keepends))
        self.line_lists.append(weakref.ref(result))
        return result


def _requested_evidence(source_ids, line_numbers):
    return tuple(MethodEvidenceV1("rpc-call-timeout", "模型判断。", (), tuple(
        GroundedEvidenceSourceV1(source_id, number, "", "") for number in line_numbers
    )) for source_id in source_ids)


def test_requested_line_cache_splits_each_source_once_and_releases_full_lists():
    sources = {f"source_{index}": _SplitCountingBytes(b"first\r\n\r\nthird\n") for index in range(4)}
    cache = _RequestedPhysicalLines(_requested_evidence(sources, (1, 2, 3, 9000)))
    for _ in range(2):
        for source_id, content in sources.items():
            assert cache.get(source_id, content, 1) == (b"first\r\n", "first")
            assert cache.get(source_id, content, 3) == (b"third\n", "third")
            with pytest.raises(ValueError, match="citation is empty"):
                cache.get(source_id, content, 2)
            with pytest.raises(ValueError, match="escapes its target log"):
                cache.get(source_id, content, 9000)
    assert all(content.split_count == 1 for content in sources.values())
    assert all(reference() is None for content in sources.values() for reference in content.line_lists)


def test_requested_line_cache_large_multi_source_memory_stays_below_full_cache():
    # Allocate the already-captured log bytes before tracing. The comparison
    # isolates cache overhead, not platform-dependent wall-clock performance.
    sources = {f"source_{index}": ((b"x" * 127) + b"\n") * 8192 for index in range(8)}
    evidence = _requested_evidence(sources, (1,))

    def full_cache():
        retained = {}
        for source_id, content in sources.items():
            retained[source_id] = content.splitlines(keepends=True)
            _physical_line(content, 1, lines=retained[source_id])
        return retained

    def selected_cache():
        cache = _RequestedPhysicalLines(evidence)
        for source_id, content in sources.items():
            assert cache.get(source_id, content, 1) == (b"x" * 127 + b"\n", "x" * 127)
        return cache

    def measured(build):
        gc.collect()
        tracemalloc.start()
        try:
            retained = build()
            current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        del retained
        return current, peak

    full_retained, full_peak = measured(full_cache)
    selected_retained, selected_peak = measured(selected_cache)
    assert selected_retained * 20 < full_retained
    assert selected_peak * 3 < full_peak
