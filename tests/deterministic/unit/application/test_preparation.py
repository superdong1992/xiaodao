from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from problem_locator.application.preparation import (
    build_create_case_trigger,
    build_uploading_attachment,
    claim_lifecycle_update,
    finalize_attachment,
    fixed_asset_refs,
    runtime_bindings_from_job,
)
from problem_locator.contracts import (
    AttachmentStatus,
    CreateCase,
    Job,
    JobStatus,
    JobType,
    PrepareAttachment,
    ResourceKind,
    ResourceRef,
    RuntimeBindings,
    TriggerType,
)


ROOT = Path(__file__).resolve().parents[4]
NOW = "2026-07-31T01:02:03.000Z"


def _route_job() -> Job:
    return Job.model_validate_json(
        (ROOT / "tests/fixtures/contracts/positive/job-route.json").read_text(
            encoding="utf-8"
        )
    )


def _route_bindings() -> RuntimeBindings:
    job = _route_job()
    return RuntimeBindings(
        diagnosis_mode=job.diagnosis_mode,
        review_policy=job.review_policy,
        generic_skill_name=job.generic_skill_name,
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


def _create_command() -> CreateCase:
    return CreateCase(
        idempotency_key="create-case-1",
        raw_problem_text="RPC timeout\nrequest-id: 请求-α-7",
        problem_spec={
            "statement": "RPC timeout",
            "expected_behavior": "RPC succeeds",
            "actual_behavior": "RPC times out",
            "scope": "payment to inventory",
            "goals": ["Locate the cause"],
            "non_goals": [],
            "constraints": [],
            "completion_criteria": ["Cause has evidence"],
        },
        initial_user_facts=[
            {"name": "region", "value": "us-east"},
            {"name": "request_id", "value": "req-7"},
        ],
        wait_seconds=30,
    )


def _prepare_command() -> PrepareAttachment:
    return PrepareAttachment(
        idempotency_key="prepare-1",
        case_id="00000000-0000-0000-0000-000000000001",
        expected_case_revision=1,
        name="server.log",
        content_type="text/plain",
        declared_size=5,
        declared_sha256="2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    )


def test_create_trigger_preserves_input_and_preallocated_fact_ids() -> None:
    fact_ids = [
        "00000000-0000-0000-0000-000000000101",
        "00000000-0000-0000-0000-000000000102",
    ]

    trigger = build_create_case_trigger(
        _create_command(),
        case_id="00000000-0000-0000-0000-000000000001",
        trigger_id="00000000-0000-0000-0000-000000000002",
        user_fact_ids=fact_ids,
        route_bindings=_route_bindings(),
        occurred_at=NOW,
    )

    assert trigger.trigger_type is TriggerType.CREATE_CASE
    assert trigger.expected_case_revision == 0
    assert trigger.payload.problem_spec.revision == 1
    assert trigger.payload.raw_problem_text == "RPC timeout\nrequest-id: 请求-α-7"
    assert [item.item_id for item in trigger.payload.initial_user_facts] == fact_ids
    assert [item.statement for item in trigger.payload.initial_user_facts] == [
        "us-east",
        "req-7",
    ]
    assert all(
        item.provenance.source_ref == trigger.trigger_id
        for item in trigger.payload.initial_user_facts
    )
    assert set(trigger.runtime_bindings_by_job_type) == {JobType.ROUTE}
    assert trigger.continuation_resources.model_dump(mode="json") == {
        "evidence_refs": [],
        "attachment_refs": [],
        "artifact_refs": [],
        "previous_outcome_refs": [],
    }


def test_create_trigger_rejects_misaligned_preallocated_ids() -> None:
    with pytest.raises(ValueError, match="one-for-one"):
        build_create_case_trigger(
            _create_command(),
            case_id="00000000-0000-0000-0000-000000000001",
            trigger_id="00000000-0000-0000-0000-000000000002",
            user_fact_ids=[],
            route_bindings=_route_bindings(),
            occurred_at=NOW,
        )


def test_prepare_and_finalize_attachment_preserve_immutable_metadata() -> None:
    attachment = build_uploading_attachment(
        _prepare_command(),
        attachment_id="00000000-0000-0000-0000-000000000003",
        occurred_at=NOW,
    )
    published = ResourceRef(
        resource_kind=ResourceKind.FILE,
        storage_key="cases/00000000-0000-0000-0000-000000000001/attachments/00000000-0000-0000-0000-000000000003/server.log",
        size=5,
        sha256="2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    )

    ready = finalize_attachment(
        attachment,
        published,
        occurred_at="2026-07-31T01:03:00.000Z",
    )

    assert attachment.status is AttachmentStatus.UPLOADING
    assert attachment.storage_key is None
    assert ready.status is AttachmentStatus.READY
    assert ready.name == attachment.name
    assert ready.content_type == attachment.content_type
    assert ready.storage_key == published.storage_key
    assert ready.updated_at == "2026-07-31T01:03:00.000Z"


def test_build_uploading_attachment_consumes_frozen_archive_name_type_rule() -> None:
    archive = _prepare_command().model_copy(
        update={
            "name": "logs.tar.gz",
            "content_type": "application/gzip",
        }
    )

    attachment = build_uploading_attachment(
        archive,
        attachment_id="00000000-0000-0000-0000-000000000003",
        occurred_at=NOW,
    )

    assert attachment.name == "logs.tar.gz"
    assert attachment.content_type == "application/gzip"

    drifted = PrepareAttachment.model_construct(
        **(archive.model_dump(mode="python") | {"name": "logs.zip"})
    )
    with pytest.raises(ValidationError, match="suffix|content_type"):
        build_uploading_attachment(
            drifted,
            attachment_id="00000000-0000-0000-0000-000000000004",
            occurred_at=NOW,
        )


def test_finalize_attachment_requires_uploading_file() -> None:
    attachment = build_uploading_attachment(
        _prepare_command(),
        attachment_id="00000000-0000-0000-0000-000000000003",
        occurred_at=NOW,
    )
    directory = ResourceRef(
        resource_kind=ResourceKind.DIRECTORY,
        storage_key="cases/00000000-0000-0000-0000-000000000001/artifacts/run",
        size=5,
        sha256="2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    )
    with pytest.raises(ValueError, match="FILE"):
        finalize_attachment(attachment, directory, occurred_at=NOW)


def test_fixed_asset_refs_are_complete_stable_and_unique() -> None:
    job = _route_job()
    refs = fixed_asset_refs(job)

    assert refs[: 1 + len(job.available_skill_refs)] == [
        job.agent_profile_ref,
        *job.available_skill_refs,
    ]
    assert refs[-3:] == [
        job.tool_bundle_ref,
        job.context_policy_ref,
        job.output_contract_ref,
    ]
    assert len(refs) == len(
        {(item.id, item.version, item.content_hash) for item in refs}
    )


def test_runtime_bindings_are_recovered_without_catalog_substitution() -> None:
    job = _route_job()

    bindings = runtime_bindings_from_job(job)

    assert bindings.model_dump(mode="json") == _route_bindings().model_dump(
        mode="json"
    )


def test_claim_update_uses_injected_epoch_and_time() -> None:
    job = _route_job()

    update = claim_lifecycle_update(
        job,
        runtime_epoch="00000000-0000-0000-0000-000000000777",
        started_at=NOW,
    )

    assert update.expected_status is JobStatus.PENDING
    assert update.target_status is JobStatus.RUNNING
    assert update.runtime_epoch == "00000000-0000-0000-0000-000000000777"
    assert update.started_at == NOW
    assert update.finished_at is None

    running = job.model_copy(
        update={
            "status": JobStatus.RUNNING,
            "started_at": NOW,
            "runtime_epoch": "00000000-0000-0000-0000-000000000777",
        }
    )
    with pytest.raises(ValueError, match="PENDING"):
        claim_lifecycle_update(running, runtime_epoch="epoch", started_at=NOW)
