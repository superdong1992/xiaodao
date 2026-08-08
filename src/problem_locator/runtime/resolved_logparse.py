"""Compile immutable Logparse bindings from a pinned Skill and frozen Job."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from problem_locator.contracts import (
    ArtifactKind,
    CaseAggregate,
    DiagnosisProvenanceType,
    Job,
)
from problem_locator.integrations.logparse.requests import Anchor, ResolvedLogparsePlan

from .context_policy import ResolvedJobAssets


class ResolvedLogparsePlanNotReady(ValueError):
    """The pinned plan is valid but a required frozen input is still missing."""


def _manifest(assets: ResolvedJobAssets) -> dict[str, Any]:
    if assets.skill is None:
        raise ValueError("logparse Job requires a pinned diagnosis Skill")
    path = Path(assets.skill.root_path) / "diagnosis-skill.json"
    value = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 3:
        raise ValueError("diagnosis Skill manifest v3 is required")
    return value


def _facts(job: Job) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in job.context_snapshot.user_facts:
        provenance = item.provenance
        if provenance.source_type is not DiagnosisProvenanceType.USER_INPUT:
            raise ValueError("ContextSnapshot user fact has invalid provenance")
        name = provenance.input_name
        if name is None or name in result:
            raise ValueError("each user fact input_name must be unique")
        result[name] = item.statement
    return result


def _binding(value: object, facts: dict[str, str]) -> str:
    if not isinstance(value, dict):
        raise ValueError("Skill binding must be an object")
    if set(value) == {"source", "name"} and value.get("source") == "USER_FACT":
        name = value.get("name")
        if not isinstance(name, str):
            raise ValueError("Skill user-fact binding name must be a string")
        if name not in facts:
            raise ResolvedLogparsePlanNotReady(
                f"Skill binding requires the missing user fact {name}"
            )
        return facts[name]
    if set(value) == {"source", "value"} and value.get("source") == "SKILL_FIXED":
        fixed = value.get("value")
        if not isinstance(fixed, str):
            raise ValueError("Skill fixed binding must be a string")
        return fixed
    raise ValueError("Skill binding shape is invalid")


def _attachment_id(job: Job, plan: dict[str, Any]) -> str:
    requirement_name = plan.get("attachment_requirement")
    matching: list[str] = []
    if isinstance(requirement_name, str):
        for requirement in job.context_snapshot.pending_requirements:
            if requirement.name == requirement_name:
                matching.extend(requirement.fulfilled_by_refs)
    matching = list(dict.fromkeys(matching))
    if not matching and len(job.attachment_refs) == 1:
        matching = list(job.attachment_refs)
    if not matching:
        raise ResolvedLogparsePlanNotReady(
            "logparse attachment is not fixed by the Job yet"
        )
    if len(matching) != 1 or matching[0] not in job.attachment_refs:
        raise ValueError("logparse attachment binding is not unique in the Job")
    return matching[0]


def compile_resolved_logparse_plan(
    job: Job,
    aggregate: CaseAggregate,
    assets: ResolvedJobAssets,
) -> ResolvedLogparsePlan | None:
    """Return the exact plan the broker must enforce for this Job."""

    if job.logparse_tool_ref is None:
        return None
    manifest = _manifest(assets)
    plan = manifest.get("logparse_plan")
    if not isinstance(plan, dict):
        raise ValueError("logparse Job Skill requires logparse_plan")
    facts = _facts(job)
    problem_binding = plan.get("problem_time_binding")
    problem_time = _binding(problem_binding, facts)
    raw_anchors = plan.get("anchors")
    if not isinstance(raw_anchors, list) or not raw_anchors:
        raise ValueError("logparse plan requires ordered anchors")
    anchors: list[Anchor] = []
    for raw in raw_anchors:
        if not isinstance(raw, dict):
            raise ValueError("logparse anchor must be an object")
        label = raw.get("label")
        if not isinstance(label, str):
            raise ValueError("logparse anchor label must be a string")
        pid_value = raw.get("pid")
        anchors.append(
            Anchor(
                label=label,
                module=_binding(raw.get("module"), facts),
                slot=_binding(raw.get("slot"), facts),
                process_name=_binding(raw.get("process_name"), facts),
                pid=None if pid_value is None else _binding(pid_value, facts),
            )
        )

    run_ids = [
        artifact_id
        for artifact_id in job.artifact_refs
        if artifact_id in aggregate.artifacts
        and aggregate.artifacts[artifact_id].kind is ArtifactKind.LOGPARSE_RUN
    ]
    if len(run_ids) > 1:
        raise ValueError("Job contains multiple LOGPARSE_RUN artifacts")
    if run_ids:
        return ResolvedLogparsePlan(
            schema_version=1,
            problem_time=problem_time,
            anchors=anchors,
            attachment_id=None,
            artifact_id=run_ids[0],
        )
    return ResolvedLogparsePlan(
        schema_version=1,
        problem_time=problem_time,
        anchors=anchors,
        attachment_id=_attachment_id(job, plan),
        artifact_id=None,
    )


__all__ = [
    "ResolvedLogparsePlanNotReady",
    "compile_resolved_logparse_plan",
]
