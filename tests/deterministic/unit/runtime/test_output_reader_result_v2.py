from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from problem_locator.contracts import (
    Job,
    TreeManifest,
    TreeManifestEntry,
    WorkspaceInputManifest,
    bytes_sha256,
    canonical_json_bytes,
)
from problem_locator.runtime.outcome_finalizer import (
    DRAFT_FINALIZATION_MARKER_NAME,
    SealedAgentOutcomeDraftMarker,
)
from problem_locator.runtime.output_reader import RejectedAgentOutputError, read_agent_output


ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "tests/fixtures/contracts/positive"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000060"
EVIDENCE_ID = "00000000-0000-0000-0000-000000000040"


def _json(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_bytes())


def _write_draft(root: Path, value: dict[str, object]) -> None:
    data = canonical_json_bytes(value)
    path = root / "output/job_outcome.draft.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    marker = SealedAgentOutcomeDraftMarker(
        schema_version=2,
        relative_path="output/job_outcome.draft.json",
        size=len(data),
        sha256=bytes_sha256(data),
    )
    state = root / "runtime/tool-state"
    state.mkdir(parents=True)
    (state / DRAFT_FINALIZATION_MARKER_NAME).write_bytes(
        canonical_json_bytes(marker)
    )


def _audit(
    manifest: WorkspaceInputManifest,
    targets: list[dict[str, object]],
) -> bytes:
    plan = manifest.resolved_logparse_plan
    assert plan is not None and plan.artifact_id is not None
    request = {
        "schema_version": 1,
        "problem_time": plan.problem_time,
        "anchors": [item.model_dump(mode="json") for item in plan.anchors],
        "artifact_id": plan.artifact_id,
    }
    result = {
        "schema_version": 1,
        "api_version": 1,
        "target_logs": targets,
    }
    return canonical_json_bytes(
        {
            "schema_version": 1,
            "job_id": manifest.job_id,
            "operations": [
                {
                    "operation": "target-logs",
                    "request_sha256": bytes_sha256(canonical_json_bytes(request)),
                    "request": request,
                    "http_status": 200,
                    "result_sha256": bytes_sha256(canonical_json_bytes(result)),
                    "result": result,
                }
            ],
        }
    )


def _parse_audit(
    manifest: WorkspaceInputManifest,
    targets: list[dict[str, object]],
    artifact_draft: dict[str, object],
) -> bytes:
    plan = manifest.resolved_logparse_plan
    assert plan is not None and plan.attachment_id is not None
    request = {
        "schema_version": 1,
        "problem_time": plan.problem_time,
        "anchors": [item.model_dump(mode="json") for item in plan.anchors],
        "attachment_id": plan.attachment_id,
        "artifact_proposal_key": "logparse-run",
    }
    result = {
        "schema_version": 1,
        "api_version": 1,
        "target_logs": targets,
        "logparse_run_artifact_draft": artifact_draft,
    }
    return canonical_json_bytes(
        {
            "schema_version": 1,
            "job_id": manifest.job_id,
            "operations": [
                {
                    "operation": "parse-targets",
                    "request_sha256": bytes_sha256(canonical_json_bytes(request)),
                    "request": request,
                    "http_status": 200,
                    "result_sha256": bytes_sha256(canonical_json_bytes(result)),
                    "result": result,
                }
            ],
        }
    )


def _prepared_values(
    root: Path,
) -> tuple[Job, WorkspaceInputManifest, dict[str, object], list[dict[str, object]]]:
    job = Job.model_validate(_json("job-diagnose.json"))
    manifest_value = _json("workspace-input-manifest.json")
    anchors = [
        {
            "label": "caller",
            "module": "payment",
            "slot": "request",
            "process_name": "payment-service",
            "pid": None,
        },
        {
            "label": "server",
            "module": "inventory",
            "slot": "backend",
            "process_name": "inventory-service",
            "pid": "202",
        },
    ]
    manifest_value["resolved_logparse_plan"]["anchors"] = anchors
    tree_files = {
        "logs/caller.log": b"[0001] caller timeout\r\n",
        "logs/server.log": b"[0001] server takeover\n",
        "parse_manifest.json": canonical_json_bytes({"schema_version": 1}),
    }
    tree = TreeManifest(
        version=1,
        entries=[
            TreeManifestEntry(
                path=path,
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
            for path, content in sorted(tree_files.items())
        ],
    )
    tree_hash = bytes_sha256(canonical_json_bytes(tree))
    artifact = next(
        item
        for item in manifest_value["entries"]
        if item["input_kind"] == "ARTIFACT"
    )
    artifact["size"] = sum(len(content) for content in tree_files.values())
    artifact["sha256"] = tree_hash
    artifact["metadata"]["tree_manifest_sha256"] = tree_hash
    evidence = next(
        item
        for item in manifest_value["entries"]
        if item["input_kind"] == "EVIDENCE"
    )
    evidence.update(
        source_type="LOGPARSE",
        source_ref=ARTIFACT_ID,
        locator={
            "kind": "LOGPARSE",
            "relative_path": "logs/caller.log",
            "start_line": 1,
            "end_line": 1,
            "start_time": None,
            "end_time": None,
        },
    )
    manifest = WorkspaceInputManifest.model_validate(manifest_value)
    tree_root = root / f"inputs/artifacts/{ARTIFACT_ID}/tree"
    for path, content in tree_files.items():
        target = tree_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    targets = [
        {
            "label": "caller",
            "module": "payment",
            "module_key": "payment",
            "module_name": "payment",
            "slot": "slot_request",
            "process_name": "payment-service",
            "match_status": "exact",
            "caveats": [],
            "log_path": "logs/caller.log",
        },
        {
            "label": "server",
            "module": "inventory",
            "module_key": "inventory",
            "module_name": "inventory",
            "slot": "slot_backend",
            "process_name": "inventory-service",
            "pid": "202",
            "match_status": "exact",
            "caveats": [],
            "log_path": "logs/server.log",
        },
    ]
    return job, manifest, _json("agent-job-outcome-draft-diagnosis.json"), targets


def test_capture_scope_and_order_come_from_all_resolved_anchors(tmp_path: Path) -> None:
    job, manifest, draft, targets = _prepared_values(tmp_path)
    _write_draft(tmp_path, draft)

    validated = read_agent_output(
        tmp_path,
        job,
        manifest,
        broker_audit_bytes=_audit(manifest, targets),
    )

    assert validated.authoritative_targets is not None
    assert [item.target.label for item in validated.target_logs] == [
        "caller",
        "server",
    ]
    assert [item.target.archive_name for item in validated.target_logs] == [
        "caller__payment__slot_request__payment-service.log",
        "server__inventory__slot_backend__inventory-service-202.log",
    ]
    assert [item.content for item in validated.target_logs] == [
        b"[0001] caller timeout\r\n",
        b"[0001] server takeover\n",
    ]
    assert [
        binding.existing_evidence_id
        for binding in validated.target_logs[0].evidence_bindings
    ] == [EVIDENCE_ID]
    # The Candidate cites only the caller Evidence, but the second anchor is
    # still captured and delivered in plan order.
    assert validated.target_logs[1].evidence_bindings == ()


@pytest.mark.parametrize("audit_mode", ["absent", "no-success"])
def test_failed_draft_with_resolved_plan_does_not_require_target_audit_success(
    tmp_path: Path,
    audit_mode: str,
) -> None:
    job, manifest, draft, _targets = _prepared_values(tmp_path)
    draft.update(
        result_type="FAILED",
        payload=None,
        consumed_evidence_refs=[],
        proposed_evidence_drafts=[],
        proposed_artifact_drafts=[],
        rule_claims=[],
        error={
            "stage": "TOOL_EXECUTE",
            "code": "LOGPARSE_FAILED",
            "message": "The fixed logparse execution failed.",
            "retryable": False,
            "details": [],
        },
    )
    _write_draft(tmp_path, draft)
    broker_audit_bytes = (
        None
        if audit_mode == "absent"
        else canonical_json_bytes(
            {
                "schema_version": 1,
                "job_id": manifest.job_id,
                "operations": [],
            }
        )
    )

    validated = read_agent_output(
        tmp_path,
        job,
        manifest,
        broker_audit_bytes=broker_audit_bytes,
    )

    assert validated.draft.result_type.value == "FAILED"
    assert validated.draft.error is not None
    assert validated.draft.error.code.value == "LOGPARSE_FAILED"
    assert validated.authoritative_targets is None
    assert validated.target_logs == ()


def test_missing_target_is_preserved_for_inconclusive_server_result(
    tmp_path: Path,
) -> None:
    job, manifest, draft, targets = _prepared_values(tmp_path)
    targets[1].pop("log_path")
    targets[1]["match_status"] = "missing"
    _write_draft(tmp_path, draft)

    validated = read_agent_output(
        tmp_path,
        job,
        manifest,
        broker_audit_bytes=_audit(manifest, targets),
    )

    assert validated.authoritative_targets is not None
    assert [item.match_status for item in validated.authoritative_targets.targets] == [
        "exact",
        "missing",
    ]
    assert [item.target.label for item in validated.target_logs] == ["caller"]


def test_new_parse_tree_cannot_drift_from_broker_owned_tree_hash(
    tmp_path: Path,
) -> None:
    job, existing_manifest, draft, targets = _prepared_values(tmp_path)
    manifest_value = existing_manifest.model_dump(mode="json")
    attachment = next(
        item
        for item in manifest_value["entries"]
        if item["input_kind"] == "ATTACHMENT"
    )
    plan = manifest_value["resolved_logparse_plan"]
    plan["attachment_id"] = attachment["resource_id"]
    plan["artifact_id"] = None
    manifest = WorkspaceInputManifest.model_validate(manifest_value)

    original_files = {
        "logs/caller.log": b"[0001] caller timeout\r\n",
        "logs/server.log": b"[0001] server takeover\n",
        "parse_manifest.json": canonical_json_bytes({"schema_version": 1}),
    }

    def tree_hash(files: dict[str, bytes]) -> str:
        tree = TreeManifest(
            version=1,
            entries=[
                TreeManifestEntry(
                    path=path,
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
                for path, content in sorted(files.items())
            ],
        )
        return bytes_sha256(canonical_json_bytes(tree))

    broker_hash = tree_hash(original_files)
    broker_draft = {
        "proposal_key": "logparse-run",
        "artifact_kind": "LOGPARSE_RUN",
        "name": "logparse-run",
        "content_type": "application/vnd.problem-locator.logparse-run+directory",
        "resource_kind": "DIRECTORY",
        "workspace_relative_path": "output/proposals/logparse-run/tree",
        "declared_size": None,
        "declared_sha256": None,
        "metadata": {
            "tree_manifest_sha256": broker_hash,
            "logparse_version_ref": job.logparse_tool_ref.model_dump(mode="json"),
            "parse_manifest_relative_path": "parse_manifest.json",
            "source_attachment_id": attachment["resource_id"],
            "source_attachment_sha256": attachment["sha256"],
            "parse_parameters": {"product": job.logparse_product},
        },
    }
    drifted_files = dict(original_files)
    drifted_files["logs/caller.log"] = b"[0001] tampered after broker response\r\n"
    agent_draft = json.loads(canonical_json_bytes(broker_draft))
    agent_draft["metadata"]["tree_manifest_sha256"] = tree_hash(drifted_files)
    draft["proposed_artifact_drafts"] = [agent_draft]
    output_tree = tmp_path / "output/proposals/logparse-run/tree"
    for path, content in drifted_files.items():
        destination = output_tree / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    _write_draft(tmp_path, draft)

    with pytest.raises(RejectedAgentOutputError) as captured:
        read_agent_output(
            tmp_path,
            job,
            manifest,
            broker_audit_bytes=_parse_audit(manifest, targets, broker_draft),
        )

    assert captured.value.failure_category == "authoritative_target_capture"
