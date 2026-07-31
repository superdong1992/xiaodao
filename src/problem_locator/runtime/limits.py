"""S04 aliases for the frozen S00 execution limits.

Values are deliberately imported rather than repeated so the contract package
remains their only authority.
"""

from problem_locator.contracts.limits import (
    DIAGNOSE_CONTEXT_BYTES,
    JOB_STDOUT_STDERR_BYTES,
    JOB_WALL_TIME_SECONDS,
    JOB_WORKSPACE_BYTES,
    REVIEW_CONTEXT_BYTES,
    ROUTER_CONTEXT_BYTES,
)

PROCESS_TERMINATION_GRACE_SECONDS = 5.0

__all__ = [
    "DIAGNOSE_CONTEXT_BYTES",
    "JOB_STDOUT_STDERR_BYTES",
    "JOB_WALL_TIME_SECONDS",
    "JOB_WORKSPACE_BYTES",
    "PROCESS_TERMINATION_GRACE_SECONDS",
    "REVIEW_CONTEXT_BYTES",
    "ROUTER_CONTEXT_BYTES",
]
