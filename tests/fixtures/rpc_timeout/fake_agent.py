#!/usr/bin/env python3
"""Deterministic external Agent for the SameJob RPC-timeout journey.

The fixture deliberately implements the production protocol split:

* ROUTE retains the legacy route envelope.
* Pass A requires one installed ``logparse-diagnose`` Helper load before one
  product-owned Logparse broker command.
* Specialist and Reviewer receive the same server-generated Evaluation Plan
  in isolated workspaces.
* Each role writes only a complete ``evaluation_ref/verdict/reason`` array.

It never reads target logs, reconstructs Evidence, or invokes Logparse from a
Methods role.
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
    MethodEvaluationPlanV2,
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


def _evaluation_response(*, verdict: str, reason: str) -> list[dict[str, str]]:
    plan_bytes = Path("inputs/method-evaluation-plan.json").read_bytes()
    plan = parse_canonical_json_bytes(plan_bytes, MethodEvaluationPlanV2)
    if canonical_json_bytes(plan) != plan_bytes:
        raise RuntimeError("the Evaluation Plan is not canonical")
    return [
        {
            "evaluation_ref": item.evaluation_ref,
            "verdict": verdict,
            "reason": reason,
        }
        for item in plan.evaluations
    ]


def _diagnose(instruction: dict[str, Any], context: str) -> None:
    del context
    Path("output/method-diagnosis.draft.json").write_bytes(
        canonical_json_bytes(
            _evaluation_response(
                verdict="CONFIRMED",
                reason="The server-generated Evidence Graph satisfies this method.",
            )
        )
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
    _record_invocation(
        job_id=job_id,
        job_type=JobType.REVIEW,
        phase="METHODS_REVIEW",
    )
    _await_review_release(job_id)
    Path("output/method-review.draft.json").write_bytes(
        canonical_json_bytes(
            _evaluation_response(
                verdict="CONFIRMED",
                reason="Independent blind review confirms this evaluation.",
            )
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
