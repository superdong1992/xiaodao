"""Frozen Problem Locator V8 public contract package.

All later implementation slices import public vocabulary from this package.
The schema registry is the single source used to generate the twenty frozen
``schemas/v2/*.schema.json`` documents.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Final, Mapping

from . import (
    commands,
    enums,
    errors,
    limits,
    methods_reason_v2,
    methods_state_v2,
    methods_v2,
    models,
    outcomes,
    ports,
    serialization,
)
from .limits import CONTRACT_REVISION, GENERATOR_VERSION, SCHEMA_VERSION
from .models import (
    AgentJobOutcome,
    AgentJobOutcomeDraftV2,
    FixtureManifest,
    HandoffRecord,
    Job,
    JobOutcome,
    LogparseParseClaim,
    MethodsReviewerResultV2,
    MethodsTerminalProjectionV2,
    StateFile,
    UserResultPayloadV3,
    WorkspaceInputManifest,
)
from .methods_state_v2 import MethodStateV2, MethodTerminalResultV2
from .methods_v2 import (
    MethodConsensusV2,
    MethodEvidenceGraphV2,
    MethodEvaluationOutputItemV2,
    MethodEvaluationPlanV2,
    MethodLimitationsRecordV2,
    MethodRoleEvaluationV2,
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
        "method-consensus.schema.json": MethodConsensusV2,
        "method-evaluation-plan.schema.json": MethodEvaluationPlanV2,
        "method-evaluation-response.schema.json": tuple[
            MethodEvaluationOutputItemV2, ...
        ],
        "method-evidence-graph.schema.json": MethodEvidenceGraphV2,
        "method-limitations-record.schema.json": MethodLimitationsRecordV2,
        "method-role-evaluation.schema.json": MethodRoleEvaluationV2,
        "method-state.schema.json": MethodStateV2,
        "method-terminal-result.schema.json": MethodTerminalResultV2,
        "methods-reviewer-result.schema.json": MethodsReviewerResultV2,
        "methods-terminal-projection.schema.json": MethodsTerminalProjectionV2,
        "state.schema.json": StateFile,
        "user-result.schema.json": UserResultPayloadV3,
        "workspace-input-manifest.schema.json": WorkspaceInputManifest,
    }
)


_PUBLIC_MODULES = (
    commands,
    enums,
    errors,
    limits,
    methods_reason_v2,
    methods_state_v2,
    methods_v2,
    models,
    outcomes,
    ports,
    serialization,
)
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
        "methods_reason_v2",
        "methods_state_v2",
        "methods_v2",
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
