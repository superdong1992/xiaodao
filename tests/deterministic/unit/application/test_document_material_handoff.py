"""Only the current document Skill may request deferred optional materials."""
from __future__ import annotations

import pytest

from problem_locator.contracts import (
    ApplicationPortError, ErrorCode, MarkInitialLogArchiveExpected,
    SpecializedInputPolicy, StateFile, canonical_json_bytes,
)
from problem_locator.domain import DomainCoordinator
from tests.deterministic.contracts.fakes import FakeAssetCatalog
from tests.deterministic.unit.application.test_external_commands import (
    CASE_ID, SOURCE_JOB_ID, _handler, _waiting_state,
)


def _setup(mode="direct_files", *, wrong_source=False, wrong_version=False):
    state = _waiting_state()
    aggregate = state.cases[CASE_ID]
    selected = aggregate.case.selected_skill_ref
    catalog = FakeAssetCatalog()
    policy = (None if mode is None else SpecializedInputPolicy(
        skill_ref=selected.model_copy(update={"content_hash": "f" * 64}) if wrong_version else selected,
        optional_inputs=[], attachment_mode=mode))
    catalog.specialized_input_policy = lambda ref: policy
    source_id = SOURCE_JOB_ID
    if wrong_source:
        source_id = "40000000-0000-0000-0000-000000000001"
        raw = state.model_dump(mode="python")
        raw["cases"][CASE_ID]["jobs"][source_id] = {
            **aggregate.jobs[SOURCE_JOB_ID].model_dump(mode="python"), "job_id": source_id,
        }
        state = StateFile.model_validate(raw)
    handler, repository, _, _, dispatcher, *_ = _handler(state, DomainCoordinator(), catalog=catalog)
    command = MarkInitialLogArchiveExpected(idempotency_key="document-material-selection",
        case_id=CASE_ID, expected_case_revision=aggregate.case.case_revision, source_job_id=source_id)
    return handler, repository, dispatcher, command


def test_waiting_document_material_marker_replays_without_creating_or_dispatching_jobs():
    handler, repository, dispatcher, command = _setup()
    before = repository.read_snapshot().cases[CASE_ID]
    assert not before.case.initial_log_archive_expected
    frozen_jobs = canonical_json_bytes(before.model_dump(mode="json")["jobs"])
    frozen_diagnosis = canonical_json_bytes(before.case.diagnosis_state)

    first = handler.execute(command)
    replay = handler.execute(command)

    after = repository.read_snapshot().cases[CASE_ID]
    assert after.case.initial_log_archive_expected
    assert after.case.status.value == "WAITING_INPUT" and after.case.active_job_id is None
    assert after.case.case_revision == before.case.case_revision + 1
    assert canonical_json_bytes(after.model_dump(mode="json")["jobs"]) == frozen_jobs
    assert canonical_json_bytes(after.case.diagnosis_state) == frozen_diagnosis
    assert first.business_receipt == replay.business_receipt
    assert dispatcher.submit_calls == [] and dispatcher.cancel_calls == []


@pytest.mark.parametrize(("mode", "wrong_source"), [("none", False), (None, False), ("direct_files", True)])
def test_waiting_marker_rejects_none_legacy_and_unrelated_source_without_mutation(mode, wrong_source):
    handler, repository, dispatcher, command = _setup(mode, wrong_source=wrong_source)
    before = canonical_json_bytes(repository.read_snapshot())

    with pytest.raises(ApplicationPortError) as failure:
        handler.execute(command)

    assert failure.value.error.code is ErrorCode.INVALID_CASE_STATE
    assert canonical_json_bytes(repository.read_snapshot()) == before
    assert dispatcher.submit_calls == [] and dispatcher.cancel_calls == []


def test_waiting_marker_cannot_use_another_skill_version_policy():
    handler, repository, dispatcher, command = _setup(wrong_version=True)
    before = canonical_json_bytes(repository.read_snapshot())

    with pytest.raises(ApplicationPortError) as failure:
        handler.execute(command)

    assert failure.value.error.code is ErrorCode.CONFIG_INVALID
    assert canonical_json_bytes(repository.read_snapshot()) == before
    assert dispatcher.submit_calls == [] and dispatcher.cancel_calls == []
