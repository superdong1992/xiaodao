#!/usr/bin/env python3
"""Prove that a server-owned Result v2 archive consumed same-Job Logparse logs."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import uuid
import zipfile
from pathlib import Path
from typing import Any

from problem_locator.contracts import (
    AgentJobOutcome,
    AgentJobOutcomeDraftV2,
    ArtifactKind,
    StateFile,
    UserResultPayloadV2,
)
from problem_locator.contracts.serialization import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)


EVIDENCE_ROOT = Path("/evidence")
DATA_ROOT = Path("/var/lib/problem-locator")
TARGET_PATTERN = re.compile(r"^output/proposals/[^/]+/tree/.+")
PUBLIC_RESULT_KINDS = {
    ArtifactKind.USER_RESULT,
    ArtifactKind.USER_RESULT_ARCHIVE,
}


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


def canonical_model(path: Path, model_type: type[Any], code: str) -> Any:
    raw = ordinary_bytes(path, code)
    try:
        value = parse_canonical_json_bytes(raw, model_type=model_type)
    except Exception:
        fail(code)
    if canonical_json_bytes(value) != raw:
        fail(code)
    return value


def canonical_object(path: Path, code: str) -> dict[str, Any]:
    raw = ordinary_bytes(path, code)
    try:
        value = parse_canonical_json_bytes(raw)
    except Exception:
        fail(code)
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        fail(code)
    return value


def write_exclusive(path: Path, payload: dict[str, Any]) -> None:
    raw = canonical_json_bytes(payload)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _formal_payload(storage_key: str, code: str) -> bytes:
    if not isinstance(storage_key, str) or not storage_key.startswith("resources/cases/"):
        fail(code)
    path = DATA_ROOT.joinpath(*storage_key.split("/"))
    return ordinary_bytes(path, code)


def _public_artifact_id(summary: dict[str, Any], field: str, code: str) -> str:
    value = summary.get(field)
    artifact_id = value.get("artifact_id") if isinstance(value, dict) else None
    try:
        uuid.UUID(artifact_id)
    except (TypeError, ValueError, AttributeError):
        fail(code)
    return artifact_id


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

    state = canonical_model(DATA_ROOT / "state.json", StateFile, "STATE_FILE")
    aggregate = state.cases.get(case_id)
    if aggregate is None:
        fail("STATE_CASE")
    result_artifact_id = _public_artifact_id(summary, "public_artifact", "RESULT_ID")
    archive_artifact_id = _public_artifact_id(
        summary,
        "public_result_archive",
        "ARCHIVE_ID",
    )
    result_artifact = aggregate.artifacts.get(result_artifact_id)
    archive_artifact = aggregate.artifacts.get(archive_artifact_id)
    if (
        result_artifact is None
        or result_artifact.kind is not ArtifactKind.USER_RESULT
        or archive_artifact is None
        or archive_artifact.kind is not ArtifactKind.USER_RESULT_ARCHIVE
    ):
        fail("FORMAL_RESULTS")
    if result_artifact.created_by_job_id != archive_artifact.created_by_job_id:
        fail("FORMAL_RESULT_OWNER")

    matches: list[
        tuple[Path, AgentJobOutcomeDraftV2, AgentJobOutcome]
    ] = []
    workspaces = DATA_ROOT / "tmp" / "workspaces"
    for outcome_path in workspaces.glob("*/output/job_outcome.json"):
        try:
            final_outcome = canonical_model(
                outcome_path,
                AgentJobOutcome,
                "SERVER_OUTCOME",
            )
        except SystemExit:
            raise
        if (
            final_outcome.case_id != case_id
            or final_outcome.job_id != result_artifact.created_by_job_id
        ):
            continue
        workspace = outcome_path.parents[1]
        draft = canonical_model(
            workspace / "output/job_outcome.draft.json",
            AgentJobOutcomeDraftV2,
            "AGENT_DRAFT",
        )
        matches.append((workspace, draft, final_outcome))
    if len(matches) != 1:
        fail("MATCH_COUNT")
    workspace, draft, final_outcome = matches[0]
    job_id = workspace.name
    if (
        draft.job_id != job_id
        or draft.job_id != final_outcome.job_id
        or draft.job_type.value != "DIAGNOSE"
        or draft.result_type.value != "COMPLETED"
        or draft.payload.candidate_conclusion_draft is None
    ):
        fail("OUTCOME_BINDING")
    agent_public = [
        item
        for item in draft.proposed_artifact_drafts
        if item.artifact_kind in PUBLIC_RESULT_KINDS
    ]
    if agent_public:
        fail("AGENT_PUBLIC_RESULT")

    final_public = {
        item.proposal_key: item
        for item in final_outcome.proposed_artifact_drafts
        if item.artifact_kind in PUBLIC_RESULT_KINDS
    }
    if set(final_public) != {
        "server-user-result",
        "server-user-result-archive",
    }:
        fail("SERVER_PUBLIC_KEYS")
    result_proposal = final_public["server-user-result"]
    archive_proposal = final_public["server-user-result-archive"]
    if (
        result_proposal.artifact_kind is not ArtifactKind.USER_RESULT
        or result_proposal.metadata.schema_version != 2
        or result_proposal.metadata.format_id != "problem-locator-diagnosis-v2"
        or archive_proposal.artifact_kind is not ArtifactKind.USER_RESULT_ARCHIVE
        or archive_proposal.metadata.schema_version != 2
        or archive_proposal.metadata.format_id
        != "problem-locator-result-archive-v2"
        or archive_proposal.metadata.user_result_proposal_key
        != result_proposal.proposal_key
    ):
        fail("SERVER_PUBLIC_CONTRACT")

    result_bytes = _formal_payload(result_artifact.storage_key, "RESULT_FILE")
    archive_bytes = _formal_payload(archive_artifact.storage_key, "ARCHIVE_FILE")
    if (
        len(result_bytes) != result_artifact.size
        or hashlib.sha256(result_bytes).hexdigest() != result_artifact.sha256
        or result_proposal.declared_size != result_artifact.size
        or result_proposal.declared_sha256 != result_artifact.sha256
        or len(archive_bytes) != archive_artifact.size
        or hashlib.sha256(archive_bytes).hexdigest() != archive_artifact.sha256
        or archive_proposal.declared_size != archive_artifact.size
        or archive_proposal.declared_sha256 != archive_artifact.sha256
    ):
        fail("FORMAL_RESULT_BYTES")
    try:
        report = parse_canonical_json_bytes(
            result_bytes,
            model_type=UserResultPayloadV2,
        )
    except Exception:
        fail("RESULT_PAYLOAD")
    if (
        canonical_json_bytes(report) != result_bytes
        or report.status != "COMPLETED"
        or report.root_cause is None
        or not report.verification_rules
        or not report.recommendations
    ):
        fail("RESULT_RICHNESS")

    logparse_drafts = [
        item
        for item in draft.proposed_artifact_drafts
        if item.artifact_kind is ArtifactKind.LOGPARSE_RUN
    ]
    if len(logparse_drafts) != 1:
        fail("LOGPARSE_DRAFT_COUNT")
    logparse_draft = logparse_drafts[0]
    tree_relative = logparse_draft.workspace_relative_path
    if not tree_relative.startswith("output/proposals/") or not tree_relative.endswith(
        "/tree"
    ):
        fail("LOGPARSE_TREE")
    tree = workspace.joinpath(*tree_relative.split("/"))
    proposal_root = tree.parent
    target_result = canonical_object(
        proposal_root / "target_logs.json",
        "TARGET_RESULT",
    )
    target_logs = target_result.get("target_logs")
    if not isinstance(target_logs, list) or not target_logs:
        fail("TARGET_LOGS")
    request = canonical_object(proposal_root / "request.json", "LOGPARSE_REQUEST")
    if (
        request.get("artifact_proposal_key") != logparse_draft.proposal_key
        or not isinstance(request.get("problem_time"), str)
    ):
        fail("LOGPARSE_REQUEST_BINDING")

    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r") as archive:
            manifest_bytes = archive.read("archive-manifest.json")
            manifest = parse_canonical_json_bytes(manifest_bytes)
            if not isinstance(manifest, dict) or canonical_json_bytes(manifest) != manifest_bytes:
                fail("ARCHIVE_MANIFEST")
            manifest_logs = manifest.get("target_logs")
            if (
                manifest.get("schema_version") != 2
                or manifest.get("format_id")
                != "problem-locator-result-archive-v2"
                or manifest.get("problem_time") != request["problem_time"]
                or manifest.get("diagnosis_result_sha256")
                != hashlib.sha256(result_bytes).hexdigest()
                or not isinstance(manifest_logs, list)
                or len(manifest_logs) != len(target_logs)
                or manifest.get("target_log_count") != len(target_logs)
                or archive_proposal.metadata.target_log_count != len(target_logs)
            ):
                fail("ARCHIVE_MANIFEST_BINDING")
            archive_names = [
                item.get("archive_name")
                for item in manifest_logs
                if isinstance(item, dict)
            ]
            expected_names = ["result.txt", "archive-manifest.json", *archive_names]
            if (
                len(archive_names) != len(manifest_logs)
                or not all(
                    isinstance(name, str)
                    and name.endswith(".log")
                    and not name.startswith("target-log-")
                    for name in archive_names
                )
                or archive.namelist() != expected_names
            ):
                fail("ARCHIVE_NAMES")
            result_text_bytes = archive.read("result.txt")
            result_text = result_text_bytes.decode("utf-8")
            if (
                manifest.get("result_txt_sha256")
                != hashlib.sha256(result_text_bytes).hexdigest()
                or report.root_cause not in result_text
                or report.problem_statement not in result_text
                or not all(
                    rule.rule_id in result_text for rule in report.verification_rules
                )
                or [result_text.index(f"{index}. ") for index in range(1, 10)]
                != sorted(result_text.index(f"{index}. ") for index in range(1, 10))
            ):
                fail("RESULT_TEXT")
            target_paths: list[str] = []
            comparable_fields = (
                "label",
                "module_key",
                "module_name",
                "slot",
                "cpu_id",
                "process_name",
                "pid",
                "match_status",
                "caveats",
            )
            for ordinal, (manifest_log, target_log, archive_name) in enumerate(
                zip(manifest_logs, target_logs, archive_names, strict=True),
                start=1,
            ):
                if not isinstance(manifest_log, dict) or not isinstance(target_log, dict):
                    fail("TARGET_SHAPE")
                log_path = target_log.get("log_path")
                target_relative = f"{tree_relative}/{log_path}"
                if (
                    manifest_log.get("ordinal") != ordinal
                    or any(
                        manifest_log.get(field) != target_log.get(field)
                        for field in comparable_fields
                    )
                    or not isinstance(log_path, str)
                    or not TARGET_PATTERN.fullmatch(target_relative)
                ):
                    fail("TARGET_BINDING")
                source_bytes = ordinary_bytes(
                    tree.joinpath(*log_path.split("/")),
                    "TARGET_FILE",
                )
                archived_bytes = archive.read(archive_name)
                if (
                    source_bytes != archived_bytes
                    or manifest_log.get("size") != len(source_bytes)
                    or manifest_log.get("sha256")
                    != hashlib.sha256(source_bytes).hexdigest()
                ):
                    fail("TARGET_BYTES")
                target_paths.append(target_relative)
    except SystemExit:
        raise
    except Exception:
        fail("ARCHIVE_INVALID")

    output = summaries[0].parent / "same-job-output-archive-audit.json"
    write_exclusive(
        output,
        {
            "agent_draft_path": "output/job_outcome.draft.json",
            "agent_public_result_proposal_count": 0,
            "archive_artifact_id": archive_artifact.artifact_id,
            "archive_sha256": archive_artifact.sha256,
            "diagnosis_job_id": job_id,
            "server_result_proposal_keys": sorted(final_public),
            "status": "PASS",
            "target_log_count": len(target_paths),
            "target_log_paths": target_paths,
        },
    )


if __name__ == "__main__":
    main()
