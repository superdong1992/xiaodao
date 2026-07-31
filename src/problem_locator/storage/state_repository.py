"""Strict, atomic ``state.json`` implementation of the frozen StateRepository.

The repository keeps one validated in-memory snapshot, but every durable
change is a whole-file replacement through :class:`AtomicStateFileWriter`.
All snapshot reads, conditional mutation, reference validation, persistence,
and in-memory replacement run under the shared storage coordination lock.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from problem_locator.contracts import (
    CONTRACT_REVISION,
    SCHEMA_VERSION,
    ApplicationError,
    ApplicationPortError,
    Artifact,
    AttachmentStatus,
    CaseAggregate,
    Clock,
    CommitReceipt,
    ERROR_SPECS,
    ErrorCode,
    ExecutionRecordStore,
    IdGenerator,
    Job,
    JobStatus,
    OutcomeDisposition,
    ResourceKind,
    ResourceRef,
    StateExportObjectCounts,
    StateFile,
    StateMutation,
    ValidationIssue,
    ValidationReport,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)

from .atomic import FileSync, Replacer, read_stable_file_bytes
from .coordination import StorageCoordinationLock
from .execution_records import FileExecutionRecordStore
from .layout import StorageLayout
from .paths import parse_storage_key
from .platform import PlatformFileSync, PlatformReplaceOperation
from .resource_files import validate_formal_resource
from .state_atomic import AtomicStateFileWriter


def _port_error(code: ErrorCode, message: str) -> ApplicationPortError:
    return ApplicationPortError(
        ApplicationError(
            code=code,
            message=message,
            details=[],
            retryable=ERROR_SPECS[code].application_retryable,
        )
    )


def _clone[T](value: T) -> T:
    copier = getattr(value, "model_copy", None)
    if copier is None:  # pragma: no cover - every current caller passes a DTO
        raise TypeError("state repository values must be contract DTOs")
    return copier(deep=True)


def _empty_counts() -> StateExportObjectCounts:
    return StateExportObjectCounts(
        cases=0,
        jobs=0,
        outcomes=0,
        outcome_processing_records=0,
        execution_failure_records=0,
        attachments=0,
        evidence=0,
        artifacts=0,
        idempotency_records=0,
        runtime_epochs=0,
        recovery_processing_records=0,
    )


def _object_counts(state: StateFile) -> StateExportObjectCounts:
    aggregates = tuple(state.cases.values())
    return StateExportObjectCounts(
        cases=len(aggregates),
        jobs=sum(len(item.jobs) for item in aggregates),
        outcomes=sum(len(item.outcomes) for item in aggregates),
        outcome_processing_records=sum(
            len(item.outcome_processing_records) for item in aggregates
        ),
        execution_failure_records=sum(
            len(item.execution_failure_records) for item in aggregates
        ),
        attachments=sum(len(item.attachments) for item in aggregates),
        evidence=sum(len(item.evidence) for item in aggregates),
        artifacts=sum(len(item.artifacts) for item in aggregates),
        idempotency_records=len(state.idempotency_records),
        runtime_epochs=len(state.runtime_epochs),
        recovery_processing_records=len(state.recovery_processing_records),
    )


class JsonFileStateRepository:
    """One-process repository for the authoritative V1 ``state.json`` file."""

    def __init__(
        self,
        data_root: Path,
        coordination_lock: StorageCoordinationLock,
        clock: Clock,
        id_generator: IdGenerator,
        *,
        file_sync: FileSync | None = None,
        replacer: Replacer | None = None,
        execution_record_store: ExecutionRecordStore | None = None,
        read_file: Callable[[Path], bytes] = read_stable_file_bytes,
    ) -> None:
        self._layout = StorageLayout.at(Path(data_root))
        self._coordination_lock = coordination_lock
        self._clock = clock
        self._id_generator = id_generator
        self._file_sync = file_sync if file_sync is not None else PlatformFileSync()
        self._replacer = (
            replacer if replacer is not None else PlatformReplaceOperation()
        )
        self._read_file = read_file
        self._state: StateFile | None = None
        self._state_failure: ApplicationError | None = None

        try:
            self._layout.ensure_directories(self._file_sync)
        except (OSError, ValueError) as exc:
            raise _port_error(
                ErrorCode.STATE_CORRUPT,
                "The DATA_ROOT storage layout is invalid.",
            ) from exc

        self._execution_record_store = execution_record_store or FileExecutionRecordStore(
            self._layout.data_root,
            self._coordination_lock,
            self._file_sync,
            self._replacer,
        )
        self._writer = AtomicStateFileWriter(
            self._layout,
            self._file_sync,
            self._replacer,
            self._id_generator,
            read_file=self._read_file,
        )

        with self._coordination_lock:
            try:
                self._state = self._open_or_initialize()
            except ApplicationPortError:
                raise
            except (OSError, TypeError, ValueError, ValidationError) as exc:
                raise _port_error(
                    ErrorCode.STATE_CORRUPT,
                    "The stored state is corrupt or inconsistent.",
                ) from exc

    @property
    def layout(self) -> StorageLayout:
        """Expose only the fixed layout object for S02 composition and tests."""

        return self._layout

    def _open_or_initialize(self) -> StateFile:
        try:
            state_bytes = self._read_file(self._layout.state)
        except FileNotFoundError:
            if self._layout.has_business_content_without_state():
                raise _port_error(
                    ErrorCode.STATE_CORRUPT,
                    "state.json is missing while existing business content remains.",
                )
            try:
                now = self._clock.now()
                initial = StateFile(
                    schema_version=SCHEMA_VERSION,
                    contract_revision=CONTRACT_REVISION,
                    generation=1,
                    installation_id=self._id_generator.new("installation"),
                    created_at=now,
                    updated_at=now,
                    runtime_epochs=[],
                    recovery_processing_records={},
                    cases={},
                    idempotency_records={},
                )
                state_bytes = self._writer.write(canonical_json_bytes(initial))
            except (OSError, TypeError, ValueError, ValidationError) as exc:
                raise _port_error(
                    ErrorCode.STATE_WRITE_FAILED,
                    "The initial state could not be written durably.",
                ) from exc
        return self._decode_and_validate(state_bytes)

    @staticmethod
    def _decode_state(state_bytes: bytes) -> StateFile:
        try:
            payload = parse_canonical_json_bytes(state_bytes)
        except (TypeError, ValueError) as exc:
            raise _port_error(
                ErrorCode.STATE_CORRUPT,
                "state.json is not valid Canonical JSON.",
            ) from exc
        if not isinstance(payload, dict):
            raise _port_error(
                ErrorCode.STATE_CORRUPT,
                "state.json must contain a JSON object.",
            )
        if (
            "schema_version" in payload
            and payload["schema_version"] != SCHEMA_VERSION
        ) or (
            "contract_revision" in payload
            and payload["contract_revision"] != CONTRACT_REVISION
        ):
            raise _port_error(
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
                "The stored state schema or contract revision is unsupported.",
            )
        try:
            state = StateFile.model_validate(payload)
        except (TypeError, ValueError, ValidationError) as exc:
            raise _port_error(
                ErrorCode.STATE_CORRUPT,
                "state.json violates the frozen StateFile schema or invariants.",
            ) from exc
        if state.generation < 1:
            raise _port_error(
                ErrorCode.STATE_CORRUPT,
                "state.json generation must be at least one.",
            )
        return state

    def _decode_and_validate(self, state_bytes: bytes) -> StateFile:
        state = self._decode_state(state_bytes)
        try:
            self._validate_external_references(state)
        except ApplicationPortError as exc:
            if exc.error.code in {
                ErrorCode.STATE_CORRUPT,
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            }:
                raise
            raise _port_error(
                ErrorCode.STATE_CORRUPT,
                "A state-referenced execution record is invalid.",
            ) from exc
        except (OSError, TypeError, ValueError, ValidationError) as exc:
            raise _port_error(
                ErrorCode.STATE_CORRUPT,
                "A state-referenced resource or execution record is invalid.",
            ) from exc
        return state

    def _load_disk_state(self) -> StateFile:
        try:
            state_bytes = self._read_file(self._layout.state)
        except (OSError, ValueError) as exc:
            raise _port_error(
                ErrorCode.STATE_CORRUPT,
                "The authoritative state.json file cannot be read safely.",
            ) from exc
        return self._decode_and_validate(state_bytes)

    @staticmethod
    def _pending_job_version(job: Job) -> Job:
        payload = job.model_dump(mode="python")
        payload.update(
            status=JobStatus.PENDING,
            started_at=None,
            finished_at=None,
            runtime_epoch=None,
        )
        return Job.model_validate(payload)

    @staticmethod
    def _include_resource(
        resources: dict[str, ResourceRef],
        resource: ResourceRef,
        *,
        case_id: str,
        category: str,
        resource_id: str,
    ) -> None:
        address = parse_storage_key(resource.storage_key)
        if (
            address.case_id != case_id
            or address.category != category
            or address.resource_id != resource_id
            or address.resource_kind is not resource.resource_kind
        ):
            raise ValueError("formal ResourceRef identity does not match its state owner")
        existing = resources.setdefault(resource.storage_key, resource)
        if existing != resource:
            raise ValueError("one storage_key describes conflicting resource metadata")

    def _validate_external_references(self, state: StateFile) -> None:
        resources: dict[str, ResourceRef] = {}
        for case_id, aggregate in state.cases.items():
            for attachment_id, attachment in aggregate.attachments.items():
                if attachment.status is AttachmentStatus.READY:
                    assert (
                        attachment.storage_key is not None
                        and attachment.size is not None
                        and attachment.sha256 is not None
                    )
                    self._include_resource(
                        resources,
                        ResourceRef(
                            resource_kind=ResourceKind.FILE,
                            storage_key=attachment.storage_key,
                            size=attachment.size,
                            sha256=attachment.sha256,
                        ),
                        case_id=case_id,
                        category="attachments",
                        resource_id=attachment_id,
                    )
            for evidence_id, evidence in aggregate.evidence.items():
                if evidence.resource_ref is not None:
                    self._include_resource(
                        resources,
                        evidence.resource_ref,
                        case_id=case_id,
                        category="evidence",
                        resource_id=evidence_id,
                    )
            for artifact_id, artifact in aggregate.artifacts.items():
                self._include_resource(
                    resources,
                    ResourceRef(
                        resource_kind=artifact.resource_kind,
                        storage_key=artifact.storage_key,
                        size=artifact.size,
                        sha256=artifact.sha256,
                    ),
                    case_id=case_id,
                    category="artifacts",
                    resource_id=artifact_id,
                )

            for job_id, job in aggregate.jobs.items():
                published = self._execution_record_store.read_published_job(job_id)
                if published is None or published.job != self._pending_job_version(job):
                    raise ValueError(
                        "state Job does not match its immutable published job.json"
                    )

            for outcome_id, outcome in aggregate.outcomes.items():
                record = aggregate.outcome_processing_records[outcome_id]
                published = self._execution_record_store.read_published_outcome(
                    record.job_id
                )
                if (
                    published is None
                    or published.job_outcome != outcome
                    or published.outcome_file_ref != record.outcome_file_ref
                ):
                    raise ValueError(
                        "saved Outcome does not match its published job_outcome.json"
                    )

            for outcome_id, record in aggregate.outcome_processing_records.items():
                if outcome_id in aggregate.outcomes:
                    continue
                if not (
                    record.disposition is OutcomeDisposition.REJECTED
                    and record.error_code is not None
                ):
                    raise ValueError(
                        "untrusted Outcome audit is not a technical rejection"
                    )

        for resource in resources.values():
            validate_formal_resource(
                self._layout.data_root,
                resource,
                require_read_only=True,
            )

    def _require_state(self) -> StateFile:
        if self._state is None:
            failure = self._state_failure
            if failure is None:
                failure = _port_error(
                    ErrorCode.STATE_CORRUPT,
                    "The authoritative state is unavailable.",
                ).error
            raise ApplicationPortError(
                failure.model_copy(deep=True)
            )
        return self._state

    def read_case(self, case_id: str) -> CaseAggregate:
        with self._coordination_lock:
            aggregate = self._require_state().cases.get(case_id)
            if aggregate is None:
                raise _port_error(
                    ErrorCode.CASE_NOT_FOUND,
                    "The requested Case does not exist.",
                )
            return _clone(aggregate)

    def read_job(self, job_id: str) -> Job:
        with self._coordination_lock:
            for aggregate in self._require_state().cases.values():
                job = aggregate.jobs.get(job_id)
                if job is not None:
                    return _clone(job)
            raise _port_error(
                ErrorCode.JOB_NOT_FOUND,
                "The requested Job does not exist.",
            )

    def read_artifact(self, artifact_id: str) -> Artifact:
        with self._coordination_lock:
            for aggregate in self._require_state().cases.values():
                artifact = aggregate.artifacts.get(artifact_id)
                if artifact is not None:
                    return _clone(artifact)
            raise _port_error(
                ErrorCode.ARTIFACT_NOT_FOUND,
                "The requested Artifact does not exist.",
            )

    def read_snapshot(self) -> StateFile:
        with self._coordination_lock:
            try:
                return _clone(self._require_state())
            except (TypeError, ValueError, ValidationError) as exc:
                raise _port_error(
                    ErrorCode.STATE_CORRUPT,
                    "The in-memory state snapshot is invalid.",
                ) from exc

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

    def _reload_after_failed_commit(self) -> None:
        try:
            reloaded = self._load_disk_state()
        except ApplicationPortError as exc:
            self._state = None
            if exc.error.code in {
                ErrorCode.STATE_CORRUPT,
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            }:
                self._state_failure = exc.error.model_copy(deep=True)
            else:  # pragma: no cover - _load_disk_state closes to state failures
                self._state_failure = _port_error(
                    ErrorCode.STATE_CORRUPT,
                    "The authoritative state could not be reloaded safely.",
                ).error
        except (OSError, TypeError, ValueError, ValidationError):
            self._state = None
            self._state_failure = _port_error(
                ErrorCode.STATE_CORRUPT,
                "The authoritative state could not be reloaded safely.",
            ).error
        else:
            self._state = reloaded
            self._state_failure = None

    def commit(
        self,
        expected_generation: int,
        expected_case_revision: int | None,
        mutation: StateMutation,
    ) -> CommitReceipt:
        if not isinstance(expected_generation, int) or isinstance(
            expected_generation, bool
        ):
            raise TypeError("expected_generation must be an integer")
        if expected_case_revision is not None and (
            not isinstance(expected_case_revision, int)
            or isinstance(expected_case_revision, bool)
        ):
            raise TypeError("expected_case_revision must be an integer or None")
        if not isinstance(mutation, StateMutation):
            raise TypeError("mutation must be a StateMutation")

        with self._coordination_lock:
            try:
                current = self._require_state()
            except ApplicationPortError as exc:
                raise _port_error(
                    ErrorCode.STATE_WRITE_FAILED,
                    "The state repository is fail-stopped and cannot accept writes.",
                ) from exc
            if current.generation != expected_generation:
                raise _port_error(
                    ErrorCode.REVISION_CONFLICT,
                    "The state generation changed before commit.",
                )
            try:
                candidate, affected_case_id = self._apply_mutation(
                    current,
                    expected_case_revision,
                    mutation,
                )
                self._validate_external_references(candidate)
                final_bytes = self._writer.write(canonical_json_bytes(candidate))
                committed = self._decode_and_validate(final_bytes)
            except ApplicationPortError as exc:
                if exc.error.code is ErrorCode.REVISION_CONFLICT:
                    raise
                self._reload_after_failed_commit()
                raise _port_error(
                    ErrorCode.STATE_WRITE_FAILED,
                    "The state commit failed and disk state was reloaded.",
                ) from exc
            except (OSError, TypeError, ValueError, ValidationError) as exc:
                self._reload_after_failed_commit()
                raise _port_error(
                    ErrorCode.STATE_WRITE_FAILED,
                    "The state commit failed and disk state was reloaded.",
                ) from exc

            self._state = committed
            self._state_failure = None
            case_revision = (
                committed.cases[affected_case_id].case.case_revision
                if affected_case_id is not None
                else None
            )
            return CommitReceipt(
                generation=committed.generation,
                case_revision=case_revision,
            )

    def validate_all(self) -> ValidationReport:
        with self._coordination_lock:
            try:
                state = self._load_disk_state()
            except ApplicationPortError as exc:
                issue = ValidationIssue(
                    code=exc.error.code.value,
                    object_type="StateFile",
                    object_id=None,
                    field_path=None,
                    message=exc.error.message,
                )
                return ValidationReport(
                    valid=False,
                    schema_version=None,
                    contract_revision=None,
                    generation=None,
                    object_counts=_empty_counts(),
                    errors=[issue],
                )
            except (OSError, TypeError, ValueError, ValidationError):
                issue = ValidationIssue(
                    code=ErrorCode.STATE_CORRUPT.value,
                    object_type="StateFile",
                    object_id=None,
                    field_path=None,
                    message="The stored state could not be validated.",
                )
                return ValidationReport(
                    valid=False,
                    schema_version=None,
                    contract_revision=None,
                    generation=None,
                    object_counts=_empty_counts(),
                    errors=[issue],
                )
            return ValidationReport(
                valid=True,
                schema_version=state.schema_version,
                contract_revision=state.contract_revision,
                generation=state.generation,
                object_counts=_object_counts(state),
                errors=[],
            )

    def export_snapshot(self) -> bytes:
        with self._coordination_lock:
            state = self._load_disk_state()
            return canonical_json_bytes(state)


__all__ = ["JsonFileStateRepository"]
