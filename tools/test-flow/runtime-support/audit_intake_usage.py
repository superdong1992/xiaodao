"""Audit actual final CLI streams from no-Job INTAKE workspaces."""
from __future__ import annotations
import argparse
import importlib.util
import json
from pathlib import Path
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspaces-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-turns", type=int, required=True)
    parser.add_argument("--max-total-tokens", type=int, required=True)
    parser.add_argument("--max-budget-usd", type=float, required=True)
    parser.add_argument("--hard-timeout-seconds", type=int, required=True)
    parser.add_argument("--exclude-execution-id", action="append", default=[])
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("usage_boundary", Path(__file__).with_name("audit_service_agent_usage.py"))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    invocations = []
    for root in sorted(args.workspaces_root.iterdir()):
        stream = root / "runtime" / "stdout.log"
        if not stream.exists() or root.name in args.exclude_execution_id:
            continue
        if str(uuid.UUID(root.name)) != root.name:
            raise ValueError("INTAKE_WORKSPACE_ID_INVALID")
        raw = helper._regular_bytes(stream, "INTAKE_STDOUT")
        helper._regular_bytes(root / "runtime" / "stderr.log", "INTAKE_STDERR")
        lines = [json.loads(line) for line in raw.splitlines() if line.strip()]
        inits = [line for line in lines if line.get("type") == "system" and line.get("subtype") == "init"]
        terminals = [line for line in lines if line.get("type") == "result"]
        if len(inits) != 1 or len(terminals) != 1 or lines[-1] is not terminals[0]:
            raise ValueError("INTAKE_REPAIR_OR_INCOMPLETE_STREAM")
        if inits[0].get("tools") != []:
            raise ValueError("INTAKE_TOOLS_NOT_DISABLED")
        response = json.loads(terminals[0]["result"])
        if type(response.get("schema_version")) is not int or response["schema_version"] != 1:
            raise ValueError("INTAKE_SCHEMA_VERSION_INVALID")
        if response.get("action") not in {"NEED_CLARIFICATION", "SUBMIT_SUPPLEMENT", "NEW_CASE_REQUIRED"}:
            raise ValueError("INTAKE_ACTION_INVALID")
        receipt = helper._model_invocation(job_id=root.name, job_type="INTAKE", init=inits[0],
            final=terminals[0], ordinal=1, count=1, arguments=args)
        for field in ("job_id", "job_type", "job_invocation_ordinal", "job_invocation_count"):
            receipt.pop(field)
        receipt.update({"invocation_id": "server-intake:" + root.name, "class": "server-intake",
            "execution_id": root.name, "phase": "INTAKE", "action": response["action"],
            "stdout_sha256": helper.hashlib.sha256(raw).hexdigest(), "stdout_size": len(raw)})
        receipt["hard_cap_enforcement"]["hard_timeout_seconds"] = "agent-backend-120-second-limit"
        invocations.append(receipt)
    helper._write_new(args.output, {"schema_version": 1, "status": "PASS", "invocations": invocations,
        "new_execution_ids": sorted(item["execution_id"] for item in invocations)})


if __name__ == "__main__":
    main()
