from __future__ import annotations

import pytest
from pydantic import ValidationError

from problem_locator.application.projection import project_case_components
from problem_locator.contracts import (
    CaseAggregate, DiagnosisMode, GenericDiagnosisOutcome,
    GenericResultStatus, Job, JobOutcome, JobSpec, ReviewPolicy, RuntimeBindings,
    is_specialized_direct, specialized_direct_skill_name, validate_outcome_for_job,
)

from ._specialized_direct_support import REPORT, direct_aggregate, direct_job, direct_outcome


def test_direct_identity_is_frozen_in_job_spec_and_runtime_bindings() -> None:
    job = direct_job()
    assert is_specialized_direct(job)
    assert specialized_direct_skill_name(job) == "rpc-timeout"
    values = job.model_dump(mode="python")
    bindings = RuntimeBindings.model_validate({name: values[name] for name in RuntimeBindings.model_fields})
    assert is_specialized_direct(bindings)
    spec_values = {name: values[name] for name in JobSpec.model_fields if name in values}
    spec_values.update(target_state_revision=job.base_state_revision,
        evidence_bindings=[], artifact_bindings=[], review_target_binding=None)
    spec = JobSpec.model_validate(spec_values)
    assert is_specialized_direct(spec)
    assert spec.diagnosis_mode is DiagnosisMode.SPECIALIZED
    assert spec.skill_ref == job.skill_ref


@pytest.mark.parametrize("field,value", [
    ("review_policy", ReviewPolicy.INDEPENDENT),
    ("agent_profile_ref", {"id": "agent-profile/specialist", "version": "1", "content_hash": "a" * 64}),
    ("output_contract_ref", {"id": "output-contract/diagnose", "version": "1", "content_hash": "a" * 64}),
    ("skill_ref", {"id": "rpc-timeout", "version": "1", "content_hash": "a" * 64}),
])
def test_direct_job_rejects_partial_identity_or_review(field, value) -> None:
    values = direct_job().model_dump(mode="python")
    values[field] = value
    with pytest.raises(ValidationError):
        Job.model_validate(values)


@pytest.mark.parametrize("status", list(GenericResultStatus))
def test_direct_outcome_preserves_skill_status_and_bytes_without_semantic_evidence(status) -> None:
    job = direct_job()
    outcome = direct_outcome(job, status)
    assert validate_outcome_for_job(job, outcome) is outcome
    assert outcome.payload.status is status
    assert outcome.payload.report_markdown == REPORT
    assert outcome.decision_audit is None
    assert outcome.proposed_evidence == []


def test_direct_payload_cannot_be_used_by_old_specialized_job() -> None:
    direct = direct_job()
    values = direct.model_dump(mode="python")
    values["agent_profile_ref"]["id"] = "agent-profile/specialist"
    values["output_contract_ref"]["id"] = "output-contract/diagnose"
    old_job = Job.model_validate(values)
    assert not is_specialized_direct(old_job)
    with pytest.raises(ValueError, match="SPECIALIZED Job cannot publish"):
        validate_outcome_for_job(old_job, direct_outcome(direct))


def test_direct_outcome_rejects_legacy_carrier_and_wrong_skill() -> None:
    job = direct_job()
    outcome = direct_outcome(job)
    wrong_name = outcome.payload.model_copy(update={"skill_name": "another-skill"})
    legacy = GenericDiagnosisOutcome(status=GenericResultStatus.RESOLVED,
        conclusion="根因", root_cause_analysis="分析", skill_name="rpc-timeout")
    for payload in (wrong_name, legacy):
        changed = JobOutcome.model_validate({**outcome.model_dump(mode="python"), "payload": payload})
        with pytest.raises(ValueError, match="exact Skill Markdown"):
            validate_outcome_for_job(job, changed)


@pytest.mark.parametrize("status", list(GenericResultStatus))
def test_direct_aggregate_and_public_view_keep_full_skill_binding(status) -> None:
    aggregate = direct_aggregate(status)
    case = aggregate.case
    view = project_case_components(case, None, aggregate.artifacts.values())
    assert view.generic_result_v2.report_markdown == REPORT
    assert view.selected_skill_ref == case.selected_skill_ref
    assert case.final_result is None
    assert case.unresolved_result is None
    assert case.diagnosis_state.candidate_conclusion is None
    assert view.archive_status == "NOT_REQUIRED"
    assert [item.kind.value for item in view.artifacts] == ["GENERIC_REPORT"]


@pytest.mark.parametrize("field,value", [("version", "wrong-version"), ("content_hash", "0" * 64)])
def test_direct_aggregate_rejects_selected_skill_version_or_hash_drift(field, value) -> None:
    values = direct_aggregate().model_dump(mode="python")
    values["case"]["selected_skill_ref"][field] = value
    with pytest.raises(ValidationError, match="generic_result_v2 must exactly bind"):
        CaseAggregate.model_validate(values)


def test_direct_case_and_view_reject_unrelated_skill_name() -> None:
    aggregate = direct_aggregate()
    view = project_case_components(aggregate.case, None, aggregate.artifacts.values())
    for model in (aggregate.case, view):
        values = model.model_dump(mode="python")
        values["selected_skill_ref"]["id"] = "diagnosis-skill/unrelated"
        with pytest.raises(ValidationError, match="Skill name"):
            type(model).model_validate(values)


def test_direct_aggregate_rejects_missing_selected_skill() -> None:
    values = direct_aggregate().model_dump(mode="python")
    values["case"]["selected_skill_ref"] = None
    with pytest.raises(ValidationError, match="generic_result_v2 must exactly bind"):
        CaseAggregate.model_validate(values)


def test_generic_v2_still_requires_no_selected_specialized_skill() -> None:
    values = direct_aggregate().model_dump(mode="python")
    source = next(iter(values["jobs"].values()))
    source.update(
        diagnosis_mode=DiagnosisMode.GENERIC, review_policy=None,
        context_snapshot=None, skill_ref=None, generic_skill_name="rpc-timeout",
        generic_problem_text=values["case"]["raw_problem_text"],
        logparse_tool_ref=None, logparse_product=None,
    )
    source["agent_profile_ref"]["id"] = "agent-profile/generic-locator"
    source["output_contract_ref"]["id"] = "output-contract/generic-locator"
    with pytest.raises(ValidationError, match="generic_result_v2 must exactly bind"):
        CaseAggregate.model_validate(values)
    values["case"]["selected_skill_ref"] = None
    aggregate = CaseAggregate.model_validate(values)
    assert aggregate.case.generic_result_v2.report_markdown == REPORT
