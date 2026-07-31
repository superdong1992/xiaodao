from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from problem_locator.application.projection import project_case_view
from problem_locator.application.service import (
    ApplicationService,
    build_application_service,
)
from problem_locator.contracts import (
    ApplicationCommandPort,
    ApplicationError,
    ApplicationPortError,
    ApplicationQueryPort,
    ApplicationResponse,
    BusinessReceipt,
    CaseQueryResponse,
    ERROR_SPECS,
    ErrorCode,
    JobControlPort,
    PrepareAttachment,
    StateFile,
    UploadAttachmentContent,
)
from tests.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeClock,
    InMemoryAttachmentUploadGuard,
    InMemoryBinaryStream,
    InMemoryExecutionRecordStore,
    InMemoryPublicationCommitGuard,
    InMemoryResourceStore,
    InMemoryStateChangeNotifier,
    InMemoryStateRepository,
    PureContextSnapshotProjector,
    RecordingDispatcher,
    ScriptedCoordinator,
)


ROOT = Path(__file__).resolve().parents[3]
CASE_ID = "00000000-0000-0000-0000-000000000001"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000090"


def _state() -> StateFile:
    payload = json.loads(
        (ROOT / "tests/fixtures/contracts/positive/state.json").read_text(
            encoding="utf-8"
        )
    )
    return StateFile.model_validate(payload)


class _External:
    def __init__(self, response: ApplicationResponse) -> None:
        self.response = response
        self.calls = []

    def execute(self, command):
        self.calls.append(command)
        return self.response


class _Upload:
    def __init__(self, receipt: BusinessReceipt) -> None:
        self.receipt = receipt
        self.calls = []

    def execute(self, command):
        self.calls.append(command)
        return self.receipt


class _Queries:
    def __init__(self, response: CaseQueryResponse) -> None:
        self.response = response
        self.get_calls = []

    def get_case(self, case_id, wait_for_job_id=None, wait_seconds=0):
        self.get_calls.append((case_id, wait_for_job_id, wait_seconds))
        return self.response


class _FailingQueries:
    def __init__(self, error: ApplicationPortError) -> None:
        self.error = error
        self.get_calls = []

    def get_case(self, case_id, wait_for_job_id=None, wait_seconds=0):
        self.get_calls.append((case_id, wait_for_job_id, wait_seconds))
        raise self.error


def _port_error(code: ErrorCode) -> ApplicationPortError:
    return ApplicationPortError(
        ApplicationError(
            code=code,
            message="injected state read failure",
            details=[],
            retryable=ERROR_SPECS[code].application_retryable,
        )
    )


def test_facade_routes_streaming_and_nonstreaming_commands_once() -> None:
    state = _state()
    view = project_case_view(state, CASE_ID)
    external_receipt = BusinessReceipt(
        operation="PrepareAttachment",
        primary_resource_id=ATTACHMENT_ID,
        case_id=CASE_ID,
        case_revision=1,
        job_id=None,
        status="UPLOADING",
    )
    external_response = ApplicationResponse(
        business_receipt=external_receipt,
        case_view=view,
        wait_timed_out=False,
        dispatch_pending=False,
    )
    upload_receipt = external_receipt.model_copy(
        update={"operation": "UploadAttachmentContent", "status": "READY"}
    )
    external = _External(external_response)
    upload = _Upload(upload_receipt)
    queries = _Queries(CaseQueryResponse(case_view=view, wait_timed_out=False))
    service = ApplicationService(external, upload, queries, object(), object())

    prepare = PrepareAttachment(
        idempotency_key="prepare-1",
        case_id=CASE_ID,
        expected_case_revision=1,
        name="server.log",
        content_type="text/plain",
        declared_size=None,
        declared_sha256=None,
    )
    payload = b"payload"
    upload_command = UploadAttachmentContent(
        idempotency_key=ATTACHMENT_ID,
        attachment_id=ATTACHMENT_ID,
        expected_content_type="text/plain",
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        byte_stream=InMemoryBinaryStream(payload),
    )

    assert service.execute(prepare) is external_response
    upload_response = service.execute(upload_command)

    assert external.calls == [prepare]
    assert upload.calls == [upload_command]
    assert queries.get_calls == [(CASE_ID, None, 0)]
    assert upload_response.business_receipt == upload_receipt
    assert upload_response.case_view == view
    assert upload_response.dispatch_pending is False


@pytest.mark.parametrize(
    "code",
    [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED],
)
def test_upload_projection_state_failure_does_not_hide_durable_receipt(
    code: ErrorCode,
) -> None:
    receipt = BusinessReceipt(
        operation="UploadAttachmentContent",
        primary_resource_id=ATTACHMENT_ID,
        case_id=CASE_ID,
        case_revision=2,
        job_id=None,
        status="READY",
    )
    upload = _Upload(receipt)
    queries = _FailingQueries(_port_error(code))
    service = ApplicationService(object(), upload, queries, object(), object())
    payload = b"payload"
    command = UploadAttachmentContent(
        idempotency_key=ATTACHMENT_ID,
        attachment_id=ATTACHMENT_ID,
        expected_content_type="text/plain",
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        byte_stream=InMemoryBinaryStream(payload),
    )

    response = service.execute(command)

    assert response.business_receipt == receipt
    assert response.case_view is None
    assert response.wait_timed_out is False
    assert response.dispatch_pending is False
    assert upload.calls == [command]
    assert queries.get_calls == [(CASE_ID, None, 0)]


def test_factory_wires_all_three_frozen_application_ports() -> None:
    state = _state()
    repository = InMemoryStateRepository(state)
    upload_guard = InMemoryAttachmentUploadGuard()
    publication_guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(
        upload_guard=upload_guard,
        publication_guard=publication_guard,
    )
    service = build_application_service(
        repository=repository,
        resource_store=resources,
        publication_guard=publication_guard,
        upload_guard=upload_guard,
        execution_records=InMemoryExecutionRecordStore(),
        coordinator=ScriptedCoordinator(),
        projector=PureContextSnapshotProjector(),
        asset_catalog=FakeAssetCatalog(),
        dispatcher=RecordingDispatcher(),
        notifier=InMemoryStateChangeNotifier(),
        clock=FakeClock(),
        ids=DeterministicIdGenerator(),
    )

    assert isinstance(service, ApplicationCommandPort)
    assert isinstance(service, ApplicationQueryPort)
    assert isinstance(service, JobControlPort)
