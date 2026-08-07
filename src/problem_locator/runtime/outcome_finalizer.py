"""Server-installed finalizer for all formal JSON in one Agent outcome."""

from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from problem_locator.contracts import (
    AgentJobOutcome,
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


FINALIZATION_MARKER_NAME = "agent-job-outcome.finalized"
FINALIZATION_MARKER_RELATIVE_PATH = (
    f"runtime/tool-state/{FINALIZATION_MARKER_NAME}"
)
OUTCOME_RELATIVE_PATH = "output/job_outcome.json"


class FinalizedAgentOutcomeMarker(BaseModel):
    """Private proof that the finalizer owns the current Outcome bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    relative_path: Literal["output/job_outcome.json"]
    size: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _utc_milliseconds() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _user_result_indexes(outcome: AgentJobOutcome) -> list[int]:
    return [
        index
        for index, draft in enumerate(outcome.proposed_artifact_drafts)
        if draft.artifact_kind is ArtifactKind.USER_RESULT
    ]


def finalize_agent_outcome(workspace_root: Path) -> FinalizedAgentOutcomeMarker:
    """Normalize companion JSON, finalize the Outcome, and publish its marker."""

    root = Path(workspace_root)
    outcome_path = resolve_workspace_path(
        root,
        OUTCOME_RELATIVE_PATH,
        must_exist=True,
    )
    _, draft_document = read_agent_json_file(
        outcome_path,
        max_bytes=JOB_WORKSPACE_BYTES,
    )
    if not isinstance(draft_document.value, dict):
        raise ValueError("Agent outcome draft must be one JSON object")
    draft_value = dict(draft_document.value)
    draft_value["outcome_id"] = str(uuid.uuid4())
    draft_value["produced_at"] = _utc_milliseconds()
    outcome = AgentJobOutcome.model_validate(draft_value)

    user_result_indexes = _user_result_indexes(outcome)
    if len(user_result_indexes) > 1:
        raise ValueError("Agent outcome may declare at most one USER_RESULT")
    if user_result_indexes:
        index = user_result_indexes[0]
        proposal = outcome.proposed_artifact_drafts[index]
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
            raise ValueError("Agent outcome proposal list changed during finalization")
        raw_proposal = raw_proposals[index]
        if not isinstance(raw_proposal, dict):
            raise ValueError("USER_RESULT proposal must be one JSON object")
        raw_proposal["declared_size"] = result_document.size
        raw_proposal["declared_sha256"] = result_document.sha256
        outcome = AgentJobOutcome.model_validate(draft_value)

    outcome_bytes = canonical_json_bytes(outcome)
    atomic_replace_agent_json(outcome_path, outcome_bytes)
    marker = FinalizedAgentOutcomeMarker(
        schema_version=1,
        relative_path=OUTCOME_RELATIVE_PATH,
        size=len(outcome_bytes),
        sha256=hashlib.sha256(outcome_bytes).hexdigest(),
    )
    marker_path = resolve_workspace_path(
        root,
        FINALIZATION_MARKER_RELATIVE_PATH,
        must_exist=False,
    )
    atomic_replace_agent_json(marker_path, canonical_json_bytes(marker))
    return marker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="problem-locator-finalize-outcome")
    parser.parse_args(argv)
    try:
        finalize_agent_outcome(Path.cwd())
    except (OSError, TypeError, ValueError):
        print(
            "problem-locator-finalize-outcome: Agent output finalization failed",
            file=sys.stderr,
        )
        return 2
    print(OUTCOME_RELATIVE_PATH)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "FINALIZATION_MARKER_NAME",
    "FINALIZATION_MARKER_RELATIVE_PATH",
    "FinalizedAgentOutcomeMarker",
    "OUTCOME_RELATIVE_PATH",
    "finalize_agent_outcome",
    "main",
]
