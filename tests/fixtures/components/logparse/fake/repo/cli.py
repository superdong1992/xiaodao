#!/usr/bin/env python3
"""Small deterministic stand-in for the external logparse CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


CLIENT_LOG = (
    "2026-07-31T00:00:00.000Z caller=checkout-synthetic "
    "server=inventory-synthetic method=ReserveStock "
    "order_id=synthetic-order-0001 deadline exceeded after 3000ms\n"
)
SERVER_LOG = (
    "2026-07-31T00:00:00.100Z server=inventory-synthetic "
    "method=ReserveStock order_id=synthetic-order-0001 request accepted\n"
    "2026-07-31T00:00:02.900Z server=inventory-synthetic "
    "order_id=synthetic-order-0001 connection pool wait 2800ms\n"
)


def _reserved_environment_present() -> bool:
    reserved = {"logparse_repo", "logparse_config_path", "logparse_python"}
    return any(
        key.casefold() in reserved
        or key.casefold().startswith("problem_locator_logparse_")
        for key in os.environ
    )


def _record_invocation(command: str, output: Path, argv: list[str]) -> None:
    del output
    configured = os.environ.get("S07_FAKE_LOGPARSE_RECORD")
    if configured is None:
        return
    record_path = Path(configured)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    if record_path.is_file():
        record = json.loads(record_path.read_text(encoding="utf-8"))
    else:
        record = {
            "schema_version": 1,
            "parse_count": 0,
            "target_logs_count": 0,
            "invocations": [],
        }
    counter = "parse_count" if command == "parse" else "target_logs_count"
    record[counter] += 1
    record["invocations"].append(
        {
            "command": command,
            "argv": argv,
            "reserved_environment_present": _reserved_environment_present(),
        }
    )
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    record_path.with_name("parse_counter.json").write_text(
        json.dumps(
            {"parse_count": record["parse_count"]},
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _parse(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_path", type=Path)
    parser.add_argument("-c", dest="config", type=Path, required=True)
    parser.add_argument("-o", dest="output", type=Path, required=True)
    parser.add_argument("--product", required=True)
    args = parser.parse_args(argv)
    _record_invocation("parse", args.output, ["parse", *argv])
    if not args.config.is_file() or args.product != "compact":
        return 6
    marker = args.input_path.read_bytes()
    if marker.startswith(b"NONZERO"):
        return 7
    if marker.startswith(b"UNSUPPORTED_FORMAT"):
        return 9

    task = args.output / "task-synthetic"
    client_dir = task / "mech_modules" / "COMPACT" / "slot_1" / "cycle"
    server_dir = task / "mech_modules" / "COMPACT" / "slot_2" / "cycle"
    client_dir.mkdir(parents=True)
    server_dir.mkdir(parents=True)
    (client_dir / "checkout-client-101.log").write_text(
        CLIENT_LOG, encoding="utf-8", newline="\n"
    )
    (server_dir / "inventory-server-202.log").write_text(
        SERVER_LOG, encoding="utf-8", newline="\n"
    )
    (task / "result.json").write_text(
        json.dumps(
            {
                "mech_results": [
                    {
                        "module_key": "compact",
                        "module_name": "COMPACT",
                        "slots": [
                            {
                                "slot_id": "1",
                                "board_cycles": [
                                    {
                                        "dir_name": "cycle",
                                        "start_time": "2026-07-30T23:55:00.000Z",
                                        "end_time": "2026-07-31T00:05:00.000Z",
                                        "processes": [
                                            {
                                                "process_name": "checkout-client",
                                                "pid": "101",
                                                "total_count": 1,
                                                "missing_sequences": [],
                                                "missing_count": 0,
                                            }
                                        ],
                                        "cpu_cycles": [],
                                    }
                                ],
                            },
                            {
                                "slot_id": "2",
                                "board_cycles": [
                                    {
                                        "dir_name": "cycle",
                                        "start_time": "2026-07-30T23:55:00.000Z",
                                        "end_time": "2026-07-31T00:05:00.000Z",
                                        "processes": [
                                            {
                                                "process_name": "inventory-server",
                                                "pid": "202",
                                                "total_count": 2,
                                                "missing_sequences": [],
                                                "missing_count": 0,
                                            }
                                        ],
                                        "cpu_cycles": [],
                                    }
                                ],
                            },
                        ],
                    }
                ]
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    if marker.startswith(b"MANIFEST_DIRECTORY"):
        (task / "parse_manifest.json").mkdir()
    elif not marker.startswith(b"MISSING_MANIFEST"):
        (task / "parse_manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "artifact_contract_version": 1,
                    "task_id": "task-synthetic",
                    "product": args.product,
                    "status": "success",
                    "stages": [],
                    "artifacts": {},
                    "counters": {},
                    "diagnostics": [],
                    "workspace": {"retained": False},
                    "created_at": "2026-07-31T00:00:00.000Z",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    if marker.startswith(b"SECOND_TASK"):
        (args.output / "unexpected-task").mkdir()
    if marker.startswith(b"HANG_AFTER_PARSE"):
        while True:
            time.sleep(0.1)
    return 0


def _target(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--problem-time", required=True)
    parser.add_argument("--module", required=True)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--process-name", required=True)
    parser.add_argument("--pid")
    parser.add_argument("--label")
    args = parser.parse_args(argv)
    _record_invocation("mech-target-logs", args.output, ["mech-target-logs", *argv])
    if args.process_name == "hang":
        while True:
            time.sleep(0.1)
    if args.process_name == "fail":
        return 8
    if args.process_name == "invalid-json":
        print("not-json")
        return 0

    label = args.label or args.process_name
    normalized_slot = args.slot.removeprefix("slot_")
    targets = {
        ("compact", "1", "checkout-client", "101"): Path(
            "mech_modules/COMPACT/slot_1/cycle/checkout-client-101.log"
        ),
        ("compact", "2", "inventory-server", "202"): Path(
            "mech_modules/COMPACT/slot_2/cycle/inventory-server-202.log"
        ),
    }
    matching = [
        (key, value)
        for key, value in targets.items()
        if args.module.casefold() in {key[0], "compact"}
        and normalized_slot == key[1]
        and args.process_name.casefold() == key[2]
        and (args.pid is None or args.pid == key[3])
    ]
    if args.task_id != "task-synthetic" or len(matching) != 1:
        payload = {
            "schema_version": 1,
            "api_version": 1,
            "target_logs": [
                {
                    "label": label,
                    "module_key": "compact",
                    "module_name": "COMPACT",
                    "slot": normalized_slot,
                    "process_name": args.process_name,
                    "match_status": "missing",
                    "caveats": ["process not found for anchor"],
                }
            ],
        }
        if args.pid is not None:
            payload["target_logs"][0]["pid"] = args.pid
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    (target_key, relative) = matching[0]
    log_path = args.output / args.task_id / relative
    if args.label == "escape":
        log_path = args.output.parent / "outside.log"
    payload = {
        "schema_version": 1,
        "api_version": 1,
        "target_logs": [
            {
                "label": label,
                "module_key": "compact",
                "module_name": "COMPACT",
                "slot": normalized_slot,
                "process_name": args.process_name,
                "pid": target_key[3],
                "match_status": "exact",
                "board_cycle": "cycle",
                "cpu_cycle": None,
                "log_path": str(log_path.resolve()),
                "caveats": [],
            }
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    command = sys.argv[1]
    if command == "parse":
        return _parse(sys.argv[2:])
    if command == "mech-target-logs":
        return _target(sys.argv[2:])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
