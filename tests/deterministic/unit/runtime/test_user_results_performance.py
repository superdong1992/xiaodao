"""Report citations reuse immutable log work while checking every reference."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from problem_locator.contracts import DecisionAuditV2, DecisionRuleAudit, Job, ServerRuleEvaluation
from problem_locator.runtime import user_results
from problem_locator.runtime.result_types import CapturedTargetLog
from problem_locator.runtime.verification_result import VerificationResult
from tests.deterministic.unit.integrations.test_result_archive import _logs, _target
from tests.deterministic.unit.integrations.test_result_archive_performance import _SplitLifetime


class _CountingBytes(bytes):
    def __new__(cls, content):
        value = super().__new__(cls, content)
        value.split_calls = 0
        return value

    def splitlines(self, keepends=False):
        self.split_calls += 1
        lines = super().splitlines(keepends)
        tracker = getattr(self, "split_tracker", None)
        return lines if tracker is None else tracker.track(lines)


def _inputs(rule_count=50, *, captures=None):
    logs = _logs()
    if captures is None:
        captures = tuple(CapturedTargetLog(
            target=item.target,
            content=_CountingBytes(item.content + b"uncited noise\n" * 1000),
            evidence_bindings=logs[0].evidence_bindings,
        ) for item in logs)
    rules = []
    for index in range(rule_count):
        captured = captures[index % len(captures)]
        raw = bytes(captured.content).splitlines(keepends=True)[0]
        rule = ServerRuleEvaluation(
            rule_id=f"rule-{index}", rule_kind="EVENT_PRESENT", status="VERIFIED_PASS",
            fact_refs=[], evidence_bindings=list(captured.evidence_bindings), anchor_id=None,
            derived_anchor_time=None, observed_times=[], event_observations=[], derived_values=[],
            line_ranges=[dict(path=captured.target.log_path, line_start=1, line_end=1,
                              raw_bytes_sha256=hashlib.sha256(raw).hexdigest())], issues=[],
        )
        rules.append(DecisionRuleAudit(rule_id=rule.rule_id, agent_claim=None, server_evaluation=rule))
    job = Job.model_validate_json((Path(__file__).parents[3] / "fixtures/contracts/positive/job-diagnose.json").read_bytes())
    audit = DecisionAuditV2(
        schema_version=2, job_id=job.job_id, case_id=job.case_id, job_type=job.job_type,
        skill_ref=job.skill_ref, source_draft_sha256="1" * 64, subject_hash="2" * 64,
        candidate_target=None, diagnosis_audit_hash=None, selected_terminal_path_id="confirmed",
        terminal_resolution_status="COMPLETE", required_rule_ids=[item.rule_id for item in rules],
        required_evidence_bindings=list(captures[0].evidence_bindings), rules=rules,
    )
    return VerificationResult(audit=audit, positive_gate_passed=True, decision_evidence_bytes=b""), captures


@pytest.mark.parametrize("rule_count", [2, 50], ids=["two-citations", "fifty-citations"])
def test_report_splits_each_source_and_hashes_each_unique_range_once(monkeypatch, rule_count):
    verification, captures = _inputs(rule_count)
    calls = []
    original_hash = user_results.bytes_sha256

    def counted(raw):
        calls.append(raw)
        return original_hash(raw)

    monkeypatch.setattr(user_results, "bytes_sha256", counted)
    rules = user_results._verification_rules(verification, captures)
    assert [item.content.split_calls for item in captures] == [1, 1]
    assert len(calls) == 2
    assert len(rules) == rule_count
    for index, rule in enumerate(rules):
        captured = captures[index % 2]
        raw = bytes(captured.content).splitlines(keepends=True)[0]
        citation = rule.citations[0]
        assert citation.archive_name == captured.target.archive_name
        assert citation.raw_bytes_sha256 == hashlib.sha256(raw).hexdigest()
        assert citation.excerpt == raw.rstrip(b"\r\n").decode()
    # A second construction owns a fresh cache; no result from a previous Job or
    # invocation is trusted solely because its source path and range match.
    assert user_results._verification_rules(verification, captures) == rules
    assert [item.content.split_calls for item in captures] == [2, 2]
    assert len(calls) == 4


def test_cached_range_still_rejects_a_later_reference_with_the_wrong_hash():
    verification, captures = _inputs(3)
    last = verification.audit.rules[-1]
    value = last.server_evaluation.line_ranges[0]
    evaluation = last.server_evaluation.model_copy(update={
        "line_ranges": [value.model_copy(update={"raw_bytes_sha256": "0" * 64})],
    })
    changed = verification.audit.model_copy(update={
        "rules": [*verification.audit.rules[:-1], last.model_copy(update={"server_evaluation": evaluation})],
    })
    with pytest.raises(ValueError, match="raw-line hash differs"):
        user_results._verification_rules(replace(verification, audit=changed), captures)
    assert captures[0].content.split_calls == 1


def test_report_path_index_preserves_rejection_of_ambiguous_sources():
    verification, captures = _inputs(2)
    duplicate = replace(captures[1], target=replace(captures[1].target, log_path=captures[0].target.log_path))
    with pytest.raises(ValueError, match="one authoritative target"):
        user_results._verification_rules(verification, (captures[0], duplicate))


@pytest.mark.parametrize("source_count", [2, 32], ids=["two-sources", "max-sources"])
def test_report_releases_uncited_lines_before_splitting_the_next_source(
    monkeypatch, record_property, source_count,
):
    tracker = _SplitLifetime()
    bindings = _logs()[0].evidence_bindings
    raw = b"x" * 31 + b"\n"
    captures = []
    for index in range(source_count):
        content = _CountingBytes(raw * 8192)
        content.split_tracker = tracker
        captures.append(CapturedTargetLog(
            target=_target(index + 1, label=f"target-{index}", module="payment",
                           process="payment-service", path=f"task/logs/target-{index}.log"),
            content=content, evidence_bindings=bindings,
        ))
    captures = tuple(captures)
    verification, _ = _inputs(source_count * 3, captures=captures)
    plain = tuple(replace(item, content=bytes(item.content)) for item in captures)
    expected = user_results._verification_rules(verification, plain)
    original_hash = user_results.bytes_sha256
    hash_calls = 0

    def counted(data):
        nonlocal hash_calls
        hash_calls += 1
        return original_hash(data)

    monkeypatch.setattr(user_results, "bytes_sha256", counted)
    assert user_results._verification_rules(verification, captures) == expected
    assert [item.content.split_calls for item in captures] == [1] * source_count
    assert hash_calls == source_count
    assert tracker.peak_sources == 1
    assert tracker.peak_bytes == tracker.largest_source_bytes
    assert tracker.live_sources == tracker.live_bytes == 0
    record_property("source_count", source_count)
    record_property("raw_log_bytes", sum(len(item.content) for item in captures))
    record_property("peak_live_split_sources", tracker.peak_sources)
    record_property("peak_copied_line_bytes", tracker.peak_bytes)
    record_property("range_sha256_calls", hash_calls)


def test_advisory_results_explain_the_missing_evidence_review_without_extra_rules():
    verification, _captures = _inputs(2)
    assert user_results._gaps(None, verification, None, completed=True) == []
    advisory = replace(verification, evidence_validation_mode="advisory")
    assert user_results._gaps(None, advisory, None, completed=True) == [
        "本次未执行证据一致性复核，以下发现为模型判断，仍需结合实际日志确认。",
    ]
    assert advisory.audit == verification.audit
