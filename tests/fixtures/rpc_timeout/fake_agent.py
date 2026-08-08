#!/usr/bin/env python3
"""Deterministic external Agent used by the S08 RPC-timeout E2E.

The process deliberately behaves like an untrusted Agent: it reads only the
bounded context and immutable Workspace manifest, uses the injected logparse
broker capability, and writes the one frozen Agent output file.  It never
imports application, domain, storage, or dispatch implementations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, os.fspath(REPOSITORY_ROOT / "src"))

from problem_locator.contracts import (  # noqa: E402
    AgentArtifactProposalDraft,
    AgentEvidenceCitation,
    AgentEvidenceProposalDraft,
    AgentJobOutcomeDraftV2,
    AgentRuleClaim,
    ArtifactKind,
    CandidateConclusionDraft,
    CompletionCriterionDraftMapping,
    DiagnosisOutcome,
    DiagnosisStateDelta,
    EvidenceBinding,
    EvidenceSourceBinding,
    EvidenceSourceType,
    JobOutcome,
    JobType,
    LogparseEvidenceLocator,
    OutcomeResultType,
    PendingRequirement,
    RequirementStatus,
    ResourceKind,
    ReviewAssessment,
    ReviewVerdict,
    RuleClaimResult,
    RouteDecision,
    RouteKind,
    UserResultMetadata,
    UserResultArchiveMetadata,
    UserResultPayload,
    WorkspaceInputManifest,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.integrations.logparse import (  # noqa: E402
    Anchor,
    ParseTargetsRequest,
    TargetLogsRequest,
)
from problem_locator.integrations.logparse import cli as logparse_cli  # noqa: E402
from problem_locator.integrations.logparse.outputs import (  # noqa: E402
    inspect_controlled_run,
)
from problem_locator.integrations.result_archive import build_result_archive  # noqa: E402
from problem_locator.runtime.outcome_finalizer import (  # noqa: E402
    seal_agent_outcome_draft,
)


PARAMETER_REQUIREMENTS = (
    (
        "00000000-0000-0000-0000-000000000101",
        "caller_service",
        "Provide the RPC caller service.",
    ),
    (
        "00000000-0000-0000-0000-000000000102",
        "server_service",
        "Provide the RPC server service.",
    ),
    (
        "00000000-0000-0000-0000-000000000103",
        "rpc_method",
        "Provide the timed-out RPC method.",
    ),
    (
        "00000000-0000-0000-0000-000000000104",
        "problem_time",
        "Provide the millisecond UTC problem time.",
    ),
)
ATTACHMENT_REQUIREMENT_ID = "00000000-0000-0000-0000-000000000105"
ORDER_REQUIREMENT_ID = "00000000-0000-0000-0000-000000000106"
ARCHIVE_BYTES_MARKER = b"synthetic payment-to-inventory RPC timeout archive"
EVIDENCE_IDS = (
    "00000000-0000-0000-0000-000000000040",
    "00000000-0000-0000-0000-000000000041",
)
RAW_LOGPARSE_SENTINELS = (
    b"s08-raw-logparse-repo-sentinel",
    b"s08-raw-logparse-config-sentinel",
    b"s08-raw-logparse-python-sentinel",
    b"s08-stale-broker-endpoint-sentinel",
    b"s08-stale-broker-token-sentinel",
)
RAW_LOGPARSE_KEYS = (
    b"LOGPARSE_REPO",
    b"LOGPARSE_CONFIG_PATH",
    b"LOGPARSE_PYTHON",
    b"PROBLEM_LOCATOR_LOGPARSE_ENDPOINT",
    b"PROBLEM_LOCATOR_LOGPARSE_TOKEN",
)


def _section(body: str, name: str) -> str:
    match = re.search(
        rf"<<<SECTION [0-9]+ {re.escape(name)}>>>\n(.*?)<<<END SECTION>>>\n",
        body,
        flags=re.DOTALL,
    )
    if match is None:
        raise RuntimeError(f"required context section is absent: {name}")
    return match.group(1).rstrip("\n")


def _sections(body: str, name: str) -> list[str]:
    return [
        match.group(1).rstrip("\n")
        for match in re.finditer(
            rf"<<<SECTION [0-9]+ {re.escape(name)}>>>\n(.*?)<<<END SECTION>>>\n",
            body,
            flags=re.DOTALL,
        )
    ]


def _golden_json(name: str) -> tuple[bytes, object]:
    payload = (REPOSITORY_ROOT / "tests/fixtures/rpc_timeout" / name).read_bytes()
    parsed = parse_canonical_json_bytes(payload)
    if canonical_json_bytes(parsed) != payload:
        raise RuntimeError(f"golden fixture is not canonical: {name}")
    return payload, parsed


def _assert_golden_json(path: Path, name: str) -> object:
    actual = path.read_bytes()
    expected, parsed = _golden_json(name)
    if actual != expected:
        raise RuntimeError(f"broker output drifted from golden fixture: {name}")
    if parse_canonical_json_bytes(actual) != parsed:
        raise RuntimeError(f"broker output failed typed JSON comparison: {name}")
    return parsed


def _assert_parse_targets_golden(
    path: Path,
    name: str,
) -> tuple[dict[str, object], AgentArtifactProposalDraft]:
    actual = path.read_bytes()
    payload = parse_canonical_json_bytes(actual)
    if canonical_json_bytes(payload) != actual or not isinstance(payload, dict):
        raise RuntimeError("broker parse-targets output is not canonical JSON")
    target_payload = dict(payload)
    artifact_payload = target_payload.pop("logparse_run_artifact_draft", None)
    expected, parsed = _golden_json(name)
    if canonical_json_bytes(target_payload) != expected or target_payload != parsed:
        raise RuntimeError(f"broker output drifted from golden fixture: {name}")
    try:
        artifact = AgentArtifactProposalDraft.model_validate(artifact_payload)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("broker returned an invalid LOGPARSE_RUN draft") from exc
    return target_payload, artifact


def _sensitive_needles() -> tuple[bytes, ...]:
    workspace = Path.cwd().resolve()
    data_root = workspace.parents[2]
    capability_values = tuple(
        value.encode("utf-8")
        for name in (
            "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT",
            "PROBLEM_LOCATOR_LOGPARSE_TOKEN",
        )
        if (value := os.environ.get(name))
    )
    return (
        os.fspath(data_root).encode("utf-8"),
        ARCHIVE_BYTES_MARKER,
        *RAW_LOGPARSE_KEYS,
        *RAW_LOGPARSE_SENTINELS,
        *capability_values,
    )


def _assert_no_sensitive_output(outcome_bytes: bytes) -> None:
    needles = _sensitive_needles()
    surfaces = [("job_outcome.json", outcome_bytes)]
    proposals = Path("output/proposals")
    if proposals.is_dir():
        surfaces.extend(
            (path.as_posix(), path.read_bytes())
            for path in sorted(proposals.rglob("*"))
            if path.is_file()
        )
    for surface, payload in surfaces:
        for index, needle in enumerate(needles):
            if needle in payload:
                raise RuntimeError(
                    f"sensitive test sentinel {index} leaked into {surface}"
                )


def _empty_delta(**updates: object) -> DiagnosisStateDelta:
    payload: dict[str, object] = {
        "problem_spec_patch": None,
        "add_user_facts": [],
        "proposed_facts": [],
        "add_active_hypotheses": [],
        "update_hypotheses": [],
        "reject_hypotheses": [],
        "add_open_questions": [],
        "resolve_questions": [],
        "add_pending_requirements": [],
        "fulfill_requirements": [],
        "add_evidence_bindings": [],
    }
    payload.update(updates)
    return DiagnosisStateDelta.model_validate(payload)


def _skill_manifest(context: str) -> dict[str, object]:
    skill = _section(context, "SKILL")
    match = re.search(
        r"<!-- DIAGNOSIS_SKILL_MANIFEST_V3_BEGIN -->\s*"
        r"```json\s*(\{.*?\})\s*```\s*"
        r"<!-- DIAGNOSIS_SKILL_MANIFEST_V3_END -->",
        skill,
        flags=re.DOTALL,
    )
    if match is None:
        raise RuntimeError("the pinned Skill manifest v3 is absent")
    value = json.loads(match.group(1))
    if not isinstance(value, dict) or value.get("schema_version") != 3:
        raise RuntimeError("the pinned Skill manifest is not v3")
    return value


def _pinned_requirement(
    skill_manifest: dict[str, object],
    name: str,
) -> dict[str, object]:
    requirements = skill_manifest.get("requirements")
    if not isinstance(requirements, list):
        raise RuntimeError("the pinned Skill requirements are absent")
    matches = [
        item
        for item in requirements
        if isinstance(item, dict) and item.get("name") == name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"the pinned Skill requirement is not unique: {name}")
    return matches[0]


def _pending_requirement(
    *,
    requirement_id: str,
    pinned: dict[str, object],
    job_id: str,
) -> PendingRequirement:
    return PendingRequirement.model_validate(
        {
            "requirement_id": requirement_id,
            "kind": pinned["kind"],
            "name": pinned["name"],
            "prompt": pinned["prompt"],
            "required": True,
            "constraints": pinned["constraints"],
            "status": RequirementStatus.OPEN.value,
            "requested_by_job_id": job_id,
            "fulfilled_by_refs": [],
            "supplement_policy": pinned["supplement_policy"],
        }
    )


def _fact_ids(snapshot: dict[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    raw_facts = snapshot.get("user_facts")
    if not isinstance(raw_facts, list):
        raise RuntimeError("ContextSnapshot user_facts is invalid")
    for item in raw_facts:
        if not isinstance(item, dict):
            raise RuntimeError("ContextSnapshot user fact is invalid")
        provenance = item.get("provenance")
        if not isinstance(provenance, dict):
            raise RuntimeError("ContextSnapshot user fact provenance is invalid")
        name = provenance.get("input_name")
        item_id = item.get("item_id")
        if not isinstance(name, str) or not isinstance(item_id, str) or name in result:
            raise RuntimeError("ContextSnapshot user facts are ambiguous")
        result[name] = item_id
    return result


def _rule_events(rule: dict[str, object]) -> list[str]:
    kind = rule["kind"]
    parameters = rule["parameters"]
    if not isinstance(parameters, dict):
        raise RuntimeError("the pinned verification rule parameters are invalid")
    if kind in {"EVENT_PRESENT", "EVENT_TIME_WINDOW", "FACT_FIELD_EQUALS"}:
        return [str(parameters["event"])]
    if kind == "ROLE_COVERAGE":
        return [str(item["event"]) for item in parameters["coverage"]]
    if kind == "CROSS_ROLE_CORRELATION":
        return [str(item["event"]) for item in parameters["members"]]
    if kind == "EVENT_ORDER":
        return [str(parameters["before_event"]), str(parameters["after_event"])]
    if kind == "SEMANTIC_CAUSALITY":
        return [str(item) for item in parameters["evidence_events"]]
    raise RuntimeError(f"unsupported pinned verification rule kind: {kind}")


def _rule_claims(
    *,
    skill_manifest: dict[str, object],
    snapshot: dict[str, object],
    event_bindings: dict[str, tuple[EvidenceBinding, int]],
) -> list[AgentRuleClaim]:
    verification = skill_manifest.get("verification_contract")
    if not isinstance(verification, dict):
        raise RuntimeError("the pinned verification contract is absent")
    rules = verification.get("rules")
    if not isinstance(rules, list) or not rules:
        raise RuntimeError("the pinned verification rules are absent")
    facts = _fact_ids(snapshot)
    claims: list[AgentRuleClaim] = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise RuntimeError("the pinned verification rule is invalid")
        kind = str(rule["kind"])
        parameters = rule["parameters"]
        if not isinstance(parameters, dict):
            raise RuntimeError("the pinned verification parameters are invalid")
        fact_refs: list[str] = []
        missing_fact = False
        if kind == "EVENT_TIME_WINDOW":
            reference = parameters["reference"]
            if not isinstance(reference, dict):
                raise RuntimeError("the pinned time reference is invalid")
            if reference.get("source") == "USER_FACT":
                fact_id = facts.get(str(reference["name"]))
                if fact_id is None:
                    missing_fact = True
                else:
                    fact_refs = [fact_id]
        elif kind == "FACT_FIELD_EQUALS":
            fact_id = facts.get(str(parameters["fact_name"]))
            if fact_id is None:
                missing_fact = True
            else:
                fact_refs = [fact_id]

        citations: list[AgentEvidenceCitation] = []
        seen: set[tuple[str, int]] = set()
        for event_id in _rule_events(rule):
            bound = event_bindings.get(event_id)
            if bound is None:
                continue
            binding, line = bound
            key = (
                binding.existing_evidence_id
                or f"proposal:{binding.evidence_proposal_key}",
                line,
            )
            if key in seen:
                continue
            seen.add(key)
            citations.append(
                AgentEvidenceCitation(
                    evidence_binding=binding,
                    line_start=line,
                    line_end=line,
                )
            )

        has_complete_events = all(
            event_id in event_bindings for event_id in _rule_events(rule)
        )
        claimed_result = (
            RuleClaimResult.PASS
            if has_complete_events and not missing_fact
            else RuleClaimResult.UNKNOWN
        )
        claims.append(
            AgentRuleClaim(
                rule_id=str(rule["id"]),
                claimed_result=claimed_result,
                fact_refs=fact_refs,
                citations=citations,
                explanation=(
                    "The cited raw events satisfy the pinned rule."
                    if claimed_result is RuleClaimResult.PASS
                    else "The fixed inputs are not yet sufficient to decide this rule."
                ),
            )
        )
    return claims


def _agent_outcome(
    instruction: dict[str, object],
    *,
    result_type: OutcomeResultType,
    payload: object,
    rule_claims: list[AgentRuleClaim] | None = None,
    consumed_evidence_refs: list[str] | None = None,
    proposed_evidence_drafts: list[AgentEvidenceProposalDraft] | None = None,
    proposed_artifact_drafts: list[AgentArtifactProposalDraft] | None = None,
) -> AgentJobOutcomeDraftV2:
    job_id = str(instruction["job_id"])
    return AgentJobOutcomeDraftV2(
        schema_version=2,
        job_id=job_id,
        case_id=_manifest().case_id,
        job_type=JobType(str(instruction["job_type"])),
        base_state_revision=int(instruction["base_state_revision"]),
        result_type=result_type,
        payload=payload,
        consumed_evidence_refs=consumed_evidence_refs or [],
        proposed_evidence_drafts=proposed_evidence_drafts or [],
        proposed_artifact_drafts=proposed_artifact_drafts or [],
        error=None,
        rule_claims=rule_claims or [],
    )


def _manifest() -> WorkspaceInputManifest:
    return WorkspaceInputManifest.model_validate_json(
        Path("inputs/manifest.json").read_bytes()
    )


def _write_outcome(outcome: AgentJobOutcomeDraftV2) -> None:
    payload = canonical_json_bytes(outcome)
    _assert_no_sensitive_output(payload)
    Path("output/job_outcome.draft.json").write_bytes(payload)
    seal_agent_outcome_draft(Path.cwd())


def _record_invocation(instruction: dict[str, object]) -> None:
    configured = os.environ.get("S08_FAKE_AGENT_RECORD")
    if configured is None:
        return
    record = {
        "job_id": instruction["job_id"],
        "job_type": instruction["job_type"],
        "pid": os.getpid(),
    }
    path = Path(configured)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, separators=(",", ":"), sort_keys=True))
        stream.write("\n")


def _resolved_anchors(manifest: WorkspaceInputManifest) -> list[Anchor]:
    plan = manifest.resolved_logparse_plan
    if plan is None:
        raise RuntimeError("the server-resolved Logparse plan is absent")
    return [Anchor.model_validate(item.model_dump(mode="json")) for item in plan.anchors]


def _event_bindings(
    bindings: list[EvidenceBinding],
) -> dict[str, tuple[EvidenceBinding, int]]:
    if len(bindings) != 2:
        raise RuntimeError("the RPC fixture requires exactly two Evidence bindings")
    return {
        "client_timeout": (bindings[0], 1),
        "server_takeover_accepted": (bindings[1], 1),
        "server_pool_wait_complete": (bindings[1], 2),
    }


def _invoke_broker(operation: str, proposal_key: str, request: object) -> Path:
    proposal = Path("output/proposals") / proposal_key
    proposal.mkdir(parents=True, exist_ok=True)
    (proposal / "request.json").write_bytes(canonical_json_bytes(request))
    result = logparse_cli.main(
        [
            operation,
            "--request",
            f"output/proposals/{proposal_key}/request.json",
            "--result",
            f"output/proposals/{proposal_key}/target_logs.json",
        ]
    )
    if result != 0:
        raise RuntimeError(f"logparse broker operation failed: {operation}")
    return proposal / "target_logs.json"


def _validated_previous_outcomes(
    context: str,
    manifest: WorkspaceInputManifest,
) -> list[JobOutcome]:
    previous_sections = _sections(context, "PREVIOUS_OUTCOME")
    previous_entries = [
        entry for entry in manifest.entries if entry.input_kind == "PREVIOUS_OUTCOME"
    ]
    if len(previous_sections) != len(previous_entries) or not previous_sections:
        raise RuntimeError("previous Outcome sections do not match the manifest")
    outcomes = [
        parse_canonical_json_bytes((section + "\n").encode("utf-8"), JobOutcome)
        for section in previous_sections
    ]
    if [outcome.outcome_id for outcome in outcomes] != [
        entry.resource_id for entry in previous_entries
    ]:
        raise RuntimeError("previous Outcome section order drifted from the manifest")
    for outcome, entry in zip(outcomes, previous_entries, strict=True):
        if (
            outcome.job_id != entry.source_job_id
            or outcome.result_type is not entry.result_type
        ):
            raise RuntimeError("previous Outcome metadata drifted from the manifest")
    waiting_for_order = [
        outcome
        for outcome in outcomes
        if outcome.result_type is OutcomeResultType.NEED_INPUT
        and isinstance(outcome.payload, DiagnosisOutcome)
        and any(
            requirement.requirement_id == ORDER_REQUIREMENT_ID
            and requirement.name == "order_id"
            for requirement in outcome.payload.state_delta.add_pending_requirements
        )
        and ORDER_REQUIREMENT_ID in outcome.payload.requested_input
    ]
    if len(waiting_for_order) != 1 or waiting_for_order[0] != outcomes[0]:
        raise RuntimeError("the current waiting order Outcome is not first and unique")
    return outcomes


def _route(
    instruction: dict[str, object],
    context: str,
) -> AgentJobOutcomeDraftV2:
    skill_index = json.loads(_section(context, "SKILL_INDEX"))
    matches = [
        item["ref"]
        for item in skill_index["skills"]
        if item["ref"]["id"] == "diagnosis-skill/diagnose-service-takeover"
    ]
    if len(matches) != 1:
        raise RuntimeError("the RPC diagnosis skill is not uniquely routable")
    return _agent_outcome(
        instruction,
        result_type=OutcomeResultType.COMPLETED,
        payload=RouteDecision(
            kind=RouteKind.MATCHED,
            skill_ref=matches[0],
            reason="The fixed catalog contains the RPC service-takeover skill.",
            confidence=1.0,
        ),
    )


def _request_parameter_group_a(
    instruction: dict[str, object],
    skill_manifest: dict[str, object],
    snapshot: dict[str, object],
) -> AgentJobOutcomeDraftV2:
    job_id = str(instruction["job_id"])
    requirements = [
        _pending_requirement(
            requirement_id=requirement_id,
            pinned=_pinned_requirement(skill_manifest, name),
            job_id=job_id,
        )
        for requirement_id, name, _ in PARAMETER_REQUIREMENTS
    ]
    return _agent_outcome(
        instruction,
        result_type=OutcomeResultType.NEED_INPUT,
        payload=DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(add_pending_requirements=requirements),
            requested_input=[item.requirement_id for item in requirements],
            requested_attachments=[],
            candidate_conclusion_draft=None,
            recommended_next_step="Collect the complete parameter group A.",
        ),
        rule_claims=_rule_claims(
            skill_manifest=skill_manifest,
            snapshot=snapshot,
            event_bindings={},
        ),
    )


def _request_attachment(
    instruction: dict[str, object],
    skill_manifest: dict[str, object],
    snapshot: dict[str, object],
) -> AgentJobOutcomeDraftV2:
    job_id = str(instruction["job_id"])
    requirement = _pending_requirement(
        requirement_id=ATTACHMENT_REQUIREMENT_ID,
        pinned=_pinned_requirement(skill_manifest, "log_archive"),
        job_id=job_id,
    )
    return _agent_outcome(
        instruction,
        result_type=OutcomeResultType.NEED_ATTACHMENT,
        payload=DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(add_pending_requirements=[requirement]),
            requested_input=[],
            requested_attachments=[requirement.requirement_id],
            candidate_conclusion_draft=None,
            recommended_next_step="Upload and explicitly submit the log archive.",
        ),
        rule_claims=_rule_claims(
            skill_manifest=skill_manifest,
            snapshot=snapshot,
            event_bindings={},
        ),
    )


def _first_log_analysis(
    instruction: dict[str, object],
    manifest: WorkspaceInputManifest,
    skill_manifest: dict[str, object],
    snapshot: dict[str, object],
) -> AgentJobOutcomeDraftV2:
    plan = manifest.resolved_logparse_plan
    if plan is None or plan.attachment_id is None or plan.artifact_id is not None:
        raise RuntimeError("the first Logparse Job lacks its resolved attachment plan")
    proposal_key = "logparse-run"
    request = ParseTargetsRequest(
        schema_version=1,
        problem_time=plan.problem_time,
        anchors=_resolved_anchors(manifest),
        attachment_id=plan.attachment_id,
        artifact_proposal_key=proposal_key,
    )
    target_result = _invoke_broker("parse-targets", proposal_key, request)
    target_payload, artifact = _assert_parse_targets_golden(
        target_result,
        "expected-target-logs.json",
    )
    if not isinstance(target_payload, dict) or len(target_payload["target_logs"]) != 2:
        raise RuntimeError("target-log golden has an invalid typed shape")
    tree = Path("output/proposals") / proposal_key / "tree"
    run = inspect_controlled_run(tree, product="compact")
    parse_manifest = tree / run.parse_manifest_relative_path
    parse_payload = _assert_golden_json(
        parse_manifest,
        "expected-parse-manifest.json",
    )
    if not isinstance(parse_payload, dict) or parse_payload.get("product") != "compact":
        raise RuntimeError("parse-manifest golden has an invalid typed shape")
    if (
        artifact.artifact_kind is not ArtifactKind.LOGPARSE_RUN
        or artifact.proposal_key != proposal_key
        or artifact.workspace_relative_path
        != f"output/proposals/{proposal_key}/tree"
        or artifact.metadata.tree_manifest_sha256 != run.sha256
        or artifact.metadata.parse_manifest_relative_path
        != run.parse_manifest_relative_path
    ):
        raise RuntimeError("broker LOGPARSE_RUN draft drifted from the controlled run")
    evidence_specs = (
        (
            "rpc-timeout-evidence",
            target_payload["target_logs"][0]["log_path"],
            1,
            1,
            "2026-07-31T00:00:03.000Z",
            "2026-07-31T00:00:03.000Z",
            "The payment caller exceeded its inventory RPC deadline.",
        ),
        (
            "rpc-timeout-server-evidence",
            target_payload["target_logs"][1]["log_path"],
            1,
            2,
            "2026-07-31T00:00:00.100Z",
            "2026-07-31T00:00:02.900Z",
            "The inventory server exhausted its RPC connection pool.",
        ),
    )
    evidence = [
        AgentEvidenceProposalDraft(
            proposal_key=evidence_key,
            source_type=EvidenceSourceType.LOGPARSE,
            source_binding=EvidenceSourceBinding(
                existing_source_ref=None,
                artifact_proposal_key=proposal_key,
            ),
            locator=LogparseEvidenceLocator(
                kind="LOGPARSE",
                relative_path=relative_path,
                start_line=start_line,
                end_line=end_line,
                start_time=start_time,
                end_time=end_time,
            ),
            summary=summary,
            workspace_relative_path=None,
            declared_size=None,
            declared_sha256=None,
        )
        for (
            evidence_key,
            relative_path,
            start_line,
            end_line,
            start_time,
            end_time,
            summary,
        ) in evidence_specs
    ]
    order_requirement = _pending_requirement(
        requirement_id=ORDER_REQUIREMENT_ID,
        pinned=_pinned_requirement(skill_manifest, "order_id"),
        job_id=str(instruction["job_id"]),
    )
    bindings = [
        EvidenceBinding(
            existing_evidence_id=None,
            evidence_proposal_key=item.proposal_key,
        )
        for item in evidence
    ]
    return _agent_outcome(
        instruction,
        result_type=OutcomeResultType.NEED_INPUT,
        payload=DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(
                add_pending_requirements=[order_requirement],
                add_evidence_bindings=bindings,
            ),
            requested_input=[order_requirement.requirement_id],
            requested_attachments=[],
            candidate_conclusion_draft=None,
            recommended_next_step="Provide order_id and reuse the persisted parse run.",
        ),
        proposed_evidence_drafts=evidence,
        proposed_artifact_drafts=[artifact],
        rule_claims=_rule_claims(
            skill_manifest=skill_manifest,
            snapshot=snapshot,
            event_bindings=_event_bindings(bindings),
        ),
    )


def _candidate(
    instruction: dict[str, object],
    manifest: WorkspaceInputManifest,
    snapshot: dict[str, object],
    context: str,
    skill_manifest: dict[str, object],
) -> AgentJobOutcomeDraftV2:
    _validated_previous_outcomes(context, manifest)
    plan = manifest.resolved_logparse_plan
    if plan is None or plan.artifact_id is None or plan.attachment_id is not None:
        raise RuntimeError("the follow-up Job lacks its resolved artifact plan")
    artifact = next(
        entry
        for entry in manifest.entries
        if entry.input_kind == "ARTIFACT" and entry.resource_id == plan.artifact_id
    )
    target_result = _invoke_broker(
        "target-logs",
        "reuse-logparse-run",
        TargetLogsRequest(
            schema_version=1,
            problem_time=plan.problem_time,
            anchors=_resolved_anchors(manifest),
            artifact_id=plan.artifact_id,
        ),
    )
    target_payload = _assert_golden_json(
        target_result,
        "expected-target-logs.json",
    )
    if not isinstance(target_payload, dict) or len(target_payload["target_logs"]) != 2:
        raise RuntimeError("reused target-log golden has an invalid typed shape")
    evidence_ids = [
        entry.resource_id for entry in manifest.entries if entry.input_kind == "EVIDENCE"
    ]
    if evidence_ids != list(EVIDENCE_IDS):
        raise RuntimeError("the deterministic formal Evidence ID drifted")
    evidence_bindings = [
        EvidenceBinding(
            existing_evidence_id=evidence_id,
            evidence_proposal_key=None,
        )
        for evidence_id in evidence_ids
    ]
    problem_spec = snapshot["problem_spec"]
    assert isinstance(problem_spec, dict)
    criterion = str(problem_spec["completion_criteria"][0])
    mapping = CompletionCriterionDraftMapping(
        criterion_index=0,
        criterion=criterion,
        satisfied=True,
        evidence_bindings=evidence_bindings,
        explanation="The request identifier appears in the parsed log.",
    )
    candidate = CandidateConclusionDraft(
        proposal_key="candidate",
        existing_conclusion_id=None,
        statement="The inventory RPC exceeded its deadline.",
        supporting_evidence_bindings=evidence_bindings,
        completion_criteria_mapping=[mapping],
    )
    result = UserResultPayload(
        schema_version=1,
        format_id="problem-locator-diagnosis-v1",
        problem_statement=str(problem_spec["statement"]),
        candidate_statement=candidate.statement,
        supporting_evidence_bindings=candidate.supporting_evidence_bindings,
        completion_criteria_mapping=candidate.completion_criteria_mapping,
    )
    result_bytes = canonical_json_bytes(result)
    proposal = Path("output/proposals/user-result")
    proposal.mkdir(parents=True, exist_ok=True)
    (proposal / "payload").write_bytes(result_bytes)
    user_result = AgentArtifactProposalDraft(
        proposal_key="user-result",
        artifact_kind=ArtifactKind.USER_RESULT,
        name="diagnosis-result.json",
        content_type="application/json",
        resource_kind=ResourceKind.FILE,
        workspace_relative_path="output/proposals/user-result/payload",
        declared_size=len(result_bytes),
        declared_sha256=hashlib.sha256(result_bytes).hexdigest(),
        metadata=UserResultMetadata(
            schema_version=1,
            format_id="problem-locator-diagnosis-v1",
            description="Diagnosis result",
        ),
    )
    archive_key = "user-result-archive"
    archive_request = Path("output/proposals") / archive_key / "request.json"
    archive_request.parent.mkdir(parents=True, exist_ok=True)
    target_log_paths = [
        f"inputs/artifacts/{artifact.resource_id}/tree/{item['log_path']}"
        for item in target_payload["target_logs"]
    ]
    archive_request.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "result_text": candidate.statement + "\n",
                "target_log_paths": target_log_paths,
            }
        )
    )
    archive_relative_path = f"output/proposals/{archive_key}/result.zip"
    archive_path = build_result_archive(
        Path.cwd(),
        archive_request.as_posix(),
        archive_relative_path,
    )
    archive_bytes = archive_path.read_bytes()
    result_archive = AgentArtifactProposalDraft(
        proposal_key=archive_key,
        artifact_kind=ArtifactKind.USER_RESULT_ARCHIVE,
        name="result.zip",
        content_type="application/zip",
        resource_kind=ResourceKind.FILE,
        workspace_relative_path=archive_relative_path,
        declared_size=len(archive_bytes),
        declared_sha256=hashlib.sha256(archive_bytes).hexdigest(),
        metadata=UserResultArchiveMetadata(
            schema_version=1,
            format_id="problem-locator-result-archive-v1",
            description="Controlled user-facing diagnosis archive.",
            user_result_proposal_key="user-result",
            target_log_count=len(target_log_paths),
        ),
    )
    return _agent_outcome(
        instruction,
        result_type=OutcomeResultType.COMPLETED,
        payload=DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(),
            requested_input=[],
            requested_attachments=[],
            candidate_conclusion_draft=candidate,
            recommended_next_step="Submit the fixed candidate for independent review.",
        ),
        consumed_evidence_refs=evidence_ids,
        proposed_artifact_drafts=[user_result, result_archive],
        rule_claims=_rule_claims(
            skill_manifest=skill_manifest,
            snapshot=snapshot,
            event_bindings=_event_bindings(evidence_bindings),
        ),
    )


def _review(
    instruction: dict[str, object],
    manifest: WorkspaceInputManifest,
    context: str,
    snapshot: dict[str, object],
    skill_manifest: dict[str, object],
) -> AgentJobOutcomeDraftV2:
    marker = os.environ.get("S08_REVIEW_ENTERED")
    release = os.environ.get("S08_REVIEW_RELEASE")
    if marker is not None:
        Path(marker).write_text(str(instruction["job_id"]), encoding="utf-8")
    if release is not None:
        deadline = time.monotonic() + 20.0
        while not Path(release).is_file():
            if time.monotonic() >= deadline:
                raise RuntimeError("review gate was not released")
            time.sleep(0.02)
    subject = json.loads(_section(context, "REVIEW_TARGET"))
    target = subject["candidate"]
    evidence_ids = list(subject["required_evidence_refs"])
    evidence_bindings = [
        EvidenceBinding(existing_evidence_id=evidence_id, evidence_proposal_key=None)
        for evidence_id in evidence_ids
    ]
    return _agent_outcome(
        instruction,
        result_type=OutcomeResultType.COMPLETED,
        payload=ReviewAssessment(
            candidate_conclusion_id=target["conclusion_id"],
            candidate_revision=target["revision"],
            candidate_content_hash=target["content_hash"],
            reviewed_state_revision=subject["reviewed_state_revision"],
            reviewed_evidence_refs=evidence_ids,
            verdict=ReviewVerdict.PASS,
            unsupported_findings=[],
            evidence_conflicts=[],
            missing_evidence=[],
            stale_references=[],
            recommendation="Accept the evidence-backed RPC timeout candidate.",
        ),
        consumed_evidence_refs=evidence_ids,
        rule_claims=_rule_claims(
            skill_manifest=skill_manifest,
            snapshot=snapshot,
            event_bindings=_event_bindings(evidence_bindings),
        ),
    )


def main() -> int:
    context = sys.stdin.buffer.read().decode("utf-8")
    instruction = json.loads(_section(context, "JOB_INSTRUCTION"))
    snapshot = json.loads(_section(context, "CONTEXT_SNAPSHOT"))
    manifest = _manifest()
    job_type = JobType(str(instruction["job_type"]))
    if job_type is JobType.ROUTE:
        outcome = _route(instruction, context)
    else:
        skill_manifest = _skill_manifest(context)
        if job_type is JobType.REVIEW:
            outcome = _review(
                instruction,
                manifest,
                context,
                snapshot,
                skill_manifest,
            )
            _record_invocation(instruction)
            _write_outcome(outcome)
            return 0
        entries = {entry.input_kind for entry in manifest.entries}
        user_fact_names = {
            item["provenance"]["input_name"] for item in snapshot["user_facts"]
        }
        if "ARTIFACT" in entries:
            outcome = _candidate(
                instruction,
                manifest,
                snapshot,
                context,
                skill_manifest,
            )
        elif "ATTACHMENT" in entries:
            outcome = _first_log_analysis(
                instruction,
                manifest,
                skill_manifest,
                snapshot,
            )
        elif {name for _, name, _ in PARAMETER_REQUIREMENTS} <= user_fact_names:
            outcome = _request_attachment(
                instruction,
                skill_manifest,
                snapshot,
            )
        else:
            outcome = _request_parameter_group_a(
                instruction,
                skill_manifest,
                snapshot,
            )
    _record_invocation(instruction)
    _write_outcome(outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
