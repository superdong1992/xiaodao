"""Frozen Problem Locator V3 public contract package.

All later implementation slices import public vocabulary from this package.
The schema registry is the single source used to generate the ten frozen
``schemas/v2/*.schema.json`` documents.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Final, Mapping

from . import commands, enums, errors, limits, models, outcomes, ports, serialization
from .limits import CONTRACT_REVISION, GENERATOR_VERSION, SCHEMA_VERSION
from .models import (
    AgentJobOutcome,
    AgentJobOutcomeDraftV2,
    FixtureManifest,
    HandoffRecord,
    Job,
    JobOutcome,
    LogparseParseClaim,
    StateFile,
    UserResultPayloadV2,
    WorkspaceInputManifest,
)


SCHEMA_MODELS: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "agent-job-outcome.schema.json": AgentJobOutcome,
        "agent-job-outcome-draft.schema.json": AgentJobOutcomeDraftV2,
        "fixture-manifest.schema.json": FixtureManifest,
        "handoff.schema.json": HandoffRecord,
        "job-outcome.schema.json": JobOutcome,
        "job.schema.json": Job,
        "logparse-parse-claim.schema.json": LogparseParseClaim,
        "state.schema.json": StateFile,
        "user-result.schema.json": UserResultPayloadV2,
        "workspace-input-manifest.schema.json": WorkspaceInputManifest,
    }
)


_PUBLIC_MODULES = (commands, enums, errors, limits, models, outcomes, ports, serialization)
for _module in _PUBLIC_MODULES:
    for _name in getattr(_module, "__all__", ()):
        if _name not in {"BaseModel", "StrEnum"}:
            globals()[_name] = getattr(_module, _name)

__all__ = sorted(
    {
        "CONTRACT_REVISION",
        "GENERATOR_VERSION",
        "SCHEMA_MODELS",
        "SCHEMA_VERSION",
        "commands",
        "enums",
        "errors",
        "limits",
        "models",
        "outcomes",
        "ports",
        "serialization",
        *(
            name
            for module in _PUBLIC_MODULES
            for name in getattr(module, "__all__", ())
            if name not in {"BaseModel", "StrEnum"}
        ),
    }
)

del _module, _name
