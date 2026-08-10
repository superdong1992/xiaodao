from __future__ import annotations

"""Create a bounded per-invocation receipt from persisted Agent stdout logs."""

import argparse
import json
import os
from pathlib import Path
from typing import Any


REAL_JOB_TYPES = {"ROUTE", "DIAGNOSE", "REVIEW"}


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


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON_OBJECT_REQUIRED:{path.name}")
    return value


def _invocation(job_root: Path, arguments: argparse.Namespace) -> dict[str, Any] | None:
    job = _read_json(job_root / "job.json")
    job_type = job.get("job_type")
    job_id = job.get("job_id")
    if job_type not in REAL_JOB_TYPES or not isinstance(job_id, str):
        return None
    if job_id in arguments.exclude_job_id:
        return None
    lines = [
        json.loads(line)
        for line in (job_root / "stdout.log").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    init = [
        event
        for event in lines
        if event.get("type") == "system" and event.get("subtype") == "init"
    ]
    terminal = [event for event in lines if event.get("type") == "result"]
    if len(init) != 1 or len(terminal) != 1 or lines[-1].get("type") != "result":
        raise RuntimeError(f"MODEL_USAGE_STREAM_INVALID:{job_id}")
    final = terminal[0]
    usage = final.get("usage")
    if not isinstance(usage, dict):
        raise RuntimeError(f"MODEL_USAGE_MISSING:{job_id}")
    observed = {
        "input_tokens": int(usage.get("input_tokens", -1)),
        "output_tokens": int(usage.get("output_tokens", -1)),
        "cost_usd": float(final.get("total_cost_usd", final.get("cost_usd", -1))),
    }
    if min(observed["input_tokens"], observed["output_tokens"]) < 0:
        raise RuntimeError(f"MODEL_USAGE_INVALID:{job_id}")
    if observed["cost_usd"] < 0:
        raise RuntimeError(f"MODEL_COST_INVALID:{job_id}")
    if observed["input_tokens"] + observed["output_tokens"] > arguments.max_total_tokens:
        raise RuntimeError(f"MODEL_TOKEN_CAP_EXCEEDED:{job_id}")
    if observed["cost_usd"] > arguments.max_budget_usd:
        raise RuntimeError(f"MODEL_BUDGET_CAP_EXCEEDED:{job_id}")
    if init[0].get("model") != arguments.model:
        raise RuntimeError(f"MODEL_IDENTITY_MISMATCH:{job_id}")
    turns = final.get("num_turns")
    if (
        final.get("subtype") != "success"
        or final.get("is_error") is not False
        or not isinstance(turns, int)
        or isinstance(turns, bool)
        or turns <= 0
        or turns > arguments.max_turns
    ):
        raise RuntimeError(f"MODEL_TERMINAL_INVALID:{job_id}")
    return {
        "invocation_id": f"server-agent:{job_id}",
        "class": "server-agent",
        "job_id": job_id,
        "job_type": job_type,
        "effective_model": init[0]["model"],
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
        "hard_cap_enforcement": {
            "turns": "claude-cli",
            "cost_usd": "claude-cli",
            "hard_timeout_seconds": "service-process-timeout",
            "total_tokens": "terminal-usage-postcondition",
        },
    }


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
    invocations = [
        invocation
        for root in roots
        if (invocation := _invocation(root, arguments)) is not None
    ]
    _write_new(
        arguments.output,
        {
            "schema_version": 2,
            "status": "PASS",
            "invocations": invocations,
            "new_job_ids": [invocation["job_id"] for invocation in invocations],
        },
    )


try:
    main()
except Exception as error:
    raise SystemExit(f"SERVICE_AGENT_USAGE_AUDIT_FAILED:{error}") from None
