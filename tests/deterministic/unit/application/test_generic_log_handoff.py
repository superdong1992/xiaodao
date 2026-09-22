from __future__ import annotations

import hashlib

import pytest

from problem_locator.application.preparation import runtime_bindings_from_job
from problem_locator.contracts import (
    ApplicationPortError, Attachment, AttachmentRequirementConstraints, AttachmentStatus,
    CaseStatus, CreateCase, DiagnosisMode, DiagnosisOutcome, GenericResultStatus,
    Job, JobOutcome, JobStatus, MarkInitialLogArchiveExpected, OutcomeDisposition, OutcomeResultType, PendingRequirement,
    RequirementKind, RequirementStatus, RestartGenericDiagnosis, RouteDecision, RouteKind,
    RuntimeBindings, StateFile, SubmitSupplement, SupplementPolicy, canonical_json_bytes,
    business_request_sha256, validate_outcome_for_job,
)
from problem_locator.domain import DomainCoordinator
from tests.deterministic.contracts.fakes import FakeAssetCatalog, InMemoryExecutionRecordStore
from tests.deterministic.contracts.scenario_fakes import assets_for_bindings
from tests.deterministic.unit.application.test_external_commands import _handler, _empty_delta, _attachment_record
from tests.deterministic.unit.application.test_job_control import (
    CURRENT_EPOCH, _service as _control_service,
)
from tests.deterministic.unit.application.test_outcome_submission import (
    CASE_ID, DIAGNOSE_JOB_ID, _generic_bindings, _generic_outcome, _load,
    _running_generic_state, _running_state, _service,
)

ATTACHMENT_ID = "00000000-0000-0000-0000-000000000151"
REQUIREMENT_ID = "00000000-0000-0000-0000-000000000152"


def _log_bindings():
    source = _load("job-diagnose.json")
    return RuntimeBindings.model_validate({**_generic_bindings().model_dump(mode="python"),
        "logparse_tool_ref": source["logparse_tool_ref"], "logparse_product": source["logparse_product"]})


def _with_logs(state):
    payload = state.model_dump(mode="python")
    aggregate = payload["cases"][CASE_ID]
    aggregate["case"]["initial_log_archive_expected"] = True
    job = aggregate["jobs"][DIAGNOSE_JOB_ID]
    logs = _log_bindings()
    job.update(generic_log_archive_expected=True,
        logparse_tool_ref=logs.logparse_tool_ref, logparse_product=logs.logparse_product)
    return StateFile.model_validate(payload)


def _preflight(job, *, requirement_id=REQUIREMENT_ID):
    requirement = PendingRequirement(requirement_id=requirement_id,
        kind=RequirementKind.ATTACHMENT, name="log_archive", prompt="请补充日志归档。", required=True,
        constraints=AttachmentRequirementConstraints(allowed_content_types=["application/gzip", "application/zip", "application/x-tar"],
            min_count=1, max_count=1), status=RequirementStatus.OPEN, requested_by_job_id=job.job_id,
        fulfilled_by_refs=[], supplement_policy=SupplementPolicy.MISSING_ONLY)
    return JobOutcome.model_validate({**_generic_outcome(GenericResultStatus.UNRESOLVED).model_dump(mode="python"),
        "job_id": job.job_id, "base_state_revision": job.base_state_revision,
        "result_type": OutcomeResultType.NEED_ATTACHMENT,
        "payload": DiagnosisOutcome(findings=[], state_delta=_empty_delta(add_pending_requirements=[requirement]),
            requested_input=[], requested_attachments=[requirement_id], candidate_conclusion_draft=None,
            recommended_next_step="请补充日志归档。")})


def _restart(source):
    return RestartGenericDiagnosis(idempotency_key="restart-with-log", case_id=CASE_ID,
        expected_case_revision=source.cases[CASE_ID].case.case_revision,
        source_job_id=source.cases[CASE_ID].case.active_job_id,
        supplement_text="补充：错误发生前服务刚重启。")


def test_create_case_log_intent_defaults_preserve_bytes_and_hash():
    values = dict(idempotency_key="create", raw_problem_text="原始问题",
        problem_spec=_load("state.json")["cases"][CASE_ID]["case"]["diagnosis_state"]["problem_spec"],
        initial_user_facts=[], wait_seconds=0)
    values["problem_spec"].pop("revision")
    legacy = CreateCase(**values)
    explicit_false = CreateCase(**values, initial_log_archive_expected=False)
    selected = CreateCase(**values, initial_log_archive_expected=True)
    assert canonical_json_bytes(legacy) == canonical_json_bytes(explicit_false)
    assert "initial_log_archive_expected" not in legacy.model_dump()
    assert business_request_sha256(legacy) != business_request_sha256(selected)


def test_no_capability_freezes_initial_log_intent_and_logparse_bindings():
    state = _running_state()
    payload = state.model_dump(mode="python")
    payload["cases"][CASE_ID]["case"]["initial_log_archive_expected"] = True
    state = StateFile.model_validate(payload)
    outcome = JobOutcome.model_validate({**_load("job-outcome-route.json"),
        "result_type": OutcomeResultType.NO_CAPABILITY,
        "payload": RouteDecision(kind=RouteKind.NO_CAPABILITY, skill_ref=None, reason="无专用方法", confidence=0.9)})
    records = InMemoryExecutionRecordStore()
    ref = records.publish_outcome_bytes(outcome.job_id, canonical_json_bytes(outcome))
    catalog = FakeAssetCatalog(generic=_log_bindings(), assets=assets_for_bindings(_log_bindings()))
    service, repository, *_ = _service(state, DomainCoordinator(), records, catalog=catalog)
    assert service.submit_outcome(outcome, ref).disposition is OutcomeDisposition.APPLIED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    job = aggregate.jobs[aggregate.case.active_job_id]
    assert job.generic_log_archive_expected and not job.attachment_refs
    assert job.logparse_tool_ref == _log_bindings().logparse_tool_ref
    assert job.context_snapshot is None


def _routed_state_with_historical_archives(content_types):
    payload = _running_state().model_dump(mode="python")
    aggregate = payload["cases"][CASE_ID]
    historical_job = _load("job-diagnose.json")
    historical_job.update(status=JobStatus.SUCCEEDED, attachment_refs=[], evidence_refs=[],
        context_snapshot=aggregate["jobs"][aggregate["case"]["active_job_id"]]["context_snapshot"],
        base_state_revision=aggregate["case"]["diagnosis_state"]["revision"],
        previous_outcome_refs=[], artifact_refs=[], started_at="2026-07-31T00:00:01.000Z",
        finished_at="2026-07-31T00:00:20.000Z", runtime_epoch=CURRENT_EPOCH)
    aggregate["jobs"][historical_job["job_id"]] = historical_job
    for ordinal, content_type in enumerate(content_types):
        attachment_id = f"00000000-0000-0000-0000-{170 + ordinal:012d}"
        archive = _attachment_record(attachment_id).model_copy(update={
            "name": f"history-{ordinal}.zip" if content_type == "application/zip" else f"history-{ordinal}.log",
            "content_type": content_type})
        aggregate["attachments"][attachment_id] = archive.model_dump(mode="python")
        requirement = PendingRequirement(
            requirement_id=f"00000000-0000-0000-0000-{180 + ordinal:012d}",
            kind=RequirementKind.ATTACHMENT, name="log_archive", prompt="补充历史日志。", required=True,
            constraints=AttachmentRequirementConstraints(allowed_content_types=[content_type], min_count=1, max_count=1),
            status=RequirementStatus.FULFILLED, requested_by_job_id=historical_job["job_id"],
            fulfilled_by_refs=[attachment_id], supplement_policy=SupplementPolicy.MISSING_ONLY)
        aggregate["case"]["diagnosis_state"]["pending_requirements"].append(requirement.model_dump(mode="python"))
    return StateFile.model_validate(payload)


@pytest.mark.parametrize(("content_types", "initial_expected"), [
    ([], False), (["application/zip"], False), (["application/zip", "application/zip"], False),
    (["text/plain"], False), (["text/plain", "application/zip"], False),
    (["application/zip", "text/plain", "application/zip"], False), (["text/plain"], True),
], ids=["no-logs", "one-archive", "two-archives", "legacy-text", "mixed-one-archive", "mixed-two-archives", "explicit-intent-legacy-text"])
def test_specialized_history_no_capability_selects_only_an_unambiguous_archive(content_types, initial_expected):
    state = _routed_state_with_historical_archives(content_types)
    if initial_expected:
        payload = state.model_dump(mode="python")
        payload["cases"][CASE_ID]["case"]["initial_log_archive_expected"] = True
        state = StateFile.model_validate(payload)
    archive_ids = [attachment.attachment_id for attachment in state.cases[CASE_ID].attachments.values()
        if attachment.content_type == "application/zip"]
    expected_logs = initial_expected or bool(archive_ids)
    records = InMemoryExecutionRecordStore()
    outcome = JobOutcome.model_validate({**_load("job-outcome-route.json"),
        "result_type": OutcomeResultType.NO_CAPABILITY,
        "payload": RouteDecision(kind=RouteKind.NO_CAPABILITY, skill_ref=None, reason="重新路由未命中专用方法", confidence=0.9)})
    ref = records.publish_outcome_bytes(outcome.job_id, canonical_json_bytes(outcome))
    bindings = _log_bindings() if expected_logs else _generic_bindings()
    catalog = FakeAssetCatalog(generic=bindings, assets=assets_for_bindings(bindings))
    service, repository, *_ = _service(state, DomainCoordinator(), records, catalog=catalog)
    assert service.submit_outcome(outcome, ref).disposition is OutcomeDisposition.APPLIED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    job = aggregate.jobs[aggregate.case.active_job_id]
    assert job.generic_log_archive_expected is expected_logs
    assert job.attachment_refs == (archive_ids if len(archive_ids) == 1 else [])
    assert job.logparse_tool_ref == bindings.logparse_tool_ref
    assert job.previous_outcome_refs == job.evidence_refs == job.artifact_refs == []
    assert aggregate.case.failure is None
    assert service.submit_outcome(outcome, ref).disposition is OutcomeDisposition.DUPLICATE

    if not expected_logs or len(archive_ids) == 1:
        return
    control, *_ = _control_service(repository.read_snapshot(), DomainCoordinator(), repository=repository, catalog=catalog)
    assert control.claim_job(job.job_id, CURRENT_EPOCH).claimed
    claimed = repository.read_snapshot().cases[CASE_ID].jobs[job.job_id]
    preflight = _preflight(claimed).model_copy(update={"produced_at": "2026-07-31T00:11:00.000Z"})
    preflight_ref = records.publish_outcome_bytes(job.job_id, canonical_json_bytes(preflight))
    assert service.submit_outcome(preflight, preflight_ref).disposition is OutcomeDisposition.APPLIED
    waiting = repository.read_snapshot()
    assert waiting.cases[CASE_ID].case.status is CaseStatus.WAITING_ATTACHMENT
    if not archive_ids:
        return
    selected = archive_ids[1]
    handler, *_ = _handler(waiting, DomainCoordinator(), repository=repository, catalog=catalog)
    response = handler.execute(SubmitSupplement(idempotency_key="select-one-historical-archive", case_id=CASE_ID,
        expected_case_revision=waiting.cases[CASE_ID].case.case_revision,
        inputs={}, attachment_ids=[selected], wait_seconds=0))
    continued = repository.read_snapshot().cases[CASE_ID].jobs[response.business_receipt.job_id]
    assert continued.attachment_refs == [selected]
    assert runtime_bindings_from_job(continued) == runtime_bindings_from_job(job)
    assert len(repository.read_snapshot().cases[CASE_ID].attachments) == len(content_types)


def test_specialized_supplement_still_preserves_multiple_plain_text_attachments():
    from tests.deterministic.unit.application.test_external_commands import (
        SOURCE_JOB_ID, WAIT_OUTCOME_ID, _waiting_attachment_state,
    )

    attachment_ids = [f"00000000-0000-0000-0000-{190 + index:012d}" for index in range(3)]
    state = _waiting_attachment_state([_attachment_record(item) for item in attachment_ids],
        source_attachment_refs=[attachment_ids[0]])
    payload = state.model_dump(mode="python")
    for requirement in payload["cases"][CASE_ID]["case"]["diagnosis_state"]["pending_requirements"]:
        requirement["supplement_policy"] = SupplementPolicy.MISSING_ONLY
    state = StateFile.model_validate(payload)
    aggregate = state.cases[CASE_ID]
    source = aggregate.jobs[SOURCE_JOB_ID]
    selected = aggregate.case.selected_skill_ref
    catalog = FakeAssetCatalog(diagnose={
        (selected.id, selected.version, selected.content_hash): runtime_bindings_from_job(source)})
    handler, repository, *_ = _handler(state, DomainCoordinator(), catalog=catalog)
    response = handler.execute(SubmitSupplement(idempotency_key="specialized-multiple-text-attachments",
        case_id=CASE_ID, expected_case_revision=aggregate.case.case_revision,
        inputs={"rpc_method": "ReserveStock"}, attachment_ids=attachment_ids, wait_seconds=0))
    stored = repository.read_snapshot().cases[CASE_ID]
    job = stored.jobs[response.business_receipt.job_id]
    assert stored.case.status is CaseStatus.RUNNING
    assert job.diagnosis_mode is DiagnosisMode.SPECIALIZED
    assert job.attachment_refs == attachment_ids
    assert job.previous_outcome_refs == [WAIT_OUTCOME_ID]
    assert job.skill_ref == selected
    assert runtime_bindings_from_job(job) == runtime_bindings_from_job(source)
    assert not job.generic_log_archive_expected and not job.generic_supplement_texts


def test_generic_preflight_waits_then_supplement_freezes_selected_archive_and_text():
    state = _with_logs(_running_generic_state())
    records = InMemoryExecutionRecordStore()
    job = state.cases[CASE_ID].jobs[DIAGNOSE_JOB_ID]
    outcome = _preflight(job)
    ref = records.publish_outcome_bytes(job.job_id, canonical_json_bytes(outcome))
    service, repository, *_ = _service(state, DomainCoordinator(), records)
    assert service.submit_outcome(outcome, ref).disposition is OutcomeDisposition.APPLIED
    waiting = repository.read_snapshot()
    assert waiting.cases[CASE_ID].case.status is CaseStatus.WAITING_ATTACHMENT
    payload = waiting.model_dump(mode="python")
    payload["cases"][CASE_ID]["attachments"][ATTACHMENT_ID] = Attachment(
        attachment_id=ATTACHMENT_ID, case_id=CASE_ID, status=AttachmentStatus.READY,
        name="product.zip", content_type="application/zip", declared_size=3,
        declared_sha256=hashlib.sha256(b"log").hexdigest(), size=3,
        sha256=hashlib.sha256(b"log").hexdigest(),
        storage_key=f"resources/cases/{CASE_ID}/attachments/{ATTACHMENT_ID}/payload",
        created_at=outcome.produced_at, updated_at=outcome.produced_at)
    waiting = StateFile.model_validate(payload)
    catalog = FakeAssetCatalog(assets=assets_for_bindings(_log_bindings()))
    handler, repository, *_ = _handler(waiting, DomainCoordinator(), catalog=catalog)
    command = SubmitSupplement(idempotency_key="provide-log", case_id=CASE_ID,
        expected_case_revision=waiting.cases[CASE_ID].case.case_revision,
        inputs={}, attachment_ids=[ATTACHMENT_ID], generic_supplement_text="错误发生前刚重启。", wait_seconds=0)
    result = handler.execute(command)
    aggregate = repository.read_snapshot().cases[CASE_ID]
    continued = aggregate.jobs[result.business_receipt.job_id]
    assert continued.attachment_refs == [ATTACHMENT_ID]
    assert continued.generic_supplement_texts == ["错误发生前刚重启。"]
    assert continued.generic_problem_text == job.generic_problem_text
    assert runtime_bindings_from_job(continued) == runtime_bindings_from_job(job)
    assert continued.previous_outcome_refs == continued.evidence_refs == continued.artifact_refs == []
    assert catalog.generic_calls == 0


@pytest.mark.parametrize("unexpected", ["without-intent", "input-request", "changed-format", "wrong-source"])
def test_generic_preflight_rejects_non_product_or_unsolicited_requirements(unexpected):
    state = _with_logs(_running_generic_state())
    job = state.cases[CASE_ID].jobs[DIAGNOSE_JOB_ID]
    outcome = _preflight(job)
    if unexpected == "without-intent":
        job = Job.model_validate({**job.model_dump(mode="python"), "generic_log_archive_expected": False})
    else:
        requirement = outcome.payload.state_delta.add_pending_requirements[0]
        if unexpected == "changed-format":
            requirement.constraints.allowed_content_types = ["text/plain"]
        elif unexpected == "wrong-source":
            requirement.requested_by_job_id = ATTACHMENT_ID
        else:
            outcome.payload.requested_input.append(REQUIREMENT_ID)
    with pytest.raises(ValueError):
        validate_outcome_for_job(job, outcome)


def test_restart_cancels_previous_job_keeps_case_and_replays_once():
    state = _running_generic_state()
    records = InMemoryExecutionRecordStore()
    catalog = FakeAssetCatalog(generic=_log_bindings(), assets=assets_for_bindings(_log_bindings()))
    handler, repository, _, _, dispatcher, *_ = _handler(state, DomainCoordinator(), catalog=catalog, execution_records=records)
    command = _restart(state)
    first = handler.execute(command)
    second = handler.execute(command)
    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert first.business_receipt == second.business_receipt
    assert len(aggregate.jobs) == 2
    assert aggregate.case.case_id == CASE_ID and aggregate.case.status is CaseStatus.RUNNING
    assert aggregate.jobs[DIAGNOSE_JOB_ID].status is JobStatus.CANCELLED
    new = aggregate.jobs[aggregate.case.active_job_id]
    assert new.generic_log_archive_expected and not new.attachment_refs
    assert new.generic_supplement_texts == [command.supplement_text]
    assert new.replacement_for_job_id is None
    assert dispatcher.cancel_calls == [DIAGNOSE_JOB_ID, DIAGNOSE_JOB_ID]
    outcome = _generic_outcome(GenericResultStatus.RESOLVED)
    ref = records.publish_outcome_bytes(outcome.job_id, canonical_json_bytes(outcome))
    service, *_ = _service(repository, DomainCoordinator(), records)
    assert service.submit_outcome(outcome, ref).disposition is OutcomeDisposition.STALE
    assert repository.read_snapshot().cases[CASE_ID].case.active_job_id == new.job_id


def test_restart_result_won_race_rejects_without_new_job():
    state = _running_generic_state()
    records = InMemoryExecutionRecordStore()
    outcome = _generic_outcome(GenericResultStatus.RESOLVED)
    ref = records.publish_outcome_bytes(outcome.job_id, canonical_json_bytes(outcome))
    service, repository, *_ = _service(state, DomainCoordinator(), records)
    service.submit_outcome(outcome, ref)
    handler, *_ = _handler(repository.read_snapshot(), DomainCoordinator(), repository=repository)
    with pytest.raises(ApplicationPortError):
        handler.execute(_restart(state))
    assert len(repository.read_snapshot().cases[CASE_ID].jobs) == 1


def test_restart_cancel_failure_defers_new_dispatch_and_replay_retries_cancel():
    state = _running_generic_state()
    catalog = FakeAssetCatalog(generic=_log_bindings(), assets=assets_for_bindings(_log_bindings()))
    handler, repository, _, _, dispatcher, *_ = _handler(state, DomainCoordinator(), catalog=catalog)
    original = dispatcher.cancel
    def failing_cancel(job_id):
        raise RuntimeError("cancel unavailable")
    dispatcher.cancel = failing_cancel
    command = _restart(state)
    assert handler.execute(command).dispatch_pending
    assert handler.execute(command).dispatch_pending
    assert dispatcher.submit_calls == []
    dispatcher.cancel = original
    result = handler.execute(command)
    assert not result.dispatch_pending
    assert dispatcher.submit_calls == [result.business_receipt.job_id]
    assert len(repository.read_snapshot().cases[CASE_ID].jobs) == 2


def test_restart_rebases_claim_revision_without_changing_source_or_request_hash():
    payload = _running_generic_state().model_dump(mode="python")
    payload["cases"][CASE_ID]["jobs"][DIAGNOSE_JOB_ID].update(
        status=JobStatus.PENDING, started_at=None, runtime_epoch=None)
    state = StateFile.model_validate(payload)
    command = _restart(state)
    fingerprint = business_request_sha256(command)
    control, repository, *_ = _control_service(state, DomainCoordinator(),
        catalog=FakeAssetCatalog(assets=assets_for_bindings(_generic_bindings())))
    assert control.claim_job(command.source_job_id, CURRENT_EPOCH).claimed
    claimed = repository.read_snapshot()
    assert claimed.cases[CASE_ID].case.case_revision == command.expected_case_revision + 1
    catalog = FakeAssetCatalog(generic=_log_bindings(), assets=assets_for_bindings(_log_bindings()))
    handler, *_ = _handler(claimed, DomainCoordinator(), repository=repository, catalog=catalog)
    result = handler.execute(command)
    assert result.business_receipt.case_revision == command.expected_case_revision + 2
    assert business_request_sha256(command) == fingerprint
    assert repository.read_snapshot().cases[CASE_ID].jobs[command.source_job_id].status is JobStatus.CANCELLED
    other = command.model_copy(update={"idempotency_key": "another-message"})
    with pytest.raises(ApplicationPortError):
        handler.execute(other)


def test_route_log_intent_is_idempotent_and_rebases_claim_without_redispatch():
    state = StateFile.model_validate(_load("state.json"))
    route = state.cases[CASE_ID].jobs[state.cases[CASE_ID].case.active_job_id]
    command = MarkInitialLogArchiveExpected(idempotency_key="route-log-intent", case_id=CASE_ID,
        expected_case_revision=state.cases[CASE_ID].case.case_revision, source_job_id=route.job_id)
    control, repository, *_ = _control_service(state, DomainCoordinator())
    assert control.claim_job(route.job_id, CURRENT_EPOCH).claimed
    claimed = repository.read_snapshot()
    handler, _, _, _, dispatcher, *_ = _handler(claimed, DomainCoordinator(), repository=repository)
    first = handler.execute(command)
    second = handler.execute(command)
    assert first.business_receipt == second.business_receipt
    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert aggregate.case.initial_log_archive_expected
    assert aggregate.case.case_revision == command.expected_case_revision + 2
    assert len(aggregate.jobs) == 1
    assert canonical_json_bytes(aggregate.jobs[route.job_id]) == canonical_json_bytes(claimed.cases[CASE_ID].jobs[route.job_id])
    assert dispatcher.cancel_calls == dispatcher.submit_calls == []
    outcome = JobOutcome.model_validate({**_load("job-outcome-route.json"),
        "result_type": OutcomeResultType.NO_CAPABILITY,
        "payload": RouteDecision(kind=RouteKind.NO_CAPABILITY, skill_ref=None, reason="无专用方法", confidence=0.9)})
    records = InMemoryExecutionRecordStore()
    ref = records.publish_outcome_bytes(outcome.job_id, canonical_json_bytes(outcome))
    catalog = FakeAssetCatalog(generic=_log_bindings(), assets=assets_for_bindings(_log_bindings()))
    service, *_ = _service(repository, DomainCoordinator(), records, catalog=catalog)
    assert service.submit_outcome(outcome, ref).disposition is OutcomeDisposition.APPLIED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert aggregate.jobs[aggregate.case.active_job_id].generic_log_archive_expected
    with pytest.raises(ApplicationPortError):
        handler.execute(command.model_copy(update={"idempotency_key": "late-route-log"}))


def test_route_log_intent_racing_outcome_does_not_recover_text_only_successor():
    state = _running_state()
    aggregate = state.cases[CASE_ID]
    command = MarkInitialLogArchiveExpected(idempotency_key="racing-route-log", case_id=CASE_ID,
        expected_case_revision=aggregate.case.case_revision, source_job_id=aggregate.case.active_job_id)
    outcome = JobOutcome.model_validate({**_load("job-outcome-route.json"),
        "result_type": OutcomeResultType.NO_CAPABILITY,
        "payload": RouteDecision(kind=RouteKind.NO_CAPABILITY, skill_ref=None, reason="无专用方法", confidence=0.9)})
    records = InMemoryExecutionRecordStore()
    ref = records.publish_outcome_bytes(outcome.job_id, canonical_json_bytes(outcome))

    class RacingCatalog(FakeAssetCatalog):
        accept_log = None

        def generic_diagnose_bindings(self, *, with_logs=False):
            if self.accept_log is not None:
                accept_log, self.accept_log = self.accept_log, None
                accept_log()
            return _log_bindings() if with_logs else _generic_bindings()

    catalog = RacingCatalog(assets=assets_for_bindings(_log_bindings()))
    service, repository, *_ = _service(state, DomainCoordinator(), records, catalog=catalog)
    handler, *_ = _handler(state, DomainCoordinator(), repository=repository)
    catalog.accept_log = lambda: handler.execute(command)
    assert service.submit_outcome(outcome, ref).disposition is OutcomeDisposition.APPLIED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    job = aggregate.jobs[aggregate.case.active_job_id]
    assert job.generic_log_archive_expected
    assert job.logparse_tool_ref == _log_bindings().logparse_tool_ref
    assert len(records.publish_job_calls) == 2
    old, new = records.publish_job_calls
    assert old.job_id != new.job_id
    assert not old.generic_log_archive_expected and new.generic_log_archive_expected
    assert old.job_id not in aggregate.jobs
