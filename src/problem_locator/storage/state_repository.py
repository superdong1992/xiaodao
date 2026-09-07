"""Case-scoped live state and durable, lazily loaded terminal snapshots.

The process owns active Cases. Only terminal Cases cross the SQLite durability
barrier; no active mutation scans historical Cases or their resource bytes.
"""
from __future__ import annotations

import sqlite3
import json
import threading
import weakref
from contextlib import contextmanager
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from problem_locator.contracts import (
    CONTRACT_REVISION, SCHEMA_VERSION, ApplicationError, ApplicationPortError,
    Artifact, ArtifactKind, CaseAggregate, CaseStatus, Clock, CommitReceipt, ERROR_SPECS,
    ErrorCode, ExecutionRecordStore, IdGenerator, Job, JobStatus, StateExportObjectCounts,
    StateFile, StateMutation, ValidationIssue, ValidationReport, canonical_json_bytes,
)
from .atomic import FileSync, Replacer, read_stable_file_bytes
from .coordination import StorageCoordinationLock
from .layout import StorageLayout, UnsupportedDataFormatError
from .platform import PlatformFileSync

_TERMINAL = frozenset({CaseStatus.RESOLVED, CaseStatus.PARTIALLY_RESOLVED,
                      CaseStatus.UNRESOLVED, CaseStatus.FAILED, CaseStatus.CANCELLED})

def _port_error(code: ErrorCode, message: str) -> ApplicationPortError:
    return ApplicationPortError(ApplicationError(code=code, message=message,
        details=[], retryable=ERROR_SPECS[code].application_retryable))

def _clone(value):
    return value.model_copy(deep=True)

def _object_counts(state: StateFile) -> StateExportObjectCounts:
    names = ('jobs', 'outcomes', 'outcome_processing_records',
             'execution_failure_records', 'attachments', 'evidence', 'artifacts')
    return StateExportObjectCounts(cases=len(state.cases),
        **{name: sum(len(getattr(case, name)) for case in state.cases.values()) for name in names},
        idempotency_records=len(state.idempotency_records), runtime_epochs=len(state.runtime_epochs),
        recovery_processing_records=len(state.recovery_processing_records))

class CaseStateRepository:
    """An isolated revision domain per Case; SQLite contains completed work only."""

    def __init__(self, data_root: Path, coordination_lock: StorageCoordinationLock,
                 clock: Clock, id_generator: IdGenerator, *, file_sync: FileSync | None = None,
                 replacer: Replacer | None = None,
                 execution_record_store: ExecutionRecordStore | None = None,
                 read_file: Callable[[Path], bytes] = read_stable_file_bytes) -> None:
        self._layout = StorageLayout.at(data_root)
        self._clock = clock
        self._id_generator = id_generator
        self._file_sync = file_sync or PlatformFileSync()
        self._index_lock = threading.RLock()
        self._database_lock = threading.RLock()
        self._case_locks = weakref.WeakValueDictionary()
        self._live: dict[str, StateFile] = {}
        self._objects: dict[str, str] = {}
        self._requests: dict[str, str] = {}
        self._state_failure: ApplicationError | None = None
        self.on_terminal: Callable[[str], None] = lambda case_id: None
        # Projection receives only the candidate aggregate and the shared SQL
        # transaction. It must never acquire a Case lock or re-read repository.
        self.on_case_projection: Callable[[sqlite3.Connection, StateFile], None] | None = None
        self.on_case_committed: Callable[[str], None] = lambda case_id: None
        try:
            self._layout.initialize_v2_data_root(self._file_sync)
        except UnsupportedDataFormatError as exc:
            raise _port_error(ErrorCode.STATE_SCHEMA_UNSUPPORTED,
                '数据目录格式不受支持，请为新版本配置空 DATA_ROOT。') from exc
        try:
            self._db = sqlite3.connect(self._layout.data_root / 'completed.sqlite3',
                                      check_same_thread=False, isolation_level=None, timeout=30)
            self._db.execute('PRAGMA journal_mode=WAL')
            self._db.execute('PRAGMA synchronous=FULL')
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS completed_cases (
                    case_id TEXT PRIMARY KEY, snapshot BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS object_index (
                    object_id TEXT PRIMARY KEY, case_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS request_index (
                    request_key TEXT PRIMARY KEY, case_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS resource_index (
                    storage_key TEXT PRIMARY KEY, case_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS archive_tasks (
                    case_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    payload BLOB NOT NULL, error TEXT);
                CREATE INDEX IF NOT EXISTS archive_tasks_status ON archive_tasks(status);
            """)
            self._db.execute("UPDATE archive_tasks SET status='PENDING' WHERE status='RUNNING'")
            now = self._clock.now()
            self._db.execute('INSERT OR IGNORE INTO metadata VALUES (?, ?)',
                             ('installation_id', self._id_generator.new('installation')))
            self._db.execute('INSERT OR IGNORE INTO metadata VALUES (?, ?)', ('created_at', now))
            metadata = dict(self._db.execute('SELECT key, value FROM metadata'))
            self._base = StateFile(schema_version=SCHEMA_VERSION, contract_revision=CONTRACT_REVISION,
                generation=1, installation_id=metadata['installation_id'],
                created_at=metadata['created_at'], updated_at=metadata['created_at'], runtime_epochs=[],
                recovery_processing_records={}, cases={}, idempotency_records={})
            self._file_sync.sync_directory(self._layout.data_root)
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise _port_error(ErrorCode.STATE_CORRUPT, '无法打开已完成 Case 的数据库。') from exc

    @property
    def layout(self) -> StorageLayout:
        return self._layout

    @contextmanager
    def database_read(self):
        """Serialize access to the connection shared with durable adapters."""
        with self._database_lock:
            yield self._db

    @contextmanager
    def database_transaction(self):
        """One FULL-WAL transaction; callers must not acquire Case locks."""
        with self._database_lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                yield self._db
                self._db.execute('COMMIT')
            except BaseException:
                self._db.execute('ROLLBACK')
                raise

    def _lock_for(self, case_id: str) -> threading.RLock:
        with self._index_lock:
            return self._case_locks.setdefault(case_id, threading.RLock())

    def _locate(self, key: str, *, request: bool = False) -> str | None:
        with self._index_lock:
            case_id = (self._requests if request else self._objects).get(key)
        if case_id is not None:
            return case_id
        table, column = ('request_index', 'request_key') if request else ('object_index', 'object_id')
        with self._database_lock:
            row = self._db.execute(f'SELECT case_id FROM {table} WHERE {column}=?', (key,)).fetchone()
        return None if row is None else row[0]

    def _load_case(self, case_id: str) -> StateFile | None:
        live = self._live.get(case_id)
        if live is not None:
            return live
        try:
            with self._database_lock:
                row = self._db.execute('SELECT snapshot FROM completed_cases WHERE case_id=?',
                                       (case_id,)).fetchone()
            return None if row is None else StateFile.model_validate_json(row[0])
        except (sqlite3.Error, ValidationError) as exc:
            self._state_failure = _port_error(ErrorCode.STATE_CORRUPT, '已完成 Case 的快照无法读取。').error
            raise ApplicationPortError(_clone(self._state_failure)) from exc

    def read_snapshot(self, case_id: str | None = None, *, job_id: str | None = None,
                      attachment_id: str | None = None, request_key: str | None = None) -> StateFile:
        """Business callers provide a scope. No scope is an explicit administrative export."""
        scoped = any(value is not None for value in (case_id, job_id, attachment_id, request_key))
        if case_id is None and (job_id is not None or attachment_id is not None):
            case_id = self._locate(job_id or attachment_id)
        if case_id is None and request_key is not None:
            case_id = self._locate(request_key, request=True)
        if case_id is not None:
            with self._lock_for(case_id):
                state = self._load_case(case_id)
                result = _clone(self._base if state is None else state)
            # A reused key may belong to one other Case. Read only that owner,
            # outside the first Case lock, so the command can report a conflict.
            if request_key is not None:
                owner = self._locate(request_key, request=True)
                if owner is not None and owner != case_id:
                    other = self.read_snapshot(owner)
                    result.cases.update(other.cases)
                    result.idempotency_records.update(other.idempotency_records)
            return result
        if scoped:
            return _clone(self._base)
        with self._index_lock:
            case_ids = set(self._live)
        with self._database_lock:
            case_ids.update(row[0] for row in self._db.execute('SELECT case_id FROM completed_cases'))
        result = _clone(self._base)
        for selected in sorted(case_ids):
            state = self.read_snapshot(selected)
            result.cases.update(state.cases)
            result.idempotency_records.update(state.idempotency_records)
            result.generation = max(result.generation, state.generation)
            result.updated_at = max(result.updated_at, state.updated_at)
        return result

    def read_case(self, case_id: str) -> CaseAggregate:
        aggregate = self.read_snapshot(case_id).cases.get(case_id)
        if aggregate is None:
            raise _port_error(ErrorCode.CASE_NOT_FOUND, 'Case 不存在或运行中任务已随服务重启失效。')
        return aggregate

    def read_job(self, job_id: str) -> Job:
        case_id = self._locate(job_id)
        if case_id is None:
            raise _port_error(ErrorCode.JOB_NOT_FOUND, 'Job 不存在。')
        return self.read_case(case_id).jobs[job_id]

    def read_artifact(self, artifact_id: str) -> Artifact:
        case_id = self._locate(artifact_id)
        if case_id is None:
            raise _port_error(ErrorCode.ARTIFACT_NOT_FOUND, '产物不存在。')
        return self.read_case(case_id).artifacts[artifact_id]

    def _mutation_case_id(self, mutation: StateMutation) -> str | None:
        if mutation.upsert_case is not None:
            return mutation.upsert_case.case_id
        for collection in (mutation.insert_jobs, mutation.insert_outcomes, mutation.upsert_attachments,
                           mutation.insert_evidence, mutation.insert_artifacts):
            if collection:
                return collection[0].case_id
        for collection in (mutation.job_lifecycle_updates, mutation.insert_outcome_processing_records,
                           mutation.insert_execution_failure_records):
            if collection:
                return self._locate(collection[0].job_id)
        return None

    def _archive_payload(self, aggregate: CaseAggregate):
        case = aggregate.case
        if case.status not in {CaseStatus.RESOLVED, CaseStatus.PARTIALLY_RESOLVED} or case.final_result is None:
            return None
        reports = [item for item in aggregate.artifacts.values() if item.kind is ArtifactKind.USER_RESULT
                   and item.created_by_job_id == case.final_result.proposed_by_job_id]
        if len(reports) != 1 or reports[0].metadata.archive_plan is None:
            return None
        report = reports[0]
        plan = report.metadata.archive_plan
        paths = []
        for source in plan.logs:
            if source.source_kind == 'INPUT_ARTIFACT':
                source_artifact = aggregate.artifacts[source.source_ref]
            else:
                proposals = [proposal for outcome in aggregate.outcomes.values()
                    if outcome.job_id == report.created_by_job_id
                    for proposal in outcome.proposed_artifacts
                    if proposal.proposal_key == source.source_ref and proposal.artifact_kind is ArtifactKind.LOGPARSE_RUN]
                if len(proposals) != 1:
                    raise ValueError('archive log has no unique accepted source proposal')
                matches = [item for item in aggregate.artifacts.values()
                    if item.created_by_job_id == report.created_by_job_id and item.kind is ArtifactKind.LOGPARSE_RUN
                    and item.sha256 == proposals[0].sha256 and item.size == proposals[0].size]
                if len(matches) != 1:
                    raise ValueError('archive log has no unique persisted source artifact')
                source_artifact = matches[0]
            if source_artifact.kind is not ArtifactKind.LOGPARSE_RUN:
                raise ValueError('archive source must be a LOGPARSE_RUN')
            paths.append(source_artifact.storage_key + '/' + source.relative_path)
        return {'report_artifact_id': report.artifact_id, 'source_job_id': report.created_by_job_id,
                'plan': plan.model_dump(mode='json'), 'source_storage_keys': paths}

    def _persist(self, case_id: str, state: StateFile) -> None:
        aggregate = state.cases[case_id]
        payload = self._archive_payload(aggregate)
        if payload is not None and aggregate.case.archive_status == 'NOT_REQUIRED':
            aggregate.case.archive_status = 'PENDING'
        # Resource publication owns file/directory fsync. This FULL WAL commit
        # occurs only after those immutable files have been published.
        objects = [key for name in ('jobs', 'attachments', 'evidence', 'artifacts', 'outcomes')
                   for key in getattr(aggregate, name)]
        with self._database_lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                self._db.execute('INSERT OR REPLACE INTO completed_cases VALUES (?, ?)',
                                  (case_id, canonical_json_bytes(state)))
                self._db.executemany('INSERT OR REPLACE INTO object_index VALUES (?, ?)',
                                     ((key, case_id) for key in objects))
                self._db.executemany('INSERT OR REPLACE INTO request_index VALUES (?, ?)',
                                     ((key, case_id) for key in state.idempotency_records))
                self._db.executemany('INSERT OR REPLACE INTO resource_index VALUES (?, ?)',
                                     ((key, case_id) for key in self._resource_keys(aggregate)))
                if payload is not None:
                    self._db.execute('INSERT OR IGNORE INTO archive_tasks VALUES (?, ?, ?, NULL)',
                        (case_id, 'PENDING', canonical_json_bytes(payload)))
                if aggregate.case.archive_status in {'READY', 'FAILED'}:
                    self._db.execute('UPDATE archive_tasks SET status=? WHERE case_id=?',
                        (aggregate.case.archive_status, case_id))
                if self.on_case_projection is not None:
                    self.on_case_projection(self._db, state)
                self._db.execute('COMMIT')
            except BaseException:
                self._db.execute('ROLLBACK')
                raise

    def commit(self, expected_generation: int, expected_case_revision: int | None,
               mutation: StateMutation) -> CommitReceipt:
        if self._state_failure is not None:
            raise ApplicationPortError(_clone(self._state_failure))
        case_id = self._mutation_case_id(mutation)
        if case_id is None:
            # Installation bookkeeping is process-local and has no business I/O.
            with self._index_lock:
                if expected_generation != self._base.generation:
                    raise _port_error(ErrorCode.REVISION_CONFLICT, '进程元数据已发生变化。')
                self._base, _ = self._apply_mutation(self._base, None, mutation)
                return CommitReceipt(generation=self._base.generation, case_revision=None)
        with self._lock_for(case_id):
            current = self._load_case(case_id) or self._base
            if current.generation != expected_generation:
                raise _port_error(ErrorCode.REVISION_CONFLICT, '当前 Case 已发生变化。')
            try:
                candidate, affected = self._apply_mutation(current, expected_case_revision, mutation)
            except (ValueError, ValidationError) as exc:
                raise _port_error(ErrorCode.STATE_WRITE_FAILED, '当前 Case 的状态变更无效。') from exc
            aggregate = candidate.cases[case_id]
            new_keys = [f'{item.operation}:{item.idempotency_key}' for item in mutation.insert_idempotency_records]
            reserved: list[str] = []
            try:
                for key in new_keys:
                    owner = self._locate(key, request=True)
                    with self._index_lock:
                        owner = self._requests.get(key, owner)
                        if owner is not None:
                            raise _port_error(ErrorCode.REVISION_CONFLICT, '该请求已经提交，请读取最新结果。')
                        self._requests[key] = case_id
                        reserved.append(key)
                terminal = aggregate.case.status in _TERMINAL
                if terminal:
                    self._persist(case_id, candidate)
                elif self.on_case_projection is not None:
                    with self.database_transaction() as database:
                        self.on_case_projection(database, candidate)
                with self._index_lock:
                    if terminal:
                        self._live.pop(case_id, None)
                    else:
                        self._live[case_id] = candidate
                    for name in ('jobs', 'attachments', 'evidence', 'artifacts', 'outcomes'):
                        for key in getattr(aggregate, name):
                            if terminal:
                                self._objects.pop(key, None)
                            else:
                                self._objects[key] = case_id
                    if terminal:
                        for key in candidate.idempotency_records:
                            self._requests.pop(key, None)
                        self.on_terminal(case_id)
                self.on_case_committed(case_id)
            except (sqlite3.Error, OSError) as exc:
                with self._index_lock:
                    for key in reserved:
                        self._requests.pop(key, None)
                self._state_failure = _port_error(ErrorCode.STATE_WRITE_FAILED,
                    '最终结果持久化失败，尚未交付。').error
                raise ApplicationPortError(_clone(self._state_failure)) from exc
            except BaseException:
                with self._index_lock:
                    for key in reserved:
                        self._requests.pop(key, None)
                raise
            return CommitReceipt(generation=candidate.generation,
                                 case_revision=aggregate.case.case_revision)

    def health(self) -> ValidationReport:
        failure = self._state_failure
        return ValidationReport(valid=failure is None, schema_version=SCHEMA_VERSION,
            contract_revision=CONTRACT_REVISION, generation=self._base.generation,
            object_counts=_object_counts(self._base),
            errors=[] if failure is None else [ValidationIssue(code=failure.code.value,
                object_type='CompletedCaseStore', object_id=None, field_path=None, message=failure.message)])

    @staticmethod
    def _resource_keys(aggregate):
        return {item.storage_key for item in aggregate.attachments.values() if item.storage_key} | {
            item.storage_key for item in aggregate.artifacts.values()} | {
            item.resource_ref.storage_key for item in aggregate.evidence.values() if item.resource_ref}

    def retention_in_use(self, kind: str, key: str) -> bool:
        """Indexed metadata lookup; never deserialize historical Cases or read resources."""
        if self._state_failure is not None:
            raise ApplicationPortError(_clone(self._state_failure))
        if kind == 'resource':
            case_id = key.split('/')[2]
            with self._lock_for(case_id):
                live = self._live.get(case_id)
                if live is not None and key in self._resource_keys(live.cases[case_id]):
                    return True
            with self._database_lock:
                return self._db.execute('SELECT 1 FROM resource_index WHERE storage_key=?', (key,)).fetchone() is not None
        with self._index_lock:
            owner = self._objects.get(key)
        if owner is not None:
            with self._lock_for(owner):
                live = self._live.get(owner)
                job = None if live is None else live.cases[owner].jobs.get(key)
                if job is not None:
                    return kind == 'job' or job.status in {JobStatus.PENDING, JobStatus.RUNNING}
        if kind == 'active_job':
            return False
        if kind != 'job':
            raise ValueError('unknown retention reference kind')
        with self._database_lock:
            return self._db.execute('SELECT 1 FROM object_index WHERE object_id=?', (key,)).fetchone() is not None

    def claim_archive_task(self):
        with self._database_lock:
            row = self._db.execute("SELECT case_id, payload FROM archive_tasks WHERE status='PENDING' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            self._db.execute("UPDATE archive_tasks SET status='RUNNING' WHERE case_id=?", (row[0],))
            return row[0], json.loads(row[1])

    def requeue_archive_task(self, case_id: str) -> None:
        with self._database_lock:
            self._db.execute("UPDATE archive_tasks SET status='PENDING' WHERE case_id=? AND status='RUNNING'", (case_id,))

    def validate_all(self) -> ValidationReport:
        state = self.read_snapshot()
        with self._database_lock:
            if self._db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise _port_error(ErrorCode.STATE_CORRUPT, '已完成 Case 数据库校验失败。')
        return ValidationReport(valid=True, schema_version=SCHEMA_VERSION,
            contract_revision=CONTRACT_REVISION, generation=state.generation,
            object_counts=_object_counts(state), errors=[])

    def export_snapshot(self) -> bytes:
        return canonical_json_bytes(self.read_snapshot())

    def close(self) -> None:
        with self._database_lock:
            self._db.close()

    @staticmethod
    def _empty_aggregate(case: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "case": dict(case),
            "jobs": {},
            "outcomes": {},
            "outcome_processing_records": {},
            "execution_failure_records": {},
            "attachments": {},
            "evidence": {},
            "artifacts": {},
        }

    @staticmethod
    def _find_case_id_for_job(cases: Mapping[str, Any], job_id: str) -> str:
        matches = [
            case_id
            for case_id, aggregate in cases.items()
            if job_id in aggregate["jobs"]
        ]
        if len(matches) != 1:
            raise ValueError(f"Job must belong to exactly one Case: {job_id}")
        return matches[0]

    @staticmethod
    def _insert_unique(target: dict[str, Any], key: str, value: Any) -> None:
        if key in target:
            raise ValueError(f"duplicate immutable object: {key}")
        target[key] = value

    @staticmethod
    def _upsert_recovery_processing_record(
        target: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        recovery_id = record["recovery_id"]
        existing = target.get(recovery_id)
        if existing is None:
            target[recovery_id] = record
            return
        if existing == record:
            return
        identity_fields = (
            "recovery_id",
            "current_runtime_epoch",
            "interrupted_job_ids",
            "pending_job_ids",
        )
        if (
            all(existing[field] == record[field] for field in identity_fields)
            and existing["completed_at"] is None
            and record["completed_at"] is not None
        ):
            target[recovery_id] = record
            return
        raise ValueError(
            "RecoveryProcessingRecord is immutable except for its first completion"
        )

    @staticmethod
    def _upsert_runtime_epoch_record(
        target: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        runtime_epoch = record["runtime_epoch"]
        existing = target.get(runtime_epoch)
        if existing is None:
            target[runtime_epoch] = record
            return
        if existing == record:
            return
        identity_fields = ("runtime_epoch", "started_at", "recovery_id")
        if (
            all(existing[field] == record[field] for field in identity_fields)
            and existing["recovery_completed_at"] is None
            and record["recovery_completed_at"] is not None
        ):
            target[runtime_epoch] = record
            return
        raise ValueError(
            "RuntimeEpochRecord is immutable except for its first recovery completion"
        )

    def _infer_affected_case_id(
        self,
        current: StateFile,
        cases: Mapping[str, Any],
        mutation_data: Mapping[str, Any],
    ) -> str:
        candidates: set[str] = set()
        upsert_case = mutation_data["upsert_case"]
        if upsert_case is not None:
            candidates.add(upsert_case["case_id"])
        for job in mutation_data["insert_jobs"]:
            candidates.add(job["case_id"])
        for outcome in mutation_data["insert_outcomes"]:
            candidates.add(outcome["case_id"])
        for collection in (
            "upsert_attachments",
            "insert_evidence",
            "insert_artifacts",
        ):
            for item in mutation_data[collection]:
                candidates.add(item["case_id"])
        for update in mutation_data["job_lifecycle_updates"]:
            candidates.add(self._find_case_id_for_job(cases, update["job_id"]))
        for collection in (
            "insert_outcome_processing_records",
            "insert_execution_failure_records",
        ):
            for record in mutation_data[collection]:
                candidates.add(
                    self._find_case_id_for_job(cases, record["job_id"])
                )
        for record in mutation_data["insert_idempotency_records"]:
            if record["case_id"] is not None:
                candidates.add(record["case_id"])
        if len(candidates) != 1:
            raise ValueError("cannot infer one Case for expected_case_revision")
        case_id = candidates.pop()
        existing = current.cases.get(case_id)
        if existing is None:
            raise LookupError("expected Case does not exist")
        return case_id

    def _apply_mutation(
        self,
        current: StateFile,
        expected_case_revision: int | None,
        mutation: StateMutation,
    ) -> tuple[StateFile, str | None]:
        state_data = current.model_dump(mode="python")
        cases: dict[str, Any] = state_data["cases"]
        mutation_data = mutation.model_dump(mode="python")

        upsert_case = mutation_data["upsert_case"]
        affected_case_id: str | None = None
        if upsert_case is not None:
            affected_case_id = upsert_case["case_id"]

        if expected_case_revision is not None:
            if affected_case_id is None:
                affected_case_id = self._infer_affected_case_id(
                    current,
                    cases,
                    mutation_data,
                )
            existing_case = current.cases.get(affected_case_id)
            if (
                existing_case is None
                or existing_case.case.case_revision != expected_case_revision
            ):
                raise _port_error(
                    ErrorCode.REVISION_CONFLICT,
                    "The Case revision changed before commit.",
                )

        if upsert_case is not None:
            if affected_case_id not in cases:
                cases[affected_case_id] = self._empty_aggregate(upsert_case)
            else:
                cases[affected_case_id]["case"] = upsert_case

        runtime_by_id = {
            record["runtime_epoch"]: record for record in state_data["runtime_epochs"]
        }
        for record in mutation_data["upsert_runtime_epoch_records"]:
            self._upsert_runtime_epoch_record(runtime_by_id, record)
        state_data["runtime_epochs"] = list(runtime_by_id.values())

        for record in mutation_data["upsert_recovery_processing_records"]:
            self._upsert_recovery_processing_record(
                state_data["recovery_processing_records"],
                record,
            )

        for job in mutation_data["insert_jobs"]:
            aggregate = cases[job["case_id"]]
            self._insert_unique(aggregate["jobs"], job["job_id"], job)
        for update in mutation_data["job_lifecycle_updates"]:
            case_id = self._find_case_id_for_job(cases, update["job_id"])
            job = cases[case_id]["jobs"][update["job_id"]]
            if job["status"] != update["expected_status"]:
                raise _port_error(
                    ErrorCode.REVISION_CONFLICT,
                    "The Job lifecycle changed before commit.",
                )
            job["status"] = update["target_status"]
            for field in ("started_at", "finished_at", "runtime_epoch"):
                if update[field] is not None:
                    job[field] = update[field]
        for outcome in mutation_data["insert_outcomes"]:
            aggregate = cases[outcome["case_id"]]
            self._insert_unique(
                aggregate["outcomes"],
                outcome["outcome_id"],
                outcome,
            )
        for record in mutation_data["insert_outcome_processing_records"]:
            case_id = self._find_case_id_for_job(cases, record["job_id"])
            self._insert_unique(
                cases[case_id]["outcome_processing_records"],
                record["outcome_id"],
                record,
            )
        for record in mutation_data["insert_execution_failure_records"]:
            case_id = self._find_case_id_for_job(cases, record["job_id"])
            self._insert_unique(
                cases[case_id]["execution_failure_records"],
                record["failure_id"],
                record,
            )
        for item, collection, key_name in (
            ("upsert_attachments", "attachments", "attachment_id"),
            ("insert_evidence", "evidence", "evidence_id"),
            ("insert_artifacts", "artifacts", "artifact_id"),
        ):
            for value in mutation_data[item]:
                target = cases[value["case_id"]][collection]
                if item == "upsert_attachments":
                    target[value[key_name]] = value
                else:
                    self._insert_unique(target, value[key_name], value)
        for record in mutation_data["insert_idempotency_records"]:
            compound_key = f"{record['operation']}:{record['idempotency_key']}"
            self._insert_unique(
                state_data["idempotency_records"],
                compound_key,
                record,
            )

        state_data["generation"] = current.generation + 1
        state_data["updated_at"] = self._clock.now()
        return StateFile.model_validate(state_data), affected_case_id



__all__ = ["CaseStateRepository"]
