from __future__ import annotations

import hashlib
import inspect
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Iterator, Self

import pytest

from problem_locator.contracts import (
    ApplicationPortError,
    ErrorCode,
    ExecutionRecordStore,
    Job,
    JobOutcome,
    JobStatus,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.contracts.limits import JOB_STDOUT_STDERR_BYTES
from problem_locator.storage.coordination import (
    InProcessPublicationCommitGuard,
    StorageCoordinationLock,
)
from problem_locator.storage.execution_records import FileExecutionRecordStore
from problem_locator.storage.platform import PlatformFileSync
from tests.deterministic.unit.storage.fakes import FakeFileSync
from tests.deterministic.unit.storage.platform_support import symlink_or_skip


REPOSITORY_ROOT = Path(__file__).parents[4]
CONTRACT_FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "contracts" / "positive"
ROUTE_JOB_ID = "00000000-0000-0000-0000-000000000010"
DIAGNOSE_JOB_ID = "00000000-0000-0000-0000-000000000011"


def _fixture_bytes(name: str) -> bytes:
    return (CONTRACT_FIXTURES / name).read_bytes()


def _route_job() -> Job:
    return parse_canonical_json_bytes(_fixture_bytes("job-route.json"), Job)


def _route_outcome_bytes() -> bytes:
    return _fixture_bytes("job-outcome-route.json")


@contextmanager
def _raises_port_error(code: ErrorCode) -> Iterator[None]:
    with pytest.raises(ApplicationPortError) as caught:
        yield
    assert caught.value.error.code is code


class CoordinationLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._local = threading.local()

    def __enter__(self) -> Self:
        self._lock.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._lock.release()

    def publication_held_by_current_thread(self) -> bool:
        return getattr(self._local, "publication_depth", 0) > 0

    @contextmanager
    def publication(self) -> Iterator[None]:
        with self:
            self._local.publication_depth = (
                getattr(self._local, "publication_depth", 0) + 1
            )
            try:
                yield
            finally:
                self._local.publication_depth -= 1


class RecordingFileSync:
    def __init__(self) -> None:
        self.file_calls: list[Path] = []
        self.directory_calls: list[Path] = []
        self.read_only_calls: list[Path] = []
        self._platform = PlatformFileSync()

    @staticmethod
    def _sync_path(path_or_handle: object) -> Path:
        if isinstance(path_or_handle, (str, os.PathLike)):
            return Path(path_or_handle)
        name = getattr(path_or_handle, "name", None)
        if isinstance(name, (str, os.PathLike)):
            return Path(name)
        raise TypeError("file-sync target must be a path or named handle")

    def sync_file(self, path_or_handle: object) -> None:
        path = self._sync_path(path_or_handle)
        self.file_calls.append(path)
        self._platform.sync_file(path_or_handle)  # type: ignore[arg-type]

    def sync_directory(self, path: Path) -> None:
        self.directory_calls.append(path)
        self._platform.sync_directory(path)

    def make_read_only(self, path: Path) -> None:
        self.read_only_calls.append(path)
        self._platform.make_read_only(path)

    def clear(self) -> None:
        self.file_calls.clear()
        self.directory_calls.clear()
        self.read_only_calls.clear()


class RecordingReplacer:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    def replace(self, source: Path, destination: Path) -> None:
        self.calls.append((source, destination))
        os.replace(source, destination)


class FailFirstReadOnly(RecordingFileSync):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def make_read_only(self, path: Path) -> None:
        self.read_only_calls.append(path)
        if not self.failed:
            self.failed = True
            raise OSError("injected chmod failure")
        self._platform.make_read_only(path)


class NoOpReadOnly(RecordingFileSync):
    def make_read_only(self, path: Path) -> None:
        self.read_only_calls.append(path)


class FailFirstFileSync(RecordingFileSync):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def sync_file(self, path_or_handle: object) -> None:
        path = self._sync_path(path_or_handle)
        self.file_calls.append(path)
        if not self.failed:
            self.failed = True
            raise OSError("injected fsync failure")
        self._platform.sync_file(path_or_handle)  # type: ignore[arg-type]


class DurableFaultFileSync(FakeFileSync):
    """Fault-scheduled sync double that also performs the filesystem effect."""

    def __init__(self) -> None:
        super().__init__()
        self._platform = PlatformFileSync()

    def sync_file(self, path_or_handle: object) -> None:
        super().sync_file(path_or_handle)  # type: ignore[arg-type]
        self._platform.sync_file(path_or_handle)  # type: ignore[arg-type]

    def sync_directory(self, path: Path) -> None:
        super().sync_directory(path)
        self._platform.sync_directory(path)

    def make_read_only(self, path: Path) -> None:
        super().make_read_only(path)
        self._platform.make_read_only(path)


class FailingReplacer:
    def replace(self, source: Path, destination: Path) -> None:
        raise OSError("injected replace failure")


@pytest.fixture
def coordination() -> CoordinationLock:
    return CoordinationLock()


def _store(
    root: Path,
    coordination: CoordinationLock,
    *,
    file_sync: RecordingFileSync | None = None,
    replacer: object | None = None,
) -> FileExecutionRecordStore:
    return FileExecutionRecordStore(
        root,
        coordination,
        file_sync,
        replacer,  # type: ignore[arg-type]
        temp_token_factory=lambda: "fixed-token",
    )


def test_public_methods_match_frozen_execution_record_port_signatures() -> None:
    expected_parameters = {
        "publish_job": ["self", "job"],
        "publish_outcome_bytes": ["self", "job_id", "canonical_bytes"],
        "publish_rejected_agent_output_bytes": ["self", "job_id", "raw_bytes"],
        "read_published_job": ["self", "job_id"],
        "read_published_outcome": ["self", "job_id"],
        "open_log_sinks": ["self", "job_id", "combined_limit_bytes"],
    }
    for method_name, parameter_names in expected_parameters.items():
        implementation = getattr(FileExecutionRecordStore, method_name)
        port = getattr(ExecutionRecordStore, method_name)
        assert list(inspect.signature(implementation).parameters) == parameter_names
        assert list(inspect.signature(port).parameters) == parameter_names


def test_publish_job_requires_publication_lease_without_side_effects(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    store = _store(tmp_path, coordination)

    with _raises_port_error(ErrorCode.EXECUTION_RECORD_FAILED):
        store.publish_job(_route_job())

    assert not (tmp_path / "jobs").exists()


def test_publish_job_writes_canonical_read_only_file_and_reads_receipt(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    file_sync = RecordingFileSync()
    replacer = RecordingReplacer()
    store = _store(
        tmp_path,
        coordination,
        file_sync=file_sync,
        replacer=replacer,
    )
    job = _route_job()

    with coordination.publication():
        file_ref = store.publish_job(job)

    final_path = tmp_path / "jobs" / ROUTE_JOB_ID / "job.json"
    expected_bytes = canonical_json_bytes(job)
    assert final_path.read_bytes() == expected_bytes
    assert stat.S_IMODE(final_path.stat().st_mode) & 0o222 == 0
    assert file_ref.relative_key == f"jobs/{ROUTE_JOB_ID}/job.json"
    assert file_ref.size == len(expected_bytes)
    assert len(replacer.calls) == 1

    receipt = store.read_published_job(ROUTE_JOB_ID)
    assert receipt is not None
    assert receipt.job == job
    assert receipt.job_file_ref == file_ref


@pytest.mark.parametrize("sync_occurrence", [1, 2])
def test_directory_creation_sync_failure_is_reapplied_on_publish_retry(
    tmp_path: Path,
    coordination: CoordinationLock,
    sync_occurrence: int,
) -> None:
    sync = DurableFaultFileSync()
    sync.fail_on(
        "sync_directory",
        sync_occurrence,
        OSError("directory creation sync failed"),
    )
    store = FileExecutionRecordStore(
        tmp_path,
        coordination,
        sync,
        temp_token_factory=lambda: "directory-sync-retry",
    )

    with coordination.publication(), _raises_port_error(
        ErrorCode.EXECUTION_RECORD_FAILED
    ):
        store.publish_job(_route_job())

    with coordination.publication():
        published = store.publish_job(_route_job())
    assert published.relative_key == f"jobs/{ROUTE_JOB_ID}/job.json"


def test_publish_job_composes_with_shared_s02_lock_guard_and_platform(
    tmp_path: Path,
) -> None:
    coordination_lock = StorageCoordinationLock()
    publication_guard = InProcessPublicationCommitGuard(coordination_lock)
    store = FileExecutionRecordStore(
        tmp_path,
        coordination_lock,
        temp_token_factory=lambda: "shared-adapters",
    )

    with publication_guard.acquire():
        file_ref = store.publish_job(_route_job())

    receipt = store.read_published_job(ROUTE_JOB_ID)
    assert receipt is not None
    assert receipt.job_file_ref == file_ref


def test_same_job_bytes_are_adopted_and_finalize_is_reapplied(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    file_sync = RecordingFileSync()
    replacer = RecordingReplacer()
    store = _store(
        tmp_path,
        coordination,
        file_sync=file_sync,
        replacer=replacer,
    )
    job = _route_job()
    with coordination.publication():
        first = store.publish_job(job)

    file_sync.clear()
    with coordination.publication():
        adopted = store.publish_job(job)

    final_path = tmp_path / "jobs" / ROUTE_JOB_ID / "job.json"
    assert adopted == first
    assert replacer.calls and len(replacer.calls) == 1
    assert file_sync.read_only_calls == [final_path]
    assert file_sync.file_calls == [final_path]
    assert file_sync.directory_calls == [
        tmp_path,
        tmp_path / "jobs",
        final_path.parent,
    ]


def test_different_valid_job_bytes_conflict_without_overwrite(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    store = _store(tmp_path, coordination)
    job = _route_job()
    different_job = Job.model_validate(
        {**job.model_dump(mode="python"), "goal": "A different immutable goal."}
    )
    with coordination.publication():
        store.publish_job(job)
    final_path = tmp_path / "jobs" / ROUTE_JOB_ID / "job.json"
    original_bytes = final_path.read_bytes()

    with coordination.publication(), _raises_port_error(
        ErrorCode.IDEMPOTENCY_CONFLICT
    ):
        store.publish_job(different_job)

    assert final_path.read_bytes() == original_bytes


def test_non_pending_job_is_rejected_before_filesystem_publication(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    job = _route_job()
    running_job = Job.model_validate(
        {
            **job.model_dump(mode="python"),
            "status": JobStatus.RUNNING,
            "started_at": "2026-07-31T00:00:01.000Z",
            "runtime_epoch": "00000000-0000-0000-0000-000000000090",
        }
    )
    store = _store(tmp_path, coordination)

    with coordination.publication(), _raises_port_error(
        ErrorCode.EXECUTION_RECORD_FAILED
    ):
        store.publish_job(running_job)

    assert not (tmp_path / "jobs").exists()


def test_replace_success_then_finalize_failure_is_completed_by_retry(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    file_sync = FailFirstReadOnly()
    store = _store(tmp_path, coordination, file_sync=file_sync)
    job = _route_job()

    with coordination.publication(), _raises_port_error(
        ErrorCode.EXECUTION_RECORD_FAILED
    ):
        store.publish_job(job)

    final_path = tmp_path / "jobs" / ROUTE_JOB_ID / "job.json"
    assert final_path.read_bytes() == canonical_json_bytes(job)
    assert not list(final_path.parent.glob("*.tmp"))

    with coordination.publication():
        receipt_ref = store.publish_job(job)

    assert receipt_ref.relative_key == f"jobs/{ROUTE_JOB_ID}/job.json"
    assert stat.S_IMODE(final_path.stat().st_mode) & 0o222 == 0


def test_noop_read_only_adapter_never_yields_a_publication_receipt(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    replacer = RecordingReplacer()
    unsafe_store = _store(
        tmp_path,
        coordination,
        file_sync=NoOpReadOnly(),
        replacer=replacer,
    )
    job = _route_job()

    with coordination.publication(), _raises_port_error(
        ErrorCode.EXECUTION_RECORD_FAILED
    ):
        unsafe_store.publish_job(job)

    final_path = tmp_path / "jobs" / ROUTE_JOB_ID / "job.json"
    assert final_path.read_bytes() == canonical_json_bytes(job)
    assert stat.S_IMODE(final_path.stat().st_mode) & 0o222

    safe_store = _store(
        tmp_path,
        coordination,
        file_sync=RecordingFileSync(),
        replacer=replacer,
    )
    with coordination.publication():
        safe_store.publish_job(job)

    assert stat.S_IMODE(final_path.stat().st_mode) & 0o222 == 0
    assert len(replacer.calls) == 1


def test_replace_failure_leaves_no_final_or_temp_record(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    store = _store(tmp_path, coordination, replacer=FailingReplacer())

    with coordination.publication(), _raises_port_error(
        ErrorCode.EXECUTION_RECORD_FAILED
    ):
        store.publish_job(_route_job())

    job_directory = tmp_path / "jobs" / ROUTE_JOB_ID
    assert not (job_directory / "job.json").exists()
    assert list(job_directory.iterdir()) == []


def test_publish_and_read_outcome_do_not_require_publication_lease(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    store = _store(tmp_path, coordination)
    canonical_bytes = _route_outcome_bytes()

    file_ref = store.publish_outcome_bytes(ROUTE_JOB_ID, canonical_bytes)

    final_path = tmp_path / "jobs" / ROUTE_JOB_ID / "job_outcome.json"
    assert final_path.read_bytes() == canonical_bytes
    assert stat.S_IMODE(final_path.stat().st_mode) & 0o222 == 0
    assert file_ref.relative_key == f"jobs/{ROUTE_JOB_ID}/job_outcome.json"
    receipt = store.read_published_outcome(ROUTE_JOB_ID)
    assert receipt is not None
    assert receipt.job_outcome == parse_canonical_json_bytes(
        canonical_bytes,
        JobOutcome,
    )
    assert receipt.outcome_file_ref == file_ref


def test_same_outcome_is_adopted_but_different_valid_outcome_conflicts(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    file_sync = RecordingFileSync()
    replacer = RecordingReplacer()
    store = _store(
        tmp_path,
        coordination,
        file_sync=file_sync,
        replacer=replacer,
    )
    canonical_bytes = _route_outcome_bytes()
    first = store.publish_outcome_bytes(ROUTE_JOB_ID, canonical_bytes)
    file_sync.clear()

    adopted = store.publish_outcome_bytes(ROUTE_JOB_ID, canonical_bytes)

    final_path = tmp_path / "jobs" / ROUTE_JOB_ID / "job_outcome.json"
    assert adopted == first
    assert len(replacer.calls) == 1
    assert file_sync.read_only_calls == [final_path]
    assert file_sync.file_calls == [final_path]
    assert file_sync.directory_calls == [
        tmp_path,
        tmp_path / "jobs",
        final_path.parent,
    ]

    conflicting_bytes = _fixture_bytes("job-outcome-failure.json")
    with _raises_port_error(ErrorCode.IDEMPOTENCY_CONFLICT):
        store.publish_outcome_bytes(ROUTE_JOB_ID, conflicting_bytes)
    assert final_path.read_bytes() == canonical_bytes


def test_rejected_agent_output_is_archived_exactly_and_idempotently(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    replacer = RecordingReplacer()
    store = _store(
        tmp_path,
        coordination,
        replacer=replacer,
    )
    raw_bytes = b'{\n  "not": "canonical"\n}\n'

    first = store.publish_rejected_agent_output_bytes(ROUTE_JOB_ID, raw_bytes)
    final_path = (
        tmp_path / "jobs" / ROUTE_JOB_ID / "agent_job_outcome.rejected.json"
    )
    assert final_path.read_bytes() == raw_bytes
    assert stat.S_IMODE(final_path.stat().st_mode) & 0o222 == 0
    assert first.relative_key == (
        f"jobs/{ROUTE_JOB_ID}/agent_job_outcome.rejected.json"
    )
    assert first.size == len(raw_bytes)
    assert first.sha256 == hashlib.sha256(raw_bytes).hexdigest()

    adopted = store.publish_rejected_agent_output_bytes(ROUTE_JOB_ID, raw_bytes)
    assert adopted == first
    assert len(replacer.calls) == 1

    with _raises_port_error(ErrorCode.IDEMPOTENCY_CONFLICT):
        store.publish_rejected_agent_output_bytes(ROUTE_JOB_ID, b"different")
    assert final_path.read_bytes() == raw_bytes


def test_rejected_agent_output_replace_failure_leaves_no_partial_file(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    store = _store(tmp_path, coordination, replacer=FailingReplacer())

    with _raises_port_error(ErrorCode.EXECUTION_RECORD_FAILED):
        store.publish_rejected_agent_output_bytes(ROUTE_JOB_ID, b"invalid")

    job_directory = tmp_path / "jobs" / ROUTE_JOB_ID
    assert list(job_directory.iterdir()) == []


@pytest.mark.parametrize(
    "filename",
    (
        "method-diagnosis.draft.json",
        "method-review.draft.json",
        "logparse_broker_audit.json",
        "method-grounding-audit.json",
        "methods-consensus-attribution-v2.json",
        "methods-limitations-v2.json",
        "methods_logparse_receipt.json",
        "methods_preflight.json",
        "methods_request.json",
        "methods_target_logs.json",
    ),
)
def test_methods_runtime_records_are_allowlisted_for_publish_and_audit_read(
    filename: str,
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    store = _store(tmp_path, coordination)
    payload = f"observable:{filename}".encode()

    ref = store.publish_audit_bytes(DIAGNOSE_JOB_ID, filename, payload)

    assert ref.relative_key == f"jobs/{DIAGNOSE_JOB_ID}/{filename}"
    assert store.read_audit_bytes(DIAGNOSE_JOB_ID, filename) == payload


@pytest.mark.parametrize(
    ("job_id", "payload"),
    [
        (DIAGNOSE_JOB_ID, _route_outcome_bytes()),
        (ROUTE_JOB_ID, _route_outcome_bytes().rstrip(b"\n")),
        (ROUTE_JOB_ID, b"{}\n"),
    ],
)
def test_invalid_outcome_bytes_are_rejected_before_filesystem_publication(
    tmp_path: Path,
    coordination: CoordinationLock,
    job_id: str,
    payload: bytes,
) -> None:
    store = _store(tmp_path, coordination)

    with _raises_port_error(ErrorCode.EXECUTION_RECORD_FAILED):
        store.publish_outcome_bytes(job_id, payload)

    assert not (tmp_path / "jobs").exists()


def test_reads_ignore_temporary_files_and_return_none_for_missing_final(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    job_directory = tmp_path / "jobs" / ROUTE_JOB_ID
    job_directory.mkdir(parents=True)
    (job_directory / ".job.json.interrupted.tmp").write_bytes(
        _fixture_bytes("job-route.json")
    )
    (job_directory / "job_outcome.json.part").write_bytes(_route_outcome_bytes())
    store = _store(tmp_path, coordination)

    assert store.read_published_job(ROUTE_JOB_ID) is None
    assert store.read_published_outcome(ROUTE_JOB_ID) is None


@pytest.mark.parametrize("kind", ["noncanonical", "wrong-id", "directory", "hardlink"])
def test_read_job_rejects_corrupt_or_non_ordinary_final_files(
    tmp_path: Path,
    coordination: CoordinationLock,
    kind: str,
) -> None:
    job_directory = tmp_path / "jobs" / ROUTE_JOB_ID
    job_directory.mkdir(parents=True)
    final_path = job_directory / "job.json"
    if kind == "noncanonical":
        final_path.write_bytes(_fixture_bytes("job-route.json").rstrip(b"\n"))
    elif kind == "wrong-id":
        final_path.write_bytes(_fixture_bytes("job-diagnose.json"))
    elif kind == "directory":
        final_path.mkdir()
    else:
        source = tmp_path / "hardlink-source"
        source.write_bytes(_fixture_bytes("job-route.json"))
        os.link(source, final_path)

    store = _store(tmp_path, coordination)
    with _raises_port_error(ErrorCode.EXECUTION_RECORD_FAILED):
        store.read_published_job(ROUTE_JOB_ID)


def test_read_rejects_final_file_and_job_directory_symlinks(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    target = tmp_path / "target-job.json"
    target.write_bytes(_fixture_bytes("job-route.json"))
    job_directory = tmp_path / "jobs" / ROUTE_JOB_ID
    job_directory.mkdir(parents=True)
    symlink_or_skip(job_directory / "job.json", target)
    store = _store(tmp_path, coordination)

    with _raises_port_error(ErrorCode.EXECUTION_RECORD_FAILED):
        store.read_published_job(ROUTE_JOB_ID)

    (job_directory / "job.json").unlink()
    job_directory.rmdir()
    symlink_or_skip(job_directory, tmp_path, target_is_directory=True)
    with _raises_port_error(ErrorCode.EXECUTION_RECORD_FAILED):
        store.read_published_job(ROUTE_JOB_ID)


def test_open_log_sinks_atomically_creates_pair_and_appends_bytes(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    store = _store(tmp_path, coordination)
    sinks = store.open_log_sinks(ROUTE_JOB_ID, JOB_STDOUT_STDERR_BYTES)
    stdout_path = tmp_path / "jobs" / ROUTE_JOB_ID / "stdout.log"
    stderr_path = tmp_path / "jobs" / ROUTE_JOB_ID / "stderr.log"
    assert stdout_path.read_bytes() == b""
    assert stderr_path.read_bytes() == b""

    assert sinks.stdout.write(b"stdout") is None
    assert sinks.stderr.write(b"stderr") is None
    sinks.stdout.flush()
    sinks.stderr.flush()
    assert stdout_path.read_bytes() == b"stdout"
    assert stderr_path.read_bytes() == b"stderr"

    with _raises_port_error(ErrorCode.EXECUTION_RECORD_FAILED):
        store.open_log_sinks(ROUTE_JOB_ID, JOB_STDOUT_STDERR_BYTES)

    sinks.stdout.close()
    sinks.stdout.flush()
    sinks.stdout.close()
    with _raises_port_error(ErrorCode.EXECUTION_RECORD_FAILED):
        store.open_log_sinks(ROUTE_JOB_ID, JOB_STDOUT_STDERR_BYTES)
    sinks.stderr.close()
    sinks.stderr.flush()
    sinks.stderr.close()

    with _raises_port_error(ErrorCode.EXECUTION_RECORD_FAILED):
        store.open_log_sinks(ROUTE_JOB_ID, JOB_STDOUT_STDERR_BYTES)
    assert stdout_path.read_bytes() == b"stdout"
    assert stderr_path.read_bytes() == b"stderr"


def test_log_sinks_share_exact_limit_and_enforce_sink_protocol(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    store = _store(tmp_path, coordination)
    sinks = store.open_log_sinks(ROUTE_JOB_ID, JOB_STDOUT_STDERR_BYTES)

    with pytest.raises(TypeError, match="bytes"):
        sinks.stdout.write(bytearray(b"x"))
    with pytest.raises(ValueError, match="non-empty"):
        sinks.stdout.write(b"")

    # Put the counter next to the frozen boundary without creating a 64 MiB fixture.
    sinks.stdout._session.used = JOB_STDOUT_STDERR_BYTES - 3
    sinks.stdout.write(b"ab")
    sinks.stderr.write(b"c")
    with pytest.raises(OverflowError, match="limit"):
        sinks.stderr.write(b"x")

    sinks.stdout.close()
    with pytest.raises(ValueError, match="closed"):
        sinks.stdout.write(b"x")
    sinks.stderr.close()
    assert (tmp_path / "jobs" / ROUTE_JOB_ID / "stdout.log").read_bytes() == b"ab"
    assert (tmp_path / "jobs" / ROUTE_JOB_ID / "stderr.log").read_bytes() == b"c"


@pytest.mark.parametrize("invalid_limit", [0, JOB_STDOUT_STDERR_BYTES - 1, True])
def test_invalid_log_limit_has_no_filesystem_side_effects(
    tmp_path: Path,
    coordination: CoordinationLock,
    invalid_limit: int,
) -> None:
    store = _store(tmp_path, coordination)

    with _raises_port_error(ErrorCode.EXECUTION_RECORD_FAILED):
        store.open_log_sinks(ROUTE_JOB_ID, invalid_limit)

    assert not (tmp_path / "jobs").exists()


def test_empty_log_pair_can_be_reopened_but_partial_pair_is_rejected(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    store = _store(tmp_path, coordination)
    first = store.open_log_sinks(ROUTE_JOB_ID, JOB_STDOUT_STDERR_BYTES)
    first.stdout.close()
    first.stderr.close()

    second = store.open_log_sinks(ROUTE_JOB_ID, JOB_STDOUT_STDERR_BYTES)
    second.stdout.close()
    second.stderr.close()

    stderr_path = tmp_path / "jobs" / ROUTE_JOB_ID / "stderr.log"
    stderr_path.unlink()
    with _raises_port_error(ErrorCode.EXECUTION_RECORD_FAILED):
        store.open_log_sinks(ROUTE_JOB_ID, JOB_STDOUT_STDERR_BYTES)


def test_nonempty_or_linked_logs_are_never_truncated_or_reused(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    job_directory = tmp_path / "jobs" / ROUTE_JOB_ID
    job_directory.mkdir(parents=True)
    stdout_path = job_directory / "stdout.log"
    stderr_path = job_directory / "stderr.log"
    stdout_path.write_bytes(b"old stdout")
    stderr_path.write_bytes(b"")
    store = _store(tmp_path, coordination)

    with _raises_port_error(ErrorCode.EXECUTION_RECORD_FAILED):
        store.open_log_sinks(ROUTE_JOB_ID, JOB_STDOUT_STDERR_BYTES)
    assert stdout_path.read_bytes() == b"old stdout"
    assert stderr_path.read_bytes() == b""

    stdout_path.unlink()
    target = tmp_path / "outside-log"
    target.write_bytes(b"")
    symlink_or_skip(stdout_path, target)
    with _raises_port_error(ErrorCode.EXECUTION_RECORD_FAILED):
        store.open_log_sinks(ROUTE_JOB_ID, JOB_STDOUT_STDERR_BYTES)
    assert target.read_bytes() == b""


def test_log_pair_creation_failure_rolls_back_both_names(
    tmp_path: Path,
    coordination: CoordinationLock,
) -> None:
    file_sync = FailFirstFileSync()
    store = _store(tmp_path, coordination, file_sync=file_sync)

    with _raises_port_error(ErrorCode.EXECUTION_RECORD_FAILED):
        store.open_log_sinks(ROUTE_JOB_ID, JOB_STDOUT_STDERR_BYTES)

    job_directory = tmp_path / "jobs" / ROUTE_JOB_ID
    assert not (job_directory / "stdout.log").exists()
    assert not (job_directory / "stderr.log").exists()

    sinks = store.open_log_sinks(ROUTE_JOB_ID, JOB_STDOUT_STDERR_BYTES)
    sinks.stdout.close()
    sinks.stderr.close()
