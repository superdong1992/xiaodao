from __future__ import annotations

import copy
import json

import pytest

from problem_locator.contracts import (
    AgentJobOutcomeDraftV2, OutcomeResultType, WorkspaceInputManifest, bytes_sha256, canonical_json_bytes,
)
from problem_locator.runtime.keyword_route import try_direct_keyword_route
from problem_locator.runtime.server_outcome_finalizer import finalize_server_outcome
from tests.deterministic.unit.runtime.test_keyword_route import KEYWORD, keyword_inputs, with_current_fact


def _case():
    job, index = keyword_inputs()
    index_text = json.dumps(index, ensure_ascii=False)
    text = f"当前请求报错 {KEYWORD}。"
    admission = try_direct_keyword_route(job, skill_index=index_text, raw_problem_text=text)
    assert admission is not None
    draft = AgentJobOutcomeDraftV2(
        schema_version=2, job_id=job.job_id, case_id=job.case_id, job_type=job.job_type,
        base_state_revision=job.base_state_revision, result_type=OutcomeResultType.COMPLETED,
        payload=admission.decision, consumed_evidence_refs=[], proposed_evidence_drafts=[],
        proposed_artifact_drafts=[], rule_claims=[], error=None,
    )
    draft_bytes = canonical_json_bytes(draft)
    audit = {**admission.audit, "draft_sha256": bytes_sha256(draft_bytes)}
    return job, index_text, text, draft, draft_bytes, audit


def _finalize(tmp_path, job, index, text, draft, draft_bytes, audit):
    (tmp_path / "output").mkdir(exist_ok=True)
    (tmp_path / "runtime").mkdir(exist_ok=True)
    manifest = WorkspaceInputManifest(schema_version=2, job_id=job.job_id, case_id=job.case_id,
        job_type=job.job_type, entries=[], logparse_tool_ref=None, logparse_product=None)
    return finalize_server_outcome(
        workspace_root=tmp_path, job=job, manifest=manifest, draft=draft, draft_bytes=draft_bytes,
        outcome_id="00000000-0000-4000-8000-000000000798", produced_at="2026-11-10T00:00:00.000Z",
        verification=None, authoritative_targets=None, target_logs=(), route_admission=audit,
        route_skill_index=index, route_raw_problem_text=text,
    )


def test_keyword_route_finalizer_recomputes_the_decision_from_the_original_inputs(tmp_path):
    values = _case()
    result = _finalize(tmp_path, *values)
    assert result.outcome.payload == values[3].payload
    assert result.outcome.decision_audit is None
    assert b"direct_keyword_route" not in result.canonical_bytes
    assert (tmp_path / "output/job_outcome.json").read_bytes() == result.canonical_bytes


def test_original_keyword_admission_cannot_publish_over_current_facts(tmp_path):
    job, index, text, draft, draft_bytes, audit = _case()
    job = with_current_fact(job)
    audit["input_hashes"]["context_snapshot_sha256"] = bytes_sha256(canonical_json_bytes(job.context_snapshot))
    with pytest.raises(ValueError, match="准入"):
        _finalize(tmp_path, job, index, text, draft, draft_bytes, audit)
    assert not (tmp_path / "output/job_outcome.json").exists()


@pytest.mark.parametrize("mutation", [
    "missing-index", "missing-raw", "raw-content", "index-content", "snapshot-hash", "raw-hash",
    "index-hash", "job", "case", "revision", "policy", "model-called", "model-id", "effective-id",
    "reason-code", "reason", "confidence", "confidence-bool", "match-text", "match-position",
    "offset-unit", "draft-hash", "extra-field", "payload-ref", "payload-reason", "draft-bytes",
    "new-exclusion", "new-ambiguity",
])
def test_keyword_route_cannot_publish_tampered_or_unbound_admission(tmp_path, mutation):
    job, index, text, draft, draft_bytes, audit = _case()
    if mutation == "missing-index":
        index = None
    elif mutation == "missing-raw":
        text = None
    elif mutation == "raw-content":
        text = "当前问题没有可直接匹配的特征。"
    elif mutation == "index-content":
        index += " "
    elif mutation.endswith("-hash") and mutation != "draft-hash":
        key = {"snapshot-hash": "context_snapshot_sha256", "raw-hash": "raw_problem_text_sha256", "index-hash": "skill_index_sha256"}[mutation]
        audit["input_hashes"][key] = "0" * 64
    elif mutation in {"match-text", "match-position"}:
        audit["matches"][0]["keyword" if mutation == "match-text" else "start"] = "fake" if mutation == "match-text" else 0
    elif mutation.startswith("payload-"):
        value = draft.model_dump(mode="json")
        if mutation == "payload-ref":
            value["payload"]["skill_ref"]["content_hash"] = "0" * 64
        else:
            value["payload"]["reason"] = "准入后更换理由。"
        draft = AgentJobOutcomeDraftV2.model_validate(value)
        draft_bytes = canonical_json_bytes(draft)
        audit["draft_sha256"] = bytes_sha256(draft_bytes)
    elif mutation == "draft-bytes":
        draft_bytes += b" "
    elif mutation == "new-exclusion":
        value = json.loads(index)
        value["skills"][0]["routing"]["exclusions"] = [{"id": "other", "description": "存在排除条件。"}]
        index = json.dumps(value, ensure_ascii=False)
        audit["input_hashes"]["skill_index_sha256"] = bytes_sha256(index.encode())
    elif mutation == "new-ambiguity":
        text = "示例：" + text
        audit["input_hashes"]["raw_problem_text_sha256"] = bytes_sha256(text.encode())
    else:
        key, value = {
            "job": ("job_id", "another"), "case": ("case_id", "another"),
            "revision": ("base_state_revision", job.base_state_revision + 1),
            "policy": ("policy", "strict_route_admission_v1"), "model-called": ("model_called", True),
            "model-id": ("model_skill_id", job.available_skill_refs[0].id), "effective-id": ("effective_skill_id", None),
            "reason-code": ("reason_code", "ADMITTED"), "reason": ("reason", "other"),
            "confidence": ("confidence", 0.99), "confidence-bool": ("confidence", True),
            "offset-unit": ("match_offset_unit", "utf8"), "draft-hash": ("draft_sha256", "0" * 64),
            "extra-field": ("unchecked", True),
        }[mutation]
        audit[key] = value
    with pytest.raises(ValueError, match="准入"):
        _finalize(tmp_path, job, index, text, draft, draft_bytes, audit)
    assert not (tmp_path / "output/job_outcome.json").exists()
    assert not (tmp_path / "runtime/server-state").exists()
