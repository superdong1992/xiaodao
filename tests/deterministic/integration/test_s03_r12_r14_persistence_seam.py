from __future__ import annotations

import hashlib
import json
from pathlib import Path

from problem_locator.application.preparation import make_user_fact
from problem_locator.contracts import (
    Attachment,
    AttachmentStatus,
    Evidence,
    EvidenceSourceType,
    Job,
    JobStatus,
    ResourceKind,
    ResourceType,
    StateFile,
    UserFactInput,
)
from problem_locator.storage.coordination import (
    InProcessAttachmentUploadGuard,
    InProcessPublicationCommitGuard,
)
from problem_locator.storage.resource_store import FileResourceStore
from tests.deterministic.contracts.fakes import InMemoryBinaryStream


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/contracts/positive"
CASE_ID = "00000000-0000-0000-0000-000000000001"
EVIDENCE_ID = "00000000-0000-0000-0000-000000000040"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000050"
USER_FACT_ID = "00000000-0000-0000-0000-000000000030"
USER_FACT_TRIGGER_ID = "00000000-0000-0000-0000-000000000031"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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
