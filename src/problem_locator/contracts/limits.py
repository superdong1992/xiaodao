"""Frozen version markers, resource limits, and revision semantics."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping


SCHEMA_VERSION = 8
CONTRACT_REVISION = "v8-contract-r1"
GENERATOR_VERSION = "8"

ROUTER_CONTEXT_BYTES = 131_072
DIAGNOSE_CONTEXT_BYTES = 262_144
SPECIALIST_CONTEXT_BYTES = DIAGNOSE_CONTEXT_BYTES
REVIEW_CONTEXT_BYTES = 204_800
REVIEWER_CONTEXT_BYTES = REVIEW_CONTEXT_BYTES
MAX_ATTACHMENT_BYTES = 2_684_354_560
MAX_CASE_RESOURCE_BYTES = 5_368_709_120
JOB_WALL_TIME_SECONDS = 1_800
JOB_STDOUT_STDERR_BYTES = 67_108_864
JOB_WORKSPACE_BYTES = 1_073_741_824
ACTIVE_WORKERS = 1

UPLOAD_TEMP_RETENTION_SECONDS = 86_400
PROPOSAL_STAGING_RETENTION_SECONDS = 86_400
WORKSPACE_RETENTION_SECONDS = 86_400
ORPHAN_RESOURCE_RETENTION_SECONDS = 604_800

MAX_USER_TEXT_UTF8_BYTES = 65_536
MAX_INITIAL_USER_FACTS = 64
MAX_DESCRIPTION_UTF8_BYTES = 4_096
MAX_WAIT_SECONDS = 30


RevisionDelta = int | Literal["set_1", "n/a"]


@dataclass(frozen=True, slots=True)
class RevisionRule:
    case_revision: RevisionDelta
    diagnosis_state_revision: RevisionDelta


REVISION_MATRIX: Mapping[str, RevisionRule] = MappingProxyType(
    {
        "CREATE_CASE": RevisionRule("set_1", "set_1"),
        "JOB_LIFECYCLE": RevisionRule(1, 0),
        "ROUTE_OUTCOME": RevisionRule(1, 0),
        "SEMANTIC_OUTCOME_OR_SUPPLEMENT": RevisionRule(1, 1),
        "OUTCOME_WITH_EMPTY_STATE_DELTA": RevisionRule(1, 0),
        "DUPLICATE_OUTCOME": RevisionRule(0, 0),
        "FIRST_STALE_OUTCOME": RevisionRule(1, 0),
        "ATTACHMENT_LIFECYCLE": RevisionRule(1, 0),
        "CANCEL_FAIL_INTERRUPT_OR_RESUME": RevisionRule(1, 0),
        "PENDING_JOB_RESUME_WAKEUP": RevisionRule(0, 0),
        "RUNTIME_EPOCH_RECORD": RevisionRule("n/a", "n/a"),
        "READ_ONLY_OR_WAIT_TIMEOUT": RevisionRule(0, 0),
        "IDEMPOTENT_REPLAY": RevisionRule(0, 0),
        "STALE_ACTIVE_OUTCOME": RevisionRule(1, 0),
    }
)


def default_resource_limits(job_type: object) -> object:
    """Return frozen limits without introducing a module import cycle.

    The public return value is ``models.ResourceLimits``; the local imports keep
    version/limit constants as the single dependency-free authority.
    """

    from .enums import JobType
    from .models import ResourceLimits

    parsed = job_type if isinstance(job_type, JobType) else JobType(job_type)
    context_bytes = {
        JobType.ROUTE: ROUTER_CONTEXT_BYTES,
        JobType.DIAGNOSE: DIAGNOSE_CONTEXT_BYTES,
        JobType.REVIEW: REVIEW_CONTEXT_BYTES,
    }[parsed]
    return ResourceLimits(
        context_bytes=context_bytes,
        wall_time_seconds=JOB_WALL_TIME_SECONDS,
        stdout_stderr_bytes=JOB_STDOUT_STDERR_BYTES,
        workspace_bytes=JOB_WORKSPACE_BYTES,
    )


__all__ = [
    "ACTIVE_WORKERS",
    "CONTRACT_REVISION",
    "DIAGNOSE_CONTEXT_BYTES",
    "GENERATOR_VERSION",
    "JOB_STDOUT_STDERR_BYTES",
    "JOB_WALL_TIME_SECONDS",
    "JOB_WORKSPACE_BYTES",
    "MAX_ATTACHMENT_BYTES",
    "MAX_CASE_RESOURCE_BYTES",
    "MAX_DESCRIPTION_UTF8_BYTES",
    "MAX_INITIAL_USER_FACTS",
    "MAX_USER_TEXT_UTF8_BYTES",
    "MAX_WAIT_SECONDS",
    "ORPHAN_RESOURCE_RETENTION_SECONDS",
    "PROPOSAL_STAGING_RETENTION_SECONDS",
    "REVIEW_CONTEXT_BYTES",
    "REVIEWER_CONTEXT_BYTES",
    "REVISION_MATRIX",
    "RevisionRule",
    "ROUTER_CONTEXT_BYTES",
    "SCHEMA_VERSION",
    "SPECIALIST_CONTEXT_BYTES",
    "UPLOAD_TEMP_RETENTION_SECONDS",
    "WORKSPACE_RETENTION_SECONDS",
    "default_resource_limits",
]
