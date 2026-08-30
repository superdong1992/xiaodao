from __future__ import annotations

import hashlib
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
    Attachment,
    AttachmentEvidenceLocator,
    AttachmentStatus,
    CreateCase,
    Evidence,
    EvidenceSourceType,
    Job,
    ResourceKind,
    ResourceType,
    canonical_json_bytes,
)
from problem_locator.contracts.limits import (
    ORPHAN_RESOURCE_RETENTION_SECONDS,
    PROPOSAL_STAGING_RETENTION_SECONDS,
)
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.storage.atomic import is_reparse_point
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
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeClock,
    InMemoryBinaryStream,
    InMemoryStateChangeNotifier,
    RecordingDispatcher,
)


ROOT = Path(__file__).resolve().parents[3]
ROUTE_JOB_FIXTURE = ROOT / "tests/fixtures/contracts/positive/job-route.json"
CASE_ID = "00000000-0000-0000-0000-000000000001"
TRIGGER_ID = "00000000-0000-0000-0000-000000000896"
ROUTE_JOB_ID = "00000000-0000-0000-0000-000000000897"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000892"
FORMAL_EVIDENCE_ID = "00000000-0000-0000-0000-000000000893"
RACE_EVIDENCE_ID = "00000000-0000-0000-0000-000000000894"
ORPHAN_JOB_ID = "00000000-0000-0000-0000-000000000895"
CLEANUP_NOW = "2026-08-10T12:00:00.000Z"


def _create_case_command() -> CreateCase:
    return CreateCase(
        idempotency_key="retention-reachable-route",
        raw_problem_text="Payment to inventory RPC times out",
        problem_spec={
            "statement": "Payment to inventory RPC times out",
            "expected_behavior": "The inventory RPC returns successfully",
            "actual_behavior": "The payment service observes a timeout",
            "scope": "payment-service to inventory-service",
            "goals": ["Locate the evidence-backed cause"],
            "non_goals": [],
            "constraints": [],
            "completion_criteria": ["A terminal diagnosis is available"],
        },
        initial_user_facts=[],
        wait_seconds=0,
    )


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
    """Pause one candidate before the real mover acquires the shared lock."""

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
    assert not path.is_symlink()
    os.utime(path, ns=timestamps)


def _publish_file(
    resources: FileResourceStore,
    guard: InProcessPublicationCommitGuard,
    *,
    owner_job_id: str,
    proposal_key: str,
    resource_type: ResourceType,
    resource_id: str,
    body: bytes,
    upload_guard: InProcessAttachmentUploadGuard | None = None,
):
    if resource_type is ResourceType.ATTACHMENT:
        assert upload_guard is not None
        with upload_guard.acquire(resource_id) as upload_lease:
            staged = resources.stage_attachment(
                resource_id,
                upload_lease,
                InMemoryBinaryStream(body),
            )
            target = resources.plan_target(
                CASE_ID,
                resource_type,
                resource_id,
                ResourceKind.FILE,
                staged.size,
                staged.sha256,
            )
            with guard.acquire():
                published = resources.publish(staged, target.final_storage_key)
        return staged, published
    staged = resources.stage_file(owner_job_id, proposal_key, InMemoryBinaryStream(body))
    target = resources.plan_target(
        CASE_ID,
        resource_type,
        resource_id,
        ResourceKind.FILE,
        staged.size,
        staged.sha256,
    )
    with guard.acquire():
        published = resources.publish(staged, target.final_storage_key)
    return staged, published


def test_reachable_route_state_retention_cleanup_and_commit_race(
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
    resources = FileResourceStore(
        layout,
        lock,
        attachment_registry,
        DeterministicIdGenerator(seed="retention-route-resources"),
        file_sync=file_sync,
        replacer=replacer,
    )
    records = FileExecutionRecordStore(data_root, lock, file_sync, replacer)
    clock = FakeClock("2026-07-31T00:01:00.000Z")
    repository = JsonFileStateRepository(
        data_root,
        lock,
        clock,
        DeterministicIdGenerator(seed="retention-route-state"),
        file_sync=file_sync,
        replacer=replacer,
        execution_record_store=records,
    )
    route_template = Job.model_validate_json(
        ROUTE_JOB_FIXTURE.read_text(encoding="utf-8")
    )
    service = build_application_service(
        repository=repository,
        resource_store=resources,
        publication_guard=publication_guard,
        upload_guard=upload_guard,
        execution_records=records,
        coordinator=DomainCoordinator(),
        projector=PureContextSnapshotProjector(),
        asset_catalog=FakeAssetCatalog(
            route=runtime_bindings_from_job(route_template)
        ),
        dispatcher=RecordingDispatcher(),
        notifier=InMemoryStateChangeNotifier(),
        clock=clock,
        ids=DeterministicIdGenerator(
            scripted_ids={
                "case": [CASE_ID],
                "trigger": [TRIGGER_ID],
                "job": [ROUTE_JOB_ID],
            }
        ),
    )
    created = service.execute(_create_case_command())
    assert created.business_receipt.case_id == CASE_ID
    assert created.business_receipt.job_id == ROUTE_JOB_ID
    aggregate = repository.read_snapshot().cases[CASE_ID]
    route_job_id = aggregate.case.active_job_id
    assert route_job_id == ROUTE_JOB_ID
    route_job = aggregate.jobs[route_job_id]

    attachment_body = b"reachable route attachment\n"
    _, attachment_ref = _publish_file(
        resources,
        publication_guard,
        owner_job_id=route_job_id,
        proposal_key="formal_attachment",
        resource_type=ResourceType.ATTACHMENT,
        resource_id=ATTACHMENT_ID,
        body=attachment_body,
        upload_guard=upload_guard,
    )
    attachment = Attachment(
        attachment_id=ATTACHMENT_ID,
        case_id=CASE_ID,
        status=AttachmentStatus.READY,
        name="route-input.zip",
        content_type="application/zip",
        declared_size=len(attachment_body),
        declared_sha256=hashlib.sha256(attachment_body).hexdigest(),
        size=attachment_ref.size,
        sha256=attachment_ref.sha256,
        storage_key=attachment_ref.storage_key,
        created_at="2026-07-31T00:02:00.000Z",
        updated_at="2026-07-31T00:02:00.000Z",
    )
    formal_body = b"formal evidence retained by state\n"
    _, formal_ref = _publish_file(
        resources,
        publication_guard,
        owner_job_id=route_job_id,
        proposal_key="formal_evidence",
        resource_type=ResourceType.EVIDENCE,
        resource_id=FORMAL_EVIDENCE_ID,
        body=formal_body,
    )
    formal_evidence = Evidence(
        evidence_id=FORMAL_EVIDENCE_ID,
        case_id=CASE_ID,
        source_type=EvidenceSourceType.ATTACHMENT,
        source_ref=ATTACHMENT_ID,
        locator=AttachmentEvidenceLocator(
            kind="ATTACHMENT",
            byte_start=0,
            byte_end_exclusive=1,
        ),
        summary="A reachable route attachment has retained evidence bytes.",
        collected_at="2026-07-31T00:02:00.000Z",
        content_hash=formal_ref.sha256,
        resource_ref=formal_ref,
    )
    snapshot = repository.read_snapshot()
    repository.commit(
        snapshot.generation,
        None,
        build_state_mutation(
            upsert_attachments=[attachment],
            insert_evidence=[formal_evidence],
        ),
    )

    orphan_stage = resources.stage_file(
        ORPHAN_JOB_ID,
        "expired_unreferenced_stage",
        InMemoryBinaryStream(b"obsolete staged bytes\n"),
    )
    orphan_stage_path = proposal_stage_path(
        data_root,
        orphan_stage.owner_job_id,
        orphan_stage.proposal_key,
    )
    _set_expired(
        orphan_stage_path / "staged.json",
        PROPOSAL_STAGING_RETENTION_SECONDS,
    )
    formal_paths = {
        data_root / attachment_ref.storage_key,
        data_root / formal_ref.storage_key,
    }
    for path in formal_paths:
        _set_expired(path.parent, ORPHAN_RESOURCE_RETENTION_SECONDS)
    route_job_path = layout.jobs / route_job_id
    _set_expired(route_job_path, ORPHAN_RESOURCE_RETENTION_SECONDS)

    before_cleanup = repository.read_snapshot()
    cleaner = StorageRetentionCleaner(
        layout,
        lock,
        repository,
        resources,
        records,
        DeterministicIdGenerator(seed="retention-route-cleanup"),
        RetentionScanner(layout, FakeClock(CLEANUP_NOW)),
        QuarantineMover(layout, lock, file_sync, replacer),
        resources.stage_registry,
        attachment_registry,
    )
    cleanup = cleaner.run_once()

    assert cleanup.interrupted is False
    assert cleanup.failed_deletions == ()
    assert not orphan_stage_path.exists()
    assert formal_paths <= set(cleanup.skipped)
    assert route_job_path in cleanup.skipped
    assert repository.read_snapshot() == before_cleanup
    assert all(path.exists() for path in formal_paths)
    assert (route_job_path / "job.json").read_bytes() == canonical_json_bytes(route_job)

    race_body = b"resource becomes referenced while cleanup is waiting\n"
    _, race_ref = _publish_file(
        resources,
        publication_guard,
        owner_job_id=route_job_id,
        proposal_key="cleanup_commit_race_evidence",
        resource_type=ResourceType.EVIDENCE,
        resource_id=RACE_EVIDENCE_ID,
        body=race_body,
    )
    race_path = data_root / race_ref.storage_key
    _set_expired(race_path.parent, ORPHAN_RESOURCE_RETENTION_SECONDS)
    race_evidence = Evidence(
        evidence_id=RACE_EVIDENCE_ID,
        case_id=CASE_ID,
        source_type=EvidenceSourceType.ATTACHMENT,
        source_ref=ATTACHMENT_ID,
        locator=AttachmentEvidenceLocator(
            kind="ATTACHMENT",
            byte_start=0,
            byte_end_exclusive=1,
        ),
        summary="The winning state commit retains this evidence resource.",
        collected_at="2026-07-31T00:03:00.000Z",
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
        DeterministicIdGenerator(seed="retention-route-race-cleanup"),
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
