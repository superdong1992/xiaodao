"""Server-owned decision verification value shared by final result builders."""

from __future__ import annotations

from dataclasses import dataclass

from problem_locator.contracts import DecisionAuditV2


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """One immutable audit decision plus its bounded raw-line transcript."""

    audit: DecisionAuditV2
    positive_gate_passed: bool
    decision_evidence_bytes: bytes


__all__ = ["VerificationResult"]
