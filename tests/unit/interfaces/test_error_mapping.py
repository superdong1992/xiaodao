from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from problem_locator.contracts.commands import CaseQueryResponse
from problem_locator.contracts.enums import ErrorCode
from problem_locator.contracts.errors import (
    CLI_EXIT_BY_ERROR_CODE,
    CLI_EXIT_CONFIG_OR_STATE_CORRUPT,
    CLI_EXIT_REQUEST_OR_STATE_CONFLICT,
    ERROR_SPECS,
    PORT_ERROR_CODES,
    ApplicationPortError,
)
from problem_locator.contracts.models import ApplicationError, ApplicationErrorDetail
from problem_locator.interfaces.error_mapping import (
    cli_exit_for,
    error_envelope,
    http_status_for,
    success_envelope,
    validation_error,
    validation_error_from,
)
from tests.unit.interfaces.fakes import FakeQuery
from tests.unit.interfaces.helpers import CASE_ID, case_view


@pytest.mark.parametrize("code", list(ErrorCode))
def test_frozen_error_mapping_is_lossless(code: ErrorCode) -> None:
    error = ApplicationError(
        code=code,
        message=f"Safe {code.value} message.",
        details=[
            ApplicationErrorDetail(
                field="case_id",
                resource_type=None,
                resource_id=None,
                resource_ref=None,
                expected="current",
                actual="stale",
                limit=None,
                observed=None,
            )
        ],
        retryable=ERROR_SPECS[code].application_retryable,
    )

    assert http_status_for(error) == ERROR_SPECS[code].http_status
    assert cli_exit_for(error) == CLI_EXIT_BY_ERROR_CODE[code]
    assert error_envelope(error) == {
        "ok": False,
        "data": None,
        "error": error.model_dump(mode="json"),
    }


@pytest.mark.parametrize(
    ("code", "http_status", "cli_exit"),
    [
        (ErrorCode.VALIDATION_ERROR, 400, CLI_EXIT_REQUEST_OR_STATE_CONFLICT),
        (ErrorCode.INVALID_CASE_STATE, 409, CLI_EXIT_REQUEST_OR_STATE_CONFLICT),
        (ErrorCode.ACTIVE_JOB_EXISTS, 409, CLI_EXIT_REQUEST_OR_STATE_CONFLICT),
        (ErrorCode.NEW_CASE_REQUIRED, 409, CLI_EXIT_REQUEST_OR_STATE_CONFLICT),
        (ErrorCode.STATE_CORRUPT, 503, CLI_EXIT_CONFIG_OR_STATE_CORRUPT),
        (
            ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            503,
            CLI_EXIT_CONFIG_OR_STATE_CORRUPT,
        ),
    ],
)
def test_r3_boundary_error_semantics_are_independently_fixed(
    code: ErrorCode,
    http_status: int,
    cli_exit: int,
) -> None:
    assert ERROR_SPECS[code].http_status == http_status
    assert CLI_EXIT_BY_ERROR_CODE[code] == cli_exit
    assert ERROR_SPECS[code].application_retryable is False


def test_r3_validation_and_claim_channels_remain_method_qualified() -> None:
    validation_methods = (
        "ApplicationQueryPort.get_case",
        "ApplicationQueryPort.list_artifacts",
        "ApplicationQueryPort.open_artifact",
        "JobControlPort.claim_job",
        "JobControlPort.submit_outcome",
        "JobControlPort.report_execution_infrastructure_failure",
        "JobControlPort.interrupt_previous_epoch",
    )
    assert all(
        ErrorCode.VALIDATION_ERROR in PORT_ERROR_CODES[method]
        for method in validation_methods
    )
    assert ErrorCode.CLAIM_REJECTED not in PORT_ERROR_CODES[
        "JobControlPort.claim_job"
    ]


def test_success_envelope_preserves_explicit_nulls() -> None:
    assert success_envelope({"value": None, "items": []}) == {
        "ok": True,
        "data": {"value": None, "items": []},
        "error": None,
    }


def test_validation_error_uses_only_s00_vocabulary() -> None:
    error = validation_error()
    assert error.code is ErrorCode.VALIDATION_ERROR
    assert error.details == []
    assert error.retryable is False


def test_validation_error_from_exposes_field_path_type_message_and_input() -> None:
    class Request(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        count: int

    with pytest.raises(ValidationError) as captured:
        Request.model_validate({"count": "3", "unexpected": "forbidden"})

    error = validation_error_from(captured.value)

    assert error.code is ErrorCode.VALIDATION_ERROR
    assert [detail.field for detail in error.details] == ["count", "unexpected"]
    assert error.details[0].expected.startswith("int_type:")
    assert error.details[0].actual == "3"
    assert error.details[1].expected.startswith("extra_forbidden:")
    assert error.details[1].actual == "forbidden"


def test_value_error_is_returned_as_a_root_validation_detail() -> None:
    error = validation_error_from(ValueError("body must be a JSON object"))

    assert len(error.details) == 1
    assert error.details[0].field == "$"
    assert error.details[0].expected == "ValueError: body must be a JSON object"
    assert error.details[0].actual is None


def test_public_port_exception_maps_without_private_error_protocol() -> None:
    error = ApplicationError(
        code=ErrorCode.REVISION_CONFLICT,
        message="Case revision changed.",
        details=[],
        retryable=True,
    )
    failure = ApplicationPortError(error)

    assert failure.error is error
    assert error_envelope(failure.error) == {
        "ok": False,
        "data": None,
        "error": error.model_dump(mode="json"),
    }
    assert http_status_for(failure.error) == ERROR_SPECS[error.code].http_status
    assert cli_exit_for(failure.error) == CLI_EXIT_BY_ERROR_CODE[error.code]


def test_query_fake_rejects_invalid_raw_input_without_recording_or_consuming() -> None:
    expected = CaseQueryResponse(case_view=case_view(), wait_timed_out=False)
    query = FakeQuery()
    query.queue("get_case", expected)

    with pytest.raises(ApplicationPortError) as caught:
        query.get_case("not-an-opaque-id")

    assert caught.value.error.code is ErrorCode.VALIDATION_ERROR
    assert query.calls == []
    assert query.get_case(CASE_ID) == expected
    assert query.calls == [("get_case", (CASE_ID, None, 0))]


def test_public_fakes_reject_scripted_errors_outside_the_method_contract() -> None:
    disallowed = ApplicationPortError(
        ApplicationError(
            code=ErrorCode.CLAIM_REJECTED,
            message="This Query method does not expose claim outcomes.",
            details=[],
            retryable=False,
        )
    )
    query = FakeQuery()
    query.queue("get_case", disallowed)

    with pytest.raises(ValueError, match="does not allow CLAIM_REJECTED"):
        query.get_case(CASE_ID)
