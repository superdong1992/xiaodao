from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from problem_locator.contracts import (
    AttachmentRequirementConstraints,
    DiagnosisOutcome,
    DiagnosisStateDelta,
    InputRequirementConstraints,
    Job,
    JobOutcome,
    OutcomeResultType,
    PendingRequirement,
    RequirementKind,
    RequirementStatus,
    SupplementPolicy,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
AUDITOR = (
    REPOSITORY_ROOT
    / "tools"
    / "test-flow"
    / "runtime-support"
    / "audit_service_agent_usage.py"
)
MODEL = "deepseek-v4-flash[1m]"
CONTRACT_FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "contracts" / "positive"
REGISTRATION_ID = "rpc-timeout-methods-v1"
INPUT_REQUIREMENT_ID = "00000000-0000-0000-0000-000000000098"
ATTACHMENT_REQUIREMENT_ID = "00000000-0000-0000-0000-000000000099"


def _write_canonical(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value))


def _run_audit(tmp_path: Path) -> dict[str, object]:
    output = tmp_path / "service-agent-usage.json"
    result = _invoke_audit(tmp_path, output)
    assert result.returncode == 0, result.stderr
    return json.loads(output.read_text(encoding="utf-8"))


def _invoke_audit(tmp_path: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(AUDITOR),
            "--jobs-root",
            str(tmp_path / "jobs"),
            "--output",
            str(output),
            "--model",
            MODEL,
            "--max-turns",
            "8",
            "--max-total-tokens",
            "2000000",
            "--max-budget-usd",
            "3",
            "--hard-timeout-seconds",
            "900",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _preflight_job() -> Job:
    source = parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / "job-diagnose.json").read_bytes(),
        Job,
    )
    value = source.model_dump(mode="python")
    assert value["skill_ref"] is not None
    value["skill_ref"]["id"] = f"diagnosis-skill/{REGISTRATION_ID}"
    return Job.model_validate(value)


def _preflight_outcome(
    job: Job,
    *,
    missing_user_inputs: tuple[str, ...] = (),
    missing_log_archive: bool = True,
) -> JobOutcome:
    source = parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / "job-outcome-diagnosis.json").read_bytes(),
        JobOutcome,
    )
    requirements = [
        PendingRequirement(
            requirement_id=INPUT_REQUIREMENT_ID,
            kind=RequirementKind.INPUT,
            name=name,
            prompt=f"请输入 {name}。",
            required=True,
            constraints=InputRequirementConstraints(
                value_type="STRING",
                min_utf8_bytes=1,
                max_utf8_bytes=256,
                pattern=None,
                allowed_values=[],
            ),
            status=RequirementStatus.OPEN,
            requested_by_job_id=job.job_id,
            fulfilled_by_refs=[],
            supplement_policy=SupplementPolicy.MISSING_ONLY,
        )
        for name in missing_user_inputs
    ]
    if missing_log_archive:
        requirements.append(
            PendingRequirement(
                requirement_id=ATTACHMENT_REQUIREMENT_ID,
                kind=RequirementKind.ATTACHMENT,
                name="log_archive",
                prompt="请上传 Logparse 支持的日志归档。",
                required=True,
                constraints=AttachmentRequirementConstraints(
                    allowed_content_types=[
                        "application/gzip",
                        "application/zip",
                        "application/x-tar",
                    ],
                    min_count=1,
                    max_count=1,
                ),
                status=RequirementStatus.OPEN,
                requested_by_job_id=job.job_id,
                fulfilled_by_refs=[],
                supplement_policy=SupplementPolicy.MISSING_ONLY,
            )
        )
    assert requirements
    delta = DiagnosisStateDelta(
        problem_spec_patch=None,
        add_user_facts=[],
        proposed_facts=[],
        add_active_hypotheses=[],
        update_hypotheses=[],
        reject_hypotheses=[],
        add_open_questions=[],
        resolve_questions=[],
        add_pending_requirements=requirements,
        fulfill_requirements=[],
        add_evidence_bindings=[],
    )
    payload = DiagnosisOutcome(
        findings=[],
        state_delta=delta,
        requested_input=[INPUT_REQUIREMENT_ID] if missing_user_inputs else [],
        requested_attachments=(
            [ATTACHMENT_REQUIREMENT_ID] if missing_log_archive else []
        ),
        candidate_conclusion_draft=None,
        recommended_next_step=(
            "Supply the required Methods inputs and log archive."
            if missing_user_inputs and missing_log_archive
            else (
                "Supply the required Methods inputs."
                if missing_user_inputs
                else "Upload and submit the required log archive."
            )
        ),
    )
    value = source.model_dump(mode="python")
    value.update(
        {
            "job_id": job.job_id,
            "case_id": job.case_id,
            "job_type": job.job_type,
            "base_state_revision": job.base_state_revision,
            "result_type": (
                OutcomeResultType.NEED_INPUT
                if missing_user_inputs
                else OutcomeResultType.NEED_ATTACHMENT
            ),
            "payload": payload,
            "consumed_evidence_refs": [],
            "proposed_evidence": [],
            "proposed_artifacts": [],
            "error": None,
            "decision_audit": None,
        }
    )
    return JobOutcome.model_validate(value)


def _job_root(tmp_path: Path) -> tuple[Path, Job]:
    job = _preflight_job()
    root = tmp_path / "jobs" / job.job_id
    root.mkdir(parents=True)
    _write_canonical(root / "job.json", job)
    return root, job


def _valid_preflight_root(
    tmp_path: Path,
    *,
    missing_user_inputs: tuple[str, ...] = (),
    missing_log_archive: bool = True,
) -> tuple[Path, Job, JobOutcome, dict[str, object]]:
    root, job = _job_root(tmp_path)
    outcome = _preflight_outcome(
        job,
        missing_user_inputs=missing_user_inputs,
        missing_log_archive=missing_log_archive,
    )
    result_type = "NEED_INPUT" if missing_user_inputs else "NEED_ATTACHMENT"
    audit: dict[str, object] = {
        "schema_version": 1,
        "job_id": job.job_id,
        "registration_id": REGISTRATION_ID,
        "result_type": result_type,
        "missing_user_inputs": list(missing_user_inputs),
        "missing_artifacts": ["log_archive"] if missing_log_archive else [],
    }
    _write_canonical(root / "job_outcome.json", outcome)
    _write_canonical(root / "methods_preflight.json", audit)
    return root, job, outcome, audit


def _model_events(*, total_tokens_seed: int) -> list[dict[str, object]]:
    return [
        {"type": "system", "subtype": "init", "model": MODEL},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 1,
            "total_cost_usd": 0.25,
            "usage": {
                "input_tokens": total_tokens_seed,
                "output_tokens": 2,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 4,
            },
        },
    ]


def test_absent_diagnose_stream_is_sealed_as_methods_no_model_preflight(
    tmp_path: Path,
) -> None:
    root, job, outcome, audit = _valid_preflight_root(tmp_path)

    receipt = _run_audit(tmp_path)

    assert receipt["invocations"] == []
    assert receipt["new_job_ids"] == [job.job_id]
    assert receipt["no_model_jobs"] == [
        {
            "schema_version": 2,
            "kind": "methods-server-preflight",
            "job_id": job.job_id,
            "job_type": "DIAGNOSE",
            "result_type": "NEED_ATTACHMENT",
            "registration_id": REGISTRATION_ID,
            "decision_audit_absent": True,
            "model_invoked": False,
            "log_pair": "ABSENT",
            "job_sha256": hashlib.sha256(canonical_json_bytes(job)).hexdigest(),
            "job_outcome_sha256": hashlib.sha256(
                canonical_json_bytes(outcome)
            ).hexdigest(),
            "methods_preflight_sha256": hashlib.sha256(
                canonical_json_bytes(audit)
            ).hexdigest(),
        }
    ]
    assert {path.name for path in root.iterdir()} == {
        "job.json",
        "job_outcome.json",
        "methods_preflight.json",
    }


def test_need_input_only_preflight_is_sealed_without_model_logs(
    tmp_path: Path,
) -> None:
    _, job, _, _ = _valid_preflight_root(
        tmp_path,
        missing_user_inputs=("service",),
        missing_log_archive=False,
    )

    receipt = _run_audit(tmp_path)

    assert receipt["new_job_ids"] == [job.job_id]
    assert receipt["no_model_jobs"][0]["result_type"] == "NEED_INPUT"
    assert receipt["no_model_jobs"][0]["registration_id"] == REGISTRATION_ID


def test_need_input_and_attachment_preflight_is_sealed_in_product_order(
    tmp_path: Path,
) -> None:
    _, job, outcome, audit = _valid_preflight_root(
        tmp_path,
        missing_user_inputs=("service",),
        missing_log_archive=True,
    )

    receipt = _run_audit(tmp_path)

    assert receipt["new_job_ids"] == [job.job_id]
    assert receipt["no_model_jobs"][0]["result_type"] == "NEED_INPUT"
    assert audit["missing_user_inputs"] == ["service"]
    assert audit["missing_artifacts"] == ["log_archive"]
    assert isinstance(outcome.payload, DiagnosisOutcome)
    assert [
        requirement.name
        for requirement in outcome.payload.state_delta.add_pending_requirements
    ] == ["service", "log_archive"]


def test_need_input_preflight_rejects_wrong_result_type(tmp_path: Path) -> None:
    root, job, _, audit = _valid_preflight_root(
        tmp_path,
        missing_user_inputs=("service",),
        missing_log_archive=False,
    )
    _write_canonical(
        root / "methods_preflight.json",
        {**audit, "result_type": "NEED_ATTACHMENT"},
    )
    output = tmp_path / "service-agent-usage.json"

    result = _invoke_audit(tmp_path, output)

    assert result.returncode != 0
    assert (
        "SERVICE_AGENT_USAGE_AUDIT_FAILED:"
        f"METHODS_PREFLIGHT_RECEIPT_INVALID:{job.job_id}"
    ) in result.stderr
    assert not output.exists()


def test_missing_model_stream_without_methods_preflight_fails_closed(
    tmp_path: Path,
) -> None:
    _, job = _job_root(tmp_path)
    output = tmp_path / "service-agent-usage.json"

    result = _invoke_audit(tmp_path, output)

    assert result.returncode != 0
    assert (
        f"SERVICE_AGENT_USAGE_AUDIT_FAILED:MODEL_STDOUT_MISSING:{job.job_id}"
        in result.stderr
    )
    assert not output.exists()


def test_absent_stream_with_invalid_methods_preflight_fails_closed(
    tmp_path: Path,
) -> None:
    root, job = _job_root(tmp_path)
    _write_canonical(
        root / "job_outcome.json",
        {
            "job_id": job.job_id,
            "result_type": "NEED_ATTACHMENT",
        },
    )
    _write_canonical(
        root / "methods_preflight.json",
        {
            "schema_version": 1,
            "job_id": job.job_id,
            "result_type": "NEED_ATTACHMENT",
            "registration_id": REGISTRATION_ID,
            "missing_user_inputs": [],
            "missing_artifacts": ["log_archive"],
        },
    )
    output = tmp_path / "service-agent-usage.json"

    result = _invoke_audit(tmp_path, output)

    assert result.returncode != 0
    assert (
        "SERVICE_AGENT_USAGE_AUDIT_FAILED:"
        f"METHODS_PREFLIGHT_OUTCOME:{job.job_id}_INVALID"
    ) in result.stderr
    assert not output.exists()


def test_one_sided_execution_log_pair_fails_closed(tmp_path: Path) -> None:
    root, job, _, _ = _valid_preflight_root(tmp_path)
    (root / "stdout.log").write_text("", encoding="utf-8")
    output = tmp_path / "service-agent-usage.json"

    result = _invoke_audit(tmp_path, output)

    assert result.returncode != 0
    assert (
        f"SERVICE_AGENT_USAGE_AUDIT_FAILED:MODEL_LOG_PAIR_INVALID:{job.job_id}"
        in result.stderr
    )
    assert not output.exists()


def test_hardlinked_job_record_fails_closed(tmp_path: Path) -> None:
    root, job, _, _ = _valid_preflight_root(tmp_path)
    os.link(root / "job.json", tmp_path / "hardlinked-job.json")
    output = tmp_path / "service-agent-usage.json"

    result = _invoke_audit(tmp_path, output)

    assert result.returncode != 0
    assert (
        "SERVICE_AGENT_USAGE_AUDIT_FAILED:"
        f"JOB_RECORD:{job.job_id}_NOT_ORDINARY"
    ) in result.stderr
    assert not output.exists()


def test_hardlinked_model_log_fails_closed(tmp_path: Path) -> None:
    root, job = _job_root(tmp_path)
    (root / "stdout.log").write_text(
        "".join(
            json.dumps(event, separators=(",", ":")) + "\n"
            for event in _model_events(total_tokens_seed=10)
        ),
        encoding="utf-8",
    )
    (root / "stderr.log").write_text("", encoding="utf-8")
    os.link(root / "stdout.log", tmp_path / "hardlinked-stdout.log")
    output = tmp_path / "service-agent-usage.json"

    result = _invoke_audit(tmp_path, output)

    assert result.returncode != 0
    assert (
        "SERVICE_AGENT_USAGE_AUDIT_FAILED:"
        f"MODEL_STDOUT:{job.job_id}_NOT_ORDINARY"
    ) in result.stderr
    assert not output.exists()


def test_empty_execution_log_pair_cannot_impersonate_preflight(tmp_path: Path) -> None:
    root, job, _, _ = _valid_preflight_root(tmp_path)
    (root / "stdout.log").write_text("", encoding="utf-8")
    (root / "stderr.log").write_text("", encoding="utf-8")
    output = tmp_path / "service-agent-usage.json"

    result = _invoke_audit(tmp_path, output)

    assert result.returncode != 0
    assert (
        f"SERVICE_AGENT_USAGE_AUDIT_FAILED:MODEL_USAGE_STREAM_INVALID:{job.job_id}"
        in result.stderr
    )
    assert not output.exists()


def test_model_stream_with_methods_preflight_fails_closed(tmp_path: Path) -> None:
    root, job, _, _ = _valid_preflight_root(tmp_path)
    (root / "stdout.log").write_text(
        "".join(
            json.dumps(event, separators=(",", ":")) + "\n"
            for event in _model_events(total_tokens_seed=10)
        ),
        encoding="utf-8",
    )
    (root / "stderr.log").write_text("", encoding="utf-8")
    output = tmp_path / "service-agent-usage.json"

    result = _invoke_audit(tmp_path, output)

    assert result.returncode != 0
    assert (
        "SERVICE_AGENT_USAGE_AUDIT_FAILED:"
        f"METHODS_PREFLIGHT_WITH_MODEL_STREAM:{job.job_id}"
    ) in result.stderr
    assert not output.exists()


def test_preflight_file_set_fails_closed_on_agent_context(tmp_path: Path) -> None:
    root, job, _, audit = _valid_preflight_root(tmp_path)
    (root / "context.txt").write_text("forbidden agent context", encoding="utf-8")
    output = tmp_path / "service-agent-usage.json"

    result = _invoke_audit(tmp_path, output)

    assert result.returncode != 0
    assert (
        "SERVICE_AGENT_USAGE_AUDIT_FAILED:"
        f"METHODS_PREFLIGHT_FILE_SET_INVALID:{job.job_id}"
    ) in result.stderr
    assert not output.exists()


def test_preflight_closed_schema_rejects_extra_key(tmp_path: Path) -> None:
    root, job, _, audit = _valid_preflight_root(tmp_path)
    _write_canonical(root / "methods_preflight.json", {**audit, "unexpected": True})
    output = tmp_path / "service-agent-usage.json"

    result = _invoke_audit(tmp_path, output)

    assert result.returncode != 0
    assert (
        "SERVICE_AGENT_USAGE_AUDIT_FAILED:"
        f"METHODS_PREFLIGHT_RECEIPT_INVALID:{job.job_id}"
    ) in result.stderr
    assert not output.exists()


def test_preflight_registration_linkage_fails_closed(tmp_path: Path) -> None:
    root, job, _, audit = _valid_preflight_root(tmp_path)
    _write_canonical(
        root / "methods_preflight.json",
        {
            **audit,
            "registration_id": "another-methods-v1",
        },
    )
    output = tmp_path / "service-agent-usage.json"

    result = _invoke_audit(tmp_path, output)

    assert result.returncode != 0
    assert (
        "SERVICE_AGENT_USAGE_AUDIT_FAILED:"
        f"METHODS_PREFLIGHT_RECEIPT_INVALID:{job.job_id}"
    ) in result.stderr
    assert not output.exists()


def test_preflight_artifact_name_fails_closed(tmp_path: Path) -> None:
    root, job, _, audit = _valid_preflight_root(tmp_path)
    _write_canonical(
        root / "methods_preflight.json",
        {**audit, "missing_artifacts": ["rpc_log"]},
    )
    output = tmp_path / "service-agent-usage.json"

    result = _invoke_audit(tmp_path, output)

    assert result.returncode != 0
    assert (
        "SERVICE_AGENT_USAGE_AUDIT_FAILED:"
        f"METHODS_PREFLIGHT_RECEIPT_INVALID:{job.job_id}"
    ) in result.stderr
    assert not output.exists()


def test_preflight_outcome_requirement_linkage_fails_closed(tmp_path: Path) -> None:
    root, job, outcome, _ = _valid_preflight_root(tmp_path)
    value = outcome.model_dump(mode="json")
    value["payload"]["requested_attachments"] = []
    _write_canonical(root / "job_outcome.json", value)
    output = tmp_path / "service-agent-usage.json"

    result = _invoke_audit(tmp_path, output)

    assert result.returncode != 0
    assert (
        "SERVICE_AGENT_USAGE_AUDIT_FAILED:"
        f"METHODS_PREFLIGHT_OUTCOME:{job.job_id}_INVALID"
    ) in result.stderr
    assert not output.exists()


def test_two_methods_passes_in_one_diagnose_job_emit_two_exact_invocations(
    tmp_path: Path,
) -> None:
    root, job = _job_root(tmp_path)
    events = [*_model_events(total_tokens_seed=10), *_model_events(total_tokens_seed=20)]
    (root / "stdout.log").write_text(
        "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
        encoding="utf-8",
    )
    (root / "stderr.log").write_text("", encoding="utf-8")

    receipt = _run_audit(tmp_path)

    assert receipt["no_model_jobs"] == []
    assert receipt["new_job_ids"] == [job.job_id]
    assert [item["invocation_id"] for item in receipt["invocations"]] == [
        f"server-agent:{job.job_id}:1",
        f"server-agent:{job.job_id}:2",
    ]
    assert [item["job_invocation_ordinal"] for item in receipt["invocations"]] == [1, 2]
    assert [item["job_invocation_count"] for item in receipt["invocations"]] == [2, 2]
    assert [item["usage"]["total_tokens"] for item in receipt["invocations"]] == [19, 29]
