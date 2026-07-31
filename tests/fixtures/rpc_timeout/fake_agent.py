#!/usr/bin/env python3
"""Deterministic external Agent used by the S08 RPC-timeout E2E.

The process deliberately behaves like an untrusted Agent: it reads only the
bounded context and immutable Workspace manifest, uses the injected logparse
broker capability, and writes the one frozen Agent output file.  It never
imports application, domain, storage, or dispatch implementations.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, os.fspath(REPOSITORY_ROOT / "src"))

from problem_locator.contracts import (  # noqa: E402
    AgentArtifactProposalDraft,
    AgentEvidenceProposalDraft,
    AgentJobOutcome,
    ArtifactKind,
    AttachmentRequirementConstraints,
    CandidateConclusionDraft,
    CompletionCriterionDraftMapping,
    DiagnosisOutcome,
    DiagnosisStateDelta,
    EvidenceBinding,
    EvidenceSourceBinding,
    EvidenceSourceType,
    InputRequirementConstraints,
    JobOutcome,
    JobType,
    LogparseEvidenceLocator,
    LogparseRunMetadata,
    OutcomeResultType,
    PendingRequirement,
    RequirementKind,
    RequirementStatus,
    ResourceKind,
    ReviewAssessment,
    ReviewVerdict,
    RouteDecision,
    RouteKind,
    UserResultMetadata,
    UserResultPayload,
    VersionedRef,
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


PRODUCED_AT = "2026-07-31T00:10:00.000Z"
PROBLEM_TIME = "2026-07-31T00:00:03.000Z"
SKILL_REF = VersionedRef(
    id="diagnosis-skill/diagnose-service-takeover",
    version="2.0.0",
    content_hash=(
        "66ddd0b345df043b99489e26d9c0b7bc9ac9fa4f7ba3322783f956182ed17ba2"
    ),
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


def _outcome_id(job_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"s08-rpc-timeout:{job_id}"))


def _input_requirement(
    requirement_id: str,
    name: str,
    prompt: str,
    job_id: str,
) -> PendingRequirement:
    pattern = (
        r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
        r"[0-9]{2}\.[0-9]{3}Z$"
        if name == "problem_time"
        else r"^[A-Za-z0-9._:/-]+$"
    )
    return PendingRequirement(
        requirement_id=requirement_id,
        kind=RequirementKind.INPUT,
        name=name,
        prompt=prompt,
        required=True,
        constraints=InputRequirementConstraints(
            value_type="STRING",
            min_utf8_bytes=1,
            max_utf8_bytes=256,
            pattern=pattern,
            allowed_values=[],
        ),
        status=RequirementStatus.OPEN,
        requested_by_job_id=job_id,
        fulfilled_by_refs=[],
    )


def _agent_outcome(
    instruction: dict[str, object],
    *,
    result_type: OutcomeResultType,
    payload: object,
    consumed_evidence_refs: list[str] | None = None,
    proposed_evidence_drafts: list[AgentEvidenceProposalDraft] | None = None,
    proposed_artifact_drafts: list[AgentArtifactProposalDraft] | None = None,
) -> AgentJobOutcome:
    job_id = str(instruction["job_id"])
    return AgentJobOutcome(
        outcome_id=_outcome_id(job_id),
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
        produced_at=PRODUCED_AT,
    )


def _manifest() -> WorkspaceInputManifest:
    return WorkspaceInputManifest.model_validate_json(
        Path("inputs/manifest.json").read_bytes()
    )


def _write_outcome(outcome: AgentJobOutcome) -> None:
    payload = canonical_json_bytes(outcome)
    _assert_no_sensitive_output(payload)
    Path("output/job_outcome.json").write_bytes(payload)


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


def _anchors() -> list[Anchor]:
    return [
        Anchor(
            label="client",
            module="COMPACT",
            slot="1",
            process_name="checkout-client",
            pid="101",
        ),
        Anchor(
            label="server",
            module="COMPACT",
            slot="2",
            process_name="inventory-server",
            pid="202",
        ),
    ]


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


def _route(instruction: dict[str, object]) -> AgentJobOutcome:
    return _agent_outcome(
        instruction,
        result_type=OutcomeResultType.COMPLETED,
        payload=RouteDecision(
            kind=RouteKind.MATCHED,
            skill_ref=SKILL_REF,
            reason="The fixed catalog contains the RPC service-takeover skill.",
            confidence=1.0,
        ),
    )


def _request_parameter_group_a(
    instruction: dict[str, object],
) -> AgentJobOutcome:
    job_id = str(instruction["job_id"])
    requirements = [
        _input_requirement(requirement_id, name, prompt, job_id)
        for requirement_id, name, prompt in PARAMETER_REQUIREMENTS
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
    )


def _request_attachment(instruction: dict[str, object]) -> AgentJobOutcome:
    job_id = str(instruction["job_id"])
    requirement = PendingRequirement(
        requirement_id=ATTACHMENT_REQUIREMENT_ID,
        kind=RequirementKind.ATTACHMENT,
        name="log_archive",
        prompt="Upload the payment and inventory RPC logs.",
        required=True,
        constraints=AttachmentRequirementConstraints(
            allowed_content_types=["application/zip"],
            min_count=1,
            max_count=1,
        ),
        status=RequirementStatus.OPEN,
        requested_by_job_id=job_id,
        fulfilled_by_refs=[],
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
    )


def _first_log_analysis(
    instruction: dict[str, object],
    manifest: WorkspaceInputManifest,
) -> AgentJobOutcome:
    attachment = next(entry for entry in manifest.entries if entry.input_kind == "ATTACHMENT")
    proposal_key = "logparse-run"
    request = ParseTargetsRequest(
        schema_version=1,
        problem_time=PROBLEM_TIME,
        anchors=_anchors(),
        attachment_id=attachment.resource_id,
        artifact_proposal_key=proposal_key,
    )
    target_result = _invoke_broker("parse-targets", proposal_key, request)
    target_payload = _assert_golden_json(
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
    assert manifest.logparse_tool_ref is not None
    metadata = LogparseRunMetadata(
        tree_manifest_sha256=run.sha256,
        logparse_version_ref=manifest.logparse_tool_ref,
        parse_manifest_relative_path=run.parse_manifest_relative_path,
        source_attachment_id=attachment.resource_id,
        source_attachment_sha256=attachment.sha256,
        parse_parameters={"product": "compact"},
    )
    artifact = AgentArtifactProposalDraft(
        proposal_key=proposal_key,
        artifact_kind=ArtifactKind.LOGPARSE_RUN,
        name="rpc-timeout-logparse-run",
        content_type="application/vnd.problem-locator.logparse-run+directory",
        resource_kind=ResourceKind.DIRECTORY,
        workspace_relative_path=f"output/proposals/{proposal_key}/tree",
        declared_size=run.size,
        declared_sha256=run.sha256,
        metadata=metadata,
    )
    evidence_key = "rpc-timeout-evidence"
    evidence = AgentEvidenceProposalDraft(
        proposal_key=evidence_key,
        source_type=EvidenceSourceType.LOGPARSE,
        source_binding=EvidenceSourceBinding(
            existing_source_ref=None,
            artifact_proposal_key=proposal_key,
        ),
        locator=LogparseEvidenceLocator(
            kind="LOGPARSE",
            relative_path=(
                "task-synthetic/mech_modules/COMPACT/slot_1/cycle/"
                "checkout-client-101.log"
            ),
            start_line=1,
            end_line=1,
            start_time=None,
            end_time=None,
        ),
        summary="The payment caller exceeded its inventory RPC deadline.",
        workspace_relative_path=None,
        declared_size=None,
        declared_sha256=None,
    )
    order_requirement = _input_requirement(
        ORDER_REQUIREMENT_ID,
        "order_id",
        "Provide the order identifier that uniquely selects the request.",
        str(instruction["job_id"]),
    )
    binding = EvidenceBinding(
        existing_evidence_id=None,
        evidence_proposal_key=evidence_key,
    )
    return _agent_outcome(
        instruction,
        result_type=OutcomeResultType.NEED_INPUT,
        payload=DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(
                add_pending_requirements=[order_requirement],
                add_evidence_bindings=[binding],
            ),
            requested_input=[order_requirement.requirement_id],
            requested_attachments=[],
            candidate_conclusion_draft=None,
            recommended_next_step="Provide order_id and reuse the persisted parse run.",
        ),
        proposed_evidence_drafts=[evidence],
        proposed_artifact_drafts=[artifact],
    )


def _candidate(
    instruction: dict[str, object],
    manifest: WorkspaceInputManifest,
    snapshot: dict[str, object],
    context: str,
) -> AgentJobOutcome:
    _validated_previous_outcomes(context, manifest)
    artifact = next(entry for entry in manifest.entries if entry.input_kind == "ARTIFACT")
    target_result = _invoke_broker(
        "target-logs",
        "reuse-logparse-run",
        TargetLogsRequest(
            schema_version=1,
            problem_time=PROBLEM_TIME,
            anchors=_anchors(),
            artifact_id=artifact.resource_id,
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
    if evidence_ids != ["00000000-0000-0000-0000-000000000040"]:
        raise RuntimeError("the deterministic formal Evidence ID drifted")
    evidence_binding = EvidenceBinding(
        existing_evidence_id=evidence_ids[0],
        evidence_proposal_key=None,
    )
    problem_spec = snapshot["problem_spec"]
    assert isinstance(problem_spec, dict)
    criterion = str(problem_spec["completion_criteria"][0])
    mapping = CompletionCriterionDraftMapping(
        criterion_index=0,
        criterion=criterion,
        satisfied=True,
        evidence_bindings=[evidence_binding],
        explanation="The request identifier appears in the parsed log.",
    )
    candidate = CandidateConclusionDraft(
        proposal_key="candidate",
        existing_conclusion_id=None,
        statement="The inventory RPC exceeded its deadline.",
        supporting_evidence_bindings=[evidence_binding],
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
    (proposal / "diagnosis-result.json").write_bytes(result_bytes)
    user_result = AgentArtifactProposalDraft(
        proposal_key="user-result",
        artifact_kind=ArtifactKind.USER_RESULT,
        name="diagnosis-result.json",
        content_type="application/json",
        resource_kind=ResourceKind.FILE,
        workspace_relative_path="output/proposals/user-result/diagnosis-result.json",
        declared_size=len(result_bytes),
        declared_sha256=__import__("hashlib").sha256(result_bytes).hexdigest(),
        metadata=UserResultMetadata(
            schema_version=1,
            format_id="problem-locator-diagnosis-v1",
            description="Canonical diagnosis result for the proposed candidate.",
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
        proposed_artifact_drafts=[user_result],
    )


def _review(
    instruction: dict[str, object],
    manifest: WorkspaceInputManifest,
    context: str,
) -> AgentJobOutcome:
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
    target = json.loads(_section(context, "REVIEW_TARGET"))
    evidence_ids = [
        entry.resource_id for entry in manifest.entries if entry.input_kind == "EVIDENCE"
    ]
    return _agent_outcome(
        instruction,
        result_type=OutcomeResultType.COMPLETED,
        payload=ReviewAssessment(
            candidate_conclusion_id=target["candidate_conclusion_id"],
            candidate_revision=target["candidate_revision"],
            candidate_content_hash=target["candidate_content_hash"],
            reviewed_state_revision=int(instruction["base_state_revision"]),
            reviewed_evidence_refs=evidence_ids,
            verdict=ReviewVerdict.PASS,
            unsupported_findings=[],
            evidence_conflicts=[],
            missing_evidence=[],
            stale_references=[],
            recommendation="Accept the evidence-backed RPC timeout candidate.",
        ),
        consumed_evidence_refs=evidence_ids,
    )


def main() -> int:
    context = Path("runtime/context.txt").read_text(encoding="utf-8")
    instruction = json.loads(_section(context, "JOB_INSTRUCTION"))
    snapshot = json.loads(_section(context, "CONTEXT_SNAPSHOT"))
    manifest = _manifest()
    _record_invocation(instruction)
    job_type = JobType(str(instruction["job_type"]))
    if job_type is JobType.ROUTE:
        outcome = _route(instruction)
    elif job_type is JobType.REVIEW:
        outcome = _review(instruction, manifest, context)
    else:
        entries = {entry.input_kind for entry in manifest.entries}
        user_fact_names = {
            item["provenance"]["input_name"] for item in snapshot["user_facts"]
        }
        if "ARTIFACT" in entries:
            outcome = _candidate(instruction, manifest, snapshot, context)
        elif "ATTACHMENT" in entries:
            outcome = _first_log_analysis(instruction, manifest)
        elif {name for _, name, _ in PARAMETER_REQUIREMENTS} <= user_fact_names:
            outcome = _request_attachment(instruction)
        else:
            outcome = _request_parameter_group_a(instruction)
    _write_outcome(outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
