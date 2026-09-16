from __future__ import annotations

import copy
import json
import zipfile
from dataclasses import replace

import pytest

from problem_locator.contracts import canonical_json_bytes
from problem_locator.contracts import ArtifactKind, Job, OutcomeResultType, ReviewPolicy
from problem_locator.integrations.result_archive import write_result_archive_file
from problem_locator.runtime.methods_selection import diagnosis_mapping, select_method_diagnosis
from tests.deterministic.contracts.fakes import InMemoryCancellationSignal
from tests.deterministic.unit.runtime.test_diagnosis_runtime import _public_fake_claiming_runtime
from tests.deterministic.unit.runtime.test_methods_output_pipeline import _verified_input


def _input():
    skill, target, scan, draft = _verified_input()
    return dict(skill=skill, target_logs=(target,), skill_load=scan,
                logparse_receipt_sha256="a" * 64,
                draft=json.loads(canonical_json_bytes(diagnosis_mapping(draft))))


def test_complete_grounded_result_keeps_its_claims():
    kwargs = _input()
    selected = select_method_diagnosis(**kwargs)
    assert selected.draft.status == "CONFIRMED"
    assert selected.draft.evidence[0].summary == kwargs["draft"]["evidence"][0]["summary"]
    assert selected.selection["rejected"] == []
    assert selected.selection["source_draft_sha256"] == selected.selection["effective_draft_sha256"]


@pytest.mark.parametrize("malformed", [False, True])
def test_bad_finding_is_removed_as_a_whole_without_borrowed_sources(malformed):
    kwargs = _input()
    rejected = copy.deepcopy(kwargs["draft"]["evidence"][0])
    rejected["identity_tokens"] = ["request_id=43"]
    rejected["summary"] = "UNTRUSTED ROOT CAUSE"
    rejected["sources"] = [] if malformed else [
        *rejected["sources"], {**rejected["sources"][0], "source_id": "absent"},
    ]
    kwargs["draft"]["evidence"].append(rejected)
    result = select_method_diagnosis(**kwargs)
    assert result.draft.status == "PARTIAL"
    assert len(result.draft.evidence) == 1
    assert "UNTRUSTED" not in str(diagnosis_mapping(result.draft))
    assert result.selection["rejected"][0]["index"] == 1
    assert result.draft.candidate_methods == ()


def test_all_structured_but_ungrounded_findings_become_insufficient():
    kwargs = _input()
    kwargs["draft"]["evidence"][0]["sources"][0]["line"] = "forged line"
    result = select_method_diagnosis(**kwargs)
    assert result.draft.status == "INSUFFICIENT"
    assert result.draft.evidence == result.draft.confirmed_methods == ()
    assert result.selection["gap_messages"]


@pytest.mark.parametrize("conflict", [False, True])
def test_identical_duplicates_merge_but_conflicting_identity_is_rejected(conflict):
    kwargs = _input()
    duplicate = copy.deepcopy(kwargs["draft"]["evidence"][0])
    if conflict:
        duplicate["summary"] = "A different claim for the same identity."
    kwargs["draft"]["evidence"].append(duplicate)
    result = select_method_diagnosis(**kwargs)
    if conflict:
        assert result.draft.status == "INSUFFICIENT"
        assert not result.draft.evidence
        assert [item["reason_code"] for item in result.selection["rejected"]].count("EVIDENCE_IDENTITY_CONFLICT") == 2
    else:
        assert result.draft.status == "CONFIRMED"
        assert len(result.draft.evidence) == 1
        assert result.selection["merged"] == [{"index": 1, "retained_index": 0}]


def test_malformed_duplicate_cannot_preserve_conflicting_group():
    kwargs = _input()
    duplicate = copy.deepcopy(kwargs["draft"]["evidence"][0])
    duplicate["sources"] = []
    kwargs["draft"]["evidence"].append(duplicate)
    result = select_method_diagnosis(**kwargs)
    assert result.draft.status == "INSUFFICIENT"
    assert not result.draft.evidence


@pytest.mark.parametrize("items", [[], [None], [{"method_id": "unknown"}]])
def test_confirmed_without_any_recognizable_evidence_remains_format_failure(items):
    kwargs = _input()
    kwargs["draft"]["evidence"] = items
    with pytest.raises(ValueError):
        select_method_diagnosis(**kwargs)


def test_missing_target_downgrades_valid_finding_without_removing_it():
    result = select_method_diagnosis(**_input(), missing_targets=("server",))
    assert result.draft.status == "PARTIAL"
    assert len(result.draft.evidence) == 1
    assert result.selection["missing_targets"] == ["server"]


def test_unknown_named_methods_become_gaps_without_candidate_fabrication():
    kwargs = _input()
    kwargs["draft"]["confirmed_methods"].append("unknown")
    kwargs["draft"]["candidate_methods"] = ["also-unknown"]
    result = select_method_diagnosis(**kwargs)
    assert result.draft.status == "PARTIAL"
    assert result.draft.confirmed_methods == ("rpc-call-timeout",)
    assert not result.draft.candidate_methods


def test_shared_scan_drift_is_not_an_item_rejection():
    kwargs = _input()
    kwargs["skill_load"] = replace(kwargs["skill_load"], package_tree_sha256="b" * 64)
    with pytest.raises(ValueError, match="frozen marker scan"):
        select_method_diagnosis(**kwargs)


def _no_review_runtime(tmp_path, kind):
    runtime, job, factory, backend, resources = _public_fake_claiming_runtime(tmp_path, kind)
    job = Job.model_validate({**job.model_dump(mode="json"), "review_policy": "NONE"})
    runtime._state_repository.aggregate.jobs[job.job_id] = job
    backend.job = job
    return runtime, job, factory, backend, resources


def _report(outcome, resources):
    proposal = next(item for item in outcome.proposed_artifacts if item.artifact_kind is ArtifactKind.USER_RESULT)
    data = resources._staged[("proposal", proposal.staged_resource_ref.staging_id)].payload
    return json.loads(data), proposal.metadata.archive_plan


def test_missing_target_keeps_finding_through_finalizer_and_zip(tmp_path):
    runtime, job, factory, backend, resources = _no_review_runtime(tmp_path, "confirmed_missing")
    outcome = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert outcome.result_type is OutcomeResultType.COMPLETED, outcome.error
    report, plan = _report(outcome, resources)
    assert report["status"] == "PARTIAL" and report["root_cause"] is None
    assert report["findings"] and any("client" in gap for gap in report["evidence_gaps"])
    assert all(item["status"] == "UNKNOWN" for item in report["completion_criteria_mapping"])
    assert len(plan.logs) == 1 and "server" in plan.logs[0].archive_name
    tree = factory.open_calls[0][1] / "output/proposals/methods-preprocess/tree"
    destination = tmp_path / "partial.zip"
    write_result_archive_file(destination, plan=plan,
                              source_paths=[tree / log.relative_path for log in plan.logs])
    with zipfile.ZipFile(destination) as archive:
        assert archive.read(plan.logs[0].archive_name) == backend.target_contents["server"]
        assert not any("client" in name for name in archive.namelist())
    assert len(backend.calls) == 1


@pytest.mark.parametrize("review", [False, True])
def test_partial_criteria_are_unknown_without_independent_completion_evidence(tmp_path, monkeypatch, review):
    factory = _public_fake_claiming_runtime if review else _no_review_runtime
    runtime, job, _, backend, resources = factory(tmp_path, "success")
    execute = backend._run_methods

    def partial(kwargs):
        result = execute(kwargs)
        value = json.loads(result.final_result)
        value["status"] = "PARTIAL"
        return replace(result, final_result=canonical_json_bytes(value).decode())

    monkeypatch.setattr(backend, "_run_methods", partial)
    outcome = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert outcome.result_type is OutcomeResultType.COMPLETED, outcome.error
    report, _ = _report(outcome, resources)
    assert report["status"] == "PARTIAL"
    assert report["findings"] and report["evidence_gaps"]
    assert all(item["status"] == "UNKNOWN" and not item["evidence_bindings"]
               for item in report["completion_criteria_mapping"])
    assert len(backend.calls) == 1


def test_same_log_findings_retain_only_their_own_line_citations(tmp_path, monkeypatch):
    runtime, job, _, backend, resources = _no_review_runtime(tmp_path, "success")
    lines = [f"rpc deadline exceeded request_id={number}" for number in (42, 43)]
    backend.target_contents["client"] = ("\n".join(lines) + "\n").encode()
    execute = backend._run_methods

    def two_findings(kwargs):
        result = execute(kwargs)
        value = json.loads(result.final_result)
        value["status"] = "PARTIAL"
        value["evidence"] = [{"method_id": "rpc-call-timeout", "summary": f"finding {index}",
            "identity_tokens": [f"request_id={42 + index}"], "sources": [{"source_id": "client",
                "line_number": index + 1, "marker": "rpc deadline exceeded", "line": line}]}
            for index, line in enumerate(lines)]
        return replace(result, final_result=canonical_json_bytes(value).decode())

    monkeypatch.setattr(backend, "_run_methods", two_findings)
    outcome = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert outcome.result_type is OutcomeResultType.COMPLETED, outcome.error
    report, _ = _report(outcome, resources)
    assert [item["statement"] for item in report["findings"]] == ["finding 0", "finding 1"]
    assert [[citation["line_start"] for citation in finding["citations"]]
            for finding in report["findings"]] == [[1], [2]]
    assert len(backend.calls) == 1
