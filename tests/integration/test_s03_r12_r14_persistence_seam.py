from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from problem_locator.application import build_application_service
from problem_locator.application.preparation import (
    fixed_asset_refs,
    make_user_fact,
    runtime_bindings_from_job,
)
from problem_locator.contracts import (
    ApplicationPortError,
    ArtifactKind,
    ArtifactProposal,
    AssetKind,
    Attachment,
    AttachmentStatus,
    CandidateStatus,
    CaseStatus,
    Evidence,
    EvidenceBinding,
    EvidenceProposal,
    EvidenceSourceBinding,
    EvidenceSourceType,
    ErrorCode,
    Job,
    JobOutcome,
    JobStatus,
    LogparseEvidenceLocator,
    LogparseParseParameters,
    LogparseRunMetadata,
    OutcomeDisposition,
    ResolvedAsset,
    ResourceKind,
    ResourceType,
    ReviewVerdict,
    StateFile,
    UserFactInput,
    UserResultPayload,
    canonical_json_bytes,
)
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
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
from tests.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeClock,
    InMemoryBinaryStream,
    InMemoryStateChangeNotifier,
    RecordingDispatcher,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/contracts/positive"
CASE_ID = "00000000-0000-0000-0000-000000000001"
EVIDENCE_ID = "00000000-0000-0000-0000-000000000040"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000050"
USER_FACT_ID = "00000000-0000-0000-0000-000000000030"
USER_FACT_TRIGGER_ID = "00000000-0000-0000-0000-000000000031"
DIAGNOSE_EPOCH = "00000000-0000-0000-0000-000000000090"
REVIEW_EPOCH = "00000000-0000-0000-0000-000000000091"
USER_RESULT_SHA256 = (
    "37ee245a8ae705561575e2c353fd1cc4e2a57653ed05d095f4d2292c287cdf09"
)


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _available_assets(*jobs: Job) -> list[ResolvedAsset]:
    unique = {
        (ref.id, ref.version, ref.content_hash): ref
        for job in jobs
        for ref in fixed_asset_refs(job)
    }
    return [
        ResolvedAsset(
            ref=ref,
            asset_kind=AssetKind.AGENT_PROFILE,
            root_path=f"/s08/assets/{ref.id}/{ref.version}",
        )
        for ref in unique.values()
    ]


def _seed_diagnosis_state(attachment: Attachment) -> tuple[StateFile, Job]:
    payload = _fixture("state.json")
    source_payload = _fixture("job-diagnose.json")
    source_payload.update(
        status=JobStatus.PENDING,
        started_at=None,
        runtime_epoch=None,
        attachment_refs=[attachment.attachment_id],
        artifact_refs=[],
        previous_outcome_refs=[],
    )
    source = Job.model_validate(source_payload)
    fact = make_user_fact(
        UserFactInput(name="request_id", value="payment-42"),
        item_id=USER_FACT_ID,
        trigger_id=USER_FACT_TRIGGER_ID,
        created_revision=2,
    )
    evidence = Evidence(
        evidence_id=EVIDENCE_ID,
        case_id=CASE_ID,
        source_type=EvidenceSourceType.USER_FACT,
        source_ref=USER_FACT_ID,
        locator={"kind": "USER_FACT", "input_name": "request_id"},
        summary="The request identifier observed in the diagnosis input.",
        collected_at="2026-07-31T00:00:30.000Z",
        content_hash=None,
        resource_ref=None,
    )
    aggregate = payload["cases"][CASE_ID]
    aggregate["case"].update(
        case_revision=2,
        active_job_id=source.job_id,
        selected_skill_ref=source.skill_ref,
        updated_at="2026-07-31T00:01:00.000Z",
    )
    aggregate["case"]["diagnosis_state"].update(
        revision=2,
        user_facts=[fact.model_dump(mode="python")],
        evidence_refs=[evidence.evidence_id],
    )
    aggregate["jobs"] = {source.job_id: source.model_dump(mode="python")}
    aggregate["attachments"] = {
        attachment.attachment_id: attachment.model_dump(mode="python")
    }
    aggregate["evidence"] = {
        evidence.evidence_id: evidence.model_dump(mode="python")
    }
    payload["generation"] = 2
    return StateFile.model_validate(payload), source


def _publish_attachment(
    resources: FileResourceStore,
    publication_guard: InProcessPublicationCommitGuard,
    upload_guard: InProcessAttachmentUploadGuard,
) -> Attachment:
    body = b"fixed payment-to-inventory RPC archive\n"
    digest = hashlib.sha256(body).hexdigest()
    with upload_guard.acquire(ATTACHMENT_ID) as upload_lease:
        staged = resources.stage_attachment(
            ATTACHMENT_ID,
            upload_lease,
            InMemoryBinaryStream(body),
            expected_size=len(body),
            expected_sha256=digest,
        )
        target = resources.plan_target(
            CASE_ID,
            ResourceType.ATTACHMENT,
            ATTACHMENT_ID,
            ResourceKind.FILE,
            staged.size,
            staged.sha256,
        )
        with publication_guard.acquire():
            published = resources.publish(staged, target.final_storage_key)
    return Attachment(
        attachment_id=ATTACHMENT_ID,
        case_id=CASE_ID,
        status=AttachmentStatus.READY,
        name="rpc-logs.tar.gz",
        content_type="application/gzip",
        declared_size=len(body),
        declared_sha256=digest,
        size=published.size,
        sha256=published.sha256,
        storage_key=published.storage_key,
        created_at="2026-07-31T00:00:30.000Z",
        updated_at="2026-07-31T00:00:30.000Z",
    )


def _candidate_outcome_with_real_resources(
    resources: FileResourceStore,
    source: Job,
    attachment: Attachment,
    tmp_path: Path,
) -> tuple[JobOutcome, bytes]:
    result_bytes = (FIXTURES / "user-result.json").read_bytes()
    assert len(result_bytes) == 622
    assert hashlib.sha256(result_bytes).hexdigest() == USER_RESULT_SHA256
    user_result = resources.stage_file(
        source.job_id,
        "user_result",
        InMemoryBinaryStream(result_bytes),
        expected_size=len(result_bytes),
        expected_sha256=USER_RESULT_SHA256,
    )

    logparse_tree = tmp_path / "logparse-result"
    events = logparse_tree / "events"
    events.mkdir(parents=True)
    (logparse_tree / "parse_manifest.json").write_bytes(
        b'{"schema_version":1}\n'
    )
    (events / "timeout.json").write_bytes(b'{"request_id":"payment-42"}\n')
    logparse_run = resources.stage_tree(
        source.job_id,
        "logparse_run",
        logparse_tree,
    )
    assert logparse_run.tree_manifest is not None
    assert source.logparse_tool_ref is not None
    assert source.logparse_product is not None
    logparse_proposal = ArtifactProposal(
        proposal_key=logparse_run.proposal_key,
        artifact_kind=ArtifactKind.LOGPARSE_RUN,
        name="rpc-logparse-run",
        content_type="application/vnd.problem-locator.logparse-run+directory",
        resource_kind=ResourceKind.DIRECTORY,
        size=logparse_run.size,
        sha256=logparse_run.sha256,
        staged_resource_ref=logparse_run,
        metadata=LogparseRunMetadata(
            tree_manifest_sha256=logparse_run.sha256,
            logparse_version_ref=source.logparse_tool_ref,
            parse_manifest_relative_path="parse_manifest.json",
            source_attachment_id=attachment.attachment_id,
            source_attachment_sha256=attachment.sha256,
            parse_parameters=LogparseParseParameters(product=source.logparse_product),
        ),
    )
    logparse_evidence = EvidenceProposal(
        proposal_key="parsed_timeout_evidence",
        source_type=EvidenceSourceType.LOGPARSE,
        source_binding=EvidenceSourceBinding(
            existing_source_ref=None,
            artifact_proposal_key=logparse_proposal.proposal_key,
        ),
        locator=LogparseEvidenceLocator(
            kind="LOGPARSE",
            relative_path="events/timeout.json",
            start_line=None,
            end_line=None,
            start_time=None,
            end_time=None,
        ),
        summary="The parsed run identifies the timed-out request.",
        content_hash=None,
        staged_resource_ref=None,
    )

    payload = _fixture("job-outcome-diagnosis.json")
    payload["proposed_artifacts"][0]["staged_resource_ref"] = (
        user_result.model_dump(mode="python")
    )
    payload["proposed_artifacts"].append(
        logparse_proposal.model_dump(mode="python")
    )
    payload["proposed_evidence"].append(
        logparse_evidence.model_dump(mode="python")
    )
    payload["payload"]["state_delta"]["add_evidence_bindings"].append(
        EvidenceBinding(
            existing_evidence_id=None,
            evidence_proposal_key=logparse_evidence.proposal_key,
        ).model_dump(mode="python")
    )
    return JobOutcome.model_validate(payload), result_bytes


def _review_pass_outcome(review_job: Job) -> JobOutcome:
    assert review_job.review_target is not None
    payload = _fixture("job-outcome-review.json")
    payload.update(
        job_id=review_job.job_id,
        base_state_revision=review_job.base_state_revision,
        produced_at="2026-07-31T00:06:30.000Z",
        consumed_evidence_refs=list(review_job.evidence_refs),
    )
    payload["payload"].update(
        candidate_conclusion_id=review_job.review_target.candidate_conclusion_id,
        candidate_revision=review_job.review_target.candidate_revision,
        candidate_content_hash=review_job.review_target.candidate_content_hash,
        reviewed_state_revision=review_job.base_state_revision,
        reviewed_evidence_refs=list(review_job.evidence_refs),
    )
    return JobOutcome.model_validate(payload)


def test_r12_r14_candidate_review_download_and_restart_are_one_durable_path(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    layout = StorageLayout.at(data_root)
    layout.ensure_directories()
    coordination_lock = StorageCoordinationLock()
    attachment_registry = AttachmentUploadRegistry()
    publication_guard = InProcessPublicationCommitGuard(coordination_lock)
    upload_guard = InProcessAttachmentUploadGuard(attachment_registry)
    storage_ids = DeterministicIdGenerator(seed="s08-r12-r14-storage")
    resources = FileResourceStore(
        layout,
        coordination_lock,
        attachment_registry,
        storage_ids,
    )
    attachment = _publish_attachment(resources, publication_guard, upload_guard)
    state, source = _seed_diagnosis_state(attachment)
    records = FileExecutionRecordStore(data_root, coordination_lock)
    with publication_guard.acquire():
        records.publish_job(source)
    layout.state.write_bytes(canonical_json_bytes(state))
    clock = FakeClock("2026-07-31T00:01:00.000Z")
    repository = JsonFileStateRepository(
        data_root,
        coordination_lock,
        clock,
        storage_ids,
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
    dispatcher = RecordingDispatcher()
    service = build_application_service(
        repository=repository,
        resource_store=resources,
        publication_guard=publication_guard,
        upload_guard=upload_guard,
        execution_records=records,
        coordinator=DomainCoordinator(),
        projector=PureContextSnapshotProjector(),
        asset_catalog=catalog,
        dispatcher=dispatcher,
        notifier=InMemoryStateChangeNotifier(),
        clock=clock,
        ids=DeterministicIdGenerator(seed="s08-r12-r14-application"),
    )

    claim = service.claim_job(source.job_id, DIAGNOSE_EPOCH)
    assert claim.claimed is True
    assert claim.job is not None and claim.job.status is JobStatus.RUNNING
    outcome, expected_result_bytes = _candidate_outcome_with_real_resources(
        resources,
        claim.job,
        attachment,
        tmp_path,
    )
    outcome_ref = records.publish_outcome_bytes(
        source.job_id,
        canonical_json_bytes(outcome),
    )
    clock.set("2026-07-31T00:05:00.000Z")

    candidate_receipt = service.submit_outcome(outcome, outcome_ref)

    assert candidate_receipt.disposition is OutcomeDisposition.APPLIED
    candidate_state = repository.read_snapshot().cases[CASE_ID]
    candidate = candidate_state.case.diagnosis_state.candidate_conclusion
    assert candidate is not None
    assert candidate.status is CandidateStatus.REVIEWING
    assert candidate.proposed_by_job_id == source.job_id
    user_results = [
        artifact
        for artifact in candidate_state.artifacts.values()
        if artifact.kind is ArtifactKind.USER_RESULT
    ]
    assert len(user_results) == 1
    user_result = user_results[0]
    assert user_result.created_by_job_id == candidate.proposed_by_job_id
    assert user_result.size == 622
    assert user_result.sha256 == USER_RESULT_SHA256
    assert user_result.metadata.schema_version == 1
    assert user_result.metadata.format_id == "problem-locator-diagnosis-v1"
    processing = candidate_state.outcome_processing_records[outcome.outcome_id]
    assert processing.accepted_artifact_ids.count(user_result.artifact_id) == 1
    logparse_runs = [
        artifact
        for artifact in candidate_state.artifacts.values()
        if artifact.kind is ArtifactKind.LOGPARSE_RUN
    ]
    assert len(logparse_runs) == 1
    assert service.list_artifacts(CASE_ID).artifacts == []
    internal = service.list_artifacts(CASE_ID, include_internal=True).artifacts
    assert {item.artifact_id for item in internal} == {
        user_result.artifact_id,
        logparse_runs[0].artifact_id,
    }
    with pytest.raises(ApplicationPortError) as hidden:
        service.open_artifact(CASE_ID, logparse_runs[0].artifact_id)
    assert hidden.value.error.code is ErrorCode.ARTIFACT_NOT_FOUND

    review_job_id = processing.created_job_id
    assert review_job_id is not None
    review_job = candidate_state.jobs[review_job_id]
    assert review_job.review_target is not None
    assert review_job.review_target.candidate_conclusion_id == candidate.conclusion_id
    clock.set("2026-07-31T00:06:00.000Z")
    review_claim = service.claim_job(review_job.job_id, REVIEW_EPOCH)
    assert review_claim.claimed is True
    assert review_claim.job is not None
    review_outcome = _review_pass_outcome(review_claim.job)
    assert review_outcome.payload.verdict is ReviewVerdict.PASS
    assert review_outcome.payload.unsupported_findings == []
    assert review_outcome.payload.missing_evidence == []
    assert review_outcome.payload.evidence_conflicts == []
    assert review_outcome.payload.stale_references == []
    review_outcome_ref = records.publish_outcome_bytes(
        review_job.job_id,
        canonical_json_bytes(review_outcome),
    )
    clock.set("2026-07-31T00:07:00.000Z")

    review_receipt = service.submit_outcome(review_outcome, review_outcome_ref)

    assert review_receipt.disposition is OutcomeDisposition.APPLIED
    resolved = service.get_case(CASE_ID).case_view
    assert resolved.status is CaseStatus.RESOLVED
    assert resolved.final_result is not None
    assert resolved.final_result.status is CandidateStatus.ACCEPTED
    assert resolved.final_result.conclusion_id == candidate.conclusion_id
    assert [
        item.artifact_id for item in service.list_artifacts(CASE_ID).artifacts
    ] == [user_result.artifact_id]

    restart_lock = StorageCoordinationLock()
    restarted_records = FileExecutionRecordStore(data_root, restart_lock)
    restarted_repository = JsonFileStateRepository(
        data_root,
        restart_lock,
        clock,
        DeterministicIdGenerator(seed="s08-r12-r14-restart"),
        execution_record_store=restarted_records,
    )
    restart_registry = AttachmentUploadRegistry()
    restarted_resources = FileResourceStore(
        StorageLayout.at(data_root),
        restart_lock,
        restart_registry,
        DeterministicIdGenerator(seed="s08-r12-r14-restart-resources"),
    )
    restarted_service = build_application_service(
        repository=restarted_repository,
        resource_store=restarted_resources,
        publication_guard=InProcessPublicationCommitGuard(restart_lock),
        upload_guard=InProcessAttachmentUploadGuard(restart_registry),
        execution_records=restarted_records,
        coordinator=DomainCoordinator(),
        projector=PureContextSnapshotProjector(),
        asset_catalog=catalog,
        dispatcher=RecordingDispatcher(),
        notifier=InMemoryStateChangeNotifier(),
        clock=clock,
        ids=DeterministicIdGenerator(seed="s08-r12-r14-restarted-application"),
    )

    restarted_view = restarted_service.get_case(CASE_ID).case_view
    assert restarted_view == resolved
    restarted_snapshot = restarted_repository.read_snapshot().cases[CASE_ID]
    assert restarted_snapshot.case.status is CaseStatus.RESOLVED
    assert restarted_snapshot.case.final_result is not None
    assert restarted_snapshot.case.final_result.status is CandidateStatus.ACCEPTED
    opened = restarted_service.open_artifact(CASE_ID, user_result.artifact_id)
    try:
        downloaded = opened.stream.read(623)
        assert opened.stream.read(1) == b""
    finally:
        opened.stream.close()
    assert downloaded == expected_result_bytes
    assert len(downloaded) == 622
    assert hashlib.sha256(downloaded).hexdigest() == USER_RESULT_SHA256
    payload = UserResultPayload.model_validate_json(downloaded)
    assert payload.problem_statement == restarted_view.problem_spec.statement
    assert payload.candidate_statement == restarted_view.final_result.statement
    assert [
        binding.existing_evidence_id
        for binding in payload.supporting_evidence_bindings
    ] == restarted_view.final_result.supporting_evidence_refs
    assert all(
        binding.evidence_proposal_key is None
        for binding in payload.supporting_evidence_bindings
    )
    assert [
        [binding.existing_evidence_id for binding in item.evidence_bindings]
        for item in payload.completion_criteria_mapping
    ] == [
        item.evidence_refs
        for item in restarted_view.final_result.completion_criteria_mapping
    ]
