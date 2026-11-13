from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from problem_locator.contracts import (
    AgentJobOutcomeDraftV2, OutcomeResultType, RouteDecision, RouteKind,
    WorkspaceInputManifest, bytes_sha256, canonical_json_bytes,
)
from problem_locator.runtime.server_outcome_finalizer import finalize_server_outcome

from test_route_admission import _inputs as _route_inputs


def _inputs(*, selected=True, count=1):
    job, index, _response = _route_inputs(count)
    index_text = json.dumps(index, ensure_ascii=False)
    skill_id = job.available_skill_refs[0].id if selected else None
    draft = AgentJobOutcomeDraftV2(
        schema_version=2, job_id=job.job_id, case_id=job.case_id, job_type=job.job_type,
        base_state_revision=job.base_state_revision,
        result_type=OutcomeResultType.COMPLETED if selected else OutcomeResultType.NO_CAPABILITY,
        payload=RouteDecision(
            kind=RouteKind.MATCHED if selected else RouteKind.NO_CAPABILITY,
            skill_ref=job.available_skill_refs[0] if selected else None,
            reason="问题属于候选 Skill 的适用范围。" if selected else "无法确定专用 Skill，转入通用定位。",
            confidence=0.99,
        ),
        consumed_evidence_refs=[], proposed_evidence_drafts=[], proposed_artifact_drafts=[],
        rule_claims=[], error=None,
    )
    draft_bytes = canonical_json_bytes(draft)
    hashes = {
        "context_snapshot_sha256": bytes_sha256(canonical_json_bytes(job.context_snapshot)),
        "skill_index_sha256": bytes_sha256(index_text.encode("utf-8")),
    }
    audit = {
        "schema_version": 1, "policy": "shortlisted_route_v1", "job_id": job.job_id,
        "case_id": job.case_id, "base_state_revision": job.base_state_revision,
        "input_hashes": hashes, "draft_sha256": bytes_sha256(draft_bytes),
        "model_skill_id": skill_id, "effective_skill_id": skill_id, "confidence": 0.99,
        "reason_code": "ADMITTED" if selected else "MODEL_NO_MATCH",
        "retrieval": {
            "policy": "lexical_top5_v1", "candidate_ids": [job.available_skill_refs[0].id],
            "ranked_candidates": [{"skill_id": job.available_skill_refs[0].id, "score": 1.0}],
            **hashes, "model_index_sha256": "a" * 64,
        },
    }
    return job, index_text, SimpleNamespace(draft=draft, canonical_bytes=draft_bytes, route_admission=audit)


def _finalize(tmp_path, job, index, result, *, admission, draft_bytes=None, draft=None):
    manifest = WorkspaceInputManifest(
        schema_version=2, job_id=job.job_id, case_id=job.case_id, job_type=job.job_type,
        entries=[], logparse_tool_ref=None, logparse_product=None,
    )
    (tmp_path / "output").mkdir(exist_ok=True)
    (tmp_path / "runtime").mkdir(exist_ok=True)
    return finalize_server_outcome(
        workspace_root=tmp_path, job=job, manifest=manifest,
        draft=result.draft if draft is None else draft,
        draft_bytes=result.canonical_bytes if draft_bytes is None else draft_bytes,
        outcome_id="00000000-0000-4000-8000-000000000700",
        produced_at="2026-09-28T00:00:00.000Z", verification=None,
        authoritative_targets=None, target_logs=(), route_admission=admission,
        route_skill_index=index,
    )


@pytest.mark.parametrize("selected", [True, False])
def test_route_cannot_publish_without_this_executions_admission(tmp_path, selected):
    job, index, result = _inputs(selected=selected)
    with pytest.raises(ValueError, match="准入"):
        _finalize(tmp_path, job, index, result, admission=None)
    assert not (tmp_path / "output/job_outcome.json").exists()
    assert not (tmp_path / "runtime/server-state").exists()


@pytest.mark.parametrize("mutation", [
    "job", "case", "revision", "policy", "schema", "schema_bool", "draft_hash", "effective_id",
    "model_id", "reason_code", "confidence", "snapshot_hash", "missing_hashes", "index_hash",
    "missing_retrieval", "retrieval_policy", "retrieval_snapshot", "retrieval_index",
    "candidate_not_recalled", "candidate_ids_not_list", "missing_index", "changed_index", "old_policy",
])
def test_route_cannot_publish_with_mismatched_admission(tmp_path, mutation):
    job, index, result = _inputs()
    admission = copy.deepcopy(result.route_admission)
    if mutation == "snapshot_hash":
        admission["input_hashes"]["context_snapshot_sha256"] = "0" * 64
    elif mutation == "missing_hashes":
        del admission["input_hashes"]
    elif mutation == "index_hash":
        admission["input_hashes"]["skill_index_sha256"] = "0" * 64
    elif mutation == "missing_retrieval":
        del admission["retrieval"]
    elif mutation.startswith("retrieval_"):
        key = {"retrieval_policy": "policy", "retrieval_snapshot": "context_snapshot_sha256",
               "retrieval_index": "skill_index_sha256"}[mutation]
        admission["retrieval"][key] = "0" * 64
    elif mutation == "candidate_not_recalled":
        admission["retrieval"]["candidate_ids"] = []
    elif mutation == "candidate_ids_not_list":
        admission["retrieval"]["candidate_ids"] = job.available_skill_refs[0].id
    elif mutation == "missing_index":
        index = None
    elif mutation == "changed_index":
        index += " "
    else:
        field, value = {
            "job": ("job_id", "another-job"), "case": ("case_id", "another-case"),
            "revision": ("base_state_revision", job.base_state_revision + 1),
            "policy": ("policy", "unchecked"), "schema": ("schema_version", 2),
            "old_policy": ("policy", "strict_route_admission_v1"),
            "schema_bool": ("schema_version", True), "draft_hash": ("draft_sha256", "0" * 64),
            "effective_id": ("effective_skill_id", None), "model_id": ("model_skill_id", None),
            "reason_code": ("reason_code", "UNCERTAIN_CANDIDATE"), "confidence": ("confidence", 0.5),
        }[mutation]
        admission[field] = value
    with pytest.raises(ValueError, match="准入"):
        _finalize(tmp_path, job, index, result, admission=admission)
    assert not (tmp_path / "output/job_outcome.json").exists()


@pytest.mark.parametrize("change_bytes", [True, False])
def test_route_draft_bytes_and_dto_must_both_match_admitted_draft(tmp_path, change_bytes):
    job, index, result = _inputs()
    draft = result.draft
    draft_bytes = result.canonical_bytes + b" " if change_bytes else result.canonical_bytes
    if not change_bytes:
        value = draft.model_dump(mode="json")
        value["payload"]["reason"] = "草稿在审核后被更改。"
        draft = AgentJobOutcomeDraftV2.model_validate(value)
    with pytest.raises(ValueError, match="准入"):
        _finalize(tmp_path, job, index, result, admission=result.route_admission, draft=draft, draft_bytes=draft_bytes)
    assert not (tmp_path / "output/job_outcome.json").exists()


@pytest.mark.parametrize("selected", [True, False])
def test_admitted_route_publishes_without_expanding_persisted_outcome_schema(tmp_path, selected):
    job, index, result = _inputs(selected=selected)
    finalized = _finalize(tmp_path, job, index, result, admission=result.route_admission)
    assert finalized.outcome.payload.kind is (RouteKind.MATCHED if selected else RouteKind.NO_CAPABILITY)
    assert finalized.outcome.decision_audit is None
    assert finalized.decision_audit_bytes is None
    assert set(finalized.outcome.payload.model_dump()) == {"kind", "skill_ref", "reason", "confidence"}
    assert b"route_admission" not in canonical_json_bytes(finalized.outcome)
    assert (tmp_path / "output/job_outcome.json").read_bytes() == finalized.canonical_bytes


@pytest.mark.parametrize("mutation", ["unrecalled_frozen_skill", "same_id_different_version"])
def test_route_cannot_publish_outside_the_recalled_frozen_refs(tmp_path, mutation):
    job, index, result = _inputs(count=2)
    value = result.draft.model_dump(mode="json")
    if mutation == "unrecalled_frozen_skill":
        value["payload"]["skill_ref"] = job.available_skill_refs[1].model_dump(mode="json")
    else:
        value["payload"]["skill_ref"]["content_hash"] = "0" * 64
    draft = AgentJobOutcomeDraftV2.model_validate(value)
    draft_bytes = canonical_json_bytes(draft)
    admission = copy.deepcopy(result.route_admission)
    admission.update(draft_sha256=bytes_sha256(draft_bytes),
                     model_skill_id=draft.payload.skill_ref.id,
                     effective_skill_id=draft.payload.skill_ref.id)
    with pytest.raises(ValueError, match="准入"):
        _finalize(tmp_path, job, index, result, admission=admission, draft=draft, draft_bytes=draft_bytes)
    assert not (tmp_path / "output/job_outcome.json").exists()
