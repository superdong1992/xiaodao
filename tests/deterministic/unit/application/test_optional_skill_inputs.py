"""Optional Skill inputs are accepted only by a pinned declaration while waiting."""
from __future__ import annotations

import pytest

from problem_locator.application.external_commands import ExternalCommandHandler
from problem_locator.contracts import (
    ApplicationPortError, ErrorCode, InputRequirementConstraints, OptionalInputDefinition,
    SpecializedInputPolicy, SubmitSupplement, SupplementPolicy,
    canonical_json_bytes, canonical_json_sha256,
)
from problem_locator.domain import DomainCoordinator
from tests.deterministic.contracts.fakes import FakeAssetCatalog
from tests.deterministic.unit.application.test_external_commands import CASE_ID, SOURCE_JOB_ID, _bindings, _handler, _waiting_state


def _policy(skill_ref, *, name="device_model"):
    return SpecializedInputPolicy(skill_ref=skill_ref, attachment_mode="none", optional_inputs=[
        OptionalInputDefinition(name=name, prompt="设备型号（选填）", constraints=InputRequirementConstraints(
            value_type="STRING", min_utf8_bytes=1, max_utf8_bytes=8, pattern=r"X[0-9]+", allowed_values=["X1", "X2"]))])


def test_optional_fact_is_persisted_without_a_requirement_and_replays_once():
    waiting = _waiting_state()
    waiting.cases[CASE_ID].case.diagnosis_state.pending_requirements[0].supplement_policy = SupplementPolicy.MISSING_ONLY
    aggregate = waiting.cases[CASE_ID]
    outcome = next(iter(aggregate.outcomes.values()))
    record = aggregate.outcome_processing_records[outcome.outcome_id]
    digest = canonical_json_sha256(outcome)
    aggregate.outcome_processing_records[outcome.outcome_id] = record.model_copy(update={
        "outcome_hash": digest, "outcome_file_ref": record.outcome_file_ref.model_copy(update={
            "sha256": digest, "size": len(canonical_json_bytes(outcome))})})
    selected = waiting.cases[CASE_ID].case.selected_skill_ref
    source = waiting.cases[CASE_ID].jobs[SOURCE_JOB_ID]
    catalog = FakeAssetCatalog(diagnose={(selected.id, selected.version, selected.content_hash): _bindings(source)})
    catalog.specialized_input_policy = lambda ref: _policy(ref)
    handler, repository, _, _, dispatcher, _, _, _ = _handler(waiting, DomainCoordinator(), catalog=catalog)
    command = SubmitSupplement(idempotency_key="optional-input", case_id=CASE_ID, expected_case_revision=2,
        inputs={"device_model": "X1"}, attachment_ids=[], wait_seconds=0)

    response = handler.execute(command)
    replay = handler.execute(command)

    case = repository.read_snapshot().cases[CASE_ID].case
    assert case.status.value == "WAITING_INPUT"
    assert case.case_revision == 3
    assert [item.name for item in case.diagnosis_state.pending_requirements] == ["rpc_method"]
    assert case.diagnosis_state.pending_requirements[0].status.value == "OPEN"
    assert [item.statement for item in case.diagnosis_state.user_facts if item.provenance.input_name == "device_model"] == ["X1"]
    assert replay.business_receipt == response.business_receipt
    assert dispatcher.submit_calls == []
    with pytest.raises(ApplicationPortError) as failure:
        handler.execute(command.model_copy(update={"idempotency_key": "overwrite", "expected_case_revision": 3,
            "inputs": {"device_model": "X2"}}))
    assert failure.value.error.code is ErrorCode.NEW_CASE_REQUIRED
    handler.execute(SubmitSupplement(idempotency_key="required-input", case_id=CASE_ID,
        expected_case_revision=3, inputs={"rpc_method": "ReserveStock"}, attachment_ids=[], wait_seconds=0))
    final_case = repository.read_snapshot().cases[CASE_ID].case
    assert final_case.status.value == "RUNNING"
    assert {item.provenance.input_name for item in final_case.diagnosis_state.user_facts} >= {"device_model", "rpc_method"}
    assert len(dispatcher.submit_calls) == 1


@pytest.mark.parametrize("inputs", [{"unknown": "X1"}, {"device_model": "Y1"}, {"device_model": "X3"}, {"device_model": "X" * 9}])
def test_optional_policy_rejects_unknown_values_and_all_constraint_failures(inputs):
    case = _waiting_state().cases[CASE_ID].case
    with pytest.raises(ApplicationPortError) as failure:
        ExternalCommandHandler._validate_supplement_inputs(case, inputs, _policy(case.selected_skill_ref))
    assert failure.value.error.code is ErrorCode.VALIDATION_ERROR


def test_optional_policy_cannot_be_substituted_from_another_skill_version():
    case = _waiting_state().cases[CASE_ID].case
    other = case.selected_skill_ref.model_copy(update={"content_hash": "f" * 64})
    with pytest.raises(ApplicationPortError) as failure:
        ExternalCommandHandler._validate_supplement_inputs(case, {"device_model": "X1"}, _policy(other))
    assert failure.value.error.code is ErrorCode.CONFIG_INVALID
