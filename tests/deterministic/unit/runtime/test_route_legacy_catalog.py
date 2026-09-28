from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from problem_locator.contracts import Job, OutcomeResultType, RouteKind, canonical_json_bytes
from problem_locator.runtime.catalog import VersionedAssetCatalog
from problem_locator.runtime.context_policy import RuntimeAssetResolver
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime
from problem_locator.runtime.workspace import WorkspaceManager
from tests.deterministic.contracts.fakes import InMemoryCancellationSignal, InMemoryExecutionRecordStore

from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    CATALOG_FIXTURES,
    _Clock,
    _Ids,
    _NeverBackend,
    _StateView,
    _UnusedResourceStore,
    _route_aggregate,
    _route_job,
)


@pytest.mark.parametrize("candidate_count", [1, 2])
def test_legacy_candidates_remain_visible_and_fall_back_without_model(
    tmp_path: Path, candidate_count: int,
) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    for index in range(candidate_count):
        registration_id = f"legacy-triage-{index}"
        root = skill_dir / registration_id
        shutil.copytree(CATALOG_FIXTURES / "skill-dir/manual-triage", root)
        template_path = root / "registration-template.json"
        template = json.loads(template_path.read_bytes())
        template.update(schema_version=1, registration_id=registration_id)
        template.pop("routing", None)
        template_path.write_bytes(canonical_json_bytes(template))
    catalog = VersionedAssetCatalog(
        skill_dir=skill_dir, generic_skill_name="generic-problem-locator-smoke",
    )
    bindings = catalog.route_bindings(["unknown_fact"])
    assert len(bindings.available_skill_refs) == candidate_count
    payload = _route_job().model_dump(mode="json")
    payload.update(bindings.model_dump(mode="json"))
    payload.update(
        status="RUNNING", started_at="2026-07-31T00:00:01.000Z",
        runtime_epoch="00000000-0000-4000-8000-000000000499",
    )
    job = Job.model_validate(payload)
    assets = RuntimeAssetResolver(catalog).resolve_job(job)
    index = json.loads(assets.skill_index_text)
    assert index["schema_version"] == 3
    assert len(index["skills"]) == candidate_count
    assert all(skill["routing"] is None for skill in index["skills"])
    state = _StateView(_route_aggregate(job))
    records = InMemoryExecutionRecordStore()
    runtime = DiagnosisRuntime(
        methods_evidence_validation="strict", state_repository=state,
        resource_store=_UnusedResourceStore(), asset_catalog=catalog,
        logparse_broker_factory=None, execution_records=records,
        clock=_Clock(), id_generator=_Ids(),
        workspace_manager=WorkspaceManager(tmp_path / "data"),
        backend=_NeverBackend(),
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.result_type is OutcomeResultType.NO_CAPABILITY
    assert receipt.job_outcome.payload.kind is RouteKind.NO_CAPABILITY
    assert receipt.job_outcome.payload.skill_ref is None
    assert state.calls == []
    assert records.log_sinks == {}
    assert len(records.publish_outcome_calls) == 1
    audit = json.loads(records.read_audit_bytes(job.job_id, "route-admission.json"))
    assert audit["reason_code"] == "NO_ELIGIBLE_SKILL"
    assert audit["model_called"] is False
    assert audit["model_skill_id"] is audit["effective_skill_id"] is None
    assert audit["input_hashes"] == {
        "skill_index_sha256": hashlib.sha256(assets.skill_index_text.encode("utf-8")).hexdigest(),
        "context_snapshot_sha256": hashlib.sha256(canonical_json_bytes(job.context_snapshot)).hexdigest(),
    }
