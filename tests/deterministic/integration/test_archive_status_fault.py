"""A second archive-state failure stays observable without repeating ZIP work."""
from __future__ import annotations

from problem_locator.application.errors import port_error
from problem_locator.contracts import ErrorCode
from problem_locator.operational import OperationalState
from tests.deterministic.integration.test_async_archive import pending, _report


def test_archive_failure_and_failed_commit_preserve_report_and_unknown(pending, monkeypatch):
    stack, case_id = pending
    report, before = _report(stack, case_id)
    operational = OperationalState(stack.clock.now)
    operational.install_epoch("archive-epoch")
    stack.archive.operational_state = operational
    stack.application.queries._operational = operational
    calls = []

    def fail_generation(*_):
        calls.append("zip")
        raise port_error(ErrorCode.RESOURCE_STAGE_FAILED, "injected archive generation failure")

    def fail_commit(*_):
        calls.append("commit")
        raise port_error(ErrorCode.STATE_WRITE_FAILED, "injected status write failure")

    monkeypatch.setattr(stack.resources, "stage_archive", fail_generation)
    monkeypatch.setattr(stack.repository, "commit", fail_commit)
    assert stack.archive.run_once()
    assert not stack.archive.run_once()
    assert calls == ["zip", "commit"]
    fault, = operational.faults
    assert fault.error_code is ErrorCode.RESOURCE_STAGE_FAILED
    assert fault.secondary_error_code is ErrorCode.STATE_WRITE_FAILED
    assert (fault.phase, fault.persistence) == ("ARCHIVE_STATUS_COMMIT", "UNKNOWN")
    assert stack.repository.read_case(case_id).case.archive_status == "PENDING"
    assert stack.application.get_case(case_id).case_view.archive_status == "PENDING"
    assert operational.error_for_case(case_id, "RESOLVED", "PENDING") is not None
    assert _report(stack, case_id)[1] == before
    opened = stack.application.open_artifact(case_id, report.artifact_id)
    try:
        assert opened.stream.read(len(before) + 1) == before
    finally:
        opened.stream.close()
