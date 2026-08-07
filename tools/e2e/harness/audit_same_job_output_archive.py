#!/usr/bin/env python3
"""Capture proof that the same-Job result archive consumed output/ logs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from problem_locator.contracts.serialization import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)


EVIDENCE_ROOT = Path("/evidence")
DATA_ROOT = Path("/var/lib/problem-locator")
TARGET_PATTERN = re.compile(r"^output/proposals/[^/]+/tree/.+")


def fail(code: str) -> None:
    raise SystemExit(f"SAME_JOB_ARCHIVE_AUDIT_FAILED:{code}")


def strict_json(raw: bytes) -> Any:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=pairs,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
    )


def ordinary_bytes(path: Path, code: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        fail(code)
    return path.read_bytes()


def write_exclusive(path: Path, payload: dict[str, Any]) -> None:
    raw = canonical_json_bytes(payload)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def main() -> None:
    summaries = list(
        (EVIDENCE_ROOT / "same-job").glob(
            "attempt[0-9]*-[0-9]*-[0-9]*/journey-authoritative-summary.json"
        )
    )
    if len(summaries) != 1:
        fail("SUMMARY_COUNT")
    summary = strict_json(ordinary_bytes(summaries[0], "SUMMARY_FILE"))
    if not isinstance(summary, dict) or summary.get("scenario") != "SameJob":
        fail("SUMMARY_SCENARIO")
    case_id = summary.get("case_id")
    try:
        uuid.UUID(case_id)
    except (TypeError, ValueError, AttributeError):
        fail("CASE_ID")

    matches: list[tuple[str, Path, bytes, list[str]]] = []
    workspaces = DATA_ROOT / "tmp" / "workspaces"
    for outcome_path in workspaces.glob("*/output/job_outcome.json"):
        try:
            outcome_raw = ordinary_bytes(outcome_path, "OUTCOME_FILE")
            outcome = parse_canonical_json_bytes(outcome_raw)
        except Exception:
            fail("OUTCOME_CANONICAL")
        if not isinstance(outcome, dict) or outcome.get("case_id") != case_id:
            continue
        workspace = outcome_path.parents[1]
        job_id = workspace.name
        request_path = (
            workspace
            / "output"
            / "proposals"
            / "user-result-archive"
            / "request.json"
        )
        if not request_path.exists():
            continue
        raw = ordinary_bytes(request_path, "REQUEST_FILE")
        try:
            request = parse_canonical_json_bytes(raw)
        except Exception:
            fail("REQUEST_CANONICAL")
        if canonical_json_bytes(request) != raw or not isinstance(request, dict):
            fail("REQUEST_CANONICAL_ROUNDTRIP")
        if set(request) != {"schema_version", "result_text", "target_log_paths"}:
            fail("REQUEST_FIELDS")
        targets = request.get("target_log_paths")
        if (
            request.get("schema_version") != 1
            or not isinstance(targets, list)
            or not targets
            or len(targets) != len(set(targets))
            or not all(isinstance(path, str) for path in targets)
        ):
            fail("TARGET_PATHS")
        if not all(TARGET_PATTERN.fullmatch(path) for path in targets):
            continue
        payload = outcome.get("payload")
        artifact_drafts = outcome.get("proposed_artifact_drafts")
        if (
            outcome.get("job_id") != job_id
            or outcome.get("job_type") != "DIAGNOSE"
            or outcome.get("result_type") != "COMPLETED"
            or not isinstance(payload, dict)
            or not isinstance(payload.get("candidate_conclusion_draft"), dict)
            or not isinstance(artifact_drafts, list)
            or {
                draft.get("artifact_kind")
                for draft in artifact_drafts
                if isinstance(draft, dict)
            }
            < {"LOGPARSE_RUN", "USER_RESULT_ARCHIVE"}
        ):
            fail("OUTCOME_BINDING")
        matches.append((job_id, request_path, raw, targets))

    if len(matches) != 1:
        fail("MATCH_COUNT")
    job_id, request_path, raw, targets = matches[0]
    workspace = request_path.parents[3]
    for target in targets:
        target_path = workspace.joinpath(*target.split("/"))
        ordinary_bytes(target_path, "TARGET_FILE")

    output = summaries[0].parent / "same-job-output-archive-audit.json"
    write_exclusive(
        output,
        {
            "diagnosis_job_id": job_id,
            "request_path": request_path.relative_to(workspace).as_posix(),
            "request_sha256": hashlib.sha256(raw).hexdigest(),
            "status": "PASS",
            "target_log_count": len(targets),
            "target_log_paths": targets,
        },
    )


if __name__ == "__main__":
    main()
