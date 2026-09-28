from __future__ import annotations

import copy

import pytest

from problem_locator.contracts import AgentJobOutcomeDraftV2, RouteKind, WorkspaceInputManifest, canonical_json_bytes
from problem_locator.runtime.server_outcome_finalizer import finalize_server_outcome

from test_route_admission import _inputs, _parse


def _finalize(tmp_path, job, result, *, admission, draft_bytes=None, draft=None):
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
    )


@pytest.mark.parametrize("selected", [True, False])
def test_route_cannot_publish_without_this_executions_admission(tmp_path, selected):
    job, index, response = _inputs()
    if not selected:
        response["skill_id"] = None
    result = _parse(job, index, response)
    with pytest.raises(ValueError, match="准入"):
        _finalize(tmp_path, job, result, admission=None)
    assert not (tmp_path / "output/job_outcome.json").exists()
    assert not (tmp_path / "runtime/server-state").exists()


@pytest.mark.parametrize("mutation", [
    "job", "case", "revision", "policy", "schema", "schema_bool", "draft_hash", "effective_id",
    "model_id", "reason_code", "confidence", "snapshot_hash", "missing_hashes",
])
def test_route_cannot_publish_with_mismatched_admission(tmp_path, mutation):
    job, index, response = _inputs()
    result = _parse(job, index, response)
    admission = copy.deepcopy(result.route_admission)
    if mutation == "snapshot_hash":
        admission["input_hashes"]["context_snapshot_sha256"] = "0" * 64
    elif mutation == "missing_hashes":
        del admission["input_hashes"]
    else:
        field, value = {
            "job": ("job_id", "another-job"), "case": ("case_id", "another-case"),
            "revision": ("base_state_revision", job.base_state_revision + 1),
            "policy": ("policy", "unchecked"), "schema": ("schema_version", 2),
            "schema_bool": ("schema_version", True), "draft_hash": ("draft_sha256", "0" * 64),
            "effective_id": ("effective_skill_id", None), "model_id": ("model_skill_id", None),
            "reason_code": ("reason_code", "UNCERTAIN_CANDIDATE"), "confidence": ("confidence", 0.5),
        }[mutation]
        admission[field] = value
    with pytest.raises(ValueError, match="准入"):
        _finalize(tmp_path, job, result, admission=admission)
    assert not (tmp_path / "output/job_outcome.json").exists()


@pytest.mark.parametrize("change_bytes", [True, False])
def test_route_draft_bytes_and_dto_must_both_match_admitted_draft(tmp_path, change_bytes):
    job, index, response = _inputs()
    result = _parse(job, index, response)
    draft = result.draft
    draft_bytes = result.canonical_bytes + b" " if change_bytes else result.canonical_bytes
    if not change_bytes:
        value = draft.model_dump(mode="json")
        value["payload"]["reason"] = "草稿在审核后被更改。"
        draft = AgentJobOutcomeDraftV2.model_validate(value)
    with pytest.raises(ValueError, match="准入"):
        _finalize(tmp_path, job, result, admission=result.route_admission, draft=draft, draft_bytes=draft_bytes)
    assert not (tmp_path / "output/job_outcome.json").exists()


@pytest.mark.parametrize("selected", [True, False])
def test_admitted_route_publishes_without_expanding_persisted_outcome_schema(tmp_path, selected):
    job, index, response = _inputs()
    if not selected:
        response["skill_id"] = None
    result = _parse(job, index, response)
    finalized = _finalize(tmp_path, job, result, admission=result.route_admission)
    assert finalized.outcome.payload.kind is (RouteKind.MATCHED if selected else RouteKind.NO_CAPABILITY)
    assert finalized.outcome.decision_audit is None
    assert finalized.decision_audit_bytes is None
    assert set(finalized.outcome.payload.model_dump()) == {"kind", "skill_ref", "reason", "confidence"}
    assert b"route_admission" not in canonical_json_bytes(finalized.outcome)
    assert (tmp_path / "output/job_outcome.json").read_bytes() == finalized.canonical_bytes
