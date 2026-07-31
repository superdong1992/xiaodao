from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, get_args, get_type_hints

import pytest
from pydantic import ValidationError

from problem_locator.contracts import errors, ports
from problem_locator.contracts.enums import ErrorCode, OutcomeDisposition
from problem_locator.contracts.limits import CONTRACT_REVISION, MAX_ATTACHMENT_BYTES
from problem_locator.contracts.models import (
    ApplicationError,
    CaseAggregate,
    OutcomeProcessingRecord,
    PlannedResourceTarget,
    RecoveryReceipt,
    RuntimeEpochRecord,
    StateFile,
    StateMutation,
    StagedResourceRef,
    SubmitSupplement,
    SubmitSupplementTriggerPayload,
    UploadAttachmentContent,
    PrepareAttachment,
)
from tests.contracts.fakes import InMemoryBinaryStream


ROOT = Path(__file__).resolve().parents[3]
BLOCKER_FIXTURE = (
    ROOT / "tests/fixtures/components/application/contract-blockers.json"
)
CONTRACT_STATE_FIXTURE = ROOT / "tests/fixtures/contracts/positive/state.json"

CASE_ID = "00000000-0000-0000-0000-000000000001"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000020"
REJECTED_OUTCOME_ID = "00000000-0000-0000-0000-000000000099"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _blocker_fixture() -> dict[str, Any]:
    return json.loads(BLOCKER_FIXTURE.read_text(encoding="utf-8"))


def _observations() -> dict[str, Any]:
    observations = _blocker_fixture()["observations"]
    assert isinstance(observations, dict)
    return observations


def _contains_type(annotation: object, target: type[object]) -> bool:
    if annotation is target:
        return True
    return any(_contains_type(argument, target) for argument in get_args(annotation))


def _methods_accepting(protocol: type[object], target: type[object]) -> list[str]:
    consumers: list[str] = []
    for name, member in protocol.__dict__.items():
        if not inspect.isfunction(member) or name == "__init__":
            continue
        hints = get_type_hints(member)
        if any(
            parameter != "return" and _contains_type(annotation, target)
            for parameter, annotation in hints.items()
        ):
            consumers.append(name)
    return sorted(consumers)


def _port_members_returning(target: type[object]) -> list[str]:
    producers: list[str] = []
    for protocol_name in ports.__all__:
        protocol = getattr(ports, protocol_name)
        if not inspect.isclass(protocol) or not getattr(protocol, "_is_protocol", False):
            continue
        for member_name, member in protocol.__dict__.items():
            if not inspect.isfunction(member) or member_name == "__init__":
                continue
            return_type = get_type_hints(member).get("return")
            if return_type is not None and _contains_type(return_type, target):
                producers.append(f"{protocol_name}.{member_name}")
    return sorted(producers)


def _validation_error_type(error: ValidationError, location: tuple[str, ...]) -> str:
    matching = [
        item["type"]
        for item in error.errors(include_url=False)
        if item["loc"] == location
    ]
    assert len(matching) == 1
    return matching[0]


def test_fixture_records_three_known_revisions_and_five_new_contract_gaps() -> None:
    fixture = _blocker_fixture()

    assert fixture["schema_version"] == 1
    assert fixture["owner_spec"] == "S03"
    assert fixture["contract_revision"] == CONTRACT_REVISION == "v1-contract-r1"
    assert [item["id"] for item in fixture["blockers"]] == [
        "error_carrier",
        "stable_target_changed",
        "expected_content_type",
        "staged_preflight",
        "recovery_receipt_persistence",
        "rejected_processing_without_valid_outcome",
        "prepare_size_classification",
        "resource_target_key_derivation",
    ]
    assert [
        item["id"]
        for item in fixture["blockers"]
        if item["classification"] == "known_revision"
    ] == ["error_carrier", "stable_target_changed", "expected_content_type"]
    assert [
        item["id"]
        for item in fixture["blockers"]
        if item["classification"] == "new_contract_gap"
    ] == [
        "staged_preflight",
        "recovery_receipt_persistence",
        "rejected_processing_without_valid_outcome",
        "prepare_size_classification",
        "resource_target_key_derivation",
    ]


def test_public_ports_have_no_general_typed_application_error_carrier() -> None:
    public_exception_types = sorted(
        name
        for name in errors.__all__
        if inspect.isclass(candidate := getattr(errors, name))
        and issubclass(candidate, BaseException)
    )

    assert public_exception_types == _observations()["public_exception_types"]
    assert public_exception_types == ["RuntimeInfrastructureError"]
    assert not issubclass(ApplicationError, BaseException)
    assert "application_error" not in get_type_hints(errors.RuntimeInfrastructureError)


def test_submit_supplement_cannot_carry_the_stable_target_decision() -> None:
    observations = _observations()

    assert list(SubmitSupplement.model_fields) == observations[
        "submit_supplement_fields"
    ]
    assert list(SubmitSupplementTriggerPayload.model_fields) == observations[
        "submit_supplement_trigger_payload_fields"
    ]

    with pytest.raises(ValidationError) as command_error:
        SubmitSupplement.model_validate(
            {
                "idempotency_key": "supplement-1",
                "case_id": CASE_ID,
                "expected_case_revision": 1,
                "inputs": {"order_id": "order-1"},
                "attachment_ids": [],
                "wait_seconds": 0,
                "stable_target_changed": False,
            }
        )
    assert _validation_error_type(
        command_error.value, ("stable_target_changed",)
    ) == "extra_forbidden"

    with pytest.raises(ValidationError) as trigger_error:
        SubmitSupplementTriggerPayload.model_validate(
            {
                "user_facts": [],
                "ready_attachment_ids": [ATTACHMENT_ID],
                "stable_target_changed": False,
            }
        )
    assert _validation_error_type(
        trigger_error.value, ("stable_target_changed",)
    ) == "extra_forbidden"


def test_upload_command_cannot_carry_the_prepared_content_type() -> None:
    assert list(UploadAttachmentContent.model_fields) == _observations()[
        "upload_attachment_content_fields"
    ]

    with pytest.raises(ValidationError) as captured:
        UploadAttachmentContent.model_validate(
            {
                "idempotency_key": ATTACHMENT_ID,
                "attachment_id": ATTACHMENT_ID,
                "expected_size": 0,
                "expected_sha256": EMPTY_SHA256,
                "expected_content_type": "application/octet-stream",
                "byte_stream": InMemoryBinaryStream(),
            }
        )

    assert _validation_error_type(
        captured.value, ("expected_content_type",)
    ) == "extra_forbidden"


def test_staged_resources_have_only_mutating_or_destructive_consumers() -> None:
    consumers = _methods_accepting(ports.ResourceStore, StagedResourceRef)

    assert consumers == _observations()["resource_store_staged_ref_consumers"]
    assert consumers == ["discard", "publish"]
    assert get_type_hints(ports.ResourceStore.publish)["return"].__name__ == "ResourceRef"
    assert get_type_hints(ports.ResourceStore.discard)["return"] is type(None)


def test_recovery_receipt_is_not_reachable_from_persistent_contract_roots() -> None:
    observations = _observations()
    roots = {"StateFile": StateFile, "StateMutation": StateMutation}
    referencing = [
        name
        for name, model in roots.items()
        if "RecoveryReceipt" in json.dumps(model.model_json_schema(), sort_keys=True)
    ]

    assert sorted(roots) == sorted(
        observations["persistent_roots_checked_for_recovery_receipt"]
    )
    assert referencing == observations[
        "persistent_roots_referencing_recovery_receipt"
    ]
    assert list(RuntimeEpochRecord.model_fields) == observations[
        "runtime_epoch_record_fields"
    ]
    assert list(RecoveryReceipt.model_fields) == observations[
        "recovery_receipt_fields"
    ]


def test_rejected_processing_cannot_persist_without_a_valid_job_outcome() -> None:
    state_payload = json.loads(CONTRACT_STATE_FIXTURE.read_text(encoding="utf-8"))
    aggregate_payload = state_payload["cases"][CASE_ID]
    job_id = next(iter(aggregate_payload["jobs"]))
    processing = OutcomeProcessingRecord(
        outcome_id=REJECTED_OUTCOME_ID,
        job_id=job_id,
        outcome_hash="0" * 64,
        outcome_file_ref={
            "relative_key": f"jobs/{job_id}/job_outcome.json",
            "size": 0,
            "sha256": "0" * 64,
        },
        disposition=OutcomeDisposition.REJECTED,
        processed_at="2026-07-31T00:00:00.000Z",
        error_code=ErrorCode.OUTCOME_MISSING,
        accepted_evidence_ids=[],
        accepted_artifact_ids=[],
        created_job_id=None,
        reason="The finalized Outcome is missing.",
    )
    aggregate_payload["outcome_processing_records"][REJECTED_OUTCOME_ID] = (
        processing.model_dump(mode="json")
    )

    assert _observations()[
        "case_aggregate_requires_exact_outcome_processing_pair_set"
    ]
    with pytest.raises(
        ValidationError,
        match="saved Outcomes and OutcomeProcessingRecords must form an exact pair set",
    ):
        CaseAggregate.model_validate(aggregate_payload)


def test_prepare_attachment_classifies_oversize_before_s03_can_handle_it() -> None:
    assert MAX_ATTACHMENT_BYTES == _observations()[
        "prepare_attachment_declared_size_maximum"
    ]

    with pytest.raises(ValidationError) as captured:
        PrepareAttachment.model_validate(
            {
                "idempotency_key": "prepare-1",
                "case_id": CASE_ID,
                "expected_case_revision": 1,
                "name": "logs.tar.gz",
                "content_type": "application/gzip",
                "declared_size": MAX_ATTACHMENT_BYTES + 1,
                "declared_sha256": None,
            }
        )

    assert _validation_error_type(captured.value, ("declared_size",)) == (
        "less_than_equal"
    )


def test_no_public_port_produces_the_required_resource_target() -> None:
    producers = _port_members_returning(PlannedResourceTarget)

    assert producers == _observations()[
        "planned_resource_target_returning_port_members"
    ]
    assert producers == []
    assert "final_storage_key" in inspect.signature(
        ports.ResourceStore.publish
    ).parameters
    assert _contains_type(
        get_type_hints(ports.ResourceStore.validate_case_capacity)[
            "planned_final_targets"
        ],
        PlannedResourceTarget,
    )
