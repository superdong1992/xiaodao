"""Durable filesystem-backed Job, Outcome, and execution-log records.

The public surface in this module deliberately mirrors the frozen
``ExecutionRecordStore`` port.  Filesystem coordination and durability
operations are injected as narrow duck-typed collaborators so the store can
share S02's one re-entrant coordination lock and platform adapter.
"""

from __future__ import annotations

import os
import re
import stat
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Protocol, Self, TypeVar, cast

from pydantic import TypeAdapter

from problem_locator.contracts import (
    ExecutionFileRef,
    ExecutionLogSinks,
    Job,
    JobOutcome,
    JobStatus,
    OpaqueId,
    PublishedJobReceipt,
    RuntimeExecutionReceipt,
    bytes_sha256,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.contracts.limits import JOB_STDOUT_STDERR_BYTES
from problem_locator.storage.platform import PlatformFileSync, PlatformReplaceOperation


class _CoordinationLock(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def publication_held_by_current_thread(self) -> bool: ...


class _FileSync(Protocol):
    def sync_file(self, path: Path) -> None: ...

    def sync_directory(self, path: Path) -> None: ...

    def make_read_only(self, path: Path) -> None: ...


class _Replacer(Protocol):
    def replace(self, source: Path, destination: Path) -> None: ...


_OPAQUE_ID_ADAPTER = TypeAdapter(OpaqueId)
_TEMP_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MODEL = TypeVar("_MODEL", Job, JobOutcome)


def _validated_job_id(value: OpaqueId) -> str:
    return cast(str, _OPAQUE_ID_ADAPTER.validate_python(value))


def _open_read_only(path: Path) -> int:
    return os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))


def _require_regular_metadata(path: Path, metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise OSError(f"execution record must be one ordinary, unlinked file: {path}")


def _require_regular_path(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise OSError(f"symbolic links are forbidden for execution records: {path}")
    _require_regular_metadata(path, metadata)
    return metadata


def _regular_path_if_present(path: Path) -> os.stat_result | None:
    try:
        return _require_regular_path(path)
    except FileNotFoundError:
        return None


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _stable_metadata(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_nlink,
    )


def _read_regular_bytes(path: Path) -> bytes:
    """Read one stable ordinary file without following a path-level symlink."""

    path_metadata = _require_regular_path(path)
    descriptor = _open_read_only(path)
    try:
        before = os.fstat(descriptor)
        _require_regular_metadata(path, before)
        if not _same_file(path_metadata, before):
            raise OSError(f"execution record changed while it was opened: {path}")

        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        _require_regular_metadata(path, after)
    finally:
        os.close(descriptor)

    final_path_metadata = _require_regular_path(path)
    if (
        _stable_metadata(before) != _stable_metadata(after)
        or not _same_file(after, final_path_metadata)
    ):
        raise OSError(f"execution record changed while it was read: {path}")
    payload = b"".join(chunks)
    if len(payload) != after.st_size:
        raise OSError(f"execution record size changed while it was read: {path}")
    return payload


def _write_new_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError(f"zero-length write while creating execution record: {path}")
            offset += written
    finally:
        os.close(descriptor)


def _create_empty_file(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        _require_regular_metadata(path, os.fstat(descriptor))
    finally:
        os.close(descriptor)


def _ensure_existing_directory(path: Path, *, label: str) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OSError(f"{label} must be an ordinary directory: {path}")


class _LogSession:
    def __init__(self, limit: int) -> None:
        self.lock = threading.RLock()
        self.limit = limit
        self.used = 0
        self.open_sinks = 2


class _AppendOnlyFileSink:
    def __init__(
        self,
        handle: BinaryIO,
        path: Path,
        session: _LogSession,
        file_sync: _FileSync,
        on_last_close: Callable[[], None],
    ) -> None:
        self._handle = handle
        self._path = path
        self._session = session
        self._file_sync = file_sync
        self._on_last_close = on_last_close
        self._closed = False

    def write(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes):
            raise TypeError("execution log chunks must be bytes")
        if not chunk:
            raise ValueError("execution log chunks must be non-empty")
        with self._session.lock:
            if self._closed:
                raise ValueError("execution log sink is closed")
            if self._session.used + len(chunk) > self._session.limit:
                raise OverflowError("combined stdout/stderr limit exceeded")

            view = memoryview(chunk)
            offset = 0
            while offset < len(view):
                written = self._handle.write(view[offset:])
                if written is None or written <= 0:
                    raise OSError("zero-length append to execution log")
                offset += written
                self._session.used += written

    def _flush_locked(self) -> None:
        self._handle.flush()
        self._file_sync.sync_file(self._path)

    def flush(self) -> None:
        with self._session.lock:
            if self._closed:
                return
            self._flush_locked()

    def close(self) -> None:
        last_close = False
        failure: BaseException | None = None
        with self._session.lock:
            if self._closed:
                return
            try:
                self._flush_locked()
            except BaseException as exc:  # close still releases the active session
                failure = exc
            try:
                self._handle.close()
            except BaseException as exc:
                if failure is None:
                    failure = exc
            self._closed = True
            self._session.open_sinks -= 1
            last_close = self._session.open_sinks == 0

        if last_close:
            try:
                self._on_last_close()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure


class FileExecutionRecordStore:
    """Filesystem implementation of the frozen ``ExecutionRecordStore`` port."""

    def __init__(
        self,
        data_root: Path,
        coordination_lock: _CoordinationLock,
        file_sync: _FileSync | None = None,
        replacer: _Replacer | None = None,
        *,
        temp_token_factory: Callable[[], str] | None = None,
    ) -> None:
        root = Path(data_root)
        if not root.is_absolute():
            raise ValueError("DATA_ROOT must be an absolute path")
        _ensure_existing_directory(root, label="DATA_ROOT")
        self._data_root = root
        self._jobs_root = root / "jobs"
        self._coordination_lock = coordination_lock
        self._file_sync = file_sync if file_sync is not None else PlatformFileSync()
        self._replacer = (
            replacer if replacer is not None else PlatformReplaceOperation()
        )
        self._temp_token_factory = temp_token_factory or (lambda: uuid.uuid4().hex)
        self._active_log_sessions: dict[str, _LogSession] = {}

    def publish_job(self, job: Job) -> ExecutionFileRef:
        if not isinstance(job, Job):
            raise TypeError("job must be a Job DTO")
        canonical_bytes = canonical_json_bytes(job)
        validated_job = parse_canonical_json_bytes(canonical_bytes, Job)
        if validated_job.status is not JobStatus.PENDING:
            raise ValueError("published job.json must contain a PENDING Job")

        with self._coordination_lock:
            if not self._coordination_lock.publication_held_by_current_thread():
                raise RuntimeError("publish_job requires the current publication lease")
            job_directory = self._ensure_job_directory(validated_job.job_id)
            _, stored_bytes = self._publish_canonical_record(
                job_directory / "job.json",
                canonical_bytes,
                Job,
                validated_job.job_id,
            )
            return self._file_ref(validated_job.job_id, "job.json", stored_bytes)

    def publish_outcome_bytes(
        self,
        job_id: OpaqueId,
        canonical_bytes: bytes,
    ) -> ExecutionFileRef:
        validated_job_id = _validated_job_id(job_id)
        outcome = parse_canonical_json_bytes(canonical_bytes, JobOutcome)
        if outcome.job_id != validated_job_id:
            raise ValueError("job_outcome.json job_id must match its path")

        with self._coordination_lock:
            job_directory = self._ensure_job_directory(validated_job_id)
            _, stored_bytes = self._publish_canonical_record(
                job_directory / "job_outcome.json",
                canonical_bytes,
                JobOutcome,
                validated_job_id,
            )
            return self._file_ref(
                validated_job_id,
                "job_outcome.json",
                stored_bytes,
            )

    def read_published_job(self, job_id: OpaqueId) -> PublishedJobReceipt | None:
        validated_job_id = _validated_job_id(job_id)
        with self._coordination_lock:
            job_directory = self._find_job_directory(validated_job_id)
            if job_directory is None:
                return None
            final_path = job_directory / "job.json"
            if _regular_path_if_present(final_path) is None:
                return None
            job, canonical_bytes = self._validated_final_record(
                final_path,
                Job,
                validated_job_id,
            )
            return PublishedJobReceipt(
                job=job,
                job_file_ref=self._file_ref(
                    validated_job_id,
                    "job.json",
                    canonical_bytes,
                ),
            )

    def read_published_outcome(
        self,
        job_id: OpaqueId,
    ) -> RuntimeExecutionReceipt | None:
        validated_job_id = _validated_job_id(job_id)
        with self._coordination_lock:
            job_directory = self._find_job_directory(validated_job_id)
            if job_directory is None:
                return None
            final_path = job_directory / "job_outcome.json"
            if _regular_path_if_present(final_path) is None:
                return None
            outcome, canonical_bytes = self._validated_final_record(
                final_path,
                JobOutcome,
                validated_job_id,
            )
            return RuntimeExecutionReceipt(
                job_outcome=outcome,
                outcome_file_ref=self._file_ref(
                    validated_job_id,
                    "job_outcome.json",
                    canonical_bytes,
                ),
            )

    def open_log_sinks(
        self,
        job_id: OpaqueId,
        combined_limit_bytes: int,
    ) -> ExecutionLogSinks:
        validated_job_id = _validated_job_id(job_id)
        if (
            not isinstance(combined_limit_bytes, int)
            or isinstance(combined_limit_bytes, bool)
            or combined_limit_bytes != JOB_STDOUT_STDERR_BYTES
        ):
            raise ValueError(
                f"combined_limit_bytes must be {JOB_STDOUT_STDERR_BYTES}"
            )

        with self._coordination_lock:
            if validated_job_id in self._active_log_sessions:
                raise RuntimeError("execution log sinks are already open for this job")
            job_directory = self._ensure_job_directory(validated_job_id)
            stdout_path = job_directory / "stdout.log"
            stderr_path = job_directory / "stderr.log"
            self._ensure_empty_log_pair(stdout_path, stderr_path)

            stdout_handle: BinaryIO | None = None
            stderr_handle: BinaryIO | None = None
            try:
                stdout_handle = self._open_empty_append_handle(stdout_path)
                stderr_handle = self._open_empty_append_handle(stderr_path)
            except BaseException:
                if stdout_handle is not None:
                    stdout_handle.close()
                if stderr_handle is not None:
                    stderr_handle.close()
                raise

            session = _LogSession(combined_limit_bytes)
            self._active_log_sessions[validated_job_id] = session

            def on_last_close() -> None:
                with self._coordination_lock:
                    if self._active_log_sessions.get(validated_job_id) is session:
                        del self._active_log_sessions[validated_job_id]

            stdout_sink = _AppendOnlyFileSink(
                stdout_handle,
                stdout_path,
                session,
                self._file_sync,
                on_last_close,
            )
            stderr_sink = _AppendOnlyFileSink(
                stderr_handle,
                stderr_path,
                session,
                self._file_sync,
                on_last_close,
            )
            try:
                return ExecutionLogSinks(
                    stdout=stdout_sink,
                    stderr=stderr_sink,
                    combined_limit_bytes=combined_limit_bytes,
                )
            except BaseException:
                stdout_sink.close()
                stderr_sink.close()
                raise

    def _ensure_jobs_root(self) -> Path:
        try:
            _ensure_existing_directory(self._jobs_root, label="jobs root")
        except FileNotFoundError:
            self._jobs_root.mkdir(mode=0o700)
            _ensure_existing_directory(self._jobs_root, label="jobs root")
            self._file_sync.sync_directory(self._data_root)
        return self._jobs_root

    def _ensure_job_directory(self, job_id: OpaqueId) -> Path:
        validated_job_id = _validated_job_id(job_id)
        jobs_root = self._ensure_jobs_root()
        job_directory = jobs_root / validated_job_id
        try:
            _ensure_existing_directory(job_directory, label="job directory")
        except FileNotFoundError:
            job_directory.mkdir(mode=0o700)
            _ensure_existing_directory(job_directory, label="job directory")
            self._file_sync.sync_directory(jobs_root)
        return job_directory

    def _find_job_directory(self, job_id: str) -> Path | None:
        try:
            _ensure_existing_directory(self._jobs_root, label="jobs root")
        except FileNotFoundError:
            return None
        job_directory = self._jobs_root / job_id
        try:
            _ensure_existing_directory(job_directory, label="job directory")
        except FileNotFoundError:
            return None
        return job_directory

    def _new_temp_path(self, parent: Path, filename: str) -> Path:
        token = self._temp_token_factory()
        if not isinstance(token, str) or _TEMP_TOKEN_PATTERN.fullmatch(token) is None:
            raise ValueError("temp_token_factory must return one safe ASCII token")
        return parent / f".{filename}.{token}.tmp"

    def _publish_canonical_record(
        self,
        final_path: Path,
        expected_bytes: bytes,
        model_type: type[_MODEL],
        expected_job_id: str,
    ) -> tuple[_MODEL, bytes]:
        if _regular_path_if_present(final_path) is not None:
            model, stored_bytes = self._validated_final_record(
                final_path,
                model_type,
                expected_job_id,
            )
            if stored_bytes != expected_bytes:
                raise FileExistsError(
                    f"different canonical bytes already exist at {final_path}"
                )
            self._finalize_record(final_path)
            return model, stored_bytes

        temp_path = self._new_temp_path(final_path.parent, final_path.name)
        try:
            _write_new_file(temp_path, expected_bytes)
            self._file_sync.sync_file(temp_path)
            if _regular_path_if_present(final_path) is not None:
                model, stored_bytes = self._validated_final_record(
                    final_path,
                    model_type,
                    expected_job_id,
                )
                if stored_bytes != expected_bytes:
                    raise FileExistsError(
                        f"different canonical bytes already exist at {final_path}"
                    )
                self._finalize_record(final_path)
                return model, stored_bytes
            self._replacer.replace(temp_path, final_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                # A failed operation must never hide the publication/finalize error.
                pass

        model, stored_bytes = self._validated_final_record(
            final_path,
            model_type,
            expected_job_id,
        )
        if stored_bytes != expected_bytes:
            raise OSError("atomic replacer published different execution-record bytes")
        self._finalize_record(final_path)
        return model, stored_bytes

    def _validated_final_record(
        self,
        final_path: Path,
        model_type: type[_MODEL],
        expected_job_id: str,
    ) -> tuple[_MODEL, bytes]:
        canonical_bytes = _read_regular_bytes(final_path)
        model = parse_canonical_json_bytes(canonical_bytes, model_type)
        if model.job_id != expected_job_id:
            raise ValueError(f"execution-record job_id does not match {final_path.parent.name}")
        if isinstance(model, Job) and model.status is not JobStatus.PENDING:
            raise ValueError("published job.json must contain a PENDING Job")
        return model, canonical_bytes

    def _finalize_record(self, final_path: Path) -> None:
        _require_regular_path(final_path)
        self._file_sync.make_read_only(final_path)
        _require_regular_path(final_path)
        self._file_sync.sync_file(final_path)
        self._file_sync.sync_directory(final_path.parent)

    @staticmethod
    def _file_ref(job_id: str, filename: str, canonical_bytes: bytes) -> ExecutionFileRef:
        return ExecutionFileRef(
            relative_key=f"jobs/{job_id}/{filename}",
            size=len(canonical_bytes),
            sha256=bytes_sha256(canonical_bytes),
        )

    def _ensure_empty_log_pair(self, stdout_path: Path, stderr_path: Path) -> None:
        stdout_metadata = _regular_path_if_present(stdout_path)
        stderr_metadata = _regular_path_if_present(stderr_path)
        if (stdout_metadata is None) != (stderr_metadata is None):
            raise OSError("stdout.log and stderr.log must exist or be absent as one pair")
        if stdout_metadata is not None and stderr_metadata is not None:
            if stdout_metadata.st_size != 0 or stderr_metadata.st_size != 0:
                raise FileExistsError("non-empty execution logs cannot be reused")
            return

        created: list[Path] = []
        try:
            _create_empty_file(stdout_path)
            created.append(stdout_path)
            _create_empty_file(stderr_path)
            created.append(stderr_path)
            self._file_sync.sync_file(stdout_path)
            self._file_sync.sync_file(stderr_path)
            self._file_sync.sync_directory(stdout_path.parent)
        except BaseException:
            for path in reversed(created):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                self._file_sync.sync_directory(stdout_path.parent)
            except OSError:
                pass
            raise

        stdout_metadata = _require_regular_path(stdout_path)
        stderr_metadata = _require_regular_path(stderr_path)
        if stdout_metadata.st_size != 0 or stderr_metadata.st_size != 0:
            raise OSError("new execution logs must both be empty")

    @staticmethod
    def _open_empty_append_handle(path: Path) -> BinaryIO:
        path_metadata = _require_regular_path(path)
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            descriptor_metadata = os.fstat(descriptor)
            _require_regular_metadata(path, descriptor_metadata)
            if not _same_file(path_metadata, descriptor_metadata):
                raise OSError(f"execution log changed while it was opened: {path}")
            if descriptor_metadata.st_size != 0:
                raise FileExistsError("non-empty execution logs cannot be reused")
            return cast(BinaryIO, os.fdopen(descriptor, "wb", buffering=0))
        except BaseException:
            os.close(descriptor)
            raise


__all__ = ["FileExecutionRecordStore"]
