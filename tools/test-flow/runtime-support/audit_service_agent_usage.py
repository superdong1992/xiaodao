from __future__ import annotations

"""Create a bounded per-invocation receipt from persisted Agent stdout logs."""

import argparse
import json
import math
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


def _stream_segments(
    lines: list[dict[str, Any]], job_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Return complete top-level Claude stream segments for one process.

    Claude 2.1.89 can finish the initial turn while a background Bash task is
    still running, then append ``task_notification -> init -> result`` to the
    same stream and session.  Each segment must still be complete and ordered;
    this is not permission to accept duplicate or unrelated invocations.
    """

    if not lines or lines[-1].get("type") != "result":
        raise RuntimeError(f"MODEL_USAGE_STREAM_INVALID:{job_id}")
    initializers: list[dict[str, Any]] = []
    terminals: list[dict[str, Any]] = []
    active = False
    session_id: str | None = None
    for event in lines:
        if not isinstance(event, dict):
            raise RuntimeError(f"MODEL_USAGE_STREAM_INVALID:{job_id}")
        if event.get("type") == "system" and event.get("subtype") == "init":
            if active:
                raise RuntimeError(f"MODEL_USAGE_STREAM_INVALID:{job_id}")
            observed_session = event.get("session_id")
            if not isinstance(observed_session, str) or not observed_session:
                raise RuntimeError(f"MODEL_USAGE_STREAM_INVALID:{job_id}")
            if session_id is None:
                session_id = observed_session
            elif observed_session != session_id:
                raise RuntimeError(f"MODEL_USAGE_STREAM_INVALID:{job_id}")
            initializers.append(event)
            active = True
        elif event.get("type") == "result":
            if not active or event.get("session_id") != session_id:
                raise RuntimeError(f"MODEL_USAGE_STREAM_INVALID:{job_id}")
            terminals.append(event)
            active = False
    if active or not initializers or len(initializers) != len(terminals):
        raise RuntimeError(f"MODEL_USAGE_STREAM_INVALID:{job_id}")
    assert session_id is not None
    return initializers, terminals, session_id


def _usage_integer(value: object, job_id: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"MODEL_USAGE_INVALID:{job_id}")
    return value


def _cost_number(value: object, job_id: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise RuntimeError(f"MODEL_COST_INVALID:{job_id}")
    return float(value)


def _invocation(
    job_root: Path, arguments: argparse.Namespace
) -> dict[str, Any] | None:
    job = _read_json(job_root / "job.json")
    job_type = job.get("job_type")
    job_id = job.get("job_id")
    if job_type not in REAL_JOB_TYPES or not isinstance(job_id, str):
        return None
    if job_id in arguments.exclude_job_id:
        return None
    lines = []
    for line in (job_root / "stdout.log").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict):
            raise RuntimeError(f"MODEL_USAGE_STREAM_INVALID:{job_id}")
        lines.append(event)
    initializers, terminals, session_id = _stream_segments(lines, job_id)
    input_tokens = 0
    output_tokens = 0
    turns = 0
    cumulative_costs: list[float] = []
    segment_costs: list[float] = []
    for final in terminals:
        usage = final.get("usage")
        if not isinstance(usage, dict):
            raise RuntimeError(f"MODEL_USAGE_MISSING:{job_id}")
        input_tokens += _usage_integer(usage.get("input_tokens"), job_id)
        output_tokens += _usage_integer(usage.get("output_tokens"), job_id)
        terminal_turns = final.get("num_turns")
        if (
            final.get("subtype") != "success"
            or final.get("is_error") is not False
            or not isinstance(terminal_turns, int)
            or isinstance(terminal_turns, bool)
            or terminal_turns <= 0
        ):
            raise RuntimeError(f"MODEL_TERMINAL_INVALID:{job_id}")
        turns += terminal_turns
        if "total_cost_usd" in final:
            cumulative_costs.append(_cost_number(final["total_cost_usd"], job_id))
        elif "cost_usd" in final:
            segment_costs.append(_cost_number(final["cost_usd"], job_id))
        else:
            raise RuntimeError(f"MODEL_COST_INVALID:{job_id}")
    if cumulative_costs and segment_costs:
        raise RuntimeError(f"MODEL_COST_INVALID:{job_id}")
    if cumulative_costs:
        if len(cumulative_costs) != len(terminals) or any(
            current < previous
            for previous, current in zip(cumulative_costs, cumulative_costs[1:])
        ):
            raise RuntimeError(f"MODEL_COST_INVALID:{job_id}")
        cost_usd = cumulative_costs[-1]
        cost_accounting = "cumulative-terminal"
    else:
        if len(segment_costs) != len(terminals):
            raise RuntimeError(f"MODEL_COST_INVALID:{job_id}")
        cost_usd = sum(segment_costs)
        cost_accounting = "segment-sum"
    observed = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
    }
    if (
        observed["input_tokens"] + observed["output_tokens"]
        > arguments.max_total_tokens
    ):
        raise RuntimeError(f"MODEL_TOKEN_CAP_EXCEEDED:{job_id}")
    if observed["cost_usd"] > arguments.max_budget_usd:
        raise RuntimeError(f"MODEL_BUDGET_CAP_EXCEEDED:{job_id}")
    if any(
        initializer.get("model") != arguments.model
        for initializer in initializers
    ):
        raise RuntimeError(f"MODEL_IDENTITY_MISMATCH:{job_id}")
    if turns > arguments.max_turns:
        raise RuntimeError(f"MODEL_TURN_CAP_EXCEEDED:{job_id}")
    final = terminals[-1]
    return {
        "invocation_id": f"server-agent:{job_id}",
        "class": "server-agent",
        "job_id": job_id,
        "job_type": job_type,
        "effective_model": initializers[0]["model"],
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
        "stream_segments": len(terminals),
        "session_id": session_id,
        "cost_accounting": cost_accounting,
        "hard_cap_enforcement": {
            "turns": "claude-cli-plus-aggregate-terminal-postcondition",
            "cost_usd": "claude-cli-plus-terminal-postcondition",
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
    roots = (
        sorted(
            entry
            for entry in arguments.jobs_root.iterdir()
            if entry.is_dir() and (entry / "job.json").is_file()
        )
        if arguments.jobs_root.is_dir()
        else []
    )
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
