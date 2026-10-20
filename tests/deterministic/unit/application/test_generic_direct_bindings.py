from __future__ import annotations

import pytest

from problem_locator.application.preparation import runtime_bindings_from_job
from problem_locator.contracts import (
    GENERIC_DIRECT_LOG_CONTENT_TYPES, Job, JobOutcome, JobStatus, OutcomeDisposition,
    OutcomeResultType, RouteDecision, RouteKind, RuntimeBindings, StateFile,
    WorkspaceInputManifest, canonical_json_bytes, derive_attachment_content_type,
    derive_attachment_filename_suffix, validate_workspace_manifest_for_job,
)
from problem_locator.domain import DomainCoordinator
from problem_locator.runtime.catalog import VersionedAssetCatalog
from tests.deterministic.contracts.fakes import FakeAssetCatalog, InMemoryExecutionRecordStore
from tests.deterministic.contracts.scenario_fakes import assets_for_bindings
from tests.deterministic.unit.application.test_external_commands import _handler, _attachment_record
from tests.deterministic.unit.application.test_generic_log_handoff import (
    ATTACHMENT_ID, _log_bindings, _restart, _routed_state_with_historical_archives, _with_logs,
)
from tests.deterministic.unit.application.test_outcome_submission import (
    CASE_ID, DIAGNOSE_JOB_ID, _generic_bindings, _load, _running_generic_state, _service,
)


def _direct_bindings():
    return RuntimeBindings.model_validate({
        **_generic_bindings().model_dump(mode="python"), "generic_log_preparation": "direct-v1",
    })


def _direct_state():
    payload = _running_generic_state().model_dump(mode="python")
    payload["cases"][CASE_ID]["jobs"][DIAGNOSE_JOB_ID]["generic_log_preparation"] = "direct-v1"
    return StateFile.model_validate(payload)


@pytest.mark.parametrize("with_logs", [False, True])
def test_generic_catalog_fixes_direct_preparation_without_logparse(tmp_path, with_logs):
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    catalog = VersionedAssetCatalog(skill_dir=skill_dir, generic_skill_name="generic-problem-locator-smoke",
                                    defer_snapshot=True)
    try:
        bindings = catalog.generic_diagnose_bindings(with_logs=with_logs)
        assert bindings.generic_log_preparation == "direct-v1"
        assert bindings.logparse_tool_ref is bindings.logparse_product is None
    finally:
        catalog.close()


def test_missing_preparation_field_preserves_legacy_job_and_binding_bytes():
    job = _running_generic_state().cases[CASE_ID].jobs[DIAGNOSE_JOB_ID]
    explicit = Job.model_validate({**job.model_dump(mode="python"), "generic_log_preparation": None})
    assert canonical_json_bytes(job) == canonical_json_bytes(explicit)
    assert "generic_log_preparation" not in job.model_dump()
    assert "generic_log_preparation" not in runtime_bindings_from_job(job).model_dump()


def test_direct_strategy_is_rejected_on_specialized_job_or_with_logparse():
    with pytest.raises(ValueError, match="GENERIC mode without Logparse"):
        Job.model_validate({**_load("job-diagnose.json"), "generic_log_preparation": "direct-v1"})
    with pytest.raises(ValueError, match="GENERIC mode without Logparse"):
        RuntimeBindings.model_validate({
            **_log_bindings().model_dump(mode="python"), "generic_log_preparation": "direct-v1",
        })


@pytest.mark.parametrize("content_type", sorted(GENERIC_DIRECT_LOG_CONTENT_TYPES))
def test_direct_aggregate_adopts_supported_text_and_archives(content_type):
    payload = _direct_state().model_dump(mode="python")
    aggregate = payload["cases"][CASE_ID]
    name = {"application/zip": "logs.zip", "application/gzip": "logs.gz",
            "application/x-tar": "logs.tar"}.get(content_type, "service.log")
    attachment = _attachment_record(ATTACHMENT_ID).model_copy(update={"name": name, "content_type": content_type})
    aggregate["attachments"][ATTACHMENT_ID] = attachment.model_dump(mode="python")
    aggregate["jobs"][DIAGNOSE_JOB_ID]["attachment_refs"] = [ATTACHMENT_ID]
    assert StateFile.model_validate(payload).cases[CASE_ID].jobs[DIAGNOSE_JOB_ID].attachment_refs == [ATTACHMENT_ID]
    if content_type not in {"application/zip", "application/gzip", "application/x-tar"}:
        aggregate["jobs"][DIAGNOSE_JOB_ID].pop("generic_log_preparation")
        with pytest.raises(ValueError, match="fixed log preparation strategy"):
            StateFile.model_validate(payload)


@pytest.mark.parametrize(("name", "content_type"), [
    ("service.log", "text/plain"), ("service.txt", "text/plain"),
    ("service.json", "application/json"), ("service.jsonl", "application/x-ndjson"),
    ("service.ndjson", "application/x-ndjson"),
])
def test_text_upload_type_is_derived_without_changing_archive_suffix_contract(name, content_type):
    assert derive_attachment_content_type(name) == content_type
    assert derive_attachment_filename_suffix(name, content_type) is None
    with pytest.raises(ValueError, match="lowercase"):
        derive_attachment_content_type(name.upper())


def test_direct_manifest_has_no_parse_only_plan_and_must_match_fixed_strategy():
    job = _direct_state().cases[CASE_ID].jobs[DIAGNOSE_JOB_ID]
    manifest = WorkspaceInputManifest(
        schema_version=2, job_id=job.job_id, case_id=job.case_id, job_type=job.job_type,
        generic_log_preparation="direct-v1", logparse_tool_ref=None, logparse_product=None, entries=[],
    )
    assert validate_workspace_manifest_for_job(manifest, job) == manifest
    with pytest.raises(ValueError, match="generic_log_preparation"):
        validate_workspace_manifest_for_job(manifest.model_copy(update={"generic_log_preparation": None}), job)
    with pytest.raises(ValueError, match="without Logparse"):
        WorkspaceInputManifest.model_validate({
            **manifest.model_dump(mode="python"),
            "logparse_tool_ref": _log_bindings().logparse_tool_ref, "logparse_product": "default",
        })


@pytest.mark.parametrize("source_mode", ["direct", "legacy-text", "legacy-logparse"])
def test_restart_fixes_preparation_and_preserves_legacy_logparse(source_mode):
    state = (_direct_state() if source_mode == "direct" else
             _with_logs(_running_generic_state()) if source_mode == "legacy-logparse" else _running_generic_state())
    source = state.cases[CASE_ID].jobs[DIAGNOSE_JOB_ID]
    catalog = FakeAssetCatalog(generic=_direct_bindings(),
        assets=[*assets_for_bindings(_direct_bindings()), *assets_for_bindings(runtime_bindings_from_job(source))])
    handler, repository, _, _, dispatcher, *_ = _handler(state, DomainCoordinator(), catalog=catalog)
    command = _restart(state)
    first, replay = handler.execute(command), handler.execute(command)
    aggregate = repository.read_snapshot().cases[CASE_ID]
    successor = aggregate.jobs[aggregate.case.active_job_id]
    assert first.business_receipt == replay.business_receipt
    assert len(aggregate.jobs) == 2 and aggregate.jobs[source.job_id].status is JobStatus.CANCELLED
    assert dispatcher.cancel_calls == [source.job_id, source.job_id]
    assert successor.generic_problem_text == source.generic_problem_text
    assert successor.generic_log_archive_expected
    if source_mode == "legacy-logparse":
        assert successor.generic_log_preparation is None
        assert runtime_bindings_from_job(successor) == runtime_bindings_from_job(source)
        assert catalog.generic_calls == 0
    else:
        assert successor.generic_log_preparation == "direct-v1"
        assert successor.logparse_tool_ref is successor.logparse_product is None
        assert catalog.generic_calls == int(source_mode == "legacy-text")


def test_specialized_reroute_adopts_text_in_direct_successor_only():
    state = _routed_state_with_historical_archives(["text/plain", "application/zip"])
    records = InMemoryExecutionRecordStore()
    outcome = JobOutcome.model_validate({**_load("job-outcome-route.json"),
        "result_type": OutcomeResultType.NO_CAPABILITY,
        "payload": RouteDecision(kind=RouteKind.NO_CAPABILITY, skill_ref=None, reason="无专用方法", confidence=0.9)})
    receipt = records.publish_outcome_bytes(outcome.job_id, canonical_json_bytes(outcome))
    catalog = FakeAssetCatalog(generic=_direct_bindings(), assets=assets_for_bindings(_direct_bindings()))
    service, repository, *_ = _service(state, DomainCoordinator(), records, catalog=catalog)
    assert service.submit_outcome(outcome, receipt).disposition is OutcomeDisposition.APPLIED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    successor = aggregate.jobs[aggregate.case.active_job_id]
    assert successor.generic_log_preparation == "direct-v1"
    assert successor.attachment_refs == list(state.cases[CASE_ID].attachments)
    assert successor.logparse_tool_ref is None
    assert service.submit_outcome(outcome, receipt).disposition is OutcomeDisposition.DUPLICATE
