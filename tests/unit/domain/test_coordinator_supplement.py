from __future__ import annotations

import pytest

from problem_locator.contracts import (
    ApplicationError,
    AttachmentRequirementConstraints,
    CaseStatus,
    DiagnosisItem,
    DiagnosisItemStatus,
    DiagnosisProvenance,
    DiagnosisProvenanceType,
    InputRequirementConstraints,
    JobType,
    PendingRequirement,
    RequirementKind,
    RequirementStatus,
    SubmitSupplementTriggerPayload,
    TriggerType,
    canonical_json_bytes,
    validate_coordinator_plan_result,
)
from problem_locator.domain import DomainCoordinator

from ._builders import (
    TRIGGER_ID,
    continuation,
    diagnose_job,
    rebuild,
    runtime_bindings,
    state_from_job,
    trigger,
    waiting_snapshot,
)


REQ_A = "00000000-0000-0000-0000-000000000034"
REQ_B = "00000000-0000-0000-0000-000000000035"
FACT_A = "00000000-0000-0000-0000-000000000036"
FACT_B = "00000000-0000-0000-0000-000000000037"
WAIT_OUTCOME = "00000000-0000-0000-0000-000000000038"
REQ_ATTACHMENT = "00000000-0000-0000-0000-000000000039"


def _requirement(requirement_id: str, name: str) -> PendingRequirement:
    return PendingRequirement(
        requirement_id=requirement_id,
        kind=RequirementKind.INPUT,
        name=name,
        prompt=f"Provide {name}.",
        required=True,
        constraints=InputRequirementConstraints(
            value_type="STRING",
            min_utf8_bytes=1,
            max_utf8_bytes=128,
            pattern=None,
            allowed_values=[],
        ),
        status=RequirementStatus.OPEN,
        requested_by_job_id=diagnose_job().job_id,
        fulfilled_by_refs=[],
    )


def _attachment_requirement() -> PendingRequirement:
    return PendingRequirement(
        requirement_id=REQ_ATTACHMENT,
        kind=RequirementKind.ATTACHMENT,
        name="log_archive",
        prompt="Attach the fixed log archive.",
        required=True,
        constraints=AttachmentRequirementConstraints(
            allowed_content_types=["application/gzip"],
            min_count=1,
            max_count=1,
        ),
        status=RequirementStatus.OPEN,
        requested_by_job_id=diagnose_job().job_id,
        fulfilled_by_refs=[],
    )


def _fact(item_id: str, name: str, value: str, revision: int) -> DiagnosisItem:
    return DiagnosisItem(
        item_id=item_id,
        statement=value,
        status=DiagnosisItemStatus.ACTIVE,
        provenance=DiagnosisProvenance(
            source_type=DiagnosisProvenanceType.USER_INPUT,
            source_ref=TRIGGER_ID,
            input_name=name,
        ),
        evidence_refs=[],
        created_revision=revision,
        supersedes=[],
    )


def test_partial_input_is_persisted_without_creating_a_job() -> None:
    source = diagnose_job()
    base_state = state_from_job(source)
    state = rebuild(
        base_state,
        pending_requirements=[
            _requirement(REQ_A, "caller_service"),
            _requirement(REQ_B, "rpc_method"),
        ],
    )
    snapshot = waiting_snapshot(state, status="WAITING_INPUT")
    fact = _fact(FACT_A, "caller_service", "payment-service", state.revision + 1)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.SUBMIT_SUPPLEMENT,
        payload=SubmitSupplementTriggerPayload(
            user_facts=[fact],
            ready_attachment_ids=[],
            stable_target_changed=False,
        ),
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status.value == "WAITING_INPUT"
    assert plan.next_job_spec is None
    assert plan.accepted_state_delta.add_user_facts == [fact]
    assert [
        item.requirement_id
        for item in plan.accepted_state_delta.fulfill_requirements
    ] == [REQ_A]
    assert plan.clear_active_job is False


def test_last_required_input_creates_exactly_one_diagnosis_job() -> None:
    source = diagnose_job()
    base_state = state_from_job(source)
    requirement = _requirement(REQ_B, "order_id")
    state = rebuild(base_state, pending_requirements=[requirement])
    snapshot = waiting_snapshot(state, status="WAITING_INPUT")
    fact = _fact(FACT_B, "order_id", "order-42", state.revision + 1)
    resources = continuation(incoming_outcome_id=WAIT_OUTCOME, job=source)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.SUBMIT_SUPPLEMENT,
        payload=SubmitSupplementTriggerPayload(
            user_facts=[fact],
            ready_attachment_ids=[],
            stable_target_changed=False,
        ),
        bindings={JobType.DIAGNOSE: runtime_bindings(source)},
        continuation_resources=resources,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status.value == "RUNNING"
    assert plan.next_job_spec is not None
    assert plan.next_job_spec.job_type is JobType.DIAGNOSE
    assert plan.next_job_spec.target_state_revision == state.revision + 1
    assert plan.next_job_spec.attachment_refs == resources.attachment_refs
    assert plan.next_job_spec.previous_outcome_refs == resources.previous_outcome_refs
    assert [
        binding.existing_resource_id for binding in plan.next_job_spec.evidence_bindings
    ] == resources.evidence_refs
    assert [
        binding.existing_resource_id for binding in plan.next_job_spec.artifact_bindings
    ] == resources.artifact_refs


def test_input_completion_moves_directly_to_waiting_attachment() -> None:
    source = diagnose_job()
    base_state = state_from_job(source)
    state = rebuild(
        base_state,
        pending_requirements=[
            _requirement(REQ_A, "caller_service"),
            _attachment_requirement(),
        ],
    )
    snapshot = waiting_snapshot(state, status=CaseStatus.WAITING_INPUT)
    fact = _fact(FACT_A, "caller_service", "payment-service", state.revision + 1)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.SUBMIT_SUPPLEMENT,
        payload=SubmitSupplementTriggerPayload(
            user_facts=[fact],
            ready_attachment_ids=[],
            stable_target_changed=False,
        ),
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is CaseStatus.WAITING_ATTACHMENT
    assert plan.next_job_spec is None
    assert [
        item.requirement_id
        for item in plan.accepted_state_delta.fulfill_requirements
    ] == [REQ_A]


def test_ready_attachment_completes_wait_and_creates_one_diagnosis_job() -> None:
    source = diagnose_job()
    state = rebuild(
        state_from_job(source),
        pending_requirements=[_attachment_requirement()],
    )
    snapshot = waiting_snapshot(state, status=CaseStatus.WAITING_ATTACHMENT)
    attachment_id = source.attachment_refs[0]
    resources = continuation(incoming_outcome_id=WAIT_OUTCOME, job=source)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.SUBMIT_SUPPLEMENT,
        payload=SubmitSupplementTriggerPayload(
            user_facts=[],
            ready_attachment_ids=[attachment_id],
            stable_target_changed=False,
        ),
        bindings={JobType.DIAGNOSE: runtime_bindings(source)},
        continuation_resources=resources,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is CaseStatus.RUNNING
    assert plan.next_job_spec is not None
    assert plan.next_job_spec.job_type is JobType.DIAGNOSE
    assert plan.next_job_spec.attachment_refs == resources.attachment_refs
    assert plan.accepted_state_delta.fulfill_requirements[0].fulfilled_by_refs == [
        attachment_id
    ]


@pytest.mark.parametrize(
    "status",
    [CaseStatus.WAITING_INPUT, CaseStatus.WAITING_ATTACHMENT],
)
def test_stable_target_change_returns_only_new_case_required(
    status: CaseStatus,
) -> None:
    source = diagnose_job()
    state = rebuild(
        state_from_job(source),
        pending_requirements=[
            _requirement(REQ_A, "caller_service")
            if status is CaseStatus.WAITING_INPUT
            else _attachment_requirement()
        ],
    )
    snapshot = waiting_snapshot(state, status=status)
    fact = _fact(FACT_A, "caller_service", "payment-service", state.revision + 1)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.SUBMIT_SUPPLEMENT,
        payload=SubmitSupplementTriggerPayload(
            user_facts=[fact] if status is CaseStatus.WAITING_INPUT else [],
            ready_attachment_ids=(
                []
                if status is CaseStatus.WAITING_INPUT
                else [source.attachment_refs[0]]
            ),
            stable_target_changed=True,
        ),
    )
    before_snapshot = canonical_json_bytes(snapshot)
    before_trigger = canonical_json_bytes(request)

    first = DomainCoordinator().plan(snapshot, request)
    second = DomainCoordinator().plan(snapshot, request)

    assert isinstance(first, ApplicationError)
    assert first.code.value == "NEW_CASE_REQUIRED"
    assert first.retryable is False
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert validate_coordinator_plan_result(request, first) is first
    assert canonical_json_bytes(snapshot) == before_snapshot
    assert canonical_json_bytes(request) == before_trigger


@pytest.mark.parametrize(
    ("constraints", "value"),
    [
        (
            InputRequirementConstraints(
                value_type="STRING",
                min_utf8_bytes=5,
                max_utf8_bytes=8,
                pattern=None,
                allowed_values=[],
            ),
            "x",
        ),
        (
            InputRequirementConstraints(
                value_type="STRING",
                min_utf8_bytes=1,
                max_utf8_bytes=8,
                pattern="^[a-z]+$",
                allowed_values=[],
            ),
            "ABC",
        ),
        (
            InputRequirementConstraints(
                value_type="STRING",
                min_utf8_bytes=1,
                max_utf8_bytes=8,
                pattern=None,
                allowed_values=["valid"],
            ),
            "other",
        ),
    ],
)
def test_input_constraints_reject_the_entire_supplement(
    constraints: InputRequirementConstraints,
    value: str,
) -> None:
    source = diagnose_job()
    requirement = rebuild(
        _requirement(REQ_A, "caller_service"),
        constraints=constraints,
    )
    state = rebuild(
        state_from_job(source),
        pending_requirements=[requirement],
    )
    snapshot = waiting_snapshot(state, CaseStatus.WAITING_INPUT)
    fact = _fact(FACT_A, "caller_service", value, state.revision + 1)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.SUBMIT_SUPPLEMENT,
        payload=SubmitSupplementTriggerPayload(
            user_facts=[fact],
            ready_attachment_ids=[],
            stable_target_changed=False,
        ),
    )

    result = DomainCoordinator().plan(snapshot, request)

    assert isinstance(result, ApplicationError)
    assert result.code.value == "VALIDATION_ERROR"


def test_supplement_fact_id_must_be_new_across_the_diagnosis_state() -> None:
    source = diagnose_job()
    base_state = state_from_job(source)
    existing = _fact(
        FACT_A,
        "existing_input",
        "existing-value",
        base_state.revision,
    )
    state = rebuild(
        base_state,
        user_facts=[existing],
        pending_requirements=[_requirement(REQ_A, "caller_service")],
    )
    snapshot = waiting_snapshot(state, CaseStatus.WAITING_INPUT)
    collision = _fact(
        existing.item_id,
        "caller_service",
        "payment-service",
        state.revision + 1,
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.SUBMIT_SUPPLEMENT,
        payload=SubmitSupplementTriggerPayload(
            user_facts=[collision],
            ready_attachment_ids=[],
            stable_target_changed=False,
        ),
    )

    result = DomainCoordinator().plan(snapshot, request)

    assert isinstance(result, ApplicationError)
    assert result.code.value == "VALIDATION_ERROR"


def test_attachment_count_constraints_reject_the_entire_supplement() -> None:
    source = diagnose_job()
    state = rebuild(
        state_from_job(source),
        pending_requirements=[_attachment_requirement()],
    )
    snapshot = waiting_snapshot(state, CaseStatus.WAITING_ATTACHMENT)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.SUBMIT_SUPPLEMENT,
        payload=SubmitSupplementTriggerPayload(
            user_facts=[],
            ready_attachment_ids=[
                source.attachment_refs[0],
                "00000000-0000-0000-0000-000000000099",
            ],
            stable_target_changed=False,
        ),
    )

    result = DomainCoordinator().plan(snapshot, request)

    assert isinstance(result, ApplicationError)
    assert result.code.value == "VALIDATION_ERROR"


def test_supplement_outside_current_requirements_is_rejected_atomically() -> None:
    source = diagnose_job()
    state = rebuild(
        state_from_job(source),
        pending_requirements=[_requirement(REQ_A, "caller_service")],
    )
    snapshot = waiting_snapshot(state, status="WAITING_INPUT")
    fact = _fact(FACT_A, "rpc_method", "GetInventory", state.revision + 1)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.SUBMIT_SUPPLEMENT,
        payload=SubmitSupplementTriggerPayload(
            user_facts=[fact],
            ready_attachment_ids=[],
            stable_target_changed=False,
        ),
    )

    result = DomainCoordinator().plan(snapshot, request)

    assert isinstance(result, ApplicationError)
    assert result.code.value == "VALIDATION_ERROR"
    assert result.retryable is False
