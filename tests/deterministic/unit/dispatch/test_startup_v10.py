"""Startup is independent of abandoned work, including corrupt old outboxes."""
import pytest
from problem_locator.contracts import JobStatus, ErrorCode
from .test_recovery import _coordinator, _state_with_job_status, CURRENT_EPOCH
from ._support import application_port_error


@pytest.mark.parametrize('status', [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.CANCELLED])
@pytest.mark.parametrize('error', [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED,
                                 ErrorCode.EXECUTION_RECORD_FAILED, ErrorCode.ASSET_VERSION_UNAVAILABLE])
def test_startup_does_not_read_validate_replay_or_interrupt_abandoned_work(status, error, monkeypatch):
    coordinator, view, records, application, dispatcher, epoch = _coordinator(_state_with_job_status(status))

    def forbidden(*args, **kwargs):
        raise application_port_error(error)

    monkeypatch.setattr(view, 'read_snapshot', forbidden)
    monkeypatch.setattr(records, 'read_published_outcome', forbidden)
    monkeypatch.setattr(application, 'submit_outcome', forbidden)
    monkeypatch.setattr(application, 'interrupt_recovery', forbidden, raising=False)
    result = coordinator.recover()
    assert result.completed
    assert result.runtime_epoch == epoch.require() == CURRENT_EPOCH
    assert result.replayed_job_ids == result.pending_job_ids == result.interrupted_job_ids == ()
    assert application.operation_log == []
    assert dispatcher.submitted == []
    assert dispatcher.claiming_enabled
