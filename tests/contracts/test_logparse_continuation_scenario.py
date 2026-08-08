from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from problem_locator.contracts.models import (
    ArtifactProposal,
    Job,
    JobOutcome,
    LogparseParseClaim,
    WorkspaceInputManifest,
)
from problem_locator.contracts.serialization import (
    canonical_json_bytes,
    canonical_json_sha256,
)

from tests.contracts._support import FIXTURE_ROOT, load_json
from tests.contracts.fakes import CountingLogparseAdapter, InMemoryResourceStore


CASE_ID = "00000000-0000-0000-0000-000000000001"
FIRST_JOB_ID = "00000000-0000-0000-0000-000000000011"
NEXT_JOB_ID = "00000000-0000-0000-0000-000000000012"
FIRST_OUTCOME_ID = "00000000-0000-0000-0000-000000000021"
LOG_ARCHIVE_ID = "00000000-0000-0000-0000-000000000050"
LOGPARSE_ARTIFACT_ID = "00000000-0000-0000-0000-000000000060"
LOGPARSE_EVIDENCE_ID = "00000000-0000-0000-0000-000000000040"
ORDER_REQUIREMENT_ID = "00000000-0000-0000-0000-000000000090"
ORDER_FACT_ID = "00000000-0000-0000-0000-000000000076"
LOG_ARCHIVE_SHA256 = "2" * 64
PRODUCT = "payment-service"
RPC_TIMEOUT_SCENARIO = load_json(
    FIXTURE_ROOT / "positive" / "rpc-timeout-continuation.json"
)
PARAMETERS_A = RPC_TIMEOUT_SCENARIO["parameter_group_a"]
PARAMETER_B = RPC_TIMEOUT_SCENARIO["parameter_group_b"]
LOGPARSE_TOOL_REF = {
    "id": "logparse",
    "version": "1.0.0",
    "content_hash": "f" * 64,
}


def _write_parsed_tree(root: Path) -> tuple[bytes, bytes]:
    parse_manifest_bytes = canonical_json_bytes(
        {
            "product": PRODUCT,
            "target_logs": ["targets/timeout.log"],
            "version": 1,
        }
    )
    target_bytes = (
        b"2026-07-31T00:00:00.000Z ReserveStock order pending\n"
    )
    (root / "targets").mkdir(parents=True)
    (root / "parse_manifest.json").write_bytes(parse_manifest_bytes)
    (root / "targets" / "timeout.log").write_bytes(target_bytes)
    return parse_manifest_bytes, target_bytes


def _parse_stage_and_propose(
    tmp_path: Path,
) -> tuple[
    CountingLogparseAdapter,
    InMemoryResourceStore,
    Path,
    ArtifactProposal,
    bytes,
    bytes,
]:
    parsed_root = tmp_path / "first-job" / "parsed-run"
    parse_bytes: bytes | None = None
    target_bytes: bytes | None = None

    def first_parse(archive: dict[str, str], *, product: str) -> Path:
        nonlocal parse_bytes, target_bytes
        assert archive == {
            "attachment_id": LOG_ARCHIVE_ID,
            "sha256": LOG_ARCHIVE_SHA256,
        }
        assert product == PRODUCT
        parse_bytes, target_bytes = _write_parsed_tree(parsed_root)
        return parsed_root

    adapter = CountingLogparseAdapter(
        parse_results=[first_parse],
        target_log_results=[["targets/timeout.log"]],
    )
    actual_root = adapter.parse(
        {"attachment_id": LOG_ARCHIVE_ID, "sha256": LOG_ARCHIVE_SHA256},
        product=PRODUCT,
    )
    assert actual_root == parsed_root
    assert parse_bytes is not None and target_bytes is not None

    store = InMemoryResourceStore()
    staged = store.stage_tree(FIRST_JOB_ID, "logparse_run", parsed_root)
    proposal = ArtifactProposal(
        proposal_key="logparse_run",
        artifact_kind="LOGPARSE_RUN",
        name="rpc-logparse-run",
        content_type="application/vnd.problem-locator.logparse-run+directory",
        resource_kind="DIRECTORY",
        size=staged.size,
        sha256=staged.sha256,
        staged_resource_ref=staged,
        metadata={
            "tree_manifest_sha256": staged.sha256,
            "logparse_version_ref": LOGPARSE_TOOL_REF,
            "parse_manifest_relative_path": "parse_manifest.json",
            "source_attachment_id": LOG_ARCHIVE_ID,
            "source_attachment_sha256": LOG_ARCHIVE_SHA256,
            "parse_parameters": {"product": PRODUCT},
        },
    )
    return adapter, store, parsed_root, proposal, parse_bytes, target_bytes


def test_real_staged_tree_hashes_into_a_valid_logparse_run_proposal(
    tmp_path: Path,
) -> None:
    _, _, root, proposal, parse_bytes, target_bytes = _parse_stage_and_propose(
        tmp_path
    )
    staged = proposal.staged_resource_ref
    assert staged.tree_manifest is not None
    expected_entries = [
        {
            "path": "parse_manifest.json",
            "size": len(parse_bytes),
            "sha256": hashlib.sha256(parse_bytes).hexdigest(),
        },
        {
            "path": "targets/timeout.log",
            "size": len(target_bytes),
            "sha256": hashlib.sha256(target_bytes).hexdigest(),
        },
    ]
    assert staged.tree_manifest.model_dump(mode="json") == {
        "version": 1,
        "entries": expected_entries,
    }
    tree_manifest_bytes = canonical_json_bytes(staged.tree_manifest)
    assert staged.sha256 == hashlib.sha256(tree_manifest_bytes).hexdigest()
    assert staged.size == len(parse_bytes) + len(target_bytes)
    parse_manifest_path = root / proposal.metadata.parse_manifest_relative_path
    assert parse_manifest_path.is_file()
    assert parse_manifest_path.read_bytes() == parse_bytes

    missing_manifest = proposal.model_dump(mode="json")
    missing_manifest["metadata"]["parse_manifest_relative_path"] = "missing.json"
    with pytest.raises(ValidationError, match="must name a manifest file"):
        ArtifactProposal.model_validate(missing_manifest)


def _user_fact(
    *, item_id: str, name: str, value: str, source_ref: str, revision: int
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "statement": value,
        "status": "ACTIVE",
        "provenance": {
            "source_type": "USER_INPUT",
            "source_ref": source_ref,
            "input_name": name,
        },
        "evidence_refs": [],
        "created_revision": revision,
        "supersedes": [],
    }


def _first_job_and_manifest() -> tuple[Job, WorkspaceInputManifest]:
    job_payload = load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    job_payload.update(
        {
            "job_id": FIRST_JOB_ID,
            "case_id": CASE_ID,
            "goal": "Analyze parameter group A and the unique log_archive; request missing inputs.",
            "evidence_refs": [],
            "attachment_refs": [LOG_ARCHIVE_ID],
            "artifact_refs": [],
            "previous_outcome_refs": [],
            "logparse_tool_ref": LOGPARSE_TOOL_REF,
            "logparse_product": PRODUCT,
        }
    )
    job_payload["context_snapshot"]["evidence_refs"] = []
    job_payload["context_snapshot"]["user_facts"] = [
        _user_fact(
            item_id=f"00000000-0000-0000-0000-{71 + index:012d}",
            name=name,
            value=value,
            source_ref="00000000-0000-0000-0000-000000000031",
            revision=2,
        )
        for index, (name, value) in enumerate(PARAMETERS_A.items())
    ]
    job = Job.model_validate(job_payload)

    manifest = WorkspaceInputManifest(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        logparse_tool_ref=job.logparse_tool_ref,
        logparse_product=job.logparse_product,
        resolved_logparse_plan={
            "schema_version": 2,
            "attachment_id": LOG_ARCHIVE_ID,
            "artifact_id": None,
            "problem_time": PARAMETERS_A["problem_time"],
            "anchors": [
                {
                    "label": "caller",
                    "module": PARAMETERS_A["caller_service"],
                    "slot": "1",
                    "process_name": PARAMETERS_A["caller_service"],
                    "pid": None,
                }
            ],
        },
        review_subject=None,
        entries=[
            {
                "input_kind": "ATTACHMENT",
                "resource_id": LOG_ARCHIVE_ID,
                "relative_path": (
                    f"inputs/attachments/{LOG_ARCHIVE_ID}/payload.tar.gz"
                ),
                "resource_kind": "FILE",
                "size": 128,
                "sha256": LOG_ARCHIVE_SHA256,
                "content_type": "application/gzip",
                "filename_suffix": ".tar.gz",
            }
        ],
    )
    return job, manifest


def _need_order_id_outcome(
    job: Job, proposal: ArtifactProposal
) -> JobOutcome:
    outcome_payload = load_json(
        FIXTURE_ROOT / "positive" / "job-outcome-diagnosis.json"
    )
    outcome_payload.update(
        {
            "outcome_id": FIRST_OUTCOME_ID,
            "job_id": job.job_id,
            "case_id": job.case_id,
            "job_type": job.job_type.value,
            "base_state_revision": job.base_state_revision,
            "result_type": "NEED_INPUT",
            "consumed_evidence_refs": [],
            "proposed_artifacts": [proposal.model_dump(mode="json")],
            "error": None,
        }
    )
    evidence_binding = {
        "existing_evidence_id": None,
        "evidence_proposal_key": "logparse_timeout",
    }
    logparse_locator = {
        "kind": "LOGPARSE",
        "relative_path": "targets/timeout.log",
        "start_line": 1,
        "end_line": 1,
        "start_time": None,
        "end_time": None,
    }
    outcome_payload["proposed_evidence"] = [
        {
            "proposal_key": "logparse_timeout",
            "source_type": "LOGPARSE",
            "source_binding": {
                "existing_source_ref": None,
                "artifact_proposal_key": "logparse_run",
            },
            "locator": logparse_locator,
            "summary": "The parsed timeout log identifies the pending ReserveStock call.",
            "content_hash": hashlib.sha256(
                b"2026-07-31T00:00:00.000Z ReserveStock order pending\n"
            ).hexdigest(),
            "staged_resource_ref": None,
        }
    ]
    payload = outcome_payload["payload"]
    payload.update(
        {
            "findings": [
                {
                    "statement": "ReserveStock is pending, but the order identifier is required.",
                    "evidence_bindings": [evidence_binding],
                    "confidence": 0.8,
                }
            ],
            "requested_input": [ORDER_REQUIREMENT_ID],
            "requested_attachments": [],
            "candidate_conclusion_draft": None,
            "recommended_next_step": "Supply order_id and continue from the parsed run.",
        }
    )
    delta = payload["state_delta"]
    delta.update(
        {
            "proposed_facts": [
                {
                    "item_id": "00000000-0000-0000-0000-000000000075",
                    "statement": "ReserveStock remained pending in the target log.",
                    "provenance": {
                        "source_type": "AGENT_OUTCOME",
                        "source_ref": FIRST_OUTCOME_ID,
                        "input_name": None,
                    },
                    "evidence_bindings": [evidence_binding],
                    "supersedes": [],
                }
            ],
            "add_pending_requirements": [
                {
                    "requirement_id": ORDER_REQUIREMENT_ID,
                    "kind": "INPUT",
                    "name": "order_id",
                    "prompt": "Provide the order identifier for the timed-out request.",
                    "required": True,
                    "constraints": {
                        "value_type": "STRING",
                        "min_utf8_bytes": 1,
                        "max_utf8_bytes": 128,
                        "pattern": r"^order-[0-9-]+$",
                        "allowed_values": [],
                    },
                    "status": "OPEN",
                    "requested_by_job_id": job.job_id,
                    "fulfilled_by_refs": [],
                }
            ],
        }
    )
    return JobOutcome.model_validate(outcome_payload)


def _next_job(first_job: Job, outcome: JobOutcome) -> Job:
    payload = first_job.model_dump(mode="json")
    payload.update(
        {
            "job_id": NEXT_JOB_ID,
            "base_state_revision": 3,
            "goal": "Continue diagnosis with order_id without parsing the archive again.",
            "evidence_refs": [LOGPARSE_EVIDENCE_ID],
            "attachment_refs": [LOG_ARCHIVE_ID],
            "artifact_refs": [LOGPARSE_ARTIFACT_ID],
            "previous_outcome_refs": [outcome.outcome_id],
            "created_at": "2026-07-31T00:02:00.000Z",
        }
    )
    snapshot = payload["context_snapshot"]
    snapshot["diagnosis_state_revision"] = 3
    snapshot["evidence_refs"] = [LOGPARSE_EVIDENCE_ID]
    snapshot["user_facts"].append(
        _user_fact(
            item_id=ORDER_FACT_ID,
            name="order_id",
            value=PARAMETER_B["order_id"],
            source_ref="00000000-0000-0000-0000-000000000032",
            revision=3,
        )
    )
    snapshot["confirmed_facts"] = [
        {
            "item_id": "00000000-0000-0000-0000-000000000075",
            "statement": "ReserveStock remained pending in the target log.",
            "status": "ACTIVE",
            "provenance": {
                "source_type": "AGENT_OUTCOME",
                "source_ref": outcome.outcome_id,
                "input_name": None,
            },
            "evidence_refs": [LOGPARSE_EVIDENCE_ID],
            "created_revision": 3,
            "supersedes": [],
        }
    ]
    fulfilled = outcome.payload.state_delta.add_pending_requirements[0].model_dump(
        mode="json"
    )
    fulfilled["status"] = "FULFILLED"
    fulfilled["fulfilled_by_refs"] = [ORDER_FACT_ID]
    snapshot["pending_requirements"] = [fulfilled]
    return Job.model_validate(payload)


def _continuation_manifest(
    job: Job,
    first_manifest: WorkspaceInputManifest,
    outcome: JobOutcome,
    proposal: ArtifactProposal,
) -> WorkspaceInputManifest:
    previous_bytes = canonical_json_bytes(outcome)
    attachment_entry = first_manifest.entries[0].model_dump(mode="json")
    return WorkspaceInputManifest(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        logparse_tool_ref=job.logparse_tool_ref,
        logparse_product=job.logparse_product,
        resolved_logparse_plan={
            "schema_version": 2,
            "attachment_id": None,
            "artifact_id": LOGPARSE_ARTIFACT_ID,
            "problem_time": PARAMETERS_A["problem_time"],
            "anchors": [
                {
                    "label": "caller",
                    "module": PARAMETERS_A["caller_service"],
                    "slot": "1",
                    "process_name": PARAMETERS_A["caller_service"],
                    "pid": None,
                }
            ],
        },
        review_subject=None,
        entries=[
            attachment_entry,
            {
                "input_kind": "EVIDENCE",
                "resource_id": LOGPARSE_EVIDENCE_ID,
                "relative_path": None,
                "resource_kind": None,
                "size": None,
                "sha256": None,
                "source_type": "LOGPARSE",
                "source_ref": LOGPARSE_ARTIFACT_ID,
                "locator": outcome.proposed_evidence[0].locator.model_dump(
                    mode="json"
                ),
                "summary": outcome.proposed_evidence[0].summary,
                "content_hash": outcome.proposed_evidence[0].content_hash,
            },
            {
                "input_kind": "ARTIFACT",
                "resource_id": LOGPARSE_ARTIFACT_ID,
                "relative_path": f"inputs/artifacts/{LOGPARSE_ARTIFACT_ID}/tree",
                "resource_kind": "DIRECTORY",
                "size": proposal.size,
                "sha256": proposal.sha256,
                "artifact_kind": "LOGPARSE_RUN",
                "name": proposal.name,
                "content_type": proposal.content_type,
                "metadata": proposal.metadata.model_dump(mode="json"),
            },
            {
                "input_kind": "PREVIOUS_OUTCOME",
                "resource_id": outcome.outcome_id,
                "relative_path": f"inputs/outcomes/{outcome.outcome_id}/job_outcome.json",
                "resource_kind": "FILE",
                "size": len(previous_bytes),
                "sha256": hashlib.sha256(previous_bytes).hexdigest(),
                "source_job_id": outcome.job_id,
                "result_type": outcome.result_type,
            },
        ],
    )


def _input_values(job: Job) -> dict[str, str]:
    return {
        item.provenance.input_name: item.statement
        for item in job.context_snapshot.user_facts
        if item.provenance.input_name is not None
    }


def test_parameter_a_log_archive_then_parameter_b_reuses_the_logparse_run(
    tmp_path: Path,
) -> None:
    assert set(PARAMETERS_A) == {
        "caller_service",
        "server_service",
        "rpc_method",
        "problem_time",
    }
    assert RPC_TIMEOUT_SCENARIO["log_attachment_requirement"] == "log_archive"
    first_job, first_manifest = _first_job_and_manifest()
    claim = LogparseParseClaim(
        schema_version=1,
        job_id=first_job.job_id,
        attachment_id=LOG_ARCHIVE_ID,
        attachment_sha256=LOG_ARCHIVE_SHA256,
        artifact_proposal_key="logparse_run",
        logparse_tool_ref=first_job.logparse_tool_ref,
        request_sha256=canonical_json_sha256(
            {
                "attachment_id": LOG_ARCHIVE_ID,
                "product": first_manifest.logparse_product,
            }
        ),
    )
    adapter, store, _, proposal, _, _ = _parse_stage_and_propose(tmp_path)
    first_outcome = _need_order_id_outcome(first_job, proposal)
    next_job = _next_job(first_job, first_outcome)

    final_storage_key = (
        f"resources/cases/{CASE_ID}/artifacts/{LOGPARSE_ARTIFACT_ID}/tree"
    )
    published = store.publish(proposal.staged_resource_ref, final_storage_key)
    next_manifest = _continuation_manifest(
        next_job, first_manifest, first_outcome, proposal
    )
    materialized_root = (
        tmp_path
        / "next-job"
        / f"inputs/artifacts/{LOGPARSE_ARTIFACT_ID}/tree"
    )
    materialized = store.materialize_read_only(published, materialized_root)
    selected = adapter.target_logs(
        materialized.path,
        order_id=PARAMETER_B["order_id"],
    )

    assert claim.job_id == first_job.job_id
    assert claim.attachment_id == first_manifest.entries[0].resource_id
    assert _input_values(first_job) == PARAMETERS_A
    assert _input_values(next_job) == PARAMETERS_A | PARAMETER_B
    assert [entry.resource_id for entry in first_manifest.entries] == [
        LOG_ARCHIVE_ID
    ]
    assert first_outcome.result_type.value == "NEED_INPUT"
    assert first_outcome.payload.requested_input == [ORDER_REQUIREMENT_ID]
    assert first_outcome.payload.state_delta.add_pending_requirements[0].name == (
        "order_id"
    )
    assert first_outcome.proposed_artifacts == [proposal]

    assert [entry.input_kind for entry in next_manifest.entries] == [
        "ATTACHMENT",
        "EVIDENCE",
        "ARTIFACT",
        "PREVIOUS_OUTCOME",
    ]
    grouped_ids = {
        kind: [
            entry.resource_id
            for entry in next_manifest.entries
            if entry.input_kind == kind
        ]
        for kind in ("ATTACHMENT", "EVIDENCE", "ARTIFACT", "PREVIOUS_OUTCOME")
    }
    assert grouped_ids == {
        "ATTACHMENT": next_job.attachment_refs,
        "EVIDENCE": next_job.evidence_refs,
        "ARTIFACT": next_job.artifact_refs,
        "PREVIOUS_OUTCOME": next_job.previous_outcome_refs,
    }
    artifact_entry = next_manifest.entries[2]
    assert artifact_entry.sha256 == proposal.sha256 == published.sha256
    assert artifact_entry.size == proposal.size == published.size
    assert artifact_entry.metadata == proposal.metadata
    assert next_manifest.logparse_tool_ref == first_manifest.logparse_tool_ref
    assert next_manifest.logparse_product == first_manifest.logparse_product == PRODUCT
    assert (
        materialized_root / proposal.metadata.parse_manifest_relative_path
    ).is_file()
    assert selected == ["targets/timeout.log"]
    assert adapter.parse_count == 1
    assert adapter.target_logs_count == 1
    assert adapter.parse_calls[0][1] == {"product": PRODUCT}
    assert adapter.target_log_calls[0][1] == {"order_id": PARAMETER_B["order_id"]}

    previous_entry = next_manifest.entries[3]
    previous_bytes = canonical_json_bytes(first_outcome)
    assert previous_entry.size == len(previous_bytes)
    assert previous_entry.sha256 == hashlib.sha256(previous_bytes).hexdigest()


def test_continuation_manifest_is_stable_under_model_round_trip(
    tmp_path: Path,
) -> None:
    first_job, first_manifest = _first_job_and_manifest()
    _, _, _, proposal, _, _ = _parse_stage_and_propose(tmp_path)
    outcome = _need_order_id_outcome(first_job, proposal)
    next_job = _next_job(first_job, outcome)
    manifest = _continuation_manifest(next_job, first_manifest, outcome, proposal)

    encoded = canonical_json_bytes(manifest)
    reparsed = WorkspaceInputManifest.model_validate_json(encoded)
    assert canonical_json_bytes(reparsed) == encoded
    assert reparsed.entries[2].metadata.parse_parameters.product == PRODUCT


def test_logparse_run_proposal_rejects_a_manifest_path_not_in_its_tree(
    tmp_path: Path,
) -> None:
    _, _, _, proposal, _, _ = _parse_stage_and_propose(tmp_path)
    invalid = copy.deepcopy(proposal.model_dump(mode="json"))
    invalid["metadata"]["parse_manifest_relative_path"] = "absent.json"
    with pytest.raises(ValidationError, match="must name a manifest file"):
        ArtifactProposal.model_validate(invalid)
