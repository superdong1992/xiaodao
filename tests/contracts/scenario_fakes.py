"""Small observable harnesses for cross-port S00 contract scenarios.

These helpers deliberately implement no product slice.  They compose only the
public contracts and the shared S00 fakes so the recovery, upload, and revision
protocols can be frozen before S01--S08 exist.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from pydantic import BaseModel

from problem_locator.contracts.enums import (
    ArtifactKind,
    AssetKind,
    AttachmentStatus,
    ErrorCode,
    OutcomeDisposition,
    ResourceKind,
    ResourceType,
)
from problem_locator.contracts.limits import MAX_ATTACHMENT_BYTES
from problem_locator.contracts.models import (
    ApplicationError,
    ApplicationErrorDetail,
    AssetAvailabilityReport,
    BusinessReceipt,
    IdempotencyRecord,
    Job,
    JobOutcome,
    PlannedResourceTarget,
    ProblemSpec,
    ProblemSpecPatch,
    ResolvedAsset,
    ResourceRef,
    RuntimeBindings,
    RuntimeExecutionReceipt,
    UploadDescriptor,
    VersionedRef,
)
from problem_locator.contracts.serialization import canonical_json_bytes

from tests.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeClock,
    InMemoryAttachmentUploadGuard,
    InMemoryBinaryStream,
    InMemoryCancellationSignal,
    InMemoryExecutionRecordStore,
    InMemoryPublicationCommitGuard,
    InMemoryResourceStore,
    ScriptedRuntime,
)


CASE_ID = "00000000-0000-0000-0000-000000000001"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000050"
INSTALLATION_ID = "00000000-0000-0000-0000-000000000099"
FIXED_OCCURRED_AT = "2026-07-31T00:02:00.000Z"


class ScenarioError(Exception):
    """Expose the exact public error produced by a frozen scenario branch."""

    def __init__(self, error: ApplicationError) -> None:
        self.error = error
        super().__init__(error.message)


class CountingBinaryStream:
    """A valid forward-only stream whose memory use is independent of size.

    The logical byte count can be multi-GiB, but instances retain only counters;
    reads reuse a single bounded zero chunk.  Upload command-boundary tests can
    therefore exercise size metadata without allocating the declared body.
    """

    _CHUNK = bytes(1024 * 1024)

    def __init__(self, logical_size: int) -> None:
        if logical_size < 0:
            raise ValueError("logical_size must be non-negative")
        self.logical_size = logical_size
        self._remaining = logical_size
        self._closed = False
        self.read_calls = 0
        self.returned_logical_bytes = 0
        self.close_calls = 0
        self._lock = threading.Lock()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def read(self, max_bytes: int) -> bytes:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        with self._lock:
            if self._closed:
                raise ValueError("stream is closed")
            self.read_calls += 1
            size = min(self._remaining, max_bytes, len(self._CHUNK))
            if size == 0:
                return b""
            self._remaining -= size
            self.returned_logical_bytes += size
            return self._CHUNK[:size]

    def close(self) -> None:
        with self._lock:
            self.close_calls += 1
            self._closed = True

    def __enter__(self) -> CountingBinaryStream:
        if self.closed:
            raise ValueError("stream is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def application_error(code: ErrorCode, message: str) -> ApplicationError:
    return ApplicationError(
        code=code,
        message=message,
        details=[],
        retryable=code
        in {
            ErrorCode.REVISION_CONFLICT,
            ErrorCode.STATE_WRITE_FAILED,
            ErrorCode.RESOURCE_PUBLISH_FAILED,
        },
    )


def validate_put_content_type(
    descriptor: UploadDescriptor,
    actual_content_type: str,
) -> None:
    """Freeze S06's literal PUT header comparison without implementing S06."""

    expected = descriptor.required_headers["Content-Type"]
    if actual_content_type != expected:
        raise ScenarioError(
            application_error(
                ErrorCode.VALIDATION_ERROR,
                "PUT Content-Type must exactly match the UploadDescriptor.",
            )
        )


def normalized_request_hash(command: BaseModel) -> str:
    """Hash one command after applying its public hash exclusion metadata."""

    schema_extra = command.model_config.get("json_schema_extra") or {}
    excluded = set(schema_extra.get("hash_excluded_fields", []))
    value = command.model_dump(mode="json", exclude=excluded)
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class IdempotencyScenario:
    """Minimal business-receipt ledger for the S00 idempotency matrix."""

    def __init__(self, *, case_revision: int = 0) -> None:
        self.case_revision = case_revision
        self.clock = FakeClock(FIXED_OCCURRED_AT)
        self.ids = DeterministicIdGenerator(seed="idempotency-scenario")
        self.records: dict[str, IdempotencyRecord] = {}

    def submit(self, command: BaseModel) -> tuple[BusinessReceipt, bool]:
        operation = type(command).__name__
        idempotency_key = getattr(command, "idempotency_key")
        record_key = f"{operation}:{idempotency_key}"
        request_hash = normalized_request_hash(command)
        previous = self.records.get(record_key)
        if previous is not None:
            if previous.request_hash != request_hash:
                raise ScenarioError(
                    application_error(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "The idempotency key is already bound to another request.",
                    )
                )
            return previous.business_receipt, True

        expected_revision = getattr(command, "expected_case_revision", None)
        if expected_revision is not None and expected_revision != self.case_revision:
            raise ScenarioError(
                application_error(
                    ErrorCode.REVISION_CONFLICT,
                    "The expected Case revision is stale.",
                )
            )

        self.case_revision = 1 if self.case_revision == 0 else self.case_revision + 1
        case_id = getattr(command, "case_id", CASE_ID)
        receipt = BusinessReceipt(
            operation=operation,
            primary_resource_id=self.ids.derive(
                "business_receipt",
                [operation, idempotency_key],
            ),
            case_id=case_id,
            case_revision=self.case_revision,
            job_id=None,
            status="ACCEPTED",
        )
        self.records[record_key] = IdempotencyRecord(
            operation=operation,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            business_receipt=receipt,
            case_id=case_id,
            created_at=self.clock.now(),
        )
        return receipt, False


class OutcomeAuditScenario:
    """Observable natural-key handling for duplicate and late Outcomes."""

    def __init__(
        self,
        *,
        active_job_id: str,
        case_revision: int,
        diagnosis_state_revision: int,
    ) -> None:
        self.active_job_id = active_job_id
        self.case_revision = case_revision
        self.diagnosis_state_revision = diagnosis_state_revision
        self.records: dict[str, tuple[str, OutcomeDisposition]] = {}

    def submit(self, outcome: JobOutcome) -> OutcomeDisposition:
        outcome_hash = hashlib.sha256(canonical_json_bytes(outcome)).hexdigest()
        existing = self.records.get(outcome.outcome_id)
        if existing is not None:
            if existing[0] == outcome_hash:
                return OutcomeDisposition.DUPLICATE
            return OutcomeDisposition.REJECTED

        if (
            outcome.job_id != self.active_job_id
            or outcome.base_state_revision != self.diagnosis_state_revision
        ):
            disposition = OutcomeDisposition.STALE
            self.case_revision += 1
        else:
            disposition = OutcomeDisposition.APPLIED
            self.case_revision += 1
            self.diagnosis_state_revision += 1
        self.records[outcome.outcome_id] = (outcome_hash, disposition)
        return disposition


def runtime_bindings(version: str, digest_character: str) -> RuntimeBindings:
    """Create a complete, internally consistent DIAGNOSE binding set."""

    digest = digest_character * 64

    def ref(asset_id: str) -> VersionedRef:
        return VersionedRef(id=asset_id, version=version, content_hash=digest)

    from problem_locator.contracts.models import default_resource_limits
    from problem_locator.contracts.enums import JobType

    return RuntimeBindings(
        agent_profile_ref=ref("specialist-profile"),
        available_skill_refs=[],
        skill_ref=ref("rpc-timeout"),
        tool_bundle_ref=ref("diagnosis-tools"),
        context_policy_ref=ref("diagnose-context"),
        output_contract_ref=ref("diagnosis-outcome"),
        logparse_tool_ref=ref("logparse"),
        logparse_product="payment-service",
        resource_limits=default_resource_limits(JobType.DIAGNOSE),
    )


def assets_for_bindings(bindings: RuntimeBindings) -> list[ResolvedAsset]:
    pairs: list[tuple[VersionedRef, AssetKind]] = [
        (bindings.agent_profile_ref, AssetKind.AGENT_PROFILE),
        (bindings.tool_bundle_ref, AssetKind.TOOL_BUNDLE),
        (bindings.context_policy_ref, AssetKind.CONTEXT_POLICY),
        (bindings.output_contract_ref, AssetKind.OUTPUT_CONTRACT),
    ]
    pairs.extend(
        (ref, AssetKind.DIAGNOSIS_SKILL)
        for ref in bindings.available_skill_refs
    )
    if bindings.skill_ref is not None:
        pairs.append((bindings.skill_ref, AssetKind.DIAGNOSIS_SKILL))
    if bindings.logparse_tool_ref is not None:
        pairs.append((bindings.logparse_tool_ref, AssetKind.LOGPARSE_TOOL))
    unique: dict[tuple[str, str, str], tuple[VersionedRef, AssetKind]] = {}
    for ref, kind in pairs:
        unique[(ref.id, ref.version, ref.content_hash)] = (ref, kind)
    return [
        ResolvedAsset(
            ref=ref,
            asset_kind=kind,
            root_path=f"/resolved/{ref.id}/{ref.version}",
        )
        for ref, kind in unique.values()
    ]


def bindings_from_job(job: Job) -> RuntimeBindings:
    return RuntimeBindings(
        agent_profile_ref=job.agent_profile_ref,
        available_skill_refs=job.available_skill_refs,
        skill_ref=job.skill_ref,
        tool_bundle_ref=job.tool_bundle_ref,
        context_policy_ref=job.context_policy_ref,
        output_contract_ref=job.output_contract_ref,
        logparse_tool_ref=job.logparse_tool_ref,
        logparse_product=job.logparse_product,
        resource_limits=job.resource_limits,
    )


def job_asset_refs(job: Job) -> list[VersionedRef]:
    refs = [job.agent_profile_ref, *job.available_skill_refs]
    refs.extend(
        ref
        for ref in (
            job.skill_ref,
            job.tool_bundle_ref,
            job.context_policy_ref,
            job.output_contract_ref,
            job.logparse_tool_ref,
        )
        if ref is not None
    )
    return refs


def claim_asset_error(job: Job, catalog: FakeAssetCatalog) -> ApplicationError | None:
    report: AssetAvailabilityReport = catalog.check(job_asset_refs(job))
    if report.available:
        return None
    details = [
        ApplicationErrorDetail(
            field="runtime_bindings",
            resource_type="asset",
            resource_id=None,
            resource_ref=ref,
            expected="available",
            actual="missing",
            limit=None,
            observed=None,
        )
        for ref in report.missing_refs
    ]
    return ApplicationError(
        code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
        message="One or more Job-pinned assets are unavailable.",
        details=details,
        retryable=False,
    )


@dataclass(frozen=True, slots=True)
class ReplayObservation:
    artifact_id: str
    candidate_id: str
    next_job_id: str
    occurred_at: str
    next_job: Job
    next_job_bytes: bytes
    resource_ref: ResourceRef
    resource_bytes: bytes
    capacity_new_bytes: int


class ExecutionReplayScenario:
    """Publish-then-commit-fault and restart replay across public Ports."""

    def __init__(
        self,
        *,
        source_job: Job,
        next_job_template: Job,
        outcome: JobOutcome,
        user_result_bytes: bytes,
        execution_records: InMemoryExecutionRecordStore,
        resources: InMemoryResourceStore,
        publication_guard: InMemoryPublicationCommitGuard,
        ids: DeterministicIdGenerator,
        clock: FakeClock,
        catalog: FakeAssetCatalog,
        runtime: ScriptedRuntime | None,
    ) -> None:
        self.source_job = source_job
        self.next_job_template = next_job_template
        self.outcome = outcome
        self.user_result_bytes = user_result_bytes
        self.execution_records = execution_records
        self.resources = resources
        self.publication_guard = publication_guard
        self.ids = ids
        self.clock = clock
        self.catalog = catalog
        self.runtime = runtime
        self.last_observation: ReplayObservation | None = None
        self.state_commit_attempts = 0

    def _derived_ids(self) -> tuple[str, str, str]:
        common = [INSTALLATION_ID, self.outcome.case_id, self.outcome.outcome_id]
        artifact_key = self.outcome.proposed_artifacts[0].proposal_key
        candidate_key = self.outcome.payload.candidate_conclusion_draft.proposal_key  # type: ignore[union-attr]
        return (
            self.ids.derive("artifact", [*common, artifact_key]),
            self.ids.derive("candidate_conclusion", [*common, candidate_key]),
            self.ids.derive("job", [*common, "next_job"]),
        )

    def _next_job(
        self,
        bindings: RuntimeBindings,
        next_job_id: str,
        occurred_at: str,
    ) -> Job:
        value = self.next_job_template.model_dump(mode="python")
        value.update(
            {
                "job_id": next_job_id,
                "case_id": self.outcome.case_id,
                "previous_outcome_refs": [self.outcome.outcome_id],
                "agent_profile_ref": bindings.agent_profile_ref,
                "available_skill_refs": bindings.available_skill_refs,
                "skill_ref": bindings.skill_ref,
                "tool_bundle_ref": bindings.tool_bundle_ref,
                "context_policy_ref": bindings.context_policy_ref,
                "output_contract_ref": bindings.output_contract_ref,
                "logparse_tool_ref": bindings.logparse_tool_ref,
                "logparse_product": bindings.logparse_product,
                "resource_limits": bindings.resource_limits,
                "created_at": occurred_at,
                "status": "PENDING",
                "started_at": None,
                "finished_at": None,
                "runtime_epoch": None,
            }
        )
        return Job.model_validate(value)

    def _resource_target(self, artifact_id: str) -> PlannedResourceTarget:
        proposal = self.outcome.proposed_artifacts[0]
        assert proposal.artifact_kind is ArtifactKind.USER_RESULT
        return self.resources.plan_target(
            self.outcome.case_id,
            ResourceType.ARTIFACT,
            artifact_id,
            proposal.resource_kind,
            proposal.size,
            proposal.sha256,
        )

    @staticmethod
    def _read_all(resources: InMemoryResourceStore, resource_ref: ResourceRef) -> bytes:
        chunks: list[bytes] = []
        with resources.open_read(resource_ref) as stream:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)

    def deliver_then_fail_state_commit(self) -> None:
        if self.runtime is None:
            raise AssertionError("the first delivery requires a Runtime")
        runtime_receipt: RuntimeExecutionReceipt = self.runtime.execute(
            self.source_job,
            InMemoryCancellationSignal(),
        )
        assert runtime_receipt.job_outcome == self.outcome
        artifact_id, candidate_id, next_job_id = self._derived_ids()
        occurred_at = self.clock.now()
        assert self.source_job.skill_ref is not None
        bindings = self.catalog.diagnose_bindings(self.source_job.skill_ref)
        next_job = self._next_job(bindings, next_job_id, occurred_at)
        proposal = self.outcome.proposed_artifacts[0]
        staged = self.resources.stage_file(
            self.source_job.job_id,
            proposal.proposal_key,
            InMemoryBinaryStream(self.user_result_bytes),
            expected_size=proposal.size,
            expected_sha256=proposal.sha256,
        )
        target = self._resource_target(artifact_id)
        lease = self.publication_guard.acquire()
        try:
            usage = self.resources.validate_case_capacity(
                self.outcome.case_id,
                [target],
            )
            resource_ref = self.resources.publish(staged, target.final_storage_key)
            self.execution_records.publish_job(next_job)
        finally:
            lease.release()

        self.last_observation = ReplayObservation(
            artifact_id=artifact_id,
            candidate_id=candidate_id,
            next_job_id=next_job_id,
            occurred_at=occurred_at,
            next_job=next_job,
            next_job_bytes=canonical_json_bytes(next_job),
            resource_ref=resource_ref,
            resource_bytes=self._read_all(self.resources, resource_ref),
            capacity_new_bytes=usage.new_bytes,
        )
        self.state_commit_attempts += 1
        raise ScenarioError(
            application_error(
                ErrorCode.STATE_WRITE_FAILED,
                "The state commit failed after durable publication.",
            )
        )

    def replay_after_restart(self) -> ReplayObservation:
        runtime_receipt = self.execution_records.read_published_outcome(
            self.source_job.job_id
        )
        if runtime_receipt is None:
            raise AssertionError("the durable Outcome is required for replay")
        self.outcome = runtime_receipt.job_outcome
        artifact_id, candidate_id, next_job_id = self._derived_ids()
        published_job = self.execution_records.read_published_job(next_job_id)
        if published_job is None:
            raise AssertionError("the complete prepublished Job is required for replay")
        target = self._resource_target(artifact_id)
        lease = self.publication_guard.acquire()
        try:
            usage = self.resources.validate_case_capacity(
                self.outcome.case_id,
                [target],
            )
        finally:
            lease.release()
        resource_ref = ResourceRef(
            resource_kind=target.resource_kind,
            storage_key=target.final_storage_key,
            size=target.size,
            sha256=target.sha256,
        )
        observation = ReplayObservation(
            artifact_id=artifact_id,
            candidate_id=candidate_id,
            next_job_id=next_job_id,
            occurred_at=published_job.job.created_at,
            next_job=published_job.job,
            next_job_bytes=canonical_json_bytes(published_job.job),
            resource_ref=resource_ref,
            resource_bytes=self._read_all(self.resources, resource_ref),
            capacity_new_bytes=usage.new_bytes,
        )
        self.last_observation = observation
        return observation


class UploadScenario:
    """Attachment stream/post-stage protocol with an injected commit fault."""

    def __init__(
        self,
        *,
        attachment_id: str,
        declared_sha256: str,
        content_type: str,
        declared_size: int,
        generation: int = 10,
    ) -> None:
        self.attachment_id = attachment_id
        self.declared_sha256 = declared_sha256
        self.content_type = content_type
        self.declared_size = declared_size
        self.generation = generation
        self.status = AttachmentStatus.UPLOADING
        self.upload_guard = InMemoryAttachmentUploadGuard()
        self.publication_guard = InMemoryPublicationCommitGuard()
        self.resources = InMemoryResourceStore(
            upload_guard=self.upload_guard,
            publication_guard=self.publication_guard,
        )
        self.snapshot_reads: list[tuple[str, int]] = []
        self.commit_expected_generations: list[int] = []
        self.capacity_new_bytes: list[int] = []
        self.events: list[str] = []
        self._staged: Any | None = None
        self._formal: ResourceRef | None = None

    def descriptor(self, url: str) -> UploadDescriptor:
        return UploadDescriptor(
            attachment_id=self.attachment_id,
            method="PUT",
            url=url,
            required_headers={
                "Idempotency-Key": self.attachment_id,
                "Content-Type": self.content_type,
                "Content-Length": str(self.declared_size),
                "X-Content-SHA256": self.declared_sha256,
            },
            max_bytes=MAX_ATTACHMENT_BYTES,
            expires_at=None,
        )

    def _snapshot(self, phase: str) -> int:
        self.snapshot_reads.append((phase, self.generation))
        self.events.append(f"snapshot:{phase}")
        return self.generation

    def _target(self, sha256: str) -> PlannedResourceTarget:
        return self.resources.plan_target(
            CASE_ID,
            ResourceType.ATTACHMENT,
            self.attachment_id,
            ResourceKind.FILE,
            self.declared_size,
            sha256,
        )

    def publish_then_fail_ready_commit(
        self,
        body: InMemoryBinaryStream,
        *,
        expected_sha256: str,
    ) -> None:
        if expected_sha256 != self.declared_sha256:
            raise ScenarioError(
                application_error(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "The attachment upload is bound to different bytes.",
                )
            )
        self._snapshot("early")
        upload_lease = self.upload_guard.acquire(self.attachment_id)
        try:
            self.events.append("body:consume")
            with body:
                self._staged = self.resources.stage_attachment(
                    self.attachment_id,
                    upload_lease,
                    body,
                    expected_size=self.declared_size,
                    expected_sha256=expected_sha256,
                )

            # An unrelated state change while the body was in flight makes the
            # early generation stale.  Only the post-stage snapshot may commit.
            self.generation += 1
            expected_generation = self._snapshot("post-stage")
            target = self._target(expected_sha256)
            lease = self.publication_guard.acquire()
            try:
                usage = self.resources.validate_case_capacity(CASE_ID, [target])
                self.capacity_new_bytes.append(usage.new_bytes)
                self.events.append("publish")
                self._formal = self.resources.publish(
                    self._staged,
                    target.final_storage_key,
                )
                self.commit_expected_generations.append(expected_generation)
                self.events.append("commit:failed")
            finally:
                lease.release()

            # The formal bytes survived, while READY and the idempotency record
            # did not.  A later retry must adopt rather than re-read the body.
            self.generation += 1
            raise ScenarioError(
                application_error(
                    ErrorCode.STATE_WRITE_FAILED,
                    "READY state commit failed after resource publication.",
                )
            )
        finally:
            upload_lease.release()

    def resume_after_commit_failure(self, *, expected_sha256: str) -> ResourceRef:
        if expected_sha256 != self.declared_sha256:
            raise ScenarioError(
                application_error(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "The attachment upload is bound to different bytes.",
                )
            )
        if self._staged is None or self._formal is None:
            raise AssertionError("there is no completed staged upload to resume")
        expected_generation = self._snapshot("retry-post-stage")
        target = self._target(expected_sha256)
        lease = self.publication_guard.acquire()
        try:
            usage = self.resources.validate_case_capacity(CASE_ID, [target])
            self.capacity_new_bytes.append(usage.new_bytes)
            self.events.append("adopt")
            adopted = self.resources.publish(self._staged, target.final_storage_key)
            self.commit_expected_generations.append(expected_generation)
            self.status = AttachmentStatus.READY
            self.generation += 1
            self.events.append("commit:ready")
        finally:
            lease.release()
        return adopted


class ProblemSpecScenario:
    """Mechanical patch application with the frozen target-change gate."""

    _TARGET_FIELDS = frozenset(
        {
            "statement",
            "expected_behavior",
            "scope",
            "goals",
            "completion_criteria",
        }
    )

    def __init__(
        self,
        problem_spec: ProblemSpec,
        *,
        case_revision: int,
        diagnosis_state_revision: int,
    ) -> None:
        self.problem_spec = problem_spec
        self.case_revision = case_revision
        self.diagnosis_state_revision = diagnosis_state_revision

    def apply_patch(self, patch: ProblemSpecPatch) -> bool:
        supplied = patch.model_dump(mode="python", exclude_unset=True)
        changes = {
            field: value
            for field, value in supplied.items()
            if getattr(self.problem_spec, field) != value
        }
        if not changes:
            return False
        if self._TARGET_FIELDS.intersection(changes):
            raise ScenarioError(
                application_error(
                    ErrorCode.NEW_CASE_REQUIRED,
                    "The patch changes the stable diagnosis target.",
                )
            )
        value = self.problem_spec.model_dump(mode="python")
        value.update(changes)
        value["revision"] = self.problem_spec.revision + 1
        self.problem_spec = ProblemSpec.model_validate(value)
        self.case_revision += 1
        self.diagnosis_state_revision += 1
        return True

    def apply_semantic_change_without_problem_patch(self) -> None:
        self.case_revision += 1
        self.diagnosis_state_revision += 1
