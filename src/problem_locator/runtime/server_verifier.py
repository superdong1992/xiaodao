"""Deterministic, server-owned verification of V2 Agent rule claims.

The verifier intentionally records only contract inputs, cited raw lines, and
mechanical results.  It never records or attempts to reconstruct model chain of
thought.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from problem_locator.contracts import (
    AgentJobOutcomeDraftV2,
    AgentRuleClaim,
    DecisionAuditV2,
    DecisionRuleAudit,
    DiagnosisOutcome,
    EvidenceBinding,
    EvidenceSourceType,
    Job,
    JobType,
    LogparseEvidenceLocator,
    OutcomeResultType,
    PendingRequirement,
    RequirementKind,
    RequirementStatus,
    ReviewAssessment,
    ReviewVerdict,
    RuleClaimResult,
    ReviewSubjectV2,
    ServerRuleEvaluation,
    ServerRuleStatus,
    SupplementPolicy,
    VerifiedLogLineRange,
    WorkspaceArtifactInput,
    WorkspaceEvidenceInput,
    WorkspaceInputManifest,
    bytes_sha256,
    canonical_json_bytes,
)


_RFC3339_MILLIS_UTC = "%Y-%m-%dT%H:%M:%S.%fZ"
_RFC3339_MILLIS_UTC_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z"
)


def _parse_millisecond_utc(value: str) -> datetime:
    if _RFC3339_MILLIS_UTC_PATTERN.fullmatch(value) is None:
        raise ValueError("timestamp is not millisecond UTC")
    return datetime.strptime(value, _RFC3339_MILLIS_UTC).replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    audit: DecisionAuditV2
    positive_gate_passed: bool
    decision_evidence_bytes: bytes


@dataclass(frozen=True, slots=True)
class _ResolvedLine:
    binding: EvidenceBinding
    anchor: str | None
    source_key: str
    relative_path: str
    line_number: int
    raw_line: bytes
    text: str

    @property
    def binding_key(self) -> str:
        return _binding_key(self.binding)


@dataclass(frozen=True, slots=True)
class _EventMatch:
    event_id: str
    anchor: str
    event_time: str
    fields: Mapping[str, str]
    line: _ResolvedLine


def _binding_key(binding: EvidenceBinding) -> str:
    if binding.existing_evidence_id is not None:
        return binding.existing_evidence_id
    assert binding.evidence_proposal_key is not None
    return f"proposal:{binding.evidence_proposal_key}"


def _unique_bindings(values: Iterable[EvidenceBinding]) -> list[EvidenceBinding]:
    result: list[EvidenceBinding] = []
    seen: set[str] = set()
    for value in values:
        key = _binding_key(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _unique_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _load_contract(skill_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = Path(skill_root) / "diagnosis-skill.json"

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("diagnosis Skill manifest has duplicate fields")
            result[key] = value
        return result

    value = json.loads(
        path.read_bytes().decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite manifest value: {value}")
        ),
    )
    if not isinstance(value, dict) or value.get("schema_version") != 3:
        raise ValueError("server verifier requires a pinned Skill manifest v3")
    contract = value.get("verification_contract")
    if not isinstance(contract, dict) or contract.get("schema_version") != 1:
        raise ValueError("server verifier requires verification_contract v1")
    extractors = contract.get("event_extractors")
    rules = contract.get("rules")
    if not isinstance(extractors, list) or not isinstance(rules, list) or not rules:
        raise ValueError("verification contract is incomplete")
    requirements = value.get("requirements")
    if not isinstance(requirements, list):
        raise ValueError("server verifier requires pinned Skill requirements")
    return contract, requirements


def _pinned_requirements_by_name(
    requirements: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    required_fields = {
        "name",
        "kind",
        "stage",
        "fulfillment_source",
        "prompt",
        "constraints",
        "supplement_policy",
    }
    for item in requirements:
        if not isinstance(item, dict) or set(item) != required_fields:
            raise ValueError("pinned Skill requirement is invalid")
        name = item["name"]
        if not isinstance(name, str) or not name or name in result:
            raise ValueError("pinned Skill requirement names are invalid")
        result[name] = item
    return result


def _requirement_is_missing(
    requirement: PendingRequirement,
    *,
    job: Job,
    manifest: WorkspaceInputManifest,
) -> bool:
    if requirement.kind is RequirementKind.INPUT:
        return all(
            item.provenance.input_name != requirement.name
            for item in job.context_snapshot.user_facts
        )
    # Catalog V3 permits only one INITIAL attachment requirement.  A fixed
    # attachment or an already-resolved Logparse source therefore proves it
    # has been fulfilled for this Job.
    plan = manifest.resolved_logparse_plan
    has_logparse_evidence = any(
        isinstance(item, WorkspaceEvidenceInput)
        and item.source_type is EvidenceSourceType.LOGPARSE
        for item in manifest.entries
    )
    return not job.attachment_refs and plan is None and not has_logparse_evidence


def _validate_requirement_requests(
    *,
    job: Job,
    manifest: WorkspaceInputManifest,
    draft: AgentJobOutcomeDraftV2,
    pinned_requirements: list[dict[str, Any]],
) -> None:
    """Reject Agent-invented waits before they can become an Outcome."""

    payload = draft.payload
    if isinstance(payload, DiagnosisOutcome):
        new_requirements = list(payload.state_delta.add_pending_requirements)
        requested_ids = [*payload.requested_input, *payload.requested_attachments]
        if not new_requirements and not requested_ids:
            return
    elif isinstance(payload, ReviewAssessment):
        new_requirements = []
        requested_ids = list(payload.requested_requirement_ids)
        if payload.verdict is ReviewVerdict.NEED_MORE_EVIDENCE:
            if len(requested_ids) != 1:
                raise ValueError(
                    "NEED_MORE_EVIDENCE requires one pinned OPEN requirement"
                )
        elif requested_ids:
            raise ValueError("only NEED_MORE_EVIDENCE may request a requirement")
        else:
            return
    else:
        return

    pinned_by_name = _pinned_requirements_by_name(pinned_requirements)
    existing = list(job.context_snapshot.pending_requirements)
    existing_ids = {item.requirement_id for item in existing}
    if any(item.requirement_id in existing_ids for item in new_requirements):
        raise ValueError("Agent requirement reuses an existing requirement ID")
    combined = [
        *(item for item in existing if item.status is RequirementStatus.OPEN),
        *new_requirements,
    ]
    if len({item.requirement_id for item in combined}) != len(combined):
        raise ValueError("Agent requirement IDs are ambiguous")
    if len({item.name for item in combined}) != len(combined):
        raise ValueError("Agent requirement names are ambiguous")
    by_id = {item.requirement_id: item for item in combined}
    requested: list[PendingRequirement] = []
    for requirement_id in requested_ids:
        requirement = by_id.get(requirement_id)
        if requirement is None or requirement.status is not RequirementStatus.OPEN:
            raise ValueError("requested requirement is not one fixed OPEN requirement")
        requested.append(requirement)
    if any(
        item.requirement_id not in set(requested_ids)
        for item in new_requirements
    ):
        raise ValueError("new Agent requirements must be requested by the wait result")

    if isinstance(payload, DiagnosisOutcome):
        if draft.result_type is OutcomeResultType.NEED_INPUT:
            expected_kind = RequirementKind.INPUT
        elif draft.result_type is OutcomeResultType.NEED_ATTACHMENT:
            expected_kind = RequirementKind.ATTACHMENT
        else:
            raise ValueError("only NEED_INPUT/NEED_ATTACHMENT may add requirements")
        if any(item.kind is not expected_kind for item in requested):
            raise ValueError("wait result requests the wrong requirement kind")

    for requirement in requested:
        pinned = pinned_by_name.get(requirement.name)
        if pinned is None:
            raise ValueError("Agent requirement is absent from the pinned Skill")
        expected_fulfillment = (
            "USER_FACT"
            if requirement.kind is RequirementKind.INPUT
            else "READY_ATTACHMENT"
        )
        if (
            pinned["kind"] != requirement.kind.value
            or pinned["fulfillment_source"] != expected_fulfillment
            or pinned["prompt"] != requirement.prompt
            or pinned["constraints"]
            != requirement.constraints.model_dump(mode="json")
            or pinned["supplement_policy"] != requirement.supplement_policy.value
            or requirement.supplement_policy is not SupplementPolicy.MISSING_ONLY
        ):
            raise ValueError("Agent requirement differs from the pinned Skill")
        stage = pinned["stage"]
        has_logparse_phase = manifest.resolved_logparse_plan is not None or any(
            isinstance(item, WorkspaceEvidenceInput)
            and item.source_type is EvidenceSourceType.LOGPARSE
            for item in manifest.entries
        )
        if stage not in {"INITIAL", "AFTER_LOGPARSE"} or (
            stage == "AFTER_LOGPARSE" and not has_logparse_phase
        ):
            raise ValueError("Agent requirement is invalid for the current Skill stage")
        if not _requirement_is_missing(
            requirement,
            job=job,
            manifest=manifest,
        ):
            raise ValueError("MISSING_ONLY requirement is already fulfilled")


def _artifact_source_key(artifact_id: str) -> str:
    return f"artifact:{artifact_id}"


def _proposal_source_key(proposal_key: str) -> str:
    return f"proposal:{proposal_key}"


def _target_anchor_paths(
    broker_audit_bytes: bytes | None,
    *,
    job: Job,
    manifest: WorkspaceInputManifest,
) -> tuple[dict[tuple[str, str], str], set[str]]:
    """Bind broker-selected anchors to the exact resolved LOGPARSE_RUN source.

    A relative path is not a source identity: two persisted runs may both
    contain (for example) ``client.log``.  The resolved plan owns the existing
    Artifact identity, while a successful initial parse owns the proposal key
    recorded by the broker transcript.
    """

    plan = manifest.resolved_logparse_plan
    if plan is None:
        return {}, set()
    allowed_sources = (
        {_artifact_source_key(plan.artifact_id)}
        if plan.artifact_id is not None
        else set()
    )
    if broker_audit_bytes is None:
        return {}, allowed_sources
    value = json.loads(broker_audit_bytes.decode("utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("job_id") != job.job_id
    ):
        raise ValueError("broker audit is invalid")
    result: dict[tuple[str, str], str] = {}
    operations = value.get("operations")
    if not isinstance(operations, list):
        raise ValueError("broker audit operations are invalid")
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("http_status") != 200:
            continue
        request = operation.get("request")
        if not isinstance(request, dict):
            raise ValueError("broker audit request is invalid")
        payload = operation.get("result")
        if not isinstance(payload, dict):
            raise ValueError("broker audit result is invalid")
        if plan.artifact_id is not None:
            if (
                operation.get("operation") != "target-logs"
                or request.get("artifact_id") != plan.artifact_id
            ):
                raise ValueError("broker audit source differs from the resolved plan")
            source_key = _artifact_source_key(plan.artifact_id)
        else:
            proposal_key = request.get("artifact_proposal_key")
            artifact_draft = payload.get("logparse_run_artifact_draft")
            if (
                operation.get("operation") != "parse-targets"
                or request.get("attachment_id") != plan.attachment_id
                or not isinstance(proposal_key, str)
                or not proposal_key
                or not isinstance(artifact_draft, dict)
                or artifact_draft.get("proposal_key") != proposal_key
            ):
                raise ValueError("broker audit source differs from the resolved plan")
            source_key = _proposal_source_key(proposal_key)
            allowed_sources.add(source_key)
        targets = payload.get("target_logs") if isinstance(payload, dict) else None
        if not isinstance(targets, list):
            continue
        for target in targets:
            if not isinstance(target, dict):
                raise ValueError("broker target audit is invalid")
            label = target.get("label")
            path = target.get("log_path")
            if not isinstance(label, str) or not isinstance(path, str):
                continue
            normalized = PurePosixPath(path).as_posix()
            key = (source_key, normalized)
            existing = result.get(key)
            if existing is not None and existing != label:
                raise ValueError("one target log is bound to multiple anchors")
            result[key] = label
    return result, allowed_sources


def _diagnosis_anchor_paths(
    diagnosis_audit: DecisionAuditV2 | None,
    *,
    manifest: WorkspaceInputManifest,
) -> tuple[dict[tuple[str, str], str], set[str]]:
    """Recover REVIEW anchor bindings through server-built formal Evidence."""

    result: dict[tuple[str, str], str] = {}
    subject = manifest.review_subject
    if diagnosis_audit is None or subject is None:
        return result, set()
    evidence_entries = {
        entry.resource_id: entry
        for entry in manifest.entries
        if isinstance(entry, WorkspaceEvidenceInput)
    }
    allowed_sources = {
        _artifact_source_key(entry.source_ref)
        for evidence_ref in subject.required_evidence_refs
        if (entry := evidence_entries.get(evidence_ref)) is not None
        and entry.source_type is EvidenceSourceType.LOGPARSE
    }
    facts_by_rule = {
        item.source_rule_id: item
        for item in subject.mechanical_facts
        if item.name == item.source_rule_id
    }
    for rule in diagnosis_audit.rules:
        evaluation = rule.server_evaluation
        if evaluation.anchor_id is None:
            continue
        fact = facts_by_rule.get(rule.rule_id)
        if fact is None:
            continue
        for evidence_ref in fact.evidence_refs:
            entry = evidence_entries.get(evidence_ref)
            if entry is None or entry.source_type is not EvidenceSourceType.LOGPARSE:
                continue
            source_key = _artifact_source_key(entry.source_ref)
            for line_range in evaluation.line_ranges:
                key = (source_key, line_range.path)
                existing = result.get(key)
                if existing is not None and existing != evaluation.anchor_id:
                    raise ValueError("diagnosis audit has conflicting anchor paths")
                result[key] = evaluation.anchor_id
    return result, allowed_sources


def _plain_source_file(root: Path, source_root: Path, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Evidence locator path is unsafe")
    root_resolved = root.resolve(strict=True)
    source_resolved = source_root.resolve(strict=True)
    if os.path.commonpath((os.fspath(root_resolved), os.fspath(source_resolved))) != os.fspath(root_resolved):
        raise ValueError("Evidence source root escapes the Workspace")
    current = source_resolved
    for part in relative.parts:
        current = current / part
        metadata = current.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("Evidence source path contains a symbolic link")
    path = current.resolve(strict=True)
    if os.path.commonpath((os.fspath(source_resolved), os.fspath(path))) != os.fspath(source_resolved):
        raise ValueError("Evidence source path escapes its Artifact")
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Evidence citation does not identify a plain file")
    return path


def _artifact_roots(
    workspace_root: Path,
    manifest: WorkspaceInputManifest,
    proposal_resources: Iterable[Any],
) -> tuple[dict[str, Path], dict[str, Path]]:
    existing: dict[str, Path] = {}
    for entry in manifest.entries:
        if isinstance(entry, WorkspaceArtifactInput):
            existing[entry.resource_id] = workspace_root / entry.relative_path
    proposed: dict[str, Path] = {}
    for resource in proposal_resources:
        proposal_key = getattr(resource, "proposal_key", None)
        path = getattr(resource, "path", None)
        draft = getattr(resource, "draft", None)
        if (
            isinstance(proposal_key, str)
            and isinstance(path, Path)
            and getattr(draft, "artifact_kind", None) is not None
        ):
            proposed[proposal_key] = path
    return existing, proposed


def _draft_evidence_bindings(
    draft: AgentJobOutcomeDraftV2,
    manifest: WorkspaceInputManifest,
) -> list[EvidenceBinding]:
    values: list[EvidenceBinding] = [
        EvidenceBinding(existing_evidence_id=item, evidence_proposal_key=None)
        for item in draft.consumed_evidence_refs
    ]
    payload = draft.payload
    if hasattr(payload, "findings") and hasattr(payload, "state_delta"):
        for finding in payload.findings:
            values.extend(finding.evidence_bindings)
        delta = payload.state_delta
        for item in (
            *delta.proposed_facts,
            *delta.add_active_hypotheses,
            *delta.add_open_questions,
            *delta.update_hypotheses,
            *delta.reject_hypotheses,
            *delta.resolve_questions,
        ):
            values.extend(item.evidence_bindings)
        values.extend(delta.add_evidence_bindings)
        candidate = payload.candidate_conclusion_draft
        if candidate is not None:
            values.extend(candidate.supporting_evidence_bindings)
            for mapping in candidate.completion_criteria_mapping:
                values.extend(mapping.evidence_bindings)
    if manifest.review_subject is not None:
        values.extend(
            EvidenceBinding(
                existing_evidence_id=item,
                evidence_proposal_key=None,
            )
            for item in manifest.review_subject.required_evidence_refs
        )
    values.extend(
        citation.evidence_binding
        for claim in draft.rule_claims
        for citation in claim.citations
    )
    return _unique_bindings(values)


@dataclass(frozen=True, slots=True)
class _EvidenceSource:
    binding: EvidenceBinding
    locator: LogparseEvidenceLocator
    source_path: Path
    anchor: str | None


def _resolve_evidence_lines(
    *,
    workspace_root: Path,
    job: Job,
    manifest: WorkspaceInputManifest,
    draft: AgentJobOutcomeDraftV2,
    proposal_resources: Iterable[Any],
    anchor_paths: Mapping[tuple[str, str], str],
    allowed_logparse_sources: set[str] | None,
) -> tuple[
    list[_ResolvedLine],
    dict[str, set[str]],
    dict[str, set[tuple[str, str, int]]],
    list[EvidenceBinding],
    set[str],
]:
    evidence_entries = {
        entry.resource_id: entry
        for entry in manifest.entries
        if isinstance(entry, WorkspaceEvidenceInput)
    }
    evidence_drafts = {
        item.proposal_key: item for item in draft.proposed_evidence_drafts
    }
    existing_artifacts, proposed_artifacts = _artifact_roots(
        workspace_root,
        manifest,
        proposal_resources,
    )
    scan_lines: list[_ResolvedLine] = []
    claim_bindings: dict[str, set[str]] = {}
    claim_line_keys: dict[str, set[tuple[str, str, int]]] = {}
    file_cache: dict[Path, list[bytes]] = {}
    participating: list[EvidenceBinding] = []
    sources: dict[str, _EvidenceSource] = {}
    incomplete_anchors: set[str] = set()

    def source_for(binding: EvidenceBinding, *, citation: bool) -> _EvidenceSource | None:
        key = _binding_key(binding)
        cached = sources.get(key)
        if cached is not None:
            return cached
        if binding.existing_evidence_id is not None:
            evidence = evidence_entries.get(binding.existing_evidence_id)
            if evidence is None:
                raise ValueError("Evidence binding is outside the fixed Workspace")
            source_type = evidence.source_type
            source_ref = evidence.source_ref
            locator = evidence.locator
            source_root = existing_artifacts.get(source_ref)
            source_identity = _artifact_source_key(source_ref)
        else:
            assert binding.evidence_proposal_key is not None
            evidence = evidence_drafts.get(binding.evidence_proposal_key)
            if evidence is None:
                raise ValueError("Evidence binding names an undeclared proposal")
            source_type = evidence.source_type
            locator = evidence.locator
            source_ref = evidence.source_binding.existing_source_ref
            artifact_key = evidence.source_binding.artifact_proposal_key
            source_root = (
                existing_artifacts.get(source_ref)
                if source_ref is not None
                else proposed_artifacts.get(artifact_key or "")
            )
            source_identity = (
                _artifact_source_key(source_ref)
                if source_ref is not None
                else _proposal_source_key(artifact_key or "")
            )
        if source_type is not EvidenceSourceType.LOGPARSE:
            if citation:
                raise ValueError("rule citations must bind raw LOGPARSE Evidence")
            return None
        if not isinstance(locator, LogparseEvidenceLocator) or source_root is None:
            raise ValueError("LOGPARSE Evidence has no immutable raw source")
        if (
            allowed_logparse_sources is not None
            and source_identity not in allowed_logparse_sources
        ):
            raise ValueError("LOGPARSE Evidence source differs from the resolved plan")
        normalized_path = PurePosixPath(locator.relative_path).as_posix()
        result = _EvidenceSource(
            binding=binding,
            locator=locator,
            source_path=_plain_source_file(
                workspace_root,
                source_root,
                locator.relative_path,
            ),
            anchor=anchor_paths.get((source_identity, normalized_path)),
        )
        sources[key] = result
        return result

    def physical_lines(source: _EvidenceSource) -> list[bytes]:
        path = source.source_path
        physical = file_cache.get(path)
        if physical is not None:
            return physical
        metadata = path.stat(follow_symlinks=False)
        if metadata.st_size > job.resource_limits.workspace_bytes:
            raise ValueError("cited log exceeds the Job Workspace limit")
        raw = path.read_bytes()
        if len(raw) != metadata.st_size:
            raise ValueError("cited log changed while read")
        physical = raw.splitlines(keepends=True)
        file_cache[path] = physical
        return physical

    def resolved_line(source: _EvidenceSource, line_number: int) -> _ResolvedLine:
        physical = physical_lines(source)
        if line_number > len(physical):
            raise ValueError("Evidence line range exceeds the raw log")
        raw_line = physical[line_number - 1]
        content = raw_line.rstrip(b"\r\n")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("cited raw log line is not UTF-8") from exc
        return _ResolvedLine(
            binding=source.binding,
            anchor=source.anchor,
            source_key=os.fspath(source.source_path),
            relative_path=PurePosixPath(source.locator.relative_path).as_posix(),
            line_number=line_number,
            raw_line=raw_line,
            text=text,
        )

    for binding in _draft_evidence_bindings(draft, manifest):
        source = source_for(binding, citation=False)
        if source is None:
            continue
        participating.append(binding)
        if source.anchor is None:
            # Without a server-captured path-to-anchor binding the verifier
            # cannot prove which extractor owns this Evidence.  Mark the
            # entire scan incomplete instead of letting another Evidence for
            # the same rule make the input look complete.
            incomplete_anchors.add("*")
        start = source.locator.start_line
        end = source.locator.end_line
        if start is None or end is None:
            incomplete_anchors.add(source.anchor or "*")
            continue
        physical = physical_lines(source)
        if end > len(physical):
            raise ValueError("Evidence locator exceeds the raw log")
        for line_number in range(start, end + 1):
            scan_lines.append(resolved_line(source, line_number))

    for claim in draft.rule_claims:
        current_bindings: set[str] = set()
        current_lines: set[tuple[str, str, int]] = set()
        for citation in claim.citations:
            binding = citation.evidence_binding
            key = _binding_key(binding)
            current_bindings.add(key)
            source = source_for(binding, citation=True)
            assert source is not None
            start = source.locator.start_line
            end = source.locator.end_line
            if start is None or end is None:
                # The bytes may be auditable, but cannot establish a bounded
                # EXACTLY_ONE scan set.
                continue
            if citation.line_start < start or citation.line_end > end:
                raise ValueError("citation line range escapes its Evidence locator")
            for line_number in range(citation.line_start, citation.line_end + 1):
                line = resolved_line(source, line_number)
                current_lines.add((key, line.relative_path, line.line_number))
        claim_bindings[claim.rule_id] = current_bindings
        claim_line_keys[claim.rule_id] = current_lines
    return (
        scan_lines,
        claim_bindings,
        claim_line_keys,
        participating,
        incomplete_anchors,
    )


def _extract_events(
    extractors: list[dict[str, Any]],
    lines: list[_ResolvedLine],
    incomplete_anchors: set[str],
) -> tuple[dict[str, list[_EventMatch]], dict[str, bool]]:
    matches: dict[str, list[_EventMatch]] = {}
    anchor_has_lines: dict[str, bool] = {}
    for extractor in extractors:
        event_id = extractor["id"]
        anchor = extractor["anchor"]
        pattern = re.compile(extractor["line_pattern"])
        anchor_lines = [line for line in lines if line.anchor == anchor]
        anchor_has_lines[anchor] = (
            bool(anchor_lines)
            and "*" not in incomplete_anchors
            and anchor not in incomplete_anchors
        )
        current: list[_EventMatch] = []
        seen_occurrences: set[tuple[str, int]] = set()
        for line in anchor_lines:
            match = pattern.fullmatch(line.text)
            if match is None:
                continue
            occurrence = (line.source_key, line.line_number)
            if occurrence in seen_occurrences:
                continue
            seen_occurrences.add(occurrence)
            values = match.groupdict()
            event_time = values[extractor["timestamp_group"]]
            if event_time is None:
                continue
            try:
                _parse_millisecond_utc(event_time)
            except ValueError:
                continue
            if any(values[name] is None for name in extractor["field_groups"]):
                continue
            fields = {name: values[name] for name in extractor["field_groups"]}
            current.append(
                _EventMatch(
                    event_id=event_id,
                    anchor=anchor,
                    event_time=event_time,
                    fields=fields,
                    line=line,
                )
            )
        matches[event_id] = current
    return matches, anchor_has_lines


def _event_material(
    event_ids: Iterable[str],
    events: Mapping[str, list[_EventMatch]],
) -> tuple[list[EvidenceBinding], list[str], list[VerifiedLogLineRange], list[str]]:
    selected = [match for event_id in event_ids for match in events.get(event_id, [])]
    bindings = _unique_bindings(match.line.binding for match in selected)
    observed = _unique_strings(match.event_time for match in selected)
    line_ranges: list[VerifiedLogLineRange] = []
    seen_lines: set[tuple[str, int, str]] = set()
    anchors: list[str] = []
    for match in selected:
        anchors.append(match.anchor)
        key = (match.line.relative_path, match.line.line_number, bytes_sha256(match.line.raw_line))
        if key in seen_lines:
            continue
        seen_lines.add(key)
        line_ranges.append(
            VerifiedLogLineRange(
                path=match.line.relative_path,
                line_start=match.line.line_number,
                line_end=match.line.line_number,
                raw_bytes_sha256=key[2],
            )
        )
    return bindings, observed, line_ranges, _unique_strings(anchors)


def _facts(job: Job) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for fact in job.context_snapshot.user_facts:
        name = fact.provenance.input_name
        if name is not None:
            result.setdefault(name, []).append(fact)
    return result


def _rule_events(rule: Mapping[str, Any]) -> list[str]:
    parameters = rule["parameters"]
    kind = rule["kind"]
    if kind in {"EVENT_PRESENT", "EVENT_TIME_WINDOW", "FACT_FIELD_EQUALS"}:
        return [parameters["event"]]
    if kind == "ROLE_COVERAGE":
        return [item["event"] for item in parameters["coverage"]]
    if kind == "CROSS_ROLE_CORRELATION":
        return [item["event"] for item in parameters["members"]]
    if kind == "EVENT_ORDER":
        return [parameters["before_event"], parameters["after_event"]]
    if kind == "SEMANTIC_CAUSALITY":
        return list(parameters["evidence_events"])
    raise ValueError("unsupported verification rule kind")


def _evaluation(
    *,
    rule: Mapping[str, Any],
    status: ServerRuleStatus,
    events: Mapping[str, list[_EventMatch]],
    fact_refs: Iterable[str] = (),
    derived_anchor_time: str | None = None,
    issues: Iterable[str] = (),
) -> ServerRuleEvaluation:
    event_ids = _rule_events(rule)
    bindings, observed, line_ranges, anchors = _event_material(event_ids, events)
    return ServerRuleEvaluation(
        rule_id=rule["id"],
        rule_kind=rule["kind"],
        status=status,
        fact_refs=list(fact_refs),
        evidence_bindings=bindings,
        anchor_id=anchors[0] if len(anchors) == 1 else None,
        derived_anchor_time=derived_anchor_time,
        observed_times=observed,
        line_ranges=line_ranges,
        issues=list(issues),
    )


def _evaluate_rule(
    rule: Mapping[str, Any],
    *,
    events: Mapping[str, list[_EventMatch]],
    anchor_has_lines: Mapping[str, bool],
    extractor_by_id: Mapping[str, Mapping[str, Any]],
    facts: Mapping[str, list[Any]],
    prior: Mapping[str, ServerRuleEvaluation],
) -> ServerRuleEvaluation:
    dependencies = [prior[item] for item in rule["depends_on"]]
    kind = rule["kind"]
    parameters = rule["parameters"]
    event_ids = _rule_events(rule)
    if kind != "SEMANTIC_CAUSALITY":
        for event_id in event_ids:
            matches = events[event_id]
            anchor = extractor_by_id[event_id]["anchor"]
            if not anchor_has_lines.get(anchor, False):
                return _evaluation(
                    rule=rule,
                    status=ServerRuleStatus.UNVERIFIABLE,
                    events=events,
                    issues=[
                        "The full bounded Evidence locator range is unavailable."
                    ],
                )
            if len(matches) != 1:
                return _evaluation(
                    rule=rule,
                    status=ServerRuleStatus.VERIFIED_FAIL,
                    events=events,
                    issues=[
                        "The cited raw log does not contain exactly one required event."
                    ],
                )

    if kind == "EVENT_PRESENT":
        return _evaluation(rule=rule, status=ServerRuleStatus.VERIFIED_PASS, events=events)

    if kind == "EVENT_TIME_WINDOW":
        reference = parameters["reference"]
        fact_refs: list[str] = []
        if reference["source"] == "USER_FACT":
            fact_values = facts.get(reference["name"], [])
            if len(fact_values) != 1:
                return _evaluation(
                    rule=rule,
                    status=ServerRuleStatus.UNVERIFIABLE,
                    events=events,
                    issues=["The reference user fact is missing or ambiguous."],
                )
            fact = fact_values[0]
            reference_value = fact.statement
            fact_refs = [fact.item_id]
        elif reference["source"] == "SKILL_FIXED":
            reference_value = reference["value"]
        else:
            return _evaluation(
                rule=rule,
                status=ServerRuleStatus.UNVERIFIABLE,
                events=events,
                issues=["The time reference binding is unsupported."],
            )
        try:
            anchor_time = _parse_millisecond_utc(reference_value)
        except ValueError:
            return _evaluation(
                rule=rule,
                status=ServerRuleStatus.VERIFIED_FAIL,
                events=events,
                fact_refs=fact_refs,
                issues=["The time reference is not an RFC3339 millisecond UTC timestamp."],
            )
        observed = _parse_millisecond_utc(
            events[parameters["event"]][0].event_time
        )
        lower = anchor_time - timedelta(milliseconds=parameters["before_ms"])
        upper = anchor_time + timedelta(milliseconds=parameters["after_ms"])
        lower_ok = observed >= lower if parameters["lower_bound"] == "INCLUSIVE" else observed > lower
        upper_ok = observed <= upper if parameters["upper_bound"] == "INCLUSIVE" else observed < upper
        return _evaluation(
            rule=rule,
            status=(
                ServerRuleStatus.VERIFIED_PASS
                if lower_ok and upper_ok
                else ServerRuleStatus.VERIFIED_FAIL
            ),
            events=events,
            fact_refs=fact_refs,
            derived_anchor_time=reference_value,
            issues=([] if lower_ok and upper_ok else ["The event is outside the explicit incident window."]),
        )

    if kind == "FACT_FIELD_EQUALS":
        fact_values = facts.get(parameters["fact_name"], [])
        if len(fact_values) != 1:
            return _evaluation(
                rule=rule,
                status=ServerRuleStatus.UNVERIFIABLE,
                events=events,
                issues=["The required user fact is missing or ambiguous."],
            )
        fact = fact_values[0]
        actual = events[parameters["event"]][0].fields[parameters["field"]]
        passed = actual == fact.statement
        return _evaluation(
            rule=rule,
            status=(ServerRuleStatus.VERIFIED_PASS if passed else ServerRuleStatus.VERIFIED_FAIL),
            events=events,
            fact_refs=[fact.item_id],
            issues=([] if passed else ["The raw event field does not equal the fixed user fact."]),
        )

    if kind == "ROLE_COVERAGE":
        passed = all(
            events[item["event"]][0].anchor == item["role"]
            for item in parameters["coverage"]
        )
        return _evaluation(
            rule=rule,
            status=(ServerRuleStatus.VERIFIED_PASS if passed else ServerRuleStatus.VERIFIED_FAIL),
            events=events,
            issues=([] if passed else ["The cited events do not cover the required roles."]),
        )

    if kind == "CROSS_ROLE_CORRELATION":
        values = [events[item["event"]][0].fields[item["field"]] for item in parameters["members"]]
        passed = len(set(values)) == 1
        return _evaluation(
            rule=rule,
            status=(ServerRuleStatus.VERIFIED_PASS if passed else ServerRuleStatus.VERIFIED_FAIL),
            events=events,
            issues=([] if passed else ["The cited cross-role fields do not correlate."]),
        )

    if kind == "EVENT_ORDER":
        before = _parse_millisecond_utc(
            events[parameters["before_event"]][0].event_time
        )
        after = _parse_millisecond_utc(
            events[parameters["after_event"]][0].event_time
        )
        passed = before <= after if parameters["allow_equal"] else before < after
        return _evaluation(
            rule=rule,
            status=(ServerRuleStatus.VERIFIED_PASS if passed else ServerRuleStatus.VERIFIED_FAIL),
            events=events,
            issues=([] if passed else ["The cited events violate the required order."]),
        )

    if kind == "SEMANTIC_CAUSALITY":
        missing = [
            event_id
            for event_id in event_ids
            if len(events[event_id]) != 1
            or not anchor_has_lines.get(extractor_by_id[event_id]["anchor"], False)
        ]
        return _evaluation(
            rule=rule,
            status=ServerRuleStatus.SEMANTIC_ONLY,
            events=events,
            issues=(
                []
                if not missing
                and all(
                    item.status is ServerRuleStatus.VERIFIED_PASS
                    for item in dependencies
                )
                else [
                    "Semantic review lacks verified mechanical prerequisites."
                ]
            ),
        )
    raise ValueError("unsupported verification rule kind")


def _diagnosis_audit_review_gates(
    audit: DecisionAuditV2,
    *,
    job: Job,
    rules: list[dict[str, Any]],
    review_subject: ReviewSubjectV2,
) -> tuple[bool, bool]:
    """Return inherited-audit integrity and positive-resolution gates.

    Integrity is enough for a Reviewer to reject a Candidate using a newly
    aligned FAIL claim.  Accepting the Candidate additionally requires the
    inherited diagnosis to have passed every pinned rule.
    """

    required_rule_ids = [item["id"] for item in rules]
    if (
        audit.job_type is not JobType.DIAGNOSE
        or audit.case_id != job.case_id
        or audit.skill_ref != job.skill_ref
        or audit.required_rule_ids != required_rule_ids
        or [item.rule_id for item in audit.rules] != required_rule_ids
    ):
        return False, False
    integrity_facts = [
        item
        for item in review_subject.mechanical_facts
        if item.name == "diagnosis_audit_integrity"
    ]
    if len(integrity_facts) != 1:
        return False, False
    integrity = integrity_facts[0]
    if (
        integrity.value != ServerRuleStatus.VERIFIED_PASS.value
        or not set(review_subject.required_evidence_refs)
        <= set(integrity.evidence_refs)
    ):
        return False, False
    positive = True
    for rule, item in zip(rules, audit.rules, strict=True):
        evaluation = item.server_evaluation
        expected_status = (
            ServerRuleStatus.SEMANTIC_ONLY
            if rule["kind"] == "SEMANTIC_CAUSALITY"
            else ServerRuleStatus.VERIFIED_PASS
        )
        if (
            evaluation.rule_kind != rule["kind"]
            or evaluation.status is not expected_status
            or evaluation.issues
            or item.agent_claim is None
            or item.agent_claim.claimed_result is not RuleClaimResult.PASS
        ):
            positive = False
    return True, positive


def _claim_alignment(
    claim: AgentRuleClaim | None,
    evaluation: ServerRuleEvaluation,
    claim_bindings: Mapping[str, set[str]],
    claim_line_keys: Mapping[str, set[tuple[str, str, int]]],
    required_line_keys: set[tuple[str, str, int]],
) -> RuleClaimResult | None:
    """Return the server-aligned claim result, or ``None`` for a mismatch."""

    if claim is None:
        return None
    if claim.fact_refs != evaluation.fact_refs:
        return None
    cited = claim_bindings.get(claim.rule_id, set())
    required = {_binding_key(item) for item in evaluation.evidence_bindings}
    if required and not required <= cited:
        return None
    if required_line_keys and not required_line_keys <= claim_line_keys.get(
        claim.rule_id,
        set(),
    ):
        return None

    if evaluation.status is ServerRuleStatus.VERIFIED_PASS:
        expected = RuleClaimResult.PASS
    elif evaluation.status is ServerRuleStatus.VERIFIED_FAIL:
        expected = RuleClaimResult.FAIL
    elif evaluation.status is ServerRuleStatus.UNVERIFIABLE:
        expected = RuleClaimResult.UNKNOWN
    elif evaluation.status is ServerRuleStatus.SEMANTIC_ONLY:
        if evaluation.issues:
            expected = RuleClaimResult.UNKNOWN
        elif claim.claimed_result in {
            RuleClaimResult.PASS,
            RuleClaimResult.FAIL,
            RuleClaimResult.UNKNOWN,
        }:
            # The server can establish the cited inputs and prerequisites, but
            # the independent Agent owns the semantic causality judgment.
            expected = claim.claimed_result
        else:  # pragma: no cover - enum exhaustiveness
            return None
    else:  # pragma: no cover - enum exhaustiveness
        return None
    return claim.claimed_result if claim.claimed_result is expected else None


def _candidate_bindings(payload: DiagnosisOutcome) -> list[EvidenceBinding]:
    candidate = payload.candidate_conclusion_draft
    if candidate is None:
        return []
    return _unique_bindings(
        [
            *candidate.supporting_evidence_bindings,
            *(
                binding
                for mapping in candidate.completion_criteria_mapping
                for binding in mapping.evidence_bindings
            ),
        ]
    )


def _diagnosis_decision_gate(
    *,
    draft: AgentJobOutcomeDraftV2,
    alignments: list[RuleClaimResult | None],
    required_bindings: list[EvidenceBinding],
) -> bool:
    if (
        draft.result_type is not OutcomeResultType.COMPLETED
        or not isinstance(draft.payload, DiagnosisOutcome)
    ):
        return False
    candidate_keys = [
        _binding_key(item) for item in _candidate_bindings(draft.payload)
    ]
    required_keys = [_binding_key(item) for item in required_bindings]
    return (
        bool(required_keys)
        and candidate_keys == required_keys
        and all(item is RuleClaimResult.PASS for item in alignments)
    )


def _review_decision_gate(
    *,
    draft: AgentJobOutcomeDraftV2,
    alignments: list[RuleClaimResult | None],
    inherited_integrity: bool,
    inherited_positive: bool,
) -> bool:
    if (
        draft.result_type is not OutcomeResultType.COMPLETED
        or not isinstance(draft.payload, ReviewAssessment)
        or any(item is None for item in alignments)
    ):
        return False
    verdict = draft.payload.verdict
    if verdict is ReviewVerdict.PASS:
        return inherited_positive and all(
            item is RuleClaimResult.PASS for item in alignments
        )
    if verdict is ReviewVerdict.REJECT:
        return inherited_integrity and any(
            item is RuleClaimResult.FAIL for item in alignments
        )
    if verdict is ReviewVerdict.NEED_MORE_EVIDENCE:
        return (
            all(item is not RuleClaimResult.FAIL for item in alignments)
            and any(item is RuleClaimResult.UNKNOWN for item in alignments)
            and len(draft.payload.requested_requirement_ids) == 1
        )
    return False


def _decision_evidence(lines: list[_ResolvedLine], *, maximum_bytes: int) -> bytes:
    ordered = sorted(
        lines,
        key=lambda item: (
            item.anchor or "",
            item.relative_path,
            item.line_number,
            item.binding_key,
        ),
    )
    records: list[bytes] = []
    seen: set[tuple[str, str, int]] = set()
    for line in ordered:
        key = (line.binding_key, line.relative_path, line.line_number)
        if key in seen:
            continue
        seen.add(key)
        records.append(
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "evidence_ref": line.binding_key,
                    "anchor": line.anchor,
                    "relative_path": line.relative_path,
                    "line_number": line.line_number,
                    "raw_line": line.text,
                    "raw_line_sha256": bytes_sha256(line.raw_line),
                }
            )
        )
    result = b"".join(records)
    if len(result) > maximum_bytes:
        raise ValueError("decision evidence exceeds the Job context limit")
    return result


def _diagnosis_subject_hash(
    job: Job,
    draft: AgentJobOutcomeDraftV2,
    required_rule_ids: list[str],
) -> str:
    return bytes_sha256(
        canonical_json_bytes(
            {
                "schema_version": 2,
                "job_id": job.job_id,
                "case_id": job.case_id,
                "skill_ref": job.skill_ref.model_dump(mode="json"),
                "problem_spec": job.context_snapshot.problem_spec.model_dump(
                    mode="json"
                ),
                "user_facts": [
                    item.model_dump(mode="json")
                    for item in job.context_snapshot.user_facts
                ],
                "candidate_conclusion_draft": (
                    None
                    if getattr(
                        draft.payload,
                        "candidate_conclusion_draft",
                        None,
                    )
                    is None
                    else draft.payload.candidate_conclusion_draft.model_dump(
                        mode="json"
                    )
                ),
                "required_rule_ids": required_rule_ids,
            }
        )
    )


def verify_agent_draft(
    *,
    workspace_root: Path,
    job: Job,
    manifest: WorkspaceInputManifest,
    draft: AgentJobOutcomeDraftV2,
    draft_bytes: bytes,
    proposal_resources: Iterable[Any],
    skill_root: Path,
    broker_audit_bytes: bytes | None,
    diagnosis_audit: DecisionAuditV2 | None,
) -> VerificationResult:
    """Recompute every pinned rule and return one complete DecisionAuditV2."""

    if job.job_type is JobType.ROUTE or job.skill_ref is None:
        raise ValueError("ROUTE drafts do not enter the decision verifier")
    contract, pinned_requirements = _load_contract(skill_root)
    _validate_requirement_requests(
        job=job,
        manifest=manifest,
        draft=draft,
        pinned_requirements=pinned_requirements,
    )
    extractors = list(contract["event_extractors"])
    rules = list(contract["rules"])
    required_rule_ids = [item["id"] for item in rules]
    claims = {item.rule_id: item for item in draft.rule_claims}
    unknown_claims = [item for item in claims if item not in set(required_rule_ids)]
    if unknown_claims:
        raise ValueError("Agent draft claims rules outside the pinned Skill")

    anchor_paths, allowed_logparse_sources = _target_anchor_paths(
        broker_audit_bytes,
        job=job,
        manifest=manifest,
    )
    review_anchor_paths, review_sources = _diagnosis_anchor_paths(
        diagnosis_audit,
        manifest=manifest,
    )
    for source_path, anchor in review_anchor_paths.items():
        anchor_paths.setdefault(source_path, anchor)
    if job.job_type is JobType.REVIEW:
        allowed_logparse_sources = review_sources
    enforce_logparse_source_lock = (
        job.job_type is JobType.REVIEW or manifest.logparse_tool_ref is not None
    )
    (
        lines,
        claim_bindings,
        claim_line_keys,
        participating_bindings,
        incomplete_anchors,
    ) = _resolve_evidence_lines(
        workspace_root=workspace_root,
        job=job,
        manifest=manifest,
        draft=draft,
        proposal_resources=proposal_resources,
        anchor_paths=anchor_paths,
        allowed_logparse_sources=(
            allowed_logparse_sources if enforce_logparse_source_lock else None
        ),
    )
    events, anchor_has_lines = _extract_events(
        extractors,
        lines,
        incomplete_anchors,
    )
    extractor_by_id = {item["id"]: item for item in extractors}
    fact_values = _facts(job)
    evaluated: dict[str, ServerRuleEvaluation] = {}
    audit_rules: list[DecisionRuleAudit] = []
    alignments: list[RuleClaimResult | None] = []
    for rule in rules:
        evaluation = _evaluate_rule(
            rule,
            events=events,
            anchor_has_lines=anchor_has_lines,
            extractor_by_id=extractor_by_id,
            facts=fact_values,
            prior=evaluated,
        )
        evaluated[rule["id"]] = evaluation
        claim = claims.get(rule["id"])
        required_line_keys = {
            (
                match.line.binding_key,
                match.line.relative_path,
                match.line.line_number,
            )
            for event_id in _rule_events(rule)
            for match in events[event_id]
        }
        alignments.append(
            _claim_alignment(
                claim,
                evaluation,
                claim_bindings,
                claim_line_keys,
                required_line_keys,
            )
        )
        audit_rules.append(
            DecisionRuleAudit(
                rule_id=rule["id"],
                agent_claim=claim,
                server_evaluation=evaluation,
            )
        )

    review_subject = manifest.review_subject
    if job.job_type is JobType.REVIEW:
        if review_subject is None or diagnosis_audit is None:
            raise ValueError("REVIEW verification lacks its private diagnosis binding")
        if review_subject.required_rule_ids != required_rule_ids:
            raise ValueError("Review subject rule IDs differ from the pinned Skill")
        semantic_ids = [
            item["id"] for item in rules if item["kind"] == "SEMANTIC_CAUSALITY"
        ]
        if [item.rule_id for item in review_subject.causal_assertions] != semantic_ids:
            raise ValueError("Review subject semantic rules differ from the pinned Skill")
        inherited_integrity, inherited_positive = (
            _diagnosis_audit_review_gates(
                diagnosis_audit,
                job=job,
                rules=rules,
                review_subject=review_subject,
            )
        )
        required_bindings = [
            EvidenceBinding(existing_evidence_id=item, evidence_proposal_key=None)
            for item in review_subject.required_evidence_refs
        ]
        subject_hash = review_subject.subject_hash
        candidate_target = job.review_target
        diagnosis_hash = bytes_sha256(canonical_json_bytes(diagnosis_audit))
        decision_gate_passed = _review_decision_gate(
            draft=draft,
            alignments=alignments,
            inherited_integrity=inherited_integrity,
            inherited_positive=inherited_positive,
        )
    else:
        candidate_bindings = (
            _candidate_bindings(draft.payload)
            if isinstance(draft.payload, DiagnosisOutcome)
            else []
        )
        required_bindings = _unique_bindings(
            [*participating_bindings, *candidate_bindings]
        )
        subject_hash = _diagnosis_subject_hash(job, draft, required_rule_ids)
        candidate_target = None
        diagnosis_hash = None
        decision_gate_passed = _diagnosis_decision_gate(
            draft=draft,
            alignments=alignments,
            required_bindings=required_bindings,
        )

    audit = DecisionAuditV2(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        skill_ref=job.skill_ref,
        source_draft_sha256=bytes_sha256(draft_bytes),
        subject_hash=subject_hash,
        candidate_target=candidate_target,
        diagnosis_audit_hash=diagnosis_hash,
        required_rule_ids=required_rule_ids,
        required_evidence_bindings=required_bindings,
        rules=audit_rules,
    )
    return VerificationResult(
        audit=audit,
        positive_gate_passed=decision_gate_passed,
        decision_evidence_bytes=_decision_evidence(
            [
                match.line
                for event_id in _unique_strings(
                    event_id for rule in rules for event_id in _rule_events(rule)
                )
                for match in events[event_id]
            ],
            maximum_bytes=min(
                job.resource_limits.context_bytes,
                job.resource_limits.workspace_bytes,
            ),
        ),
    )


__all__ = ["VerificationResult", "verify_agent_draft"]
