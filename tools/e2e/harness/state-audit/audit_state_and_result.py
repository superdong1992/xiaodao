#!/usr/bin/env python3
"""Offline audit for the final Windows -> Linux Problem Locator journey.

The script intentionally reads only explicit evidence files.  It never reads
Claude settings, environment variables, DATA_ROOT, or network endpoints.  All
failures are reduced to stable codes so validation exceptions cannot echo
result content into the evidence report.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import uuid
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, TypeVar

from jsonschema import Draft202012Validator
from problem_locator.contracts.enums import (
    ArtifactKind,
    AttachmentStatus,
    CandidateStatus,
    CaseStatus,
    EvidenceSourceType,
    JobStatus,
    JobType,
    OutcomeDisposition,
    OutcomeResultType,
    RequirementKind,
    RequirementStatus,
    ResourceKind,
    ReviewVerdict,
    RouteKind,
)
from problem_locator.contracts.models import (
    Artifact,
    ArtifactView,
    CandidateConclusion,
    CaseAggregate,
    DiagnosisOutcome,
    Evidence,
    Job,
    JobOutcome,
    ReviewAssessment,
    RouteDecision,
    StateExport,
    UserResultPayload,
    ValidationReport,
    VersionedRef,
)
from problem_locator.contracts.outcomes import (
    validate_user_result_for_outcome,
    validate_user_result_resolution,
)
from problem_locator.contracts.serialization import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)


SCHEMA_VERSION = 1
SERVICE_BASE_URL = "http://127.0.0.1:18000"
EXPECTED_SKILL = VersionedRef(
    id="diagnosis-skill/diagnose-service-takeover",
    version="3.0.4",
    content_hash=(
        "08573b8e01e2b5c213c59b0b27b3922566293af1aed963c09c6f735f41abdd95"
    ),
)
EXPECTED_ARCHIVE_NAME = "synthetic-rpc-service-takeover.zip"
EXPECTED_ARCHIVE_CONTENT_TYPE = "application/zip"
EXPECTED_ARCHIVE_SIZE = 2367
EXPECTED_ARCHIVE_SHA256 = (
    "194f69fecd8dc8d40d1aedeb6fc25d2b7b4922b176be2b15be73ffe386cc5064"
)
EXPECTED_PROBLEM_SPEC = {
    "statement": (
        "A checkout-to-inventory ReserveStock RPC times out during a service "
        "takeover."
    ),
    "expected_behavior": (
        "The checkout operation completes after inventory reservation."
    ),
    "actual_behavior": (
        "During an active service takeover, the ReserveStock RPC times out "
        "and checkout does not complete."
    ),
    "scope": "checkout-to-inventory service-takeover RPC diagnosis",
    "goals": [
        "Locate the service-takeover timeout cause using the supplied logs."
    ],
    "non_goals": ["Modify production systems."],
    "constraints": ["Use only evidence persisted in this diagnosis case."],
    "completion_criteria": [
        "Identify the timed-out request and an evidence-backed root cause."
    ],
    "revision": 1,
}
EXPECTED_FACTS = {
    "caller_service": "checkout-synthetic",
    "server_service": "inventory-synthetic",
    "rpc_method": "ReserveStock",
    "problem_time": "2026-07-31T00:00:03.000Z",
    "order_id": "synthetic-order-0001",
}
PARAMETER_A_NAMES = frozenset(
    {"caller_service", "server_service", "rpc_method", "problem_time"}
)
EXPECTED_REQUIREMENT_KINDS = {
    "caller_service": RequirementKind.INPUT,
    "server_service": RequirementKind.INPUT,
    "rpc_method": RequirementKind.INPUT,
    "problem_time": RequirementKind.INPUT,
    "log_archive": RequirementKind.ATTACHMENT,
    "order_id": RequirementKind.INPUT,
}


class AuditFailure(Exception):
    """A safe, stable failure that never embeds untrusted input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def require(condition: bool, code: str) -> None:
    if not condition:
        raise AuditFailure(code)


_T = TypeVar("_T")


def require_one(values: Iterable[_T], code: str) -> _T:
    items = list(values)
    require(len(items) == 1, code)
    return items[0]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite number")


def read_ordinary_file(path: Path, code: str) -> bytes:
    try:
        require(not path.is_symlink(), code)
        require(path.is_file(), code)
        return path.read_bytes()
    except AuditFailure:
        raise
    except Exception as exc:
        raise AuditFailure(code) from exc


def load_strict_json(path: Path, code: str) -> Any:
    raw = read_ordinary_file(path, code)
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except Exception as exc:
        raise AuditFailure(code) from exc


def load_canonical_model(path: Path, model_type: type[_T], code: str) -> tuple[_T, bytes]:
    raw = read_ordinary_file(path, code)
    try:
        value = parse_canonical_json_bytes(raw, model_type=model_type)
        require(canonical_json_bytes(value) == raw, code)
        return value, raw
    except AuditFailure:
        raise
    except Exception as exc:
        raise AuditFailure(code) from exc


def write_exclusive_canonical(path: Path, value: Mapping[str, Any]) -> None:
    payload = canonical_json_bytes(dict(value))
    descriptor: int | None = None
    try:
        require(path.parent.is_dir(), "OUTPUT_PARENT_INVALID")
        require(not path.parent.is_symlink(), "OUTPUT_PARENT_INVALID")
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise AuditFailure("OUTPUT_EXISTS") from exc
    except AuditFailure:
        raise
    except Exception as exc:
        raise AuditFailure("OUTPUT_WRITE_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def derive_id(kind: str, parts: Sequence[str]) -> str:
    name = canonical_json_bytes({"kind": kind, "parts": list(parts)})[
        :-1
    ].decode("utf-8")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


@dataclass(frozen=True)
class JourneySummary:
    attempt: str | None
    case_id: str
    attachment_id: str
    resolved_case_revision: int
    diagnosis_state_revision: int
    selected_skill_ref: VersionedRef
    final_result: CandidateConclusion
    public_artifact: ArtifactView
    public_result_archive: ArtifactView
    observed_statuses: tuple[str, ...]
    request_ids: Mapping[str, str] | None


def _required_string(value: Any, key: str, code: str) -> str:
    require(isinstance(value, dict), code)
    item = value.get(key)
    require(isinstance(item, str) and bool(item), code)
    return item


def _required_positive_int(value: Any, key: str, code: str) -> int:
    require(isinstance(value, dict), code)
    item = value.get(key)
    require(type(item) is int and item > 0, code)
    return item


def load_journey_summary(path: Path, *, require_requests: bool, code: str) -> JourneySummary:
    payload = load_strict_json(path, code)
    require(isinstance(payload, dict), code)
    require(payload.get("schema_version") == 1, code)
    try:
        selected = VersionedRef.model_validate(payload.get("selected_skill_ref"))
        final = CandidateConclusion.model_validate(payload.get("final_result"))
        public = ArtifactView.model_validate(payload.get("public_artifact"))
        public_archive = ArtifactView.model_validate(payload.get("public_result_archive"))
    except Exception as exc:
        raise AuditFailure(code) from exc
    statuses_value = payload.get("observed_statuses", [])
    require(
        isinstance(statuses_value, list)
        and all(isinstance(item, str) for item in statuses_value),
        code,
    )
    request_ids_value = payload.get("request_ids")
    request_ids: Mapping[str, str] | None = None
    if request_ids_value is not None:
        require(
            isinstance(request_ids_value, dict)
            and all(
                isinstance(key, str)
                and isinstance(item, str)
                and bool(item)
                for key, item in request_ids_value.items()
            ),
            code,
        )
        request_ids = dict(request_ids_value)
    require(not require_requests or request_ids is not None, code)
    attempt_value = payload.get("attempt")
    require(attempt_value is None or isinstance(attempt_value, str), code)
    return JourneySummary(
        attempt=attempt_value,
        case_id=_required_string(payload, "case_id", code),
        attachment_id=_required_string(payload, "attachment_id", code),
        resolved_case_revision=_required_positive_int(
            payload, "resolved_case_revision", code
        ),
        diagnosis_state_revision=_required_positive_int(
            payload, "diagnosis_state_revision", code
        ),
        selected_skill_ref=selected,
        final_result=final,
        public_artifact=public,
        public_result_archive=public_archive,
        observed_statuses=tuple(statuses_value),
        request_ids=request_ids,
    )


def audit_validation(
    report: ValidationReport,
    exported: StateExport,
    prefix: str,
) -> None:
    require(report.valid is True, f"{prefix}_VALIDATION_NOT_VALID")
    require(report.errors == [], f"{prefix}_VALIDATION_ERRORS")
    require(
        report.schema_version == exported.schema_version,
        f"{prefix}_VALIDATION_SCHEMA",
    )
    require(
        report.contract_revision == exported.contract_revision,
        f"{prefix}_VALIDATION_CONTRACT",
    )
    require(
        report.generation == exported.source_generation,
        f"{prefix}_VALIDATION_GENERATION",
    )
    require(
        report.object_counts == exported.object_counts,
        f"{prefix}_VALIDATION_COUNTS",
    )


def expected_proposal_id(
    exported: StateExport,
    case_id: str,
    outcome_id: str,
    kind: str,
    proposal_key: str,
) -> str:
    return derive_id(
        kind,
        [exported.installation_id, case_id, outcome_id, proposal_key],
    )


def outcome_for_job(aggregate: CaseAggregate, job_id: str, code: str) -> JobOutcome:
    return require_one(
        (outcome for outcome in aggregate.outcomes.values() if outcome.job_id == job_id),
        code,
    )


def formal_artifact_for_proposal(
    exported: StateExport,
    aggregate: CaseAggregate,
    outcome: JobOutcome,
    proposal: Any,
    code: str,
) -> Artifact:
    artifact_id = expected_proposal_id(
        exported,
        aggregate.case.case_id,
        outcome.outcome_id,
        "artifact",
        proposal.proposal_key,
    )
    artifact = aggregate.artifacts.get(artifact_id)
    require(artifact is not None, code)
    require(artifact.case_id == aggregate.case.case_id, code)
    require(artifact.created_by_job_id == outcome.job_id, code)
    require(artifact.created_at == outcome.produced_at, code)
    require(artifact.kind is proposal.artifact_kind, code)
    require(artifact.name == proposal.name, code)
    require(artifact.content_type == proposal.content_type, code)
    require(artifact.resource_kind is proposal.resource_kind, code)
    require(artifact.size == proposal.size, code)
    require(artifact.sha256 == proposal.sha256, code)
    require(artifact.metadata == proposal.metadata, code)
    return artifact


def formal_evidence_for_proposal(
    exported: StateExport,
    aggregate: CaseAggregate,
    outcome: JobOutcome,
    proposal: Any,
    code: str,
) -> Evidence:
    evidence_id = expected_proposal_id(
        exported,
        aggregate.case.case_id,
        outcome.outcome_id,
        "evidence",
        proposal.proposal_key,
    )
    evidence = aggregate.evidence.get(evidence_id)
    require(evidence is not None, code)
    require(evidence.case_id == aggregate.case.case_id, code)
    require(evidence.collected_at == outcome.produced_at, code)
    require(evidence.source_type is proposal.source_type, code)
    require(evidence.locator == proposal.locator, code)
    require(evidence.summary == proposal.summary, code)
    require(evidence.content_hash == proposal.content_hash, code)
    source_binding = proposal.source_binding
    if source_binding.existing_source_ref is not None:
        require(evidence.source_ref == source_binding.existing_source_ref, code)
    else:
        proposal_key = source_binding.artifact_proposal_key
        require(isinstance(proposal_key, str), code)
        expected_source = expected_proposal_id(
            exported,
            aggregate.case.case_id,
            outcome.outcome_id,
            "artifact",
            proposal_key,
        )
        require(evidence.source_ref == expected_source, code)
    staged = proposal.staged_resource_ref
    if staged is None:
        require(evidence.resource_ref is None, code)
    else:
        require(evidence.resource_ref is not None, code)
        require(evidence.resource_ref.resource_kind is staged.resource_kind, code)
        require(evidence.resource_ref.size == staged.size, code)
        require(evidence.resource_ref.sha256 == staged.sha256, code)
    return evidence


def audit_outcome_processing(
    exported: StateExport,
    aggregate: CaseAggregate,
) -> None:
    for outcome in aggregate.outcomes.values():
        record = aggregate.outcome_processing_records.get(outcome.outcome_id)
        require(record is not None, "OUTCOME_PROCESSING_MISSING")
        require(record.job_id == outcome.job_id, "OUTCOME_PROCESSING_JOB")
        require(record.disposition is OutcomeDisposition.APPLIED, "OUTCOME_NOT_APPLIED")
        require(record.error_code is None, "OUTCOME_PROCESSING_ERROR")
        require(outcome.error is None, "OUTCOME_EXECUTION_ERROR")
        require(outcome.result_type is not OutcomeResultType.FAILED, "OUTCOME_FAILED")

        evidence_ids_by_key = {
            proposal.proposal_key: expected_proposal_id(
                exported,
                aggregate.case.case_id,
                outcome.outcome_id,
                "evidence",
                proposal.proposal_key,
            )
            for proposal in outcome.proposed_evidence
        }
        artifact_ids_by_key = {
            proposal.proposal_key: expected_proposal_id(
                exported,
                aggregate.case.case_id,
                outcome.outcome_id,
                "artifact",
                proposal.proposal_key,
            )
            for proposal in outcome.proposed_artifacts
        }
        require(
            set(record.accepted_evidence_ids) <= set(evidence_ids_by_key.values()),
            "OUTCOME_ACCEPTED_UNKNOWN_EVIDENCE",
        )
        require(
            set(record.accepted_artifact_ids) <= set(artifact_ids_by_key.values()),
            "OUTCOME_ACCEPTED_UNKNOWN_ARTIFACT",
        )
        for proposal in outcome.proposed_evidence:
            evidence_id = evidence_ids_by_key[proposal.proposal_key]
            if evidence_id in record.accepted_evidence_ids:
                formal_evidence_for_proposal(
                    exported,
                    aggregate,
                    outcome,
                    proposal,
                    "FORMAL_EVIDENCE_MISMATCH",
                )
            else:
                require(
                    evidence_id not in aggregate.evidence,
                    "UNACCEPTED_EVIDENCE_FORMALIZED",
                )
        for proposal in outcome.proposed_artifacts:
            artifact_id = artifact_ids_by_key[proposal.proposal_key]
            if artifact_id in record.accepted_artifact_ids:
                formal_artifact_for_proposal(
                    exported,
                    aggregate,
                    outcome,
                    proposal,
                    "FORMAL_ARTIFACT_MISMATCH",
                )
            else:
                require(
                    artifact_id not in aggregate.artifacts,
                    "UNACCEPTED_ARTIFACT_FORMALIZED",
                )


@dataclass(frozen=True)
class ExportFacts:
    exported: StateExport
    aggregate: CaseAggregate
    attachment_id: str
    logparse_artifact_id: str
    user_result_artifact_id: str
    user_result_archive_artifact_id: str
    parse_job_id: str
    parse_outcome_id: str
    candidate_job_id: str
    candidate_outcome_id: str
    review_job_id: str
    review_outcome_id: str
    user_result_sha256: str
    user_result_size: int
    user_result_archive_sha256: str
    user_result_archive_size: int


def _require_job_roles(aggregate: CaseAggregate) -> tuple[Job, list[Job], Job]:
    route_job = require_one(
        (job for job in aggregate.jobs.values() if job.job_type is JobType.ROUTE),
        "ROUTE_JOB_COUNT",
    )
    diagnose_jobs = [
        job for job in aggregate.jobs.values() if job.job_type is JobType.DIAGNOSE
    ]
    review_job = require_one(
        (job for job in aggregate.jobs.values() if job.job_type is JobType.REVIEW),
        "REVIEW_JOB_COUNT",
    )
    require(len(diagnose_jobs) == 4, "DIAGNOSE_JOB_COUNT")
    require(route_job.skill_ref is None, "ROUTE_SKILL_BINDING")
    require(route_job.available_skill_refs == [EXPECTED_SKILL], "ROUTE_SKILL_CATALOG")
    for job in diagnose_jobs:
        require(job.skill_ref == EXPECTED_SKILL, "DIAGNOSE_SKILL_BINDING")
        require(job.available_skill_refs == [], "DIAGNOSE_AVAILABLE_SKILLS")
        require(job.logparse_product == "compact", "DIAGNOSE_LOGPARSE_PRODUCT")
        require(job.logparse_tool_ref is not None, "DIAGNOSE_LOGPARSE_TOOL")
    require(review_job.skill_ref == EXPECTED_SKILL, "REVIEW_SKILL_BINDING")
    require(review_job.available_skill_refs == [], "REVIEW_AVAILABLE_SKILLS")
    require(review_job.logparse_tool_ref is None, "REVIEW_LOGPARSE_FORBIDDEN")
    return route_job, diagnose_jobs, review_job


def _require_requirements_and_facts(
    aggregate: CaseAggregate,
    attachment_id: str,
) -> None:
    state = aggregate.case.diagnosis_state
    require(
        state.problem_spec.model_dump(mode="json") == EXPECTED_PROBLEM_SPEC,
        "PROBLEM_SPEC_CHANGED",
    )
    facts_by_name: dict[str, Any] = {}
    for fact in state.user_facts:
        name = fact.provenance.input_name
        require(isinstance(name, str), "USER_FACT_PROVENANCE")
        require(name not in facts_by_name, "USER_FACT_DUPLICATE")
        facts_by_name[name] = fact
    require(
        {name: fact.statement for name, fact in facts_by_name.items()}
        == EXPECTED_FACTS,
        "USER_FACT_VALUES",
    )

    requirements: dict[str, Any] = {}
    for requirement in state.pending_requirements:
        require(requirement.name not in requirements, "REQUIREMENT_DUPLICATE")
        requirements[requirement.name] = requirement
    require(set(requirements) == set(EXPECTED_REQUIREMENT_KINDS), "REQUIREMENT_NAMES")
    for name, expected_kind in EXPECTED_REQUIREMENT_KINDS.items():
        requirement = requirements[name]
        require(requirement.kind is expected_kind, "REQUIREMENT_KIND")
        require(requirement.status is RequirementStatus.FULFILLED, "REQUIREMENT_STATUS")
        if expected_kind is RequirementKind.INPUT:
            require(
                requirement.fulfilled_by_refs == [facts_by_name[name].item_id],
                "INPUT_REQUIREMENT_FULFILLMENT",
            )
        else:
            require(
                requirement.fulfilled_by_refs == [attachment_id],
                "ATTACHMENT_REQUIREMENT_FULFILLMENT",
            )


def _outcome_requirement_names(outcome: JobOutcome) -> frozenset[str]:
    payload = outcome.payload
    if not isinstance(payload, DiagnosisOutcome):
        return frozenset()
    return frozenset(
        requirement.name for requirement in payload.state_delta.add_pending_requirements
    )


def audit_export(
    exported: StateExport,
    summary: JourneySummary,
    result_bytes: bytes,
    archive_bytes: bytes,
    result_schema: Mapping[str, Any],
) -> ExportFacts:
    require(exported.export_schema_version == 1, "EXPORT_SCHEMA_VERSION")
    require(exported.source_generation == exported.state.generation, "EXPORT_GENERATION")
    require(exported.installation_id == exported.state.installation_id, "EXPORT_INSTALLATION")
    require(len(exported.state.cases) == 1, "CASE_COUNT")
    require(exported.object_counts.cases == 1, "COUNT_CASES")
    require(exported.object_counts.jobs == 6, "COUNT_JOBS")
    require(exported.object_counts.outcomes == 6, "COUNT_OUTCOMES")
    require(
        exported.object_counts.outcome_processing_records == 6,
        "COUNT_OUTCOME_PROCESSING",
    )
    require(
        exported.object_counts.execution_failure_records == 0,
        "COUNT_EXECUTION_FAILURES",
    )
    require(exported.object_counts.attachments == 1, "COUNT_ATTACHMENTS")
    require(exported.object_counts.artifacts == 3, "COUNT_ARTIFACTS")
    require(exported.object_counts.idempotency_records == 6, "COUNT_IDEMPOTENCY")

    aggregate = exported.state.cases.get(summary.case_id)
    require(aggregate is not None, "SUMMARY_CASE_NOT_FOUND")
    case = aggregate.case
    require(case.status is CaseStatus.RESOLVED, "CASE_NOT_RESOLVED")
    require(case.failure is None, "CASE_FAILURE_PRESENT")
    require(case.active_job_id is None, "CASE_ACTIVE_JOB_PRESENT")
    require(case.selected_skill_ref == EXPECTED_SKILL, "CASE_SELECTED_SKILL")
    require(case.selected_skill_ref == summary.selected_skill_ref, "SUMMARY_SELECTED_SKILL")
    require(case.case_revision == summary.resolved_case_revision, "SUMMARY_CASE_REVISION")
    require(
        case.diagnosis_state.revision == summary.diagnosis_state_revision,
        "SUMMARY_DIAGNOSIS_REVISION",
    )
    require(case.final_result is not None, "FINAL_RESULT_MISSING")
    final_result = case.final_result
    require(final_result.status is CandidateStatus.ACCEPTED, "FINAL_RESULT_NOT_ACCEPTED")
    require(final_result == case.diagnosis_state.candidate_conclusion, "FINAL_RESULT_STATE")
    require(final_result == summary.final_result, "SUMMARY_FINAL_RESULT")
    require(bool(final_result.supporting_evidence_refs), "FINAL_SUPPORTING_EVIDENCE_EMPTY")
    require(
        len(final_result.completion_criteria_mapping) == 1,
        "FINAL_CRITERIA_COUNT",
    )
    require(
        all(
            mapping.satisfied and bool(mapping.evidence_refs)
            for mapping in final_result.completion_criteria_mapping
        ),
        "FINAL_CRITERIA_NOT_EVIDENCED",
    )

    require(len(aggregate.execution_failure_records) == 0, "EXECUTION_FAILURE_RECORDS")
    require(len(aggregate.jobs) == 6, "JOB_MAP_COUNT")
    require(len(aggregate.outcomes) == 6, "OUTCOME_MAP_COUNT")
    require(
        len(aggregate.outcome_processing_records) == 6,
        "OUTCOME_PROCESSING_MAP_COUNT",
    )
    require(
        all(job.status is JobStatus.SUCCEEDED for job in aggregate.jobs.values()),
        "JOB_NOT_SUCCEEDED",
    )
    audit_outcome_processing(exported, aggregate)

    attachment = require_one(aggregate.attachments.values(), "ATTACHMENT_COUNT")
    require(attachment.attachment_id == summary.attachment_id, "SUMMARY_ATTACHMENT_ID")
    require(attachment.status is AttachmentStatus.READY, "ATTACHMENT_NOT_READY")
    require(attachment.name == EXPECTED_ARCHIVE_NAME, "ATTACHMENT_NAME")
    require(
        attachment.content_type == EXPECTED_ARCHIVE_CONTENT_TYPE,
        "ATTACHMENT_CONTENT_TYPE",
    )
    require(
        attachment.declared_size == attachment.size == EXPECTED_ARCHIVE_SIZE,
        "ATTACHMENT_SIZE",
    )
    require(
        attachment.declared_sha256
        == attachment.sha256
        == EXPECTED_ARCHIVE_SHA256,
        "ATTACHMENT_SHA256",
    )
    require(attachment.storage_key is not None, "ATTACHMENT_STORAGE")
    _require_requirements_and_facts(aggregate, attachment.attachment_id)

    route_job, diagnose_jobs, review_job = _require_job_roles(aggregate)
    route_outcome = outcome_for_job(aggregate, route_job.job_id, "ROUTE_OUTCOME_COUNT")
    require(route_outcome.result_type is OutcomeResultType.COMPLETED, "ROUTE_RESULT_TYPE")
    require(isinstance(route_outcome.payload, RouteDecision), "ROUTE_PAYLOAD")
    require(route_outcome.payload.kind is RouteKind.MATCHED, "ROUTE_NOT_MATCHED")
    require(route_outcome.payload.skill_ref == EXPECTED_SKILL, "ROUTE_MATCHED_SKILL")

    diagnose_outcomes = [
        outcome_for_job(aggregate, job.job_id, "DIAGNOSE_OUTCOME_COUNT")
        for job in diagnose_jobs
    ]
    parameter_outcome = require_one(
        (
            outcome
            for outcome in diagnose_outcomes
            if outcome.result_type is OutcomeResultType.NEED_INPUT
            and _outcome_requirement_names(outcome) == PARAMETER_A_NAMES
        ),
        "PARAMETER_A_OUTCOME",
    )
    attachment_outcome = require_one(
        (
            outcome
            for outcome in diagnose_outcomes
            if outcome.result_type is OutcomeResultType.NEED_ATTACHMENT
            and _outcome_requirement_names(outcome) == frozenset({"log_archive"})
        ),
        "LOG_ATTACHMENT_OUTCOME",
    )
    parse_outcome = require_one(
        (
            outcome
            for outcome in diagnose_outcomes
            if outcome.result_type is OutcomeResultType.NEED_INPUT
            and _outcome_requirement_names(outcome) == frozenset({"order_id"})
            if any(
                proposal.artifact_kind is ArtifactKind.LOGPARSE_RUN
                for proposal in outcome.proposed_artifacts
            )
        ),
        "LOGPARSE_OUTCOME",
    )
    candidate_outcome = require_one(
        (
            outcome
            for outcome in diagnose_outcomes
            if isinstance(outcome.payload, DiagnosisOutcome)
            and outcome.payload.candidate_conclusion_draft is not None
        ),
        "CANDIDATE_OUTCOME",
    )
    require(
        len(
            {
                parameter_outcome.outcome_id,
                attachment_outcome.outcome_id,
                parse_outcome.outcome_id,
                candidate_outcome.outcome_id,
            }
        )
        == 4,
        "DIAGNOSE_OUTCOME_PARTITION",
    )
    require(parse_outcome.result_type is OutcomeResultType.NEED_INPUT, "PARSE_RESULT_TYPE")
    require(
        _outcome_requirement_names(parse_outcome) == frozenset({"order_id"}),
        "ORDER_REQUIREMENT_OUTCOME",
    )
    require(
        candidate_outcome.result_type is OutcomeResultType.COMPLETED,
        "CANDIDATE_RESULT_TYPE",
    )

    logparse_proposal = require_one(
        (
            proposal
            for proposal in parse_outcome.proposed_artifacts
            if proposal.artifact_kind is ArtifactKind.LOGPARSE_RUN
        ),
        "LOGPARSE_PROPOSAL_COUNT",
    )
    logparse_artifact = formal_artifact_for_proposal(
        exported,
        aggregate,
        parse_outcome,
        logparse_proposal,
        "LOGPARSE_FORMAL_ARTIFACT",
    )
    parse_processing = aggregate.outcome_processing_records[parse_outcome.outcome_id]
    require(
        parse_processing.accepted_artifact_ids == [logparse_artifact.artifact_id],
        "PARSE_ACCEPTED_ARTIFACT",
    )
    expected_parse_evidence_ids = {
        expected_proposal_id(
            exported,
            aggregate.case.case_id,
            parse_outcome.outcome_id,
            "evidence",
            proposal.proposal_key,
        )
        for proposal in parse_outcome.proposed_evidence
    }
    require(bool(expected_parse_evidence_ids), "PARSE_EVIDENCE_PROPOSALS_EMPTY")
    require(
        set(parse_processing.accepted_evidence_ids) == expected_parse_evidence_ids
        and len(parse_processing.accepted_evidence_ids) == len(expected_parse_evidence_ids),
        "PARSE_ACCEPTED_EVIDENCE",
    )
    require(
        logparse_artifact.resource_kind is ResourceKind.DIRECTORY,
        "LOGPARSE_RESOURCE_KIND",
    )
    metadata = logparse_artifact.metadata
    require(
        getattr(metadata, "source_attachment_id", None) == attachment.attachment_id,
        "LOGPARSE_SOURCE_ATTACHMENT",
    )
    require(
        getattr(metadata, "source_attachment_sha256", None) == attachment.sha256,
        "LOGPARSE_SOURCE_SHA256",
    )
    require(
        getattr(getattr(metadata, "parse_parameters", None), "product", None)
        == "compact",
        "LOGPARSE_METADATA_PRODUCT",
    )
    parse_job = aggregate.jobs[parse_outcome.job_id]
    require(parse_job.attachment_refs == [attachment.attachment_id], "PARSE_JOB_ATTACHMENT")
    require(parse_job.artifact_refs == [], "PARSE_JOB_EXISTING_RUN")
    for evidence_id in parse_processing.accepted_evidence_ids:
        evidence = aggregate.evidence.get(evidence_id)
        require(evidence is not None, "PARSE_EVIDENCE_MISSING")
        require(evidence.source_type is EvidenceSourceType.LOGPARSE, "PARSE_EVIDENCE_TYPE")
        require(evidence.source_ref == logparse_artifact.artifact_id, "PARSE_EVIDENCE_SOURCE")

    candidate_job = aggregate.jobs.get(final_result.proposed_by_job_id)
    require(candidate_job is not None, "CANDIDATE_JOB_MISSING")
    require(candidate_job.job_id == candidate_outcome.job_id, "CANDIDATE_JOB_IDENTITY")
    require(candidate_job.attachment_refs == [attachment.attachment_id], "CONTINUATION_ATTACHMENT")
    require(
        candidate_job.artifact_refs == [logparse_artifact.artifact_id],
        "CONTINUATION_ARTIFACT_INPUT",
    )
    require(
        set(candidate_job.evidence_refs) == set(parse_processing.accepted_evidence_ids)
        and len(candidate_job.evidence_refs) == len(parse_processing.accepted_evidence_ids),
        "CONTINUATION_EVIDENCE_INPUT",
    )
    require(
        candidate_job.previous_outcome_refs
        == [parse_outcome.outcome_id, *parse_job.previous_outcome_refs],
        "CONTINUATION_PREVIOUS_OUTCOME",
    )
    require(
        candidate_job.logparse_tool_ref == parse_job.logparse_tool_ref,
        "CONTINUATION_LOGPARSE_VERSION",
    )
    require(candidate_job.logparse_product == "compact", "CONTINUATION_PRODUCT")
    require(
        getattr(metadata, "logparse_version_ref", None)
        == candidate_job.logparse_tool_ref,
        "LOGPARSE_VERSION_REF",
    )
    require(
        all(
            proposal.artifact_kind is not ArtifactKind.LOGPARSE_RUN
            for proposal in candidate_outcome.proposed_artifacts
        ),
        "CONTINUATION_REPARSED_LOGPARSE_RUN",
    )

    user_result_proposal = require_one(
        (
            proposal
            for proposal in candidate_outcome.proposed_artifacts
            if proposal.artifact_kind is ArtifactKind.USER_RESULT
        ),
        "USER_RESULT_PROPOSAL_COUNT",
    )
    require(user_result_proposal.proposal_key == "user-result", "USER_RESULT_PROPOSAL_KEY")
    user_result_artifact = formal_artifact_for_proposal(
        exported,
        aggregate,
        candidate_outcome,
        user_result_proposal,
        "USER_RESULT_FORMAL_ARTIFACT",
    )
    require(user_result_artifact.name == "diagnosis-result.json", "USER_RESULT_NAME")
    require(user_result_artifact.content_type == "application/json", "USER_RESULT_CONTENT_TYPE")
    require(user_result_artifact.resource_kind is ResourceKind.FILE, "USER_RESULT_RESOURCE_KIND")
    archive_proposal = require_one(
        (
            proposal
            for proposal in candidate_outcome.proposed_artifacts
            if proposal.artifact_kind is ArtifactKind.USER_RESULT_ARCHIVE
        ),
        "USER_RESULT_ARCHIVE_PROPOSAL_COUNT",
    )
    require(
        archive_proposal.proposal_key == "user-result-archive",
        "USER_RESULT_ARCHIVE_PROPOSAL_KEY",
    )
    archive_artifact = formal_artifact_for_proposal(
        exported,
        aggregate,
        candidate_outcome,
        archive_proposal,
        "USER_RESULT_ARCHIVE_FORMAL_ARTIFACT",
    )
    require(archive_artifact.name == "result.zip", "USER_RESULT_ARCHIVE_NAME")
    require(
        archive_artifact.content_type == "application/zip",
        "USER_RESULT_ARCHIVE_CONTENT_TYPE",
    )
    require(
        archive_artifact.resource_kind is ResourceKind.FILE,
        "USER_RESULT_ARCHIVE_RESOURCE_KIND",
    )
    require(
        archive_artifact.metadata.user_result_proposal_key == user_result_proposal.proposal_key,
        "USER_RESULT_ARCHIVE_BINDING",
    )
    candidate_processing = aggregate.outcome_processing_records[candidate_outcome.outcome_id]
    require(
        set(candidate_processing.accepted_artifact_ids)
        == {
            user_result_artifact.artifact_id,
            archive_artifact.artifact_id,
        },
        "CANDIDATE_ACCEPTED_PUBLIC_ARTIFACTS",
    )
    expected_candidate_evidence_ids = {
        expected_proposal_id(
            exported,
            aggregate.case.case_id,
            candidate_outcome.outcome_id,
            "evidence",
            proposal.proposal_key,
        )
        for proposal in candidate_outcome.proposed_evidence
    }
    require(
        set(candidate_processing.accepted_evidence_ids)
        == expected_candidate_evidence_ids
        and len(candidate_processing.accepted_evidence_ids)
        == len(expected_candidate_evidence_ids),
        "CANDIDATE_ACCEPTED_EVIDENCE",
    )
    require(
        expected_candidate_evidence_ids.isdisjoint(expected_parse_evidence_ids),
        "CANDIDATE_EVIDENCE_ID_COLLISION",
    )

    all_artifact_kinds = [artifact.kind for artifact in aggregate.artifacts.values()]
    require(
        all_artifact_kinds.count(ArtifactKind.LOGPARSE_RUN) == 1,
        "FORMAL_LOGPARSE_COUNT",
    )
    require(
        all_artifact_kinds.count(ArtifactKind.USER_RESULT) == 1,
        "FORMAL_USER_RESULT_COUNT",
    )
    require(
        all_artifact_kinds.count(ArtifactKind.USER_RESULT_ARCHIVE) == 1,
        "FORMAL_USER_RESULT_ARCHIVE_COUNT",
    )
    require(
        ArtifactKind.DIAGNOSTIC_EXPORT not in all_artifact_kinds,
        "UNEXPECTED_DIAGNOSTIC_EXPORT",
    )

    used_evidence_ids = [
        *final_result.supporting_evidence_refs,
        *(
            evidence_id
            for mapping in final_result.completion_criteria_mapping
            for evidence_id in mapping.evidence_refs
        ),
    ]
    expected_final_evidence_ids = (
        expected_parse_evidence_ids | expected_candidate_evidence_ids
    )
    require(
        set(used_evidence_ids) == expected_final_evidence_ids,
        "FINAL_CANDIDATE_EVIDENCE_SET",
    )
    for evidence_id in used_evidence_ids:
        evidence = aggregate.evidence.get(evidence_id)
        require(evidence is not None, "FINAL_EVIDENCE_MISSING")
        require(evidence.source_type is EvidenceSourceType.LOGPARSE, "FINAL_EVIDENCE_TYPE")
        require(evidence.source_ref == logparse_artifact.artifact_id, "FINAL_EVIDENCE_SOURCE")

    candidate_draft = candidate_outcome.payload
    require(isinstance(candidate_draft, DiagnosisOutcome), "CANDIDATE_PAYLOAD")
    draft = candidate_draft.candidate_conclusion_draft
    require(draft is not None, "CANDIDATE_DRAFT_MISSING")
    expected_candidate_id = expected_proposal_id(
        exported,
        aggregate.case.case_id,
        candidate_outcome.outcome_id,
        "candidate_conclusion",
        draft.proposal_key,
    )
    require(final_result.conclusion_id == expected_candidate_id, "CANDIDATE_DERIVED_ID")

    try:
        result_payload = parse_canonical_json_bytes(
            result_bytes,
            model_type=UserResultPayload,
        )
        require(canonical_json_bytes(result_payload) == result_bytes, "USER_RESULT_CANONICAL")
        Draft202012Validator.check_schema(dict(result_schema))
        Draft202012Validator(dict(result_schema)).validate(
            result_payload.model_dump(mode="json")
        )
        validate_user_result_for_outcome(
            candidate_job,
            candidate_outcome,
            result_bytes,
        )
        evidence_ids_by_proposal = {
            proposal.proposal_key: expected_proposal_id(
                exported,
                aggregate.case.case_id,
                candidate_outcome.outcome_id,
                "evidence",
                proposal.proposal_key,
            )
            for proposal in candidate_outcome.proposed_evidence
        }
        validate_user_result_resolution(
            result_payload,
            final_result,
            evidence_ids_by_proposal,
        )
    except AuditFailure:
        raise
    except Exception as exc:
        raise AuditFailure("USER_RESULT_SCHEMA_OR_SEAM") from exc
    require(
        result_payload.problem_statement == case.diagnosis_state.problem_spec.statement,
        "USER_RESULT_PROBLEM_STATEMENT",
    )
    require(
        result_payload.candidate_statement == final_result.statement,
        "USER_RESULT_CANDIDATE_STATEMENT",
    )
    require(len(result_bytes) == user_result_artifact.size, "USER_RESULT_SIZE")
    result_sha256 = hashlib.sha256(result_bytes).hexdigest()
    require(result_sha256 == user_result_artifact.sha256, "USER_RESULT_SHA256")
    require(len(archive_bytes) == archive_artifact.size, "USER_RESULT_ARCHIVE_SIZE")
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    require(
        archive_sha256 == archive_artifact.sha256,
        "USER_RESULT_ARCHIVE_SHA256",
    )
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r") as result_archive:
            infos = result_archive.infolist()
            names = [info.filename for info in infos]
            expected_names = ["result.txt"] + [
                f"target-log-{index:03d}.log"
                for index in range(1, archive_artifact.metadata.target_log_count + 1)
            ]
            require(names == expected_names, "USER_RESULT_ARCHIVE_NAMES")
            require(len(names) == len(set(names)), "USER_RESULT_ARCHIVE_DUPLICATE")
            for info in infos:
                require(
                    info.date_time == (1980, 1, 1, 0, 0, 0),
                    "USER_RESULT_ARCHIVE_TIMESTAMP",
                )
                require(
                    info.compress_type == zipfile.ZIP_DEFLATED,
                    "USER_RESULT_ARCHIVE_COMPRESSION",
                )
            require(
                result_archive.read("result.txt")
                == (final_result.statement + "\n").encode("utf-8"),
                "USER_RESULT_ARCHIVE_RESULT_TEXT",
            )
    except AuditFailure:
        raise
    except Exception as exc:
        raise AuditFailure("USER_RESULT_ARCHIVE_INVALID") from exc

    public = summary.public_artifact
    require(public.artifact_id == user_result_artifact.artifact_id, "PUBLIC_ARTIFACT_ID")
    require(public.name == user_result_artifact.name, "PUBLIC_ARTIFACT_NAME")
    require(public.content_type == user_result_artifact.content_type, "PUBLIC_ARTIFACT_CONTENT_TYPE")
    require(public.size == user_result_artifact.size, "PUBLIC_ARTIFACT_SIZE")
    require(public.sha256 == user_result_artifact.sha256, "PUBLIC_ARTIFACT_SHA256")
    require(public.created_at == user_result_artifact.created_at, "PUBLIC_ARTIFACT_CREATED_AT")
    expected_url = (
        f"{SERVICE_BASE_URL}/api/v1/artifacts/{public.artifact_id}/content"
        f"?case_id={summary.case_id}"
    )
    require(public.download_url == expected_url, "PUBLIC_ARTIFACT_URL")
    require(public.artifact_id != logparse_artifact.artifact_id, "PUBLIC_INTERNAL_COLLISION")
    public_archive = summary.public_result_archive
    require(
        public_archive.artifact_id == archive_artifact.artifact_id,
        "PUBLIC_ARCHIVE_ID",
    )
    require(public_archive.name == archive_artifact.name, "PUBLIC_ARCHIVE_NAME")
    require(
        public_archive.content_type == archive_artifact.content_type,
        "PUBLIC_ARCHIVE_CONTENT_TYPE",
    )
    require(public_archive.size == archive_artifact.size, "PUBLIC_ARCHIVE_SIZE")
    require(public_archive.sha256 == archive_artifact.sha256, "PUBLIC_ARCHIVE_SHA256")
    require(
        public_archive.created_at == archive_artifact.created_at,
        "PUBLIC_ARCHIVE_CREATED_AT",
    )
    expected_archive_url = (
        f"{SERVICE_BASE_URL}/api/v1/artifacts/{public_archive.artifact_id}/content"
        f"?case_id={summary.case_id}"
    )
    require(public_archive.download_url == expected_archive_url, "PUBLIC_ARCHIVE_URL")
    require(public_archive.artifact_id != public.artifact_id, "PUBLIC_ARTIFACT_COLLISION")

    require(candidate_processing.created_job_id == review_job.job_id, "REVIEW_JOB_CREATION")
    require(review_job.review_target is not None, "REVIEW_TARGET_MISSING")
    review_target = review_job.review_target
    require(
        review_target.candidate_conclusion_id == final_result.conclusion_id
        and review_target.candidate_revision == final_result.revision
        and review_target.candidate_content_hash == final_result.content_hash,
        "REVIEW_TARGET_MISMATCH",
    )
    require(
        set(final_result.supporting_evidence_refs) <= set(review_job.evidence_refs),
        "REVIEW_EVIDENCE_SET",
    )
    review_outcome = outcome_for_job(aggregate, review_job.job_id, "REVIEW_OUTCOME_COUNT")
    require(review_outcome.result_type is OutcomeResultType.COMPLETED, "REVIEW_RESULT_TYPE")
    require(isinstance(review_outcome.payload, ReviewAssessment), "REVIEW_PAYLOAD")
    review = review_outcome.payload
    require(review.verdict is ReviewVerdict.PASS, "REVIEW_NOT_PASS")
    require(review.reviewed_state_revision == review_job.base_state_revision, "REVIEW_STATE_REVISION")
    require(
        set(final_result.supporting_evidence_refs)
        <= set(review.reviewed_evidence_refs),
        "REVIEW_MISSING_EVIDENCE",
    )
    require(
        review.unsupported_findings == []
        and review.evidence_conflicts == []
        and review.missing_evidence == []
        and review.stale_references == [],
        "REVIEW_PROBLEM_ARRAYS",
    )
    review_processing = aggregate.outcome_processing_records[review_outcome.outcome_id]
    require(review_processing.created_job_id is None, "REVIEW_CREATED_JOB")
    require(review_processing.accepted_evidence_ids == [], "REVIEW_ACCEPTED_EVIDENCE")
    require(review_processing.accepted_artifact_ids == [], "REVIEW_ACCEPTED_ARTIFACT")

    request_ids = summary.request_ids
    require(request_ids is not None, "REQUEST_IDS_MISSING")
    require(
        set(request_ids)
        == {"create", "submit_a", "prepare", "submit_attachment", "submit_order"},
        "REQUEST_ID_NAMES",
    )
    if summary.attempt is not None:
        expected_request_ids = {
            "create": f"{summary.attempt}-windows-create-v1",
            "submit_a": f"{summary.attempt}-windows-submit-a-v1",
            "prepare": f"{summary.attempt}-windows-prepare-log-v1",
            "submit_attachment": f"{summary.attempt}-windows-submit-attachment-v1",
            "submit_order": f"{summary.attempt}-windows-submit-order-v1",
        }
        require(dict(request_ids) == expected_request_ids, "REQUEST_ID_VALUES")
    expected_idempotency_keys = {
        f"CreateCase:{request_ids['create']}",
        f"SubmitSupplement:{request_ids['submit_a']}",
        f"PrepareAttachment:{request_ids['prepare']}",
        f"UploadAttachmentContent:{attachment.attachment_id}",
        f"SubmitSupplement:{request_ids['submit_attachment']}",
        f"SubmitSupplement:{request_ids['submit_order']}",
    }
    require(
        set(exported.state.idempotency_records) == expected_idempotency_keys,
        "IDEMPOTENCY_RECORD_KEYS",
    )

    return ExportFacts(
        exported=exported,
        aggregate=aggregate,
        attachment_id=attachment.attachment_id,
        logparse_artifact_id=logparse_artifact.artifact_id,
        user_result_artifact_id=user_result_artifact.artifact_id,
        user_result_archive_artifact_id=archive_artifact.artifact_id,
        parse_job_id=parse_job.job_id,
        parse_outcome_id=parse_outcome.outcome_id,
        candidate_job_id=candidate_job.job_id,
        candidate_outcome_id=candidate_outcome.outcome_id,
        review_job_id=review_job.job_id,
        review_outcome_id=review_outcome.outcome_id,
        user_result_sha256=result_sha256,
        user_result_size=len(result_bytes),
        user_result_archive_sha256=archive_sha256,
        user_result_archive_size=len(archive_bytes),
    )


def compare_summaries(before: JourneySummary, after: JourneySummary) -> None:
    require(before.case_id == after.case_id, "RESTART_CASE_ID")
    require(before.attachment_id == after.attachment_id, "RESTART_ATTACHMENT_ID")
    require(
        before.resolved_case_revision == after.resolved_case_revision,
        "RESTART_CASE_REVISION",
    )
    require(
        before.diagnosis_state_revision == after.diagnosis_state_revision,
        "RESTART_DIAGNOSIS_REVISION",
    )
    require(before.selected_skill_ref == after.selected_skill_ref, "RESTART_SELECTED_SKILL")
    require(before.final_result == after.final_result, "RESTART_FINAL_RESULT")
    require(before.public_artifact == after.public_artifact, "RESTART_PUBLIC_ARTIFACT")
    require(
        before.public_result_archive == after.public_result_archive,
        "RESTART_PUBLIC_ARCHIVE",
    )
    if before.observed_statuses:
        try:
            reviewing_index = before.observed_statuses.index("REVIEWING")
            resolved_index = before.observed_statuses.index("RESOLVED")
        except ValueError as exc:
            raise AuditFailure("JOURNEY_STATUS_OBSERVATION") from exc
        require(reviewing_index < resolved_index, "JOURNEY_STATUS_ORDER")
    if after.observed_statuses:
        require("RESOLVED" in after.observed_statuses, "RESTART_RESOLVED_OBSERVATION")


def compare_exports(before: ExportFacts, after: ExportFacts) -> None:
    pre = before.exported
    post = after.exported
    require(pre.installation_id == post.installation_id, "RESTART_INSTALLATION_ID")
    require(post.source_generation > pre.source_generation, "RESTART_GENERATION")
    require(pre.object_counts.runtime_epochs == 1, "CLEAN_BEFORE_RUNTIME_EPOCH_COUNT")
    require(
        pre.object_counts.recovery_processing_records == 1,
        "CLEAN_BEFORE_RECOVERY_COUNT",
    )
    require(before.aggregate == after.aggregate, "RESTART_CASE_AGGREGATE")
    require(pre.state.idempotency_records == post.state.idempotency_records, "RESTART_IDEMPOTENCY")
    require(pre.resources == post.resources, "RESTART_RESOURCES")
    require(pre.state.created_at == post.state.created_at, "RESTART_STATE_CREATED_AT")

    stable_count_fields = (
        "cases",
        "jobs",
        "outcomes",
        "outcome_processing_records",
        "execution_failure_records",
        "attachments",
        "evidence",
        "artifacts",
        "idempotency_records",
    )
    for field_name in stable_count_fields:
        require(
            getattr(pre.object_counts, field_name)
            == getattr(post.object_counts, field_name),
            "RESTART_STABLE_COUNTS",
        )
    require(
        post.object_counts.runtime_epochs == pre.object_counts.runtime_epochs + 1,
        "RESTART_RUNTIME_EPOCH_COUNT",
    )
    require(
        post.object_counts.recovery_processing_records
        == pre.object_counts.recovery_processing_records + 1,
        "RESTART_RECOVERY_COUNT",
    )
    require(
        post.state.runtime_epochs[: len(pre.state.runtime_epochs)]
        == pre.state.runtime_epochs,
        "RESTART_RUNTIME_PREFIX",
    )
    require(
        all(
            recovery_id in post.state.recovery_processing_records
            and post.state.recovery_processing_records[recovery_id] == record
            for recovery_id, record in pre.state.recovery_processing_records.items()
        ),
        "RESTART_RECOVERY_SUBSET",
    )
    require(
        all(
            record.recovery_completed_at is not None
            for record in post.state.runtime_epochs
        ),
        "RESTART_RUNTIME_INCOMPLETE",
    )
    require(
        all(
            record.completed_at is not None
            for record in post.state.recovery_processing_records.values()
        ),
        "RESTART_RECOVERY_INCOMPLETE",
    )
    require(
        before.logparse_artifact_id == after.logparse_artifact_id,
        "RESTART_LOGPARSE_ID",
    )
    require(
        before.user_result_artifact_id == after.user_result_artifact_id,
        "RESTART_USER_RESULT_ID",
    )
    require(
        before.user_result_sha256 == after.user_result_sha256,
        "RESTART_USER_RESULT_SHA256",
    )
    require(
        before.user_result_archive_artifact_id
        == after.user_result_archive_artifact_id,
        "RESTART_USER_RESULT_ARCHIVE_ID",
    )
    require(
        before.user_result_archive_sha256 == after.user_result_archive_sha256,
        "RESTART_USER_RESULT_ARCHIVE_SHA256",
    )


def load_result_schema(path: Path) -> Mapping[str, Any]:
    payload = load_strict_json(path, "USER_RESULT_SCHEMA_INVALID")
    require(isinstance(payload, dict), "USER_RESULT_SCHEMA_INVALID")
    try:
        raw = read_ordinary_file(path, "USER_RESULT_SCHEMA_INVALID")
        require(canonical_json_bytes(payload) == raw, "USER_RESULT_SCHEMA_NOT_CANONICAL")
        Draft202012Validator.check_schema(payload)
    except AuditFailure:
        raise
    except Exception as exc:
        raise AuditFailure("USER_RESULT_SCHEMA_INVALID") from exc
    return payload


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before-export", type=Path, required=True)
    parser.add_argument("--after-export", type=Path, required=True)
    parser.add_argument("--before-validation", type=Path, required=True)
    parser.add_argument("--after-validation", type=Path, required=True)
    parser.add_argument("--journey-summary", type=Path, required=True)
    parser.add_argument("--restart-summary", type=Path, required=True)
    parser.add_argument("--before-result", type=Path, required=True)
    parser.add_argument("--after-result", type=Path, required=True)
    parser.add_argument("--before-archive", type=Path, required=True)
    parser.add_argument("--after-archive", type=Path, required=True)
    parser.add_argument("--user-result-schema", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def perform_audit(arguments: argparse.Namespace) -> Mapping[str, Any]:
    before_export, _ = load_canonical_model(
        arguments.before_export, StateExport, "BEFORE_EXPORT_INVALID"
    )
    after_export, _ = load_canonical_model(
        arguments.after_export, StateExport, "AFTER_EXPORT_INVALID"
    )
    before_validation, _ = load_canonical_model(
        arguments.before_validation,
        ValidationReport,
        "BEFORE_VALIDATION_INVALID",
    )
    after_validation, _ = load_canonical_model(
        arguments.after_validation,
        ValidationReport,
        "AFTER_VALIDATION_INVALID",
    )
    journey = load_journey_summary(
        arguments.journey_summary,
        require_requests=True,
        code="JOURNEY_SUMMARY_INVALID",
    )
    restart = load_journey_summary(
        arguments.restart_summary,
        require_requests=False,
        code="RESTART_SUMMARY_INVALID",
    )
    before_result = read_ordinary_file(arguments.before_result, "BEFORE_RESULT_INVALID")
    after_result = read_ordinary_file(arguments.after_result, "AFTER_RESULT_INVALID")
    require(before_result == after_result, "RESTART_RESULT_BYTES")
    before_archive = read_ordinary_file(arguments.before_archive, "BEFORE_ARCHIVE_INVALID")
    after_archive = read_ordinary_file(arguments.after_archive, "AFTER_ARCHIVE_INVALID")
    require(before_archive == after_archive, "RESTART_ARCHIVE_BYTES")
    result_schema = load_result_schema(arguments.user_result_schema)

    audit_validation(before_validation, before_export, "BEFORE")
    audit_validation(after_validation, after_export, "AFTER")
    compare_summaries(journey, restart)
    before_facts = audit_export(
        before_export,
        journey,
        before_result,
        before_archive,
        result_schema,
    )
    restart_for_state = replace(
        restart,
        attempt=journey.attempt,
        request_ids=journey.request_ids,
    )
    after_facts = audit_export(
        after_export,
        restart_for_state,
        after_result,
        after_archive,
        result_schema,
    )
    compare_exports(before_facts, after_facts)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "case_id": journey.case_id,
        "case_revision": journey.resolved_case_revision,
        "diagnosis_state_revision": journey.diagnosis_state_revision,
        "selected_skill_ref": EXPECTED_SKILL.model_dump(mode="json"),
        "object_counts_before": before_export.object_counts.model_dump(mode="json"),
        "object_counts_after": after_export.object_counts.model_dump(mode="json"),
        "attachment": {
            "attachment_id": before_facts.attachment_id,
            "size": EXPECTED_ARCHIVE_SIZE,
            "sha256": EXPECTED_ARCHIVE_SHA256,
        },
        "logparse_run": {
            "artifact_id": before_facts.logparse_artifact_id,
            "producer_job_id": before_facts.parse_job_id,
            "producer_outcome_id": before_facts.parse_outcome_id,
        },
        "post_parse_input": {
            "job_id": before_facts.parse_job_id,
            "outcome_id": before_facts.parse_outcome_id,
            "evidence_and_run_accepted": True,
        },
        "continuation": {
            "job_id": before_facts.candidate_job_id,
            "outcome_id": before_facts.candidate_outcome_id,
            "reused_logparse_run": True,
        },
        "review": {
            "job_id": before_facts.review_job_id,
            "outcome_id": before_facts.review_outcome_id,
            "verdict": "PASS",
        },
        "user_result": {
            "artifact_id": before_facts.user_result_artifact_id,
            "size": before_facts.user_result_size,
            "sha256": before_facts.user_result_sha256,
        },
        "user_result_archive": {
            "artifact_id": before_facts.user_result_archive_artifact_id,
            "size": before_facts.user_result_archive_size,
            "sha256": before_facts.user_result_archive_sha256,
        },
        "persistence": {
            "before_generation": before_export.source_generation,
            "after_generation": after_export.source_generation,
            "case_aggregate_equal": True,
            "resources_equal": True,
            "result_bytes_equal": True,
            "archive_bytes_equal": True,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        report = perform_audit(arguments)
    except AuditFailure as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "FAIL",
            "failure_code": exc.code,
        }
        exit_code = 1
    except Exception:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "FAIL",
            "failure_code": "AUDIT_INTERNAL",
        }
        exit_code = 1
    else:
        exit_code = 0

    try:
        write_exclusive_canonical(arguments.output, report)
    except AuditFailure as exc:
        sys.stderr.write(f"state-audit={exc.code}\n")
        return 2
    sys.stdout.write(
        "state-audit=passed\n" if exit_code == 0 else "state-audit=failed\n"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
