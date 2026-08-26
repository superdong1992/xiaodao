#!/usr/bin/env python3
"""Deterministic external Agent for the SameJob RPC-timeout journey.

The fixture deliberately implements the production protocol split:

* ROUTE retains the legacy route envelope.
* Pass A requires one installed ``logparse-diagnose`` Helper load before one
  product-owned Logparse broker command.
* Pass B receives the frozen Methods package/target logs and writes only the
  Methods diagnosis draft.
* REVIEW receives the frozen prior Methods diagnosis and writes only the
  Methods review draft.

It never reconstructs a verification contract and never lets the Methods pass
invoke Logparse.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, os.fspath(REPOSITORY_ROOT / "src"))

from problem_locator.contracts import (  # noqa: E402
    AgentJobOutcomeDraftV2,
    JobType,
    OutcomeResultType,
    RouteDecision,
    RouteKind,
    WorkspaceInputManifest,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.integrations.logparse import cli as logparse_cli  # noqa: E402
from problem_locator.runtime.outcome_finalizer import (  # noqa: E402
    seal_agent_outcome_draft,
)


_PREPROCESS_COMMAND = re.compile(
    r"^problem-locator-logparse (parse-targets|target-logs) "
    r"--request ([A-Za-z0-9_./-]+) --result ([A-Za-z0-9_./-]+)$",
    flags=re.MULTILINE,
)
_PREPROCESS_HEADER = (
    "You are the product-owned Logparse preprocessing pass in "
    "SERVER_PREPROCESS mode."
)
_PREPROCESS_HELPER_CALL = "Skill(logparse-diagnose)"
_METHODS_FILE = re.compile(
    r'<<<METHODS_SKILL_FILE path="methods\.json">>>\n'
    r"(.*?)\n<<<END METHODS_SKILL_FILE>>>",
    flags=re.DOTALL,
)
_IDENTITY = re.compile(
    r"\b(?:order_id|request_id|trace_id|rpc_id)=[^\s,;]+"
)


def _section(body: str, name: str) -> str:
    match = re.search(
        rf"<<<SECTION [0-9]+ {re.escape(name)}>>>\n(.*?)<<<END SECTION>>>\n",
        body,
        flags=re.DOTALL,
    )
    if match is None:
        raise RuntimeError(f"required context section is absent: {name}")
    return match.group(1).rstrip("\n")


def _manifest() -> WorkspaceInputManifest:
    return WorkspaceInputManifest.model_validate_json(
        Path("inputs/manifest.json").read_bytes()
    )


def _record_invocation(*, job_id: str, job_type: JobType, phase: str) -> None:
    configured = os.environ.get("S08_FAKE_AGENT_RECORD")
    if configured is None:
        return
    record = {
        "job_id": job_id,
        "job_type": job_type.value,
        "phase": phase,
        "pid": os.getpid(),
    }
    path = Path(configured)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, separators=(",", ":"), sort_keys=True))
        stream.write("\n")


def _assert_safe_relative(value: object, *, prefix: str | None = None) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeError("workspace path is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise RuntimeError("workspace path is unsafe")
    normalized = relative.as_posix()
    if prefix is not None and not normalized.startswith(prefix):
        raise RuntimeError("workspace path escapes its frozen surface")
    return normalized


def _preprocess(prompt: str) -> int:
    helper_offsets = [
        match.start()
        for match in re.finditer(re.escape(_PREPROCESS_HELPER_CALL), prompt)
    ]
    if len(helper_offsets) != 1:
        raise RuntimeError(
            "Pass A must contain exactly one logparse-diagnose Helper call"
        )
    commands = list(_PREPROCESS_COMMAND.finditer(prompt))
    if len(commands) != 1:
        raise RuntimeError("Pass A must contain exactly one Logparse broker command")
    command = commands[0]
    if helper_offsets[0] >= command.start():
        raise RuntimeError("Pass A must load the Helper before the broker command")
    operation = command.group(1)
    request_path = _assert_safe_relative(
        command.group(2),
        prefix="output/proposals/methods-preprocess/",
    )
    result_path = _assert_safe_relative(
        command.group(3),
        prefix="output/proposals/methods-preprocess/",
    )
    manifest = _manifest()
    _record_invocation(
        job_id=manifest.job_id,
        job_type=JobType.DIAGNOSE,
        phase="LOGPARSE_PREPROCESS",
    )
    status = logparse_cli.main(
        [operation, "--request", request_path, "--result", result_path]
    )
    if status != 0:
        raise RuntimeError(f"Logparse preprocessing failed: {operation}")
    result = Path(result_path).read_bytes()
    if canonical_json_bytes(parse_canonical_json_bytes(result)) != result:
        raise RuntimeError("Logparse preprocessing result is not canonical JSON")
    return 0


def _route(instruction: dict[str, Any], context: str) -> None:
    index = json.loads(_section(context, "SKILL_INDEX"))
    skills = index.get("skills")
    if not isinstance(skills, list):
        raise RuntimeError("route Skill index is invalid")
    matches = [
        item
        for item in skills
        if isinstance(item, dict)
        and item.get("requires_logparse") is True
        and "rpc" in " ".join(
            str(item.get(field, "")).lower()
            for field in ("registration_id", "capability", "summary")
        )
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("ref"), dict):
        raise RuntimeError("the RPC Methods registration is not uniquely routable")
    manifest = _manifest()
    draft = AgentJobOutcomeDraftV2(
        schema_version=2,
        job_id=str(instruction["job_id"]),
        case_id=manifest.case_id,
        job_type=JobType.ROUTE,
        base_state_revision=int(instruction["base_state_revision"]),
        result_type=OutcomeResultType.COMPLETED,
        payload=RouteDecision(
            kind=RouteKind.MATCHED,
            skill_ref=matches[0]["ref"],
            reason="The product catalog contains one RPC Logparse Methods registration.",
            confidence=1.0,
        ),
        consumed_evidence_refs=[],
        proposed_evidence_drafts=[],
        proposed_artifact_drafts=[],
        error=None,
        rule_claims=[],
    )
    payload = canonical_json_bytes(draft)
    Path("output/job_outcome.draft.json").write_bytes(payload)
    seal_agent_outcome_draft(Path.cwd())
    _record_invocation(
        job_id=draft.job_id,
        job_type=JobType.ROUTE,
        phase="ROUTE",
    )


def _methods_manifest(context: str) -> dict[str, Any]:
    match = _METHODS_FILE.search(_section(context, "SKILL"))
    if match is None:
        raise RuntimeError("the frozen Methods index is absent")
    value = json.loads(match.group(1))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeError("the frozen Methods index is invalid")
    methods = value.get("methods")
    if not isinstance(methods, list) or not methods:
        raise RuntimeError("the frozen Methods index has no method cards")
    return value


def _frozen_target_logs() -> list[tuple[str, list[str]]]:
    manifest_bytes = Path("inputs/target_logs.json").read_bytes()
    value = parse_canonical_json_bytes(manifest_bytes)
    if canonical_json_bytes(value) != manifest_bytes or not isinstance(value, dict):
        raise RuntimeError("frozen target_logs manifest is invalid")
    rows = value.get("target_logs")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("frozen target_logs manifest is empty")
    result: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("frozen target log row is invalid")
        source_id = row.get("source_id")
        if not isinstance(source_id, str) or source_id in seen:
            raise RuntimeError("frozen target log source identity is invalid")
        seen.add(source_id)
        relative = _assert_safe_relative(
            row.get("log_path"),
            prefix="inputs/target-logs/",
        )
        payload = Path(relative).read_bytes()
        if len(payload) != row.get("size"):
            raise RuntimeError("frozen target log size drifted")
        try:
            lines = payload.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise RuntimeError("frozen target log is not UTF-8") from exc
        result.append((source_id, lines))
    return result


def _diagnose(instruction: dict[str, Any], context: str) -> None:
    methods_manifest = _methods_manifest(context)
    target_logs = _frozen_target_logs()
    evidence: list[dict[str, Any]] = []
    confirmed: list[str] = []
    for method in methods_manifest["methods"]:
        if not isinstance(method, dict):
            raise RuntimeError("Methods card index entry is invalid")
        method_id = method.get("id")
        markers = method.get("evidence_markers")
        if (
            not isinstance(method_id, str)
            or not isinstance(markers, list)
            or not markers
            or any(not isinstance(marker, str) or not marker for marker in markers)
        ):
            raise RuntimeError("Methods evidence marker index is invalid")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for source_id, lines in target_logs:
            for line_number, line in enumerate(lines, start=1):
                marker = next((item for item in markers if item in line), None)
                if marker is None:
                    continue
                identity = _IDENTITY.search(line)
                token = identity.group(0) if identity is not None else marker
                grouped.setdefault(token, []).append(
                    {
                        "source_id": source_id,
                        "line_number": line_number,
                        "marker": marker,
                        "line": line,
                    }
                )
        if not grouped:
            continue
        confirmed.append(method_id)
        for token, sources in grouped.items():
            evidence.append(
                {
                    "method_id": method_id,
                    "summary": (
                        f"Frozen target logs contain positive marker evidence for {method_id}."
                    ),
                    "identity_tokens": [token],
                    "sources": sources,
                }
            )
    draft = {
        "schema_version": 1,
        "status": "CONFIRMED" if confirmed else "INSUFFICIENT",
        "confirmed_methods": confirmed,
        "candidate_methods": [],
        "evidence": evidence,
        "limitations": (
            [] if confirmed else ["No indexed positive marker appears in the frozen logs."]
        ),
        "safety_notes": [
            "Only server-frozen target logs and the pinned Methods index were inspected."
        ],
    }
    Path("output/method-diagnosis.draft.json").write_bytes(
        canonical_json_bytes(draft)
    )
    _record_invocation(
        job_id=str(instruction["job_id"]),
        job_type=JobType.DIAGNOSE,
        phase="METHODS_DIAGNOSE",
    )


def _await_review_release(job_id: str) -> None:
    marker = os.environ.get("S08_REVIEW_ENTERED")
    release = os.environ.get("S08_REVIEW_RELEASE")
    if marker is not None:
        Path(marker).write_text(job_id, encoding="utf-8")
    if release is None:
        return
    deadline = time.monotonic() + 20.0
    while not Path(release).is_file():
        if time.monotonic() >= deadline:
            raise RuntimeError("review gate was not released")
        time.sleep(0.02)


def _review(instruction: dict[str, Any]) -> None:
    job_id = str(instruction["job_id"])
    diagnosis_bytes = Path("inputs/method-diagnosis.json").read_bytes()
    diagnosis = parse_canonical_json_bytes(diagnosis_bytes)
    if canonical_json_bytes(diagnosis) != diagnosis_bytes or not isinstance(diagnosis, dict):
        raise RuntimeError("prior Methods diagnosis is not canonical")
    evidence = diagnosis.get("evidence")
    if not isinstance(evidence, list):
        raise RuntimeError("prior Methods diagnosis evidence is invalid")
    findings: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict):
            raise RuntimeError("prior Methods evidence item is invalid")
        findings.append(
            {
                "method_id": item["method_id"],
                "identity_tokens": item["identity_tokens"],
                "verdict": "PASS",
                "reason": "The exact grounded identity remains supported by the frozen diagnosis.",
            }
        )
    _record_invocation(
        job_id=job_id,
        job_type=JobType.REVIEW,
        phase="METHODS_REVIEW",
    )
    _await_review_release(job_id)
    Path("output/method-review.draft.json").write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "verdict": "PASS",
                "findings": findings,
                "limitations": [],
            }
        )
    )


def main() -> int:
    context = sys.stdin.buffer.read().decode("utf-8")
    if context.startswith(_PREPROCESS_HEADER + "\n"):
        return _preprocess(context)
    instruction = json.loads(_section(context, "JOB_INSTRUCTION"))
    job_type = JobType(str(instruction["job_type"]))
    if job_type is JobType.ROUTE:
        _route(instruction, context)
    elif job_type is JobType.DIAGNOSE:
        _diagnose(instruction, context)
    elif job_type is JobType.REVIEW:
        _review(instruction)
    else:  # pragma: no cover - JobType is closed, retained for defensive clarity.
        raise RuntimeError(f"unsupported fake Agent job type: {job_type.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
