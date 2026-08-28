from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from problem_locator.contracts import (
    ApplicationPortError,
    ErrorCode,
    Job,
    JobOutcome,
    ResourceKind,
    ResourceRef,
    ResourceType,
    canonical_json_bytes,
)
from problem_locator.storage.coordination import (
    AttachmentUploadRegistry,
    InProcessPublicationCommitGuard,
    StorageCoordinationLock,
)
from problem_locator.storage.execution_records import FileExecutionRecordStore
from problem_locator.storage.layout import StorageLayout
from problem_locator.storage.paths import proposal_stage_path
from problem_locator.storage.platform import PlatformFileSync
from problem_locator.storage.resource_store import FileResourceStore
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
    InMemoryBinaryStream,
)
from tests.deterministic.integration import test_runtime_dispatch_recovery as recovery_seam


ROOT = Path(__file__).resolve().parents[3]
FAILURE_MATRIX = ROOT / "tests/fixtures/failures/failure-matrix.json"
RESTART_RESOURCE_ID = "00000000-0000-0000-0000-000000000060"


def _failure_scenario(category: str) -> dict[str, Any]:
    payload = json.loads(FAILURE_MATRIX.read_bytes())
    matching = [
        scenario
        for scenario in payload["scenarios"]
        if scenario["category"] == category
    ]
    assert len(matching) == 1
    return matching[0]


_REPLACE_AFTER_POINTS = tuple(
    _failure_scenario("replace_after")["failure_points"]
)


class _FailFinalizationAfterReplace:
    """Delegate real durability except for one armed final-path operation."""

    def __init__(self) -> None:
        self._platform = PlatformFileSync()
        self._failure_point: str | None = None
        self._expected_path: Path | None = None
        self.failed_paths: list[Path] = []

    @staticmethod
    def _path(path_or_handle: object) -> Path:
        if isinstance(path_or_handle, (str, os.PathLike)):
            return Path(path_or_handle)
        name = getattr(path_or_handle, "name", None)
        if isinstance(name, (str, os.PathLike)):
            return Path(name)
        raise TypeError("file sync target must expose a filesystem path")

    def arm(self, failure_point: str, expected_path: Path) -> None:
        assert failure_point in {"chmod", "fsync"}
        assert self._failure_point is None
        self._failure_point = failure_point
        self._expected_path = expected_path

    def _fail_if_armed(self, operation: str, path: Path) -> None:
        if (
            self._failure_point == operation
            and self._expected_path == path
        ):
            assert path.exists()
            self.failed_paths.append(path)
            self._failure_point = None
            self._expected_path = None
            raise OSError(f"injected post-replace {operation} failure")

    def sync_file(self, path_or_handle: object) -> None:
        path = self._path(path_or_handle)
        self._fail_if_armed("fsync", path)
        self._platform.sync_file(path_or_handle)  # type: ignore[arg-type]

    def sync_directory(self, path: Path) -> None:
        self._platform.sync_directory(path)

    def make_read_only(self, path: Path) -> None:
        self._fail_if_armed("chmod", path)
        self._platform.make_read_only(path)


@pytest.mark.parametrize("failure_point", _REPLACE_AFTER_POINTS)
def test_real_storage_adapters_restart_finalize_post_replace_publications(
    tmp_path: Path,
    failure_point: str,
) -> None:
    resource_root = tmp_path / f"resource-{failure_point}"
    resource_layout = StorageLayout.at(resource_root)
    resource_layout.ensure_directories()
    first_resource_lock = StorageCoordinationLock()
    first_resource_guard = InProcessPublicationCommitGuard(first_resource_lock)
    first_resource_registry = AttachmentUploadRegistry()
    resource_sync = _FailFinalizationAfterReplace()
    first_resources = FileResourceStore(
        resource_layout,
        first_resource_lock,
        first_resource_registry,
        DeterministicIdGenerator(seed=f"s08-post-replace-{failure_point}"),
        file_sync=resource_sync,
    )
    resource_bytes = b"post-replace resource bytes\n"
    staged = first_resources.stage_file(
        recovery_seam.ROUTE_JOB_ID,
        "post_replace_resource",
        InMemoryBinaryStream(resource_bytes),
        expected_size=len(resource_bytes),
    )
    target = first_resources.plan_target(
        recovery_seam.CASE_ID,
        ResourceType.ARTIFACT,
        RESTART_RESOURCE_ID,
        ResourceKind.FILE,
        staged.size,
        staged.sha256,
    )
    formal_path = resource_root / target.final_storage_key
    resource_sync.arm(failure_point, formal_path)

    with pytest.raises(ApplicationPortError) as resource_failure:
        with first_resource_guard.acquire():
            first_resources.validate_case_capacity(
                recovery_seam.CASE_ID,
                [target],
            )
            first_resources.publish(staged, target.final_storage_key)
    assert resource_failure.value.error.code is ErrorCode.RESOURCE_PUBLISH_FAILED
    assert resource_sync.failed_paths == [formal_path]
    assert formal_path.read_bytes() == resource_bytes
    assert not (
        proposal_stage_path(
            resource_root,
            recovery_seam.ROUTE_JOB_ID,
            staged.proposal_key,
        )
        / "payload"
    ).exists()

    restarted_resource_lock = StorageCoordinationLock()
    restarted_resource_guard = InProcessPublicationCommitGuard(
        restarted_resource_lock
    )
    restarted_resources = FileResourceStore(
        resource_layout,
        restarted_resource_lock,
        AttachmentUploadRegistry(),
        DeterministicIdGenerator(
            seed=f"s08-post-replace-{failure_point}-restart"
        ),
    )
    with restarted_resource_guard.acquire():
        replay_usage = restarted_resources.validate_case_capacity(
            recovery_seam.CASE_ID,
            [target],
        )
        resource_receipt = restarted_resources.publish(
            staged,
            target.final_storage_key,
        )
    assert replay_usage.new_bytes == 0
    assert resource_receipt == ResourceRef(
        resource_kind=ResourceKind.FILE,
        storage_key=target.final_storage_key,
        size=len(resource_bytes),
        sha256=staged.sha256,
    )
    assert stat.S_IMODE(formal_path.stat().st_mode) & 0o222 == 0
    opened = restarted_resources.open_read(resource_receipt)
    try:
        assert opened.read(len(resource_bytes) + 1) == resource_bytes
        assert opened.read(1) == b""
    finally:
        opened.close()

    record_root = tmp_path / f"records-{failure_point}"
    record_layout = StorageLayout.at(record_root)
    record_layout.ensure_directories()
    job = Job.model_validate(recovery_seam._json("job-route.json"))
    job_bytes = canonical_json_bytes(job)
    first_job_lock = StorageCoordinationLock()
    first_job_guard = InProcessPublicationCommitGuard(first_job_lock)
    job_sync = _FailFinalizationAfterReplace()
    first_job_records = FileExecutionRecordStore(
        record_root,
        first_job_lock,
        file_sync=job_sync,
        temp_token_factory=lambda: f"job-{failure_point}",
    )
    job_path = record_root / "jobs" / job.job_id / "job.json"
    job_sync.arm(failure_point, job_path)

    with pytest.raises(ApplicationPortError) as job_failure:
        with first_job_guard.acquire():
            first_job_records.publish_job(job)
    assert job_failure.value.error.code is ErrorCode.EXECUTION_RECORD_FAILED
    assert job_sync.failed_paths == [job_path]
    assert job_path.read_bytes() == job_bytes

    restarted_job_lock = StorageCoordinationLock()
    restarted_job_guard = InProcessPublicationCommitGuard(restarted_job_lock)
    restarted_job_records = FileExecutionRecordStore(
        record_root,
        restarted_job_lock,
        temp_token_factory=lambda: f"job-{failure_point}-restart",
    )
    with restarted_job_guard.acquire():
        job_ref = restarted_job_records.publish_job(job)
    published_job = restarted_job_records.read_published_job(job.job_id)
    assert published_job is not None
    assert published_job.job_file_ref == job_ref
    assert canonical_json_bytes(published_job.job) == job_bytes
    assert job_path.read_bytes() == job_bytes
    assert stat.S_IMODE(job_path.stat().st_mode) & 0o222 == 0

    outcome = JobOutcome.model_validate(
        recovery_seam._json("job-outcome-route.json")
    )
    outcome_bytes = canonical_json_bytes(outcome)
    outcome_path = record_root / "jobs" / job.job_id / "job_outcome.json"
    first_outcome_lock = StorageCoordinationLock()
    outcome_sync = _FailFinalizationAfterReplace()
    first_outcome_records = FileExecutionRecordStore(
        record_root,
        first_outcome_lock,
        file_sync=outcome_sync,
        temp_token_factory=lambda: f"outcome-{failure_point}",
    )
    outcome_sync.arm(failure_point, outcome_path)

    with pytest.raises(ApplicationPortError) as outcome_failure:
        first_outcome_records.publish_outcome_bytes(job.job_id, outcome_bytes)
    assert outcome_failure.value.error.code is ErrorCode.EXECUTION_RECORD_FAILED
    assert outcome_sync.failed_paths == [outcome_path]
    assert outcome_path.read_bytes() == outcome_bytes

    restarted_outcome_lock = StorageCoordinationLock()
    restarted_outcome_records = FileExecutionRecordStore(
        record_root,
        restarted_outcome_lock,
        temp_token_factory=lambda: f"outcome-{failure_point}-restart",
    )
    outcome_ref = restarted_outcome_records.publish_outcome_bytes(
        job.job_id,
        outcome_bytes,
    )
    published_outcome = restarted_outcome_records.read_published_outcome(job.job_id)
    assert published_outcome is not None
    assert published_outcome.outcome_file_ref == outcome_ref
    assert canonical_json_bytes(published_outcome.job_outcome) == outcome_bytes
    assert outcome_path.read_bytes() == outcome_bytes
    assert stat.S_IMODE(outcome_path.stat().st_mode) & 0o222 == 0
