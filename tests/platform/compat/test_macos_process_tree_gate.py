from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sys
import time

import pytest

from problem_locator.application import build_application_service
from problem_locator.contracts import (
    CaseStatus,
    ErrorCode,
    Job,
    JobStatus,
    OutcomeDisposition,
    RuntimeExecutionReceipt,
    StateFile,
    canonical_json_bytes,
)
from problem_locator.dispatch import CancellationController, JobWorker, RuntimeEpochContext
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.runtime.agent_backend import AgentBackend, BackendExecutionLimits
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.outcome_publisher import OutcomePublisher
from problem_locator.storage.coordination import (
    AttachmentUploadRegistry,
    InProcessAttachmentUploadGuard,
    InProcessPublicationCommitGuard,
    StorageCoordinationLock,
)
from problem_locator.storage.execution_records import FileExecutionRecordStore
from problem_locator.storage.layout import StorageLayout
from problem_locator.storage.resource_store import FileResourceStore
from problem_locator.storage.state_repository import JsonFileStateRepository
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeClock,
    InMemoryStateChangeNotifier,
    RecordingDispatcher,
)
from tests.deterministic.contracts.scenario_fakes import assets_for_bindings, bindings_from_job


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/contracts/positive"
FAKE_CLAUDE = ROOT / "tests/fixtures/components/runtime-backend/fake_claude.py"
CASE_ID = "00000000-0000-0000-0000-000000000001"
ROUTE_JOB_ID = "00000000-0000-0000-0000-000000000010"
RUNTIME_EPOCH = "00000000-0000-0000-0000-000000000887"
FIXED_TIME = "2026-07-31T08:17:00.000Z"


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _BackendTimeoutRuntime:
    def __init__(
        self,
        records: FileExecutionRecordStore,
        workspace_root: Path,
    ) -> None:
        command = (
            f"FAKE_CLAUDE_MODE=child-hang {shlex.quote(sys.executable)} "
            f"{shlex.quote(os.fspath(FAKE_CLAUDE))}"
        )
        self.backend = AgentBackend(command)
        self.publisher = OutcomePublisher(
            records,
            FakeClock(FIXED_TIME),
            DeterministicIdGenerator(seed="s08-macos-timeout-outcome"),
        )
        self.records = records
        self.workspace_root = workspace_root
        self.calls: list[Job] = []
        self.failure_code: ErrorCode | None = None
        self.child_pid: int | None = None

    def execute(self, job, cancellation) -> RuntimeExecutionReceipt:
        self.calls.append(job)
        (self.workspace_root / "inputs").mkdir(parents=True)
        (self.workspace_root / "runtime/tool-state").mkdir(parents=True)
        (self.workspace_root / "output/proposals").mkdir(parents=True)
        sinks = self.records.open_log_sinks(
            job.job_id,
            job.resource_limits.stdout_stderr_bytes,
        )
        try:
            self.backend.execute(
                prompt="Execute the fixed timeout process-tree gate.",
                workspace_root=self.workspace_root,
                cancellation=cancellation,
                log_sinks=sinks,
                resource_limits=job.resource_limits,
                test_limits=BackendExecutionLimits(
                    wall_time_seconds=0.25,
                    stdout_stderr_bytes=1024 * 1024,
                    workspace_bytes=1024 * 1024,
                    poll_interval_seconds=0.01,
                    termination_grace_seconds=0.5,
                ),
            )
        except RuntimeExecutionError as exc:
            self.failure_code = exc.failure.code
            marker = self.workspace_root / "output/proposals/child/child.pid"
            self.child_pid = int(marker.read_text(encoding="ascii"))
            return self.publisher.publish_failure(job, exc.failure)
        raise AssertionError("child-hang backend unexpectedly returned success")


@pytest.mark.skipif(os.name == "nt", reason="native POSIX process gate")
def test_posix_timeout_kills_the_real_child_tree_without_rerunning_agent(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    layout = StorageLayout.at(data_root)
    layout.initialize_v2_data_root()
    coordination_lock = StorageCoordinationLock()
    publication_guard = InProcessPublicationCommitGuard(coordination_lock)
    attachment_registry = AttachmentUploadRegistry()
    upload_guard = InProcessAttachmentUploadGuard(attachment_registry)
    ids = DeterministicIdGenerator(seed="s08-macos-tree-storage")
    records = FileExecutionRecordStore(data_root, coordination_lock)
    state = StateFile.model_validate_json(
        (FIXTURES / "state.json").read_text(encoding="utf-8")
    )
    route_job = state.cases[CASE_ID].jobs[ROUTE_JOB_ID]
    with publication_guard.acquire():
        records.publish_job(route_job)
    layout.state.write_bytes(canonical_json_bytes(state))
    repository = JsonFileStateRepository(
        data_root,
        coordination_lock,
        FakeClock(FIXED_TIME),
        ids,
        execution_record_store=records,
    )
    resources = FileResourceStore(
        layout,
        coordination_lock,
        attachment_registry,
        ids,
    )
    bindings = bindings_from_job(route_job)
    application = build_application_service(
        repository=repository,
        resource_store=resources,
        publication_guard=publication_guard,
        upload_guard=upload_guard,
        execution_records=records,
        coordinator=DomainCoordinator(),
        projector=PureContextSnapshotProjector(),
        asset_catalog=FakeAssetCatalog(assets=assets_for_bindings(bindings)),
        dispatcher=RecordingDispatcher(),
        notifier=InMemoryStateChangeNotifier(),
        clock=FakeClock(FIXED_TIME),
        ids=DeterministicIdGenerator(seed="s08-macos-tree-application"),
    )
    runtime = _BackendTimeoutRuntime(records, tmp_path / "workspace")
    epoch = RuntimeEpochContext()
    epoch.install(RUNTIME_EPOCH)
    worker = JobWorker(application, runtime, epoch)

    result = worker.execute_one(ROUTE_JOB_ID, CancellationController())

    assert result.claimed is True
    assert result.runtime_called is True
    assert result.delivery_completed is True
    assert result.outcome_disposition is OutcomeDisposition.APPLIED
    assert runtime.failure_code is ErrorCode.BACKEND_TIMEOUT
    assert len(runtime.calls) == 1
    assert runtime.child_pid is not None
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and _pid_exists(runtime.child_pid):
        time.sleep(0.02)
    assert not _pid_exists(runtime.child_pid)

    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert aggregate.case.status is CaseStatus.INTERRUPTED
    assert aggregate.case.active_job_id is None
    assert aggregate.jobs[ROUTE_JOB_ID].status is JobStatus.INTERRUPTED
    assert len(aggregate.outcomes) == 1
    stored_outcome = next(iter(aggregate.outcomes.values()))
    assert stored_outcome.error is not None
    assert stored_outcome.error.code is ErrorCode.BACKEND_TIMEOUT

    replay = worker.execute_one(ROUTE_JOB_ID, CancellationController())
    assert replay.claimed is False
    assert replay.runtime_called is False
    assert len(runtime.calls) == 1
