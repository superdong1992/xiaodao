from __future__ import annotations

import hashlib

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from problem_locator.contracts.models import (
    CancelCase,
    CreateCase,
    PrepareAttachment,
    ProblemSpec,
    ProblemSpecInput,
    ProblemSpecPatch,
    ResumeCase,
    SubmitSupplement,
    UploadAttachmentContent,
    UserFactInput,
)
from problem_locator.contracts.outcomes import apply_problem_spec_patch
from problem_locator.contracts.serialization import (
    business_request_preimage,
    business_request_sha256,
    canonical_json_bytes,
    hash_excluded_fields,
)

from tests.deterministic.contracts.fakes import InMemoryBinaryStream


CASE_ID = "00000000-0000-0000-0000-000000000001"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000030"
PAYLOAD = b"contract upload"
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()


def _problem_spec_input() -> ProblemSpecInput:
    return ProblemSpecInput(
        statement="Locate the timed-out inventory request.",
        expected_behavior="The request completes within its deadline.",
        actual_behavior="The request times out.",
        scope="One inventory RPC.",
        goals=["Identify the cause."],
        non_goals=[],
        constraints=["Use the supplied logs."],
        completion_criteria=["Identify the timed-out request."],
    )


@pytest.mark.parametrize(
    ("model_type", "excluded"),
    [
        (CreateCase, ("wait_seconds",)),
        (PrepareAttachment, ()),
        (UploadAttachmentContent, ("byte_stream",)),
        (SubmitSupplement, ("wait_seconds",)),
        (ResumeCase, ("wait_seconds",)),
        (CancelCase, ()),
    ],
)
def test_every_external_write_command_declares_hash_exclusions(
    model_type: type,
    excluded: tuple[str, ...],
) -> None:
    assert hash_excluded_fields(model_type) == excluded
    assert model_type.model_json_schema()["hash_excluded_fields"] == list(excluded)


def test_wait_duration_is_excluded_but_array_order_remains_semantic() -> None:
    base = CreateCase(
        idempotency_key="create-1",
        raw_problem_text="A payment service call to inventory times out.",
        problem_spec=_problem_spec_input(),
        initial_user_facts=[
            UserFactInput(name="region", value="us-east-1"),
            UserFactInput(name="tenant", value="acme"),
        ],
        wait_seconds=0,
    )
    assert business_request_sha256(base) == business_request_sha256(
        base.model_copy(update={"wait_seconds": 30})
    )
    assert business_request_sha256(base) != business_request_sha256(
        base.model_copy(update={"initial_user_facts": list(reversed(base.initial_user_facts))})
    )


def test_upload_hash_never_serializes_or_consumes_the_transport_stream() -> None:
    first_stream = InMemoryBinaryStream(PAYLOAD)
    second_stream = InMemoryBinaryStream(b"different transport object")
    base = UploadAttachmentContent(
        idempotency_key=ATTACHMENT_ID,
        attachment_id=ATTACHMENT_ID,
        expected_content_type="application/octet-stream",
        expected_size=len(PAYLOAD),
        expected_sha256=PAYLOAD_SHA256,
        byte_stream=first_stream,
    )
    replay = base.model_copy(update={"byte_stream": second_stream})
    assert business_request_preimage(base) == {
        "attachment_id": ATTACHMENT_ID,
        "expected_content_type": "application/octet-stream",
        "expected_sha256": PAYLOAD_SHA256,
        "expected_size": len(PAYLOAD),
        "idempotency_key": ATTACHMENT_ID,
    }
    assert business_request_sha256(base) == business_request_sha256(replay)
    assert first_stream.read_requests == []
    assert second_stream.read_requests == []


def test_problem_spec_patch_is_optional_by_absence_not_nullable() -> None:
    schema = ProblemSpecPatch.model_json_schema()
    assert "required" not in schema
    assert schema["properties"]["statement"]["type"] == "string"
    assert canonical_json_bytes(ProblemSpecPatch(statement="Clarified statement.")) == (
        b'{"statement":"Clarified statement."}\n'
    )
    with pytest.raises(ValidationError):
        ProblemSpecPatch.model_validate({})
    with pytest.raises(ValidationError):
        ProblemSpecPatch.model_validate({"statement": None})
    assert list(Draft202012Validator(schema).iter_errors({"statement": None}))


def test_problem_spec_patch_replaces_whole_fields_and_increments_once() -> None:
    current = ProblemSpec(**_problem_spec_input().model_dump(), revision=7)
    no_op, changed = apply_problem_spec_patch(
        current,
        ProblemSpecPatch(statement=current.statement),
    )
    assert not changed
    assert no_op == current
    assert no_op is not current

    updated, changed = apply_problem_spec_patch(
        current,
        ProblemSpecPatch(
            statement="Clarified timeout target.",
            constraints=["Only immutable evidence may be cited."],
        ),
    )
    assert changed
    assert updated.revision == 8
    assert updated.statement == "Clarified timeout target."
    assert updated.constraints == ["Only immutable evidence may be cited."]
    assert updated.goals == current.goals
