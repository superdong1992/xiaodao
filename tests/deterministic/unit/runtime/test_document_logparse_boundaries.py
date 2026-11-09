"""Narrow regressions for document Logparse inputs, batches, and route history."""
from __future__ import annotations

import hashlib
import json

import pytest

from problem_locator.contracts import (
    CaseAggregate, Job, JobOutcome, OutcomeResultType, canonical_json_bytes,
)
from tests.document_skill_helpers import document_input, document_registration
from tests.deterministic.contracts.fakes import InMemoryCancellationSignal
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    _MethodsRuntimeBackend, _json, _route_job,
)
from tests.deterministic.unit.runtime.test_document_runtime import (
    ReadingBackend, _zip, make_runtime, restore_permissions,
)


def _client_registration():
    value = document_registration(mode="logparse")
    preparation = value["runtime"]["preprocessing"]
    preparation["roles"][0]["label"] = "client"
    preparation["logparse_plan"]["anchors"][0]["label"] = "client"
    return value


def _facts():
    return {"problem_time": "2026-07-31T00:00:00.000Z", "process_name": "client"}


@pytest.mark.parametrize(("provided", "missing"), [
    ({"server_pid": "42"}, ["server_module", "server_slot", "server_process"]),
    ({"server_module": "service", "server_slot": "1"}, ["server_process"]),
])
def test_partial_optional_role_requests_only_missing_non_pid_inputs(tmp_path, provided, missing):
    registration = _client_registration()
    names = ("server_module", "server_slot", "server_process", "server_pid")
    registration["inputs"]["user"].extend(document_input(name, required=False) for name in names)
    preparation = registration["runtime"]["preprocessing"]
    preparation["roles"].append({"label": "server", "description": "可选服务端",
        "presence": "OPTIONAL", "source_reference": "SKILL.md"})
    preparation["logparse_plan"]["anchors"].append({"label": "server", **{
        field: {"source": "USER_FACT", "name": name}
        for field, name in zip(("module", "slot", "process_name", "pid"), names, strict=True)
    }})
    runtime, job, backend, broker, _, _ = make_runtime(tmp_path, registration=registration,
        facts={**_facts(), **provided}, files=[("logs.zip", "application/zip", _zip(b"source"))])

    outcome = runtime.execute(job, InMemoryCancellationSignal()).job_outcome

    assert outcome.result_type == OutcomeResultType.NEED_INPUT, outcome.error
    requirements = outcome.payload.state_delta.add_pending_requirements
    assert [item.name for item in requirements] == missing
    assert all(item.required and item.kind.value == "INPUT" for item in requirements)
    assert outcome.payload.requested_input == [item.requirement_id for item in requirements]
    assert not outcome.payload.requested_attachments
    assert backend.calls == [] and broker.open_calls == []


def test_multiple_archives_keep_independent_plans_receipts_and_origins(tmp_path):
    runtime, job, backend, broker, records, _ = make_runtime(tmp_path,
        registration=_client_registration(), facts=_facts(), files=[
            ("first.zip", "application/zip", _zip(b"first archive")),
            ("second.zip", "application/zip", _zip(b"second archive")),
        ])
    producer = _MethodsRuntimeBackend(broker, job, "confirmed")
    expected_contents = [b"first parsed target\n", b"second parsed target\n"]

    def preprocess(session, *_):
        producer.target_contents["client"] = expected_contents[len(broker.sessions) - 1]
        producer._run_preprocessing({"workspace_root": session.workspace_root})

    broker.preprocessing_executor = preprocess
    outcome = runtime.execute(job, InMemoryCancellationSignal()).job_outcome

    assert outcome.result_type == OutcomeResultType.COMPLETED, outcome.error
    assert len(backend.calls) == 1 and len(broker.sessions) == 2
    assert all(session.closed for session in broker.sessions)
    assert len({session.workspace_root for session in broker.sessions}) == 2
    assert [call[2].resolved_logparse_plan.attachment_id for call in broker.open_calls] == job.attachment_refs
    assert [[entry.resource_id for entry in call[2].entries
             if entry.input_kind == "ATTACHMENT" and (call[1] / entry.relative_path).is_file()]
            for call in broker.open_calls] == [[ref] for ref in job.attachment_refs]
    manifest = backend.manifests[0]
    assert backend.contents == expected_contents
    assert [item["source_attachment_id"] for item in manifest["sources"]] == job.attachment_refs
    assert [item["source_attachment_id"] for item in manifest["files"]] == job.attachment_refs
    assert [item["source_attachment_name"] for item in manifest["files"]] == ["first.zip", "second.zip"]
    assert len({item["path"] for item in manifest["files"]}) == 2
    receipts = manifest["logparse_receipts"]
    assert len(receipts) == 2
    for attachment_id, receipt in zip(job.attachment_refs, receipts, strict=True):
        audit_bytes = records.read_audit_bytes(job.job_id, f"logparse_broker_audit.{attachment_id}.json")
        operation = json.loads(audit_bytes)["operations"][0]
        assert operation["request"]["attachment_id"] == attachment_id
        assert receipt["broker_request_sha256"] == operation["request_sha256"]
        assert receipt["broker_audit_sha256"] == hashlib.sha256(audit_bytes).hexdigest()


def test_logparse_runtime_preserves_real_prior_route_outcome(tmp_path):
    backend = ReadingBackend()
    runtime, job, _, broker, _, _ = make_runtime(tmp_path, registration=_client_registration(),
        facts=_facts(), files=[("logs.zip", "application/zip", _zip(b"source"))], backend=backend)
    route_id = "30000000-0000-0000-0000-000000000001"
    route = _route_job().model_dump(mode="json")
    route.update(runtime._asset_resolver._catalog.route_bindings().model_dump(mode="json"))
    route.update(job_id=route_id, status="SUCCEEDED", started_at=job.created_at,
        finished_at=job.created_at, runtime_epoch=job.runtime_epoch)
    route_job = Job.model_validate(route)
    previous = _json("job-outcome-route.json")
    previous.update(job_id=route_id, produced_at=job.created_at)
    previous["payload"].update(skill_ref=job.skill_ref.model_dump(mode="json"),
        reason="路由已确认本问题适用文档型服务定位方法。")
    previous = JobOutcome.model_validate(previous)
    job = Job.model_validate({**job.model_dump(mode="json"), "previous_outcome_refs": [previous.outcome_id]})
    raw = runtime._state_repository.aggregate.model_dump(mode="json")
    raw["jobs"].update({route_id: route_job.model_dump(mode="json"), job.job_id: job.model_dump(mode="json")})
    raw["outcomes"][previous.outcome_id] = previous.model_dump(mode="json")
    content = canonical_json_bytes(previous)
    digest = hashlib.sha256(content).hexdigest()
    raw["outcome_processing_records"][previous.outcome_id] = {
        "outcome_id": previous.outcome_id, "job_id": route_id, "outcome_hash": digest,
        "outcome_file_ref": {"relative_key": f"jobs/{route_id}/job_outcome.json", "sha256": digest,
            "size": len(content)}, "disposition": "APPLIED", "processed_at": job.created_at,
        "error_code": None, "accepted_evidence_ids": [], "accepted_artifact_ids": [],
        "generated_artifact_ids": [], "created_job_id": job.job_id, "reason": "路由结果已应用。",
    }
    runtime._state_repository.aggregate = CaseAggregate.model_validate(raw)
    producer = _MethodsRuntimeBackend(broker, job, "confirmed")
    broker.preprocessing_executor = lambda session, *_: (
        producer._run_preprocessing({"workspace_root": session.workspace_root}), None)[1]

    outcome = runtime.execute(job, InMemoryCancellationSignal()).job_outcome

    assert outcome.result_type == OutcomeResultType.COMPLETED, outcome.error
    assert len(backend.calls) == 1 and len(broker.sessions) == 1
    prompt = backend.calls[0]["prompt"]
    assert previous.payload.reason in prompt
    assert previous.outcome_id in prompt
    assert job.context_snapshot.problem_spec.statement in prompt
    assert backend.requests[0]["problem_spec"] == job.context_snapshot.problem_spec.model_dump(mode="json")
