from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from problem_locator.contracts import (
    ApplicationPortError, CaseStatus, CreateCase, DiagnosisMode, ErrorCode,
    GENERIC_DIRECT_LOG_CONTENT_TYPES, JobOutcome, JobType, OutcomeDisposition,
    ReportContinuationContext, StateFile, SubmitSupplement,
    business_request_sha256, canonical_json_bytes,
)
from problem_locator.domain import DomainCoordinator
from problem_locator.interfaces.mcp_server import _REQUESTS
from problem_locator.runtime.generic_locator import GenericLocatorExecutor
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator, FakeAssetCatalog, FakeClock, InMemoryExecutionRecordStore,
)
from tests.deterministic.contracts.scenario_fakes import assets_for_bindings
from tests.deterministic.unit.application.test_continuation_contract_chain import context
from tests.deterministic.unit.application.test_external_commands import (
    _attachment_record, _bindings, _create_command, _handler, _job, _state,
)
from tests.deterministic.unit.application.test_generic_direct_bindings import _direct_bindings
from tests.deterministic.unit.application.test_generic_log_handoff import ATTACHMENT_ID, _preflight
from tests.deterministic.unit.application.test_job_control import CURRENT_EPOCH, _service as _control
from tests.deterministic.unit.application.test_outcome_submission import (
    CASE_ID, DIAGNOSE_JOB_ID, _service as _outcome_service,
)


class TrackingCatalog(FakeAssetCatalog):
    def __init__(self, bindings=None):
        bindings = bindings or _direct_bindings()
        super().__init__(route=_bindings(_job("job-route.json")), generic=bindings,
                         assets=assets_for_bindings(bindings))
        self.log_requests = []

    def generic_diagnose_bindings(self, *, with_logs=False):
        self.log_requests.append(with_logs)
        return super().generic_diagnose_bindings(with_logs=with_logs)


def forced_command(**changes):
    return CreateCase.model_validate({
        **_create_command(product_line="园区交换机").model_dump(),
        "force_generic": True,
        "continuation_context": context(),
        **changes,
    })


@pytest.mark.parametrize("with_logs", [False, True])
def test_forced_create_starts_pinned_generic_skill_without_route_and_replays(with_logs):
    catalog = TrackingCatalog()
    handler, repository, _, _, dispatcher, *_ = _handler(
        _state(), DomainCoordinator(), catalog=catalog,
    )
    command = forced_command(initial_log_archive_expected=with_logs)
    first = handler.execute(command)
    replay = handler.execute(command)
    aggregate = repository.read_case(first.business_receipt.case_id)
    job = aggregate.jobs[aggregate.case.active_job_id]

    assert first.business_receipt == replay.business_receipt
    assert len(aggregate.jobs) == 1
    assert job.job_type is JobType.DIAGNOSE
    assert job.diagnosis_mode is DiagnosisMode.GENERIC
    assert job.generic_skill_name == _direct_bindings().generic_skill_name
    assert job.generic_log_preparation == "direct-v1"
    assert job.generic_problem_text == command.raw_problem_text
    assert job.generic_product_line == command.product_line
    assert job.generic_log_archive_expected is with_logs
    assert job.continuation_context == command.continuation_context
    assert job.context_snapshot is None
    assert job.skill_ref is None
    assert job.attachment_refs == job.evidence_refs == job.previous_outcome_refs == []
    assert aggregate.case.status is CaseStatus.RUNNING
    assert aggregate.case.selected_skill_ref is None
    assert "force_generic" not in aggregate.case.model_dump()
    assert catalog.route_calls == 0 and catalog.generic_calls == 1
    assert catalog.log_requests == [with_logs]
    assert dispatcher.accepted_job_ids == [job.job_id]
    prompt = GenericLocatorExecutor.build_prompt(
        job, SimpleNamespace(profile_text="profile", output_contract_text="contract"),
    )
    assert f"${job.generic_skill_name}" in prompt


def test_default_create_still_routes_and_force_flag_changes_only_internal_identity():
    legacy = _create_command()
    explicit = CreateCase.model_validate({**legacy.model_dump(), "force_generic": False})
    forced = CreateCase.model_validate({**legacy.model_dump(), "force_generic": True})
    assert canonical_json_bytes(legacy) == canonical_json_bytes(explicit)
    assert "force_generic" not in legacy.model_dump()
    assert business_request_sha256(legacy) != business_request_sha256(forced)
    catalog = TrackingCatalog()
    handler, repository, *_ = _handler(_state(), DomainCoordinator(), catalog=catalog)
    result = handler.execute(legacy)
    aggregate = repository.read_case(result.business_receipt.case_id)
    assert aggregate.jobs[aggregate.case.active_job_id].job_type is JobType.ROUTE
    assert catalog.route_calls == 1 and catalog.generic_calls == 0
    assert len(_REQUESTS) == 7
    assert all("force_generic" not in request.model_fields for request in _REQUESTS.values())


def test_forced_create_rejects_specialized_catalog_bindings_before_commit():
    catalog = TrackingCatalog(_bindings(_job("job-diagnose.json")))
    handler, repository, *_ = _handler(_state(), DomainCoordinator(), catalog=catalog)
    before = canonical_json_bytes(repository.read_snapshot())
    with pytest.raises(ApplicationPortError) as error:
        handler.execute(forced_command())
    assert error.value.error.code is ErrorCode.CONFIG_INVALID
    assert canonical_json_bytes(repository.read_snapshot()) == before


def test_forced_create_log_intent_waits_then_resumes_with_selected_file_and_context():
    catalog = TrackingCatalog()
    records = InMemoryExecutionRecordStore()
    ids = DeterministicIdGenerator(scripted_ids={"case": [CASE_ID], "job": [DIAGNOSE_JOB_ID]})
    handler, repository, *_ = _handler(
        _state().model_copy(update={"generation": 0, "cases": {}}),
        DomainCoordinator(), catalog=catalog, ids=ids, execution_records=records,
        clock=FakeClock("2026-07-31T00:00:00.000Z"),
    )
    command = forced_command(initial_log_archive_expected=True)
    handler.execute(command)
    control, *_ = _control(repository.read_snapshot(), DomainCoordinator(),
                          repository=repository, catalog=catalog)
    assert control.claim_job(DIAGNOSE_JOB_ID, CURRENT_EPOCH).claimed
    job = repository.read_case(CASE_ID).jobs[DIAGNOSE_JOB_ID]
    payload = _preflight(job).model_dump(mode="python")
    payload["produced_at"] = "2026-07-31T00:11:00.000Z"
    payload["payload"]["state_delta"]["add_pending_requirements"][0]["constraints"][
        "allowed_content_types"
    ] = sorted(GENERIC_DIRECT_LOG_CONTENT_TYPES)
    outcome = JobOutcome.model_validate(payload)
    outcome_ref = records.publish_outcome_bytes(job.job_id, canonical_json_bytes(outcome))
    service, _, _, _, _, _, clock = _outcome_service(
        repository, DomainCoordinator(), records, catalog=catalog,
    )
    clock.set("2026-07-31T00:11:01.000Z")
    assert service.submit_outcome(outcome, outcome_ref).disposition is OutcomeDisposition.APPLIED
    waiting = repository.read_snapshot()
    assert waiting.cases[CASE_ID].case.status is CaseStatus.WAITING_ATTACHMENT
    state = waiting.model_dump(mode="python")
    state["cases"][CASE_ID]["attachments"][ATTACHMENT_ID] = _attachment_record(ATTACHMENT_ID).model_dump()
    handler, resumed_repository, *_ = _handler(
        StateFile.model_validate(state), DomainCoordinator(), catalog=catalog,
    )
    result = handler.execute(SubmitSupplement(
        idempotency_key="attach-to-forced-generic", case_id=CASE_ID,
        expected_case_revision=waiting.cases[CASE_ID].case.case_revision,
        inputs={}, attachment_ids=[ATTACHMENT_ID], wait_seconds=0,
    ))
    aggregate = resumed_repository.read_case(CASE_ID)
    resumed = aggregate.jobs[result.business_receipt.job_id]
    assert aggregate.case.status is CaseStatus.RUNNING
    assert resumed.job_type is JobType.DIAGNOSE
    assert resumed.diagnosis_mode is DiagnosisMode.GENERIC
    assert resumed.generic_log_preparation == "direct-v1"
    assert resumed.attachment_refs == [ATTACHMENT_ID]
    assert resumed.generic_problem_text == command.raw_problem_text
    assert resumed.continuation_context == command.continuation_context
    assert resumed.previous_outcome_refs == []
    assert catalog.route_calls == 0 and catalog.generic_calls == 1


def test_optional_followup_context_preserves_bytes_and_accepts_full_read_only_background():
    baseline = context()
    explicit = ReportContinuationContext.model_validate({
        **baseline.model_dump(), "followup_context_json": None,
    })
    assert canonical_json_bytes(baseline) == canonical_json_bytes(explicit)
    assert "followup_context_json" not in baseline.model_dump()
    value = '{"history":"' + "曾经确认的用户信息。" * 8000 + '"}'
    updated = ReportContinuationContext.model_validate({
        **baseline.model_dump(), "followup_context_json": value,
    })
    assert updated.followup_context_json == value
    command = forced_command(continuation_context=updated)
    handler, repository, *_ = _handler(_state(), DomainCoordinator(), catalog=TrackingCatalog())
    result = handler.execute(command)
    aggregate = repository.read_case(result.business_receipt.case_id)
    assert aggregate.jobs[aggregate.case.active_job_id].continuation_context == updated


@pytest.mark.parametrize("value", ["", "  \n", "中" * (16 * 1024 * 1024 // 3 + 1)])
def test_followup_context_requires_nonblank_text_within_sixteen_mib_utf8(value):
    with pytest.raises(ValidationError):
        ReportContinuationContext.model_validate({**context().model_dump(), "followup_context_json": value})
