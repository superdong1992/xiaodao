"""Keyword routing uses real pinned registrations and the normal publication seam."""
from __future__ import annotations

import json
import shutil

import pytest

from problem_locator.contracts import (
    CancellationReason, CaseAggregate, ErrorCode, Job, OutcomeResultType, RouteKind,
    canonical_json_bytes,
)
from problem_locator.runtime.catalog import VersionedAssetCatalog
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime
from problem_locator.runtime.workspace import WorkspaceManager
from tests.deterministic.contracts.fakes import InMemoryCancellationSignal, InMemoryExecutionRecordStore
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    CATALOG_FIXTURES, _Clock, _Ids, _RuntimeBackend, _StateView, _UnusedResourceStore,
    _job_from_catalog, _route_aggregate,
)


def _runtime(tmp_path, *, text="服务报错 ACME_RPC_DEADLINE，请定位。", declared=True,
             candidates=1, exclusions=False):
    skills = tmp_path / "skills"
    for index in range(candidates):
        root = skills / f"manual-triage-{index}"
        shutil.copytree(CATALOG_FIXTURES / "skill-dir/manual-triage", root)
        path = root / "registration-template.json"
        value = json.loads(path.read_bytes())
        value["registration_id"] = root.name
        if declared:
            value["routing"]["direct_match_keywords"] = ["ACME_RPC_DEADLINE"]
        if exclusions:
            value["routing"]["exclusions"] = [{"id": "offline", "description": "离线回放。"}]
        path.write_bytes(canonical_json_bytes(value))
    catalog = VersionedAssetCatalog(skill_dir=skills, generic_skill_name="generic-problem-locator-smoke")
    payload = _job_from_catalog(catalog).model_dump(mode="json")
    payload.update(status="RUNNING", started_at="2026-07-31T00:00:01.000Z",
        runtime_epoch="00000000-0000-4000-8000-000000000499")
    job = Job.model_validate(payload)
    data = _route_aggregate(job).model_dump(mode="json")
    data["case"]["raw_problem_text"] = text
    aggregate = CaseAggregate.model_validate(data)
    records = InMemoryExecutionRecordStore()
    backend = _RuntimeBackend(b'{"payload":{"skill_ref":null,"reason":"No specialized match."}}')
    runtime = DiagnosisRuntime(
        state_repository=_StateView(aggregate), resource_store=_UnusedResourceStore(),
        asset_catalog=catalog, logparse_broker_factory=None, execution_records=records,
        clock=_Clock(), id_generator=_Ids(), workspace_manager=WorkspaceManager(tmp_path / "data"),
        backend=backend,
    )
    return runtime, job, backend, records


def test_explicit_keyword_skips_route_backend_and_keeps_normal_outcome(tmp_path):
    runtime, job, backend, records = _runtime(tmp_path)
    assert "ACME_RPC_DEADLINE" not in job.context_snapshot.problem_spec.statement
    receipt = runtime.execute(job, InMemoryCancellationSignal())
    assert receipt.job_outcome.result_type is OutcomeResultType.COMPLETED, receipt.job_outcome.error
    assert receipt.job_outcome.payload.kind is RouteKind.MATCHED
    assert receipt.job_outcome.payload.skill_ref == job.available_skill_refs[0]
    assert backend.calls == [] and records.log_sinks == {}
    audit = json.loads(records.read_audit_bytes(job.job_id, "route-admission.json"))
    assert audit["policy"] == "direct_keyword_route_v1"
    assert audit["model_called"] is False and audit["model_skill_id"] is None
    assert "raw_response_sha256" not in audit
    assert records.read_audit_bytes(job.job_id, "route-response.raw.txt") is None
    assert records.read_audit_bytes(job.job_id, "finalization_manifest.json")


@pytest.mark.parametrize("options", [
    {"declared": False}, {"candidates": 2}, {"exclusions": True},
    {"text": "服务只是响应慢。"}, {"text": "并不是 ACME_RPC_DEADLINE。"},
    {"text": "服务报错 ACME_RPC_DEADLINE_EXTRA。"},
])
def test_non_unique_or_unclear_keyword_keeps_existing_llm_route(tmp_path, options):
    runtime, job, backend, records = _runtime(tmp_path, **options)
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type is OutcomeResultType.NO_CAPABILITY, result.error
    assert [call["backend_phase"] for call in backend.calls] == ["ROUTE"]
    audit = json.loads(records.read_audit_bytes(job.job_id, "route-admission.json"))
    assert audit["policy"] == "strict_route_admission_v1"


@pytest.mark.parametrize("when", ["before", "after-audit"])
def test_cancelled_keyword_route_never_publishes_matched(tmp_path, monkeypatch, when):
    runtime, job, backend, records = _runtime(tmp_path)
    signal = InMemoryCancellationSignal()
    if when == "before":
        signal.cancel(CancellationReason.USER_CANCEL)
    else:
        publish = records.publish_audit_bytes
        def cancel_after_audit(job_id, filename, payload):
            result = publish(job_id, filename, payload)
            if filename == "route-admission.json":
                signal.cancel(CancellationReason.USER_CANCEL)
            return result
        monkeypatch.setattr(records, "publish_audit_bytes", cancel_after_audit)
    result = runtime.execute(job, signal).job_outcome
    assert result.result_type is OutcomeResultType.FAILED
    assert result.error.code is ErrorCode.BACKEND_CANCELLED
    assert backend.calls == []


def test_keyword_route_cannot_publish_without_durable_admission(tmp_path, monkeypatch):
    runtime, job, backend, records = _runtime(tmp_path)
    publish = records.publish_audit_bytes
    def fail_route_audit(job_id, filename, payload):
        if filename == "route-admission.json":
            raise OSError("test audit storage failure")
        return publish(job_id, filename, payload)
    monkeypatch.setattr(records, "publish_audit_bytes", fail_route_audit)
    result = runtime.execute(job, InMemoryCancellationSignal()).job_outcome
    assert result.result_type is OutcomeResultType.FAILED
    assert result.error.code is ErrorCode.EXECUTION_RECORD_FAILED
    assert records.read_audit_bytes(job.job_id, "finalization_manifest.json") is None
    assert backend.calls == []
