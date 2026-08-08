from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from problem_locator.contracts import (
    AgentJobOutcomeDraftV2,
    DecisionAuditV2,
    EvidenceBinding,
    Job,
    OutcomeResultType,
    ReviewSubjectV2,
    ServerRuleStatus,
    WorkspaceInputManifest,
    canonical_json_bytes,
    validate_workspace_manifest_for_job,
)
from problem_locator.runtime.authoritative_targets import (
    AuthoritativeTargetLog,
    AuthoritativeTargetSet,
    semantic_archive_name,
)
from problem_locator.runtime.result_types import CapturedTargetLog
from problem_locator.runtime.server_outcome_finalizer import (
    finalize_server_outcome,
)
from problem_locator.runtime.server_verifier import verify_agent_draft


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "tests/fixtures/contracts/positive"
SPEC = (
    ROOT
    / "tests/fixtures/components/diagnosis-generator/specs/rpc-service-takeover.json"
)
MANUAL_SPEC = (
    ROOT
    / "tests/fixtures/components/diagnosis-generator/specs/manual-triage.json"
)
CLIENT_EVIDENCE = "00000000-0000-0000-0000-000000000041"
SERVER_EVIDENCE = "00000000-0000-0000-0000-000000000042"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000060"
OTHER_ARTIFACT_ID = "00000000-0000-0000-0000-000000000061"


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_bytes())


def _broker_audit(
    manifest: WorkspaceInputManifest,
    *,
    first_match_status: str = "exact",
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
        "target_logs": [
            {
                "label": "client",
                "module": "compact",
                "module_key": "compact",
                "module_name": "compact",
                "slot": "slot_1",
                "process_name": "checkout-client",
                "match_status": "exact",
                "caveats": [],
                "log_path": "client.log",
            },
            {
                "label": "server",
                "module": "compact",
                "module_key": "compact",
                "module_name": "compact",
                "slot": "slot_2",
                "process_name": "inventory-server",
                "match_status": "exact",
                "caveats": [],
                "log_path": "server.log",
            },
        ],
    }
    if first_match_status != "exact":
        first = result["target_logs"][0]
        first["match_status"] = first_match_status
        first["caveats"] = [f"Synthetic {first_match_status} target."]
        del first["log_path"]
    return canonical_json_bytes(
        {
            "schema_version": 1,
            "job_id": manifest.job_id,
            "operations": [
                {
                    "operation": "target-logs",
                    "request_sha256": hashlib.sha256(
                        canonical_json_bytes(request)
                    ).hexdigest(),
                    "request": request,
                    "http_status": 200,
                    "result_sha256": hashlib.sha256(
                        canonical_json_bytes(result)
                    ).hexdigest(),
                    "result": result,
                }
            ],
        }
    )


def _build(
    tmp_path: Path,
    *,
    problem_time: str,
    hidden_duplicate_client_event: bool = False,
    rpc_method_fact: str = "Reserve",
    client_rpc_method: str = "Reserve",
    server_rpc_method: str = "Reserve",
    client_order_id: str = "ord-1",
    server_order_id: str = "ord-1",
    client_time: str = "2026-01-03T00:00:03.000Z",
    takeover_time: str = "2026-01-03T00:00:00.500Z",
    pool_wait_time: str = "2026-01-03T00:00:02.000Z",
    null_client_locator: bool = False,
    fixed_time_reference: str | None = None,
    include_order_fact: bool = True,
):
    skill_manifest = _json(SPEC)
    if fixed_time_reference is not None:
        for rule in skill_manifest["verification_contract"]["rules"]:
            if rule["kind"] == "EVENT_TIME_WINDOW":
                rule["parameters"]["reference"] = {
                    "source": "SKILL_FIXED",
                    "value": fixed_time_reference,
                }
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    (skill_root / "diagnosis-skill.json").write_bytes(
        canonical_json_bytes(skill_manifest)
    )

    job_value = _json(CONTRACTS / "job-diagnose.json")
    names_and_values = [
        ("caller_service", "checkout"),
        ("server_service", "inventory"),
        ("rpc_method", rpc_method_fact),
        ("problem_time", problem_time),
    ]
    if include_order_fact:
        names_and_values.append(("order_id", client_order_id))
    fact_ids: dict[str, str] = {}
    user_facts = []
    for index, (name, value) in enumerate(names_and_values, start=80):
        fact_id = f"00000000-0000-0000-0000-{index:012d}"
        fact_ids[name] = fact_id
        user_facts.append(
            {
                "item_id": fact_id,
                "statement": value,
                "status": "ACTIVE",
                "provenance": {
                    "source_type": "USER_INPUT",
                    "source_ref": "00000000-0000-0000-0000-000000000001",
                    "input_name": name,
                },
                "evidence_refs": [],
                "created_revision": 2,
                "supersedes": [],
            }
        )
    evidence_ids = [CLIENT_EVIDENCE, SERVER_EVIDENCE]
    job_value["evidence_refs"] = evidence_ids
    job_value["attachment_refs"] = []
    job_value["previous_outcome_refs"] = []
    snapshot = job_value["context_snapshot"]
    snapshot["evidence_refs"] = evidence_ids
    snapshot["user_facts"] = user_facts
    job = Job.model_validate(job_value)

    old_manifest = _json(CONTRACTS / "workspace-input-manifest.json")
    artifact = next(
        item for item in old_manifest["entries"] if item["input_kind"] == "ARTIFACT"
    )
    entries = [
        {
            "input_kind": "EVIDENCE",
            "resource_id": CLIENT_EVIDENCE,
            "relative_path": None,
            "resource_kind": None,
            "size": None,
            "sha256": None,
            "source_type": "LOGPARSE",
            "source_ref": ARTIFACT_ID,
            "locator": {
                "kind": "LOGPARSE",
                "relative_path": "client.log",
                "start_line": None if null_client_locator else 1,
                "end_line": (
                    None
                    if null_client_locator
                    else 2 if hidden_duplicate_client_event else 1
                ),
                "start_time": None,
                "end_time": None,
            },
            "summary": "Cited client event.",
            "content_hash": None,
        },
        {
            "input_kind": "EVIDENCE",
            "resource_id": SERVER_EVIDENCE,
            "relative_path": None,
            "resource_kind": None,
            "size": None,
            "sha256": None,
            "source_type": "LOGPARSE",
            "source_ref": ARTIFACT_ID,
            "locator": {
                "kind": "LOGPARSE",
                "relative_path": "server.log",
                "start_line": 1,
                "end_line": 2,
                "start_time": None,
                "end_time": None,
            },
            "summary": "Cited server events.",
            "content_hash": None,
        },
        artifact,
    ]
    manifest = WorkspaceInputManifest.model_validate(
        {
            "schema_version": 2,
            "job_id": job.job_id,
            "case_id": job.case_id,
            "job_type": "DIAGNOSE",
            "logparse_tool_ref": job.logparse_tool_ref.model_dump(mode="json"),
            "logparse_product": job.logparse_product,
            "entries": entries,
            "resolved_logparse_plan": {
                "schema_version": 2,
                "attachment_id": None,
                "artifact_id": ARTIFACT_ID,
                "problem_time": problem_time,
                "anchors": [
                    {
                        "label": "client",
                        "module": "compact",
                        "slot": "slot_1",
                        "process_name": "checkout-client",
                        "pid": None,
                    },
                    {
                        "label": "server",
                        "module": "compact",
                        "slot": "slot_2",
                        "process_name": "inventory-server",
                        "pid": None,
                    },
                ],
            },
            "review_subject": None,
        }
    )
    artifact_root = tmp_path / artifact["relative_path"]
    artifact_root.mkdir(parents=True)
    client_line = (
        f"[0001] [diagnostic|slot_1/debug_20260103.log] {client_time} "
        "COMPACT checkout "
        "proc=checkout-client-101 slot 1 cpu 0 |No[1] rpc deadline "
        "exceeded after 1000ms server=inventory "
        f"method={client_rpc_method} order_id={client_order_id}\n"
    )
    duplicate_client_line = client_line.replace("[0001]", "[0002]", 1)
    (artifact_root / "client.log").write_text(
        client_line + (duplicate_client_line if hidden_duplicate_client_event else ""),
        encoding="utf-8",
    )
    (artifact_root / "server.log").write_text(
        f"[0001] [diagnostic|slot_2/debug_20260103.log] {takeover_time} "
        "COMPACT inventory "
        "proc=inventory-server-202 slot 2 cpu 0 |No[2] service takeover "
        f"active; rpc request accepted method={server_rpc_method} "
        f"order_id={server_order_id}\n"
        f"[0002] [diagnostic|slot_2/debug_20260103.log] {pool_wait_time} "
        "COMPACT inventory "
        "proc=inventory-server-202 slot 2 cpu 0 |No[3] connection pool "
        f"wait 1500ms complete order_id={server_order_id}\n",
        encoding="utf-8",
    )

    citations = [
        {
            "evidence_binding": {
                "existing_evidence_id": CLIENT_EVIDENCE,
                "evidence_proposal_key": None,
            },
            "line_start": 1,
            "line_end": 1,
        },
        {
            "evidence_binding": {
                "existing_evidence_id": SERVER_EVIDENCE,
                "evidence_proposal_key": None,
            },
            "line_start": 1,
            "line_end": 2,
        },
    ]
    claims = []
    for rule in skill_manifest["verification_contract"]["rules"]:
        parameters = rule["parameters"]
        fact_refs = []
        if rule["kind"] == "EVENT_TIME_WINDOW":
            reference = parameters["reference"]
            if reference["source"] == "USER_FACT":
                fact_refs = [fact_ids[reference["name"]]]
        elif rule["kind"] == "FACT_FIELD_EQUALS":
            fact_id = fact_ids.get(parameters["fact_name"])
            if fact_id is not None:
                fact_refs = [fact_id]
        claims.append(
            {
                "rule_id": rule["id"],
                "claimed_result": "PASS",
                "fact_refs": fact_refs,
                "citations": citations,
                "explanation": "The cited raw events satisfy the rule.",
            }
        )

    draft_value = _json(CONTRACTS / "agent-job-outcome-diagnosis.json")
    draft_value.pop("outcome_id")
    draft_value.pop("produced_at")
    draft_value.pop("decision_audit", None)
    draft_value["schema_version"] = 2
    draft_value["rule_claims"] = claims
    draft_value["consumed_evidence_refs"] = evidence_ids
    draft_value["proposed_artifact_drafts"] = []
    candidate = draft_value["payload"]["candidate_conclusion_draft"]
    client_binding = {
        "existing_evidence_id": CLIENT_EVIDENCE,
        "evidence_proposal_key": None,
    }
    server_binding = {
        "existing_evidence_id": SERVER_EVIDENCE,
        "evidence_proposal_key": None,
    }
    candidate["supporting_evidence_bindings"] = [client_binding]
    candidate["completion_criteria_mapping"][0]["evidence_bindings"] = [
        server_binding
    ]
    draft = AgentJobOutcomeDraftV2.model_validate(draft_value)
    draft_bytes = canonical_json_bytes(draft)
    verification = verify_agent_draft(
        workspace_root=tmp_path,
        job=job,
        manifest=manifest,
        draft=draft,
        draft_bytes=draft_bytes,
        proposal_resources=(),
        skill_root=skill_root,
        broker_audit_bytes=_broker_audit(manifest),
        diagnosis_audit=None,
    )
    return job, manifest, draft, draft_bytes, verification


def _captured_result_targets(
    tmp_path: Path,
    *,
    problem_time: str,
) -> tuple[AuthoritativeTargetSet, tuple[CapturedTargetLog, ...]]:
    source_root = f"inputs/artifacts/{ARTIFACT_ID}/tree"

    def target(
        *,
        ordinal: int,
        label: str,
        slot: str,
        process_name: str,
        pid: str,
        log_path: str,
    ) -> AuthoritativeTargetLog:
        value = AuthoritativeTargetLog(
            ordinal=ordinal,
            label=label,
            requested_module="compact",
            requested_slot=f"slot_{slot}",
            requested_process_name=process_name,
            requested_pid=None,
            module_key="ctrl",
            module_name="COMPACT",
            slot=slot,
            process_name=process_name,
            pid=pid,
            match_status="exact",
            board_cycle=None,
            cpu_id=None,
            cpu_cycle=None,
            caveats=(),
            source_kind="INPUT_ARTIFACT",
            source_ref=ARTIFACT_ID,
            source_root=source_root,
            log_path=log_path,
            archive_name=None,
        )
        return replace(value, archive_name=semantic_archive_name(value))

    client = target(
        ordinal=1,
        label="client",
        slot="1",
        process_name="checkout-client",
        pid="101",
        log_path="client.log",
    )
    server = target(
        ordinal=2,
        label="server",
        slot="2",
        process_name="inventory-server",
        pid="202",
        log_path="server.log",
    )
    targets = AuthoritativeTargetSet(
        problem_time=problem_time,
        targets=(client, server),
        source_size=None,
        source_sha256=None,
    )
    captured = tuple(
        CapturedTargetLog(
            target=item,
            content=(tmp_path / source_root / item.log_path).read_bytes(),
            evidence_bindings=(
                EvidenceBinding(
                    existing_evidence_id=(
                        CLIENT_EVIDENCE if item.label == "client" else SERVER_EVIDENCE
                    ),
                    evidence_proposal_key=None,
                ),
            ),
        )
        for item in targets.targets
        if item.log_path is not None
    )
    return targets, captured


def _review_inputs(
    *,
    diagnosis_job: Job,
    diagnosis_manifest: WorkspaceInputManifest,
    diagnosis_draft: AgentJobOutcomeDraftV2,
    diagnosis_audit: DecisionAuditV2,
) -> tuple[Job, WorkspaceInputManifest, AgentJobOutcomeDraftV2]:
    assert diagnosis_job.skill_ref is not None
    assert diagnosis_draft.payload is not None
    candidate_draft = diagnosis_draft.payload.candidate_conclusion_draft
    assert candidate_draft is not None
    criterion = diagnosis_job.context_snapshot.problem_spec.completion_criteria[0]
    completion_mapping = [
        {
            "criterion_index": 0,
            "criterion": criterion,
            "satisfied": True,
            "evidence_refs": [SERVER_EVIDENCE],
            "explanation": "The server event completes the fixed criterion.",
        }
    ]
    candidate_preimage = {
        "statement": candidate_draft.statement,
        "supporting_evidence_refs": [CLIENT_EVIDENCE],
        "completion_criteria_mapping": completion_mapping,
    }
    candidate = {
        "conclusion_id": "00000000-0000-0000-0000-000000000080",
        "revision": 1,
        "content_hash": hashlib.sha256(
            canonical_json_bytes(candidate_preimage)
        ).hexdigest(),
        **candidate_preimage,
        "proposed_by_job_id": diagnosis_job.job_id,
        "status": "REVIEWING",
    }

    job_value = _json(CONTRACTS / "job-review.json")
    job_value["case_id"] = diagnosis_job.case_id
    job_value["base_state_revision"] = 3
    job_value["evidence_refs"] = [CLIENT_EVIDENCE, SERVER_EVIDENCE]
    job_value["artifact_refs"] = [ARTIFACT_ID]
    job_value["skill_ref"] = diagnosis_job.skill_ref.model_dump(mode="json")
    job_value["review_target"] = {
        "candidate_conclusion_id": candidate["conclusion_id"],
        "candidate_revision": candidate["revision"],
        "candidate_content_hash": candidate["content_hash"],
    }
    snapshot = job_value["context_snapshot"]
    snapshot["diagnosis_state_revision"] = 3
    snapshot["problem_spec"] = diagnosis_job.context_snapshot.problem_spec.model_dump(
        mode="json"
    )
    snapshot["user_facts"] = [
        item.model_dump(mode="json")
        for item in diagnosis_job.context_snapshot.user_facts
    ]
    snapshot["evidence_refs"] = [CLIENT_EVIDENCE, SERVER_EVIDENCE]
    snapshot["candidate_conclusion"] = candidate
    review_job = Job.model_validate(job_value)

    rules = _json(SPEC)["verification_contract"]["rules"]
    rule_ids = [item["id"] for item in rules]
    semantic_ids = [
        item["id"] for item in rules if item["kind"] == "SEMANTIC_CAUSALITY"
    ]
    diagnosis_rule_by_id = {
        item.rule_id: item for item in diagnosis_audit.rules
    }
    mapped_required = [
        binding.existing_evidence_id
        for binding in diagnosis_audit.required_evidence_bindings
        if binding.existing_evidence_id is not None
    ]
    integrity_passed = (
        diagnosis_audit.required_rule_ids == rule_ids
        and [item.rule_id for item in diagnosis_audit.rules] == rule_ids
        and {CLIENT_EVIDENCE, SERVER_EVIDENCE} <= set(mapped_required)
    )
    mechanical_facts = [
        {
            "fact_id": "server_diagnosis_audit_integrity",
            "name": "diagnosis_audit_integrity",
            "value": "VERIFIED_PASS" if integrity_passed else "VERIFIED_FAIL",
            "source_rule_id": rule_ids[0],
            "evidence_refs": mapped_required,
        }
    ]
    for rule in rules:
        if rule["id"] in semantic_ids:
            continue
        inherited = diagnosis_rule_by_id.get(rule["id"])
        mechanical_facts.append(
            {
                "fact_id": rule["id"],
                "name": rule["id"],
                "value": (
                    "UNVERIFIABLE"
                    if inherited is None
                    else inherited.server_evaluation.status.value
                ),
                "source_rule_id": rule["id"],
                "evidence_refs": (
                    []
                    if inherited is None
                    else [
                        binding.existing_evidence_id
                        for binding in inherited.server_evaluation.evidence_bindings
                        if binding.existing_evidence_id is not None
                    ]
                ),
            }
        )
    subject_preimage = {
        "schema_version": 2,
        "review_job_id": review_job.job_id,
        "case_id": review_job.case_id,
        "reviewed_state_revision": review_job.base_state_revision,
        "skill_ref": review_job.skill_ref.model_dump(mode="json"),
        "candidate": candidate,
        "causal_assertions": [
            {
                "rule_id": rule["id"],
                "statement": rule["parameters"]["assertion"],
            }
            for rule in rules
            if rule["id"] in semantic_ids
        ],
        "required_rule_ids": rule_ids,
        "required_evidence_refs": [CLIENT_EVIDENCE, SERVER_EVIDENCE],
        "mechanical_facts": mechanical_facts,
    }
    review_subject = ReviewSubjectV2.model_validate(
        {
            **subject_preimage,
            "subject_hash": hashlib.sha256(
                canonical_json_bytes(subject_preimage)
            ).hexdigest(),
        }
    )
    manifest = WorkspaceInputManifest(
        schema_version=2,
        job_id=review_job.job_id,
        case_id=review_job.case_id,
        job_type=review_job.job_type,
        logparse_tool_ref=None,
        logparse_product=None,
        entries=list(diagnosis_manifest.entries),
        resolved_logparse_plan=None,
        review_subject=review_subject,
    )

    draft = AgentJobOutcomeDraftV2.model_validate(
        {
            "schema_version": 2,
            "job_id": review_job.job_id,
            "case_id": review_job.case_id,
            "job_type": "REVIEW",
            "base_state_revision": review_job.base_state_revision,
            "result_type": "COMPLETED",
            "payload": {
                "reviewed_state_revision": review_job.base_state_revision,
                "candidate_conclusion_id": candidate["conclusion_id"],
                "candidate_revision": candidate["revision"],
                "candidate_content_hash": candidate["content_hash"],
                "verdict": "PASS",
                "reviewed_evidence_refs": [CLIENT_EVIDENCE, SERVER_EVIDENCE],
                "unsupported_findings": [],
                "evidence_conflicts": [],
                "missing_evidence": [],
                "stale_references": [],
                "requested_requirement_ids": [],
                "recommendation": "Accept the candidate.",
            },
            "consumed_evidence_refs": [CLIENT_EVIDENCE, SERVER_EVIDENCE],
            "proposed_evidence_drafts": [],
            "proposed_artifact_drafts": [],
            "error": None,
            "rule_claims": [
                item.model_dump(mode="json")
                for item in diagnosis_draft.rule_claims
            ],
        }
    )
    return review_job, manifest, draft


def _need_order_id_draft(
    job: Job,
    source: AgentJobOutcomeDraftV2,
    *,
    name: str = "order_id",
    maximum_bytes: int = 256,
    supplement_policy: str = "MISSING_ONLY",
) -> AgentJobOutcomeDraftV2:
    pinned = next(
        item
        for item in _json(SPEC)["requirements"]
        if item["name"] == "order_id"
    )
    requirement_id = "00000000-0000-0000-0000-000000000096"
    requirement = {
        "requirement_id": requirement_id,
        "kind": pinned["kind"],
        "name": name,
        "prompt": pinned["prompt"],
        "required": True,
        "constraints": {
            **pinned["constraints"],
            "max_utf8_bytes": maximum_bytes,
        },
        "status": "OPEN",
        "requested_by_job_id": job.job_id,
        "fulfilled_by_refs": [],
        "supplement_policy": supplement_policy,
    }
    value = source.model_dump(mode="json")
    value["result_type"] = "NEED_INPUT"
    value["payload"]["candidate_conclusion_draft"] = None
    value["payload"]["requested_input"] = [requirement_id]
    value["payload"]["requested_attachments"] = []
    value["payload"]["state_delta"]["add_pending_requirements"] = [requirement]
    value["proposed_artifact_drafts"] = []
    return AgentJobOutcomeDraftV2.model_validate(value)


def _verify_review(
    tmp_path: Path,
    *,
    job: Job,
    manifest: WorkspaceInputManifest,
    draft: AgentJobOutcomeDraftV2,
    diagnosis_audit: DecisionAuditV2,
):
    return verify_agent_draft(
        workspace_root=tmp_path,
        job=job,
        manifest=manifest,
        draft=draft,
        draft_bytes=canonical_json_bytes(draft),
        proposal_resources=(),
        skill_root=tmp_path / "skill",
        broker_audit_bytes=None,
        diagnosis_audit=diagnosis_audit,
    )


def _add_open_review_requirement(
    tmp_path: Path,
    review_job: Job,
) -> tuple[Job, str]:
    requirement_id = "00000000-0000-0000-0000-000000000098"
    pinned = {
        "name": "diagnostic_note",
        "kind": "INPUT",
        "stage": "INITIAL",
        "fulfillment_source": "USER_FACT",
        "prompt": "Provide the missing diagnostic note.",
        "constraints": {
            "value_type": "STRING",
            "min_utf8_bytes": 1,
            "max_utf8_bytes": 256,
            "pattern": None,
            "allowed_values": [],
        },
        "supplement_policy": "MISSING_ONLY",
    }
    skill_path = tmp_path / "skill/diagnosis-skill.json"
    skill = _json(skill_path)
    skill["requirements"].append(pinned)
    skill_path.write_bytes(canonical_json_bytes(skill))

    job_value = review_job.model_dump(mode="json")
    job_value["context_snapshot"]["pending_requirements"].append(
        {
            "requirement_id": requirement_id,
            "kind": pinned["kind"],
            "name": pinned["name"],
            "prompt": pinned["prompt"],
            "required": True,
            "constraints": pinned["constraints"],
            "status": "OPEN",
            "requested_by_job_id": review_job.job_id,
            "fulfilled_by_refs": [],
            "supplement_policy": pinned["supplement_policy"],
        }
    )
    return Job.model_validate(job_value), requirement_id


def test_server_recomputes_all_rules_and_emits_only_cited_raw_lines(
    tmp_path: Path,
) -> None:
    _, _, _, _, verification = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )

    assert verification.positive_gate_passed is True
    assert all(
        item.server_evaluation.status
        in {ServerRuleStatus.VERIFIED_PASS, ServerRuleStatus.SEMANTIC_ONLY}
        for item in verification.audit.rules
    )
    evidence_records = [
        json.loads(line)
        for line in verification.decision_evidence_bytes.splitlines()
    ]
    assert len(evidence_records) == 3
    assert {item["anchor"] for item in evidence_records} == {"client", "server"}
    assert all("raw_line_sha256" in item for item in evidence_records)
    client_record = next(
        item for item in evidence_records if item["anchor"] == "client"
    )
    expected_physical_line = next(tmp_path.rglob("client.log")).read_bytes().splitlines(
        keepends=True
    )[0]
    assert client_record["raw_line_sha256"] == hashlib.sha256(
        expected_physical_line
    ).hexdigest()
    assert client_record["raw_line_sha256"] != hashlib.sha256(
        expected_physical_line.rstrip(b"\r\n")
    ).hexdigest()
    assert not client_record["raw_line"].endswith("\n")


def test_logparse_evidence_cannot_switch_to_same_path_in_another_run(
    tmp_path: Path,
) -> None:
    job, manifest, draft, _, _ = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    value = manifest.model_dump(mode="json")
    selected_artifact = next(
        item for item in value["entries"] if item["input_kind"] == "ARTIFACT"
    )
    other_artifact = dict(selected_artifact)
    other_artifact["resource_id"] = OTHER_ARTIFACT_ID
    other_artifact["relative_path"] = (
        f"inputs/artifacts/{OTHER_ARTIFACT_ID}/tree"
    )
    shutil.copytree(
        tmp_path / selected_artifact["relative_path"],
        tmp_path / other_artifact["relative_path"],
    )
    for entry in value["entries"]:
        if entry["input_kind"] == "EVIDENCE":
            entry["source_ref"] = OTHER_ARTIFACT_ID
    value["entries"].append(other_artifact)
    switched_manifest = WorkspaceInputManifest.model_validate(value)

    job_value = job.model_dump(mode="json")
    job_value["artifact_refs"] = [ARTIFACT_ID, OTHER_ARTIFACT_ID]
    switched_job = Job.model_validate(job_value)
    validate_workspace_manifest_for_job(switched_manifest, switched_job)

    assert switched_manifest.resolved_logparse_plan is not None
    assert switched_manifest.resolved_logparse_plan.artifact_id == ARTIFACT_ID
    with pytest.raises(ValueError, match="source differs from the resolved plan"):
        verify_agent_draft(
            workspace_root=tmp_path,
            job=switched_job,
            manifest=switched_manifest,
            draft=draft,
            draft_bytes=canonical_json_bytes(draft),
            proposal_resources=(),
            skill_root=tmp_path / "skill",
            broker_audit_bytes=_broker_audit(switched_manifest),
            diagnosis_audit=None,
        )


def test_initial_parse_evidence_is_locked_to_broker_artifact_proposal(
    tmp_path: Path,
) -> None:
    job, manifest, draft, _, _ = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    attachment_id = "00000000-0000-0000-0000-000000000050"
    artifact_key = "parsed_run"
    client_key = "parsed_client"
    server_key = "parsed_server"
    fixture_manifest = _json(CONTRACTS / "workspace-input-manifest.json")
    attachment = next(
        item
        for item in fixture_manifest["entries"]
        if item["input_kind"] == "ATTACHMENT"
    )
    selected_artifact = next(
        item
        for item in manifest.model_dump(mode="json")["entries"]
        if item["input_kind"] == "ARTIFACT"
    )

    job_value = job.model_dump(mode="json")
    job_value["attachment_refs"] = [attachment_id]
    job_value["artifact_refs"] = []
    job_value["evidence_refs"] = []
    job_value["context_snapshot"]["evidence_refs"] = []
    initial_job = Job.model_validate(job_value)

    manifest_value = manifest.model_dump(mode="json")
    manifest_value["entries"] = [attachment]
    manifest_value["resolved_logparse_plan"].update(
        {"attachment_id": attachment_id, "artifact_id": None}
    )
    initial_manifest = WorkspaceInputManifest.model_validate(manifest_value)
    validate_workspace_manifest_for_job(initial_manifest, initial_job)

    draft_value = draft.model_dump(mode="json")
    draft_value["consumed_evidence_refs"] = []
    logparse_artifact = {
        "proposal_key": artifact_key,
        "artifact_kind": "LOGPARSE_RUN",
        "name": artifact_key,
        "content_type": "application/vnd.problem-locator.logparse-run+directory",
        "resource_kind": "DIRECTORY",
        "workspace_relative_path": f"output/proposals/{artifact_key}/tree",
        "declared_size": None,
        "declared_sha256": None,
        "metadata": selected_artifact["metadata"],
    }
    client_binding = {
        "existing_evidence_id": None,
        "evidence_proposal_key": client_key,
    }
    server_binding = {
        "existing_evidence_id": None,
        "evidence_proposal_key": server_key,
    }

    def evidence_draft(
        proposal_key: str,
        relative_path: str,
        start_line: int,
        end_line: int,
    ) -> dict[str, object]:
        return {
            "proposal_key": proposal_key,
            "source_type": "LOGPARSE",
            "source_binding": {
                "existing_source_ref": None,
                "artifact_proposal_key": artifact_key,
            },
            "locator": {
                "kind": "LOGPARSE",
                "relative_path": relative_path,
                "start_line": start_line,
                "end_line": end_line,
                "start_time": None,
                "end_time": None,
            },
            "summary": "Broker-selected raw log range.",
            "workspace_relative_path": None,
            "declared_size": None,
            "declared_sha256": None,
        }

    draft_value["proposed_evidence_drafts"] = [
        evidence_draft(client_key, "client.log", 1, 1),
        evidence_draft(server_key, "server.log", 1, 2),
    ]
    draft_value["proposed_artifact_drafts"].insert(0, logparse_artifact)
    candidate = draft_value["payload"]["candidate_conclusion_draft"]
    candidate["supporting_evidence_bindings"] = [client_binding]
    candidate["completion_criteria_mapping"][0]["evidence_bindings"] = [
        server_binding
    ]
    citations = [
        {
            "evidence_binding": client_binding,
            "line_start": 1,
            "line_end": 1,
        },
        {
            "evidence_binding": server_binding,
            "line_start": 1,
            "line_end": 2,
        },
    ]
    for claim in draft_value["rule_claims"]:
        claim["citations"] = citations
    initial_draft = AgentJobOutcomeDraftV2.model_validate(draft_value)
    artifact_draft = next(
        item
        for item in initial_draft.proposed_artifact_drafts
        if item.proposal_key == artifact_key
    )
    plan = initial_manifest.resolved_logparse_plan
    assert plan is not None
    request = {
        "schema_version": 1,
        "problem_time": plan.problem_time,
        "anchors": [item.model_dump(mode="json") for item in plan.anchors],
        "attachment_id": attachment_id,
        "artifact_proposal_key": artifact_key,
    }
    result = {
        "schema_version": 1,
        "api_version": 1,
        "logparse_run_artifact_draft": artifact_draft.model_dump(mode="json"),
        "target_logs": [
            {
                "label": "client",
                "module": "compact",
                "module_key": "compact",
                "module_name": "compact",
                "slot": "slot_1",
                "process_name": "checkout-client",
                "match_status": "exact",
                "caveats": [],
                "log_path": "client.log",
            },
            {
                "label": "server",
                "module": "compact",
                "module_key": "compact",
                "module_name": "compact",
                "slot": "slot_2",
                "process_name": "inventory-server",
                "match_status": "exact",
                "caveats": [],
                "log_path": "server.log",
            },
        ],
    }
    broker_audit = canonical_json_bytes(
        {
            "schema_version": 1,
            "job_id": initial_job.job_id,
            "operations": [
                {
                    "operation": "parse-targets",
                    "request_sha256": hashlib.sha256(
                        canonical_json_bytes(request)
                    ).hexdigest(),
                    "request": request,
                    "http_status": 200,
                    "result_sha256": hashlib.sha256(
                        canonical_json_bytes(result)
                    ).hexdigest(),
                    "result": result,
                }
            ],
        }
    )
    resource = SimpleNamespace(
        proposal_key=artifact_key,
        path=tmp_path / selected_artifact["relative_path"],
        draft=artifact_draft,
    )
    verified = verify_agent_draft(
        workspace_root=tmp_path,
        job=initial_job,
        manifest=initial_manifest,
        draft=initial_draft,
        draft_bytes=canonical_json_bytes(initial_draft),
        proposal_resources=(resource,),
        skill_root=tmp_path / "skill",
        broker_audit_bytes=broker_audit,
        diagnosis_audit=None,
    )
    assert verified.positive_gate_passed is True

    wrong_value = initial_draft.model_dump(mode="json")
    wrong_key = "agent_selected_run"
    for item in wrong_value["proposed_artifact_drafts"]:
        if item["artifact_kind"] == "LOGPARSE_RUN":
            item["proposal_key"] = wrong_key
            item["name"] = wrong_key
            item["workspace_relative_path"] = (
                f"output/proposals/{wrong_key}/tree"
            )
    for item in wrong_value["proposed_evidence_drafts"]:
        item["source_binding"]["artifact_proposal_key"] = wrong_key
    wrong_draft = AgentJobOutcomeDraftV2.model_validate(wrong_value)
    wrong_artifact = next(
        item
        for item in wrong_draft.proposed_artifact_drafts
        if item.proposal_key == wrong_key
    )
    with pytest.raises(ValueError, match="source differs from the resolved plan"):
        verify_agent_draft(
            workspace_root=tmp_path,
            job=initial_job,
            manifest=initial_manifest,
            draft=wrong_draft,
            draft_bytes=canonical_json_bytes(wrong_draft),
            proposal_resources=(
                SimpleNamespace(
                    proposal_key=wrong_key,
                    path=tmp_path / selected_artifact["relative_path"],
                    draft=wrong_artifact,
                ),
            ),
            skill_root=tmp_path / "skill",
            broker_audit_bytes=broker_audit,
            diagnosis_audit=None,
        )


def test_diagnosis_candidate_must_close_over_every_audited_evidence_binding(
    tmp_path: Path,
) -> None:
    job, manifest, draft, _, original = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    assert original.positive_gate_passed is True

    value = draft.model_dump(mode="json")
    candidate = value["payload"]["candidate_conclusion_draft"]
    candidate["completion_criteria_mapping"][0]["evidence_bindings"] = [
        {
            "existing_evidence_id": CLIENT_EVIDENCE,
            "evidence_proposal_key": None,
        }
    ]
    incomplete = AgentJobOutcomeDraftV2.model_validate(value)
    verification = verify_agent_draft(
        workspace_root=tmp_path,
        job=job,
        manifest=manifest,
        draft=incomplete,
        draft_bytes=canonical_json_bytes(incomplete),
        proposal_resources=(),
        skill_root=tmp_path / "skill",
        broker_audit_bytes=_broker_audit(manifest),
        diagnosis_audit=None,
    )

    assert verification.positive_gate_passed is False
    assert [
        item.existing_evidence_id
        for item in verification.audit.required_evidence_bindings
    ] == [CLIENT_EVIDENCE, SERVER_EVIDENCE]


def test_diagnosis_candidate_evidence_coverage_is_order_independent(
    tmp_path: Path,
) -> None:
    job, manifest, draft, _, _ = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    value = draft.model_dump(mode="json")
    client_binding = {
        "existing_evidence_id": CLIENT_EVIDENCE,
        "evidence_proposal_key": None,
    }
    server_binding = {
        "existing_evidence_id": SERVER_EVIDENCE,
        "evidence_proposal_key": None,
    }
    candidate = value["payload"]["candidate_conclusion_draft"]
    candidate["supporting_evidence_bindings"] = [server_binding, client_binding]
    candidate["completion_criteria_mapping"][0]["evidence_bindings"] = [
        client_binding,
        server_binding,
    ]
    reordered = AgentJobOutcomeDraftV2.model_validate(value)

    verification = verify_agent_draft(
        workspace_root=tmp_path,
        job=job,
        manifest=manifest,
        draft=reordered,
        draft_bytes=canonical_json_bytes(reordered),
        proposal_resources=(),
        skill_root=tmp_path / "skill",
        broker_audit_bytes=_broker_audit(manifest),
        diagnosis_audit=None,
    )

    assert verification.positive_gate_passed is True
    assert {
        item.existing_evidence_id
        for item in verification.audit.required_evidence_bindings
    } == {CLIENT_EVIDENCE, SERVER_EVIDENCE}


def test_no_log_semantic_skill_preserves_candidate_evidence_through_review(
    tmp_path: Path,
) -> None:
    original_job, _, original_draft, _, _ = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    manual_skill = _json(MANUAL_SPEC)
    manual_skill_bytes = canonical_json_bytes(manual_skill)
    (tmp_path / "skill/diagnosis-skill.json").write_bytes(manual_skill_bytes)
    manual_ref = {
        "id": manual_skill["id"],
        "version": manual_skill["version"],
        "content_hash": hashlib.sha256(manual_skill_bytes).hexdigest(),
    }

    job_value = original_job.model_dump(mode="json")
    job_value.update(
        {
            "available_skill_refs": [],
            "skill_ref": manual_ref,
            "logparse_tool_ref": None,
            "logparse_product": None,
            "evidence_refs": [CLIENT_EVIDENCE],
            "attachment_refs": [],
            "artifact_refs": [],
        }
    )
    job_value["context_snapshot"]["evidence_refs"] = [CLIENT_EVIDENCE]
    diagnosis_job = Job.model_validate(job_value)
    source_fact = diagnosis_job.context_snapshot.user_facts[0]
    source_name = source_fact.provenance.input_name
    assert source_name is not None
    evidence_entry = {
        "input_kind": "EVIDENCE",
        "resource_id": CLIENT_EVIDENCE,
        "relative_path": None,
        "resource_kind": None,
        "size": None,
        "sha256": None,
        "source_type": "USER_FACT",
        "source_ref": source_fact.item_id,
        "locator": {"kind": "USER_FACT", "input_name": source_name},
        "summary": "Structured user-fact Evidence for manual triage.",
        "content_hash": None,
    }
    diagnosis_manifest = WorkspaceInputManifest.model_validate(
        {
            "schema_version": 2,
            "job_id": diagnosis_job.job_id,
            "case_id": diagnosis_job.case_id,
            "job_type": "DIAGNOSE",
            "logparse_tool_ref": None,
            "logparse_product": None,
            "entries": [evidence_entry],
            "resolved_logparse_plan": None,
            "review_subject": None,
        }
    )
    diagnosis_value = original_draft.model_dump(mode="json")
    diagnosis_value["consumed_evidence_refs"] = [CLIENT_EVIDENCE]
    diagnosis_value["rule_claims"] = [
        {
            "rule_id": "manual_causal_assessment",
            "claimed_result": "PASS",
            "fact_refs": [],
            "citations": [],
            "explanation": "The structured Evidence supports the manual conclusion.",
        }
    ]
    diagnosis_candidate = diagnosis_value["payload"]["candidate_conclusion_draft"]
    manual_binding = {
        "existing_evidence_id": CLIENT_EVIDENCE,
        "evidence_proposal_key": None,
    }
    diagnosis_candidate["supporting_evidence_bindings"] = [manual_binding]
    diagnosis_candidate["completion_criteria_mapping"][0]["evidence_bindings"] = [
        manual_binding
    ]
    diagnosis_draft = AgentJobOutcomeDraftV2.model_validate(diagnosis_value)
    diagnosis = verify_agent_draft(
        workspace_root=tmp_path,
        job=diagnosis_job,
        manifest=diagnosis_manifest,
        draft=diagnosis_draft,
        draft_bytes=canonical_json_bytes(diagnosis_draft),
        proposal_resources=(),
        skill_root=tmp_path / "skill",
        broker_audit_bytes=None,
        diagnosis_audit=None,
    )
    assert diagnosis.positive_gate_passed is True
    assert diagnosis.decision_evidence_bytes == b""
    assert [
        item.existing_evidence_id
        for item in diagnosis.audit.required_evidence_bindings
    ] == [CLIENT_EVIDENCE]

    (tmp_path / "runtime").mkdir()
    (tmp_path / "output").mkdir()
    finalized_diagnosis = finalize_server_outcome(
        workspace_root=tmp_path,
        job=diagnosis_job,
        manifest=diagnosis_manifest,
        draft=diagnosis_draft,
        draft_bytes=canonical_json_bytes(diagnosis_draft),
        outcome_id="00000000-0000-0000-0000-000000000094",
        produced_at="2026-08-08T00:00:00.000Z",
        verification=diagnosis,
        authoritative_targets=None,
        target_logs=(),
    )
    assert finalized_diagnosis.outcome.result_type is OutcomeResultType.COMPLETED
    assert finalized_diagnosis.user_result is not None
    generated_by_kind = {
        item.draft.artifact_kind.value: item
        for item in finalized_diagnosis.generated_result_files
    }
    assert set(generated_by_kind) == {"USER_RESULT", "USER_RESULT_ARCHIVE"}
    archive = generated_by_kind["USER_RESULT_ARCHIVE"]
    assert archive.draft.metadata.target_log_count == 0
    with zipfile.ZipFile(io.BytesIO(archive.content)) as result_zip:
        assert result_zip.namelist() == ["result.txt", "archive-manifest.json"]
        archive_manifest = json.loads(result_zip.read("archive-manifest.json"))
        assert archive_manifest["target_log_count"] == 0
        assert archive_manifest["target_logs"] == []

    criterion = diagnosis_candidate["completion_criteria_mapping"][0]
    candidate_preimage = {
        "statement": diagnosis_candidate["statement"],
        "supporting_evidence_refs": [CLIENT_EVIDENCE],
        "completion_criteria_mapping": [
            {
                "criterion_index": criterion["criterion_index"],
                "criterion": criterion["criterion"],
                "satisfied": True,
                "evidence_refs": [CLIENT_EVIDENCE],
                "explanation": criterion["explanation"],
            }
        ],
    }
    candidate = {
        "conclusion_id": "00000000-0000-0000-0000-000000000080",
        "revision": 1,
        "content_hash": hashlib.sha256(
            canonical_json_bytes(candidate_preimage)
        ).hexdigest(),
        **candidate_preimage,
        "proposed_by_job_id": diagnosis_job.job_id,
        "status": "REVIEWING",
    }
    review_value = _json(CONTRACTS / "job-review.json")
    review_value.update(
        {
            "case_id": diagnosis_job.case_id,
            "base_state_revision": 3,
            "evidence_refs": [CLIENT_EVIDENCE],
            "attachment_refs": [],
            "artifact_refs": [],
            "available_skill_refs": [],
            "skill_ref": manual_ref,
            "review_target": {
                "candidate_conclusion_id": candidate["conclusion_id"],
                "candidate_revision": candidate["revision"],
                "candidate_content_hash": candidate["content_hash"],
            },
        }
    )
    review_snapshot = review_value["context_snapshot"]
    review_snapshot.update(
        {
            "diagnosis_state_revision": 3,
            "problem_spec": diagnosis_job.context_snapshot.problem_spec.model_dump(
                mode="json"
            ),
            "user_facts": [
                item.model_dump(mode="json")
                for item in diagnosis_job.context_snapshot.user_facts
            ],
            "evidence_refs": [CLIENT_EVIDENCE],
            "candidate_conclusion": candidate,
        }
    )
    review_job = Job.model_validate(review_value)
    subject_preimage = {
        "schema_version": 2,
        "review_job_id": review_job.job_id,
        "case_id": review_job.case_id,
        "reviewed_state_revision": review_job.base_state_revision,
        "skill_ref": manual_ref,
        "candidate": candidate,
        "causal_assertions": [
            {
                "rule_id": "manual_causal_assessment",
                "statement": manual_skill["verification_contract"]["rules"][0][
                    "parameters"
                ]["assertion"],
            }
        ],
        "required_rule_ids": ["manual_causal_assessment"],
        "required_evidence_refs": [CLIENT_EVIDENCE],
        "mechanical_facts": [
            {
                "fact_id": "server_diagnosis_audit_integrity",
                "name": "diagnosis_audit_integrity",
                "value": "VERIFIED_PASS",
                "source_rule_id": "manual_causal_assessment",
                "evidence_refs": [CLIENT_EVIDENCE],
            }
        ],
    }
    review_subject = ReviewSubjectV2.model_validate(
        {
            **subject_preimage,
            "subject_hash": hashlib.sha256(
                canonical_json_bytes(subject_preimage)
            ).hexdigest(),
        }
    )
    review_manifest = WorkspaceInputManifest.model_validate(
        {
            "schema_version": 2,
            "job_id": review_job.job_id,
            "case_id": review_job.case_id,
            "job_type": "REVIEW",
            "logparse_tool_ref": None,
            "logparse_product": None,
            "entries": [evidence_entry],
            "resolved_logparse_plan": None,
            "review_subject": review_subject.model_dump(mode="json"),
        }
    )
    review_draft = AgentJobOutcomeDraftV2.model_validate(
        {
            "schema_version": 2,
            "job_id": review_job.job_id,
            "case_id": review_job.case_id,
            "job_type": "REVIEW",
            "base_state_revision": review_job.base_state_revision,
            "result_type": "COMPLETED",
            "payload": {
                "reviewed_state_revision": review_job.base_state_revision,
                "candidate_conclusion_id": candidate["conclusion_id"],
                "candidate_revision": candidate["revision"],
                "candidate_content_hash": candidate["content_hash"],
                "verdict": "PASS",
                "reviewed_evidence_refs": [CLIENT_EVIDENCE],
                "unsupported_findings": [],
                "evidence_conflicts": [],
                "missing_evidence": [],
                "stale_references": [],
                "requested_requirement_ids": [],
                "recommendation": "Accept the manual-triage Candidate.",
            },
            "consumed_evidence_refs": [CLIENT_EVIDENCE],
            "proposed_evidence_drafts": [],
            "proposed_artifact_drafts": [],
            "error": None,
            "rule_claims": [
                {
                    "rule_id": "manual_causal_assessment",
                    "claimed_result": "PASS",
                    "fact_refs": [],
                    "citations": [],
                    "explanation": "Independent semantic review supports it.",
                }
            ],
        }
    )
    review = _verify_review(
        tmp_path,
        job=review_job,
        manifest=review_manifest,
        draft=review_draft,
        diagnosis_audit=diagnosis.audit,
    )
    assert review.positive_gate_passed is True
    assert review.decision_evidence_bytes == b""
    assert [
        item.existing_evidence_id
        for item in review.audit.required_evidence_bindings
    ] == [CLIENT_EVIDENCE]


def test_hidden_duplicate_in_locator_cannot_bypass_exactly_one(
    tmp_path: Path,
) -> None:
    _, _, _, _, verification = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
        hidden_duplicate_client_event=True,
    )

    assert verification.positive_gate_passed is False
    presence = next(
        item
        for item in verification.audit.rules
        if item.rule_id == "client_timeout_present"
    )
    assert (
        presence.server_evaluation.status
        is ServerRuleStatus.VERIFIED_FAIL
    )
    assert len(presence.server_evaluation.line_ranges) == 2
    # The Agent cites only client line 1, but the immutable Evidence locator
    # contains a second matching physical line.  The server scan owns
    # cardinality and therefore cannot be narrowed by the citation.
    assert presence.agent_claim is not None
    client_citation = next(
        item
        for item in presence.agent_claim.citations
        if item.evidence_binding.existing_evidence_id == CLIENT_EVIDENCE
    )
    assert (client_citation.line_start, client_citation.line_end) == (1, 1)


def test_user_fact_field_mismatch_is_mechanically_rejected(
    tmp_path: Path,
) -> None:
    _, _, _, _, verification = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
        rpc_method_fact="Commit",
    )

    assert verification.positive_gate_passed is False
    method_rules = {
        item.rule_id: item.server_evaluation.status
        for item in verification.audit.rules
        if item.rule_id in {"client_method_matches", "server_method_matches"}
    }
    assert method_rules == {
        "client_method_matches": ServerRuleStatus.VERIFIED_FAIL,
        "server_method_matches": ServerRuleStatus.VERIFIED_FAIL,
    }


def test_cross_role_field_mismatch_is_reported_by_correlation_rule(
    tmp_path: Path,
) -> None:
    _, _, _, _, verification = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
        server_rpc_method="Cancel",
    )

    assert verification.positive_gate_passed is False
    correlation = next(
        item
        for item in verification.audit.rules
        if item.rule_id == "method_correlates_across_roles"
    )
    assert (
        correlation.server_evaluation.status
        is ServerRuleStatus.VERIFIED_FAIL
    )


def test_reversed_event_order_is_mechanically_rejected(
    tmp_path: Path,
) -> None:
    _, _, _, _, verification = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
        takeover_time="2026-01-03T00:00:02.500Z",
        pool_wait_time="2026-01-03T00:00:01.000Z",
    )

    assert verification.positive_gate_passed is False
    ordering = next(
        item
        for item in verification.audit.rules
        if item.rule_id == "takeover_precedes_pool_wait"
    )
    assert ordering.server_evaluation.status is ServerRuleStatus.VERIFIED_FAIL


def test_unbounded_locator_fails_closed_even_when_agent_cites_a_line(
    tmp_path: Path,
) -> None:
    _, _, _, _, verification = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
        null_client_locator=True,
    )

    assert verification.positive_gate_passed is False
    presence = next(
        item
        for item in verification.audit.rules
        if item.rule_id == "client_timeout_present"
    )
    assert presence.server_evaluation.status is ServerRuleStatus.UNVERIFIABLE
    assert presence.server_evaluation.line_ranges == []


def test_skill_fixed_time_reference_requires_no_user_fact_binding(
    tmp_path: Path,
) -> None:
    _, _, _, _, verification = _build(
        tmp_path,
        problem_time="2026-01-03T01:00:03.000Z",
        fixed_time_reference="2026-01-03T00:00:03.000Z",
    )

    assert verification.positive_gate_passed is True
    time_rules = [
        item.server_evaluation
        for item in verification.audit.rules
        if item.server_evaluation.rule_kind == "EVENT_TIME_WINDOW"
    ]
    assert time_rules
    assert all(item.status is ServerRuleStatus.VERIFIED_PASS for item in time_rules)
    assert all(item.fact_refs == [] for item in time_rules)
    assert all(
        item.derived_anchor_time == "2026-01-03T00:00:03.000Z"
        for item in time_rules
    )


def test_invalid_skill_fixed_time_fails_closed_without_crashing(
    tmp_path: Path,
) -> None:
    _, _, _, _, verification = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
        fixed_time_reference="2026-01-03T00:00:03Z",
    )

    assert verification.positive_gate_passed is False
    time_rules = [
        item.server_evaluation
        for item in verification.audit.rules
        if item.server_evaluation.rule_kind == "EVENT_TIME_WINDOW"
    ]
    assert time_rules
    assert all(item.status is ServerRuleStatus.VERIFIED_FAIL for item in time_rules)


@pytest.mark.parametrize(
    ("name", "maximum_bytes", "supplement_policy"),
    [
        ("invented_order", 256, "MISSING_ONLY"),
        ("order_id", 255, "MISSING_ONLY"),
        ("order_id", 256, "NONE"),
    ],
)
def test_agent_cannot_invent_or_mutate_pinned_missing_only_requirement(
    tmp_path: Path,
    name: str,
    maximum_bytes: int,
    supplement_policy: str,
) -> None:
    job, manifest, draft, _, _ = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
        include_order_fact=False,
    )
    wait_draft = _need_order_id_draft(
        job,
        draft,
        name=name,
        maximum_bytes=maximum_bytes,
        supplement_policy=supplement_policy,
    )

    with pytest.raises(ValueError):
        verify_agent_draft(
            workspace_root=tmp_path,
            job=job,
            manifest=manifest,
            draft=wait_draft,
            draft_bytes=canonical_json_bytes(wait_draft),
            proposal_resources=(),
            skill_root=tmp_path / "skill",
            broker_audit_bytes=_broker_audit(manifest),
            diagnosis_audit=None,
        )


def test_missing_only_requirement_cannot_request_an_existing_user_fact(
    tmp_path: Path,
) -> None:
    job, manifest, draft, _, _ = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    wait_draft = _need_order_id_draft(job, draft)

    with pytest.raises(ValueError, match="already fulfilled"):
        verify_agent_draft(
            workspace_root=tmp_path,
            job=job,
            manifest=manifest,
            draft=wait_draft,
            draft_bytes=canonical_json_bytes(wait_draft),
            proposal_resources=(),
            skill_root=tmp_path / "skill",
            broker_audit_bytes=_broker_audit(manifest),
            diagnosis_audit=None,
        )


def test_valid_missing_only_wait_is_preserved_when_fact_is_absent(
    tmp_path: Path,
) -> None:
    job, manifest, draft, _, _ = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
        include_order_fact=False,
    )
    wait_draft = _need_order_id_draft(job, draft)
    wait_bytes = canonical_json_bytes(wait_draft)
    verification = verify_agent_draft(
        workspace_root=tmp_path,
        job=job,
        manifest=manifest,
        draft=wait_draft,
        draft_bytes=wait_bytes,
        proposal_resources=(),
        skill_root=tmp_path / "skill",
        broker_audit_bytes=_broker_audit(manifest),
        diagnosis_audit=None,
    )
    assert verification.positive_gate_passed is False
    authoritative_targets, target_logs = _captured_result_targets(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    (tmp_path / "runtime").mkdir()
    (tmp_path / "output").mkdir()
    finalized = finalize_server_outcome(
        workspace_root=tmp_path,
        job=job,
        manifest=manifest,
        draft=wait_draft,
        draft_bytes=wait_bytes,
        outcome_id="00000000-0000-0000-0000-000000000095",
        produced_at="2026-08-08T00:00:00.000Z",
        verification=verification,
        authoritative_targets=authoritative_targets,
        target_logs=target_logs,
    )
    assert finalized.outcome.result_type is OutcomeResultType.NEED_INPUT
    assert finalized.outcome.payload.requested_input == [
        "00000000-0000-0000-0000-000000000096"
    ]
    assert finalized.marker.decision_evidence_sha256 == hashlib.sha256(
        verification.decision_evidence_bytes
    ).hexdigest()


@pytest.mark.parametrize("match_status", ["missing", "ambiguous"])
def test_unresolved_authoritative_target_overrides_need_input_with_json_only_result(
    tmp_path: Path,
    match_status: str,
) -> None:
    job, manifest, draft, _, _ = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
        include_order_fact=False,
    )
    wait_draft = _need_order_id_draft(job, draft)
    wait_bytes = canonical_json_bytes(wait_draft)
    verification = verify_agent_draft(
        workspace_root=tmp_path,
        job=job,
        manifest=manifest,
        draft=wait_draft,
        draft_bytes=wait_bytes,
        proposal_resources=(),
        skill_root=tmp_path / "skill",
        broker_audit_bytes=_broker_audit(
            manifest,
            first_match_status=match_status,
        ),
        diagnosis_audit=None,
    )
    authoritative_targets, target_logs = _captured_result_targets(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    unresolved = replace(
        authoritative_targets.targets[0],
        match_status=match_status,
        log_path=None,
        archive_name=None,
    )
    target_set = replace(
        authoritative_targets,
        targets=(unresolved, authoritative_targets.targets[1]),
    )
    remaining_logs = tuple(
        item for item in target_logs if item.target.ordinal != unresolved.ordinal
    )
    (tmp_path / "runtime").mkdir()
    (tmp_path / "output").mkdir()

    finalized = finalize_server_outcome(
        workspace_root=tmp_path,
        job=job,
        manifest=manifest,
        draft=wait_draft,
        draft_bytes=wait_bytes,
        outcome_id="00000000-0000-0000-0000-000000000098",
        produced_at="2026-08-08T00:00:00.000Z",
        verification=verification,
        authoritative_targets=target_set,
        target_logs=remaining_logs,
    )

    assert finalized.outcome.result_type is OutcomeResultType.INCONCLUSIVE
    assert finalized.user_result is not None
    assert finalized.user_result.status == "INCONCLUSIVE"
    assert len(finalized.generated_result_files) == 1
    assert finalized.generated_result_files[0].draft.artifact_kind.value == "USER_RESULT"
    assert {
        item.draft.artifact_kind.value for item in finalized.generated_result_files
    } == {"USER_RESULT"}


@pytest.mark.parametrize(
    "inherited_failure",
    ["mechanical_fail", "missing_rule", "missing_required_evidence"],
)
def test_review_pass_is_inconclusive_when_inherited_audit_is_not_positive(
    tmp_path: Path,
    inherited_failure: str,
) -> None:
    (
        diagnosis_job,
        diagnosis_manifest,
        diagnosis_draft,
        _,
        diagnosis_verification,
    ) = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    inherited_value = diagnosis_verification.audit.model_dump(mode="json")
    if inherited_failure == "mechanical_fail":
        evaluation = inherited_value["rules"][0]["server_evaluation"]
        evaluation["status"] = "VERIFIED_FAIL"
        evaluation["issues"] = ["Synthetic inherited mechanical failure."]
    elif inherited_failure == "missing_rule":
        del inherited_value["required_rule_ids"][0]
        del inherited_value["rules"][0]
    else:
        inherited_value["required_evidence_bindings"] = [
            {
                "existing_evidence_id": CLIENT_EVIDENCE,
                "evidence_proposal_key": None,
            }
        ]
    inherited_audit = DecisionAuditV2.model_validate(inherited_value)
    review_job, review_manifest, review_draft = _review_inputs(
        diagnosis_job=diagnosis_job,
        diagnosis_manifest=diagnosis_manifest,
        diagnosis_draft=diagnosis_draft,
        diagnosis_audit=inherited_audit,
    )
    review_draft_bytes = canonical_json_bytes(review_draft)

    verification = verify_agent_draft(
        workspace_root=tmp_path,
        job=review_job,
        manifest=review_manifest,
        draft=review_draft,
        draft_bytes=review_draft_bytes,
        proposal_resources=(),
        skill_root=tmp_path / "skill",
        broker_audit_bytes=None,
        diagnosis_audit=inherited_audit,
    )
    assert verification.positive_gate_passed is False
    (tmp_path / "runtime").mkdir()
    (tmp_path / "output").mkdir()
    finalized = finalize_server_outcome(
        workspace_root=tmp_path,
        job=review_job,
        manifest=review_manifest,
        draft=review_draft,
        draft_bytes=review_draft_bytes,
        outcome_id="00000000-0000-0000-0000-000000000097",
        produced_at="2026-08-08T00:00:00.000Z",
        verification=verification,
        authoritative_targets=None,
        target_logs=(),
    )
    assert finalized.outcome.result_type is OutcomeResultType.INCONCLUSIVE
    assert finalized.outcome.payload.verdict.value == "REJECT"


def test_review_pass_survives_when_current_and_inherited_claims_align(
    tmp_path: Path,
) -> None:
    diagnosis_job, diagnosis_manifest, diagnosis_draft, _, diagnosis = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    review_job, review_manifest, review_draft = _review_inputs(
        diagnosis_job=diagnosis_job,
        diagnosis_manifest=diagnosis_manifest,
        diagnosis_draft=diagnosis_draft,
        diagnosis_audit=diagnosis.audit,
    )

    verification = _verify_review(
        tmp_path,
        job=review_job,
        manifest=review_manifest,
        draft=review_draft,
        diagnosis_audit=diagnosis.audit,
    )

    assert verification.positive_gate_passed is True


@pytest.mark.parametrize(
    ("claim_mode", "expected_gate"),
    [
        ("semantic_fail", True),
        ("all_pass", False),
        ("fake_mechanical_fail", False),
    ],
)
def test_review_reject_requires_one_server_aligned_fail_claim(
    tmp_path: Path,
    claim_mode: str,
    expected_gate: bool,
) -> None:
    diagnosis_job, diagnosis_manifest, diagnosis_draft, _, diagnosis = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    review_job, review_manifest, review_draft = _review_inputs(
        diagnosis_job=diagnosis_job,
        diagnosis_manifest=diagnosis_manifest,
        diagnosis_draft=diagnosis_draft,
        diagnosis_audit=diagnosis.audit,
    )
    value = review_draft.model_dump(mode="json")
    value["payload"].update(
        {
            "verdict": "REJECT",
            "unsupported_findings": ["The claimed cause is not supported."],
            "recommendation": "Reject the Candidate.",
        }
    )
    if claim_mode == "semantic_fail":
        claim = next(
            item
            for item in value["rule_claims"]
            if item["rule_id"] == "takeover_pool_wait_caused_timeout"
        )
        claim["claimed_result"] = "FAIL"
    elif claim_mode == "fake_mechanical_fail":
        claim = next(
            item
            for item in value["rule_claims"]
            if item["rule_id"] == "client_timeout_present"
        )
        claim["claimed_result"] = "FAIL"
    rejected = AgentJobOutcomeDraftV2.model_validate(value)
    verification = _verify_review(
        tmp_path,
        job=review_job,
        manifest=review_manifest,
        draft=rejected,
        diagnosis_audit=diagnosis.audit,
    )
    assert verification.positive_gate_passed is expected_gate

    (tmp_path / "runtime").mkdir()
    (tmp_path / "output").mkdir()
    finalized = finalize_server_outcome(
        workspace_root=tmp_path,
        job=review_job,
        manifest=review_manifest,
        draft=rejected,
        draft_bytes=canonical_json_bytes(rejected),
        outcome_id="00000000-0000-0000-0000-000000000097",
        produced_at="2026-08-08T00:00:00.000Z",
        verification=verification,
        authoritative_targets=None,
        target_logs=(),
    )
    assert finalized.outcome.result_type is (
        OutcomeResultType.COMPLETED
        if expected_gate
        else OutcomeResultType.INCONCLUSIVE
    )
    assert finalized.outcome.payload.verdict.value == "REJECT"
    assert finalized.user_result is not None
    relevance = finalized.user_result.time_relevance
    derived_times = {
        item.server_evaluation.derived_anchor_time
        for item in verification.audit.rules
        if item.server_evaluation.derived_anchor_time is not None
    }
    assert len(derived_times) == 1
    assert relevance.problem_time == derived_times.pop()
    assert relevance.observations
    problem_time = datetime.strptime(
        relevance.problem_time,
        "%Y-%m-%dT%H:%M:%S.%fZ",
    )
    for observation in relevance.observations:
        event_time = datetime.strptime(
            observation.event_time,
            "%Y-%m-%dT%H:%M:%S.%fZ",
        )
        assert observation.offset_ms == int(
            (event_time - problem_time).total_seconds() * 1_000
        )


@pytest.mark.parametrize(
    ("semantic_result", "expected_gate"),
    [("UNKNOWN", True), ("PASS", False)],
)
def test_review_need_more_requires_unknown_and_one_legal_missing_requirement(
    tmp_path: Path,
    semantic_result: str,
    expected_gate: bool,
) -> None:
    diagnosis_job, diagnosis_manifest, diagnosis_draft, _, diagnosis = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    review_job, review_manifest, review_draft = _review_inputs(
        diagnosis_job=diagnosis_job,
        diagnosis_manifest=diagnosis_manifest,
        diagnosis_draft=diagnosis_draft,
        diagnosis_audit=diagnosis.audit,
    )
    review_job, requirement_id = _add_open_review_requirement(
        tmp_path,
        review_job,
    )
    value = review_draft.model_dump(mode="json")
    value["payload"].update(
        {
            "verdict": "NEED_MORE_EVIDENCE",
            "missing_evidence": ["A fixed diagnostic note is still missing."],
            "requested_requirement_ids": [requirement_id],
            "recommendation": "Collect the fixed missing input.",
        }
    )
    semantic = next(
        item
        for item in value["rule_claims"]
        if item["rule_id"] == "takeover_pool_wait_caused_timeout"
    )
    semantic["claimed_result"] = semantic_result
    waiting = AgentJobOutcomeDraftV2.model_validate(value)
    verification = _verify_review(
        tmp_path,
        job=review_job,
        manifest=review_manifest,
        draft=waiting,
        diagnosis_audit=diagnosis.audit,
    )
    assert verification.positive_gate_passed is expected_gate
    authoritative_targets, target_logs = _captured_result_targets(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )

    (tmp_path / "runtime").mkdir()
    (tmp_path / "output").mkdir()
    finalized = finalize_server_outcome(
        workspace_root=tmp_path,
        job=review_job,
        manifest=review_manifest,
        draft=waiting,
        draft_bytes=canonical_json_bytes(waiting),
        outcome_id="00000000-0000-0000-0000-000000000099",
        produced_at="2026-08-08T00:00:00.000Z",
        verification=verification,
        authoritative_targets=authoritative_targets,
        target_logs=target_logs,
    )
    assert finalized.outcome.result_type is (
        OutcomeResultType.COMPLETED
        if expected_gate
        else OutcomeResultType.INCONCLUSIVE
    )
    if expected_gate:
        assert finalized.outcome.payload.verdict.value == "NEED_MORE_EVIDENCE"


def test_wrong_problem_time_downgrades_candidate_to_inconclusive(
    tmp_path: Path,
) -> None:
    job, manifest, draft, draft_bytes, verification = _build(
        tmp_path,
        problem_time="2026-01-03T01:00:03.000Z",
    )
    assert verification.positive_gate_passed is False
    time_rules = [
        item
        for item in verification.audit.rules
        if item.server_evaluation.rule_kind == "EVENT_TIME_WINDOW"
    ]
    assert time_rules
    assert all(
        item.server_evaluation.status is ServerRuleStatus.VERIFIED_FAIL
        for item in time_rules
    )
    authoritative_targets, target_logs = _captured_result_targets(
        tmp_path,
        problem_time="2026-01-03T01:00:03.000Z",
    )
    (tmp_path / "runtime").mkdir()
    (tmp_path / "output").mkdir()

    finalized = finalize_server_outcome(
        workspace_root=tmp_path,
        job=job,
        manifest=manifest,
        draft=draft,
        draft_bytes=draft_bytes,
        outcome_id="00000000-0000-0000-0000-000000000099",
        produced_at="2026-08-08T00:00:00.000Z",
        verification=verification,
        authoritative_targets=authoritative_targets,
        target_logs=target_logs,
    )

    assert finalized.outcome.result_type is OutcomeResultType.INCONCLUSIVE
    assert finalized.outcome.payload.candidate_conclusion_draft is None
    assert finalized.user_result is not None
    assert finalized.user_result.status == "INCONCLUSIVE"
    assert len(finalized.generated_result_files) == 1
    assert {
        item.draft.artifact_kind.value for item in finalized.generated_result_files
    } == {"USER_RESULT"}
    assert {
        item.artifact_kind.value for item in finalized.outcome.proposed_artifact_drafts
    } == {"USER_RESULT"}
    assert (tmp_path / "output/job_outcome.json").read_bytes() == (
        finalized.canonical_bytes
    )


def test_completed_candidate_generates_json_and_target_log_archive(
    tmp_path: Path,
) -> None:
    job, manifest, draft, draft_bytes, verification = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    assert verification.positive_gate_passed is True
    authoritative_targets, target_logs = _captured_result_targets(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    (tmp_path / "runtime").mkdir()
    (tmp_path / "output").mkdir()

    finalized = finalize_server_outcome(
        workspace_root=tmp_path,
        job=job,
        manifest=manifest,
        draft=draft,
        draft_bytes=draft_bytes,
        outcome_id="00000000-0000-0000-0000-000000000096",
        produced_at="2026-08-08T00:00:00.000Z",
        verification=verification,
        authoritative_targets=authoritative_targets,
        target_logs=target_logs,
    )

    assert finalized.outcome.result_type is OutcomeResultType.COMPLETED
    assert finalized.user_result is not None
    assert finalized.user_result.status == "COMPLETED"
    assert finalized.user_result.root_cause is not None
    assert len(finalized.generated_result_files) == 2
    generated_by_kind = {
        item.draft.artifact_kind.value: item
        for item in finalized.generated_result_files
    }
    assert set(generated_by_kind) == {"USER_RESULT", "USER_RESULT_ARCHIVE"}
    archive = generated_by_kind["USER_RESULT_ARCHIVE"]
    assert archive.draft.metadata.target_log_count == 2
    with zipfile.ZipFile(io.BytesIO(archive.content)) as result_zip:
        assert set(result_zip.namelist()) == {
            "archive-manifest.json",
            "client__COMPACT__slot_1__checkout-client-101.log",
            "result.txt",
            "server__COMPACT__slot_2__inventory-server-202.log",
        }


def test_nearest_target_caveats_survive_server_final_json_zip_and_text(
    tmp_path: Path,
) -> None:
    job, manifest, draft, draft_bytes, verification = _build(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    authoritative_targets, target_logs = _captured_result_targets(
        tmp_path,
        problem_time="2026-01-03T00:00:03.000Z",
    )
    caveat = "The exact cycle was unavailable; the nearest cycle was selected."
    nearest_server = replace(
        authoritative_targets.targets[1],
        match_status="nearest",
        caveats=(caveat,),
    )
    authoritative_targets = replace(
        authoritative_targets,
        targets=(authoritative_targets.targets[0], nearest_server),
    )
    target_logs = (
        target_logs[0],
        replace(target_logs[1], target=nearest_server),
    )
    (tmp_path / "runtime").mkdir()
    (tmp_path / "output").mkdir()

    finalized = finalize_server_outcome(
        workspace_root=tmp_path,
        job=job,
        manifest=manifest,
        draft=draft,
        draft_bytes=draft_bytes,
        outcome_id="00000000-0000-0000-0000-000000000093",
        produced_at="2026-08-08T00:00:00.000Z",
        verification=verification,
        authoritative_targets=authoritative_targets,
        target_logs=target_logs,
    )

    assert finalized.outcome.result_type is OutcomeResultType.COMPLETED
    assert finalized.user_result is not None
    assert any(caveat in item for item in finalized.user_result.limitations)
    archive = next(
        item
        for item in finalized.generated_result_files
        if item.draft.artifact_kind.value == "USER_RESULT_ARCHIVE"
    )
    with zipfile.ZipFile(io.BytesIO(archive.content)) as result_zip:
        archive_manifest = json.loads(result_zip.read("archive-manifest.json"))
        server_target = next(
            item
            for item in archive_manifest["target_logs"]
            if item["label"] == "server"
        )
        assert server_target["match_status"] == "nearest"
        assert server_target["caveats"] == [caveat]
        assert caveat in result_zip.read("result.txt").decode("utf-8")


def test_any_completed_diagnosis_is_inconclusive_when_gate_fails(
    tmp_path: Path,
) -> None:
    job, manifest, draft, draft_bytes, verification = _build(
        tmp_path,
        problem_time="2026-01-03T01:00:03.000Z",
    )
    assert draft.payload is not None
    payload_without_candidate = draft.payload.model_copy(
        update={"candidate_conclusion_draft": None}
    )
    draft_without_candidate = draft.model_copy(
        update={"payload": payload_without_candidate}
    )
    draft_without_candidate_bytes = canonical_json_bytes(draft_without_candidate)
    authoritative_targets, target_logs = _captured_result_targets(
        tmp_path,
        problem_time="2026-01-03T01:00:03.000Z",
    )
    (tmp_path / "runtime").mkdir()
    (tmp_path / "output").mkdir()

    finalized = finalize_server_outcome(
        workspace_root=tmp_path,
        job=job,
        manifest=manifest,
        draft=draft_without_candidate,
        draft_bytes=draft_without_candidate_bytes,
        outcome_id="00000000-0000-0000-0000-000000000098",
        produced_at="2026-08-08T00:00:00.000Z",
        verification=verification,
        authoritative_targets=authoritative_targets,
        target_logs=target_logs,
    )

    assert finalized.outcome.result_type is OutcomeResultType.INCONCLUSIVE
    assert finalized.outcome.payload.candidate_conclusion_draft is None
