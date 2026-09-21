"""Core resource IO owns a Case lease until bytes and streams are released."""

from __future__ import annotations

import gc
import threading
from contextlib import contextmanager

import pytest

from problem_locator.application import queries
from problem_locator.contracts import ApplicationPortError, ErrorCode
from tests.deterministic.contracts.fakes import InMemoryBinaryStream, InMemoryResourceStore, InMemoryStateRepository
from tests.deterministic.unit.application.test_queries import (
    ARTIFACT_ID, CASE_ID, _Notifier, _diagnostic_artifact, _service, _state, _with_artifact,
)
from tests.deterministic.unit.application.test_uploads import (
    PAYLOAD, _CountingRepository, _application_error, _command, _rig, _state as _upload_state,
)
from tests.deterministic.unit.storage.test_state_repository import _open


@pytest.fixture
def usage_repository(tmp_path):
    repository = _open(tmp_path)
    yield repository
    repository.close()


def _assert_idle(repository):
    lease = repository.case_cleanup_if_idle(CASE_ID)
    assert lease is not None
    with lease:
        pass


def _download(usage_repository, monkeypatch, *, stream=None, open_failure=None):
    payload = b"{}"
    repository = InMemoryStateRepository(_with_artifact(_state(), _diagnostic_artifact(payload)))
    repository.case_usage = usage_repository.case_usage
    store = InMemoryResourceStore()
    body = stream or InMemoryBinaryStream(payload)

    def open_read(resource):
        assert usage_repository.case_cleanup_if_idle(CASE_ID) is None
        if open_failure is not None:
            raise open_failure
        return body

    monkeypatch.setattr(store, "open_read", open_read)
    return _service(repository, store, _Notifier()), body


def test_download_blocks_cleanup_until_stream_closes_on_another_thread(usage_repository, monkeypatch):
    service, body = _download(usage_repository, monkeypatch)
    opened = service.open_artifact(CASE_ID, ARTIFACT_ID)
    assert usage_repository.case_cleanup_if_idle(CASE_ID) is None
    assert opened.stream.read(1) == b"{"
    assert usage_repository.case_cleanup_if_idle(CASE_ID) is None

    closer = threading.Thread(target=opened.stream.close)
    closer.start()
    closer.join(timeout=2)
    assert not closer.is_alive()
    opened.stream.close()

    assert body.close_calls == 1
    _assert_idle(usage_repository)


@pytest.mark.parametrize("failure_stage", ["metadata", "open", "read", "close"])
def test_download_failures_release_case_usage(usage_repository, monkeypatch, failure_stage):
    class FailingClose(InMemoryBinaryStream):
        def close(self):
            super().close()
            raise OSError("close failed")

    body = (FailingClose(b"{}") if failure_stage == "close" else
        InMemoryBinaryStream(b"{}", fail_on_read_number=1 if failure_stage == "read" else None))
    open_failure = _application_error(ErrorCode.RESOURCE_NOT_FOUND) if failure_stage == "open" else None
    service, body = _download(usage_repository, monkeypatch, stream=body, open_failure=open_failure)
    if failure_stage in {"metadata", "open"}:
        artifact_id = "00000000-0000-0000-0000-000000000999" if failure_stage == "metadata" else ARTIFACT_ID
        with pytest.raises(ApplicationPortError):
            service.open_artifact(CASE_ID, artifact_id)
    else:
        opened = service.open_artifact(CASE_ID, ARTIFACT_ID)
        with pytest.raises(OSError):
            opened.stream.read(1) if failure_stage == "read" else opened.stream.close()
        opened.stream.close()
        assert body.close_calls == 1
    _assert_idle(usage_repository)


def test_abandoned_download_releases_its_stream_and_case_usage(usage_repository, monkeypatch):
    service, body = _download(usage_repository, monkeypatch)
    opened = service.open_artifact(CASE_ID, ARTIFACT_ID)
    assert usage_repository.case_cleanup_if_idle(CASE_ID) is None
    del opened
    gc.collect()
    assert body.closed
    _assert_idle(usage_repository)


@pytest.mark.parametrize("operation", ["get_report", "read_conversation_delivery"])
@pytest.mark.parametrize("fails", [False, True])
def test_report_reads_hold_case_usage_and_release_on_error(usage_repository, monkeypatch, operation, fails):
    service, _ = _download(usage_repository, monkeypatch)
    expected = object()

    def report(aggregate, store):
        assert usage_repository.case_cleanup_if_idle(CASE_ID) is None
        if fails:
            raise OSError("report read failed")
        return expected

    monkeypatch.setattr(queries, "read_published_report", report)

    def read():
        if operation == "get_report":
            return service.get_report(CASE_ID)
        return service.read_conversation_delivery(CASE_ID, _state(), report=True, artifacts=False)[1]

    if fails:
        with pytest.raises(OSError):
            read()
    else:
        assert read() is expected
    _assert_idle(usage_repository)


@pytest.mark.parametrize("failure_stage", [None, "read", "close"])
def test_upload_holds_case_usage_during_receive_publish_and_close(usage_repository, monkeypatch, failure_stage):
    repository = _CountingRepository(_upload_state())
    repository.case_usage = usage_repository.case_usage
    service, _, resources, _, _, _, _ = _rig(repository=repository)

    class ObservedBody(InMemoryBinaryStream):
        def read(self, max_bytes):
            assert usage_repository.case_cleanup_if_idle(CASE_ID) is None
            if failure_stage == "read":
                raise OSError("receive failed")
            return super().read(max_bytes)

        def close(self):
            assert usage_repository.case_cleanup_if_idle(CASE_ID) is None
            super().close()
            if failure_stage == "close":
                raise OSError("close failed")

    publish = resources.publish

    def observed_publish(*args):
        assert usage_repository.case_cleanup_if_idle(CASE_ID) is None
        return publish(*args)

    monkeypatch.setattr(resources, "publish", observed_publish)
    body = ObservedBody(PAYLOAD)
    if failure_stage == "read":
        with pytest.raises(ApplicationPortError) as captured:
            service.execute(_command(body))
        assert captured.value.error.code is ErrorCode.UPLOAD_INCOMPLETE
    else:
        assert service.execute(_command(body)).status == "READY"
    assert body.closed
    _assert_idle(usage_repository)


def test_upload_rechecks_owner_after_acquiring_case_usage(usage_repository):
    repository = _CountingRepository(_upload_state())

    @contextmanager
    def case_usage(case_id):
        with usage_repository.case_usage(case_id):
            # Cleanup completed between the preliminary owner lookup and lease.
            repository.seed(_upload_state().model_copy(update={"cases": {}}))
            yield

    repository.case_usage = case_usage
    service, _, _, _, _, _, _ = _rig(repository=repository)
    body = InMemoryBinaryStream(PAYLOAD)
    with pytest.raises(ApplicationPortError) as captured:
        service.execute(_command(body))
    assert captured.value.error.code is ErrorCode.ATTACHMENT_NOT_FOUND
    assert body.read_requests == [] and body.closed
    _assert_idle(usage_repository)
