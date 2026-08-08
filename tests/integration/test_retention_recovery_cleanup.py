from __future__ import annotations

import os
import stat
import threading
from datetime import datetime
from pathlib import Path

import pytest

from problem_locator.application import build_application_service
from problem_locator.application.mutations import build_state_mutation
from problem_locator.application.preparation import runtime_bindings_from_job
from problem_locator.contracts import (
    ArtifactKind,
    AttachmentEvidenceLocator,
    CandidateStatus,
    Evidence,
    EvidenceSourceType,
    Job,
    JobStatus,
    OutcomeDisposition,
    ResourceKind,
    ResourceType,
    canonical_json_bytes,
)
from problem_locator.contracts.limits import (
    ORPHAN_RESOURCE_RETENTION_SECONDS,
    PROPOSAL_STAGING_RETENTION_SECONDS,
)
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.storage.coordination import (
    AttachmentUploadRegistry,
    InProcessAttachmentUploadGuard,
    InProcessPublicationCommitGuard,
    StorageCoordinationLock,
)
from problem_locator.storage.atomic import is_reparse_point
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
    InMemoryStateChangeNotifier,
    RecordingDispatcher,
)
from tests.integration.test_s03_r12_r14_persistence_seam import (
    CASE_ID,
    _available_assets,
    _candidate_outcome_with_real_resources,
    _fixture,
    _publish_attachment,
    _seed_diagnosis_state,
)


APP_ID_SEED = "s08-retention-applied-application"
CLEANUP_NOW = "2026-08-10T12:00:00.000Z"
RACE_EVIDENCE_ID = "00000000-0000-0000-0000-000000000891"


@pytest.fixture(autouse=True)
def _adapt_no_follow_chmod_for_plain_windows_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "nt" or os.chmod in os.supports_follow_symlinks:
        return

    real_chmod = os.chmod

    def chmod_fixture_node(
        path: os.PathLike[str] | str,
        mode: int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if follow_symlinks is False:
            assert dir_fd is None
            metadata = Path(path).lstat()
            assert not stat.S_ISLNK(metadata.st_mode)
            assert not is_reparse_point(metadata)
            real_chmod(path, mode)
            return
        assert dir_fd is None
        real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", chmod_fixture_node)


class _MoveBarrier:
    """Pause one candidate immediately before the real mover takes the lock."""

    def __init__(self, delegate: QuarantineMover, target: Path) -> None:
        self.delegate = delegate
        self.target = target
        self.entered = threading.Event()
        self.release = threading.Event()

    def move_if(self, cleanup_id, candidate, revalidate):
        if candidate == self.target:
            self.entered.set()
            if not self.release.wait(3):
                raise TimeoutError("commit did not release the cleanup race barrier")
        return self.delegate.move_if(cleanup_id, candidate, revalidate)

    def discover(self):
        return self.delegate.discover()

    def delete(self, path: Path) -> None:
        self.delegate.delete(path)


def _set_expired(path: Path, retention_seconds: int) -> None:
    now_seconds = datetime.fromisoformat(
        CLEANUP_NOW.replace("Z", "+00:00")
    ).timestamp()
    timestamp_ns = int((now_seconds - retention_seconds - 1) * 1_000_000_000)
    timestamps = (timestamp_ns, timestamp_ns)
    if os.utime in os.supports_follow_symlinks:
        os.utime(path, ns=timestamps, follow_symlinks=False)
        return
    # Windows does not expose no-follow utime. These fixtures age plain nodes,
    # so prove that precondition before using the supported call shape.
    assert not path.is_symlink()
    os.utime(path, ns=timestamps)


def _path_bytes(path: Path) -> dict[str, bytes]:
    if path.is_file():
        return {".": path.read_bytes()}
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def test_applied_candidate_stages_expire_without_deleting_formal_state(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    layout = StorageLayout.at(data_root)
    layout.initialize_v2_data_root()
    lock = StorageCoordinationLock()
    publication_guard = InProcessPublicationCommitGuard(lock)
    attachment_registry = AttachmentUploadRegistry()
    upload_guard = InProcessAttachmentUploadGuard(attachment_registry)
    file_sync = PlatformFileSync()
    replacer = PlatformReplaceOperation()
    resource_ids = DeterministicIdGenerator(seed="s08-retention-resources")
    resources = FileResourceStore(
        layout,
        lock,
        attachment_registry,
        resource_ids,
        file_sync=file_sync,
        replacer=replacer,
    )
    attachment = _publish_attachment(resources, publication_guard, upload_guard)
    state, source = _seed_diagnosis_state(attachment)
    records = FileExecutionRecordStore(
        data_root,
        lock,
        file_sync,
        replacer,
        temp_token_factory=lambda: "retention-record-temp",
    )
    with publication_guard.acquire():
        records.publish_job(source)
    layout.state.write_bytes(canonical_json_bytes(state))
    clock = FakeClock("2026-07-31T00:01:00.000Z")
    repository = JsonFileStateRepository(
        data_root,
        lock,
        clock,
        DeterministicIdGenerator(seed="s08-retention-state"),
        file_sync=file_sync,
        replacer=replacer,
        execution_record_store=records,
    )
    review_template = Job.model_validate(_fixture("job-review.json"))
    assert source.skill_ref is not None
    catalog = FakeAssetCatalog(
        assets=_available_assets(source, review_template),
        review={
            (
                source.skill_ref.id,
                source.skill_ref.version,
                source.skill_ref.content_hash,
            ): runtime_bindings_from_job(review_template)
        },
    )
    application = build_application_service(
        repository=repository,
        resource_store=resources,
        publication_guard=publication_guard,
        upload_guard=upload_guard,
        execution_records=records,
        coordinator=DomainCoordinator(),
        projector=PureContextSnapshotProjector(),
        asset_catalog=catalog,
        dispatcher=RecordingDispatcher(),
        notifier=InMemoryStateChangeNotifier(),
        clock=clock,
        ids=DeterministicIdGenerator(seed=APP_ID_SEED),
    )

    claim = application.claim_job(
        source.job_id,
        "00000000-0000-0000-0000-000000000890",
    )
    assert claim.claimed is True and claim.job is not None
    outcome, result_bytes = _candidate_outcome_with_real_resources(
        resources,
        claim.job,
        attachment,
        tmp_path,
    )

    # Prepublish each deterministic target from a different real stage.  S03
    # must then formally adopt these targets while leaving the Outcome's
    # original marker and payload/tree available for retention cleanup.
    application_ids = DeterministicIdGenerator(seed=APP_ID_SEED)
    installation_id = repository.read_snapshot().installation_id
    for proposal in outcome.proposed_artifacts:
        artifact_id = application_ids.derive(
            "artifact",
            [
                installation_id,
                outcome.case_id,
                outcome.outcome_id,
                proposal.proposal_key,
            ],
        )
        target = resources.plan_target(
            outcome.case_id,
            ResourceType.ARTIFACT,
            artifact_id,
            proposal.resource_kind,
            proposal.size,
            proposal.sha256,
        )
        if proposal.artifact_kind in {
            ArtifactKind.USER_RESULT,
            ArtifactKind.USER_RESULT_ARCHIVE,
        }:
            result_payload = result_bytes[proposal.artifact_kind]
            prepublished_stage = resources.stage_file(
                source.job_id,
                f"prepublish_{proposal.proposal_key}",
                InMemoryBinaryStream(result_payload),
                expected_size=proposal.size,
                expected_sha256=proposal.sha256,
            )
        else:
            assert proposal.artifact_kind is ArtifactKind.LOGPARSE_RUN
            prepublished_stage = resources.stage_tree(
                source.job_id,
                f"prepublish_{proposal.proposal_key}",
                tmp_path / "logparse-result",
                expected_manifest_hash=proposal.sha256,
            )
        assert prepublished_stage.resource_kind is proposal.resource_kind
        assert prepublished_stage.size == proposal.size
        assert prepublished_stage.sha256 == proposal.sha256
        with publication_guard.acquire():
            published = resources.publish(
                prepublished_stage,
                target.final_storage_key,
            )
        assert published.storage_key == target.final_storage_key

    outcome_ref = records.publish_outcome_bytes(
        source.job_id,
        canonical_json_bytes(outcome),
    )
    clock.set("2026-07-31T00:05:00.000Z")
    receipt = application.submit_outcome(outcome, outcome_ref)
    assert receipt.disposition is OutcomeDisposition.APPLIED
    durable_outcome = records.read_published_outcome(source.job_id)
    assert durable_outcome is not None
    assert durable_outcome.outcome_file_ref == outcome_ref
    assert durable_outcome.job_outcome == outcome

    before = repository.read_snapshot()
    aggregate = before.cases[CASE_ID]
    processing = aggregate.outcome_processing_records[outcome.outcome_id]
    assert processing.disposition is OutcomeDisposition.APPLIED
    assert aggregate.jobs[source.job_id].status is JobStatus.SUCCEEDED
    next_job_id = processing.created_job_id
    assert next_job_id is not None
    assert aggregate.jobs[next_job_id].status is JobStatus.PENDING
    candidate = aggregate.case.diagnosis_state.candidate_conclusion
    assert candidate is not None and candidate.status is CandidateStatus.REVIEWING
    assert set(processing.accepted_artifact_ids) == set(aggregate.artifacts)
    assert set(processing.accepted_evidence_ids) <= set(aggregate.evidence)
    accepted_evidence = aggregate.evidence[processing.accepted_evidence_ids[0]]
    assert accepted_evidence.source_type is EvidenceSourceType.LOGPARSE

    original_stage_paths = {
        proposal.proposal_key: proposal_stage_path(
            data_root,
            proposal.staged_resource_ref.owner_job_id,
            proposal.staged_resource_ref.proposal_key,
        )
        for proposal in outcome.proposed_artifacts
    }
    for proposal in outcome.proposed_artifacts:
        stage_path = original_stage_paths[proposal.proposal_key]
        content_name = (
            "tree" if proposal.resource_kind is ResourceKind.DIRECTORY else "payload"
        )
        assert (stage_path / "staged.json").is_file()
        assert (stage_path / content_name).exists()

    formal_paths = {
        artifact_id: data_root / artifact.storage_key
        for artifact_id, artifact in aggregate.artifacts.items()
    }
    formal_bytes = {
        artifact_id: _path_bytes(path)
        for artifact_id, path in formal_paths.items()
    }
    next_job_path = layout.jobs / next_job_id
    next_job_bytes = (next_job_path / "job.json").read_bytes()
    published_next_job = records.read_published_job(next_job_id)
    assert published_next_job is not None
    assert next_job_bytes == canonical_json_bytes(
        published_next_job.job
    )

    all_stage_paths = {
        path
        for owner in layout.proposals.iterdir()
        for path in owner.iterdir()
    }
    assert set(original_stage_paths.values()) <= all_stage_paths
    for stage_path in all_stage_paths:
        _set_expired(
            stage_path / "staged.json",
            PROPOSAL_STAGING_RETENTION_SECONDS,
        )
    for path in [*formal_paths.values(), data_root / attachment.storage_key]:
        _set_expired(path.parent, ORPHAN_RESOURCE_RETENTION_SECONDS)
    for job_id in (source.job_id, next_job_id):
        _set_expired(layout.jobs / job_id, ORPHAN_RESOURCE_RETENTION_SECONDS)

    delete_failures: list[tuple[Path, BaseException]] = []
    cleaner = StorageRetentionCleaner(
        layout,
        lock,
        repository,
        resources,
        records,
        DeterministicIdGenerator(seed="s08-retention-cleanup"),
        RetentionScanner(layout, FakeClock(CLEANUP_NOW)),
        QuarantineMover(layout, lock, file_sync, replacer),
        resources.stage_registry,
        attachment_registry,
        on_delete_failure=lambda path, error: delete_failures.append((path, error)),
    )
    result = cleaner.run_once()

    assert result.interrupted is False
    assert result.failed_deletions == (), delete_failures
    assert len(result.quarantined) >= len(all_stage_paths)
    assert len(result.deleted) >= len(all_stage_paths)
    assert all(not path.exists() for path in all_stage_paths)
    assert set(formal_paths.values()) <= set(result.skipped)
    assert next_job_path in result.skipped
    assert layout.jobs / source.job_id in result.skipped
    after = repository.read_snapshot()
    assert after == before
    after_aggregate = after.cases[CASE_ID]
    assert after_aggregate.case.diagnosis_state.candidate_conclusion == candidate
    assert after_aggregate.evidence == aggregate.evidence
    assert after_aggregate.artifacts == aggregate.artifacts
    assert after_aggregate.jobs[next_job_id] == aggregate.jobs[next_job_id]
    for artifact_id, path in formal_paths.items():
        assert path.exists()
        assert _path_bytes(path) == formal_bytes[artifact_id]
    assert next_job_path.is_dir()
    assert (next_job_path / "job.json").read_bytes() == next_job_bytes

    # Now pause a second real cleanup pass after discovery but immediately
    # before QuarantineMover takes the shared lock.  A real repository commit
    # that wins that lock must make the previously orphaned resource ineligible
    # when the mover revalidates it.
    race_bytes = b"resource becomes referenced while cleanup is waiting\n"
    race_stage = resources.stage_file(
        next_job_id,
        "cleanup_commit_race_evidence",
        InMemoryBinaryStream(race_bytes),
    )
    race_target = resources.plan_target(
        CASE_ID,
        ResourceType.EVIDENCE,
        RACE_EVIDENCE_ID,
        race_stage.resource_kind,
        race_stage.size,
        race_stage.sha256,
    )
    with publication_guard.acquire():
        race_ref = resources.publish(race_stage, race_target.final_storage_key)
    race_path = data_root / race_ref.storage_key
    _set_expired(race_path.parent, ORPHAN_RESOURCE_RETENTION_SECONDS)
    race_evidence = Evidence(
        evidence_id=RACE_EVIDENCE_ID,
        case_id=CASE_ID,
        source_type=EvidenceSourceType.ATTACHMENT,
        source_ref=attachment.attachment_id,
        locator=AttachmentEvidenceLocator(
            kind="ATTACHMENT",
            byte_start=0,
            byte_end_exclusive=1,
        ),
        summary="The attachment materialization is retained by the winning commit.",
        collected_at="2026-07-31T00:06:00.000Z",
        content_hash=race_ref.sha256,
        resource_ref=race_ref,
    )
    mover_barrier = _MoveBarrier(
        QuarantineMover(layout, lock, file_sync, replacer),
        race_path,
    )
    racing_cleaner = StorageRetentionCleaner(
        layout,
        lock,
        repository,
        resources,
        records,
        DeterministicIdGenerator(seed="s08-retention-race-cleanup"),
        RetentionScanner(layout, FakeClock(CLEANUP_NOW)),
        mover_barrier,
        resources.stage_registry,
        attachment_registry,
    )
    race_results = []
    race_failures: list[BaseException] = []

    def clean_during_commit() -> None:
        try:
            race_results.append(racing_cleaner.run_once())
        except BaseException as error:  # pragma: no cover - asserted below
            race_failures.append(error)

    cleaner_thread = threading.Thread(target=clean_during_commit, daemon=True)
    cleaner_thread.start()
    assert mover_barrier.entered.wait(2)
    precommit = repository.read_snapshot()
    try:
        repository.commit(
            precommit.generation,
            None,
            build_state_mutation(insert_evidence=[race_evidence]),
        )
    finally:
        mover_barrier.release.set()
    cleaner_thread.join(3)

    assert not cleaner_thread.is_alive()
    assert race_failures == []
    assert len(race_results) == 1
    assert race_path in race_results[0].skipped
    assert race_path.exists()
    committed = repository.read_snapshot()
    assert committed.cases[CASE_ID].evidence[RACE_EVIDENCE_ID] == race_evidence
