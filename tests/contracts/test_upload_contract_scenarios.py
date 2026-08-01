from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from problem_locator.contracts.enums import AttachmentStatus, ErrorCode
from problem_locator.contracts.limits import MAX_ATTACHMENT_BYTES
from problem_locator.contracts.models import (
    AttachmentRequirementConstraints,
    UploadAttachmentContent,
    UploadDescriptor,
)

from tests.contracts.fakes import InMemoryBinaryStream
from tests.contracts.scenario_fakes import (
    ATTACHMENT_ID,
    CountingBinaryStream,
    ScenarioError,
    UploadScenario,
    validate_put_content_type,
)


def _scenario(payload: bytes) -> tuple[UploadScenario, str]:
    digest = hashlib.sha256(payload).hexdigest()
    return (
        UploadScenario(
            attachment_id=ATTACHMENT_ID,
            declared_sha256=digest,
            content_type="application/octet-stream",
            declared_size=len(payload),
        ),
        digest,
    )


def test_upload_descriptor_freezes_all_four_headers_exactly() -> None:
    payload = b"one immutable upload"
    scenario, digest = _scenario(payload)
    descriptor = scenario.descriptor(
        f"/v1/attachments/{ATTACHMENT_ID}/content"
    )

    assert descriptor.method == "PUT"
    assert descriptor.required_headers == {
        "Idempotency-Key": ATTACHMENT_ID,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(payload)),
        "X-Content-SHA256": digest,
    }
    assert descriptor.max_bytes == MAX_ATTACHMENT_BYTES
    assert descriptor.expires_at is None

    # Undeclared size/hash keep the same four keys; null means the adapter must
    # fill the computed value before PUT, never omit the header.
    computed_at_put = UploadDescriptor(
        attachment_id=ATTACHMENT_ID,
        method="PUT",
        url=descriptor.url,
        required_headers={
            "Idempotency-Key": ATTACHMENT_ID,
            "Content-Type": "application/octet-stream",
            "Content-Length": None,
            "X-Content-SHA256": None,
        },
        max_bytes=MAX_ATTACHMENT_BYTES,
        expires_at=None,
    )
    assert set(computed_at_put.required_headers) == set(descriptor.required_headers)
    assert computed_at_put.required_headers["Content-Length"] is None
    assert computed_at_put.required_headers["X-Content-SHA256"] is None

    invalid = descriptor.model_dump(mode="python")
    invalid["required_headers"] = {
        **descriptor.required_headers,
        "X-Extra": "forbidden",
    }
    with pytest.raises(ValidationError, match="exactly the four frozen keys"):
        UploadDescriptor.model_validate(invalid)


@pytest.mark.parametrize(
    "content_types,expected_message",
    [
        (["application/json", "Application/json"], "string should match pattern"),
        (["application/json", "application/json"], "must not contain duplicates"),
    ],
)
def test_attachment_content_type_allow_list_rejects_noncanonical_and_duplicate_values(
    content_types: list[str],
    expected_message: str,
) -> None:
    with pytest.raises(ValidationError) as invalid:
        AttachmentRequirementConstraints(
            allowed_content_types=content_types,
            min_count=1,
            max_count=1,
        )
    assert expected_message in str(invalid.value).lower()


@pytest.mark.parametrize(
    "actual",
    [
        "application/json",  # canonical, but not the descriptor's value
        "Application/octet-stream",
        "application/octet-stream; charset=binary",
        " application/octet-stream",
    ],
)
def test_put_content_type_must_equal_descriptor_value_byte_for_byte(actual: str) -> None:
    scenario, _ = _scenario(b"payload")
    descriptor = scenario.descriptor("/upload")

    with pytest.raises(ScenarioError) as mismatch:
        validate_put_content_type(descriptor, actual)
    assert mismatch.value.error.code is ErrorCode.VALIDATION_ERROR
    assert mismatch.value.error.retryable is False

    validate_put_content_type(descriptor, "application/octet-stream")


def test_attachment_size_boundary_uses_counting_stream_without_giant_allocation() -> None:
    exact_stream = CountingBinaryStream(MAX_ATTACHMENT_BYTES)
    exact = UploadAttachmentContent(
        idempotency_key=ATTACHMENT_ID,
        attachment_id=ATTACHMENT_ID,
        expected_content_type="application/octet-stream",
        expected_size=MAX_ATTACHMENT_BYTES,
        expected_sha256="a" * 64,
        byte_stream=exact_stream,
    )
    assert exact.expected_size == 2_684_354_560
    assert exact.byte_stream is exact_stream
    assert exact_stream.logical_size == MAX_ATTACHMENT_BYTES
    assert exact_stream.read_calls == 0
    assert len(CountingBinaryStream._CHUNK) == 1024 * 1024

    oversized_stream = CountingBinaryStream(MAX_ATTACHMENT_BYTES + 1)
    with pytest.raises(ValidationError, match="less than or equal|V1 byte limit"):
        UploadAttachmentContent(
            idempotency_key=ATTACHMENT_ID,
            attachment_id=ATTACHMENT_ID,
            expected_content_type="application/octet-stream",
            expected_size=MAX_ATTACHMENT_BYTES + 1,
            expected_sha256="b" * 64,
            byte_stream=oversized_stream,
        )
    assert oversized_stream.logical_size == 2_684_354_561
    assert oversized_stream.read_calls == 0


def test_body_is_consumed_once_then_same_hash_adopts_formal_bytes_with_zero_delta() -> None:
    payload = b"attachment bytes that must never be read twice"
    scenario, digest = _scenario(payload)
    body = InMemoryBinaryStream(payload)

    with pytest.raises(ScenarioError) as commit_failure:
        scenario.publish_then_fail_ready_commit(
            body,
            expected_sha256=digest,
        )
    assert commit_failure.value.error.code is ErrorCode.STATE_WRITE_FAILED
    assert scenario.status is AttachmentStatus.UPLOADING
    assert body.closed is True
    assert body.bytes_read == len(payload)
    assert len(body.read_requests) == 2  # payload followed by the single EOF read
    assert scenario.snapshot_reads == [("early", 10), ("post-stage", 11)]
    assert scenario.commit_expected_generations == [11]
    assert scenario.capacity_new_bytes == [len(payload)]
    assert scenario.events[:5] == [
        "snapshot:early",
        "body:consume",
        "snapshot:post-stage",
        "publish",
        "commit:failed",
    ]

    adopted = scenario.resume_after_commit_failure(expected_sha256=digest)
    assert adopted.sha256 == digest
    assert scenario.status is AttachmentStatus.READY
    assert scenario.snapshot_reads[-1] == ("retry-post-stage", 12)
    assert scenario.commit_expected_generations == [11, 12]
    assert scenario.capacity_new_bytes == [len(payload), 0]
    assert body.bytes_read == len(payload)
    assert len(body.read_requests) == 2
    assert scenario.events[-2:] == ["adopt", "commit:ready"]
    assert scenario.resources.published_storage_keys == (adopted.storage_key,)


def test_different_hash_after_publish_commit_failure_is_idempotency_conflict() -> None:
    payload = b"first body"
    scenario, digest = _scenario(payload)
    body = InMemoryBinaryStream(payload)
    with pytest.raises(ScenarioError):
        scenario.publish_then_fail_ready_commit(body, expected_sha256=digest)
    calls_before_conflict = len(scenario.resources.capacity_calls)

    with pytest.raises(ScenarioError) as conflict:
        scenario.resume_after_commit_failure(expected_sha256="f" * 64)

    assert conflict.value.error.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert conflict.value.error.retryable is False
    assert len(scenario.resources.capacity_calls) == calls_before_conflict
    assert scenario.status is AttachmentStatus.UPLOADING
