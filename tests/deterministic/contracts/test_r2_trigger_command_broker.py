from __future__ import annotations

import inspect
from pathlib import Path
from types import ModuleType
from typing import Any, get_type_hints

import pytest
from pydantic import ValidationError

from problem_locator.contracts import errors, ports
from problem_locator.contracts.enums import ErrorCode, ExecutionStage
from problem_locator.contracts.models import (
    ExecutionFailure,
    Job,
    SubmitSupplementTriggerPayload,
    UploadAttachmentContent,
    WorkspaceInputManifest,
)
from problem_locator.contracts.serialization import (
    business_request_preimage,
    business_request_sha256,
    canonical_json_bytes,
)

from tests.deterministic.contracts._support import FIXTURE_ROOT, load_json
from tests.deterministic.contracts.fakes import (
    FakeLogparseBrokerFactory,
    InMemoryBinaryStream,
    InMemoryCancellationSignal,
)


ATTACHMENT_ID = "00000000-0000-0000-0000-000000000050"
PAYLOAD = b"r2 upload payload"
PAYLOAD_SHA256 = (
    "6d6c5f220618754a7665bc51a9b4bea83a217d49cc3a2672196eef55fe37b028"
)


def _required_symbol(module: ModuleType, name: str) -> Any:
    value = getattr(module, name, None)
    assert value is not None, f"R2 requires {module.__name__}.{name}"
    return value


def _upload_payload(content_type: str = "text/plain") -> dict[str, object]:
    return {
        "idempotency_key": ATTACHMENT_ID,
        "attachment_id": ATTACHMENT_ID,
        "expected_content_type": content_type,
        "expected_size": len(PAYLOAD),
        "expected_sha256": PAYLOAD_SHA256,
        "byte_stream": InMemoryBinaryStream(PAYLOAD),
    }


def _diagnose_job_and_manifest() -> tuple[Job, WorkspaceInputManifest]:
    return (
        Job.model_validate(
            load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
        ),
        WorkspaceInputManifest.model_validate(
            load_json(
                FIXTURE_ROOT / "positive" / "workspace-input-manifest.json"
            )
        ),
    )


def _logparse_failure() -> ExecutionFailure:
    return ExecutionFailure(
        stage=ExecutionStage.ASSET_RESOLUTION,
        code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
        message="Logparse broker asset is unavailable.",
        retryable=False,
        details=[],
    )


def test_submit_supplement_trigger_freezes_required_strict_bool_field() -> None:
    field = SubmitSupplementTriggerPayload.model_fields.get(
        "stable_target_changed"
    )
    assert field is not None, "R2 requires stable_target_changed"
    assert field.is_required()
    assert field.annotation is bool
    schema = SubmitSupplementTriggerPayload.model_json_schema()
    assert "stable_target_changed" in schema["required"]
    assert schema["properties"]["stable_target_changed"] == {"title": "Stable Target Changed", "type": "boolean"}


def test_submit_supplement_trigger_requires_bool_without_coercion() -> None:
    base = {
        "user_facts": [],
        "ready_attachment_ids": [ATTACHMENT_ID],
        "stable_target_changed": False,
    }
    assert (
        SubmitSupplementTriggerPayload.model_validate(base).stable_target_changed
        is False
    )

    missing = dict(base)
    missing.pop("stable_target_changed")
    with pytest.raises(ValidationError):
        SubmitSupplementTriggerPayload.model_validate(missing)

    for invalid in (0, 1, "false", None):
        with pytest.raises(ValidationError):
            SubmitSupplementTriggerPayload.model_validate(
                {**base, "stable_target_changed": invalid}
            )


def test_upload_command_freezes_required_expected_content_type() -> None:
    field = UploadAttachmentContent.model_fields.get("expected_content_type")
    assert field is not None, "R2 requires expected_content_type"
    assert field.is_required()
    schema = UploadAttachmentContent.model_json_schema()
    assert "expected_content_type" in schema["required"]
    property_schema = schema["properties"]["expected_content_type"]
    assert property_schema["type"] == "string"
    assert "pattern" in property_schema

    missing = _upload_payload()
    missing.pop("expected_content_type")
    with pytest.raises(ValidationError):
        UploadAttachmentContent.model_validate(missing)


def test_upload_content_type_is_part_of_the_business_request_hash() -> None:
    text_upload = UploadAttachmentContent.model_validate(_upload_payload())
    binary_upload = UploadAttachmentContent.model_validate(
        _upload_payload("application/octet-stream")
    )

    assert business_request_preimage(text_upload) == {
        "attachment_id": ATTACHMENT_ID,
        "expected_content_type": "text/plain",
        "expected_sha256": PAYLOAD_SHA256,
        "expected_size": len(PAYLOAD),
        "idempotency_key": ATTACHMENT_ID,
    }
    assert business_request_sha256(text_upload) != business_request_sha256(
        binary_upload
    )
    for invalid in ("Text/Plain", "text/plain; charset=utf-8", " text/plain"):
        with pytest.raises(ValidationError):
            UploadAttachmentContent.model_validate(_upload_payload(invalid))


def test_logparse_session_has_one_typed_parse_request_accessor() -> None:
    public_methods = {
        name
        for name, value in vars(ports.LogparseBrokerSession).items()
        if not name.startswith("_") and callable(value)
    }
    assert public_methods == {
        "agent_environment",
        "audit_bytes",
        "close",
        "parse_request_bytes",
    }
    method = ports.LogparseBrokerSession.parse_request_bytes
    assert list(inspect.signature(method).parameters) == ["self"]
    assert get_type_hints(method)["return"] == bytes | None


def test_fake_logparse_session_preserves_exact_parse_bytes_after_close(
    tmp_path: Path,
) -> None:
    job, manifest = _diagnose_job_and_manifest()
    factory = FakeLogparseBrokerFactory()
    session = factory.open(
        job,
        tmp_path,
        manifest,
        InMemoryCancellationSignal(),
    )
    assert isinstance(session, ports.LogparseBrokerSession)
    assert session.parse_request_bytes() is None

    request_bytes = canonical_json_bytes(
        {
            "id": 1,
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "arguments": {"attachment_id": ATTACHMENT_ID},
                "name": "parse",
            },
        }
    )
    record = getattr(session, "_record_parse_request")
    record(request_bytes)
    captured = session.parse_request_bytes()
    assert type(captured) is bytes
    assert captured == request_bytes
    assert memoryview(captured).readonly

    session.close()
    assert session.parse_request_bytes() == request_bytes


def test_logparse_broker_error_is_the_only_typed_read_only_failure() -> None:
    error_type = _required_symbol(errors, "LogparseBrokerError")
    typed_names = {
        name
        for name, value in vars(errors).items()
        if name.startswith("Logparse")
        and inspect.isclass(value)
        and issubclass(value, BaseException)
    }
    assert typed_names == {"LogparseBrokerError"}

    class_hint = get_type_hints(error_type).get("failure")
    descriptor = inspect.getattr_static(error_type, "failure", None)
    getter_hint = (
        get_type_hints(descriptor.fget).get("return")
        if isinstance(descriptor, property) and descriptor.fget is not None
        else None
    )
    assert class_hint is ExecutionFailure or getter_hint is ExecutionFailure

    failure = _logparse_failure()
    error = error_type(failure)
    assert error.failure == failure
    assert error.args == ()
    assert error.__dict__ == {}
    assert not hasattr(error, "execution_failure")
    with pytest.raises((AttributeError, TypeError)):
        error.failure = _logparse_failure()


def test_fake_broker_open_can_raise_logparse_broker_error_type_safely(
    tmp_path: Path,
) -> None:
    error_type = _required_symbol(errors, "LogparseBrokerError")
    failure = _logparse_failure()
    error = error_type(failure)

    def fail_open(
        job: Job,
        workspace_root: Path,
        workspace_manifest: WorkspaceInputManifest,
        cancellation: ports.CancellationSignal,
    ) -> ports.LogparseBrokerSession:
        del job, workspace_root, workspace_manifest, cancellation
        raise error

    job, manifest = _diagnose_job_and_manifest()
    factory = FakeLogparseBrokerFactory(opener=fail_open)
    with pytest.raises(error_type) as raised:
        factory.open(
            job,
            tmp_path,
            manifest,
            InMemoryCancellationSignal(),
        )
    assert raised.value.failure == failure
