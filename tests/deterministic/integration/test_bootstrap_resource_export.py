from __future__ import annotations

import hashlib
from pathlib import Path

from problem_locator.application.mutations import build_state_mutation
from problem_locator.bootstrap import StandaloneStateAdmin, build_service
from problem_locator.contracts import (
    Artifact,
    ArtifactKind,
    AttachmentEvidenceLocator,
    Evidence,
    EvidenceSourceType,
    LogparseParseParameters,
    LogparseRunMetadata,
    ResourceKind,
    ResourceType,
    StateExport,
    StateExportResource,
    UserResultMetadata,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from tests.deterministic.contracts.fakes import InMemoryBinaryStream
from tests.deterministic.integration.test_bootstrap_composition import _settings
from tests.deterministic.integration.test_s03_r12_r14_persistence_seam import (
    CASE_ID,
    FIXTURES,
    _publish_attachment,
    _seed_diagnosis_state,
)


RESOURCE_EVIDENCE_ID = "00000000-0000-0000-0000-000000000041"
LOGPARSE_ARTIFACT_ID = "00000000-0000-0000-0000-000000000060"
USER_RESULT_ARTIFACT_ID = "00000000-0000-0000-0000-000000000061"


def _publish_proposal(graph, *, staged, resource_type, resource_id):
    target = graph.resource_store.plan_target(
        CASE_ID,
        resource_type,
        resource_id,
        staged.resource_kind,
        staged.size,
        staged.sha256,
    )
    with graph.publication_guard.acquire():
        return graph.resource_store.publish(staged, target.final_storage_key)


def test_nonempty_state_export_is_complete_canonical_and_generation_consistent(
    tmp_path: Path,
) -> None:
    graph = build_service(_settings(tmp_path / "data"))
    try:
        attachment = _publish_attachment(
            graph.resource_store,
            graph.publication_guard,
            graph.upload_guard,
        )
        seed_state, source_job = _seed_diagnosis_state(attachment)
        seed_aggregate = seed_state.cases[CASE_ID]
        with graph.publication_guard.acquire():
            graph.execution_records.publish_job(source_job)

        user_result_bytes = (FIXTURES / "user-result.json").read_bytes()
        user_result_staged = graph.resource_store.stage_file(
            source_job.job_id,
            "state_export_user_result",
            InMemoryBinaryStream(user_result_bytes),
            expected_size=len(user_result_bytes),
            expected_sha256=hashlib.sha256(user_result_bytes).hexdigest(),
        )
        user_result_ref = _publish_proposal(
            graph,
            staged=user_result_staged,
            resource_type=ResourceType.ARTIFACT,
            resource_id=USER_RESULT_ARTIFACT_ID,
        )

        logparse_tree = tmp_path / "export-logparse-run"
        (logparse_tree / "events").mkdir(parents=True)
        (logparse_tree / "parse_manifest.json").write_bytes(
            b'{"schema_version":1}\n'
        )
        (logparse_tree / "events/timeout.json").write_bytes(
            b'{"request_id":"payment-42"}\n'
        )
        logparse_staged = graph.resource_store.stage_tree(
            source_job.job_id,
            "state_export_logparse_run",
            logparse_tree,
        )
        logparse_ref = _publish_proposal(
            graph,
            staged=logparse_staged,
            resource_type=ResourceType.ARTIFACT,
            resource_id=LOGPARSE_ARTIFACT_ID,
        )

        evidence_bytes = b"inventory request payment-42 exceeded its deadline\n"
        evidence_sha256 = hashlib.sha256(evidence_bytes).hexdigest()
        evidence_staged = graph.resource_store.stage_file(
            source_job.job_id,
            "state_export_evidence",
            InMemoryBinaryStream(evidence_bytes),
            expected_size=len(evidence_bytes),
            expected_sha256=evidence_sha256,
        )
        evidence_ref = _publish_proposal(
            graph,
            staged=evidence_staged,
            resource_type=ResourceType.EVIDENCE,
            resource_id=RESOURCE_EVIDENCE_ID,
        )

        assert source_job.logparse_tool_ref is not None
        assert source_job.logparse_product is not None
        user_result = Artifact(
            artifact_id=USER_RESULT_ARTIFACT_ID,
            case_id=CASE_ID,
            kind=ArtifactKind.USER_RESULT,
            name="diagnosis-result.json",
            content_type="application/json",
            resource_kind=user_result_ref.resource_kind,
            size=user_result_ref.size,
            sha256=user_result_ref.sha256,
            storage_key=user_result_ref.storage_key,
            metadata=UserResultMetadata(
                schema_version=2,
                format_id="problem-locator-diagnosis-v2",
                description="Canonical diagnosis result for export coverage.",
            ),
            created_by_job_id=source_job.job_id,
            created_at="2026-07-31T00:02:00.000Z",
        )
        logparse_run = Artifact(
            artifact_id=LOGPARSE_ARTIFACT_ID,
            case_id=CASE_ID,
            kind=ArtifactKind.LOGPARSE_RUN,
            name="rpc-logparse-run",
            content_type=(
                "application/vnd.problem-locator.logparse-run+directory"
            ),
            resource_kind=logparse_ref.resource_kind,
            size=logparse_ref.size,
            sha256=logparse_ref.sha256,
            storage_key=logparse_ref.storage_key,
            metadata=LogparseRunMetadata(
                tree_manifest_sha256=logparse_ref.sha256,
                logparse_version_ref=source_job.logparse_tool_ref,
                parse_manifest_relative_path="parse_manifest.json",
                source_attachment_id=attachment.attachment_id,
                source_attachment_sha256=attachment.sha256,
                parse_parameters=LogparseParseParameters(
                    product=source_job.logparse_product
                ),
            ),
            created_by_job_id=source_job.job_id,
            created_at="2026-07-31T00:02:00.000Z",
        )
        resource_evidence = Evidence(
            evidence_id=RESOURCE_EVIDENCE_ID,
            case_id=CASE_ID,
            source_type=EvidenceSourceType.ATTACHMENT,
            source_ref=attachment.attachment_id,
            locator=AttachmentEvidenceLocator(
                kind="ATTACHMENT",
                byte_start=0,
                byte_end_exclusive=1,
            ),
            summary="The attached RPC trace records the timed-out inventory request.",
            collected_at="2026-07-31T00:02:00.000Z",
            content_hash=evidence_ref.sha256,
            resource_ref=evidence_ref,
        )

        initial = graph.repository.read_snapshot()
        receipt = graph.repository.commit(
            initial.generation,
            None,
            build_state_mutation(
                upsert_case=seed_aggregate.case,
                insert_jobs=[source_job],
                upsert_attachments=[attachment],
                insert_evidence=[
                    *seed_aggregate.evidence.values(),
                    resource_evidence,
                ],
                insert_artifacts=[user_result, logparse_run],
            ),
        )

        report = graph.state_admin.validate_state()
        exported_bytes = graph.state_admin.export_state()
        exported = parse_canonical_json_bytes(exported_bytes, StateExport)
        expected_resources = sorted(
            [
                StateExportResource(
                    resource_kind=ResourceKind.FILE,
                    storage_key=attachment.storage_key,
                    size=attachment.size,
                    sha256=attachment.sha256,
                ),
                StateExportResource.model_validate(
                    evidence_ref.model_dump(mode="python")
                ),
                StateExportResource.model_validate(
                    logparse_ref.model_dump(mode="python")
                ),
                StateExportResource.model_validate(
                    user_result_ref.model_dump(mode="python")
                ),
            ],
            key=lambda resource: resource.storage_key,
        )

        assert report.valid is True
        assert exported.source_generation == receipt.generation == report.generation
        assert exported.state == graph.repository.read_snapshot()
        assert exported.resources == expected_resources
        assert [resource.storage_key for resource in exported.resources] == sorted(
            resource.storage_key for resource in exported.resources
        )
        assert exported.object_counts == report.object_counts
        assert exported.object_counts.model_dump(mode="python") == {
            "cases": 1,
            "jobs": 1,
            "outcomes": 0,
            "outcome_processing_records": 0,
            "execution_failure_records": 0,
            "attachments": 1,
            "evidence": 2,
            "artifacts": 2,
            "idempotency_records": 0,
            "runtime_epochs": 0,
            "recovery_processing_records": 0,
        }
        assert canonical_json_bytes(exported) == exported_bytes
    finally:
        graph.close()

    standalone = StandaloneStateAdmin(graph.layout.data_root)
    standalone_report = standalone.validate_state()
    standalone_bytes = standalone.export_state()
    standalone_export = parse_canonical_json_bytes(standalone_bytes, StateExport)

    assert standalone_report.valid is True
    assert standalone_report.generation == receipt.generation
    assert standalone_export.source_generation == receipt.generation
    assert standalone_export.object_counts == standalone_report.object_counts
    assert standalone_export.resources == expected_resources
    assert standalone_bytes == exported_bytes
    assert canonical_json_bytes(standalone_export) == standalone_bytes
