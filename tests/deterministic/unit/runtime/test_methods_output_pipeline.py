from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from problem_locator.contracts import (
    ErrorCode,
    ExecutionStage,
    Job,
    WorkspaceInputManifest,
    bytes_sha256,
    canonical_json_bytes,
    canonical_json_sha256,
    parse_canonical_json_bytes,
)
from problem_locator.contracts.enums import MethodsValidationReasonCode
from problem_locator.runtime.diagnosis_runtime import (
    DiagnosisRuntime,
    _methods_user_input_projection,
)
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.methods_grounding import (
    FrozenTargetLogV1,
    MethodDiagnosisDraftV1,
    MethodGroundingAuditV1,
    MethodsValidationError,
    SkillLoadReceiptV1,
    VerifiedMethodDiagnosisV1,
    scan_method_markers,
    verify_method_diagnosis,
)
from problem_locator.runtime.methods_skill import (
    ResolvedSpecializedSkillV1,
    load_specialized_skill_registration,
)
from problem_locator.runtime.output_reader import (
    RejectedAgentOutputError,
    ValidatedMethodDiagnosisDraft,
    ValidatedMethodReviewDraft,
    read_agent_output,
)
from problem_locator.runtime.resolved_logparse import _active_role_labels
from problem_locator.runtime.workspace import PreparedWorkspace, WorkspaceManager


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
CONTRACT_FIXTURES = REPOSITORY_ROOT / "tests/fixtures/contracts/positive"
RUNTIME_SKILL = (
    REPOSITORY_ROOT
    / "tests/fixtures/components/runtime-catalog/skill-dir/rpc-log-analysis"
)


def _contract(name: str, model: type[Job] | type[WorkspaceInputManifest]):
    return parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / name).read_bytes(),
        model_type=model,
    )


def _target_log(content: bytes) -> FrozenTargetLogV1:
    return FrozenTargetLogV1(
        source_id="client",
        relative_path="inputs/target-logs/client.log",
        content_sha256=bytes_sha256(content),
        content=content,
    )


def _verified_input(
    *,
    marker: str = "rpc deadline exceeded",
) -> tuple[
    ResolvedSpecializedSkillV1,
    FrozenTargetLogV1,
    SkillLoadReceiptV1,
    MethodDiagnosisDraftV1,
]:
    skill = load_specialized_skill_registration(RUNTIME_SKILL)
    line = "rpc deadline exceeded request_id=42"
    target = _target_log((line + "\n").encode("utf-8"))
    skill_load = scan_method_markers(skill=skill, target_logs=(target,))
    draft = MethodDiagnosisDraftV1.from_mapping(
        {
            "schema_version": 1,
            "status": "CONFIRMED",
            "confirmed_methods": ["rpc-call-timeout"],
            "candidate_methods": [],
            "evidence": [
                {
                    "method_id": "rpc-call-timeout",
                    "summary": "The frozen client line contains the timeout marker.",
                    "identity_tokens": ["request_id=42"],
                    "sources": [
                        {
                            "source_id": "client",
                            "line_number": 1,
                            "marker": marker,
                            "line": line,
                        }
                    ],
                }
            ],
            "limitations": [],
            "safety_notes": [],
        }
    )
    return skill, target, skill_load, draft


def test_verify_method_diagnosis_classifies_marker_not_indexed() -> None:
    skill, target, skill_load, draft = _verified_input(marker="rpc deadline")

    with pytest.raises(MethodsValidationError) as captured:
        verify_method_diagnosis(
            skill=skill,
            draft=draft,
            target_logs=(target,),
            logparse_receipt_sha256="a" * 64,
            skill_load=skill_load,
        )

    assert (
        captured.value.reason_code
        is MethodsValidationReasonCode.EVIDENCE_MARKER_NOT_INDEXED
    )


def test_verify_method_diagnosis_classifies_confirmed_evidence_missing() -> None:
    skill, target, skill_load, draft = _verified_input()
    missing = MethodDiagnosisDraftV1(
        status=draft.status,
        confirmed_methods=draft.confirmed_methods,
        candidate_methods=draft.candidate_methods,
        evidence=(),
        limitations=draft.limitations,
        safety_notes=draft.safety_notes,
    )

    with pytest.raises(MethodsValidationError) as captured:
        verify_method_diagnosis(
            skill=skill,
            draft=missing,
            target_logs=(target,),
            logparse_receipt_sha256="a" * 64,
            skill_load=skill_load,
        )

    assert (
        captured.value.reason_code
        is MethodsValidationReasonCode.CONFIRMED_EVIDENCE_MISSING
    )


def test_verify_method_diagnosis_classifies_full_scan_miss() -> None:
    skill = load_specialized_skill_registration(RUNTIME_SKILL)
    target = _target_log(b"unrelated log line\n")
    skill_load = scan_method_markers(skill=skill, target_logs=(target,))
    draft = MethodDiagnosisDraftV1.from_mapping(
        {
            "schema_version": 1,
            "status": "CONFIRMED",
            "confirmed_methods": ["rpc-call-timeout"],
            "candidate_methods": [],
            "evidence": [
                {
                    "method_id": "rpc-call-timeout",
                    "summary": "The claim requires a positive full-log marker scan.",
                    "identity_tokens": ["unrelated"],
                    "sources": [
                        {
                            "source_id": "client",
                            "line_number": 1,
                            "marker": "rpc deadline exceeded",
                            "line": "unrelated log line",
                        }
                    ],
                }
            ],
            "limitations": [],
            "safety_notes": [],
        }
    )

    with pytest.raises(MethodsValidationError) as captured:
        verify_method_diagnosis(
            skill=skill,
            draft=draft,
            target_logs=(target,),
            logparse_receipt_sha256="a" * 64,
            skill_load=skill_load,
        )

    assert (
        captured.value.reason_code
        is MethodsValidationReasonCode.CONFIRMED_MARKER_SCAN_MISS
    )


def _empty_workspace(root: Path) -> None:
    for relative in ("inputs", "runtime/tool-state", "output"):
        (root / relative).mkdir(parents=True, exist_ok=True)


def _prepared_workspace(
    root: Path,
    manifest: WorkspaceInputManifest,
) -> PreparedWorkspace:
    _empty_workspace(root)
    manifest_bytes = canonical_json_bytes(manifest)
    (root / "inputs/manifest.json").write_bytes(manifest_bytes)
    (root / "inputs/manifest.json").chmod(0o444)
    (root / "inputs").chmod(0o555)
    root_stat = root.stat(follow_symlinks=False)
    inputs_stat = (root / "inputs").stat(follow_symlinks=False)
    runtime_stat = (root / "runtime").stat(follow_symlinks=False)
    tool_state_stat = (root / "runtime/tool-state").stat(follow_symlinks=False)
    output_stat = (root / "output").stat(follow_symlinks=False)
    return PreparedWorkspace(
        root=root,
        root_device=root_stat.st_dev,
        root_inode=root_stat.st_ino,
        inputs_device=inputs_stat.st_dev,
        inputs_inode=inputs_stat.st_ino,
        runtime_device=runtime_stat.st_dev,
        runtime_inode=runtime_stat.st_ino,
        tool_state_device=tool_state_stat.st_dev,
        tool_state_inode=tool_state_stat.st_ino,
        output_device=output_stat.st_dev,
        output_inode=output_stat.st_ino,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        attachments=(),
        evidence=(),
        artifacts=(),
        previous_outcomes=(),
    )


def _job_with_facts(names: tuple[str, ...]) -> Job:
    value = _contract("job-diagnose.json", Job).model_dump(mode="json")
    value["context_snapshot"]["user_facts"] = [
        {
            "item_id": f"00000000-0000-4000-8000-{index:012d}",
            "statement": f"value-{name}",
            "status": "ACTIVE",
            "provenance": {
                "source_type": "USER_INPUT",
                "source_ref": "00000000-0000-4000-8000-999999999999",
                "input_name": name,
            },
            "evidence_refs": [],
            "created_revision": 1,
            "supersedes": [],
        }
        for index, name in enumerate(names, start=1)
    ]
    return Job.model_validate(value)


def test_specialized_diagnosis_hard_cut_ignores_legacy_v6_envelope(
    tmp_path: Path,
) -> None:
    job = _contract("job-diagnose.json", Job)
    manifest = _contract("workspace-input-manifest.json", WorkspaceInputManifest)
    _empty_workspace(tmp_path)
    old_bytes = (CONTRACT_FIXTURES / "agent-job-outcome-draft-diagnosis.json").read_bytes()
    (tmp_path / "output/job_outcome.draft.json").write_bytes(old_bytes)

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(tmp_path, job, manifest)

    assert captured.value.failure.code is ErrorCode.OUTCOME_MISSING


def test_specialized_diagnosis_normalizes_pretty_methods_draft(
    tmp_path: Path,
) -> None:
    job = _contract("job-diagnose.json", Job)
    manifest = _contract("workspace-input-manifest.json", WorkspaceInputManifest)
    _empty_workspace(tmp_path)
    value = {
        "schema_version": 1,
        "status": "INSUFFICIENT",
        "confirmed_methods": [],
        "candidate_methods": [],
        "evidence": [],
        "limitations": ["No positive marker is present."],
        "safety_notes": [],
    }
    canonical = canonical_json_bytes(value)
    pretty = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    draft_path = tmp_path / "output/method-diagnosis.draft.json"
    draft_path.write_bytes(pretty)
    # A malformed legacy envelope must be irrelevant to the selected protocol.
    (tmp_path / "output/job_outcome.draft.json").write_bytes(b"not-json")

    result = read_agent_output(tmp_path, job, manifest)

    assert isinstance(result, ValidatedMethodDiagnosisDraft)
    assert pretty != canonical
    assert result.canonical_bytes == canonical
    assert draft_path.read_bytes() == canonical
    assert result.draft.status == "INSUFFICIENT"


def test_review_normalizes_pretty_methods_draft_without_sealer(tmp_path: Path) -> None:
    job = _contract("job-review.json", Job)
    manifest = _contract(
        "workspace-input-manifest-review.json",
        WorkspaceInputManifest,
    )
    _empty_workspace(tmp_path)
    value = {
        "schema_version": 1,
        "verdict": "PASS",
        "findings": [
            {
                "method_id": "slow-execution",
                "identity_tokens": ["request_id=42"],
                "verdict": "PASS",
                "reason": "The exact prior identity remains supported.",
            }
        ],
        "limitations": [],
    }
    canonical = canonical_json_bytes(value)
    pretty = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    draft_path = tmp_path / "output/method-review.draft.json"
    draft_path.write_bytes(pretty)

    result = read_agent_output(tmp_path, job, manifest)

    assert isinstance(result, ValidatedMethodReviewDraft)
    assert pretty != canonical
    assert result.canonical_bytes == canonical
    assert draft_path.read_bytes() == canonical
    assert result.draft.findings[0].identity_tokens == ("request_id=42",)
    assert not (tmp_path / "runtime/tool-state/outcome-draft.finalized.json").exists()


@pytest.mark.parametrize(
    "invalid_payload",
    [
        b'\xef\xbb\xbf{"schema_version":1}',
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":NaN}',
        b'{"schema_version":1}\xff',
    ],
)
def test_methods_draft_still_rejects_ambiguous_or_invalid_json(
    tmp_path: Path,
    invalid_payload: bytes,
) -> None:
    job = _contract("job-diagnose.json", Job)
    manifest = _contract("workspace-input-manifest.json", WorkspaceInputManifest)
    _empty_workspace(tmp_path)
    draft_path = tmp_path / "output/method-diagnosis.draft.json"
    draft_path.write_bytes(invalid_payload)

    with pytest.raises(RejectedAgentOutputError) as captured:
        read_agent_output(tmp_path, job, manifest)

    assert captured.value.failure.code is ErrorCode.OUTCOME_INVALID
    assert captured.value.failure_category == "method_draft_schema"
    assert captured.value.raw_outcome_bytes == invalid_payload
    assert draft_path.read_bytes() == invalid_payload


def test_workspace_freezes_methods_audit_bytes_in_memory_without_workspace_io(
    tmp_path: Path,
) -> None:
    manifest = _contract("workspace-input-manifest.json", WorkspaceInputManifest)
    workspace = _prepared_workspace(tmp_path / "workspace", manifest)
    target = b"noise\nAPI_COMPLETE request_id=42\n"
    receipt_context = {
        "job_id": manifest.job_id,
        "case_id": manifest.case_id,
        "registration_id": "test-timeout",
        "operation": "target-logs",
        "broker_request_sha256": "a" * 64,
        "broker_audit_sha256": "b" * 64,
    }
    input_files_before = {
        path.relative_to(workspace.root).as_posix(): path.read_bytes()
        for path in workspace.root.rglob("*")
        if path.is_file()
    }
    inputs_mode_before = stat.S_IMODE((workspace.root / "inputs").stat().st_mode)

    frozen = WorkspaceManager.freeze_methods_inputs(
        workspace,
        request={
            "schema_version": 1,
            "job_id": manifest.job_id,
            "case_id": manifest.case_id,
        },
        target_logs=(("server", "server", target),),
        receipt_context=receipt_context,
    )

    target_rows = [
        {
            "source_id": "server",
            "label": "server",
            "log_path": "inputs/target-logs/server.log",
            "size": len(target),
            "content_sha256": bytes_sha256(target),
        }
    ]
    expected_request_bytes = canonical_json_bytes(
        {
            "schema_version": 1,
            "job_id": manifest.job_id,
            "case_id": manifest.case_id,
            "target_logs_path": "inputs/target_logs.json",
            "logparse_receipt_path": "inputs/logparse-receipt.json",
        }
    )
    expected_target_logs_bytes = canonical_json_bytes(
        {"schema_version": 1, "target_logs": target_rows}
    )
    expected_receipt_bytes = canonical_json_bytes(
        {"schema_version": 1, **receipt_context, "target_logs": target_rows}
    )
    request = json.loads(frozen.request_bytes)
    target_manifest = json.loads(frozen.target_logs_bytes)
    receipt = json.loads(frozen.receipt_bytes)
    assert request["target_logs_path"] == "inputs/target_logs.json"
    assert request["logparse_receipt_path"] == "inputs/logparse-receipt.json"
    assert target_manifest["target_logs"][0]["source_id"] == "server"
    assert target_manifest["target_logs"][0]["content_sha256"] == bytes_sha256(target)
    assert receipt["broker_audit_sha256"] == "b" * 64
    assert frozen.request_bytes == expected_request_bytes
    assert frozen.target_logs_bytes == expected_target_logs_bytes
    assert frozen.receipt_bytes == expected_receipt_bytes
    assert frozen.receipt_sha256 == bytes_sha256(frozen.receipt_bytes)
    assert frozen.target_logs[0].content == target
    assert {
        path.relative_to(workspace.root).as_posix(): path.read_bytes()
        for path in workspace.root.rglob("*")
        if path.is_file()
    } == input_files_before == {"inputs/manifest.json": workspace.manifest_bytes}
    assert stat.S_IMODE((workspace.root / "inputs").stat().st_mode) == (
        inputs_mode_before
    )


def test_workspace_freeze_rejects_server_owned_request_paths_without_writes(
    tmp_path: Path,
) -> None:
    manifest = _contract("workspace-input-manifest.json", WorkspaceInputManifest)
    workspace = _prepared_workspace(tmp_path / "reserved-request", manifest)

    with pytest.raises(RuntimeExecutionError) as caught:
        WorkspaceManager.freeze_methods_inputs(
            workspace,
            request={
                "schema_version": 1,
                "target_logs_path": "attacker-controlled.json",
            },
            target_logs=(("server", "server", b"safe\n"),),
            receipt_context={
                "job_id": manifest.job_id,
                "case_id": manifest.case_id,
                "registration_id": "test-timeout",
                "operation": "target-logs",
                "broker_request_sha256": "a" * 64,
                "broker_audit_sha256": "b" * 64,
            },
        )

    assert caught.value.failure.stage is ExecutionStage.WORKSPACE_PREPARE
    assert caught.value.failure.code is ErrorCode.WORKSPACE_PREPARE_FAILED
    assert {
        path.relative_to(workspace.root).as_posix()
        for path in workspace.root.rglob("*")
        if path.is_file()
    } == {"inputs/manifest.json"}


def test_workspace_freeze_rejects_replaced_inputs_directory(
    tmp_path: Path,
) -> None:
    manifest = _contract("workspace-input-manifest.json", WorkspaceInputManifest)
    workspace = _prepared_workspace(tmp_path / "replaced-inputs", manifest)
    original_inputs = workspace.root / "inputs"
    original_inputs.chmod(0o755)
    original_inputs.rename(workspace.root / "original-inputs")
    original_inputs.mkdir()

    with pytest.raises(RuntimeExecutionError) as caught:
        WorkspaceManager.freeze_methods_inputs(
            workspace,
            request={"schema_version": 1},
            target_logs=(("server", "server", b"safe\n"),),
            receipt_context={
                "job_id": manifest.job_id,
                "case_id": manifest.case_id,
                "registration_id": "test-timeout",
                "operation": "target-logs",
                "broker_request_sha256": "a" * 64,
                "broker_audit_sha256": "b" * 64,
            },
        )

    assert caught.value.failure.stage is ExecutionStage.WORKSPACE_PREPARE
    assert caught.value.failure.code is ErrorCode.WORKSPACE_PREPARE_FAILED


def test_optional_role_is_omitted_until_any_binding_activates_the_group(
    tmp_path: Path,
) -> None:
    loaded = load_specialized_skill_registration(RUNTIME_SKILL)
    preprocessing = loaded.registration.preprocessing
    assert preprocessing.logparse_plan is not None
    optional_roles = (
        preprocessing.roles[0],
        {**preprocessing.roles[1], "presence": "OPTIONAL"},
    )
    input_names = (
        "problem_time",
        "client_slot",
        "client_process_name",
        "server_slot",
        "server_process_name",
    )
    skill = replace(
        loaded,
        registration=replace(
            loaded.registration,
            preprocessing=replace(preprocessing, roles=optional_roles),
        ),
        methods=replace(
            loaded.methods,
            required_user_inputs=input_names,
            required_artifacts=(),
        ),
    )
    inactive_names = input_names[:3]
    inactive = _methods_user_input_projection(skill, set(inactive_names))
    assert inactive.active_required_names == inactive_names

    workspace = _prepared_workspace(
        tmp_path / "workspace",
        _contract("workspace-input-manifest.json", WorkspaceInputManifest),
    )
    request = DiagnosisRuntime._methods_request_value(
        _job_with_facts(inactive_names),
        workspace,
        skill,
    )
    assert [item["name"] for item in request["user_inputs"]] == list(inactive_names)

    activated = _methods_user_input_projection(
        skill,
        {*inactive_names, "server_slot"},
    )
    assert activated.active_required_names == input_names
    bound_anchors = json.loads(json.dumps(preprocessing.logparse_plan["anchors"]))
    bound_anchors[1]["slot"]["name"] = "remote_slot_alias"
    assert _active_role_labels(
        list(optional_roles),
        bound_anchors,
        {name: "value" for name in inactive_names},
    ) == ("client",)
    assert _active_role_labels(
        list(optional_roles),
        bound_anchors,
        {**{name: "value" for name in inactive_names}, "remote_slot_alias": "2"},
    ) == ("client", "server")
    with pytest.raises(ValueError, match="server_process_name"):
        DiagnosisRuntime._methods_request_value(
            _job_with_facts((*inactive_names, "server_slot")),
            workspace,
            skill,
        )

    complete = DiagnosisRuntime._methods_request_value(
        _job_with_facts(input_names),
        workspace,
        skill,
    )
    assert [item["name"] for item in complete["user_inputs"]] == list(input_names)


def test_methods_request_projects_declared_inputs_and_keeps_extra_fact_in_snapshot(
    tmp_path: Path,
) -> None:
    loaded = load_specialized_skill_registration(RUNTIME_SKILL)
    skill = replace(
        loaded,
        methods=replace(loaded.methods, required_artifacts=()),
    )
    declared = skill.methods.required_user_inputs
    job = _job_with_facts((*declared, "order_id"))
    workspace = _prepared_workspace(
        tmp_path / "workspace",
        _contract("workspace-input-manifest.json", WorkspaceInputManifest),
    )

    request = DiagnosisRuntime._methods_request_value(job, workspace, skill)

    assert [item["name"] for item in request["user_inputs"]] == list(declared)
    assert "order_id" not in {
        item["name"] for item in request["user_inputs"]
    }
    assert "order_id" in {
        item.provenance.input_name for item in job.context_snapshot.user_facts
    }


def test_partial_review_subject_preserves_grounded_and_candidate_rule_order() -> None:
    job = _contract("job-review.json", Job)
    diagnosis = MethodDiagnosisDraftV1.from_mapping(
        {
            "schema_version": 1,
            "status": "PARTIAL",
            "confirmed_methods": ["slow-execution"],
            "candidate_methods": ["api-overrun"],
            "evidence": [
                {
                    "method_id": "slow-execution",
                    "summary": "One method is grounded while another remains open.",
                    "identity_tokens": ["request_id=42"],
                    "sources": [
                        {
                            "source_id": "server",
                            "line_number": 2,
                            "marker": "API_COMPLETE",
                            "line": "API_COMPLETE request_id=42",
                        }
                    ],
                }
            ],
            "limitations": ["The API overrun method is not yet grounded."],
            "safety_notes": [],
        }
    )
    verified = VerifiedMethodDiagnosisV1(
        draft=diagnosis,
        audit=MethodGroundingAuditV1(
            schema_version=1,
            registration_id="test-timeout",
            registration_sha256="a" * 64,
            package_tree_sha256="b" * 64,
            combined_sha256="c" * 64,
            logparse_receipt_sha256="d" * 64,
            status="PARTIAL",
            confirmed_methods=("slow-execution",),
            evidence_count=1,
            checked_source_count=1,
            skill_load=SkillLoadReceiptV1(
                package_tree_sha256="b" * 64,
                scanned_source_ids=("server",),
                marker_hits=(("server", "API_COMPLETE", 2),),
                loaded_method_ids=("slow-execution",),
            ),
        ),
    )
    identity_hash = canonical_json_sha256(
        {
            "schema_version": 1,
            "method_id": "slow-execution",
            "identity_tokens": ["request_id=42"],
        }
    )[:16]

    subject = DiagnosisRuntime._methods_review_subject(job, verified)

    assert subject.required_rule_ids == [
        f"methods:slow-execution:{identity_hash}",
        "methods:candidate:api-overrun",
    ]
    assert [item.rule_id for item in subject.causal_assertions] == [
        f"methods:slow-execution:{identity_hash}"
    ]
    assert [item.value for item in subject.mechanical_facts] == [
        "SERVER_GROUNDED",
        "UNGROUNDED",
    ]
    assert [item.source_rule_id for item in subject.mechanical_facts] == (
        subject.required_rule_ids
    )

    first_ref = job.evidence_refs[0]
    second_ref = "00000000-0000-0000-0000-000000000041"
    candidate = job.context_snapshot.candidate_conclusion
    assert candidate is not None and job.review_target is not None
    candidate_payload = candidate.model_dump(mode="json")
    candidate_payload["supporting_evidence_refs"] = [second_ref, first_ref]
    candidate_payload["completion_criteria_mapping"][0]["evidence_refs"] = [
        first_ref,
        second_ref,
    ]
    candidate_payload["content_hash"] = canonical_json_sha256(
        {
            "resolution_status": candidate_payload["resolution_status"],
            "terminal_path_id": candidate_payload["terminal_path_id"],
            "statement": candidate_payload["statement"],
            "causal_factors": candidate_payload["causal_factors"],
            "candidate_factors": candidate_payload["candidate_factors"],
            "excluded_factors": candidate_payload["excluded_factors"],
            "supporting_evidence_refs": candidate_payload["supporting_evidence_refs"],
            "completion_criteria_mapping": candidate_payload["completion_criteria_mapping"],
        }
    )
    reordered_candidate = type(candidate).model_validate(candidate_payload)
    reordered_snapshot = job.context_snapshot.model_copy(
        update={
            "evidence_refs": [first_ref, second_ref],
            "candidate_conclusion": reordered_candidate,
        }
    )
    reordered_target = job.review_target.model_copy(
        update={"candidate_content_hash": reordered_candidate.content_hash}
    )
    reordered_job = job.model_copy(
        update={
            "evidence_refs": [first_ref, second_ref],
            "context_snapshot": reordered_snapshot,
            "review_target": reordered_target,
        }
    )

    reordered_subject = DiagnosisRuntime._methods_review_subject(
        reordered_job,
        verified,
    )

    assert reordered_subject.required_evidence_refs == [first_ref, second_ref]
