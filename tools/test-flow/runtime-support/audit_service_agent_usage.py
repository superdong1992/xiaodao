from __future__ import annotations

"""Create a bounded per-invocation receipt from persisted Agent stdout logs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from problem_locator.contracts.enums import (
    DiagnosisMode,
    JobStatus,
    JobType,
    OutcomeResultType,
    RequirementKind,
    RequirementStatus,
    SupplementPolicy,
)
from problem_locator.contracts.models import DiagnosisOutcome, Job, JobOutcome
from problem_locator.contracts.outcomes import validate_outcome_for_job
from problem_locator.contracts.serialization import parse_canonical_json_bytes


REAL_JOB_TYPES = {"ROUTE", "DIAGNOSE", "REVIEW"}
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
TOKEN_FORMULA = "+".join(TOKEN_FIELDS)
PREFLIGHT_KEYS = frozenset(
    {
        "schema_version",
        "job_id",
        "registration_id",
        "result_type",
        "missing_user_inputs",
        "missing_artifacts",
    }
)
PREFLIGHT_JOB_FILES = frozenset(
    {"job.json", "job_outcome.json", "methods_preflight.json"}
)
REGISTRATION_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
NON_PREFLIGHT_DELTA_FIELDS = (
    "add_user_facts",
    "proposed_facts",
    "add_active_hypotheses",
    "update_hypotheses",
    "reject_hypotheses",
    "add_open_questions",
    "resolve_questions",
    "fulfill_requirements",
    "add_evidence_bindings",
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-turns", type=int, required=True)
    parser.add_argument("--max-total-tokens", type=int, required=True)
    parser.add_argument("--max-budget-usd", type=float, required=True)
    parser.add_argument("--hard-timeout-seconds", type=int, required=True)
    parser.add_argument("--exclude-job-id", action="append", default=[])
    return parser.parse_args()


def _regular_bytes(path: Path, code: str) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise RuntimeError(f"{code}_MISSING") from None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError(f"{code}_NOT_ORDINARY")
    return path.read_bytes()


def _regular_file_present(path: Path, code: str) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError(f"{code}_NOT_ORDINARY")
    return True


def _contract_file(
    path: Path,
    model_type: type[Job] | type[JobOutcome],
    code: str,
) -> tuple[Job | JobOutcome, bytes]:
    raw = _regular_bytes(path, code)
    try:
        return parse_canonical_json_bytes(raw, model_type), raw
    except Exception:
        raise RuntimeError(f"{code}_INVALID") from None


def _model_invocation(
    *,
    job_id: str,
    job_type: str,
    init: dict[str, Any],
    final: dict[str, Any],
    ordinal: int,
    count: int,
    arguments: argparse.Namespace,
) -> dict[str, Any]:
    usage = final.get("usage")
    if not isinstance(usage, dict):
        raise RuntimeError(f"MODEL_USAGE_MISSING:{job_id}:{ordinal}")
    token_values = {name: usage.get(name) for name in TOKEN_FIELDS}
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in token_values.values()
    ):
        raise RuntimeError(f"MODEL_USAGE_INVALID:{job_id}:{ordinal}")
    cost = final.get("total_cost_usd", final.get("cost_usd"))
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0:
        raise RuntimeError(f"MODEL_COST_INVALID:{job_id}:{ordinal}")
    observed = {
        "schema_version": 1,
        **token_values,
        "total_tokens": sum(token_values.values()),
        "cost_usd": round(float(cost), 6),
    }
    if observed["total_tokens"] > arguments.max_total_tokens:
        raise RuntimeError(f"MODEL_TOKEN_CAP_EXCEEDED:{job_id}:{ordinal}")
    if observed["cost_usd"] > arguments.max_budget_usd:
        raise RuntimeError(f"MODEL_BUDGET_CAP_EXCEEDED:{job_id}:{ordinal}")
    if init.get("model") != arguments.model:
        raise RuntimeError(f"MODEL_IDENTITY_MISMATCH:{job_id}:{ordinal}")
    turns = final.get("num_turns")
    if (
        final.get("subtype") != "success"
        or final.get("is_error") is not False
        or not isinstance(turns, int)
        or isinstance(turns, bool)
        or turns <= 0
        or turns > arguments.max_turns
    ):
        raise RuntimeError(f"MODEL_TERMINAL_INVALID:{job_id}:{ordinal}")
    return {
        "schema_version": 3,
        "invocation_id": f"server-agent:{job_id}:{ordinal}",
        "class": "server-agent",
        "job_id": job_id,
        "job_type": job_type,
        "job_invocation_ordinal": ordinal,
        "job_invocation_count": count,
        "effective_model": init["model"],
        "effective_caps": {
            "max_turns": arguments.max_turns,
            "max_total_tokens": arguments.max_total_tokens,
            "max_budget_usd": arguments.max_budget_usd,
            "hard_timeout_seconds": arguments.hard_timeout_seconds,
        },
        "usage_complete": True,
        "usage": observed,
        "terminal": {
            "subtype": final.get("subtype"),
            "is_error": final.get("is_error"),
        },
        "turns": turns,
        "wrapper_outcome": {
            "schema_version": 1,
            "status": "PASS",
            "code": None,
        },
        "hard_cap_enforcement": {
            "turns": "claude-cli",
            "cost_usd": "claude-cli",
            "hard_timeout_seconds": "service-process-timeout",
            "total_tokens": f"terminal-usage-postcondition:{TOKEN_FORMULA}",
        },
    }


def _methods_preflight(
    job_root: Path,
    job: Job,
    job_bytes: bytes,
) -> dict[str, Any]:
    job_id = job.job_id
    if {entry.name for entry in job_root.iterdir()} != PREFLIGHT_JOB_FILES:
        raise RuntimeError(f"METHODS_PREFLIGHT_FILE_SET_INVALID:{job_id}")
    outcome_value, outcome_bytes = _contract_file(
        job_root / "job_outcome.json",
        JobOutcome,
        f"METHODS_PREFLIGHT_OUTCOME:{job_id}",
    )
    assert isinstance(outcome_value, JobOutcome)
    outcome = outcome_value
    try:
        validate_outcome_for_job(job, outcome)
    except Exception:
        raise RuntimeError(f"METHODS_PREFLIGHT_JOB_OUTCOME_INVALID:{job_id}") from None
    audit_path = job_root / "methods_preflight.json"
    audit_bytes = _regular_bytes(
        audit_path,
        f"METHODS_PREFLIGHT_RECEIPT:{job_id}",
    )
    try:
        audit = parse_canonical_json_bytes(audit_bytes)
    except Exception:
        raise RuntimeError(f"METHODS_PREFLIGHT_RECEIPT_INVALID:{job_id}") from None
    missing_user_inputs = audit.get("missing_user_inputs") if isinstance(audit, dict) else None
    missing_artifacts = audit.get("missing_artifacts") if isinstance(audit, dict) else None
    registration_id = audit.get("registration_id") if isinstance(audit, dict) else None
    payload = outcome.payload
    requirements = (
        payload.state_delta.add_pending_requirements
        if isinstance(payload, DiagnosisOutcome)
        else []
    )
    input_requirements = [
        requirement
        for requirement in requirements
        if requirement.kind is RequirementKind.INPUT
    ]
    attachment_requirements = [
        requirement
        for requirement in requirements
        if requirement.kind is RequirementKind.ATTACHMENT
    ]
    result_type = outcome.result_type
    if (
        job.status is not JobStatus.PENDING
        or job.job_type is not JobType.DIAGNOSE
        or job.diagnosis_mode is not DiagnosisMode.SPECIALIZED
        or job.skill_ref is None
        or outcome.job_type is not JobType.DIAGNOSE
        or result_type not in {
            OutcomeResultType.NEED_INPUT,
            OutcomeResultType.NEED_ATTACHMENT,
        }
        or outcome.decision_audit is not None
        or outcome.error is not None
        or outcome.consumed_evidence_refs
        or outcome.proposed_evidence
        or outcome.proposed_artifacts
        or not isinstance(payload, DiagnosisOutcome)
        or payload.findings
        or payload.candidate_conclusion_draft is not None
        or payload.limitations
        or payload.safety_notes
        or payload.state_delta.problem_spec_patch is not None
        or any(getattr(payload.state_delta, name) for name in NON_PREFLIGHT_DELTA_FIELDS)
        or not isinstance(audit, dict)
        or frozenset(audit) != PREFLIGHT_KEYS
        or audit.get("schema_version") != 1
        or audit.get("job_id") != job_id
        or audit.get("result_type") != result_type.value
        or not isinstance(registration_id, str)
        or REGISTRATION_ID.fullmatch(registration_id) is None
        or job.skill_ref.id != f"diagnosis-skill/{registration_id}"
        or not isinstance(missing_user_inputs, list)
        or any(
            not isinstance(name, str) or not name or name.strip() != name
            for name in (missing_user_inputs or [])
        )
        or len(set(missing_user_inputs or [])) != len(missing_user_inputs or [])
        or missing_artifacts not in ([], ["log_archive"])
        or not ((missing_user_inputs or []) or (missing_artifacts or []))
        or result_type
        is not (
            OutcomeResultType.NEED_ATTACHMENT
            if not missing_user_inputs
            else OutcomeResultType.NEED_INPUT
        )
        or [requirement.name for requirement in input_requirements]
        != missing_user_inputs
        or [requirement.name for requirement in attachment_requirements]
        != missing_artifacts
        or len(requirements)
        != len(input_requirements) + len(attachment_requirements)
        or any(
            not requirement.required
            or requirement.status is not RequirementStatus.OPEN
            or requirement.requested_by_job_id != job_id
            or requirement.fulfilled_by_refs
            or requirement.supplement_policy is not SupplementPolicy.MISSING_ONLY
            for requirement in requirements
        )
        or payload.requested_input
        != [requirement.requirement_id for requirement in input_requirements]
        or payload.requested_attachments
        != [requirement.requirement_id for requirement in attachment_requirements]
    ):
        raise RuntimeError(f"METHODS_PREFLIGHT_RECEIPT_INVALID:{job_id}")
    return {
        "schema_version": 2,
        "kind": "methods-server-preflight",
        "job_id": job_id,
        "job_type": "DIAGNOSE",
        "result_type": result_type.value,
        "registration_id": registration_id,
        "decision_audit_absent": True,
        "model_invoked": False,
        "log_pair": "ABSENT",
        "job_sha256": hashlib.sha256(job_bytes).hexdigest(),
        "job_outcome_sha256": hashlib.sha256(outcome_bytes).hexdigest(),
        "methods_preflight_sha256": hashlib.sha256(audit_bytes).hexdigest(),
    }


def _job_evidence(
    job_root: Path,
    arguments: argparse.Namespace,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    job_value, job_bytes = _contract_file(
        job_root / "job.json",
        Job,
        f"JOB_RECORD:{job_root.name}",
    )
    assert isinstance(job_value, Job)
    job = job_value
    job_type = job.job_type.value
    job_id = job.job_id
    if job_type not in REAL_JOB_TYPES:
        return [], []
    if job_id in arguments.exclude_job_id:
        return [], []
    stdout_path = job_root / "stdout.log"
    stderr_path = job_root / "stderr.log"
    stdout_exists = _regular_file_present(stdout_path, f"MODEL_STDOUT:{job_id}")
    stderr_exists = _regular_file_present(stderr_path, f"MODEL_STDERR:{job_id}")
    if stdout_exists != stderr_exists:
        raise RuntimeError(f"MODEL_LOG_PAIR_INVALID:{job_id}")
    if not stdout_exists:
        if not _regular_file_present(
            job_root / "methods_preflight.json",
            f"METHODS_PREFLIGHT_RECEIPT:{job_id}",
        ):
            raise RuntimeError(f"MODEL_STDOUT_MISSING:{job_id}")
        return [], [_methods_preflight(job_root, job, job_bytes)]
    lines = [
        json.loads(line)
        for line in stdout_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not lines:
        raise RuntimeError(f"MODEL_USAGE_STREAM_INVALID:{job_id}")
    if os.path.lexists(job_root / "methods_preflight.json"):
        raise RuntimeError(f"METHODS_PREFLIGHT_WITH_MODEL_STREAM:{job_id}")
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    active_init: dict[str, Any] | None = None
    for event in lines:
        is_init = event.get("type") == "system" and event.get("subtype") == "init"
        is_terminal = event.get("type") == "result"
        if is_init:
            if active_init is not None:
                raise RuntimeError(f"MODEL_USAGE_STREAM_INVALID:{job_id}")
            active_init = event
        elif is_terminal:
            if active_init is None:
                raise RuntimeError(f"MODEL_USAGE_STREAM_INVALID:{job_id}")
            pairs.append((active_init, event))
            active_init = None
        elif active_init is None:
            raise RuntimeError(f"MODEL_USAGE_STREAM_INVALID:{job_id}")
    if active_init is not None or not pairs or lines[-1].get("type") != "result":
        raise RuntimeError(f"MODEL_USAGE_STREAM_INVALID:{job_id}")
    count = len(pairs)
    return [
        _model_invocation(
            job_id=job_id,
            job_type=job_type,
            init=init,
            final=final,
            ordinal=ordinal,
            count=count,
            arguments=arguments,
        )
        for ordinal, (init, final) in enumerate(pairs, start=1)
    ], []


def _write_new(path: Path, value: object) -> None:
    payload = (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> None:
    arguments = _arguments()
    if (
        arguments.max_turns <= 0
        or arguments.max_total_tokens <= 0
        or arguments.max_budget_usd <= 0
        or arguments.hard_timeout_seconds <= 0
    ):
        raise RuntimeError("SERVICE_AGENT_CAP_INVALID")
    roots = sorted(
        entry
        for entry in arguments.jobs_root.iterdir()
        if entry.is_dir() and (entry / "job.json").is_file()
    ) if arguments.jobs_root.is_dir() else []
    evidence = [_job_evidence(root, arguments) for root in roots]
    invocations = [item for model_items, _ in evidence for item in model_items]
    no_model_jobs = [item for _, preflights in evidence for item in preflights]
    _write_new(
        arguments.output,
        {
            "schema_version": 3,
            "status": "PASS",
            "usage_complete": True,
            "token_formula": TOKEN_FORMULA,
            "invocations": invocations,
            "no_model_jobs": no_model_jobs,
            "new_job_ids": sorted(
                {item["job_id"] for item in [*invocations, *no_model_jobs]}
            ),
        },
    )


try:
    main()
except Exception as error:
    raise SystemExit(f"SERVICE_AGENT_USAGE_AUDIT_FAILED:{error}") from None
