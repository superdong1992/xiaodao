from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping
from types import NoneType
from typing import Any, get_args, get_type_hints

import pytest

from problem_locator.contracts import commands, enums, errors, models, outcomes, ports
from problem_locator.contracts.enums import ErrorCode, ResourceKind
from problem_locator.contracts.models import ApplicationError

from tests.deterministic.contracts.fakes import (
    InMemoryBinaryStream,
    InMemoryResourceStore,
    ScriptedCoordinator,
)


CASE_ID = "00000000-0000-0000-0000-000000000001"
JOB_ID = "00000000-0000-0000-0000-000000000011"
RESOURCE_ID = "00000000-0000-0000-0000-000000000061"
OTHER_ID = "00000000-0000-0000-0000-000000000099"


PORT_TYPES = (
    ports.ApplicationCommandPort,
    ports.ApplicationQueryPort,
    ports.StateAdminPort,
    ports.StateRepository,
    ports.ResourceStore,
    ports.ExecutionRecordStore,
    ports.AssetCatalogPort,
    ports.Runtime,
    ports.JobControlPort,
)


def _declared_methods(protocol: type[Any]) -> dict[str, Any]:
    return {
        name: member
        for name, member in protocol.__dict__.items()
        if inspect.isfunction(member) and name != "__init__"
    }


def _application_error(
    code: ErrorCode = ErrorCode.VALIDATION_ERROR,
) -> ApplicationError:
    retryable_codes = getattr(errors, "APPLICATION_ERROR_RETRYABLE_CODES")
    return ApplicationError(
        code=code,
        message=f"Modeled port failure: {code.value}.",
        details=[],
        retryable=code in retryable_codes,
    )


def _application_port_error_type() -> type[Exception]:
    value = getattr(errors, "ApplicationPortError", None)
    assert inspect.isclass(value), "errors.ApplicationPortError must be public"
    assert issubclass(value, Exception)
    return value


def _port_error_codes() -> Mapping[str, frozenset[ErrorCode]]:
    value = getattr(errors, "PORT_ERROR_CODES", None)
    assert isinstance(value, Mapping), "errors.PORT_ERROR_CODES must be a public mapping"
    return value


def _assert_application_port_error(
    callback: Any,
    expected_code: ErrorCode,
) -> None:
    error_type = _application_port_error_type()
    with pytest.raises(error_type) as caught:
        callback()
    assert caught.value.error.code is expected_code
    assert isinstance(caught.value.error, ApplicationError)


def test_application_port_error_is_the_only_public_application_error_exception() -> None:
    error_type = _application_port_error_type()
    assert "ApplicationPortError" in errors.__all__

    descriptor = inspect.getattr_static(error_type, "error")
    assert isinstance(descriptor, property)
    assert descriptor.fset is None
    assert descriptor.fget is not None
    assert get_type_hints(descriptor.fget)["return"] is ApplicationError

    application_error = _application_error()
    exception = error_type(application_error)
    assert exception.error is application_error
    assert exception.args == ()
    assert exception.__dict__ == {}
    assert str(exception) == application_error.message
    with pytest.raises(AttributeError):
        exception.error = _application_error(ErrorCode.CASE_NOT_FOUND)

    semantic_exports: list[type[Exception]] = []
    for name in errors.__all__:
        exported = getattr(errors, name)
        if not inspect.isclass(exported) or not issubclass(exported, Exception):
            continue
        exported_error = inspect.getattr_static(exported, "error", None)
        if issubclass(exported, error_type) or isinstance(exported_error, property):
            semantic_exports.append(exported)
    assert semantic_exports == [error_type], (
        "ApplicationPortError must not have a synonymous public exception export"
    )


def test_port_error_code_mapping_is_frozen_public_and_covers_each_declared_method() -> None:
    mapping = _port_error_codes()
    assert "PORT_ERROR_CODES" in errors.__all__

    expected_keys = {
        f"{protocol.__name__}.{method_name}"
        for protocol in PORT_TYPES
        for method_name in _declared_methods(protocol)
    }
    assert set(mapping) == expected_keys
    assert all(isinstance(codes, frozenset) for codes in mapping.values())
    assert all(
        isinstance(code, ErrorCode)
        for codes in mapping.values()
        for code in codes
    )
    assert all(len(codes) == len(set(codes)) for codes in mapping.values())

    with pytest.raises(TypeError):
        mapping["StateRepository.read_case"] = frozenset()
    sample_codes = mapping["StateRepository.read_case"]
    with pytest.raises(AttributeError):
        sample_codes.add(ErrorCode.CASE_NOT_FOUND)


EXPECTED_SUCCESS_RETURNS: dict[str, Any] = {
    "ApplicationCommandPort.execute": commands.ApplicationResponse,
    "ApplicationQueryPort.get_case": commands.CaseQueryResponse,
    "ApplicationQueryPort.list_artifacts": commands.ArtifactListResponse,
    "ApplicationQueryPort.open_artifact": commands.OpenArtifactResult,
    "StateAdminPort.readiness": models.ReadinessReport,
    "StateAdminPort.validate_state": models.ValidationReport,
    "StateAdminPort.export_state": bytes,
    "StateRepository.read_case": models.CaseAggregate,
    "StateRepository.read_job": models.Job,
    "StateRepository.read_artifact": models.Artifact,
    "StateRepository.read_snapshot": models.StateFile,
    "StateRepository.commit": models.CommitReceipt,
    "StateRepository.validate_all": models.ValidationReport,
    "StateRepository.export_snapshot": bytes,
    "ResourceStore.stage_file": models.StagedResourceRef,
    "ResourceStore.stage_generated_file": models.StagedResourceRef,
    "ResourceStore.stage_tree": models.StagedResourceRef,
    "ResourceStore.stage_attachment": models.AttachmentStagedRef,
    "ResourceStore.validate_staged": NoneType,
    "ResourceStore.plan_target": models.PlannedResourceTarget,
    "ResourceStore.publish": models.ResourceRef,
    "ResourceStore.validate_case_capacity": models.CaseResourceUsage,
    "ResourceStore.open_read": ports.BinaryStream,
    "ResourceStore.materialize_read_only": models.MaterializedPath,
    "ResourceStore.discard": NoneType,
    "ExecutionRecordStore.publish_job": models.ExecutionFileRef,
    "ExecutionRecordStore.publish_outcome_bytes": models.ExecutionFileRef,
    "ExecutionRecordStore.publish_rejected_agent_output_bytes": models.ExecutionFileRef,
    "ExecutionRecordStore.publish_audit_bytes": models.ExecutionFileRef,
    "ExecutionRecordStore.read_published_job": models.PublishedJobReceipt | None,
    "ExecutionRecordStore.read_published_outcome": models.RuntimeExecutionReceipt | None,
    "ExecutionRecordStore.read_audit_bytes": bytes | None,
    "ExecutionRecordStore.open_log_sinks": models.ExecutionLogSinks,
    "AssetCatalogPort.check": models.AssetAvailabilityReport,
    "AssetCatalogPort.resolve": models.ResolvedAsset,
    "AssetCatalogPort.route_bindings": models.RuntimeBindings,
    "AssetCatalogPort.diagnose_bindings": models.RuntimeBindings,
    "AssetCatalogPort.review_bindings": models.RuntimeBindings,
    "Runtime.execute": models.RuntimeExecutionReceipt,
    "JobControlPort.claim_job": commands.ClaimReceipt,
    "JobControlPort.submit_outcome": commands.OutcomeReceipt,
    "JobControlPort.report_execution_infrastructure_failure": commands.FailureReceipt,
    "JobControlPort.interrupt_previous_epoch": commands.RecoveryReceipt,
}


def test_modeled_port_families_keep_their_success_return_annotations() -> None:
    actual_methods = {
        f"{protocol.__name__}.{method_name}": method
        for protocol in PORT_TYPES
        for method_name, method in _declared_methods(protocol).items()
    }
    assert set(actual_methods) == set(EXPECTED_SUCCESS_RETURNS)
    for key, method in actual_methods.items():
        actual = get_type_hints(method, include_extras=True)["return"]
        assert actual == EXPECTED_SUCCESS_RETURNS[key], (
            f"{key} must retain its success-only return annotation; "
            "modeled failure uses ApplicationPortError"
        )


def test_key_method_error_code_sets_are_exact_where_the_v1_spec_is_closed() -> None:
    mapping = _port_error_codes()
    state_failures = {
        ErrorCode.STATE_CORRUPT,
        ErrorCode.STATE_SCHEMA_UNSUPPORTED,
    }
    exact = {
        "ApplicationQueryPort.get_case": frozenset(
            {
                ErrorCode.VALIDATION_ERROR,
                ErrorCode.CASE_NOT_FOUND,
                ErrorCode.JOB_NOT_FOUND,
                ErrorCode.JOB_CASE_MISMATCH,
                *state_failures,
            }
        ),
        "ApplicationQueryPort.list_artifacts": frozenset(
            {
                ErrorCode.VALIDATION_ERROR,
                ErrorCode.CASE_NOT_FOUND,
                *state_failures,
            }
        ),
        "ApplicationQueryPort.open_artifact": frozenset(
            {
                ErrorCode.VALIDATION_ERROR,
                ErrorCode.CASE_NOT_FOUND,
                ErrorCode.ARTIFACT_NOT_FOUND,
                ErrorCode.RESOURCE_NOT_FOUND,
                ErrorCode.RESOURCE_HASH_MISMATCH,
                ErrorCode.RESOURCE_SIZE_MISMATCH,
                *state_failures,
            }
        ),
        "StateRepository.read_case": frozenset(
            {ErrorCode.CASE_NOT_FOUND, *state_failures}
        ),
        "StateRepository.read_job": frozenset(
            {ErrorCode.JOB_NOT_FOUND, *state_failures}
        ),
        "StateRepository.read_artifact": frozenset(
            {ErrorCode.ARTIFACT_NOT_FOUND, *state_failures}
        ),
        "StateRepository.commit": frozenset(
            {ErrorCode.REVISION_CONFLICT, ErrorCode.STATE_WRITE_FAILED}
        ),
        "ResourceStore.validate_staged": frozenset(
            {ErrorCode.RESOURCE_NOT_FOUND, ErrorCode.RESOURCE_HASH_MISMATCH}
        ),
        "ExecutionRecordStore.read_published_job": frozenset(
            {ErrorCode.EXECUTION_RECORD_FAILED}
        ),
        "ExecutionRecordStore.read_published_outcome": frozenset(
            {ErrorCode.EXECUTION_RECORD_FAILED}
        ),
    }
    assert {key: mapping[key] for key in exact} == exact


def test_coordinator_uses_an_explicit_result_union_and_fake_supports_both_branches() -> None:
    result_type = getattr(outcomes, "CoordinatorPlanResult", None)
    assert result_type is not None
    assert frozenset(get_args(result_type)) == frozenset(
        {outcomes.TransitionPlan, ApplicationError}
    )
    assert "CoordinatorPlanResult" in outcomes.__all__
    assert get_type_hints(ports.Coordinator.plan)["return"] == result_type
    assert get_type_hints(ScriptedCoordinator.plan)["return"] == result_type

    plan = outcomes.TransitionPlan.model_construct()
    application_error = _application_error(ErrorCode.INVALID_CASE_STATE)
    coordinator = ScriptedCoordinator([plan, application_error])
    snapshot = outcomes.CaseSnapshot.model_construct()
    trigger = outcomes.ValidatedTrigger.model_construct(
        trigger_type=enums.TriggerType.CREATE_CASE,
    )
    assert isinstance(coordinator.plan(snapshot, trigger), outcomes.TransitionPlan)
    returned_error = coordinator.plan(snapshot, trigger)
    assert isinstance(returned_error, ApplicationError)
    assert returned_error == application_error


def test_coordinator_error_codes_and_stable_target_decision_are_closed() -> None:
    expected_codes = frozenset(
        {
            ErrorCode.INVALID_CASE_STATE,
            ErrorCode.ACTIVE_JOB_EXISTS,
            ErrorCode.NEW_CASE_REQUIRED,
            ErrorCode.VALIDATION_ERROR,
        }
    )
    assert errors.COORDINATOR_PLAN_ERROR_CODES == expected_codes

    trigger = outcomes.ValidatedTrigger.model_construct(
        trigger_type=enums.TriggerType.SUBMIT_SUPPLEMENT,
        payload=outcomes.SubmitSupplementTriggerPayload(
            user_facts=[],
            ready_attachment_ids=[RESOURCE_ID],
            stable_target_changed=True,
        )
    )
    snapshot = outcomes.CaseSnapshot.model_construct()
    new_case_required = _application_error(ErrorCode.NEW_CASE_REQUIRED)
    assert (
        ScriptedCoordinator([new_case_required]).plan(snapshot, trigger)
        == new_case_required
    )

    with pytest.raises(ValueError, match="stable_target_changed"):
        ScriptedCoordinator([outcomes.TransitionPlan.model_construct()]).plan(
            snapshot,
            trigger,
        )
    with pytest.raises(ValueError, match="frozen non-retryable code"):
        ScriptedCoordinator([_application_error(ErrorCode.CASE_NOT_FOUND)]).plan(
            snapshot,
            trigger,
        )


def test_resource_type_and_planned_target_are_frozen_public_contracts() -> None:
    resource_type = getattr(enums, "ResourceType", None)
    assert inspect.isclass(resource_type)
    assert tuple(member.value for member in resource_type) == (
        "ATTACHMENT",
        "EVIDENCE",
        "ARTIFACT",
    )
    assert "ResourceType" in enums.__all__
    assert set(models.PlannedResourceTarget.model_fields) == {
        "case_id",
        "resource_type",
        "resource_id",
        "resource_kind",
        "size",
        "sha256",
        "final_storage_key",
    }


def test_resource_store_new_methods_have_the_frozen_signatures() -> None:
    resource_type = getattr(enums, "ResourceType")

    validate_staged = inspect.signature(ports.ResourceStore.validate_staged)
    assert list(validate_staged.parameters) == ["self", "staged_ref"]
    validate_hints = get_type_hints(ports.ResourceStore.validate_staged)
    assert validate_hints["staged_ref"] is models.StagedResourceRef
    assert validate_hints["return"] is NoneType

    plan_target = inspect.signature(ports.ResourceStore.plan_target)
    assert list(plan_target.parameters) == [
        "self",
        "case_id",
        "resource_type",
        "resource_id",
        "resource_kind",
        "size",
        "sha256",
    ]
    assert all(
        parameter.default is inspect.Signature.empty
        for parameter in plan_target.parameters.values()
    )
    target_hints = get_type_hints(ports.ResourceStore.plan_target)
    assert target_hints == {
        "case_id": str,
        "resource_type": resource_type,
        "resource_id": str,
        "resource_kind": ResourceKind,
        "size": int,
        "sha256": str,
        "return": models.PlannedResourceTarget,
    }


def test_validate_staged_is_read_only_and_unifies_missing_and_drift_errors() -> None:
    payload = b"staged-resource"
    store = InMemoryResourceStore()
    staged = store.stage_file(
        JOB_ID,
        "diagnostic_export",
        InMemoryBinaryStream(payload),
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )

    assert store.validate_staged(staged) is None
    assert store.validate_staged(staged) is None
    assert store.stage_file_calls == [(JOB_ID, "diagnostic_export")]

    drifted = staged.model_copy(update={"sha256": "9" * 64})
    _assert_application_port_error(
        lambda: store.validate_staged(drifted),
        ErrorCode.RESOURCE_HASH_MISMATCH,
    )

    store.discard(staged)
    _assert_application_port_error(
        lambda: store.validate_staged(staged),
        ErrorCode.RESOURCE_NOT_FOUND,
    )


@pytest.mark.parametrize(
    ("resource_type_name", "resource_kind", "expected_key"),
    [
        (
            "ATTACHMENT",
            ResourceKind.FILE,
            f"resources/cases/{CASE_ID}/attachments/{RESOURCE_ID}/payload",
        ),
        (
            "EVIDENCE",
            ResourceKind.FILE,
            f"resources/cases/{CASE_ID}/evidence/{RESOURCE_ID}/payload",
        ),
        (
            "EVIDENCE",
            ResourceKind.DIRECTORY,
            f"resources/cases/{CASE_ID}/evidence/{RESOURCE_ID}/tree",
        ),
        (
            "ARTIFACT",
            ResourceKind.DIRECTORY,
            f"resources/cases/{CASE_ID}/artifacts/{RESOURCE_ID}/tree",
        ),
    ],
)
def test_plan_target_is_stable_for_each_resource_type_and_both_resource_kinds(
    resource_type_name: str,
    resource_kind: ResourceKind,
    expected_key: str,
) -> None:
    resource_type = getattr(enums, "ResourceType")[resource_type_name]
    store = InMemoryResourceStore()
    arguments = (
        CASE_ID,
        resource_type,
        RESOURCE_ID,
        resource_kind,
        17,
        "7" * 64,
    )

    first = store.plan_target(*arguments)
    second = store.plan_target(*arguments)
    assert isinstance(first, models.PlannedResourceTarget)
    assert first == second
    assert first.model_dump(mode="python") == {
        "case_id": CASE_ID,
        "resource_type": resource_type,
        "resource_id": RESOURCE_ID,
        "resource_kind": resource_kind,
        "size": 17,
        "sha256": "7" * 64,
        "final_storage_key": expected_key,
    }
    assert not first.final_storage_key.startswith("/")
    assert ".." not in first.final_storage_key.split("/")


@pytest.mark.parametrize(
    "invalid_arguments",
    [
        ("../outside", "ATTACHMENT", RESOURCE_ID, ResourceKind.FILE, 1),
        (CASE_ID, "ATTACHMENT", "../outside", ResourceKind.FILE, 1),
        (CASE_ID, "ATTACHMENT", RESOURCE_ID, ResourceKind.DIRECTORY, 1),
        (CASE_ID, "ARTIFACT", RESOURCE_ID, ResourceKind.FILE, -1),
    ],
)
def test_plan_target_rejects_boundary_and_shape_violations_through_port_error(
    invalid_arguments: tuple[str, str, str, ResourceKind, int],
) -> None:
    case_id, resource_type_name, resource_id, resource_kind, size = invalid_arguments
    resource_type = getattr(enums, "ResourceType")[resource_type_name]
    store = InMemoryResourceStore()
    _assert_application_port_error(
        lambda: store.plan_target(
            case_id,
            resource_type,
            resource_id,
            resource_kind,
            size,
            "7" * 64,
        ),
        ErrorCode.VALIDATION_ERROR,
    )


def test_planned_target_identity_cannot_drift_from_its_storage_key() -> None:
    target = InMemoryResourceStore().plan_target(
        CASE_ID,
        enums.ResourceType.ARTIFACT,
        RESOURCE_ID,
        ResourceKind.FILE,
        17,
        "7" * 64,
    )
    payload = target.model_dump(mode="python")
    payload["final_storage_key"] = (
        f"resources/cases/{CASE_ID}/artifacts/{OTHER_ID}/payload"
    )
    with pytest.raises(ValueError, match="deterministic target"):
        models.PlannedResourceTarget.model_validate(payload)
