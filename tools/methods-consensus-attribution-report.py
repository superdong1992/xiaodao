#!/usr/bin/env python3
"""Aggregate internal Evidence V2 consensus attribution execution records."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any


FILENAME = "methods-consensus-attribution-v2.json"
STATE_FILENAME = "methods-state-v2.json"
SUBREASONS = {
    "UNKNOWN_PRESENT",
    "VERDICT_MISMATCH",
    "EVIDENCE_SET_MISMATCH",
    "NO_CONFIRMED",
}
TERMINAL_STATUSES = {"RESOLVED", "UNRESOLVED", "FAILED"}
ATTRIBUTION_STAGES = {"CONSENSUS", "PRE_CONSENSUS_TERMINAL"}
RECORD_KEYS = {
    "schema_version",
    "record_type",
    "case_id",
    "job_id",
    "source_job_id",
    "evaluation_id",
    "evidence_graph_ref",
    "plan_ref",
    "skill_sha256",
    "terminal_status",
    "reason_code",
    "attribution_stage",
    "consensus_subreason",
    "evaluation_count",
    "evaluation_event_counts",
    "activated_method_count",
    "package_method_count",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not value.isspace()


def _validate_record(value: Any, *, path: Path) -> dict[str, Any]:
    label = str(path)
    _require(isinstance(value, dict), f"{label}: record must be an object")
    _require(set(value) == RECORD_KEYS, f"{label}: record fields differ")
    _require(value["schema_version"] == 1, f"{label}: schema_version must be 1")
    _require(
        value["record_type"] == "methods-consensus-attribution-v2",
        f"{label}: record_type differs",
    )
    for field in (
        "case_id",
        "job_id",
        "source_job_id",
        "evaluation_id",
        "evidence_graph_ref",
        "plan_ref",
        "skill_sha256",
    ):
        _require(_nonblank(value[field]), f"{label}: {field} must be non-blank")
    _require(
        len(value["skill_sha256"]) == 64
        and all(character in "0123456789abcdef" for character in value["skill_sha256"]),
        f"{label}: skill_sha256 must be lowercase SHA-256",
    )
    _require(
        value["terminal_status"] in TERMINAL_STATUSES,
        f"{label}: terminal_status is invalid",
    )
    _require(
        value["reason_code"] is None or _nonblank(value["reason_code"]),
        f"{label}: reason_code is invalid",
    )
    _require(
        value["attribution_stage"] in ATTRIBUTION_STAGES,
        f"{label}: attribution_stage is invalid",
    )
    _require(
        value["consensus_subreason"] is None
        or value["consensus_subreason"] in SUBREASONS,
        f"{label}: consensus_subreason is invalid",
    )
    if (
        value["terminal_status"] == "UNRESOLVED"
        and value["attribution_stage"] == "CONSENSUS"
    ):
        _require(
            value["consensus_subreason"] in SUBREASONS,
            f"{label}: unresolved consensus must have a subreason",
        )
    else:
        _require(
            value["consensus_subreason"] is None,
            f"{label}: only unresolved consensus may have a subreason",
        )

    for field in ("evaluation_count", "activated_method_count", "package_method_count"):
        _require(
            type(value[field]) is int and value[field] >= 0,
            f"{label}: {field} must be a non-negative integer",
        )
    _require(
        value["package_method_count"] >= 1
        and value["activated_method_count"] <= value["package_method_count"],
        f"{label}: method counts are inconsistent",
    )
    _require(
        value["evaluation_count"] == value["activated_method_count"],
        f"{label}: evaluation count must equal activated method count",
    )
    event_counts = value["evaluation_event_counts"]
    _require(isinstance(event_counts, list), f"{label}: event counts must be an array")
    _require(
        len(event_counts) == value["evaluation_count"],
        f"{label}: event count entries do not cover evaluations",
    )
    evaluation_refs: set[str] = set()
    for index, item in enumerate(event_counts):
        item_label = f"{label}: evaluation_event_counts[{index}]"
        _require(
            isinstance(item, dict) and set(item) == {"evaluation_ref", "event_count"},
            f"{item_label} fields differ",
        )
        _require(_nonblank(item["evaluation_ref"]), f"{item_label} ref is invalid")
        _require(
            item["evaluation_ref"] not in evaluation_refs,
            f"{item_label} ref is duplicated",
        )
        evaluation_refs.add(item["evaluation_ref"])
        _require(
            type(item["event_count"]) is int and item["event_count"] >= 1,
            f"{item_label} event_count must be positive",
        )
    return value


def _terminal_state_summary(value: Any, *, path: Path) -> dict[str, str | None] | None:
    label = str(path)
    _require(isinstance(value, dict), f"{label}: Methods state must be an object")
    status = value.get("status")
    if status not in TERMINAL_STATUSES:
        return None
    reason_code = value.get("reason_code")
    _require(
        reason_code is None or _nonblank(reason_code),
        f"{label}: Methods state reason_code is invalid",
    )
    if status == "RESOLVED":
        _require(reason_code is None, f"{label}: resolved state has a reason_code")
    else:
        _require(_nonblank(reason_code), f"{label}: terminal state lacks a reason_code")
    return {"terminal_status": status, "reason_code": reason_code}


def aggregate(records_root: Path) -> dict[str, Any]:
    root = records_root.resolve()
    _require(root.is_dir(), f"records root is not a directory: {root}")
    paths = sorted(root.rglob(FILENAME), key=lambda item: item.as_posix())
    records = [
        _validate_record(json.loads(path.read_text(encoding="utf-8")), path=path)
        for path in paths
    ]
    state_paths = sorted(root.rglob(STATE_FILENAME), key=lambda item: item.as_posix())
    terminal_states = [
        (path.parent, summary)
        for path in state_paths
        if (
            summary := _terminal_state_summary(
                json.loads(path.read_text(encoding="utf-8")),
                path=path,
            )
        )
        is not None
    ]

    terminal_statuses: Counter[str] = Counter()
    reason_codes: Counter[str] = Counter()
    subreasons: Counter[str] = Counter()
    evaluation_counts: Counter[str] = Counter()
    activated_counts: Counter[str] = Counter()
    package_counts: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    activated_total = 0
    package_total = 0
    unresolved_records = 0
    state_directories = {directory for directory, _ in terminal_states}
    for _, state in terminal_states:
        terminal_statuses[state["terminal_status"]] += 1
        if state["terminal_status"] == "UNRESOLVED":
            unresolved_records += 1
        if state["reason_code"] is not None:
            reason_codes[state["reason_code"]] += 1
    for path, record in zip(paths, records, strict=True):
        if path.parent not in state_directories:
            terminal_statuses[record["terminal_status"]] += 1
            if record["terminal_status"] == "UNRESOLVED":
                unresolved_records += 1
            if record["reason_code"] is not None:
                reason_codes[record["reason_code"]] += 1
        if record["consensus_subreason"] is not None:
            subreasons[record["consensus_subreason"]] += 1
        evaluation_counts[str(record["evaluation_count"])] += 1
        activated_counts[str(record["activated_method_count"])] += 1
        package_counts[str(record["package_method_count"])] += 1
        activated_total += record["activated_method_count"]
        package_total += record["package_method_count"]
        for item in record["evaluation_event_counts"]:
            event_counts[str(item["event_count"])] += 1

    return {
        "schema_version": 1,
        "record_type": "methods-consensus-attribution-summary-v1",
        "records_root": str(root),
        "records_scanned": len(records),
        "terminal_state_records_scanned": len(terminal_states),
        "terminal_records_scanned": len(terminal_states)
        + sum(path.parent not in state_directories for path in paths),
        "unresolved_records": unresolved_records,
        "terminal_status_distribution": dict(sorted(terminal_statuses.items())),
        "reason_code_distribution": dict(sorted(reason_codes.items())),
        "consensus_subreason_distribution": dict(sorted(subreasons.items())),
        "evaluation_count_distribution": dict(sorted(evaluation_counts.items())),
        "event_count_per_evaluation_distribution": dict(sorted(event_counts.items())),
        "activated_method_count_distribution": dict(sorted(activated_counts.items())),
        "package_method_count_distribution": dict(sorted(package_counts.items())),
        "activation_rate": {
            "activated_method_total": activated_total,
            "package_method_total": package_total,
            "weighted_ratio": (
                None if package_total == 0 else round(activated_total / package_total, 6)
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="汇总内部 Evidence V2 共识归因 execution records。"
    )
    parser.add_argument("--records-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        summary = aggregate(args.records_root)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"归因记录无效：{exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
