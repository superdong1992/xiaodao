from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from problem_locator.application.preparation import runtime_bindings_from_job
from problem_locator.contracts import (
    ApplicationPortError,
    ArtifactKind,
    CandidateStatus,
    DiagnosisOutcome,
    ErrorCode,
    EvidenceSourceType,
    Job,
    JobOutcome,
    JobStatus,
    OutcomeDisposition,
    ResourceKind,
    ResourceRef,
    ResourceType,
    RuntimeExecutionReceipt,
    StateMutation,
    canonical_json_bytes,
)
from problem_locator.dispatch import (
    CancellationController,
    RecoveryCoordinator,
    RuntimeEpochContext,
    RuntimeEpochFactory,
)
from problem_locator.storage.coordination import (
    AttachmentUploadRegistry,
    InProcessAttachmentUploadGuard,
    InProcessPublicationCommitGuard,
    StorageCoordinationLock,
)
from problem_locator.storage.execution_records import FileExecutionRecordStore
from problem_locator.storage.layout import StorageLayout
from problem_locator.storage.paths import proposal_stage_path
from problem_locator.storage.platform import PlatformFileSync, PlatformReplaceOperation
from problem_locator.storage.quarantine import QuarantineMover
from problem_locator.storage.resource_store import FileResourceStore
from problem_locator.storage.retention import RetentionScanner
from problem_locator.storage.retention_cleaner import StorageRetentionCleaner
from problem_locator.storage.state_repository import JsonFileStateRepository
from tests.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeClock,
    InMemoryBinaryStream,
)
from tests.contracts.scenario_fakes import assets_for_bindings, bindings_from_job
from tests.integration import test_runtime_dispatch_recovery as recovery_seam
from tests.unit.dispatch.fakes import (
    ManualSubmissionBackoff,
    RecordingDispatcher as RecoveryDispatcher,
)


ROOT = Path(__file__).resolve().parents[2]
FAILURE_MATRIX = ROOT / "tests/fixtures/failures/failure-matrix.json"
APP_ID_SEED = "s08-candidate-outbox-application"
STATE_ID_SEED = "s08-candidate-outbox-state"
RESOURCE_ID_SEED = "s08-candidate-outbox-resources"
RECOVERY_EPOCH_SEED = "s08-candidate-outbox-recovery-epoch"
EXPIRED_MTIME = 1.0
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


_REPLACE_BEFORE = _failure_scenario("replace_before")
_REPLACE_AFTER_POINTS = tuple(
    _failure_scenario("replace_after")["failure_points"]
)


@dataclass(slots=True)
class _ParkedCandidateGraph:
    data_root: Path
    layout: StorageLayout
    lock: StorageCoordinationLock
    registry: AttachmentUploadRegistry
    records: FileExecutionRecordStore
    repository: JsonFileStateRepository
    resources: FileResourceStore
    source: Job
    review_template: Job
    outcome: JobOutcome
    durable: RuntimeExecutionReceipt
    attempted_mutation: StateMutation
    runtime: Any
    result_bytes: bytes
    next_job_id: str
    next_job_bytes: bytes
    artifact_ids_by_key: dict[str, str]
    evidence_ids_by_key: dict[str, str]
    candidate_id: str
    existing_evidence_bytes_by_id: dict[str, bytes]
    formal_paths_by_key: dict[str, Path]
    formal_bytes_by_key: dict[str, dict[str, bytes]]
    stage_paths_by_key: dict[str, Path]
    stage_bytes_by_key: dict[str, dict[str, bytes]]


@dataclass(slots=True)
class _RestartedGraph:
    records: FileExecutionRecordStore
    repository: JsonFileStateRepository
    resources: FileResourceStore
    application: Any
    control: Any
    result: Any


class _CapturingFaultingStateRepository(
    recovery_seam._FaultingStateRepository
):
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.attempted_outcome_mutations: list[StateMutation] = []
        super().__init__(*args, **kwargs)

    def commit(
        self,
        expected_generation: int,
        expected_case_revision: int | None,
        mutation: StateMutation,
    ):
        if mutation.insert_outcomes:
            self.attempted_outcome_mutations.append(mutation.model_copy(deep=True))
        return super().commit(
            expected_generation,
            expected_case_revision,
            mutation,
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


def _candidate_catalog(source: Job, review_template: Job) -> FakeAssetCatalog:
    assert source.skill_ref is not None
    return FakeAssetCatalog(
        assets=[
            *assets_for_bindings(bindings_from_job(source)),
            *assets_for_bindings(bindings_from_job(review_template)),
        ],
        review={
            (
                source.skill_ref.id,
                source.skill_ref.version,
                source.skill_ref.content_hash,
            ): runtime_bindings_from_job(review_template)
        },
    )


def _path_bytes(path: Path) -> dict[str, bytes]:
    if path.is_file():
        return {".": path.read_bytes()}
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def _park_candidate_after_resource_and_job_publish(
    tmp_path: Path,
) -> _ParkedCandidateGraph:
    data_root = tmp_path / "data"
    layout = StorageLayout.at(data_root)
    layout.ensure_directories()
    lock = StorageCoordinationLock()
    publication_guard = InProcessPublicationCommitGuard(lock)
    registry = AttachmentUploadRegistry()
    upload_guard = InProcessAttachmentUploadGuard(registry)
    resources = FileResourceStore(
        layout,
        lock,
        registry,
        DeterministicIdGenerator(seed=RESOURCE_ID_SEED),
    )
    attachment = recovery_seam._publish_attachment(
        resources,
        publication_guard,
        upload_guard,
    )
    state, source = recovery_seam._seed_diagnosis_state(attachment)
    records = FileExecutionRecordStore(data_root, lock)
    with publication_guard.acquire():
        records.publish_job(source)
    layout.state.write_bytes(canonical_json_bytes(state))
    repository = _CapturingFaultingStateRepository(
        data_root,
        lock,
        FakeClock(recovery_seam.FIXED_TIME),
        DeterministicIdGenerator(seed=STATE_ID_SEED),
        execution_record_store=records,
    )
    repository.outcome_commit_faults.append(
        ErrorCode(_REPLACE_BEFORE["error_code"])
    )
    review_template = Job.model_validate(recovery_seam._json("job-review.json"))
    application = recovery_seam._route_application(
        data_root,
        repository,
        records,
        resources,
        publication_guard,
        upload_guard,
        _candidate_catalog(source, review_template),
        id_seed=APP_ID_SEED,
    )
    claim = application.claim_job(source.job_id, recovery_seam.EPOCH)
    assert claim.claimed is True
    assert claim.job is not None and claim.job.status is JobStatus.RUNNING
    outcome, result_bytes = recovery_seam._candidate_outcome_with_real_resources(
        resources,
        claim.job,
        attachment,
        tmp_path,
    )
    runtime = recovery_seam._PublishingRuntime(records, outcome, repository)
    finalized = runtime.execute(claim.job, CancellationController())

    with pytest.raises(ApplicationPortError) as failed_commit:
        application.submit_outcome(
            finalized.job_outcome,
            finalized.outcome_file_ref,
        )
    assert failed_commit.value.error.code is ErrorCode.STATE_WRITE_FAILED
    assert repository.outcome_commit_attempts == 1
    assert len(repository.attempted_outcome_mutations) == 1
    assert len(runtime.calls) == 1
    assert len(runtime.receipts) == 1

    parked_state = repository.read_snapshot()
    parked_case = parked_state.cases[recovery_seam.CASE_ID]
    assert parked_case.jobs[source.job_id].status is JobStatus.RUNNING
    assert outcome.outcome_id not in parked_case.outcome_processing_records
    durable = records.read_published_outcome(source.job_id)
    assert durable is not None
    assert durable.outcome_file_ref == finalized.outcome_file_ref
    assert canonical_json_bytes(durable.job_outcome) == canonical_json_bytes(outcome)

    ids = DeterministicIdGenerator(seed=APP_ID_SEED)
    artifact_ids_by_key: dict[str, str] = {}
    evidence_ids_by_key = {
        proposal.proposal_key: ids.derive(
            "evidence",
            [
                parked_state.installation_id,
                outcome.case_id,
                outcome.outcome_id,
                proposal.proposal_key,
            ],
        )
        for proposal in outcome.proposed_evidence
    }
    assert isinstance(outcome.payload, DiagnosisOutcome)
    candidate_draft = outcome.payload.candidate_conclusion_draft
    assert candidate_draft is not None
    candidate_id = ids.derive(
        "candidate_conclusion",
        [
            parked_state.installation_id,
            outcome.case_id,
            outcome.outcome_id,
            candidate_draft.proposal_key,
        ],
    )
    formal_paths_by_key: dict[str, Path] = {}
    stage_paths_by_key: dict[str, Path] = {}
    for proposal in outcome.proposed_artifacts:
        staged = proposal.staged_resource_ref
        artifact_id = ids.derive(
            "artifact",
            [
                parked_state.installation_id,
                outcome.case_id,
                outcome.outcome_id,
                proposal.proposal_key,
            ],
        )
        target = resources.plan_target(
            outcome.case_id,
            ResourceType.ARTIFACT,
            artifact_id,
            staged.resource_kind,
            staged.size,
            staged.sha256,
        )
        formal_path = data_root / target.final_storage_key
        stage_path = proposal_stage_path(
            data_root,
            staged.owner_job_id,
            staged.proposal_key,
        )
        assert formal_path.exists()
        assert stage_path.is_dir()
        assert (stage_path / "staged.json").is_file()
        consumed_name = (
            "payload"
            if staged.resource_kind is ResourceKind.FILE
            else "tree"
        )
        assert (stage_path / consumed_name).exists() is False
        artifact_ids_by_key[proposal.proposal_key] = artifact_id
        formal_paths_by_key[proposal.proposal_key] = formal_path
        stage_paths_by_key[proposal.proposal_key] = stage_path

    next_job_id = ids.derive(
        "job",
        [
            parked_state.installation_id,
            outcome.case_id,
            outcome.outcome_id,
            "next_job",
        ],
    )
    prepublished = records.read_published_job(next_job_id)
    assert prepublished is not None
    next_job_path = data_root / "jobs" / next_job_id / "job.json"
    next_job_bytes = next_job_path.read_bytes()
    assert next_job_bytes == canonical_json_bytes(prepublished.job)

    return _ParkedCandidateGraph(
        data_root=data_root,
        layout=layout,
        lock=lock,
        registry=registry,
        records=records,
        repository=repository,
        resources=resources,
        source=source,
        review_template=review_template,
        outcome=outcome,
        durable=durable,
        attempted_mutation=repository.attempted_outcome_mutations[0],
        runtime=runtime,
        result_bytes=result_bytes,
        next_job_id=next_job_id,
        next_job_bytes=next_job_bytes,
        artifact_ids_by_key=artifact_ids_by_key,
        evidence_ids_by_key=evidence_ids_by_key,
        candidate_id=candidate_id,
        existing_evidence_bytes_by_id={
            evidence_id: canonical_json_bytes(evidence)
            for evidence_id, evidence in parked_case.evidence.items()
        },
        formal_paths_by_key=formal_paths_by_key,
        formal_bytes_by_key={
            key: _path_bytes(path) for key, path in formal_paths_by_key.items()
        },
        stage_paths_by_key=stage_paths_by_key,
        stage_bytes_by_key={
            key: _path_bytes(path) for key, path in stage_paths_by_key.items()
        },
    )


def _restart_and_recover(graph: _ParkedCandidateGraph) -> _RestartedGraph:
    restart_lock = StorageCoordinationLock()
    restart_guard = InProcessPublicationCommitGuard(restart_lock)
    restart_registry = AttachmentUploadRegistry()
    restart_records = FileExecutionRecordStore(graph.data_root, restart_lock)
    restart_repository = JsonFileStateRepository(
        graph.data_root,
        restart_lock,
        FakeClock(recovery_seam.FIXED_TIME),
        DeterministicIdGenerator(seed=f"{STATE_ID_SEED}-restart"),
        execution_record_store=restart_records,
    )
    restart_resources = FileResourceStore(
        graph.layout,
        restart_lock,
        restart_registry,
        DeterministicIdGenerator(seed=f"{RESOURCE_ID_SEED}-restart"),
    )
    dispatcher = RecoveryDispatcher()
    application = recovery_seam._route_application(
        graph.data_root,
        restart_repository,
        restart_records,
        restart_resources,
        restart_guard,
        InProcessAttachmentUploadGuard(restart_registry),
        _candidate_catalog(graph.source, graph.review_template),
        dispatcher=dispatcher,
        id_seed=APP_ID_SEED,
    )
    control = recovery_seam._RecordingJobControl(application)
    result = RecoveryCoordinator(
        restart_repository,
        restart_records,
        control,
        dispatcher,  # type: ignore[arg-type]
        RuntimeEpochFactory(
            DeterministicIdGenerator(seed=RECOVERY_EPOCH_SEED)
        ),
        RuntimeEpochContext(),
        submission_backoff=ManualSubmissionBackoff(),
    ).recover()
    return _RestartedGraph(
        records=restart_records,
        repository=restart_repository,
        resources=restart_resources,
        application=application,
        control=control,
        result=result,
    )


def _assert_recovery_reuses_parked_bytes(
    graph: _ParkedCandidateGraph,
    restarted: _RestartedGraph,
) -> None:
    assert restarted.result.completed is True
    assert restarted.result.replayed_job_ids == (graph.source.job_id,)
    assert restarted.result.interrupted_job_ids == ()
    assert [name for name, _ in restarted.control.operation_log] == [
        "submit",
        "interrupt",
    ]
    assert len(restarted.control.submit_calls) == 1
    replayed_outcome, replayed_ref = restarted.control.submit_calls[0]
    assert canonical_json_bytes(replayed_outcome) == canonical_json_bytes(
        graph.outcome
    )
    assert replayed_ref == graph.durable.outcome_file_ref
    assert len(graph.runtime.calls) == 1
    assert len(graph.runtime.receipts) == 1

    replayed = restarted.records.read_published_outcome(graph.source.job_id)
    assert replayed is not None
    assert replayed.outcome_file_ref == graph.durable.outcome_file_ref
    assert canonical_json_bytes(replayed.job_outcome) == canonical_json_bytes(
        graph.outcome
    )
    aggregate = restarted.repository.read_snapshot().cases[recovery_seam.CASE_ID]
    assert aggregate.jobs[graph.source.job_id].status is JobStatus.SUCCEEDED
    processing = aggregate.outcome_processing_records[graph.outcome.outcome_id]
    assert processing.disposition is OutcomeDisposition.APPLIED
    assert processing.created_job_id == graph.next_job_id
    assert processing.accepted_artifact_ids == sorted(
        graph.artifact_ids_by_key.values()
    )
    assert processing.accepted_evidence_ids == sorted(
        graph.evidence_ids_by_key.values()
    )
    assert aggregate.case.active_job_id == graph.next_job_id
    next_job = aggregate.jobs[graph.next_job_id]
    assert next_job.status is JobStatus.PENDING
    assert next_job.created_at == graph.outcome.produced_at
    assert (graph.data_root / "jobs" / graph.next_job_id / "job.json").read_bytes() == (
        graph.next_job_bytes
    )
    published_next = restarted.records.read_published_job(graph.next_job_id)
    assert published_next is not None
    assert canonical_json_bytes(published_next.job) == graph.next_job_bytes

    assert set(aggregate.artifacts) == set(graph.artifact_ids_by_key.values())
    for proposal_key, artifact_id in graph.artifact_ids_by_key.items():
        artifact = aggregate.artifacts[artifact_id]
        assert artifact.artifact_id == artifact_id
        assert artifact.created_at == graph.outcome.produced_at
        assert graph.data_root / artifact.storage_key == (
            graph.formal_paths_by_key[proposal_key]
        )
        assert _path_bytes(graph.formal_paths_by_key[proposal_key]) == (
            graph.formal_bytes_by_key[proposal_key]
        )

    assert set(aggregate.evidence) == {
        *graph.existing_evidence_bytes_by_id,
        *graph.evidence_ids_by_key.values(),
    }
    for evidence_id, expected_bytes in graph.existing_evidence_bytes_by_id.items():
        assert canonical_json_bytes(aggregate.evidence[evidence_id]) == expected_bytes
    accepted_evidence_id = graph.evidence_ids_by_key["parsed_timeout_evidence"]
    accepted_evidence = aggregate.evidence[accepted_evidence_id]
    assert accepted_evidence.evidence_id == accepted_evidence_id
    assert accepted_evidence.source_type is EvidenceSourceType.LOGPARSE
    assert accepted_evidence.source_ref == graph.artifact_ids_by_key["logparse_run"]
    assert accepted_evidence.collected_at == graph.outcome.produced_at

    candidate = aggregate.case.diagnosis_state.candidate_conclusion
    assert candidate is not None
    assert candidate.conclusion_id == graph.candidate_id
    assert candidate.proposed_by_job_id == graph.source.job_id
    assert candidate.status is CandidateStatus.REVIEWING
    assert next_job.review_target is not None
    assert next_job.review_target.candidate_conclusion_id == graph.candidate_id
    assert next_job.review_target.candidate_revision == candidate.revision
    assert next_job.review_target.candidate_content_hash == candidate.content_hash

    attempted = graph.attempted_mutation
    attempted_candidate = (
        attempted.upsert_case.diagnosis_state.candidate_conclusion
        if attempted.upsert_case is not None
        else None
    )
    assert attempted_candidate is not None
    assert canonical_json_bytes(attempted_candidate) == canonical_json_bytes(candidate)
    assert {
        artifact.artifact_id: canonical_json_bytes(artifact)
        for artifact in attempted.insert_artifacts
    } == {
        artifact_id: canonical_json_bytes(aggregate.artifacts[artifact_id])
        for artifact_id in processing.accepted_artifact_ids
    }
    assert {
        evidence.evidence_id: canonical_json_bytes(evidence)
        for evidence in attempted.insert_evidence
    } == {
        evidence_id: canonical_json_bytes(aggregate.evidence[evidence_id])
        for evidence_id in processing.accepted_evidence_ids
    }
    assert [canonical_json_bytes(job) for job in attempted.insert_jobs] == [
        graph.next_job_bytes
    ]

    user_results = [
        artifact
        for artifact in aggregate.artifacts.values()
        if artifact.kind is ArtifactKind.USER_RESULT
    ]
    assert len(user_results) == 1
    user_result_path = graph.data_root / user_results[0].storage_key
    assert user_result_path.read_bytes() == graph.result_bytes
    assert len(graph.result_bytes) == 622
    assert sha256(graph.result_bytes).hexdigest() == (
        "37ee245a8ae705561575e2c353fd1cc4e2a57653ed05d095f4d2292c287cdf09"
    )


def test_candidate_outbox_restart_reuses_resources_job_and_produced_at_without_runtime(
    tmp_path: Path,
) -> None:
    graph = _park_candidate_after_resource_and_job_publish(tmp_path)

    restarted = _restart_and_recover(graph)

    _assert_recovery_reuses_parked_bytes(graph, restarted)


def test_real_retention_cleaner_protects_candidate_outbox_then_recovery_replays(
    tmp_path: Path,
) -> None:
    graph = _park_candidate_after_resource_and_job_publish(tmp_path)
    protected_stage_paths = set(graph.stage_paths_by_key.values())
    protected_formal_paths = set(graph.formal_paths_by_key.values())
    protected_next_job_path = graph.data_root / "jobs" / graph.next_job_id

    for stage_path in protected_stage_paths:
        os.utime(stage_path / "staged.json", (EXPIRED_MTIME, EXPIRED_MTIME))
    for formal_path in protected_formal_paths:
        os.utime(formal_path.parent, (EXPIRED_MTIME, EXPIRED_MTIME))
    os.utime(
        protected_next_job_path,
        (EXPIRED_MTIME, EXPIRED_MTIME),
    )
    expired_orphan = graph.layout.state_temporary / "unreferenced-state.tmp"
    expired_orphan.write_bytes(b"obsolete state temporary\n")
    os.utime(expired_orphan, (EXPIRED_MTIME, EXPIRED_MTIME))

    cleaner_ids = DeterministicIdGenerator(seed=APP_ID_SEED)
    file_sync = PlatformFileSync()
    cleaner = StorageRetentionCleaner(
        graph.layout,
        graph.lock,
        graph.repository,
        graph.resources,
        graph.records,
        cleaner_ids,
        RetentionScanner(
            graph.layout,
            FakeClock(recovery_seam.FIXED_TIME),
        ),
        QuarantineMover(
            graph.layout,
            graph.lock,
            file_sync,
            PlatformReplaceOperation(),
        ),
        graph.resources.stage_registry,
        graph.registry,
    )
    protection = cleaner._protection_snapshot()
    assert protected_stage_paths <= set(protection.outbox_stage_paths)
    assert {
        path.relative_to(graph.data_root).as_posix()
        for path in protected_formal_paths
    } <= set(protection.outbox_resource_keys)
    assert graph.next_job_id in protection.outbox_job_ids

    cleanup = cleaner.run_once()

    assert cleanup.interrupted is False
    assert cleanup.failed_deletions == ()
    assert expired_orphan.exists() is False
    assert any(path.name == expired_orphan.name for path in cleanup.deleted)
    assert protected_stage_paths <= set(cleanup.skipped)
    assert protected_formal_paths <= set(cleanup.skipped)
    assert protected_next_job_path in cleanup.skipped
    for proposal_key, stage_path in graph.stage_paths_by_key.items():
        assert _path_bytes(stage_path) == graph.stage_bytes_by_key[proposal_key]
    for proposal_key, formal_path in graph.formal_paths_by_key.items():
        assert _path_bytes(formal_path) == graph.formal_bytes_by_key[proposal_key]
    assert (protected_next_job_path / "job.json").read_bytes() == (
        graph.next_job_bytes
    )

    restarted = _restart_and_recover(graph)

    _assert_recovery_reuses_parked_bytes(graph, restarted)


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
