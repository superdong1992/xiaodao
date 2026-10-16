from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from problem_locator.application.formalization import build_job
from problem_locator.application.preparation import runtime_bindings_from_job
from problem_locator.contracts import (
    ApplicationError, CreateCase, JobType, ReportContinuationContext,
    RouteDecision, RouteKind, RouteOutcomeTriggerPayload, TriggerType,
    business_request_sha256, canonical_json_bytes,
)
from problem_locator.domain import DomainCoordinator
from problem_locator.runtime.continuation_context import build_report_continuation_context
from problem_locator.runtime.generic_locator import GenericLocatorExecutor
from problem_locator.runtime.input_profile import load_builtin_input_profile
from tests.deterministic.contracts._specialized_direct_support import direct_job
from tests.deterministic.contracts.fakes import PureContextSnapshotProjector
from tests.deterministic.unit.application.test_external_commands import _create_command, _handler, _state
from tests.deterministic.unit.application.test_generic_log_handoff import _log_bindings, _preflight, _with_logs
from tests.deterministic.unit.application.test_outcome_submission import CASE_ID, DIAGNOSE_JOB_ID, _running_generic_state
from tests.deterministic.unit.domain._builders import (
    continuation, diagnose_job, rebuild, route_job, route_outcome, snapshot_with_active, trigger,
)


def context(report="# 原报告\n\n原判断：客户端发生超时。\n"):
    return ReportContinuationContext(
        parent_run_id="00000000-0000-0000-0000-000000000901",
        parent_case_id="00000000-0000-0000-0000-000000000902",
        parent_report_artifact_id="00000000-0000-0000-0000-000000000903",
        parent_report_sha256=hashlib.sha256(report.encode()).hexdigest(),
        parent_report_markdown=report, change_note="修正：超时发生在服务端。",
        goal="核实服务端超时原因。",
    )


def test_context_checks_report_hash_size_and_version():
    assert context("a" * 65_536).parent_report_version == 1
    for changes in ({"parent_report_sha256": "0" * 64}, {"parent_report_version": 0}):
        with pytest.raises(ValidationError):
            ReportContinuationContext.model_validate({**context().model_dump(), **changes})
    with pytest.raises(ValidationError):
        context("a" * 65_537)


def test_optional_context_preserves_legacy_bytes_and_changes_request_identity():
    command = _create_command()
    explicit = CreateCase.model_validate({**command.model_dump(), "continuation_context": None})
    continued = CreateCase.model_validate({**command.model_dump(), "continuation_context": context()})
    assert canonical_json_bytes(command) == canonical_json_bytes(explicit)
    assert business_request_sha256(command) != business_request_sha256(continued)
    assert "continuation_context" not in command.model_dump()
    job = route_job()
    assert canonical_json_bytes(job) == canonical_json_bytes(rebuild(job, continuation_context=None))


def test_create_case_keeps_parent_report_out_of_router_and_current_facts():
    command = CreateCase.model_validate({**_create_command().model_dump(), "continuation_context": context()})
    handler, repository, *_ = _handler(_state(), DomainCoordinator())
    result = handler.execute(command)
    aggregate = repository.read_case(result.business_receipt.case_id)
    assert aggregate.case.continuation_context == context()
    assert aggregate.case.raw_problem_text == command.raw_problem_text
    assert [fact.statement for fact in aggregate.case.diagnosis_state.user_facts] == ["us-east"]
    assert not aggregate.case.diagnosis_state.confirmed_facts
    assert not aggregate.evidence and not aggregate.outcomes
    assert aggregate.jobs[aggregate.case.active_job_id].continuation_context is None


@pytest.mark.parametrize("mode", ["generic", "direct", "unsupported-specialized"])
def test_route_freezes_context_and_all_archives_only_for_supported_diagnosis(mode):
    template = direct_job() if mode == "direct" else diagnose_job()
    source = route_job()
    if mode != "generic":
        source = rebuild(source, available_skill_refs=[template.skill_ref])
    snapshot = snapshot_with_active(source)
    snapshot = rebuild(snapshot, case=rebuild(snapshot.case, continuation_context=context()))
    decision = (RouteDecision(kind=RouteKind.NO_CAPABILITY, skill_ref=None, reason="通用诊断", confidence=0.9)
        if mode == "generic" else RouteDecision(kind=RouteKind.MATCHED, skill_ref=template.skill_ref, reason="已匹配", confidence=0.9))
    outcome = rebuild(route_outcome(), payload=decision,
        result_type="NO_CAPABILITY" if mode == "generic" else "COMPLETED")
    resources = rebuild(continuation(incoming_outcome_id=outcome.outcome_id), attachment_refs=[
        "00000000-0000-0000-0000-000000000910", "00000000-0000-0000-0000-000000000911"])
    request = trigger(snapshot, trigger_type=TriggerType.ROUTE_OUTCOME,
        payload=RouteOutcomeTriggerPayload(job_outcome=outcome), continuation_resources=resources,
        occurred_at=outcome.produced_at,
        bindings={JobType.DIAGNOSE: _log_bindings() if mode == "generic" else runtime_bindings_from_job(template)})
    plan = DomainCoordinator().plan(snapshot, request)
    if mode == "unsupported-specialized":
        assert isinstance(plan, ApplicationError)
        assert plan.details[0].field == "continuation_context"
        return
    assert not isinstance(plan, ApplicationError)
    spec = plan.next_job_spec
    assert spec.continuation_context == context()
    assert spec.attachment_refs == resources.attachment_refs
    job = build_job(spec, job_id="00000000-0000-0000-0000-000000000912", case_id=source.case_id,
        created_at=source.created_at, target_diagnosis_state=snapshot.case.diagnosis_state,
        projector=PureContextSnapshotProjector(), existing_evidence_ids=[], evidence_ids_by_proposal_key={},
        existing_artifact_ids=[], artifact_ids_by_proposal_key={}, existing_candidate=None,
        candidates_by_proposal_key={})
    assert job.continuation_context == context()
    assert job.evidence_refs == job.artifact_refs == []
    assert job.previous_outcome_refs == ([] if mode == "generic" else [outcome.outcome_id])


def test_generic_prompt_uses_separate_read_only_context_and_full_change_report():
    job = _running_generic_state().cases[CASE_ID].jobs[DIAGNOSE_JOB_ID]
    job = rebuild(job, continuation_context=context())
    prompt = GenericLocatorExecutor.build_prompt(job, SimpleNamespace(profile_text="profile", output_contract_text="output"))
    raw = prompt.split("<<<RAW_PROBLEM_TEXT_UTF8_BYTES:", 1)[1].split("<<<END_RAW_PROBLEM_TEXT>>>", 1)[0]
    assert context().parent_report_markdown not in raw
    assert context().parent_report_markdown.replace("\n", "\\n") in prompt
    assert "## 本轮变化" in prompt and "完整诊断报告" in prompt
    assert "未被本轮提及的内容不等于被撤回" in prompt
    assert build_report_continuation_context(None) == ""


@pytest.mark.parametrize("maximum", [1, 20])
def test_current_multi_archive_requirement_and_legacy_single_archive_outcome_are_valid(maximum):
    from problem_locator.contracts import validate_outcome_for_job
    job = _with_logs(_running_generic_state()).cases[CASE_ID].jobs[DIAGNOSE_JOB_ID]
    assert validate_outcome_for_job(job, _preflight(job, max_count=maximum))
    assert load_builtin_input_profile()["log_archive_requirement"]["constraints"]["max_count"] == 20
