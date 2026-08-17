from __future__ import annotations

import json
from pathlib import Path

import pytest

from problem_locator.contracts import AgentJobOutcomeDraftV2, Job, WorkspaceInputManifest
from problem_locator.runtime.server_verifier import _validate_requirement_requests


ROOT = Path(__file__).resolve().parents[4]
CONTRACTS = ROOT / "tests/fixtures/contracts/positive"
SKILL_MANIFEST = (
    ROOT
    / "tests/fixtures/components/diagnosis-generator/diagnose-service-takeover"
    / "diagnosis-skill.json"
)


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_bytes())


def _partial_job() -> Job:
    value = _json(CONTRACTS / "job-diagnose.json")
    value["artifact_refs"] = []
    value["attachment_refs"] = []
    value["evidence_refs"] = []
    value["previous_outcome_refs"] = []
    snapshot = value["context_snapshot"]
    assert isinstance(snapshot, dict)
    snapshot["evidence_refs"] = []
    snapshot["user_facts"] = [
        {
            "item_id": "00000000-0000-0000-0000-000000000090",
            "statement": "checkout",
            "status": "ACTIVE",
            "provenance": {
                "source_type": "USER_INPUT",
                "source_ref": "00000000-0000-0000-0000-000000000001",
                "input_name": "caller_service",
            },
            "evidence_refs": [],
            "created_revision": 2,
            "supersedes": [],
        }
    ]
    return Job.model_validate(value)


def _empty_manifest(job: Job) -> WorkspaceInputManifest:
    return WorkspaceInputManifest(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        logparse_tool_ref=job.logparse_tool_ref,
        logparse_product=job.logparse_product,
        entries=[],
        resolved_logparse_plan=None,
        review_subject=None,
    )


def _pending_requirement(
    pinned: dict[str, object],
    *,
    requirement_id: str,
    job_id: str,
) -> dict[str, object]:
    return {
        "requirement_id": requirement_id,
        "kind": pinned["kind"],
        "name": pinned["name"],
        "prompt": pinned["prompt"],
        "required": True,
        "constraints": pinned["constraints"],
        "status": "OPEN",
        "requested_by_job_id": job_id,
        "fulfilled_by_refs": [],
        "supplement_policy": pinned["supplement_policy"],
    }


def _mixed_draft(
    job: Job,
) -> tuple[
    AgentJobOutcomeDraftV2,
    list[dict[str, object]],
    list[dict[str, object]],
]:
    manifest = _json(SKILL_MANIFEST)
    pinned = manifest["requirements"]
    assert isinstance(pinned, list)
    pinned_requirements = [dict(item) for item in pinned if isinstance(item, dict)]
    by_name = {str(item["name"]): item for item in pinned_requirements}
    requested_names = [
        "problem_time",
        "client_slot",
        "client_process_name",
        "server_slot",
        "server_process_name",
        "server_service",
        "rpc_method",
        "log_archive",
    ]
    ids_by_name = {
        name: f"00000000-0000-0000-0000-{index:012d}"
        for index, name in enumerate(requested_names, start=96)
    }
    requirements = [
        _pending_requirement(
            by_name[name],
            requirement_id=ids_by_name[name],
            job_id=job.job_id,
        )
        for name in requested_names
    ]

    value = _json(CONTRACTS / "agent-job-outcome-draft-diagnosis.json")
    value["job_id"] = job.job_id
    value["case_id"] = job.case_id
    value["base_state_revision"] = job.base_state_revision
    value["result_type"] = "NEED_INPUT"
    payload = value["payload"]
    assert isinstance(payload, dict)
    payload["candidate_conclusion_draft"] = None
    payload["requested_input"] = [
        ids_by_name[name] for name in requested_names if name != "log_archive"
    ]
    payload["requested_attachments"] = [ids_by_name["log_archive"]]
    state_delta = payload["state_delta"]
    assert isinstance(state_delta, dict)
    state_delta["add_pending_requirements"] = requirements
    roles = manifest["roles"]
    assert isinstance(roles, list)
    return (
        AgentJobOutcomeDraftV2.model_validate(value),
        pinned_requirements,
        [dict(item) for item in roles if isinstance(item, dict)],
    )


def test_server_verifier_accepts_missing_inputs_and_attachment_after_partial_input(
) -> None:
    job = _partial_job()
    draft, pinned_requirements, pinned_roles = _mixed_draft(job)

    _validate_requirement_requests(
        job=job,
        manifest=_empty_manifest(job),
        draft=draft,
        pinned_requirements=pinned_requirements,
        pinned_roles=pinned_roles,
        rule_results={},
    )

    requested = [
        item.name for item in draft.payload.state_delta.add_pending_requirements
    ]
    assert requested == [
        "problem_time",
        "client_slot",
        "client_process_name",
        "server_slot",
        "server_process_name",
        "server_service",
        "rpc_method",
        "log_archive",
    ]
    assert "caller_service" not in requested


def test_server_verifier_rejects_requirement_ids_in_the_wrong_requested_array(
) -> None:
    job = _partial_job()
    draft, pinned_requirements, pinned_roles = _mixed_draft(job)
    value = draft.model_dump(mode="json")
    payload = value["payload"]
    first_input = payload["requested_input"].pop()
    payload["requested_attachments"].insert(0, first_input)
    invalid = AgentJobOutcomeDraftV2.model_validate(value)

    with pytest.raises(
        ValueError,
        match="requested_attachments must resolve OPEN ATTACHMENT",
    ):
        _validate_requirement_requests(
            job=job,
            manifest=_empty_manifest(job),
            draft=invalid,
            pinned_requirements=pinned_requirements,
            pinned_roles=pinned_roles,
            rule_results={},
        )


def test_server_verifier_rejects_requesting_an_already_present_partial_input(
) -> None:
    job = _partial_job()
    draft, pinned_requirements, pinned_roles = _mixed_draft(job)
    pinned_by_name = {str(item["name"]): item for item in pinned_requirements}
    value = draft.model_dump(mode="json")
    payload = value["payload"]
    requirement_id = "00000000-0000-0000-0000-000000000120"
    payload["requested_input"].append(requirement_id)
    payload["state_delta"]["add_pending_requirements"].append(
        _pending_requirement(
            pinned_by_name["caller_service"],
            requirement_id=requirement_id,
            job_id=job.job_id,
        )
    )
    invalid = AgentJobOutcomeDraftV2.model_validate(value)

    with pytest.raises(ValueError, match="exactly the server-activated"):
        _validate_requirement_requests(
            job=job,
            manifest=_empty_manifest(job),
            draft=invalid,
            pinned_requirements=pinned_requirements,
            pinned_roles=pinned_roles,
            rule_results={},
        )


def test_server_verifier_rejects_omitting_one_active_missing_input() -> None:
    job = _partial_job()
    draft, pinned_requirements, pinned_roles = _mixed_draft(job)
    value = draft.model_dump(mode="json")
    payload = value["payload"]
    omitted_id = payload["requested_input"].pop()
    payload["state_delta"]["add_pending_requirements"] = [
        item
        for item in payload["state_delta"]["add_pending_requirements"]
        if item["requirement_id"] != omitted_id
    ]
    invalid = AgentJobOutcomeDraftV2.model_validate(value)

    with pytest.raises(ValueError, match="exactly the server-activated"):
        _validate_requirement_requests(
            job=job,
            manifest=_empty_manifest(job),
            draft=invalid,
            pinned_requirements=pinned_requirements,
            pinned_roles=pinned_roles,
            rule_results={},
        )


def test_server_verifier_rejects_requesting_an_inactive_optional_role() -> None:
    job = _partial_job()
    draft, pinned_requirements, pinned_roles = _mixed_draft(job)
    pinned_roles[1]["presence"] = "OPTIONAL"

    with pytest.raises(ValueError, match="exactly the server-activated"):
        _validate_requirement_requests(
            job=job,
            manifest=_empty_manifest(job),
            draft=draft,
            pinned_requirements=pinned_requirements,
            pinned_roles=pinned_roles,
            rule_results={},
        )
