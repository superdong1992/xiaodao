"""Server-installed tool that seals, but never decides, an Agent draft."""

from __future__ import annotations

import argparse
import hashlib
import stat
import sys
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from problem_locator.contracts import (
    AgentJobOutcomeDraftV2,
    ArtifactKind,
    JOB_WORKSPACE_BYTES,
    UserResultPayload,
    canonical_json_bytes,
)
from problem_locator.integrations.agent_json import (
    AgentJsonSurface,
    atomic_replace_agent_json,
    normalize_agent_json_file,
    read_agent_json_file,
)
from problem_locator.integrations.logparse.paths import resolve_workspace_path


DRAFT_FINALIZATION_MARKER_NAME = "agent-job-outcome-draft.finalized"
DRAFT_FINALIZATION_MARKER_RELATIVE_PATH = (
    f"runtime/tool-state/{DRAFT_FINALIZATION_MARKER_NAME}"
)
DRAFT_OUTCOME_RELATIVE_PATH = "output/job_outcome.draft.json"
SERVER_OUTCOME_RELATIVE_PATH = "output/job_outcome.json"


class SealedAgentOutcomeDraftMarker(BaseModel):
    """Private proof that the installed tool owns the current draft bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2]
    relative_path: Literal["output/job_outcome.draft.json"]
    size: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _user_result_indexes(outcome: AgentJobOutcomeDraftV2) -> list[int]:
    return [
        index
        for index, draft in enumerate(outcome.proposed_artifact_drafts)
        if draft.artifact_kind is ArtifactKind.USER_RESULT
    ]


def _require_server_output_absent(root: Path) -> None:
    path = resolve_workspace_path(root, SERVER_OUTCOME_RELATIVE_PATH, must_exist=False)
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISREG(metadata.st_mode):
        raise ValueError("Agent must not create the server-owned Outcome")
    raise ValueError("server-owned Outcome path is occupied")


def seal_agent_outcome_draft(workspace_root: Path) -> SealedAgentOutcomeDraftMarker:
    """Normalize companion JSON and atomically seal the V2 Agent draft.

    This command deliberately does not mint an Outcome ID, set server time,
    evaluate evidence, or write ``output/job_outcome.json``.  Those operations
    occur only after the Agent process tree has exited.
    """

    root = Path(workspace_root)
    _require_server_output_absent(root)
    draft_path = resolve_workspace_path(
        root,
        DRAFT_OUTCOME_RELATIVE_PATH,
        must_exist=True,
    )
    _, draft_document = read_agent_json_file(
        draft_path,
        max_bytes=JOB_WORKSPACE_BYTES,
    )
    if not isinstance(draft_document.value, dict):
        raise ValueError("Agent outcome draft must be one JSON object")
    draft_value = dict(draft_document.value)
    draft = AgentJobOutcomeDraftV2.model_validate(draft_value)

    user_result_indexes = _user_result_indexes(draft)
    if len(user_result_indexes) > 1:
        raise ValueError("Agent outcome may declare at most one USER_RESULT")
    if user_result_indexes:
        index = user_result_indexes[0]
        proposal = draft.proposed_artifact_drafts[index]
        result_path = resolve_workspace_path(
            root,
            proposal.workspace_relative_path,
            must_exist=True,
        )
        result_document = normalize_agent_json_file(
            result_path,
            surface=AgentJsonSurface.USER_RESULT,
            max_bytes=JOB_WORKSPACE_BYTES,
            validate=UserResultPayload.model_validate,
        )
        raw_proposals = draft_value.get("proposed_artifact_drafts")
        if not isinstance(raw_proposals, list) or index >= len(raw_proposals):
            raise ValueError("Agent outcome proposal list changed during sealing")
        raw_proposal = raw_proposals[index]
        if not isinstance(raw_proposal, dict):
            raise ValueError("USER_RESULT proposal must be one JSON object")
        raw_proposal["declared_size"] = result_document.size
        raw_proposal["declared_sha256"] = result_document.sha256
        draft = AgentJobOutcomeDraftV2.model_validate(draft_value)

    draft_bytes = canonical_json_bytes(draft)
    atomic_replace_agent_json(draft_path, draft_bytes)
    marker = SealedAgentOutcomeDraftMarker(
        schema_version=2,
        relative_path=DRAFT_OUTCOME_RELATIVE_PATH,
        size=len(draft_bytes),
        sha256=hashlib.sha256(draft_bytes).hexdigest(),
    )
    marker_path = resolve_workspace_path(
        root,
        DRAFT_FINALIZATION_MARKER_RELATIVE_PATH,
        must_exist=False,
    )
    atomic_replace_agent_json(marker_path, canonical_json_bytes(marker))
    return marker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="problem-locator-seal-outcome-draft")
    parser.parse_args(argv)
    try:
        seal_agent_outcome_draft(Path.cwd())
    except (OSError, TypeError, ValueError):
        print(
            "problem-locator-seal-outcome-draft: Agent draft sealing failed",
            file=sys.stderr,
        )
        return 2
    print(DRAFT_OUTCOME_RELATIVE_PATH)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DRAFT_FINALIZATION_MARKER_NAME",
    "DRAFT_FINALIZATION_MARKER_RELATIVE_PATH",
    "DRAFT_OUTCOME_RELATIVE_PATH",
    "SERVER_OUTCOME_RELATIVE_PATH",
    "SealedAgentOutcomeDraftMarker",
    "seal_agent_outcome_draft",
    "main",
]
