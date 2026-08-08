from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any, get_args, get_type_hints

import pytest
from pydantic import ValidationError

from problem_locator.contracts import errors, ports
from problem_locator.contracts.enums import (
    ErrorCode,
    OutcomeDisposition,
    ResourceKind,
    ResourceType,
)
from problem_locator.contracts.limits import CONTRACT_REVISION, MAX_ATTACHMENT_BYTES
from problem_locator.contracts.models import (
    ApplicationError,
    AttachmentRequirementConstraints,
    CaseAggregate,
    FixtureManifest,
    OutcomeProcessingRecord,
    PlannedResourceTarget,
    PrepareAttachment,
    RecoveryProcessingRecord,
    StateFile,
    StateMutation,
    StagedResourceRef,
    SubmitSupplement,
    SubmitSupplementTriggerPayload,
    UploadAttachmentContent,
)
from tests.contracts.fakes import InMemoryBinaryStream, InMemoryResourceStore


ROOT = Path(__file__).resolve().parents[3]
READINESS_FIXTURE = (
    ROOT / "tests/fixtures/components/application/contract-blockers.json"
)
SCENARIO_FIXTURE = (
    ROOT / "tests/fixtures/components/application/scenario-matrix.json"
)
COMPONENT_FIXTURE_ROOT = ROOT / "tests/fixtures/components/application"
CONTRACT_STATE_FIXTURE = ROOT / "tests/fixtures/contracts/positive/state.json"

CASE_ID = "00000000-0000-0000-0000-000000000001"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000020"
REJECTED_OUTCOME_ID = "00000000-0000-0000-0000-000000000099"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _readiness_fixture() -> dict[str, Any]:
    return json.loads(READINESS_FIXTURE.read_text(encoding="utf-8"))


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


def test_fixture_records_every_s03_contract_gap_as_resolved_by_r3() -> None:
    fixture = _readiness_fixture()

    assert fixture["schema_version"] == 1
    assert fixture["owner_spec"] == "S03"
    assert fixture["contract_revision"] == CONTRACT_REVISION == "v3-contract-r1"
    expected_resolutions = {
        "application_port_error": "v1-contract-r2",
        "coordinator_plan_result": "v1-contract-r2",
        "stable_target_changed": "v1-contract-r2",
        "expected_content_type": "v1-contract-r2",
        "staged_preflight": "v1-contract-r2",
        "recovery_receipt_persistence": "v1-contract-r2",
        "rejected_processing_without_valid_outcome": "v1-contract-r2",
        "prepare_size_classification": "v1-contract-r2",
        "resource_target_key_derivation": "v1-contract-r2",
        "CCR-S03-R2-STATE-READ-ERROR-CLOSURE": "v1-contract-r3",
        "CCR-S03-R2-COORDINATOR-ERROR-CLOSURE": "v1-contract-r3",
        "CCR-S03-R2-CAPACITY-OBSERVED-CLOSURE": "v1-contract-r3",
        "CCR-S03-R2-NEXT-JOB-PUBLISH-ERROR-CLOSURE": "v1-contract-r3",
        "CCR-S03-R2-ASSET-BINDING-FAILURE-CLOSURE": "v1-contract-r3",
        "CCR-S03-R2-QUERY-RAW-VALIDATION-ERROR-CLOSURE": "v1-contract-r3",
        "CCR-S03-R2-JOBCONTROL-RAW-VALIDATION-ERROR-CLOSURE": "v1-contract-r3",
    }
    assert {
        item["id"]: item["resolved_by"] for item in fixture["resolutions"]
    } == expected_resolutions
    assert fixture["unresolved_contract_change_requests"] == []


def test_r3_scenario_matrix_has_unique_ids_and_closed_boundary_examples() -> None:
    fixture = json.loads(SCENARIO_FIXTURE.read_text(encoding="utf-8"))

    assert fixture["schema_version"] == 1
    assert fixture["owner_spec"] == "S03"
    assert fixture["contract_revision"] == CONTRACT_REVISION
    scenarios = fixture["scenarios"]
    assert all(set(item) == {"id", "disposition", "error"} for item in scenarios)
    scenario_ids = [item["id"] for item in scenarios]
    assert len(scenario_ids) == len(set(scenario_ids))
    assert {
        "upload.post_stage_state_read_fault",
        "outcome.first_next_job_publish_failure",
        "outcome.existing_next_job_bytes_drift",
        "outcome.finalized_asset_failure_parked",
        "claim.normal_competition",
        "query.invalid_raw_input",
        "recovery.concurrent_interrupt",
        "state.postcommit_replay_projection_fault",
    } <= set(scenario_ids)


def test_contract_exposes_one_typed_application_port_failure() -> None:
    error = ApplicationError(
        code=ErrorCode.CASE_NOT_FOUND,
        message="The Case does not exist.",
        details=[],
        retryable=False,
    )
    failure = errors.ApplicationPortError(error)

    assert isinstance(failure, Exception)
    assert failure.error is error
    assert str(failure) == error.message
    assert "ApplicationPortError" in errors.__all__


def test_submit_supplement_trigger_carries_stable_target_decision() -> None:
    assert "stable_target_changed" not in SubmitSupplement.model_fields
    assert list(SubmitSupplementTriggerPayload.model_fields) == [
        "user_facts",
        "ready_attachment_ids",
        "stable_target_changed",
    ]
    payload = SubmitSupplementTriggerPayload(
        user_facts=[],
        ready_attachment_ids=[ATTACHMENT_ID],
        stable_target_changed=False,
    )
    assert payload.stable_target_changed is False


def test_upload_command_carries_content_type_for_pre_read_validation() -> None:
    stream = InMemoryBinaryStream()
    command = UploadAttachmentContent(
        idempotency_key=ATTACHMENT_ID,
        attachment_id=ATTACHMENT_ID,
        expected_content_type="application/octet-stream",
        expected_size=0,
        expected_sha256=EMPTY_SHA256,
        byte_stream=stream,
    )

    assert command.expected_content_type == "application/octet-stream"
    assert command.byte_stream is stream


def test_all_attachment_boundaries_accept_the_same_vendor_content_type() -> None:
    content_type = "application/vnd.acme.problem+json"

    prepared = PrepareAttachment(
        idempotency_key="prepare-vendor-content-type",
        case_id=CASE_ID,
        expected_case_revision=1,
        name="evidence.bin",
        content_type=content_type,
        declared_size=0,
        declared_sha256=EMPTY_SHA256,
    )
    requirement = AttachmentRequirementConstraints(
        allowed_content_types=[content_type],
        min_count=1,
        max_count=1,
    )
    upload = UploadAttachmentContent(
        idempotency_key=ATTACHMENT_ID,
        attachment_id=ATTACHMENT_ID,
        expected_content_type=content_type,
        expected_size=0,
        expected_sha256=EMPTY_SHA256,
        byte_stream=InMemoryBinaryStream(),
    )

    assert prepared.content_type == content_type
    assert requirement.allowed_content_types == [content_type]
    assert upload.expected_content_type == content_type


@pytest.mark.parametrize(
    "content_type",
    [
        "Application/json",
        "application/json; charset=utf-8",
        " application/json",
        "application/ json",
        "application/json\r\nX-Injected: true",
        "应用/json",
        f"application/{'a' * 116}",
        "application/*",
    ],
)
def test_all_attachment_boundaries_reject_noncanonical_content_type(
    content_type: str,
) -> None:
    with pytest.raises(ValidationError):
        PrepareAttachment(
            idempotency_key="prepare-invalid-content-type",
            case_id=CASE_ID,
            expected_case_revision=1,
            name="evidence.bin",
            content_type=content_type,
            declared_size=0,
            declared_sha256=EMPTY_SHA256,
        )
    with pytest.raises(ValidationError):
        AttachmentRequirementConstraints(
            allowed_content_types=[content_type],
            min_count=1,
            max_count=1,
        )
    with pytest.raises(ValidationError):
        UploadAttachmentContent(
            idempotency_key=ATTACHMENT_ID,
            attachment_id=ATTACHMENT_ID,
            expected_content_type=content_type,
            expected_size=0,
            expected_sha256=EMPTY_SHA256,
            byte_stream=InMemoryBinaryStream(),
        )


def test_resource_store_exposes_non_mutating_staged_preflight() -> None:
    consumers = _methods_accepting(ports.ResourceStore, StagedResourceRef)

    assert consumers == ["discard", "publish", "validate_staged"]
    assert get_type_hints(ports.ResourceStore.validate_staged)["return"] is type(None)


def test_recovery_processing_is_persisted_with_runtime_epoch() -> None:
    assert "recovery_processing_records" in StateFile.model_fields
    assert "upsert_recovery_processing_records" in StateMutation.model_fields
    assert list(RecoveryProcessingRecord.model_fields) == [
        "recovery_id",
        "current_runtime_epoch",
        "interrupted_job_ids",
        "pending_job_ids",
        "completed_at",
    ]
    state = StateFile.model_validate_json(
        CONTRACT_STATE_FIXTURE.read_text(encoding="utf-8")
    )
    assert set(state.recovery_processing_records) == {
        record.recovery_id for record in state.runtime_epochs
    }


def test_rejected_processing_can_persist_without_trusted_outcome() -> None:
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

    aggregate = CaseAggregate.model_validate(aggregate_payload)
    assert REJECTED_OUTCOME_ID not in aggregate.outcomes
    assert (
        aggregate.outcome_processing_records[REJECTED_OUTCOME_ID].error_code
        is ErrorCode.OUTCOME_MISSING
    )


def test_prepare_oversize_reaches_s03_for_business_classification() -> None:
    command = PrepareAttachment(
        idempotency_key="prepare-1",
        case_id=CASE_ID,
        expected_case_revision=1,
        name="logs.tar.gz",
        content_type="application/gzip",
        declared_size=MAX_ATTACHMENT_BYTES + 1,
        declared_sha256=None,
    )

    assert command.declared_size == MAX_ATTACHMENT_BYTES + 1


def test_resource_store_plans_the_deterministic_formal_target() -> None:
    annotation = get_type_hints(ports.ResourceStore.plan_target)["return"]
    assert annotation is PlannedResourceTarget
    store = InMemoryResourceStore()

    target = store.plan_target(
        CASE_ID,
        ResourceType.ATTACHMENT,
        ATTACHMENT_ID,
        ResourceKind.FILE,
        0,
        EMPTY_SHA256,
    )

    assert target.final_storage_key == (
        f"resources/cases/{CASE_ID}/attachments/{ATTACHMENT_ID}/payload"
    )


def test_component_fixture_manifest_covers_every_owned_byte() -> None:
    manifest = FixtureManifest.model_validate_json(
        (COMPONENT_FIXTURE_ROOT / "fixture-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest.owner_spec == "S03"
    assert manifest.root == "tests/fixtures/components/application"
    actual = {
        path.name: path
        for path in COMPONENT_FIXTURE_ROOT.iterdir()
        if path.is_file() and path.name != "fixture-manifest.json"
    }
    assert [entry.path for entry in manifest.files] == sorted(actual)
    for entry in manifest.files:
        data = actual[entry.path].read_bytes()
        assert entry.size == len(data)
        assert entry.sha256 == hashlib.sha256(data).hexdigest()
