"""Immutable values shared by Result v2 capture, finalization, and staging."""

from __future__ import annotations

from dataclasses import dataclass

from problem_locator.contracts import AgentArtifactProposalDraft, EvidenceBinding

from .authoritative_targets import AuthoritativeTargetLog


@dataclass(frozen=True, slots=True)
class CapturedTargetLog:
    """One server-authoritative target and its frozen Logparse bytes."""

    target: AuthoritativeTargetLog
    content: bytes
    evidence_bindings: tuple[EvidenceBinding, ...]


@dataclass(frozen=True, slots=True)
class ServerGeneratedResultFile:
    """One server-owned proposal draft paired with immutable in-memory bytes."""

    draft: AgentArtifactProposalDraft
    content: bytes


__all__ = ["CapturedTargetLog", "ServerGeneratedResultFile"]
