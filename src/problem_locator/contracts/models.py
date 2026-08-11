"""Pydantic models for the frozen Problem Locator V5 public contract.

The module deliberately keeps all wire/persistence DTO definitions in one place;
``commands``, ``outcomes`` and ``errors`` provide responsibility-oriented exports.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

from .enums import (
    ArtifactKind,
    AssetKind,
    AttachmentFilenameSuffix,
    AttachmentStatus,
    CausalFactorRole,
    CandidateMutationAction,
    CandidateStatus,
    CaseStatus,
    CompletionCriterionStatus,
    ContextSectionKind,
    DiagnosisMode,
    DiagnosisResolutionStatus,
    DiagnosisItemStatus,
    DiagnosisProvenanceType,
    ErrorCode,
    EvidenceSourceType,
    ExecutionStage,
    FailureReportDisposition,
    FieldUpdateAction,
    GenericResultStatus,
    JobStatus,
    JobType,
    OutcomeDisposition,
    OutcomeResultType,
    RequirementKind,
    RequirementStatus,
    ResourceKind,
    ResourceType,
    ReviewVerdict,
    RouteKind,
    RuleClaimResult,
    ServerRuleStatus,
    SupplementPolicy,
    TriggerType,
    UnresolvedReasonCode,
)
from .limits import (
    CONTRACT_REVISION,
    JOB_STDOUT_STDERR_BYTES,
    MAX_ATTACHMENT_BYTES,
    MAX_CASE_RESOURCE_BYTES,
    MAX_DESCRIPTION_UTF8_BYTES,
    MAX_INITIAL_USER_FACTS,
    MAX_USER_TEXT_UTF8_BYTES,
    MAX_WAIT_SECONDS,
    SCHEMA_VERSION,
    default_resource_limits,
)


UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
UTC_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
CONTENT_TYPE_PATTERN = r"^[a-z0-9][a-z0-9!#$&^_.+\-]{0,62}/[a-z0-9][a-z0-9!#$&^_.+\-]{0,62}$"
NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
SKILL_NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
FORMAT_ID_PATTERN = r"^[a-z][a-z0-9.\-]{0,63}$"
GIT_OBJECT_PATTERN = r"^[0-9a-f]{40,64}$"


def _utf8_nonblank(value: str, *, max_bytes: int = MAX_USER_TEXT_UTF8_BYTES) -> str:
    if not value or value.isspace():
        raise ValueError("text must not be empty or Unicode whitespace")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"text exceeds {max_bytes} UTF-8 bytes")
    return value


def _validate_user_text(value: str) -> str:
    return _utf8_nonblank(value)


def _validate_description(value: str) -> str:
    return _utf8_nonblank(value, max_bytes=MAX_DESCRIPTION_UTF8_BYTES)


def _validate_utc_timestamp(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise ValueError("timestamp must be a real millisecond UTC RFC 3339 value") from exc
    if len(value.rsplit(".", 1)[-1]) != 4:  # exactly ``sssZ``
        raise ValueError("timestamp precision must be exactly milliseconds")
    return value


def _validate_relative_path(value: str) -> str:
    if not value or value.startswith("/") or "\\" in value:
        raise ValueError("path must be a non-empty relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("path contains an empty, dot, or dot-dot segment")
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError("drive-qualified paths are forbidden")
    return value


_ATTACHMENT_SUFFIXES_BY_CONTENT_TYPE = {
    "application/gzip": (
        AttachmentFilenameSuffix.TAR_GZ,
        AttachmentFilenameSuffix.TGZ,
        AttachmentFilenameSuffix.GZ,
    ),
    "application/zip": (AttachmentFilenameSuffix.ZIP,),
    "application/x-tar": (AttachmentFilenameSuffix.TAR,),
}
_ATTACHMENT_SUFFIXES_LONGEST_FIRST = tuple(
    sorted(AttachmentFilenameSuffix, key=lambda suffix: len(suffix.value), reverse=True)
)


def _validate_attachment_filename(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("attachment name must be a string")
    _utf8_nonblank(name)
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("attachment name must be a safe filename, not a path")
    if re.match(r"^[A-Za-z]:", name):
        raise ValueError("drive-qualified attachment names are forbidden")
    if any(unicodedata.category(character) == "Cc" for character in name):
        raise ValueError("attachment name must not contain control characters")
    return name


def _validate_attachment_content_type(content_type: str) -> str:
    if not isinstance(content_type, str):
        raise TypeError("content_type must be a string")
    if re.fullmatch(CONTENT_TYPE_PATTERN, content_type) is None:
        raise ValueError("content_type must use the canonical lowercase media-type form")
    return content_type


def _validate_attachment_suffix_for_content_type(
    content_type: str,
    filename_suffix: AttachmentFilenameSuffix | None,
) -> None:
    allowed_suffixes = _ATTACHMENT_SUFFIXES_BY_CONTENT_TYPE.get(content_type)
    if allowed_suffixes is None:
        if filename_suffix is not None:
            raise ValueError("filename_suffix is forbidden for this content_type")
        return
    if filename_suffix not in allowed_suffixes:
        raise ValueError("filename_suffix does not match content_type")


def derive_attachment_filename_suffix(
    name: str,
    content_type: str,
) -> AttachmentFilenameSuffix | None:
    """Derive the frozen canonical archive suffix from a safe attachment name."""

    _validate_attachment_filename(name)
    _validate_attachment_content_type(content_type)
    lowercase_name = name.lower()
    filename_suffix = next(
        (
            suffix
            for suffix in _ATTACHMENT_SUFFIXES_LONGEST_FIRST
            if lowercase_name.endswith(suffix.value)
        ),
        None,
    )
    if filename_suffix is not None and not name.endswith(filename_suffix.value):
        raise ValueError("attachment filename suffix must use canonical lowercase spelling")
    _validate_attachment_suffix_for_content_type(content_type, filename_suffix)
    return filename_suffix


def derive_attachment_content_type(name: str) -> str:
    """Derive the platform-owned archive media type from a safe filename."""

    _validate_attachment_filename(name)
    lowercase_name = name.lower()
    filename_suffix = next(
        (
            suffix
            for suffix in _ATTACHMENT_SUFFIXES_LONGEST_FIRST
            if lowercase_name.endswith(suffix.value)
        ),
        None,
    )
    if filename_suffix is None:
        raise ValueError("content_type cannot be derived from unsupported attachment suffix")
    if not name.endswith(filename_suffix.value):
        raise ValueError("attachment filename suffix must use canonical lowercase spelling")
    return next(
        content_type
        for content_type, suffixes in _ATTACHMENT_SUFFIXES_BY_CONTENT_TYPE.items()
        if filename_suffix in suffixes
    )


def workspace_attachment_relative_path(
    attachment_id: str,
    filename_suffix: AttachmentFilenameSuffix | None,
) -> str:
    """Construct the only valid workspace path for a materialized attachment."""

    if not isinstance(attachment_id, str) or re.fullmatch(UUID_PATTERN, attachment_id) is None:
        raise ValueError("attachment_id must be a canonical lowercase UUID")
    if filename_suffix is not None and not isinstance(
        filename_suffix, AttachmentFilenameSuffix
    ):
        raise TypeError("filename_suffix must be AttachmentFilenameSuffix or None")
    suffix = "" if filename_suffix is None else filename_suffix.value
    return f"inputs/attachments/{attachment_id}/payload{suffix}"


def _validate_json_pointer(value: str) -> str:
    if value == "":
        return value
    if not value.startswith("/") or re.search(r"~(?![01])", value):
        raise ValueError("value must be an RFC 6901 JSON Pointer")
    return value


def _unique(values: list[Any], label: str) -> None:
    comparable = [value.value if hasattr(value, "value") else value for value in values]
    if len(comparable) != len(set(comparable)):
        raise ValueError(f"{label} must not contain duplicates")


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


OpaqueId: TypeAlias = Annotated[str, StringConstraints(pattern=UUID_PATTERN, strict=True)]
UtcTimestamp: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=UTC_TIMESTAMP_PATTERN, strict=True),
    AfterValidator(_validate_utc_timestamp),
]
Sha256: TypeAlias = Annotated[str, StringConstraints(pattern=SHA256_PATTERN, strict=True)]
ContentType: TypeAlias = Annotated[str, StringConstraints(pattern=CONTENT_TYPE_PATTERN, strict=True)]
ContractName: TypeAlias = Annotated[str, StringConstraints(pattern=NAME_PATTERN, strict=True)]
SkillName: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=SKILL_NAME_PATTERN,
        strict=True,
    ),
]
NonEmptyText: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_USER_TEXT_UTF8_BYTES, strict=True),
    AfterValidator(_validate_user_text),
]
DescriptionText: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_DESCRIPTION_UTF8_BYTES, strict=True),
    AfterValidator(_validate_description),
]
RelativePosixPath: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, strict=True),
    Field(
        json_schema_extra={
            "pattern": r"^(?!/)(?![A-Za-z]:)(?!.*\\)(?!.*(?:^|/)(?:\.{1,2})(?:/|$))(?!.*//).+$"
        }
    ),
    AfterValidator(_validate_relative_path),
]
JsonPointer: TypeAlias = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_validate_json_pointer),
]
NonNegativeInt: TypeAlias = Annotated[int, Field(ge=0, strict=True)]
PositiveInt: TypeAlias = Annotated[int, Field(gt=0, strict=True)]
Confidence: TypeAlias = Annotated[float, Field(ge=0.0, le=1.0, strict=True, allow_inf_nan=False)]
CanonicalJsonBytes: TypeAlias = bytes


class ContractModel(BaseModel):
    """Strict base for every serializable V2 contract DTO."""

    model_config = ConfigDict(
        extra="forbid",
        # Enum wire values must be accepted from decoded JSON objects.  Scalar
        # aliases carry their own strict constraints to prevent coercion.
        strict=False,
        validate_assignment=True,
        str_strip_whitespace=False,
    )


class VersionedRef(ContractModel):
    id: NonEmptyText
    version: NonEmptyText
    content_hash: Sha256


class ResourceLimits(ContractModel):
    context_bytes: PositiveInt
    wall_time_seconds: PositiveInt
    stdout_stderr_bytes: PositiveInt
    workspace_bytes: PositiveInt


def _validate_role_bindings(
    job_type: JobType,
    *,
    diagnosis_mode: DiagnosisMode | None,
    generic_skill_name: str | None,
    available_skill_refs: list[VersionedRef],
    skill_ref: VersionedRef | None,
    logparse_tool_ref: VersionedRef | None,
    logparse_product: str | None,
    resource_limits: ResourceLimits,
) -> None:
    if resource_limits != default_resource_limits(job_type):
        raise ValueError("resource_limits must equal the frozen defaults for the Job role")
    if job_type is JobType.ROUTE:
        if (
            diagnosis_mode is not None
            or generic_skill_name is not None
            or skill_ref is not None
            or logparse_tool_ref is not None
            or logparse_product is not None
        ):
            raise ValueError("ROUTE bindings forbid selected skill and logparse")
        return
    if available_skill_refs:
        raise ValueError("DIAGNOSE/REVIEW bindings forbid route candidates")
    if job_type is JobType.REVIEW:
        if diagnosis_mode is not None or generic_skill_name is not None:
            raise ValueError("REVIEW bindings forbid diagnosis mode and generic Skill")
        if skill_ref is None:
            raise ValueError("REVIEW bindings require one selected Skill")
        if logparse_tool_ref is not None or logparse_product is not None:
            raise ValueError("REVIEW bindings forbid logparse")
        return
    if diagnosis_mode is DiagnosisMode.SPECIALIZED:
        if generic_skill_name is not None or skill_ref is None:
            raise ValueError("SPECIALIZED DIAGNOSE requires one selected Skill")
        return
    if diagnosis_mode is DiagnosisMode.GENERIC:
        if (
            generic_skill_name is None
            or skill_ref is not None
            or logparse_tool_ref is not None
            or logparse_product is not None
        ):
            raise ValueError(
                "GENERIC DIAGNOSE requires only a preinstalled generic Skill name"
            )
        return
    raise ValueError("DIAGNOSE bindings require an explicit diagnosis mode")


class ProblemSpecInput(ContractModel):
    statement: NonEmptyText
    expected_behavior: NonEmptyText
    actual_behavior: NonEmptyText
    scope: NonEmptyText
    goals: Annotated[list[NonEmptyText], Field(min_length=1)]
    non_goals: list[NonEmptyText]
    constraints: list[NonEmptyText]
    completion_criteria: Annotated[list[NonEmptyText], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_lists(self) -> ProblemSpecInput:
        if not self.goals or not self.completion_criteria:
            raise ValueError("goals and completion_criteria must be non-empty")
        for field_name in ("goals", "non_goals", "constraints", "completion_criteria"):
            _unique(getattr(self, field_name), field_name)
        return self


class ProblemSpec(ProblemSpecInput):
    revision: PositiveInt


class ProblemSpecPatch(ContractModel):
    # These fields are optional-by-absence, not nullable.  A default factory
    # keeps them out of JSON Schema's ``required`` set without teaching the
    # validator that an explicit JSON null is legal.  The serializer below
    # then preserves the exact patch presence semantics when nested inside an
    # Outcome.
    statement: NonEmptyText = Field(default_factory=lambda: None)  # type: ignore[arg-type]
    expected_behavior: NonEmptyText = Field(default_factory=lambda: None)  # type: ignore[arg-type]
    actual_behavior: NonEmptyText = Field(default_factory=lambda: None)  # type: ignore[arg-type]
    scope: NonEmptyText = Field(default_factory=lambda: None)  # type: ignore[arg-type]
    goals: Annotated[list[NonEmptyText], Field(min_length=1)] = Field(default_factory=lambda: None)  # type: ignore[arg-type]
    non_goals: list[NonEmptyText] = Field(default_factory=lambda: None)  # type: ignore[arg-type]
    constraints: list[NonEmptyText] = Field(default_factory=lambda: None)  # type: ignore[arg-type]
    completion_criteria: Annotated[list[NonEmptyText], Field(min_length=1)] = Field(default_factory=lambda: None)  # type: ignore[arg-type]

    @model_serializer(mode="wrap")
    def serialize_present_fields(self, handler: Any) -> dict[str, Any]:
        serialized = handler(self)
        return {
            field_name: serialized[field_name]
            for field_name in sorted(self.model_fields_set)
        }

    @model_validator(mode="after")
    def validate_patch(self) -> ProblemSpecPatch:
        if not self.model_fields_set:
            raise ValueError("problem spec patch must contain at least one field")
        for field_name in ("goals", "non_goals", "constraints", "completion_criteria"):
            if field_name in self.model_fields_set:
                values = getattr(self, field_name)
                if field_name in {"goals", "completion_criteria"} and not values:
                    raise ValueError(f"{field_name} must be non-empty")
                _unique(values, field_name)
        return self


class UserFactInput(ContractModel):
    name: ContractName
    value: NonEmptyText


class DiagnosisProvenance(ContractModel):
    source_type: DiagnosisProvenanceType
    source_ref: OpaqueId
    input_name: ContractName | None

    @model_validator(mode="after")
    def validate_input_name(self) -> DiagnosisProvenance:
        if self.source_type is DiagnosisProvenanceType.USER_INPUT and self.input_name is None:
            raise ValueError("USER_INPUT provenance requires input_name")
        if self.source_type is DiagnosisProvenanceType.AGENT_OUTCOME and self.input_name is not None:
            raise ValueError("AGENT_OUTCOME provenance forbids input_name")
        return self


class DiagnosisItem(ContractModel):
    item_id: OpaqueId
    statement: NonEmptyText
    status: DiagnosisItemStatus
    provenance: DiagnosisProvenance
    evidence_refs: list[OpaqueId]
    created_revision: PositiveInt
    supersedes: list[OpaqueId]

    @model_validator(mode="after")
    def validate_refs(self) -> DiagnosisItem:
        _unique(self.evidence_refs, "evidence_refs")
        _unique(self.supersedes, "supersedes")
        if self.item_id in self.supersedes:
            raise ValueError("an item cannot supersede itself")
        return self


class InputRequirementConstraints(ContractModel):
    value_type: Literal["STRING"]
    min_utf8_bytes: PositiveInt
    max_utf8_bytes: PositiveInt
    pattern: str | None
    allowed_values: list[NonEmptyText]

    @model_validator(mode="after")
    def validate_constraints(self) -> InputRequirementConstraints:
        if self.min_utf8_bytes > self.max_utf8_bytes or self.max_utf8_bytes > MAX_USER_TEXT_UTF8_BYTES:
            raise ValueError("input byte limits must satisfy 1 <= min <= max <= 65536")
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError("pattern must be valid Python regular expression syntax") from exc
        _unique(self.allowed_values, "allowed_values")
        return self


class AttachmentRequirementConstraints(ContractModel):
    allowed_content_types: list[ContentType]
    min_count: PositiveInt
    max_count: PositiveInt

    @model_validator(mode="after")
    def validate_constraints(self) -> AttachmentRequirementConstraints:
        if self.min_count > self.max_count:
            raise ValueError("attachment counts must satisfy 1 <= min_count <= max_count")
        _unique(self.allowed_content_types, "allowed_content_types")
        return self


RequirementConstraints: TypeAlias = InputRequirementConstraints | AttachmentRequirementConstraints


class PendingRequirement(ContractModel):
    requirement_id: OpaqueId
    kind: RequirementKind
    name: ContractName
    prompt: NonEmptyText
    required: Literal[True]
    constraints: RequirementConstraints
    status: RequirementStatus
    requested_by_job_id: OpaqueId
    fulfilled_by_refs: list[OpaqueId]
    supplement_policy: SupplementPolicy = SupplementPolicy.NONE

    @model_validator(mode="after")
    def validate_kind(self) -> PendingRequirement:
        expected = InputRequirementConstraints if self.kind is RequirementKind.INPUT else AttachmentRequirementConstraints
        if not isinstance(self.constraints, expected):
            raise ValueError("requirement kind and constraints type do not match")
        _unique(self.fulfilled_by_refs, "fulfilled_by_refs")
        if self.status is RequirementStatus.OPEN and self.fulfilled_by_refs:
            raise ValueError("OPEN requirements cannot have fulfillment references")
        if self.status is RequirementStatus.FULFILLED:
            if isinstance(self.constraints, InputRequirementConstraints):
                if len(self.fulfilled_by_refs) != 1:
                    raise ValueError(
                        "FULFILLED INPUT requirements require exactly one user-fact reference"
                    )
            elif not (
                self.constraints.min_count
                <= len(self.fulfilled_by_refs)
                <= self.constraints.max_count
            ):
                raise ValueError(
                    "FULFILLED ATTACHMENT references must satisfy min_count/max_count"
                )
        return self


class CompletionCriterionMapping(ContractModel):
    criterion_index: NonNegativeInt
    criterion: NonEmptyText
    status: CompletionCriterionStatus
    evidence_refs: list[OpaqueId]
    explanation: NonEmptyText

    @model_validator(mode="after")
    def validate_mapping(self) -> CompletionCriterionMapping:
        _unique(self.evidence_refs, "evidence_refs")
        if self.status in {
            CompletionCriterionStatus.SATISFIED,
            CompletionCriterionStatus.PARTIALLY_SATISFIED,
        } and not self.evidence_refs:
            raise ValueError("a satisfied or partially satisfied criterion requires evidence")
        return self


class CausalFactor(ContractModel):
    factor_id: ContractName
    role: CausalFactorRole
    statement: NonEmptyText
    evidence_refs: Annotated[list[OpaqueId], Field(min_length=1)]
    required_rule_ids: Annotated[list[NonEmptyText], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_factor(self) -> CausalFactor:
        _unique(self.evidence_refs, "CausalFactor.evidence_refs")
        _unique(self.required_rule_ids, "CausalFactor.required_rule_ids")
        return self


class CandidateTarget(ContractModel):
    candidate_conclusion_id: OpaqueId
    candidate_revision: PositiveInt
    candidate_content_hash: Sha256


class CandidateConclusion(ContractModel):
    conclusion_id: OpaqueId
    revision: PositiveInt
    content_hash: Sha256
    resolution_status: DiagnosisResolutionStatus
    terminal_path_id: ContractName
    statement: NonEmptyText
    causal_factors: list[CausalFactor]
    candidate_factors: list[CausalFactor]
    excluded_factors: list[CausalFactor]
    supporting_evidence_refs: list[OpaqueId]
    completion_criteria_mapping: Annotated[
        list[CompletionCriterionMapping], Field(min_length=1)
    ]
    proposed_by_job_id: OpaqueId
    status: CandidateStatus

    @model_validator(mode="after")
    def validate_candidate(self) -> CandidateConclusion:
        _unique(self.supporting_evidence_refs, "supporting_evidence_refs")
        indices = [entry.criterion_index for entry in self.completion_criteria_mapping]
        if indices != list(range(len(indices))):
            raise ValueError("completion criteria mappings must be contiguous and sorted from index zero")
        factors = [*self.causal_factors, *self.candidate_factors, *self.excluded_factors]
        _unique([item.factor_id for item in factors], "Candidate factor_id")
        if self.resolution_status is DiagnosisResolutionStatus.COMPLETE:
            if not self.causal_factors:
                raise ValueError("a COMPLETE Candidate requires at least one causal factor")
            if self.candidate_factors:
                raise ValueError("a COMPLETE Candidate cannot retain candidate factors")
            if any(
                item.status is not CompletionCriterionStatus.SATISFIED
                for item in self.completion_criteria_mapping
            ):
                raise ValueError("a COMPLETE Candidate requires every criterion satisfied")
        else:
            if not (self.causal_factors or self.excluded_factors):
                raise ValueError(
                    "a PARTIAL Candidate requires a confirmed or excluded factor"
                )
            if not any(
                item.status
                in {
                    CompletionCriterionStatus.SATISFIED,
                    CompletionCriterionStatus.PARTIALLY_SATISFIED,
                }
                for item in self.completion_criteria_mapping
            ):
                raise ValueError(
                    "a PARTIAL Candidate requires evidence-backed criterion progress"
                )
            if all(
                item.status is CompletionCriterionStatus.SATISFIED
                for item in self.completion_criteria_mapping
            ):
                raise ValueError(
                    "a PARTIAL Candidate requires an explicitly incomplete criterion"
                )
        preimage = {
            "resolution_status": self.resolution_status,
            "terminal_path_id": self.terminal_path_id,
            "statement": self.statement,
            "causal_factors": [item.model_dump(mode="json") for item in self.causal_factors],
            "candidate_factors": [item.model_dump(mode="json") for item in self.candidate_factors],
            "excluded_factors": [item.model_dump(mode="json") for item in self.excluded_factors],
            "supporting_evidence_refs": self.supporting_evidence_refs,
            "completion_criteria_mapping": [entry.model_dump(mode="json") for entry in self.completion_criteria_mapping],
        }
        expected = hashlib.sha256(_canonical_json_bytes(preimage)).hexdigest()
        if self.content_hash != expected:
            raise ValueError("candidate content_hash does not match its canonical semantic preimage")
        return self


def review_required_evidence_refs(
    candidate: CandidateConclusion,
) -> tuple[OpaqueId, ...]:
    """Return the ordered Evidence closure that an independent review must cover."""

    ordered: list[OpaqueId] = []
    seen: set[OpaqueId] = set()
    refs = list(candidate.supporting_evidence_refs)
    refs.extend(
        ref
        for mapping in candidate.completion_criteria_mapping
        for ref in mapping.evidence_refs
    )
    refs.extend(
        ref
        for factor in (
            candidate.causal_factors
            + candidate.candidate_factors
            + candidate.excluded_factors
        )
        for ref in factor.evidence_refs
    )
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            ordered.append(ref)
    return tuple(ordered)


class DiagnosisState(ContractModel):
    revision: PositiveInt
    problem_spec: ProblemSpec
    user_facts: list[DiagnosisItem]
    confirmed_facts: list[DiagnosisItem]
    active_hypotheses: list[DiagnosisItem]
    rejected_hypotheses: list[DiagnosisItem]
    open_questions: list[DiagnosisItem]
    pending_requirements: list[PendingRequirement]
    evidence_refs: list[OpaqueId]
    candidate_conclusion: CandidateConclusion | None

    @model_validator(mode="after")
    def validate_state(self) -> DiagnosisState:
        all_items: list[DiagnosisItem] = []
        for field_name in (
            "user_facts",
            "confirmed_facts",
            "active_hypotheses",
            "rejected_hypotheses",
            "open_questions",
        ):
            values = getattr(self, field_name)
            _unique([item.item_id for item in values], f"{field_name}.item_id")
            all_items.extend(values)
        _unique([item.item_id for item in all_items], "DiagnosisState item IDs")
        if any(
            item.provenance.source_type is not DiagnosisProvenanceType.USER_INPUT
            for item in self.user_facts
        ):
            raise ValueError("user_facts require USER_INPUT provenance")
        user_fact_names = [
            item.provenance.input_name for item in self.user_facts
        ]
        if any(name is None for name in user_fact_names):
            raise ValueError("user_facts require an input_name")
        _unique(user_fact_names, "active user fact input_name")
        if any(
            item.provenance.source_type is not DiagnosisProvenanceType.AGENT_OUTCOME
            for item in (
                self.confirmed_facts
                + self.active_hypotheses
                + self.rejected_hypotheses
                + self.open_questions
            )
        ):
            raise ValueError("Agent-derived DiagnosisState items require AGENT_OUTCOME provenance")
        if any(not item.evidence_refs for item in self.confirmed_facts):
            raise ValueError("confirmed_facts must cite Evidence")
        if any(item.status is not DiagnosisItemStatus.ACTIVE for item in self.active_hypotheses):
            raise ValueError("active_hypotheses may only contain ACTIVE items")
        if any(item.status is not DiagnosisItemStatus.REJECTED for item in self.rejected_hypotheses):
            raise ValueError("rejected_hypotheses may only contain REJECTED items")
        if any(item.status is not DiagnosisItemStatus.ACTIVE for item in self.open_questions):
            raise ValueError("open_questions may only contain ACTIVE items")
        open_requirements = [item for item in self.pending_requirements if item.status is RequirementStatus.OPEN]
        _unique([item.name for item in open_requirements], "OPEN requirement names")
        if any(
            item.kind is RequirementKind.INPUT and item.name in user_fact_names
            for item in open_requirements
        ):
            raise ValueError(
                "an OPEN INPUT requirement cannot request a user fact already fixed in this Case"
            )
        if sum(item.kind is RequirementKind.ATTACHMENT for item in open_requirements) > 1:
            raise ValueError("at most one OPEN ATTACHMENT requirement is allowed")
        _unique(
            [item.requirement_id for item in self.pending_requirements],
            "pending requirement IDs",
        )
        _unique(self.evidence_refs, "evidence_refs")
        evidence_set = set(self.evidence_refs)
        if any(
            ref not in evidence_set
            for item in all_items
            for ref in item.evidence_refs
        ):
            raise ValueError("DiagnosisItem evidence_refs must belong to DiagnosisState")
        candidate = self.candidate_conclusion
        if candidate is not None:
            criteria = self.problem_spec.completion_criteria
            mappings = candidate.completion_criteria_mapping
            if len(mappings) != len(criteria) or any(
                mapping.criterion_index != index or mapping.criterion != criterion
                for index, (mapping, criterion) in enumerate(zip(mappings, criteria, strict=True))
            ):
                raise ValueError(
                    "candidate mapping must exactly cover the current ProblemSpec criteria"
                )
            candidate_evidence = list(candidate.supporting_evidence_refs)
            candidate_evidence.extend(
                ref for mapping in mappings for ref in mapping.evidence_refs
            )
            if any(ref not in evidence_set for ref in candidate_evidence):
                raise ValueError("candidate Evidence must belong to DiagnosisState")
            factor_evidence = [
                ref
                for factor in (
                    candidate.causal_factors
                    + candidate.candidate_factors
                    + candidate.excluded_factors
                )
                for ref in factor.evidence_refs
            ]
            if any(ref not in evidence_set for ref in factor_evidence):
                raise ValueError("candidate factor Evidence must belong to DiagnosisState")
        return self


class ContextSnapshot(ContractModel):
    diagnosis_state_revision: PositiveInt
    problem_spec: ProblemSpec
    user_facts: list[DiagnosisItem]
    confirmed_facts: list[DiagnosisItem]
    active_hypotheses: list[DiagnosisItem]
    rejected_hypotheses: list[DiagnosisItem]
    open_questions: list[DiagnosisItem]
    pending_requirements: list[PendingRequirement]
    evidence_refs: list[OpaqueId]
    candidate_conclusion: CandidateConclusion | None

    @model_validator(mode="after")
    def validate_refs(self) -> ContextSnapshot:
        DiagnosisState.model_validate(
            {
                "revision": self.diagnosis_state_revision,
                "problem_spec": self.problem_spec,
                "user_facts": self.user_facts,
                "confirmed_facts": self.confirmed_facts,
                "active_hypotheses": self.active_hypotheses,
                "rejected_hypotheses": self.rejected_hypotheses,
                "open_questions": self.open_questions,
                "pending_requirements": self.pending_requirements,
                "evidence_refs": self.evidence_refs,
                "candidate_conclusion": self.candidate_conclusion,
            }
        )
        return self


class CaseFailure(ContractModel):
    code: ErrorCode
    message: NonEmptyText
    source_job_id: OpaqueId | None
    source_outcome_id: OpaqueId | None
    occurred_at: UtcTimestamp


class UnresolvedResultDraft(ContractModel):
    source_job_id: OpaqueId
    source_outcome_id: OpaqueId
    reason_code: UnresolvedReasonCode
    summary: NonEmptyText
    blocking_rule_ids: list[NonEmptyText]
    evidence_bindings: list[EvidenceBinding]
    user_result_proposal_key: NonEmptyText
    recommended_next_step: NonEmptyText
    occurred_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_refs(self) -> UnresolvedResultDraft:
        _unique(self.blocking_rule_ids, "blocking_rule_ids")
        _unique(
            [
                item.existing_evidence_id
                or f"proposal:{item.evidence_proposal_key}"
                for item in self.evidence_bindings
            ],
            "unresolved evidence_bindings",
        )
        if self.reason_code in {
            UnresolvedReasonCode.MECHANICAL_VERIFICATION_FAILED,
            UnresolvedReasonCode.INSUFFICIENT_EVIDENCE,
        } and not self.blocking_rule_ids:
            raise ValueError("mechanical/insufficient unresolved results require blocking rules")
        return self


class UnresolvedResult(ContractModel):
    source_job_id: OpaqueId
    source_outcome_id: OpaqueId
    reason_code: UnresolvedReasonCode
    summary: NonEmptyText
    blocking_rule_ids: list[NonEmptyText]
    evidence_refs: list[OpaqueId]
    user_result_artifact_id: OpaqueId
    recommended_next_step: NonEmptyText
    occurred_at: UtcTimestamp
    audit_artifact_id: OpaqueId

    @model_validator(mode="after")
    def validate_refs(self) -> UnresolvedResult:
        _unique(self.blocking_rule_ids, "blocking_rule_ids")
        _unique(self.evidence_refs, "unresolved evidence_refs")
        if self.reason_code in {
            UnresolvedReasonCode.MECHANICAL_VERIFICATION_FAILED,
            UnresolvedReasonCode.INSUFFICIENT_EVIDENCE,
        } and not self.blocking_rule_ids:
            raise ValueError("mechanical/insufficient unresolved results require blocking rules")
        return self


class GenericResult(ContractModel):
    status: GenericResultStatus
    conclusion: NonEmptyText
    root_cause_analysis: NonEmptyText
    skill_name: SkillName
    source_job_id: OpaqueId
    source_outcome_id: OpaqueId
    occurred_at: UtcTimestamp


class Case(ContractModel):
    case_id: OpaqueId
    status: CaseStatus
    case_revision: PositiveInt
    raw_problem_text: NonEmptyText
    diagnosis_state: DiagnosisState
    active_job_id: OpaqueId | None
    selected_skill_ref: VersionedRef | None
    final_result: CandidateConclusion | None
    unresolved_result: UnresolvedResult | None = None
    generic_result: GenericResult | None = None
    failure: CaseFailure | None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_terminal_fields(self) -> Case:
        if (self.status is CaseStatus.FAILED) != (self.failure is not None):
            raise ValueError("failure must be present exactly for FAILED cases")
        if self.generic_result is not None:
            expected_status = CaseStatus(self.generic_result.status.value)
            if self.status is not expected_status:
                raise ValueError("generic_result status must equal the Case terminal status")
            if (
                self.final_result is not None
                or self.unresolved_result is not None
                or self.selected_skill_ref is not None
            ):
                raise ValueError(
                    "generic terminal cases forbid specialized result and selected Skill fields"
                )
        elif self.status in {
            CaseStatus.RESOLVED,
            CaseStatus.PARTIALLY_RESOLVED,
            CaseStatus.UNRESOLVED,
        }:
            pass
        if self.status in {CaseStatus.RESOLVED, CaseStatus.PARTIALLY_RESOLVED}:
            if self.generic_result is None and (
                self.final_result is None
                or self.final_result.status is not CandidateStatus.ACCEPTED
            ):
                raise ValueError("resolved cases require an ACCEPTED final_result")
            if self.generic_result is None and (
                self.diagnosis_state.candidate_conclusion != self.final_result
            ):
                raise ValueError(
                    "RESOLVED final_result must equal the current DiagnosisState candidate"
                )
            if self.generic_result is None and self.final_result is not None:
                expected = (
                    DiagnosisResolutionStatus.COMPLETE
                    if self.status is CaseStatus.RESOLVED
                    else DiagnosisResolutionStatus.PARTIAL
                )
                if self.final_result.resolution_status is not expected:
                    raise ValueError(
                        "final_result resolution_status must equal the Case status"
                    )
        elif self.final_result is not None:
            raise ValueError("non-resolved cases must have final_result=null")
        if self.status is CaseStatus.UNRESOLVED:
            if self.generic_result is None and self.unresolved_result is None:
                raise ValueError("UNRESOLVED cases require unresolved_result")
            if self.failure is not None:
                raise ValueError("UNRESOLVED cases forbid failure")
        elif self.unresolved_result is not None:
            raise ValueError("only UNRESOLVED cases may carry unresolved_result")
        if (
            self.generic_result is not None
            and self.status not in {CaseStatus.RESOLVED, CaseStatus.UNRESOLVED}
        ):
            raise ValueError("generic_result is valid only for a generic terminal Case")
        if self.status is CaseStatus.REVIEWING:
            candidate = self.diagnosis_state.candidate_conclusion
            if candidate is None or candidate.status is not CandidateStatus.REVIEWING:
                raise ValueError("REVIEWING cases require the current REVIEWING candidate")
        if self.status in {CaseStatus.RUNNING, CaseStatus.REVIEWING} and self.active_job_id is None:
            raise ValueError("RUNNING and REVIEWING cases require active_job_id")
        if self.status in {
            CaseStatus.WAITING_INPUT,
            CaseStatus.WAITING_ATTACHMENT,
            CaseStatus.RESOLVED,
            CaseStatus.PARTIALLY_RESOLVED,
            CaseStatus.UNRESOLVED,
            CaseStatus.FAILED,
            CaseStatus.CANCELLED,
            CaseStatus.INTERRUPTED,
        } and self.active_job_id is not None:
            raise ValueError("waiting and terminal/interrupted cases cannot retain an active job")
        return self


class Job(ContractModel):
    job_id: OpaqueId
    case_id: OpaqueId
    job_type: JobType
    diagnosis_mode: DiagnosisMode | None
    generic_skill_name: SkillName | None
    generic_problem_text: NonEmptyText | None
    status: JobStatus
    goal: NonEmptyText
    base_state_revision: PositiveInt
    context_snapshot: ContextSnapshot | None
    evidence_refs: list[OpaqueId]
    attachment_refs: list[OpaqueId]
    previous_outcome_refs: list[OpaqueId]
    artifact_refs: list[OpaqueId]
    agent_profile_ref: VersionedRef
    available_skill_refs: list[VersionedRef]
    skill_ref: VersionedRef | None
    tool_bundle_ref: VersionedRef
    context_policy_ref: VersionedRef
    output_contract_ref: VersionedRef
    logparse_tool_ref: VersionedRef | None
    logparse_product: NonEmptyText | None
    review_target: CandidateTarget | None
    replacement_for_job_id: OpaqueId | None
    resource_limits: ResourceLimits
    created_at: UtcTimestamp
    started_at: UtcTimestamp | None
    finished_at: UtcTimestamp | None
    runtime_epoch: OpaqueId | None

    @model_validator(mode="after")
    def validate_job(self) -> Job:
        generic = (
            self.job_type is JobType.DIAGNOSE
            and self.diagnosis_mode is DiagnosisMode.GENERIC
        )
        if generic:
            if self.context_snapshot is not None:
                raise ValueError(
                    "GENERIC DIAGNOSE forbids a specialized ContextSnapshot"
                )
        else:
            if self.context_snapshot is None:
                raise ValueError("non-generic Jobs require a ContextSnapshot")
            if (
                self.base_state_revision
                != self.context_snapshot.diagnosis_state_revision
            ):
                raise ValueError(
                    "base_state_revision must match context snapshot revision"
                )
        for name in ("evidence_refs", "attachment_refs", "previous_outcome_refs", "artifact_refs"):
            _unique(getattr(self, name), name)
        if self.context_snapshot is not None:
            snapshot_refs = self.context_snapshot.evidence_refs
            if [ref for ref in snapshot_refs if ref in set(self.evidence_refs)] != self.evidence_refs:
                raise ValueError("job evidence_refs must be a de-duplicated subsequence of snapshot evidence_refs")
        _unique([ref.id + "@" + ref.version + "#" + ref.content_hash for ref in self.available_skill_refs], "available_skill_refs")
        if (self.logparse_tool_ref is None) != (self.logparse_product is None):
            raise ValueError("logparse_tool_ref and logparse_product must be both null or both non-null")
        if self.logparse_tool_ref is not None and self.job_type is not JobType.DIAGNOSE:
            raise ValueError("only DIAGNOSE jobs may use logparse")
        if self.job_type is JobType.DIAGNOSE:
            if self.diagnosis_mode is DiagnosisMode.GENERIC:
                if self.generic_problem_text is None:
                    raise ValueError("GENERIC DIAGNOSE requires its frozen raw problem text")
                if any(
                    (
                        self.evidence_refs,
                        self.attachment_refs,
                        self.previous_outcome_refs,
                        self.artifact_refs,
                    )
                ):
                    raise ValueError(
                        "GENERIC DIAGNOSE forbids Evidence, Attachments, Artifacts, and history"
                    )
            elif self.generic_problem_text is not None:
                raise ValueError("SPECIALIZED DIAGNOSE forbids generic problem text")
        elif (
            self.diagnosis_mode is not None
            or self.generic_skill_name is not None
            or self.generic_problem_text is not None
        ):
            raise ValueError("only DIAGNOSE jobs may carry diagnosis-mode fields")
        if self.job_type is JobType.ROUTE:
            if self.review_target is not None:
                raise ValueError("ROUTE jobs forbid skill_ref/review_target")
        elif self.job_type is JobType.DIAGNOSE:
            if self.review_target is not None:
                raise ValueError("DIAGNOSE jobs require skill_ref and forbid review_target")
        else:
            if self.review_target is None or self.logparse_tool_ref is not None:
                raise ValueError("REVIEW jobs require review_target and forbid logparse")
            assert self.context_snapshot is not None
            candidate = self.context_snapshot.candidate_conclusion
            if candidate is None or candidate.status is not CandidateStatus.REVIEWING:
                raise ValueError("REVIEW snapshot requires a REVIEWING candidate")
            if (
                candidate.conclusion_id != self.review_target.candidate_conclusion_id
                or candidate.revision != self.review_target.candidate_revision
                or candidate.content_hash != self.review_target.candidate_content_hash
            ):
                raise ValueError("review_target must match the snapshot candidate")
            if any(
                ref not in self.evidence_refs
                for ref in review_required_evidence_refs(candidate)
            ):
                raise ValueError(
                    "REVIEW jobs must include every required candidate Evidence reference"
                )
        _validate_role_bindings(
            self.job_type,
            diagnosis_mode=self.diagnosis_mode,
            generic_skill_name=self.generic_skill_name,
            available_skill_refs=self.available_skill_refs,
            skill_ref=self.skill_ref,
            logparse_tool_ref=self.logparse_tool_ref,
            logparse_product=self.logparse_product,
            resource_limits=self.resource_limits,
        )
        if self.status is JobStatus.PENDING and any(
            value is not None for value in (self.started_at, self.finished_at, self.runtime_epoch)
        ):
            raise ValueError("PENDING jobs cannot have execution timestamps or runtime_epoch")
        if self.status is JobStatus.RUNNING and (self.started_at is None or self.finished_at is not None or self.runtime_epoch is None):
            raise ValueError("RUNNING jobs require started_at/runtime_epoch and forbid finished_at")
        if self.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED} and self.finished_at is None:
            raise ValueError("terminal jobs require finished_at")
        return self


class ContinuationResourceView(ContractModel):
    evidence_refs: list[OpaqueId]
    attachment_refs: list[OpaqueId]
    artifact_refs: list[OpaqueId]
    previous_outcome_refs: list[OpaqueId]

    @model_validator(mode="after")
    def validate_unique(self) -> ContinuationResourceView:
        for name in ("evidence_refs", "attachment_refs", "artifact_refs", "previous_outcome_refs"):
            _unique(getattr(self, name), name)
        return self


class ResourceRef(ContractModel):
    resource_kind: ResourceKind
    storage_key: RelativePosixPath
    size: NonNegativeInt
    sha256: Sha256


class Attachment(ContractModel):
    attachment_id: OpaqueId
    case_id: OpaqueId
    status: AttachmentStatus
    name: NonEmptyText
    content_type: ContentType
    declared_size: Annotated[
        int, Field(ge=0, le=MAX_ATTACHMENT_BYTES, strict=True)
    ] | None
    declared_sha256: Sha256 | None
    size: Annotated[int, Field(ge=0, le=MAX_ATTACHMENT_BYTES, strict=True)] | None
    sha256: Sha256 | None
    storage_key: RelativePosixPath | None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_attachment(self) -> Attachment:
        derive_attachment_filename_suffix(self.name, self.content_type)
        if self.declared_size is not None and self.declared_size > MAX_ATTACHMENT_BYTES:
            raise ValueError("declared attachment size exceeds the V1 limit")
        actuals = (self.size, self.sha256, self.storage_key)
        if any(value is None for value in actuals) and any(value is not None for value in actuals):
            raise ValueError("size, sha256, and storage_key must be all null or all non-null")
        if self.status is AttachmentStatus.READY:
            if any(value is None for value in actuals):
                raise ValueError("READY attachments require a complete immutable resource reference")
            if self.size is not None and self.size > MAX_ATTACHMENT_BYTES:
                raise ValueError("attachment size exceeds the V1 limit")
        elif self.storage_key is not None:
            raise ValueError("only READY attachments may have a storage_key")
        return self


class UserFactEvidenceLocator(ContractModel):
    kind: Literal["USER_FACT"]
    input_name: ContractName


class AttachmentEvidenceLocator(ContractModel):
    kind: Literal["ATTACHMENT"]
    byte_start: NonNegativeInt | None
    byte_end_exclusive: PositiveInt | None

    @model_validator(mode="after")
    def validate_bounds(self) -> AttachmentEvidenceLocator:
        if (self.byte_start is None) != (self.byte_end_exclusive is None):
            raise ValueError("attachment byte bounds must be both null or both non-null")
        if self.byte_start is not None and self.byte_end_exclusive is not None and self.byte_start >= self.byte_end_exclusive:
            raise ValueError("attachment byte bounds must satisfy start < end_exclusive")
        return self


class LogparseEvidenceLocator(ContractModel):
    kind: Literal["LOGPARSE"]
    relative_path: RelativePosixPath
    start_line: PositiveInt | None
    end_line: PositiveInt | None
    start_time: UtcTimestamp | None
    end_time: UtcTimestamp | None

    @model_validator(mode="after")
    def validate_bounds(self) -> LogparseEvidenceLocator:
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("line bounds must be both null or both non-null")
        if self.start_line is not None and self.end_line is not None and self.start_line > self.end_line:
            raise ValueError("line bounds must satisfy start_line <= end_line")
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("time bounds must be both null or both non-null")
        if self.start_time is not None and self.end_time is not None:
            start = datetime.strptime(self.start_time, "%Y-%m-%dT%H:%M:%S.%fZ")
            end = datetime.strptime(self.end_time, "%Y-%m-%dT%H:%M:%S.%fZ")
            if start > end:
                raise ValueError("time bounds must satisfy start_time <= end_time")
        return self


class ToolOutputEvidenceLocator(ContractModel):
    kind: Literal["TOOL_OUTPUT"]
    relative_path: RelativePosixPath
    json_pointer: JsonPointer | None


class PreviousOutcomeEvidenceLocator(ContractModel):
    kind: Literal["PREVIOUS_OUTCOME"]
    json_pointer: JsonPointer


EvidenceLocator: TypeAlias = Annotated[
    UserFactEvidenceLocator
    | AttachmentEvidenceLocator
    | LogparseEvidenceLocator
    | ToolOutputEvidenceLocator
    | PreviousOutcomeEvidenceLocator,
    Field(discriminator="kind"),
]


class Evidence(ContractModel):
    evidence_id: OpaqueId
    case_id: OpaqueId
    source_type: EvidenceSourceType
    source_ref: OpaqueId
    locator: EvidenceLocator
    summary: NonEmptyText
    collected_at: UtcTimestamp
    content_hash: Sha256 | None
    resource_ref: ResourceRef | None

    @model_validator(mode="after")
    def validate_locator(self) -> Evidence:
        if self.locator.kind != self.source_type.value:
            raise ValueError("evidence source_type must match locator.kind")
        if self.content_hash is not None and self.resource_ref is not None and self.content_hash != self.resource_ref.sha256:
            raise ValueError("resource-backed evidence content_hash must match resource_ref.sha256")
        return self


class UserResultMetadataV3(ContractModel):
    schema_version: Literal[3]
    format_id: Literal["problem-locator-diagnosis-v3"]
    description: DescriptionText


class UserResultArchiveMetadataV3(ContractModel):
    schema_version: Literal[3]
    format_id: Literal["problem-locator-result-archive-v3"]
    description: DescriptionText
    user_result_proposal_key: NonEmptyText
    target_log_count: NonNegativeInt


UserResultMetadata = UserResultMetadataV3
UserResultArchiveMetadata = UserResultArchiveMetadataV3


class DiagnosticExportMetadata(ContractModel):
    schema_version: Literal[1]
    format_id: Annotated[str, StringConstraints(pattern=FORMAT_ID_PATTERN, strict=True)]
    description: DescriptionText


class AuditBundleMetadata(ContractModel):
    schema_version: Literal[1]
    format_id: Literal["problem-locator-audit-bundle-v1"]
    description: DescriptionText
    case_id: OpaqueId
    source_job_id: OpaqueId
    source_outcome_id: OpaqueId


class LogparseParseParameters(ContractModel):
    product: NonEmptyText


class LogparseRunMetadata(ContractModel):
    tree_manifest_sha256: Sha256
    logparse_version_ref: VersionedRef
    parse_manifest_relative_path: RelativePosixPath
    source_attachment_id: OpaqueId
    source_attachment_sha256: Sha256
    parse_parameters: LogparseParseParameters


ArtifactMetadata: TypeAlias = (
    UserResultMetadataV3
    | UserResultArchiveMetadataV3
    | DiagnosticExportMetadata
    | LogparseRunMetadata
    | AuditBundleMetadata
)


class Artifact(ContractModel):
    artifact_id: OpaqueId
    case_id: OpaqueId
    kind: ArtifactKind
    name: NonEmptyText
    content_type: ContentType
    resource_kind: ResourceKind
    size: NonNegativeInt
    sha256: Sha256
    storage_key: RelativePosixPath
    metadata: ArtifactMetadata
    created_by_job_id: OpaqueId
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_artifact(self) -> Artifact:
        expected_type = {
            ArtifactKind.USER_RESULT: UserResultMetadataV3,
            ArtifactKind.USER_RESULT_ARCHIVE: UserResultArchiveMetadataV3,
            ArtifactKind.DIAGNOSTIC_EXPORT: DiagnosticExportMetadata,
            ArtifactKind.LOGPARSE_RUN: LogparseRunMetadata,
            ArtifactKind.AUDIT_BUNDLE: AuditBundleMetadata,
        }[self.kind]
        if not isinstance(self.metadata, expected_type):
            raise ValueError("artifact kind and metadata type do not match")
        if self.kind is ArtifactKind.USER_RESULT:
            if self.resource_kind is not ResourceKind.FILE or self.content_type != "application/json":
                raise ValueError("USER_RESULT must be an application/json FILE")
        if self.kind is ArtifactKind.USER_RESULT_ARCHIVE:
            if (
                self.resource_kind is not ResourceKind.FILE
                or self.content_type != "application/zip"
            ):
                raise ValueError("USER_RESULT_ARCHIVE must be an application/zip FILE")
        if self.kind is ArtifactKind.AUDIT_BUNDLE and (
            self.resource_kind is not ResourceKind.FILE
            or self.content_type != "application/zip"
        ):
            raise ValueError("AUDIT_BUNDLE must be an application/zip FILE")
        if self.kind is ArtifactKind.LOGPARSE_RUN:
            if self.resource_kind is not ResourceKind.DIRECTORY:
                raise ValueError("LOGPARSE_RUN must be a DIRECTORY")
            if self.content_type != "application/vnd.problem-locator.logparse-run+directory":
                raise ValueError("LOGPARSE_RUN has a fixed content type")
            if isinstance(self.metadata, LogparseRunMetadata) and self.metadata.tree_manifest_sha256 != self.sha256:
                raise ValueError("LOGPARSE_RUN tree manifest hash must equal artifact sha256")
        return self


class TreeManifestEntry(ContractModel):
    path: RelativePosixPath
    size: NonNegativeInt
    sha256: Sha256


class TreeManifest(ContractModel):
    version: Literal[1]
    entries: list[TreeManifestEntry]

    @model_validator(mode="after")
    def validate_entries(self) -> TreeManifest:
        paths = [entry.path for entry in self.entries]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("tree manifest entries must have unique, ascending paths")
        path_set = set(paths)
        if any(
            "/".join(path.split("/")[:index]) in path_set
            for path in paths
            for index in range(1, len(path.split("/")))
        ):
            raise ValueError("tree manifest file paths cannot be ancestors of other files")
        return self


class StagedResourceRef(ContractModel):
    staging_id: OpaqueId
    owner_job_id: OpaqueId
    proposal_key: NonEmptyText
    resource_kind: ResourceKind
    size: NonNegativeInt
    sha256: Sha256
    tree_manifest: TreeManifest | None

    @model_validator(mode="after")
    def validate_tree(self) -> StagedResourceRef:
        if (self.resource_kind is ResourceKind.DIRECTORY) != (self.tree_manifest is not None):
            raise ValueError("tree_manifest must be present exactly for DIRECTORY staged resources")
        if self.tree_manifest is not None:
            expected_size = sum(entry.size for entry in self.tree_manifest.entries)
            expected_hash = hashlib.sha256(_canonical_json_bytes(self.tree_manifest.model_dump(mode="json"))).hexdigest()
            if self.size != expected_size or self.sha256 != expected_hash:
                raise ValueError("directory staged resource size/hash must match TreeManifest")
        return self


class AttachmentStagedRef(ContractModel):
    attachment_id: OpaqueId
    resource_kind: Literal[ResourceKind.FILE]
    size: Annotated[int, Field(ge=0, le=MAX_ATTACHMENT_BYTES, strict=True)]
    sha256: Sha256

    @model_validator(mode="after")
    def validate_size(self) -> AttachmentStagedRef:
        if self.size > MAX_ATTACHMENT_BYTES:
            raise ValueError("staged attachment exceeds the V1 byte limit")
        return self


class PlannedResourceTarget(ContractModel):
    case_id: OpaqueId
    resource_type: ResourceType
    resource_id: OpaqueId
    resource_kind: ResourceKind
    size: NonNegativeInt
    sha256: Sha256
    final_storage_key: RelativePosixPath

    @model_validator(mode="after")
    def validate_identity_key(self) -> PlannedResourceTarget:
        if (
            self.resource_type is ResourceType.ATTACHMENT
            and self.resource_kind is not ResourceKind.FILE
        ):
            raise ValueError("Attachment targets must be FILE resources")
        collection = {
            ResourceType.ATTACHMENT: "attachments",
            ResourceType.EVIDENCE: "evidence",
            ResourceType.ARTIFACT: "artifacts",
        }[self.resource_type]
        suffix = "payload" if self.resource_kind is ResourceKind.FILE else "tree"
        expected_key = (
            f"resources/cases/{self.case_id}/{collection}/"
            f"{self.resource_id}/{suffix}"
        )
        if self.final_storage_key != expected_key:
            raise ValueError(
                "final_storage_key must be the deterministic target for its identity"
            )
        return self


class CaseResourceUsage(ContractModel):
    current_bytes: NonNegativeInt
    new_bytes: NonNegativeInt
    total_bytes: NonNegativeInt
    limit_bytes: Literal[MAX_CASE_RESOURCE_BYTES]

    @model_validator(mode="after")
    def validate_totals(self) -> CaseResourceUsage:
        if self.total_bytes != self.current_bytes + self.new_bytes:
            raise ValueError("total_bytes must equal current_bytes + new_bytes")
        return self


class MaterializedPath(ContractModel):
    path: NonEmptyText
    read_only: Literal[True]


class AssetAvailabilityReport(ContractModel):
    available: bool
    missing_refs: list[VersionedRef]

    @model_validator(mode="after")
    def validate_availability(self) -> AssetAvailabilityReport:
        if self.available == bool(self.missing_refs):
            raise ValueError("available must be true exactly when missing_refs is empty")
        return self


class ResolvedAsset(ContractModel):
    ref: VersionedRef
    asset_kind: AssetKind
    root_path: NonEmptyText


class RuntimeBindings(ContractModel):
    diagnosis_mode: DiagnosisMode | None
    generic_skill_name: SkillName | None
    agent_profile_ref: VersionedRef
    available_skill_refs: list[VersionedRef]
    skill_ref: VersionedRef | None
    tool_bundle_ref: VersionedRef
    context_policy_ref: VersionedRef
    output_contract_ref: VersionedRef
    logparse_tool_ref: VersionedRef | None
    logparse_product: NonEmptyText | None
    resource_limits: ResourceLimits

    @model_validator(mode="after")
    def validate_logparse_pair(self) -> RuntimeBindings:
        if (self.logparse_tool_ref is None) != (self.logparse_product is None):
            raise ValueError("logparse_tool_ref and logparse_product must be both null or both non-null")
        _unique(
            [ref.id + "@" + ref.version + "#" + ref.content_hash for ref in self.available_skill_refs],
            "available_skill_refs",
        )
        # Role-specific validation happens where the JobType is known.  The
        # pair itself is nevertheless exact and cannot describe an ambiguous
        # generic/specialized DIAGNOSE binding.
        if self.diagnosis_mode is DiagnosisMode.GENERIC:
            if self.generic_skill_name is None or self.skill_ref is not None:
                raise ValueError("GENERIC bindings require only generic_skill_name")
        elif self.generic_skill_name is not None:
            raise ValueError("generic_skill_name is valid only for GENERIC bindings")
        return self


class WorkspaceAttachmentInput(ContractModel):
    input_kind: Literal["ATTACHMENT"]
    resource_id: OpaqueId
    relative_path: RelativePosixPath
    resource_kind: Literal[ResourceKind.FILE]
    size: NonNegativeInt
    sha256: Sha256
    content_type: ContentType
    filename_suffix: AttachmentFilenameSuffix | None

    @model_validator(mode="after")
    def validate_materialized_path(self) -> WorkspaceAttachmentInput:
        _validate_attachment_suffix_for_content_type(
            self.content_type,
            self.filename_suffix,
        )
        expected = workspace_attachment_relative_path(
            self.resource_id,
            self.filename_suffix,
        )
        if self.relative_path != expected:
            raise ValueError(
                "Attachment input must use its fixed materialization path "
                "derived from filename_suffix"
            )
        return self


class WorkspaceEvidenceInput(ContractModel):
    input_kind: Literal["EVIDENCE"]
    resource_id: OpaqueId
    relative_path: RelativePosixPath | None
    resource_kind: ResourceKind | None
    size: NonNegativeInt | None
    sha256: Sha256 | None
    source_type: EvidenceSourceType
    source_ref: OpaqueId
    locator: EvidenceLocator
    summary: NonEmptyText
    content_hash: Sha256 | None

    @model_validator(mode="after")
    def validate_optional_resource(self) -> WorkspaceEvidenceInput:
        values = (self.relative_path, self.resource_kind, self.size, self.sha256)
        if any(value is None for value in values) and any(value is not None for value in values):
            raise ValueError("evidence materialization fields must be all null or all non-null")
        if self.locator.kind != self.source_type.value:
            raise ValueError("source_type must match locator.kind")
        if self.resource_kind is not None:
            leaf = "payload" if self.resource_kind is ResourceKind.FILE else "tree"
            expected = f"inputs/evidence/{self.resource_id}/{leaf}"
            if self.relative_path != expected:
                raise ValueError("Evidence input must use its fixed materialization path")
            if self.content_hash is not None and self.content_hash != self.sha256:
                raise ValueError(
                    "resource-backed Evidence content_hash must equal materialized sha256"
                )
        return self


class WorkspaceArtifactInput(ContractModel):
    input_kind: Literal["ARTIFACT"]
    resource_id: OpaqueId
    relative_path: RelativePosixPath
    resource_kind: ResourceKind
    size: NonNegativeInt
    sha256: Sha256
    artifact_kind: ArtifactKind
    name: NonEmptyText
    content_type: ContentType
    metadata: ArtifactMetadata

    @model_validator(mode="after")
    def validate_artifact(self) -> WorkspaceArtifactInput:
        leaf = "payload" if self.resource_kind is ResourceKind.FILE else "tree"
        expected = f"inputs/artifacts/{self.resource_id}/{leaf}"
        if self.relative_path != expected:
            raise ValueError("Artifact input must use its fixed materialization path")
        _validate_artifact_shape(
            self.artifact_kind,
            self.content_type,
            self.resource_kind,
            self.metadata,
            self.sha256,
        )
        return self


class WorkspacePreviousOutcomeInput(ContractModel):
    input_kind: Literal["PREVIOUS_OUTCOME"]
    resource_id: OpaqueId
    relative_path: RelativePosixPath
    resource_kind: Literal[ResourceKind.FILE]
    size: NonNegativeInt
    sha256: Sha256
    source_job_id: OpaqueId
    result_type: OutcomeResultType

    @model_validator(mode="after")
    def validate_materialized_path(self) -> WorkspacePreviousOutcomeInput:
        expected = f"inputs/outcomes/{self.resource_id}/job_outcome.json"
        if self.relative_path != expected:
            raise ValueError("previous Outcome must use its fixed materialization path")
        return self


WorkspaceInputEntry: TypeAlias = Annotated[
    WorkspaceAttachmentInput | WorkspaceEvidenceInput | WorkspaceArtifactInput | WorkspacePreviousOutcomeInput,
    Field(discriminator="input_kind"),
]


class ResolvedLogparseAnchor(ContractModel):
    label: NonEmptyText
    module: NonEmptyText
    slot: NonEmptyText
    process_name: NonEmptyText
    pid: NonEmptyText | None

    @model_validator(mode="after")
    def validate_canonical_ascii(self) -> ResolvedLogparseAnchor:
        for value in (self.label, self.module, self.slot, self.process_name, self.pid):
            if value is not None and (
                not value.isascii()
                or value != value.strip()
                or any(character in value for character in "\r\n\x00")
            ):
                raise ValueError(
                    "resolved logparse anchors require canonical single-line ASCII"
                )
        return self


class ResolvedLogparsePlanInput(ContractModel):
    schema_version: Literal[2]
    attachment_id: OpaqueId | None
    artifact_id: OpaqueId | None
    problem_time: UtcTimestamp
    anchors: Annotated[list[ResolvedLogparseAnchor], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def validate_plan(self) -> ResolvedLogparsePlanInput:
        if (self.attachment_id is None) == (self.artifact_id is None):
            raise ValueError(
                "resolved logparse plan requires exactly one attachment or artifact"
            )
        _unique(
            [
                (item.label, item.module, item.slot, item.process_name, item.pid)
                for item in self.anchors
            ],
            "resolved logparse anchors",
        )
        return self


class ReviewCausalAssertion(ContractModel):
    rule_id: NonEmptyText
    statement: NonEmptyText


class MechanicalFact(ContractModel):
    fact_id: NonEmptyText
    name: ContractName
    value: str | int | bool | None
    source_rule_id: NonEmptyText
    evidence_refs: list[OpaqueId]

    @model_validator(mode="after")
    def validate_fact(self) -> MechanicalFact:
        _unique(self.evidence_refs, "MechanicalFact.evidence_refs")
        if isinstance(self.value, str):
            _utf8_nonblank(self.value)
        return self


class ReviewSubjectV2(ContractModel):
    schema_version: Literal[2]
    review_job_id: OpaqueId
    case_id: OpaqueId
    reviewed_state_revision: PositiveInt
    skill_ref: VersionedRef
    candidate: CandidateConclusion
    causal_assertions: list[ReviewCausalAssertion]
    required_rule_ids: Annotated[list[NonEmptyText], Field(min_length=1)]
    required_evidence_refs: Annotated[list[OpaqueId], Field(min_length=1)]
    mechanical_facts: list[MechanicalFact]
    subject_hash: Sha256

    @model_validator(mode="after")
    def validate_subject(self) -> ReviewSubjectV2:
        if self.candidate.status is not CandidateStatus.REVIEWING:
            raise ValueError("ReviewSubjectV2 requires a REVIEWING candidate")
        _unique(self.required_rule_ids, "ReviewSubjectV2.required_rule_ids")
        _unique(
            self.required_evidence_refs,
            "ReviewSubjectV2.required_evidence_refs",
        )
        assertion_ids = [item.rule_id for item in self.causal_assertions]
        _unique(assertion_ids, "ReviewSubjectV2.causal_assertions.rule_id")
        required_positions = {
            rule_id: index for index, rule_id in enumerate(self.required_rule_ids)
        }
        if any(rule_id not in required_positions for rule_id in assertion_ids) or [
            required_positions[rule_id] for rule_id in assertion_ids
        ] != sorted(required_positions[rule_id] for rule_id in assertion_ids):
            raise ValueError(
                "causal assertions must be an ordered subset of required_rule_ids"
            )
        _unique(
            [item.fact_id for item in self.mechanical_facts],
            "ReviewSubjectV2.mechanical_facts.fact_id",
        )
        evidence_set = set(self.required_evidence_refs)
        if any(
            item.source_rule_id not in required_positions
            or any(ref not in evidence_set for ref in item.evidence_refs)
            for item in self.mechanical_facts
        ):
            raise ValueError(
                "mechanical facts must bind required rules and Evidence"
            )
        candidate_evidence = set(review_required_evidence_refs(self.candidate))
        if candidate_evidence != evidence_set:
            raise ValueError(
                "ReviewSubjectV2 Evidence must exactly cover the Candidate required union"
            )
        preimage = self.model_dump(mode="json", exclude={"subject_hash"})
        expected = hashlib.sha256(_canonical_json_bytes(preimage)).hexdigest()
        if self.subject_hash != expected:
            raise ValueError("ReviewSubjectV2 subject_hash does not match its content")
        return self


class WorkspaceInputManifest(ContractModel):
    schema_version: Literal[2]
    job_id: OpaqueId
    case_id: OpaqueId
    job_type: JobType
    logparse_tool_ref: VersionedRef | None
    logparse_product: NonEmptyText | None
    entries: list[WorkspaceInputEntry]
    resolved_logparse_plan: ResolvedLogparsePlanInput | None = None
    review_subject: ReviewSubjectV2 | None = None

    @model_validator(mode="after")
    def validate_manifest(self) -> WorkspaceInputManifest:
        if (self.logparse_tool_ref is None) != (self.logparse_product is None):
            raise ValueError("logparse_tool_ref and logparse_product must be both null or both non-null")
        if self.logparse_tool_ref is not None and self.job_type is not JobType.DIAGNOSE:
            raise ValueError("only DIAGNOSE manifests may carry logparse bindings")
        if (
            self.resolved_logparse_plan is not None
            and self.logparse_tool_ref is None
        ):
            raise ValueError(
                "resolved_logparse_plan requires logparse bindings"
            )
        if (self.job_type is JobType.REVIEW) != (self.review_subject is not None):
            raise ValueError("REVIEW manifests require exactly one review_subject")
        if self.review_subject is not None and (
            self.review_subject.review_job_id != self.job_id
            or self.review_subject.case_id != self.case_id
        ):
            raise ValueError("review_subject identity must match the Workspace manifest")
        order = {"ATTACHMENT": 0, "EVIDENCE": 1, "ARTIFACT": 2, "PREVIOUS_OUTCOME": 3}
        kinds = [order[entry.input_kind] for entry in self.entries]
        if kinds != sorted(kinds):
            raise ValueError("workspace entries must use the frozen group order")
        seen: set[tuple[str, str]] = set()
        for entry in self.entries:
            key = (entry.input_kind, entry.resource_id)
            if key in seen:
                raise ValueError("workspace entries cannot repeat a resource within an input kind")
            seen.add(key)
        return self


def validate_workspace_manifest_for_job(
    manifest: WorkspaceInputManifest,
    job: Job,
) -> WorkspaceInputManifest:
    """Validate the cross-object Workspace seam that JSON Schema cannot express."""

    for field_name in (
        "job_id",
        "case_id",
        "job_type",
        "logparse_tool_ref",
        "logparse_product",
    ):
        if getattr(manifest, field_name) != getattr(job, field_name):
            raise ValueError(f"Workspace manifest {field_name} must match its Job")
    expected_by_kind = {
        "ATTACHMENT": job.attachment_refs,
        "EVIDENCE": job.evidence_refs,
        "ARTIFACT": job.artifact_refs,
        # REVIEW keeps the diagnosis Outcome private to the service so the
        # independent Reviewer cannot inspect the Specialist verdict.
        "PREVIOUS_OUTCOME": (
            [] if job.job_type is JobType.REVIEW else job.previous_outcome_refs
        ),
    }
    actual_by_kind = {
        kind: [entry.resource_id for entry in manifest.entries if entry.input_kind == kind]
        for kind in expected_by_kind
    }
    for kind, expected in expected_by_kind.items():
        if actual_by_kind[kind] != expected:
            raise ValueError(f"Workspace manifest {kind} order must match its Job")
    if manifest.review_subject is not None:
        subject = manifest.review_subject
        target = job.review_target
        if target is None or (
            subject.reviewed_state_revision != job.base_state_revision
            or subject.skill_ref != job.skill_ref
            or subject.candidate.conclusion_id != target.candidate_conclusion_id
            or subject.candidate.revision != target.candidate_revision
            or subject.candidate.content_hash != target.candidate_content_hash
        ):
            raise ValueError("Workspace review_subject must match its REVIEW Job")
        candidate_required = set(review_required_evidence_refs(subject.candidate))
        if set(subject.required_evidence_refs) != candidate_required:
            raise ValueError(
                "Workspace review_subject Evidence must equal the Candidate required union"
            )
        if subject.required_evidence_refs != [
            ref for ref in job.evidence_refs if ref in candidate_required
        ]:
            raise ValueError(
                "Workspace review_subject Evidence must be a stable Job Evidence subsequence"
            )
    if manifest.resolved_logparse_plan is not None:
        plan = manifest.resolved_logparse_plan
        if plan.attachment_id is not None and plan.attachment_id not in job.attachment_refs:
            raise ValueError("resolved logparse attachment must belong to its Job")
        if plan.artifact_id is not None and plan.artifact_id not in job.artifact_refs:
            raise ValueError("resolved logparse artifact must belong to its Job")
    return manifest


class LogparseParseClaim(ContractModel):
    schema_version: Literal[1]
    job_id: OpaqueId
    attachment_id: OpaqueId
    attachment_sha256: Sha256
    artifact_proposal_key: NonEmptyText
    logparse_tool_ref: VersionedRef
    request_sha256: Sha256


class ApplicationErrorDetail(ContractModel):
    field: NonEmptyText | None
    resource_type: NonEmptyText | None
    resource_id: OpaqueId | None
    resource_ref: VersionedRef | None
    expected: str | int | bool | None
    actual: str | int | bool | None
    limit: NonNegativeInt | None
    observed: NonNegativeInt | None


class ApplicationError(ContractModel):
    code: ErrorCode
    message: NonEmptyText
    details: list[ApplicationErrorDetail]
    retryable: bool

    @model_validator(mode="after")
    def validate_retryability(self) -> ApplicationError:
        from .errors import APPLICATION_ERROR_RETRYABLE_CODES

        if self.code in APPLICATION_ERROR_RETRYABLE_CODES and not self.retryable:
            raise ValueError("this ApplicationError code is always retryable")
        if self.code not in APPLICATION_ERROR_RETRYABLE_CODES and self.retryable:
            raise ValueError("this ApplicationError code is not retryable")
        return self


UNTRUSTED_OUTCOME_REJECTION_CODES = frozenset(
    {
        ErrorCode.OUTCOME_MISSING,
        ErrorCode.EXECUTION_RECORD_FAILED,
        ErrorCode.OUTCOME_INVALID,
    }
)
OUTCOME_REJECTION_CODES = frozenset(
    {
        *UNTRUSTED_OUTCOME_REJECTION_CODES,
        ErrorCode.RESOURCE_LIMIT_EXCEEDED,
        ErrorCode.RESOURCE_HASH_MISMATCH,
    }
)


class ExecutionFailure(ContractModel):
    stage: ExecutionStage
    code: ErrorCode
    message: NonEmptyText
    retryable: bool
    details: list[ApplicationErrorDetail]

    @model_validator(mode="after")
    def validate_retryability(self) -> ExecutionFailure:
        from .errors import EXECUTION_FAILURE_RETRYABLE_CODES

        if self.retryable and self.code not in EXECUTION_FAILURE_RETRYABLE_CODES:
            raise ValueError("this ExecutionFailure code cannot be retryable")
        return self


class EvidenceBinding(ContractModel):
    existing_evidence_id: OpaqueId | None
    evidence_proposal_key: NonEmptyText | None

    @model_validator(mode="after")
    def validate_xor(self) -> EvidenceBinding:
        if (self.existing_evidence_id is None) == (self.evidence_proposal_key is None):
            raise ValueError("exactly one evidence binding branch must be populated")
        return self


class EvidenceSourceBinding(ContractModel):
    existing_source_ref: OpaqueId | None
    artifact_proposal_key: NonEmptyText | None

    @model_validator(mode="after")
    def validate_xor(self) -> EvidenceSourceBinding:
        if (self.existing_source_ref is None) == (self.artifact_proposal_key is None):
            raise ValueError("exactly one evidence source binding branch must be populated")
        return self


class CompletionCriterionDraftMapping(ContractModel):
    criterion_index: NonNegativeInt
    criterion: NonEmptyText
    status: CompletionCriterionStatus
    evidence_bindings: list[EvidenceBinding]
    explanation: NonEmptyText

    @model_validator(mode="after")
    def validate_mapping(self) -> CompletionCriterionDraftMapping:
        if self.status in {
            CompletionCriterionStatus.SATISFIED,
            CompletionCriterionStatus.PARTIALLY_SATISFIED,
        } and not self.evidence_bindings:
            raise ValueError(
                "a satisfied or partially satisfied criterion requires an evidence binding"
            )
        keys = [
            binding.existing_evidence_id or f"proposal:{binding.evidence_proposal_key}"
            for binding in self.evidence_bindings
        ]
        _unique(keys, "evidence_bindings")
        return self


class CausalFactorDraft(ContractModel):
    factor_id: ContractName
    role: CausalFactorRole
    statement: NonEmptyText
    evidence_bindings: Annotated[list[EvidenceBinding], Field(min_length=1)]
    required_rule_ids: Annotated[list[NonEmptyText], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_factor(self) -> CausalFactorDraft:
        _unique(
            [
                binding.existing_evidence_id
                or f"proposal:{binding.evidence_proposal_key}"
                for binding in self.evidence_bindings
            ],
            "CausalFactorDraft.evidence_bindings",
        )
        _unique(self.required_rule_ids, "CausalFactorDraft.required_rule_ids")
        return self


class CandidateConclusionDraft(ContractModel):
    proposal_key: NonEmptyText
    existing_conclusion_id: OpaqueId | None
    resolution_status: DiagnosisResolutionStatus
    terminal_path_id: ContractName
    statement: NonEmptyText
    causal_factors: list[CausalFactorDraft]
    candidate_factors: list[CausalFactorDraft]
    excluded_factors: list[CausalFactorDraft]
    supporting_evidence_bindings: list[EvidenceBinding]
    completion_criteria_mapping: Annotated[
        list[CompletionCriterionDraftMapping], Field(min_length=1)
    ]

    @model_validator(mode="after")
    def validate_candidate(self) -> CandidateConclusionDraft:
        keys = [
            binding.existing_evidence_id or f"proposal:{binding.evidence_proposal_key}"
            for binding in self.supporting_evidence_bindings
        ]
        _unique(keys, "supporting_evidence_bindings")
        indices = [entry.criterion_index for entry in self.completion_criteria_mapping]
        if indices != list(range(len(indices))):
            raise ValueError("candidate criterion mappings must be contiguous and sorted from index zero")
        factors = [*self.causal_factors, *self.candidate_factors, *self.excluded_factors]
        _unique([item.factor_id for item in factors], "Candidate draft factor_id")
        if self.resolution_status is DiagnosisResolutionStatus.COMPLETE:
            if not self.causal_factors:
                raise ValueError("a COMPLETE Candidate draft requires a causal factor")
            if self.candidate_factors:
                raise ValueError(
                    "a COMPLETE Candidate draft cannot retain candidate factors"
                )
            if any(
                entry.status is not CompletionCriterionStatus.SATISFIED
                for entry in self.completion_criteria_mapping
            ):
                raise ValueError(
                    "a COMPLETE Candidate draft requires every criterion satisfied"
                )
        else:
            if not (self.causal_factors or self.excluded_factors):
                raise ValueError(
                    "a PARTIAL Candidate draft requires a confirmed or excluded factor"
                )
            if not any(
                entry.status
                in {
                    CompletionCriterionStatus.SATISFIED,
                    CompletionCriterionStatus.PARTIALLY_SATISFIED,
                }
                for entry in self.completion_criteria_mapping
            ):
                raise ValueError(
                    "a PARTIAL Candidate draft requires evidence-backed criterion progress"
                )
            if all(
                entry.status is CompletionCriterionStatus.SATISFIED
                for entry in self.completion_criteria_mapping
            ):
                raise ValueError(
                    "a PARTIAL Candidate draft requires an explicitly incomplete criterion"
                )
        return self


class UserResultCitationV2(ContractModel):
    evidence_binding: EvidenceBinding
    archive_name: Annotated[str, AfterValidator(_validate_attachment_filename)] | None
    line_start: PositiveInt | None
    line_end: PositiveInt | None
    raw_bytes_sha256: Sha256 | None
    excerpt: NonEmptyText | None

    @model_validator(mode="after")
    def validate_location(self) -> UserResultCitationV2:
        location = (
            self.archive_name,
            self.line_start,
            self.line_end,
            self.raw_bytes_sha256,
            self.excerpt,
        )
        if not (all(value is None for value in location) or all(value is not None for value in location)):
            raise ValueError(
                "citation location fields must be either all null or all non-null"
            )
        if self.line_start is not None:
            assert self.line_end is not None
            if self.line_start > self.line_end:
                raise ValueError("citation line_start must not exceed line_end")
        return self


class UserResultFindingV2(ContractModel):
    statement: NonEmptyText
    confidence: Confidence
    evidence_bindings: list[EvidenceBinding]
    citations: list[UserResultCitationV2]

    @model_validator(mode="after")
    def validate_citations(self) -> UserResultFindingV2:
        binding_keys = [
            item.existing_evidence_id
            or f"proposal:{item.evidence_proposal_key}"
            for item in self.evidence_bindings
        ]
        _unique(binding_keys, "UserResultFindingV2.evidence_bindings")
        _unique(
            [
                (
                    item.evidence_binding.existing_evidence_id,
                    item.evidence_binding.evidence_proposal_key,
                    item.archive_name,
                    item.line_start,
                    item.line_end,
                    item.raw_bytes_sha256,
                )
                for item in self.citations
            ],
            "UserResultFindingV2.citations",
        )
        citation_binding_keys: list[str] = []
        for citation in self.citations:
            key = (
                citation.evidence_binding.existing_evidence_id
                or f"proposal:{citation.evidence_binding.evidence_proposal_key}"
            )
            if key not in citation_binding_keys:
                citation_binding_keys.append(key)
        if citation_binding_keys != binding_keys:
            raise ValueError(
                "finding citations must cover evidence_bindings in their declared order"
            )
        return self


class UserResultVerificationRuleV2(ContractModel):
    rule_id: NonEmptyText
    rule_kind: NonEmptyText
    status: ServerRuleStatus
    explanation: NonEmptyText
    evidence_bindings: list[EvidenceBinding]
    citations: list[UserResultCitationV2]
    observed_times: list[UtcTimestamp]
    event_observations: list[EventObservationAudit]
    derived_values: list[DerivedValueAudit]
    issues: list[NonEmptyText]

    @model_validator(mode="after")
    def validate_rule(self) -> UserResultVerificationRuleV2:
        _unique(self.observed_times, "UserResultVerificationRuleV2.observed_times")
        _unique(
            [item.event_id for item in self.event_observations],
            "UserResultVerificationRuleV2.event_observations.event_id",
        )
        _unique(
            [item.name for item in self.derived_values],
            "UserResultVerificationRuleV2.derived_values.name",
        )
        _unique(self.issues, "UserResultVerificationRuleV2.issues")
        _unique(
            [
                (
                    item.evidence_binding.existing_evidence_id,
                    item.evidence_binding.evidence_proposal_key,
                    item.archive_name,
                    item.line_start,
                    item.line_end,
                    item.raw_bytes_sha256,
                )
                for item in self.citations
            ],
            "UserResultVerificationRuleV2.citations",
        )
        binding_keys = [
            item.existing_evidence_id
            or f"proposal:{item.evidence_proposal_key}"
            for item in self.evidence_bindings
        ]
        _unique(binding_keys, "UserResultVerificationRuleV2.evidence_bindings")
        citation_binding_keys: list[str] = []
        for citation in self.citations:
            key = (
                citation.evidence_binding.existing_evidence_id
                or f"proposal:{citation.evidence_binding.evidence_proposal_key}"
            )
            if key not in citation_binding_keys:
                citation_binding_keys.append(key)
        if citation_binding_keys != binding_keys:
            raise ValueError(
                "verification rule citations must cover evidence_bindings in their declared order"
            )
        if self.status in {
            ServerRuleStatus.VERIFIED_FAIL,
            ServerRuleStatus.UNVERIFIABLE,
        } and not self.issues:
            raise ValueError("failed or unverifiable verification rules require issues")
        if self.status is ServerRuleStatus.VERIFIED_PASS and self.issues:
            raise ValueError("VERIFIED_PASS verification rules forbid issues")
        is_semantic_rule = self.rule_kind == "SEMANTIC_CAUSALITY"
        is_semantic_status = self.status is ServerRuleStatus.SEMANTIC_ONLY
        if is_semantic_rule != is_semantic_status:
            raise ValueError(
                "SEMANTIC_CAUSALITY rules and SEMANTIC_ONLY status must correspond"
            )
        return self


class UserResultTimeObservationV2(ContractModel):
    rule_id: NonEmptyText
    event_time: UtcTimestamp
    offset_ms: Annotated[int, Field(strict=True)]


class UserResultTimeRelevanceV2(ContractModel):
    assessment: Literal["RELEVANT", "NOT_RELEVANT", "UNKNOWN"]
    problem_time: UtcTimestamp | None
    derived_anchor_time: UtcTimestamp | None
    observations: list[UserResultTimeObservationV2]
    explanation: NonEmptyText
    citations: list[UserResultCitationV2]

    @model_validator(mode="after")
    def validate_times(self) -> UserResultTimeRelevanceV2:
        _unique(
            [
                (item.rule_id, item.event_time, item.offset_ms)
                for item in self.observations
            ],
            "UserResultTimeRelevanceV2.observations",
        )
        if self.problem_time is None and self.observations:
            raise ValueError("time observations require problem_time")
        if self.problem_time is not None:
            problem_time = datetime.strptime(
                self.problem_time, "%Y-%m-%dT%H:%M:%S.%fZ"
            )
            for observation in self.observations:
                event_time = datetime.strptime(
                    observation.event_time, "%Y-%m-%dT%H:%M:%S.%fZ"
                )
                expected_offset = int(
                    (event_time - problem_time).total_seconds() * 1_000
                )
                if observation.offset_ms != expected_offset:
                    raise ValueError(
                        "time observation offset_ms must be relative to problem_time"
                    )
        _unique(
            [
                (
                    item.evidence_binding.existing_evidence_id,
                    item.evidence_binding.evidence_proposal_key,
                    item.archive_name,
                    item.line_start,
                    item.line_end,
                    item.raw_bytes_sha256,
                )
                for item in self.citations
            ],
            "UserResultTimeRelevanceV2.citations",
        )
        return self


class UserResultFactorV3(ContractModel):
    factor_id: ContractName
    role: CausalFactorRole
    statement: NonEmptyText
    evidence_bindings: Annotated[list[EvidenceBinding], Field(min_length=1)]
    required_rule_ids: Annotated[list[NonEmptyText], Field(min_length=1)]
    citations: list[UserResultCitationV2]

    @model_validator(mode="after")
    def validate_factor(self) -> UserResultFactorV3:
        binding_keys = [
            item.existing_evidence_id
            or f"proposal:{item.evidence_proposal_key}"
            for item in self.evidence_bindings
        ]
        _unique(binding_keys, "UserResultFactorV3.evidence_bindings")
        _unique(self.required_rule_ids, "UserResultFactorV3.required_rule_ids")
        citation_keys: list[str] = []
        for citation in self.citations:
            key = (
                citation.evidence_binding.existing_evidence_id
                or f"proposal:{citation.evidence_binding.evidence_proposal_key}"
            )
            if key not in citation_keys:
                citation_keys.append(key)
        if citation_keys != binding_keys:
            raise ValueError(
                "factor citations must cover evidence_bindings in declared order"
            )
        return self


class UserResultPayloadV3(ContractModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "status": {"enum": ["PARTIAL", "INCONCLUSIVE"]}
                        },
                        "required": ["status"],
                    },
                    "then": {
                        "properties": {"evidence_gaps": {"minItems": 1}},
                        "required": ["evidence_gaps"],
                    },
                }
            ]
        }
    )
    schema_version: Literal[3]
    format_id: Literal["problem-locator-diagnosis-v3"]
    status: Literal["COMPLETED", "PARTIAL", "INCONCLUSIVE"]
    source_job_type: JobType
    problem_statement: NonEmptyText
    root_cause: NonEmptyText | None
    findings: list[UserResultFindingV2]
    causal_factors: list[UserResultFactorV3]
    candidate_factors: list[UserResultFactorV3]
    excluded_factors: list[UserResultFactorV3]
    supporting_evidence_bindings: list[EvidenceBinding]
    completion_criteria_mapping: Annotated[
        list[CompletionCriterionDraftMapping], Field(min_length=1)
    ]
    verification_rules: list[UserResultVerificationRuleV2]
    time_relevance: UserResultTimeRelevanceV2
    evidence_gaps: list[NonEmptyText]
    limitations: list[NonEmptyText]
    recommendations: Annotated[list[NonEmptyText], Field(min_length=1)]
    safety_notes: Annotated[list[NonEmptyText], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_payload(self) -> UserResultPayloadV3:
        if self.source_job_type is JobType.ROUTE:
            raise ValueError("USER_RESULT source_job_type must be DIAGNOSE or REVIEW")
        keys = [
            binding.existing_evidence_id or f"proposal:{binding.evidence_proposal_key}"
            for binding in self.supporting_evidence_bindings
        ]
        _unique(keys, "supporting_evidence_bindings")
        indices = [entry.criterion_index for entry in self.completion_criteria_mapping]
        if indices != list(range(len(indices))):
            raise ValueError("user-result criterion mappings must be contiguous and sorted from index zero")
        _unique(
            [entry.rule_id for entry in self.verification_rules],
            "UserResultPayloadV3.verification_rules.rule_id",
        )
        factors = [*self.causal_factors, *self.candidate_factors, *self.excluded_factors]
        _unique([item.factor_id for item in factors], "UserResultPayloadV3.factor_id")
        for field_name in (
            "evidence_gaps",
            "limitations",
            "recommendations",
            "safety_notes",
        ):
            _unique(getattr(self, field_name), f"UserResultPayloadV3.{field_name}")
        if self.status == "COMPLETED":
            if self.root_cause is None:
                raise ValueError("COMPLETED USER_RESULT requires root_cause")
            if any(
                not finding.evidence_bindings or not finding.citations
                for finding in self.findings
            ):
                raise ValueError(
                    "every COMPLETED finding must have Evidence bindings and citations"
                )
            if any(
                entry.status is not CompletionCriterionStatus.SATISFIED
                or not entry.evidence_bindings
                for entry in self.completion_criteria_mapping
            ):
                raise ValueError(
                    "COMPLETED USER_RESULT requires every criterion to be satisfied and evidence-backed"
                )
            if not self.causal_factors:
                raise ValueError("COMPLETED USER_RESULT requires a causal factor")
            if self.candidate_factors:
                raise ValueError(
                    "COMPLETED USER_RESULT cannot retain candidate factors"
                )
        elif self.status == "PARTIAL":
            if not self.evidence_gaps:
                raise ValueError("PARTIAL USER_RESULT requires an evidence gap")
            if not (self.causal_factors or self.excluded_factors):
                raise ValueError(
                    "PARTIAL USER_RESULT requires a confirmed or excluded factor"
                )
            if not any(
                entry.status
                in {
                    CompletionCriterionStatus.SATISFIED,
                    CompletionCriterionStatus.PARTIALLY_SATISFIED,
                }
                for entry in self.completion_criteria_mapping
            ):
                raise ValueError(
                    "PARTIAL USER_RESULT requires evidence-backed criterion progress"
                )
            if all(
                entry.status is CompletionCriterionStatus.SATISFIED
                for entry in self.completion_criteria_mapping
            ):
                raise ValueError(
                    "PARTIAL USER_RESULT requires an explicitly incomplete criterion"
                )
        else:
            if self.root_cause is not None:
                raise ValueError("INCONCLUSIVE USER_RESULT requires root_cause=null")
            if not self.evidence_gaps:
                raise ValueError(
                    "INCONCLUSIVE USER_RESULT requires at least one evidence gap"
                )
            if self.findings or factors:
                raise ValueError(
                    "INCONCLUSIVE USER_RESULT cannot publish unreviewed factors or findings"
                )
        return self


UserResultPayload = UserResultPayloadV3


class Finding(ContractModel):
    statement: NonEmptyText
    evidence_bindings: list[EvidenceBinding]
    confidence: Confidence


class DiagnosisItemDraft(ContractModel):
    item_id: OpaqueId
    statement: NonEmptyText
    provenance: DiagnosisProvenance
    evidence_bindings: list[EvidenceBinding]
    supersedes: list[OpaqueId]

    @model_validator(mode="after")
    def validate_draft(self) -> DiagnosisItemDraft:
        _unique(self.supersedes, "supersedes")
        if self.item_id in self.supersedes:
            raise ValueError("an item draft cannot supersede itself")
        return self


class DiagnosisItemChange(ContractModel):
    item_id: OpaqueId
    statement: NonEmptyText | None
    reason: NonEmptyText
    evidence_bindings: list[EvidenceBinding]


class RequirementFulfillment(ContractModel):
    requirement_id: OpaqueId
    fulfilled_by_refs: Annotated[list[OpaqueId], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_refs(self) -> RequirementFulfillment:
        if not self.fulfilled_by_refs:
            raise ValueError("requirement fulfillment requires at least one reference")
        _unique(self.fulfilled_by_refs, "fulfilled_by_refs")
        return self


class DiagnosisStateDelta(ContractModel):
    problem_spec_patch: ProblemSpecPatch | None
    add_user_facts: list[DiagnosisItem]
    proposed_facts: list[DiagnosisItemDraft]
    add_active_hypotheses: list[DiagnosisItemDraft]
    update_hypotheses: list[DiagnosisItemChange]
    reject_hypotheses: list[DiagnosisItemChange]
    add_open_questions: list[DiagnosisItemDraft]
    resolve_questions: list[DiagnosisItemChange]
    add_pending_requirements: list[PendingRequirement]
    fulfill_requirements: list[RequirementFulfillment]
    add_evidence_bindings: list[EvidenceBinding]

    @model_validator(mode="after")
    def validate_delta(self) -> DiagnosisStateDelta:
        new_items = self.proposed_facts + self.add_active_hypotheses + self.add_open_questions
        _unique([item.item_id for item in new_items], "new DiagnosisItemDraft.item_id")
        requirements = self.add_pending_requirements
        _unique([item.requirement_id for item in requirements], "new requirement_id")
        _unique([item.name for item in requirements], "new requirement name")
        for item in self.reject_hypotheses + self.resolve_questions:
            if item.statement is not None:
                raise ValueError("reject_hypotheses and resolve_questions changes must have statement=null")
        return self


class AgentEvidenceProposalDraft(ContractModel):
    proposal_key: NonEmptyText
    source_type: EvidenceSourceType
    source_binding: EvidenceSourceBinding
    locator: EvidenceLocator
    summary: NonEmptyText
    workspace_relative_path: RelativePosixPath | None
    declared_size: NonNegativeInt | None
    declared_sha256: Sha256 | None

    @model_validator(mode="after")
    def validate_draft(self) -> AgentEvidenceProposalDraft:
        if self.locator.kind != self.source_type.value:
            raise ValueError("source_type must match locator.kind")
        if self.source_binding.artifact_proposal_key is not None and self.source_type is not EvidenceSourceType.LOGPARSE:
            raise ValueError("only LOGPARSE evidence may bind an artifact proposal")
        if self.workspace_relative_path is None and (self.declared_size is not None or self.declared_sha256 is not None):
            raise ValueError("declared resource fields require workspace_relative_path")
        if self.workspace_relative_path is not None and not self.workspace_relative_path.startswith(
            f"output/proposals/{self.proposal_key}/"
        ):
            raise ValueError("proposal workspace paths must be rooted below output/proposals/<proposal_key>/")
        return self


class AgentArtifactProposalDraft(ContractModel):
    proposal_key: NonEmptyText
    artifact_kind: ArtifactKind
    name: NonEmptyText
    content_type: ContentType
    resource_kind: ResourceKind
    workspace_relative_path: RelativePosixPath
    declared_size: NonNegativeInt | None
    declared_sha256: Sha256 | None
    metadata: ArtifactMetadata

    @model_validator(mode="after")
    def validate_draft(self) -> AgentArtifactProposalDraft:
        if self.artifact_kind is ArtifactKind.AUDIT_BUNDLE:
            raise ValueError("AUDIT_BUNDLE is server-generated and cannot be proposed")
        if not self.workspace_relative_path.startswith(f"output/proposals/{self.proposal_key}/"):
            raise ValueError("proposal workspace paths must be rooted below output/proposals/<proposal_key>/")
        _validate_artifact_shape(
            self.artifact_kind,
            self.content_type,
            self.resource_kind,
            self.metadata,
            None,
        )
        return self


class EvidenceProposal(ContractModel):
    proposal_key: NonEmptyText
    source_type: EvidenceSourceType
    source_binding: EvidenceSourceBinding
    locator: EvidenceLocator
    summary: NonEmptyText
    content_hash: Sha256 | None
    staged_resource_ref: StagedResourceRef | None

    @model_validator(mode="after")
    def validate_proposal(self) -> EvidenceProposal:
        if self.locator.kind != self.source_type.value:
            raise ValueError("source_type must match locator.kind")
        if self.source_binding.artifact_proposal_key is not None and self.source_type is not EvidenceSourceType.LOGPARSE:
            raise ValueError("only LOGPARSE evidence may bind an artifact proposal")
        if self.staged_resource_ref is not None:
            if self.staged_resource_ref.proposal_key != self.proposal_key:
                raise ValueError("staged resource proposal_key mismatch")
            if self.content_hash is not None and self.content_hash != self.staged_resource_ref.sha256:
                raise ValueError("content_hash must match staged resource sha256")
        return self


class ArtifactProposal(ContractModel):
    proposal_key: NonEmptyText
    artifact_kind: ArtifactKind
    name: NonEmptyText
    content_type: ContentType
    resource_kind: ResourceKind
    size: NonNegativeInt
    sha256: Sha256
    staged_resource_ref: StagedResourceRef
    metadata: ArtifactMetadata

    @model_validator(mode="after")
    def validate_proposal(self) -> ArtifactProposal:
        if self.artifact_kind is ArtifactKind.AUDIT_BUNDLE:
            raise ValueError("AUDIT_BUNDLE is server-generated and cannot be proposed")
        if self.staged_resource_ref.proposal_key != self.proposal_key:
            raise ValueError("staged resource proposal_key mismatch")
        if (
            self.staged_resource_ref.resource_kind != self.resource_kind
            or self.staged_resource_ref.size != self.size
            or self.staged_resource_ref.sha256 != self.sha256
        ):
            raise ValueError("artifact fields must match staged_resource_ref")
        _validate_artifact_shape(
            self.artifact_kind,
            self.content_type,
            self.resource_kind,
            self.metadata,
            self.sha256,
            self.staged_resource_ref.tree_manifest,
        )
        return self


def _validate_artifact_shape(
    kind: ArtifactKind,
    content_type: str,
    resource_kind: ResourceKind,
    metadata: ArtifactMetadata,
    sha256: str | None,
    tree_manifest: TreeManifest | None = None,
) -> None:
    expected_type = {
        ArtifactKind.USER_RESULT: UserResultMetadataV3,
        ArtifactKind.USER_RESULT_ARCHIVE: UserResultArchiveMetadataV3,
        ArtifactKind.DIAGNOSTIC_EXPORT: DiagnosticExportMetadata,
        ArtifactKind.LOGPARSE_RUN: LogparseRunMetadata,
        ArtifactKind.AUDIT_BUNDLE: AuditBundleMetadata,
    }[kind]
    if not isinstance(metadata, expected_type):
        raise ValueError("artifact kind and metadata type do not match")
    if kind is ArtifactKind.USER_RESULT and (resource_kind is not ResourceKind.FILE or content_type != "application/json"):
        raise ValueError("USER_RESULT must be an application/json FILE")
    if kind is ArtifactKind.USER_RESULT_ARCHIVE and (
        resource_kind is not ResourceKind.FILE or content_type != "application/zip"
    ):
        raise ValueError("USER_RESULT_ARCHIVE must be an application/zip FILE")
    if kind is ArtifactKind.AUDIT_BUNDLE and (
        resource_kind is not ResourceKind.FILE or content_type != "application/zip"
    ):
        raise ValueError("AUDIT_BUNDLE must be an application/zip FILE")
    if kind is ArtifactKind.LOGPARSE_RUN:
        if resource_kind is not ResourceKind.DIRECTORY:
            raise ValueError("LOGPARSE_RUN must be a DIRECTORY")
        if content_type != "application/vnd.problem-locator.logparse-run+directory":
            raise ValueError("LOGPARSE_RUN has a fixed content type")
        if sha256 is not None and isinstance(metadata, LogparseRunMetadata) and metadata.tree_manifest_sha256 != sha256:
            raise ValueError("LOGPARSE_RUN metadata tree hash must equal proposal sha256")
        if tree_manifest is not None and isinstance(metadata, LogparseRunMetadata):
            if metadata.parse_manifest_relative_path not in {
                entry.path for entry in tree_manifest.entries
            }:
                raise ValueError(
                    "LOGPARSE_RUN parse_manifest_relative_path must name a manifest file"
                )


class RouteDecision(ContractModel):
    kind: RouteKind
    skill_ref: VersionedRef | None
    reason: NonEmptyText
    confidence: Confidence

    @model_validator(mode="after")
    def validate_route(self) -> RouteDecision:
        if (self.kind is RouteKind.MATCHED) != (self.skill_ref is not None):
            raise ValueError("MATCHED requires skill_ref; NO_CAPABILITY forbids it")
        return self


class DiagnosisOutcome(ContractModel):
    findings: list[Finding]
    state_delta: DiagnosisStateDelta
    requested_input: list[OpaqueId]
    requested_attachments: list[OpaqueId]
    candidate_conclusion_draft: CandidateConclusionDraft | None
    recommended_next_step: NonEmptyText

    @model_validator(mode="after")
    def validate_requests(self) -> DiagnosisOutcome:
        _unique(self.requested_input, "requested_input")
        _unique(self.requested_attachments, "requested_attachments")
        return self


class GenericDiagnosisOutcome(ContractModel):
    status: GenericResultStatus
    conclusion: NonEmptyText
    root_cause_analysis: NonEmptyText
    skill_name: SkillName


class ReviewAssessment(ContractModel):
    candidate_conclusion_id: OpaqueId
    candidate_revision: PositiveInt
    candidate_content_hash: Sha256
    reviewed_state_revision: PositiveInt
    reviewed_evidence_refs: list[OpaqueId]
    verdict: ReviewVerdict
    unsupported_findings: list[NonEmptyText]
    evidence_conflicts: list[NonEmptyText]
    missing_evidence: list[NonEmptyText]
    stale_references: list[NonEmptyText]
    requested_requirement_ids: list[OpaqueId] = []
    recommendation: NonEmptyText

    @model_validator(mode="after")
    def validate_verdict(self) -> ReviewAssessment:
        _unique(self.reviewed_evidence_refs, "reviewed_evidence_refs")
        _unique(self.requested_requirement_ids, "requested_requirement_ids")
        problems = (
            self.unsupported_findings,
            self.evidence_conflicts,
            self.missing_evidence,
            self.stale_references,
        )
        if self.verdict is ReviewVerdict.PASS and any(problems):
            raise ValueError("PASS requires all four problem arrays to be empty")
        if self.verdict is ReviewVerdict.NEED_MORE_EVIDENCE and not (
            self.missing_evidence or self.unsupported_findings
        ):
            raise ValueError("NEED_MORE_EVIDENCE requires missing or unsupported findings")
        if (
            self.verdict is not ReviewVerdict.NEED_MORE_EVIDENCE
            and self.requested_requirement_ids
        ):
            raise ValueError(
                "only NEED_MORE_EVIDENCE may request requirement IDs"
            )
        if self.verdict is ReviewVerdict.REJECT and not (
            self.unsupported_findings or self.evidence_conflicts or self.stale_references
        ):
            raise ValueError("REJECT requires unsupported findings, conflicts, or stale references")
        return self


AgentOutcomePayload: TypeAlias = RouteDecision | DiagnosisOutcome | ReviewAssessment
OutcomePayload: TypeAlias = AgentOutcomePayload | GenericDiagnosisOutcome


class AgentEvidenceCitation(ContractModel):
    evidence_binding: EvidenceBinding
    line_start: PositiveInt
    line_end: PositiveInt

    @model_validator(mode="after")
    def validate_range(self) -> AgentEvidenceCitation:
        if self.line_start > self.line_end:
            raise ValueError("citation line_start must not exceed line_end")
        return self


class AgentRuleClaim(ContractModel):
    rule_id: NonEmptyText
    claimed_result: RuleClaimResult
    fact_refs: list[OpaqueId]
    citations: list[AgentEvidenceCitation]
    explanation: NonEmptyText

    @model_validator(mode="after")
    def validate_claim(self) -> AgentRuleClaim:
        _unique(self.fact_refs, "AgentRuleClaim.fact_refs")
        citation_keys = [
            (
                citation.evidence_binding.existing_evidence_id,
                citation.evidence_binding.evidence_proposal_key,
                citation.line_start,
                citation.line_end,
            )
            for citation in self.citations
        ]
        _unique(citation_keys, "AgentRuleClaim.citations")
        return self


class VerifiedLogLineRange(ContractModel):
    path: RelativePosixPath
    line_start: PositiveInt
    line_end: PositiveInt
    raw_bytes_sha256: Sha256

    @model_validator(mode="after")
    def validate_range(self) -> VerifiedLogLineRange:
        if self.line_start > self.line_end:
            raise ValueError("verified line_start must not exceed line_end")
        return self


class EventObservationAudit(ContractModel):
    event_id: ContractName
    observed_count: NonNegativeInt
    count_is_lower_bound: bool


class DerivedValueAudit(ContractModel):
    name: ContractName
    value: str | int | None
    unit: NonEmptyText | None
    lower_bound: str | int | None
    upper_bound: str | int | None


class ServerRuleEvaluation(ContractModel):
    rule_id: NonEmptyText
    rule_kind: NonEmptyText
    status: ServerRuleStatus
    fact_refs: list[OpaqueId]
    evidence_bindings: list[EvidenceBinding]
    anchor_id: NonEmptyText | None
    derived_anchor_time: UtcTimestamp | None
    observed_times: list[UtcTimestamp]
    event_observations: list[EventObservationAudit]
    derived_values: list[DerivedValueAudit]
    line_ranges: list[VerifiedLogLineRange]
    issues: list[NonEmptyText]

    @model_validator(mode="after")
    def validate_evaluation(self) -> ServerRuleEvaluation:
        _unique(self.fact_refs, "ServerRuleEvaluation.fact_refs")
        _unique(
            [
                item.existing_evidence_id
                or f"proposal:{item.evidence_proposal_key}"
                for item in self.evidence_bindings
            ],
            "ServerRuleEvaluation.evidence_bindings",
        )
        _unique(self.observed_times, "ServerRuleEvaluation.observed_times")
        _unique(
            [item.event_id for item in self.event_observations],
            "ServerRuleEvaluation.event_observations.event_id",
        )
        _unique(
            [item.name for item in self.derived_values],
            "ServerRuleEvaluation.derived_values.name",
        )
        _unique(
            [
                (item.path, item.line_start, item.line_end, item.raw_bytes_sha256)
                for item in self.line_ranges
            ],
            "ServerRuleEvaluation.line_ranges",
        )
        if self.status in {
            ServerRuleStatus.VERIFIED_FAIL,
            ServerRuleStatus.UNVERIFIABLE,
        } and not self.issues:
            raise ValueError("failed or unverifiable rules require issues")
        if self.status is ServerRuleStatus.VERIFIED_PASS and self.issues:
            raise ValueError("VERIFIED_PASS forbids issues")
        is_semantic_rule = self.rule_kind == "SEMANTIC_CAUSALITY"
        is_semantic_status = self.status is ServerRuleStatus.SEMANTIC_ONLY
        if is_semantic_rule != is_semantic_status:
            raise ValueError(
                "SEMANTIC_CAUSALITY rules and SEMANTIC_ONLY status must correspond"
            )
        return self


class DecisionRuleAudit(ContractModel):
    rule_id: NonEmptyText
    agent_claim: AgentRuleClaim | None
    server_evaluation: ServerRuleEvaluation

    @model_validator(mode="after")
    def validate_rule_id(self) -> DecisionRuleAudit:
        if self.server_evaluation.rule_id != self.rule_id:
            raise ValueError("server evaluation rule_id must match audit rule_id")
        if self.agent_claim is not None and self.agent_claim.rule_id != self.rule_id:
            raise ValueError("agent claim rule_id must match audit rule_id")
        return self


class DecisionAuditV2(ContractModel):
    schema_version: Literal[2]
    job_id: OpaqueId
    case_id: OpaqueId
    job_type: JobType
    skill_ref: VersionedRef
    source_draft_sha256: Sha256
    subject_hash: Sha256
    candidate_target: CandidateTarget | None
    diagnosis_audit_hash: Sha256 | None
    selected_terminal_path_id: ContractName
    terminal_resolution_status: Literal["COMPLETE", "PARTIAL", "NONE"]
    required_rule_ids: Annotated[list[NonEmptyText], Field(min_length=1)]
    required_evidence_bindings: list[EvidenceBinding]
    rules: Annotated[list[DecisionRuleAudit], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_audit(self) -> DecisionAuditV2:
        if self.job_type is JobType.ROUTE:
            raise ValueError("ROUTE outcomes do not have a decision audit")
        _unique(self.required_rule_ids, "DecisionAuditV2.required_rule_ids")
        _unique(
            [
                item.existing_evidence_id
                or f"proposal:{item.evidence_proposal_key}"
                for item in self.required_evidence_bindings
            ],
            "DecisionAuditV2.required_evidence_bindings",
        )
        if [item.rule_id for item in self.rules] != self.required_rule_ids:
            raise ValueError("decision audit rules must exactly follow required_rule_ids")
        if self.job_type is JobType.REVIEW:
            if self.candidate_target is None or self.diagnosis_audit_hash is None:
                raise ValueError(
                    "REVIEW decision audits require candidate and diagnosis audit bindings"
                )
        elif self.diagnosis_audit_hash is not None:
            raise ValueError("DIAGNOSE decision audits forbid diagnosis_audit_hash")
        return self


class AgentJobOutcomeDraftV2(ContractModel):
    schema_version: Literal[2]
    job_id: OpaqueId
    case_id: OpaqueId
    job_type: JobType
    base_state_revision: PositiveInt
    result_type: OutcomeResultType
    payload: AgentOutcomePayload | None
    consumed_evidence_refs: list[OpaqueId]
    proposed_evidence_drafts: list[AgentEvidenceProposalDraft]
    proposed_artifact_drafts: list[AgentArtifactProposalDraft]
    error: ExecutionFailure | None
    rule_claims: list[AgentRuleClaim]

    @model_validator(mode="after")
    def validate_draft(self) -> AgentJobOutcomeDraftV2:
        _validate_outcome_shape(
            self.job_type, self.result_type, self.payload, self.error
        )
        _unique(self.consumed_evidence_refs, "consumed_evidence_refs")
        _unique([claim.rule_id for claim in self.rule_claims], "rule_claims.rule_id")
        if self.job_type is JobType.ROUTE and self.rule_claims:
            raise ValueError("ROUTE drafts forbid rule claims")
        if (
            self.job_type in {JobType.DIAGNOSE, JobType.REVIEW}
            and self.result_type is not OutcomeResultType.FAILED
            and not self.rule_claims
        ):
            raise ValueError("non-failed DIAGNOSE/REVIEW drafts require rule claims")
        _validate_proposal_keys(
            [draft.proposal_key for draft in self.proposed_evidence_drafts],
            [draft.proposal_key for draft in self.proposed_artifact_drafts],
            self.payload,
        )
        _validate_agent_draft_user_results(self.proposed_artifact_drafts)
        if isinstance(self.payload, DiagnosisOutcome):
            _validate_diagnosis_result_requests(self.result_type, self.payload)
            _validate_agent_delta(self.payload.state_delta, self.job_id, self.job_id)
            _validate_payload_evidence_bindings(
                self.payload,
                {item.proposal_key for item in self.proposed_evidence_drafts},
            )
        _validate_source_bindings(
            self.proposed_evidence_drafts,
            {
                item.proposal_key: item.artifact_kind
                for item in self.proposed_artifact_drafts
            },
        )
        return self


class AgentJobOutcome(ContractModel):
    outcome_id: OpaqueId
    job_id: OpaqueId
    case_id: OpaqueId
    job_type: JobType
    base_state_revision: PositiveInt
    result_type: OutcomeResultType
    payload: AgentOutcomePayload | None
    consumed_evidence_refs: list[OpaqueId]
    proposed_evidence_drafts: list[AgentEvidenceProposalDraft]
    proposed_artifact_drafts: list[AgentArtifactProposalDraft]
    error: ExecutionFailure | None
    produced_at: UtcTimestamp
    decision_audit: DecisionAuditV2 | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> AgentJobOutcome:
        _validate_outcome_shape(self.job_type, self.result_type, self.payload, self.error)
        _unique(self.consumed_evidence_refs, "consumed_evidence_refs")
        _validate_proposal_keys(
            [draft.proposal_key for draft in self.proposed_evidence_drafts],
            [draft.proposal_key for draft in self.proposed_artifact_drafts],
            self.payload,
        )
        _validate_server_final_user_results(
            self.job_type,
            self.result_type,
            self.payload,
            self.proposed_artifact_drafts,
        )
        if isinstance(self.payload, DiagnosisOutcome):
            _validate_diagnosis_result_requests(self.result_type, self.payload)
            _validate_agent_delta(self.payload.state_delta, self.outcome_id, self.job_id)
            _validate_payload_evidence_bindings(
                self.payload,
                {proposal.proposal_key for proposal in self.proposed_evidence_drafts},
            )
        _validate_source_bindings(
            self.proposed_evidence_drafts,
            {proposal.proposal_key: proposal.artifact_kind for proposal in self.proposed_artifact_drafts},
        )
        _validate_decision_audit_binding(self, self.decision_audit)
        _validate_inconclusive_effective_outcome(
            self.result_type,
            self.payload,
            self.proposed_evidence_drafts,
            self.proposed_artifact_drafts,
        )
        _validate_effective_resolution_gate(
            self.result_type, self.payload, self.decision_audit
        )
        return self


class JobOutcome(ContractModel):
    outcome_id: OpaqueId
    job_id: OpaqueId
    case_id: OpaqueId
    job_type: JobType
    base_state_revision: PositiveInt
    result_type: OutcomeResultType
    payload: OutcomePayload | None
    consumed_evidence_refs: list[OpaqueId]
    proposed_evidence: list[EvidenceProposal]
    proposed_artifacts: list[ArtifactProposal]
    error: ExecutionFailure | None
    produced_at: UtcTimestamp
    decision_audit: DecisionAuditV2 | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> JobOutcome:
        _validate_outcome_shape(self.job_type, self.result_type, self.payload, self.error)
        _unique(self.consumed_evidence_refs, "consumed_evidence_refs")
        if isinstance(self.payload, GenericDiagnosisOutcome) and (
            self.consumed_evidence_refs
            or self.proposed_evidence
            or self.proposed_artifacts
            or self.decision_audit is not None
        ):
            raise ValueError(
                "generic diagnosis Outcomes forbid evidence, artifacts, and decision audit"
            )
        _validate_proposal_keys(
            [proposal.proposal_key for proposal in self.proposed_evidence],
            [proposal.proposal_key for proposal in self.proposed_artifacts],
            self.payload,
        )
        _validate_server_final_user_results(
            self.job_type,
            self.result_type,
            self.payload,
            self.proposed_artifacts,
        )
        for proposal in self.proposed_evidence:
            if proposal.staged_resource_ref is not None and proposal.staged_resource_ref.owner_job_id != self.job_id:
                raise ValueError("evidence staged resources must belong to the outcome job")
        for proposal in self.proposed_artifacts:
            if proposal.staged_resource_ref.owner_job_id != self.job_id:
                raise ValueError("artifact staged resources must belong to the outcome job")
        if isinstance(self.payload, DiagnosisOutcome):
            _validate_diagnosis_result_requests(self.result_type, self.payload)
            _validate_agent_delta(self.payload.state_delta, self.outcome_id, self.job_id)
            _validate_payload_evidence_bindings(
                self.payload,
                {proposal.proposal_key for proposal in self.proposed_evidence},
            )
        _validate_source_bindings(
            self.proposed_evidence,
            {proposal.proposal_key: proposal.artifact_kind for proposal in self.proposed_artifacts},
        )
        _validate_decision_audit_binding(self, self.decision_audit)
        _validate_inconclusive_effective_outcome(
            self.result_type,
            self.payload,
            self.proposed_evidence,
            self.proposed_artifacts,
        )
        _validate_effective_resolution_gate(
            self.result_type, self.payload, self.decision_audit
        )
        return self


def _validate_outcome_shape(
    job_type: JobType,
    result_type: OutcomeResultType,
    payload: OutcomePayload | None,
    error: ExecutionFailure | None,
) -> None:
    allowed = {
        JobType.ROUTE: {OutcomeResultType.COMPLETED, OutcomeResultType.NO_CAPABILITY, OutcomeResultType.FAILED},
        JobType.DIAGNOSE: {
            OutcomeResultType.COMPLETED,
            OutcomeResultType.NEED_INPUT,
            OutcomeResultType.NEED_ATTACHMENT,
            OutcomeResultType.REROUTE,
            OutcomeResultType.INCONCLUSIVE,
            OutcomeResultType.FAILED,
        },
        JobType.REVIEW: {
            OutcomeResultType.COMPLETED,
            OutcomeResultType.INCONCLUSIVE,
            OutcomeResultType.FAILED,
        },
    }
    if result_type not in allowed[job_type]:
        raise ValueError("result_type is not allowed for the job type")
    if result_type is OutcomeResultType.FAILED:
        if payload is not None or error is None:
            raise ValueError("FAILED outcomes require payload=null and a non-null error")
        return
    if payload is None or error is not None:
        raise ValueError("non-failed outcomes require a payload and error=null")
    expected: type[ContractModel] | tuple[type[ContractModel], ...] = {
        JobType.ROUTE: RouteDecision,
        JobType.DIAGNOSE: (DiagnosisOutcome, GenericDiagnosisOutcome),
        JobType.REVIEW: ReviewAssessment,
    }[job_type]
    if not isinstance(payload, expected):
        raise ValueError("payload type does not match job_type")
    if isinstance(payload, GenericDiagnosisOutcome) and result_type is not OutcomeResultType.COMPLETED:
        raise ValueError("generic diagnosis results must use COMPLETED")
    if result_type is OutcomeResultType.NO_CAPABILITY and (
        not isinstance(payload, RouteDecision) or payload.kind is not RouteKind.NO_CAPABILITY
    ):
        raise ValueError("NO_CAPABILITY requires a NO_CAPABILITY route decision")
    if (
        job_type is JobType.ROUTE
        and result_type is OutcomeResultType.COMPLETED
        and (not isinstance(payload, RouteDecision) or payload.kind is not RouteKind.MATCHED)
    ):
        raise ValueError("a completed ROUTE outcome requires a MATCHED route decision")
    if (
        job_type is JobType.DIAGNOSE
        and result_type is OutcomeResultType.COMPLETED
        and isinstance(payload, DiagnosisOutcome)
        and payload.candidate_conclusion_draft is not None
        and any(not finding.evidence_bindings for finding in payload.findings)
    ):
        raise ValueError("every completed diagnosis finding must be evidence-backed")


def _validate_decision_audit_binding(
    outcome: AgentJobOutcome | JobOutcome,
    audit: DecisionAuditV2 | None,
) -> None:
    requires_audit = (
        outcome.result_type is not OutcomeResultType.FAILED
        and isinstance(outcome.payload, (DiagnosisOutcome, ReviewAssessment))
    )
    if requires_audit != (audit is not None):
        raise ValueError(
            "non-failed DIAGNOSE/REVIEW outcomes require exactly one decision audit"
        )
    if audit is None:
        return
    if (
        audit.job_id != outcome.job_id
        or audit.case_id != outcome.case_id
        or audit.job_type is not outcome.job_type
    ):
        raise ValueError("decision audit identity must match its Outcome")
    payload = outcome.payload
    if isinstance(payload, ReviewAssessment):
        target = audit.candidate_target
        if target is None or (
            target.candidate_conclusion_id != payload.candidate_conclusion_id
            or target.candidate_revision != payload.candidate_revision
            or target.candidate_content_hash != payload.candidate_content_hash
        ):
            raise ValueError("REVIEW decision audit target must match its assessment")


def _validate_inconclusive_effective_outcome(
    result_type: OutcomeResultType,
    payload: OutcomePayload | None,
    evidence: list[Any],
    artifacts: list[Any],
) -> None:
    if result_type is not OutcomeResultType.INCONCLUSIVE:
        return
    if isinstance(payload, DiagnosisOutcome):
        delta = payload.state_delta
        if (
            payload.findings
            or payload.candidate_conclusion_draft is not None
            or delta.problem_spec_patch is not None
            or delta.add_user_facts
            or delta.proposed_facts
            or delta.add_active_hypotheses
            or delta.update_hypotheses
            or delta.reject_hypotheses
            or delta.add_open_questions
            or delta.resolve_questions
            or delta.add_pending_requirements
            or delta.fulfill_requirements
        ):
            raise ValueError(
                "INCONCLUSIVE diagnosis must discard semantic findings, state, and Candidate"
            )
    if any(
        item.artifact_kind is ArtifactKind.USER_RESULT_ARCHIVE for item in artifacts
    ):
        raise ValueError("INCONCLUSIVE outcomes forbid USER_RESULT_ARCHIVE")


def _validate_effective_resolution_gate(
    result_type: OutcomeResultType,
    payload: OutcomePayload | None,
    audit: DecisionAuditV2 | None,
) -> None:
    resolves_candidate = (
        result_type is OutcomeResultType.COMPLETED
        and (
            (
                isinstance(payload, DiagnosisOutcome)
                and payload.candidate_conclusion_draft is not None
            )
            or (
                isinstance(payload, ReviewAssessment)
                and payload.verdict is ReviewVerdict.PASS
            )
        )
    )
    if not resolves_candidate:
        return
    assert audit is not None
    if audit.terminal_resolution_status == "NONE":
        raise ValueError("a Candidate/PASS Outcome requires a candidate terminal path")
    if isinstance(payload, DiagnosisOutcome):
        candidate = payload.candidate_conclusion_draft
        assert candidate is not None
        if (
            candidate.terminal_path_id != audit.selected_terminal_path_id
            or candidate.resolution_status.value
            != audit.terminal_resolution_status
        ):
            raise ValueError("Candidate and DecisionAudit terminal paths differ")
    aligned_results: dict[str, RuleClaimResult] = {}
    for item in audit.rules:
        claim = item.agent_claim
        evaluation = item.server_evaluation
        if claim is None:
            raise ValueError("a Candidate/PASS Outcome requires every rule claim")
        if evaluation.status is ServerRuleStatus.VERIFIED_PASS:
            expected = RuleClaimResult.PASS
        elif evaluation.status is ServerRuleStatus.VERIFIED_FAIL:
            expected = RuleClaimResult.FAIL
        elif evaluation.status in {
            ServerRuleStatus.UNVERIFIABLE,
            ServerRuleStatus.NOT_APPLICABLE,
        }:
            expected = RuleClaimResult.UNKNOWN
        elif evaluation.status is ServerRuleStatus.SEMANTIC_ONLY:
            expected = (
                RuleClaimResult.UNKNOWN
                if evaluation.issues
                else claim.claimed_result
            )
        else:  # pragma: no cover - enum exhaustiveness
            raise ValueError("unsupported server rule status")
        if claim.claimed_result is not expected:
            raise ValueError("Agent rule claim differs from the server evaluation")
        aligned_results[item.rule_id] = claim.claimed_result
    if isinstance(payload, DiagnosisOutcome):
        candidate = payload.candidate_conclusion_draft
        assert candidate is not None
        for factor in candidate.causal_factors:
            if any(
                aligned_results.get(rule_id) is not RuleClaimResult.PASS
                for rule_id in factor.required_rule_ids
            ):
                raise ValueError("confirmed causal factors require PASS rule claims")
        for factor in candidate.candidate_factors:
            results = [
                aligned_results.get(rule_id)
                for rule_id in factor.required_rule_ids
            ]
            if (
                any(result is RuleClaimResult.FAIL for result in results)
                or not any(result is RuleClaimResult.UNKNOWN for result in results)
            ):
                raise ValueError(
                    "candidate factors require UNKNOWN claims and forbid FAIL claims"
                )
        for factor in candidate.excluded_factors:
            if not any(
                aligned_results.get(rule_id) is RuleClaimResult.FAIL
                for rule_id in factor.required_rule_ids
            ):
                raise ValueError("excluded factors require at least one FAIL rule claim")


def finalize_unresolved_result(
    draft: UnresolvedResultDraft,
    audit_artifact_id: str,
    user_result_artifact_id: str,
    evidence_refs: Sequence[str],
) -> UnresolvedResult:
    """Bind the Coordinator draft to the server-generated audit bundle identity."""

    payload = draft.model_dump(
        mode="python",
        exclude={"evidence_bindings", "user_result_proposal_key"},
    )
    payload.update(
        audit_artifact_id=audit_artifact_id,
        user_result_artifact_id=user_result_artifact_id,
        evidence_refs=list(evidence_refs),
    )
    return UnresolvedResult.model_validate(payload)


def _validate_diagnosis_result_requests(result_type: OutcomeResultType, payload: DiagnosisOutcome) -> None:
    if result_type is OutcomeResultType.NEED_INPUT:
        if not payload.requested_input:
            raise ValueError("NEED_INPUT requires requested_input")
    elif result_type is OutcomeResultType.NEED_ATTACHMENT:
        if not payload.requested_attachments or payload.requested_input:
            raise ValueError("NEED_ATTACHMENT requires requested_attachments and forbids requested_input")
    elif payload.requested_input or payload.requested_attachments:
        raise ValueError("only NEED_INPUT/NEED_ATTACHMENT may carry requested requirement IDs")


def _validate_proposal_keys(evidence_keys: list[str], artifact_keys: list[str], payload: OutcomePayload | None) -> None:
    candidate_keys: list[str] = []
    if isinstance(payload, DiagnosisOutcome) and payload.candidate_conclusion_draft is not None:
        candidate_keys.append(payload.candidate_conclusion_draft.proposal_key)
    _unique(evidence_keys + artifact_keys + candidate_keys, "outcome proposal_key")


def _user_result_artifacts(artifacts: list[Any]) -> tuple[list[Any], list[Any]]:
    user_results = [artifact for artifact in artifacts if artifact.artifact_kind is ArtifactKind.USER_RESULT]
    archives = [
        artifact
        for artifact in artifacts
        if artifact.artifact_kind is ArtifactKind.USER_RESULT_ARCHIVE
    ]
    return user_results, archives


def _validate_agent_draft_user_results(artifacts: list[Any]) -> None:
    user_results, archives = _user_result_artifacts(artifacts)
    if user_results or archives:
        raise ValueError(
            "AgentJobOutcomeDraftV2 forbids server-generated USER_RESULT artifacts"
        )


def _validate_server_final_user_results(
    job_type: JobType,
    result_type: OutcomeResultType,
    payload: OutcomePayload | None,
    artifacts: list[Any],
) -> None:
    user_results, archives = _user_result_artifacts(artifacts)
    if len(user_results) > 1 or len(archives) > 1:
        raise ValueError("a server-final Outcome may carry at most one user result pair")

    candidate_result = (
        job_type is JobType.DIAGNOSE
        and result_type is OutcomeResultType.COMPLETED
        and isinstance(payload, DiagnosisOutcome)
        and payload.candidate_conclusion_draft is not None
    )
    unresolved_result = result_type is OutcomeResultType.INCONCLUSIVE or (
        job_type is JobType.REVIEW
        and result_type is OutcomeResultType.COMPLETED
        and isinstance(payload, ReviewAssessment)
        and payload.verdict is not ReviewVerdict.PASS
    )
    if isinstance(payload, GenericDiagnosisOutcome):
        if user_results or archives:
            raise ValueError("generic diagnosis Outcomes forbid result Artifacts")
        return
    if candidate_result:
        if len(user_results) != 1 or len(archives) != 1:
            raise ValueError(
                "a reviewed Candidate Outcome requires exactly one USER_RESULT and one USER_RESULT_ARCHIVE"
            )
    elif unresolved_result:
        if len(user_results) != 1 or archives:
            raise ValueError(
                "an unresolved server-final Outcome requires exactly one USER_RESULT and forbids USER_RESULT_ARCHIVE"
            )
    elif user_results or archives:
        raise ValueError("this Outcome branch forbids user result Artifacts")

    if archives:
        metadata = archives[0].metadata
        assert isinstance(metadata, UserResultArchiveMetadataV3)
        if metadata.user_result_proposal_key != user_results[0].proposal_key:
            raise ValueError("USER_RESULT_ARCHIVE must bind the unique USER_RESULT proposal")


def _validate_agent_delta(delta: DiagnosisStateDelta, outcome_id: str, job_id: str) -> None:
    if delta.add_user_facts or delta.fulfill_requirements:
        raise ValueError("Agent DIAGNOSE outcomes must leave add_user_facts and fulfill_requirements empty")
    drafts = delta.proposed_facts + delta.add_active_hypotheses + delta.add_open_questions
    for draft in drafts:
        provenance = draft.provenance
        if (
            provenance.source_type is not DiagnosisProvenanceType.AGENT_OUTCOME
            or provenance.source_ref != outcome_id
            or provenance.input_name is not None
        ):
            raise ValueError("Agent diagnosis drafts must use this outcome as AGENT_OUTCOME provenance")
    if any(
        requirement.requested_by_job_id != job_id
        or requirement.status is not RequirementStatus.OPEN
        for requirement in delta.add_pending_requirements
    ):
        raise ValueError("new Agent requirements must be OPEN and requested by the current Job")


def _all_payload_evidence_bindings(payload: DiagnosisOutcome) -> list[EvidenceBinding]:
    bindings: list[EvidenceBinding] = []
    for finding in payload.findings:
        bindings.extend(finding.evidence_bindings)
    delta = payload.state_delta
    for draft in delta.proposed_facts + delta.add_active_hypotheses + delta.add_open_questions:
        bindings.extend(draft.evidence_bindings)
    for change in delta.update_hypotheses + delta.reject_hypotheses + delta.resolve_questions:
        bindings.extend(change.evidence_bindings)
    bindings.extend(delta.add_evidence_bindings)
    candidate = payload.candidate_conclusion_draft
    if candidate is not None:
        bindings.extend(candidate.supporting_evidence_bindings)
        for mapping in candidate.completion_criteria_mapping:
            bindings.extend(mapping.evidence_bindings)
        for factor in (
            candidate.causal_factors
            + candidate.candidate_factors
            + candidate.excluded_factors
        ):
            bindings.extend(factor.evidence_bindings)
    return bindings


def _validate_payload_evidence_bindings(payload: DiagnosisOutcome, proposal_keys: set[str]) -> None:
    for binding in _all_payload_evidence_bindings(payload):
        if binding.evidence_proposal_key is not None and binding.evidence_proposal_key not in proposal_keys:
            raise ValueError("evidence binding references a proposal absent from this Outcome")


def _validate_source_bindings(evidence_proposals: list[Any], artifact_kinds: dict[str, ArtifactKind]) -> None:
    for proposal in evidence_proposals:
        artifact_key = proposal.source_binding.artifact_proposal_key
        if artifact_key is not None and artifact_kinds.get(artifact_key) is not ArtifactKind.LOGPARSE_RUN:
            raise ValueError("artifact source binding must resolve a same-Outcome LOGPARSE_RUN proposal")


class CaseSnapshot(ContractModel):
    case: Case
    active_job: Job | None
    resume_source_job: Job | None
    replacement_job_ids_by_source: dict[OpaqueId, OpaqueId]

    @model_validator(mode="after")
    def validate_snapshot(self) -> CaseSnapshot:
        if self.case.active_job_id is None:
            if self.active_job is not None:
                raise ValueError("active_job must be null when case.active_job_id is null")
        elif self.active_job is None or self.active_job.job_id != self.case.active_job_id:
            raise ValueError("active_job must resolve case.active_job_id")
        if self.active_job is not None and self.active_job.case_id != self.case.case_id:
            raise ValueError("active_job belongs to a different case")
        if self.resume_source_job is not None:
            if self.case.status is not CaseStatus.INTERRUPTED:
                raise ValueError("resume_source_job is only valid for INTERRUPTED cases")
            if (
                self.resume_source_job.case_id != self.case.case_id
                or self.resume_source_job.status is not JobStatus.INTERRUPTED
            ):
                raise ValueError("resume_source_job must be an INTERRUPTED job in this case")
            if self.resume_source_job.job_id in self.replacement_job_ids_by_source:
                raise ValueError("resume_source_job must not already have a replacement")
        return self


class PlannedResourceBinding(ContractModel):
    existing_resource_id: OpaqueId | None
    accepted_proposal_key: NonEmptyText | None

    @model_validator(mode="after")
    def validate_xor(self) -> PlannedResourceBinding:
        if (self.existing_resource_id is None) == (self.accepted_proposal_key is None):
            raise ValueError("exactly one planned resource binding branch must be populated")
        return self


class ReviewTargetBinding(ContractModel):
    existing_candidate_target: CandidateTarget | None
    accepted_candidate_proposal_key: NonEmptyText | None

    @model_validator(mode="after")
    def validate_xor(self) -> ReviewTargetBinding:
        if (self.existing_candidate_target is None) == (self.accepted_candidate_proposal_key is None):
            raise ValueError("exactly one review target binding branch must be populated")
        return self


class SelectedSkillUpdate(ContractModel):
    action: FieldUpdateAction
    value: VersionedRef | None

    @model_validator(mode="after")
    def validate_action(self) -> SelectedSkillUpdate:
        if (self.action is FieldUpdateAction.SET) != (self.value is not None):
            raise ValueError("SET requires value; CLEAR requires value=null")
        return self


class CaseFailureUpdate(ContractModel):
    action: FieldUpdateAction
    value: CaseFailure | None

    @model_validator(mode="after")
    def validate_action(self) -> CaseFailureUpdate:
        if (self.action is FieldUpdateAction.SET) != (self.value is not None):
            raise ValueError("SET requires value; CLEAR requires value=null")
        return self


class CandidateMutation(ContractModel):
    action: CandidateMutationAction
    candidate_binding: ReviewTargetBinding
    expected_status: CandidateStatus | None
    target_status: CandidateStatus
    reason: NonEmptyText | None

    @model_validator(mode="after")
    def validate_mutation(self) -> CandidateMutation:
        if self.action is CandidateMutationAction.INSTALL:
            if (
                self.candidate_binding.accepted_candidate_proposal_key is None
                or self.expected_status is not None
                or self.target_status is not CandidateStatus.REVIEWING
            ):
                raise ValueError("INSTALL must install an accepted proposal as REVIEWING")
        else:
            if (
                self.candidate_binding.existing_candidate_target is None
                or self.expected_status is not CandidateStatus.REVIEWING
                or self.target_status not in {CandidateStatus.ACCEPTED, CandidateStatus.REJECTED}
            ):
                raise ValueError("SET_STATUS must target an existing REVIEWING candidate")
            if self.target_status is CandidateStatus.REJECTED and self.reason is None:
                raise ValueError("rejecting a candidate requires reason")
        return self


class JobLifecycleUpdate(ContractModel):
    job_id: OpaqueId
    expected_status: JobStatus
    target_status: JobStatus
    started_at: UtcTimestamp | None
    finished_at: UtcTimestamp | None
    runtime_epoch: OpaqueId | None


class JobSpec(ContractModel):
    job_type: JobType
    diagnosis_mode: DiagnosisMode | None
    generic_skill_name: SkillName | None
    generic_problem_text: NonEmptyText | None
    goal: NonEmptyText
    target_state_revision: PositiveInt
    evidence_bindings: list[PlannedResourceBinding]
    attachment_refs: list[OpaqueId]
    previous_outcome_refs: list[OpaqueId]
    artifact_bindings: list[PlannedResourceBinding]
    agent_profile_ref: VersionedRef
    available_skill_refs: list[VersionedRef]
    skill_ref: VersionedRef | None
    tool_bundle_ref: VersionedRef
    context_policy_ref: VersionedRef
    output_contract_ref: VersionedRef
    logparse_tool_ref: VersionedRef | None
    logparse_product: NonEmptyText | None
    review_target_binding: ReviewTargetBinding | None
    replacement_for_job_id: OpaqueId | None
    resource_limits: ResourceLimits

    @model_validator(mode="after")
    def validate_spec(self) -> JobSpec:
        _unique(
            [
                binding.existing_resource_id
                or f"proposal:{binding.accepted_proposal_key}"
                for binding in self.evidence_bindings
            ],
            "JobSpec evidence_bindings",
        )
        _unique(
            [
                binding.existing_resource_id
                or f"proposal:{binding.accepted_proposal_key}"
                for binding in self.artifact_bindings
            ],
            "JobSpec artifact_bindings",
        )
        _unique(self.attachment_refs, "attachment_refs")
        _unique(self.previous_outcome_refs, "previous_outcome_refs")
        _unique(
            [ref.id + "@" + ref.version + "#" + ref.content_hash for ref in self.available_skill_refs],
            "available_skill_refs",
        )
        if (self.logparse_tool_ref is None) != (self.logparse_product is None):
            raise ValueError("logparse_tool_ref and logparse_product must be both null or both non-null")
        if self.logparse_tool_ref is not None and self.job_type is not JobType.DIAGNOSE:
            raise ValueError("only DIAGNOSE JobSpec may use logparse")
        if self.job_type is JobType.DIAGNOSE:
            if self.diagnosis_mode is DiagnosisMode.GENERIC:
                if self.generic_problem_text is None:
                    raise ValueError("GENERIC JobSpec requires frozen raw problem text")
                if any(
                    (
                        self.evidence_bindings,
                        self.attachment_refs,
                        self.previous_outcome_refs,
                        self.artifact_bindings,
                    )
                ):
                    raise ValueError(
                        "GENERIC JobSpec forbids Evidence, Attachments, Artifacts, and history"
                    )
            elif self.generic_problem_text is not None:
                raise ValueError("SPECIALIZED JobSpec forbids generic problem text")
        elif (
            self.diagnosis_mode is not None
            or self.generic_skill_name is not None
            or self.generic_problem_text is not None
        ):
            raise ValueError("only DIAGNOSE JobSpec may carry diagnosis-mode fields")
        if self.job_type is JobType.ROUTE:
            if self.review_target_binding is not None:
                raise ValueError("ROUTE JobSpec forbids skill/review target")
        elif self.job_type is JobType.DIAGNOSE:
            if self.review_target_binding is not None:
                raise ValueError("DIAGNOSE JobSpec requires skill_ref and forbids review target")
        elif self.review_target_binding is None or self.logparse_tool_ref is not None:
            raise ValueError("REVIEW JobSpec requires review target and forbids logparse")
        _validate_role_bindings(
            self.job_type,
            diagnosis_mode=self.diagnosis_mode,
            generic_skill_name=self.generic_skill_name,
            available_skill_refs=self.available_skill_refs,
            skill_ref=self.skill_ref,
            logparse_tool_ref=self.logparse_tool_ref,
            logparse_product=self.logparse_product,
            resource_limits=self.resource_limits,
        )
        return self


class CreateCaseTriggerPayload(ContractModel):
    raw_problem_text: NonEmptyText
    problem_spec: ProblemSpec
    initial_user_facts: Annotated[
        list[DiagnosisItem], Field(max_length=MAX_INITIAL_USER_FACTS)
    ]

    @model_validator(mode="after")
    def validate_payload(self) -> CreateCaseTriggerPayload:
        if self.problem_spec.revision != 1:
            raise ValueError("CreateCase trigger problem_spec revision must be 1")
        if len(self.initial_user_facts) > MAX_INITIAL_USER_FACTS:
            raise ValueError("too many initial user facts")
        _unique([item.item_id for item in self.initial_user_facts], "initial user fact item IDs")
        _unique(
            [item.provenance.input_name for item in self.initial_user_facts],
            "initial user fact names",
        )
        for item in self.initial_user_facts:
            if item.status is not DiagnosisItemStatus.ACTIVE:
                raise ValueError("initial user facts must be ACTIVE")
            if item.created_revision != 1:
                raise ValueError("initial user facts must be created at revision 1")
            if item.provenance.source_type is not DiagnosisProvenanceType.USER_INPUT:
                raise ValueError("initial user facts require USER_INPUT provenance")
            if item.supersedes:
                raise ValueError("initial user facts cannot supersede prior items")
            if item.evidence_refs:
                raise ValueError("initial user facts cannot carry Agent Evidence")
        return self


class RouteOutcomeTriggerPayload(ContractModel):
    job_outcome: JobOutcome

    @model_validator(mode="after")
    def validate_job_type(self) -> RouteOutcomeTriggerPayload:
        if self.job_outcome.job_type is not JobType.ROUTE:
            raise ValueError("route trigger requires ROUTE JobOutcome")
        return self


class DiagnosisOutcomeTriggerPayload(ContractModel):
    job_outcome: JobOutcome

    @model_validator(mode="after")
    def validate_job_type(self) -> DiagnosisOutcomeTriggerPayload:
        if self.job_outcome.job_type is not JobType.DIAGNOSE:
            raise ValueError("diagnosis trigger requires DIAGNOSE JobOutcome")
        return self


class ReviewOutcomeTriggerPayload(ContractModel):
    job_outcome: JobOutcome

    @model_validator(mode="after")
    def validate_job_type(self) -> ReviewOutcomeTriggerPayload:
        if self.job_outcome.job_type is not JobType.REVIEW:
            raise ValueError("review trigger requires REVIEW JobOutcome")
        return self


class SubmitSupplementTriggerPayload(ContractModel):
    user_facts: list[DiagnosisItem]
    ready_attachment_ids: list[OpaqueId]
    stable_target_changed: Annotated[bool, Field(strict=True)]

    @model_validator(mode="after")
    def validate_payload(self) -> SubmitSupplementTriggerPayload:
        if not self.user_facts and not self.ready_attachment_ids:
            raise ValueError("supplement trigger cannot be empty")
        _unique([item.item_id for item in self.user_facts], "supplement user fact IDs")
        _unique(
            [item.provenance.input_name for item in self.user_facts],
            "supplement user fact names",
        )
        for item in self.user_facts:
            if item.status is not DiagnosisItemStatus.ACTIVE:
                raise ValueError("supplement user facts must be ACTIVE")
            if item.provenance.source_type is not DiagnosisProvenanceType.USER_INPUT:
                raise ValueError("supplement user facts require USER_INPUT provenance")
            if item.evidence_refs or item.supersedes:
                raise ValueError("new supplement user facts cannot carry Evidence or supersede items")
        _unique(self.ready_attachment_ids, "ready_attachment_ids")
        return self


class CancelCaseTriggerPayload(ContractModel):
    reason: Literal["USER_CANCEL"]
    active_job_id: OpaqueId | None


class ResumeInterruptedTriggerPayload(ContractModel):
    source_job_id: OpaqueId


class ExecutionFailedTriggerPayload(ContractModel):
    source_job_id: OpaqueId
    source_outcome_id: OpaqueId | None
    execution_failure: ExecutionFailure


class AssetUnavailableTriggerPayload(ContractModel):
    source_job_id: OpaqueId
    missing_refs: Annotated[list[VersionedRef], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_refs(self) -> AssetUnavailableTriggerPayload:
        if not self.missing_refs:
            raise ValueError("missing_refs must be non-empty")
        return self


class OldEpochTriggerPayload(ContractModel):
    source_job_id: OpaqueId
    previous_runtime_epoch: OpaqueId
    current_runtime_epoch: OpaqueId

    @model_validator(mode="after")
    def validate_epochs(self) -> OldEpochTriggerPayload:
        if self.previous_runtime_epoch == self.current_runtime_epoch:
            raise ValueError("previous and current runtime epochs must differ")
        return self


class StaleActiveOutcomeTriggerPayload(ContractModel):
    source_job_id: OpaqueId
    outcome_id: OpaqueId
    expected_base_state_revision: PositiveInt
    actual_state_revision: PositiveInt

    @model_validator(mode="after")
    def validate_drift(self) -> StaleActiveOutcomeTriggerPayload:
        if self.expected_base_state_revision == self.actual_state_revision:
            raise ValueError("stale-active trigger requires differing revisions")
        return self


TriggerPayload: TypeAlias = (
    CreateCaseTriggerPayload
    | RouteOutcomeTriggerPayload
    | DiagnosisOutcomeTriggerPayload
    | ReviewOutcomeTriggerPayload
    | SubmitSupplementTriggerPayload
    | CancelCaseTriggerPayload
    | ResumeInterruptedTriggerPayload
    | ExecutionFailedTriggerPayload
    | AssetUnavailableTriggerPayload
    | OldEpochTriggerPayload
    | StaleActiveOutcomeTriggerPayload
)


class ValidatedTrigger(ContractModel):
    trigger_id: OpaqueId
    trigger_type: TriggerType
    case_id: OpaqueId
    expected_case_revision: NonNegativeInt
    idempotency_key: NonEmptyText
    payload: TriggerPayload
    continuation_resources: ContinuationResourceView
    runtime_bindings_by_job_type: dict[JobType, RuntimeBindings]
    occurred_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_trigger(self) -> ValidatedTrigger:
        expected_type = {
            TriggerType.CREATE_CASE: CreateCaseTriggerPayload,
            TriggerType.ROUTE_OUTCOME: RouteOutcomeTriggerPayload,
            TriggerType.DIAGNOSIS_OUTCOME: DiagnosisOutcomeTriggerPayload,
            TriggerType.REVIEW_OUTCOME: ReviewOutcomeTriggerPayload,
            TriggerType.SUBMIT_SUPPLEMENT: SubmitSupplementTriggerPayload,
            TriggerType.CANCEL_CASE: CancelCaseTriggerPayload,
            TriggerType.RESUME_INTERRUPTED: ResumeInterruptedTriggerPayload,
            TriggerType.EXECUTION_FAILED: ExecutionFailedTriggerPayload,
            TriggerType.ASSET_VERSION_UNAVAILABLE: AssetUnavailableTriggerPayload,
            TriggerType.MARK_OLD_EPOCH_INTERRUPTED: OldEpochTriggerPayload,
            TriggerType.STALE_ACTIVE_OUTCOME: StaleActiveOutcomeTriggerPayload,
        }[self.trigger_type]
        if not isinstance(self.payload, expected_type):
            raise ValueError("trigger_type and payload type do not match")
        for job_type, bindings in self.runtime_bindings_by_job_type.items():
            _validate_role_bindings(
                job_type,
                diagnosis_mode=bindings.diagnosis_mode,
                generic_skill_name=bindings.generic_skill_name,
                available_skill_refs=bindings.available_skill_refs,
                skill_ref=bindings.skill_ref,
                logparse_tool_ref=bindings.logparse_tool_ref,
                logparse_product=bindings.logparse_product,
                resource_limits=bindings.resource_limits,
            )
        if isinstance(
            self.payload,
            (RouteOutcomeTriggerPayload, DiagnosisOutcomeTriggerPayload, ReviewOutcomeTriggerPayload),
        ):
            if self.payload.job_outcome.case_id != self.case_id:
                raise ValueError("Outcome Trigger case_id must equal JobOutcome.case_id")
            if self.occurred_at != self.payload.job_outcome.produced_at:
                raise ValueError("Outcome trigger occurred_at must equal JobOutcome.produced_at")
            if (
                not self.continuation_resources.previous_outcome_refs
                or self.continuation_resources.previous_outcome_refs[0]
                != self.payload.job_outcome.outcome_id
            ):
                raise ValueError(
                    "Outcome trigger continuation must start with the incoming Outcome"
                )
        if isinstance(self.payload, CreateCaseTriggerPayload):
            if self.expected_case_revision != 0:
                raise ValueError("CREATE_CASE trigger expected_case_revision must be 0")
            if any(
                getattr(self.continuation_resources, field_name)
                for field_name in (
                    "evidence_refs",
                    "attachment_refs",
                    "artifact_refs",
                    "previous_outcome_refs",
                )
            ):
                raise ValueError("CREATE_CASE continuation resources must be empty")
            if any(
                item.provenance.source_ref != self.trigger_id
                for item in self.payload.initial_user_facts
            ):
                raise ValueError("initial user fact provenance must reference trigger_id")
        elif self.expected_case_revision == 0:
            raise ValueError("non-CREATE triggers require a positive expected_case_revision")
        if isinstance(self.payload, SubmitSupplementTriggerPayload) and any(
            item.provenance.source_ref != self.trigger_id
            for item in self.payload.user_facts
        ):
            raise ValueError("supplement user fact provenance must reference trigger_id")
        return self


class TransitionPlan(ContractModel):
    accepted_state_delta: DiagnosisStateDelta
    target_case_status: CaseStatus
    job_updates: list[JobLifecycleUpdate]
    outcome_disposition: OutcomeDisposition | None
    accepted_evidence_proposal_keys: list[NonEmptyText]
    accepted_artifact_proposal_keys: list[NonEmptyText]
    accepted_candidate_proposal_key: NonEmptyText | None
    selected_skill_update: SelectedSkillUpdate | None
    case_failure_update: CaseFailureUpdate | None
    candidate_mutation: CandidateMutation | None
    next_job_spec: JobSpec | None
    final_result_target: CandidateTarget | None
    unresolved_result_draft: UnresolvedResultDraft | None = None
    generic_result: GenericResult | None = None
    clear_active_job: bool
    reason: NonEmptyText

    @model_validator(mode="after")
    def validate_plan(self) -> TransitionPlan:
        _unique(self.accepted_evidence_proposal_keys, "accepted_evidence_proposal_keys")
        _unique(self.accepted_artifact_proposal_keys, "accepted_artifact_proposal_keys")
        if self.target_case_status is CaseStatus.FAILED:
            if (
                self.case_failure_update is None
                or self.case_failure_update.action is not FieldUpdateAction.SET
            ):
                raise ValueError("FAILED plans must SET case failure")
        elif self.case_failure_update is not None and self.case_failure_update.action is FieldUpdateAction.SET:
            raise ValueError("non-FAILED plans cannot SET case failure")
        if self.generic_result is not None:
            expected_status = CaseStatus(self.generic_result.status.value)
            if self.target_case_status is not expected_status:
                raise ValueError(
                    "generic_result status must equal the planned Case terminal status"
                )
            if (
                self.unresolved_result_draft is not None
                or self.accepted_evidence_proposal_keys
                or self.accepted_artifact_proposal_keys
                or self.accepted_candidate_proposal_key is not None
                or self.candidate_mutation is not None
                or self.next_job_spec is not None
                or self.final_result_target is not None
                or not self.clear_active_job
            ):
                raise ValueError(
                    "generic terminal plans forbid specialized results, resources, and next Jobs"
                )
            if (
                self.selected_skill_update is None
                or self.selected_skill_update.action is not FieldUpdateAction.CLEAR
            ):
                raise ValueError("generic terminal plans must clear selected_skill_ref")
        if self.target_case_status is CaseStatus.UNRESOLVED:
            if self.unresolved_result_draft is None and self.generic_result is None:
                raise ValueError(
                    "UNRESOLVED plans require an unresolved or generic result"
                )
            if self.next_job_spec is not None or not self.clear_active_job:
                raise ValueError(
                    "UNRESOLVED plans must clear the active Job and cannot create another"
                )
        elif self.unresolved_result_draft is not None:
            raise ValueError(
                "only UNRESOLVED plans may carry unresolved_result_draft"
            )
        if (
            self.generic_result is not None
            and self.target_case_status not in {CaseStatus.RESOLVED, CaseStatus.UNRESOLVED}
        ):
            raise ValueError("generic_result is valid only for a terminal generic plan")
        if self.accepted_candidate_proposal_key is not None:
            if (
                self.candidate_mutation is None
                or self.candidate_mutation.action is not CandidateMutationAction.INSTALL
                or self.candidate_mutation.candidate_binding.accepted_candidate_proposal_key
                != self.accepted_candidate_proposal_key
            ):
                raise ValueError("accepted candidate key must be installed by candidate_mutation")
        if self.candidate_mutation is not None and self.candidate_mutation.action is CandidateMutationAction.INSTALL:
            if (
                self.accepted_candidate_proposal_key is None
                or self.candidate_mutation.candidate_binding.accepted_candidate_proposal_key
                != self.accepted_candidate_proposal_key
            ):
                raise ValueError("INSTALL candidate mutation requires the accepted candidate proposal key")
        if self.candidate_mutation is not None and self.candidate_mutation.target_status is CandidateStatus.ACCEPTED:
            target = self.candidate_mutation.candidate_binding.existing_candidate_target
            if target is None or self.final_result_target != target:
                raise ValueError("ACCEPTED candidate mutation requires matching final_result_target")
            if self.target_case_status not in {
                CaseStatus.RESOLVED,
                CaseStatus.PARTIALLY_RESOLVED,
            }:
                raise ValueError(
                    "ACCEPTED candidate mutation requires a resolved target Case status"
                )
        elif self.final_result_target is not None:
            raise ValueError("final_result_target is only valid for candidate acceptance")
        if self.next_job_spec is not None:
            for binding in self.next_job_spec.evidence_bindings:
                if (
                    binding.accepted_proposal_key is not None
                    and binding.accepted_proposal_key not in self.accepted_evidence_proposal_keys
                ):
                    raise ValueError("next Job Evidence binding refers to an unaccepted Evidence proposal key")
            for binding in self.next_job_spec.artifact_bindings:
                if (
                    binding.accepted_proposal_key is not None
                    and binding.accepted_proposal_key not in self.accepted_artifact_proposal_keys
                ):
                    raise ValueError("next Job Artifact binding refers to an unaccepted Artifact proposal key")
            review_binding = self.next_job_spec.review_target_binding
            if review_binding is not None:
                if self.accepted_candidate_proposal_key is not None:
                    if (
                        review_binding.accepted_candidate_proposal_key
                        != self.accepted_candidate_proposal_key
                    ):
                        raise ValueError(
                            "next REVIEW Job must bind the candidate accepted by this plan"
                        )
                elif review_binding.accepted_candidate_proposal_key is not None:
                    raise ValueError(
                        "next REVIEW Job cannot bind an unaccepted candidate proposal"
                    )
        return self


CoordinatorPlanResult: TypeAlias = TransitionPlan | ApplicationError


class BusinessReceipt(ContractModel):
    operation: NonEmptyText
    primary_resource_id: OpaqueId
    case_id: OpaqueId | None
    case_revision: PositiveInt | None
    job_id: OpaqueId | None
    status: NonEmptyText


class IdempotencyRecord(ContractModel):
    operation: NonEmptyText
    idempotency_key: NonEmptyText
    request_hash: Sha256
    business_receipt: BusinessReceipt
    case_id: OpaqueId | None
    created_at: UtcTimestamp


class OutcomeProcessingRecord(ContractModel):
    """Durable first-processing audit for one claimed Outcome submission.

    For the narrowly allowed technical rejection branch with no trusted
    ``JobOutcome``, ``outcome_id``, ``outcome_hash`` and ``outcome_file_ref``
    are the immutable values claimed by the worker receipt; they are not an
    assertion that a valid finalized DTO was observed on disk.
    """

    outcome_id: OpaqueId = Field(
        description="Trusted saved Outcome ID, or caller-claimed ID for a technical rejection without a trusted DTO."
    )
    job_id: OpaqueId
    outcome_hash: Sha256 = Field(
        description="Trusted canonical hash, or caller-claimed receipt hash for an untrusted technical rejection."
    )
    outcome_file_ref: ExecutionFileRef = Field(
        description="Trusted finalized reference, or caller-claimed receipt reference for an untrusted technical rejection."
    )
    disposition: OutcomeDisposition
    processed_at: UtcTimestamp
    error_code: ErrorCode | None
    accepted_evidence_ids: list[OpaqueId]
    accepted_artifact_ids: list[OpaqueId]
    generated_artifact_ids: list[OpaqueId] = []
    created_job_id: OpaqueId | None
    reason: NonEmptyText

    @model_validator(mode="after")
    def validate_processing(self) -> OutcomeProcessingRecord:
        _unique(self.accepted_evidence_ids, "accepted_evidence_ids")
        _unique(self.accepted_artifact_ids, "accepted_artifact_ids")
        _unique(self.generated_artifact_ids, "generated_artifact_ids")
        if set(self.accepted_artifact_ids) & set(self.generated_artifact_ids):
            raise ValueError(
                "accepted and server-generated artifact IDs must be disjoint"
            )
        if self.outcome_hash != self.outcome_file_ref.sha256:
            raise ValueError("outcome_hash must equal outcome_file_ref.sha256")
        if self.outcome_file_ref.relative_key != f"jobs/{self.job_id}/job_outcome.json":
            raise ValueError("Outcome processing file key must match its Job")
        if self.disposition is OutcomeDisposition.DUPLICATE:
            raise ValueError("DUPLICATE is a replay receipt, not a persisted processing record")
        if self.disposition is OutcomeDisposition.APPLIED:
            if self.error_code is not None:
                raise ValueError("APPLIED processing records forbid error_code")
        else:
            if (
                self.accepted_evidence_ids
                or self.accepted_artifact_ids
                or self.generated_artifact_ids
                or self.created_job_id is not None
            ):
                raise ValueError("non-APPLIED processing records cannot accept or create objects")
            if (self.disposition is OutcomeDisposition.REJECTED) != (self.error_code is not None):
                raise ValueError("error_code must be present exactly for REJECTED processing")
            if (
                self.disposition is OutcomeDisposition.REJECTED
                and self.error_code not in OUTCOME_REJECTION_CODES
            ):
                raise ValueError(
                    "REJECTED processing requires a frozen technical rejection code"
                )
        return self


class ExecutionFailureRecord(ContractModel):
    failure_id: OpaqueId
    job_id: OpaqueId
    runtime_epoch: OpaqueId
    failure: ExecutionFailure
    recorded_at: UtcTimestamp


class RuntimeEpochRecord(ContractModel):
    runtime_epoch: OpaqueId
    started_at: UtcTimestamp
    recovery_id: OpaqueId
    recovery_completed_at: UtcTimestamp | None


class RecoveryProcessingRecord(ContractModel):
    recovery_id: OpaqueId
    current_runtime_epoch: OpaqueId
    interrupted_job_ids: list[OpaqueId]
    pending_job_ids: list[OpaqueId]
    completed_at: UtcTimestamp | None

    @model_validator(mode="after")
    def validate_job_ids(self) -> RecoveryProcessingRecord:
        for field_name in ("interrupted_job_ids", "pending_job_ids"):
            values = getattr(self, field_name)
            _unique(values, field_name)
            if values != sorted(values):
                raise ValueError(f"{field_name} must be sorted")
        if set(self.interrupted_job_ids) & set(self.pending_job_ids):
            raise ValueError(
                "interrupted_job_ids and pending_job_ids must be disjoint"
            )
        return self


class ExecutionFileRef(ContractModel):
    relative_key: RelativePosixPath
    size: NonNegativeInt
    sha256: Sha256


def _validate_execution_file_ref(
    value: ContractModel,
    file_ref: ExecutionFileRef,
    *,
    job_id: str,
    filename: str,
) -> None:
    expected_key = f"jobs/{job_id}/{filename}"
    payload = _canonical_json_bytes(value.model_dump(mode="json"))
    if file_ref.relative_key != expected_key:
        raise ValueError(f"execution file key must be {expected_key}")
    if file_ref.size != len(payload) or file_ref.sha256 != hashlib.sha256(payload).hexdigest():
        raise ValueError("ExecutionFileRef must match the canonical DTO bytes")


class CaseAggregate(ContractModel):
    case: Case
    jobs: dict[OpaqueId, Job]
    outcomes: dict[OpaqueId, JobOutcome]
    outcome_processing_records: dict[OpaqueId, OutcomeProcessingRecord]
    execution_failure_records: dict[OpaqueId, ExecutionFailureRecord]
    attachments: dict[OpaqueId, Attachment]
    evidence: dict[OpaqueId, Evidence]
    artifacts: dict[OpaqueId, Artifact]

    @model_validator(mode="after")
    def validate_maps(self) -> CaseAggregate:
        mappings = {
            "jobs": (self.jobs, "job_id"),
            "outcomes": (self.outcomes, "outcome_id"),
            "outcome_processing_records": (self.outcome_processing_records, "outcome_id"),
            "execution_failure_records": (self.execution_failure_records, "failure_id"),
            "attachments": (self.attachments, "attachment_id"),
            "evidence": (self.evidence, "evidence_id"),
            "artifacts": (self.artifacts, "artifact_id"),
        }
        for label, (mapping, attribute) in mappings.items():
            if any(key != getattr(value, attribute) for key, value in mapping.items()):
                raise ValueError(f"{label} map keys must equal object IDs")
        if not set(self.outcomes) <= set(self.outcome_processing_records):
            raise ValueError(
                "every saved Outcome requires an OutcomeProcessingRecord"
            )
        untrusted_outcome_ids = set(self.outcome_processing_records) - set(self.outcomes)
        for outcome_id in untrusted_outcome_ids:
            record = self.outcome_processing_records[outcome_id]
            if (
                record.disposition is not OutcomeDisposition.REJECTED
                or record.error_code not in UNTRUSTED_OUTCOME_REJECTION_CODES
            ):
                raise ValueError(
                    "only fixed technical REJECTED processing may omit a trusted Outcome"
                )
        for collection in (self.jobs.values(), self.outcomes.values(), self.attachments.values(), self.evidence.values(), self.artifacts.values()):
            if any(item.case_id != self.case.case_id for item in collection):
                raise ValueError("case aggregate contains a resource owned by another case")

        active_job = (
            None
            if self.case.active_job_id is None
            else self.jobs.get(self.case.active_job_id)
        )
        if self.case.active_job_id is not None:
            if active_job is None or active_job.status not in {JobStatus.PENDING, JobStatus.RUNNING}:
                raise ValueError("Case.active_job_id must resolve a PENDING or RUNNING Job")
            if self.case.status is CaseStatus.REVIEWING:
                if active_job.job_type is not JobType.REVIEW:
                    raise ValueError("REVIEWING Case must point to a REVIEW Job")
            elif self.case.status is CaseStatus.RUNNING and active_job.job_type is JobType.REVIEW:
                raise ValueError("RUNNING Case must point to a ROUTE or DIAGNOSE Job")

        diagnosis = self.case.diagnosis_state
        if any(ref not in self.evidence for ref in diagnosis.evidence_refs):
            raise ValueError("DiagnosisState contains a dangling Evidence reference")
        user_facts = {item.item_id: item for item in diagnosis.user_facts}
        if any(
            requirement.requested_by_job_id not in self.jobs
            for requirement in diagnosis.pending_requirements
        ):
            raise ValueError("PendingRequirement.requested_by_job_id must resolve")
        for requirement in diagnosis.pending_requirements:
            if requirement.status is not RequirementStatus.FULFILLED:
                continue
            if requirement.kind is RequirementKind.INPUT:
                if any(ref not in user_facts for ref in requirement.fulfilled_by_refs):
                    raise ValueError("INPUT requirement fulfillment must resolve a user fact")
                assert isinstance(requirement.constraints, InputRequirementConstraints)
                fact = user_facts[requirement.fulfilled_by_refs[0]]
                encoded_length = len(fact.statement.encode("utf-8"))
                if not (
                    requirement.constraints.min_utf8_bytes
                    <= encoded_length
                    <= requirement.constraints.max_utf8_bytes
                ):
                    raise ValueError("fulfilled INPUT value violates its byte constraints")
                if (
                    requirement.constraints.pattern is not None
                    and re.fullmatch(requirement.constraints.pattern, fact.statement) is None
                ):
                    raise ValueError("fulfilled INPUT value violates its pattern")
                if (
                    requirement.constraints.allowed_values
                    and fact.statement not in requirement.constraints.allowed_values
                ):
                    raise ValueError("fulfilled INPUT value is not allowed")
            else:
                assert isinstance(
                    requirement.constraints, AttachmentRequirementConstraints
                )
                resolved_attachments = [
                    self.attachments.get(ref) for ref in requirement.fulfilled_by_refs
                ]
                if any(
                    attachment is None
                    or attachment.status is not AttachmentStatus.READY
                    for attachment in resolved_attachments
                ):
                    raise ValueError(
                        "ATTACHMENT requirement fulfillment must resolve READY attachments"
                    )
                if requirement.constraints.allowed_content_types and any(
                    attachment is not None
                    and attachment.content_type
                    not in requirement.constraints.allowed_content_types
                    for attachment in resolved_attachments
                ):
                    raise ValueError(
                        "fulfilled Attachment content type is not allowed"
                    )
        candidate = diagnosis.candidate_conclusion
        if candidate is not None and candidate.proposed_by_job_id not in self.jobs:
            raise ValueError("CandidateConclusion.proposed_by_job_id must resolve")

        replacement_sources: list[str] = []
        for job in self.jobs.values():
            if (
                job.diagnosis_mode is DiagnosisMode.GENERIC
                and job.generic_problem_text != self.case.raw_problem_text
            ):
                raise ValueError(
                    "GENERIC Job input must equal the Case raw_problem_text"
                )
            if any(ref not in self.evidence for ref in job.evidence_refs):
                raise ValueError("Job contains a dangling Evidence reference")
            if (
                job.context_snapshot is not None
                and any(
                    ref not in self.evidence
                    for ref in job.context_snapshot.evidence_refs
                )
            ):
                raise ValueError("Job snapshot contains a dangling Evidence reference")
            if any(
                ref not in self.attachments
                or self.attachments[ref].status is not AttachmentStatus.READY
                for ref in job.attachment_refs
            ):
                raise ValueError("Job attachments must resolve READY resources")
            if any(ref not in self.artifacts for ref in job.artifact_refs):
                raise ValueError("Job contains a dangling Artifact reference")
            if any(ref not in self.outcomes for ref in job.previous_outcome_refs):
                raise ValueError("Job contains a dangling previous Outcome reference")
            if job.replacement_for_job_id is not None:
                source = self.jobs.get(job.replacement_for_job_id)
                if source is None or source.status is not JobStatus.INTERRUPTED:
                    raise ValueError("replacement_for_job_id must resolve an INTERRUPTED Job")
                replacement_sources.append(job.replacement_for_job_id)
        _unique(replacement_sources, "replacement_for_job_id")

        for evidence in self.evidence.values():
            source_ref = evidence.source_ref
            if evidence.source_type is EvidenceSourceType.USER_FACT:
                source = user_facts.get(source_ref)
                if source is None or not isinstance(evidence.locator, UserFactEvidenceLocator):
                    raise ValueError("USER_FACT Evidence must resolve a Case user fact")
                if evidence.locator.input_name != source.provenance.input_name:
                    raise ValueError("USER_FACT locator input_name must match provenance")
            elif evidence.source_type is EvidenceSourceType.ATTACHMENT:
                source = self.attachments.get(source_ref)
                if source is None or source.status is not AttachmentStatus.READY:
                    raise ValueError("ATTACHMENT Evidence must resolve a READY Attachment")
            elif evidence.source_type is EvidenceSourceType.LOGPARSE:
                source = self.artifacts.get(source_ref)
                if source is None or source.kind is not ArtifactKind.LOGPARSE_RUN:
                    raise ValueError("LOGPARSE Evidence must resolve a LOGPARSE_RUN Artifact")
            elif evidence.source_type is EvidenceSourceType.TOOL_OUTPUT:
                source = self.artifacts.get(source_ref)
                if source is None or source.kind is not ArtifactKind.DIAGNOSTIC_EXPORT:
                    raise ValueError("TOOL_OUTPUT Evidence must resolve a DIAGNOSTIC_EXPORT")
            elif source_ref not in self.outcomes:
                raise ValueError("PREVIOUS_OUTCOME Evidence must resolve a saved Outcome")

        for artifact in self.artifacts.values():
            producing_job = self.jobs.get(artifact.created_by_job_id)
            if producing_job is None:
                raise ValueError("Artifact.created_by_job_id must resolve")
            if artifact.kind is ArtifactKind.AUDIT_BUNDLE:
                assert isinstance(artifact.metadata, AuditBundleMetadata)
                if (
                    artifact.metadata.case_id != self.case.case_id
                    or artifact.metadata.source_job_id != artifact.created_by_job_id
                    or artifact.metadata.source_outcome_id not in self.outcomes
                ):
                    raise ValueError(
                        "AUDIT_BUNDLE metadata must resolve its Case, Job, and Outcome"
                    )
            if artifact.kind is ArtifactKind.LOGPARSE_RUN:
                assert isinstance(artifact.metadata, LogparseRunMetadata)
                attachment = self.attachments.get(artifact.metadata.source_attachment_id)
                if (
                    producing_job.logparse_tool_ref != artifact.metadata.logparse_version_ref
                    or producing_job.logparse_product != artifact.metadata.parse_parameters.product
                    or artifact.metadata.source_attachment_id not in producing_job.attachment_refs
                    or attachment is None
                    or attachment.status is not AttachmentStatus.READY
                    or attachment.sha256 != artifact.metadata.source_attachment_sha256
                ):
                    raise ValueError("LOGPARSE_RUN metadata must match its producing Job and Attachment")

        for record in self.outcome_processing_records.values():
            outcome = self.outcomes.get(record.outcome_id)
            job = self.jobs.get(record.job_id)
            if job is None:
                raise ValueError("OutcomeProcessingRecord must resolve its Job")
            if outcome is None:
                if (
                    record.disposition is OutcomeDisposition.REJECTED
                    and record.error_code in UNTRUSTED_OUTCOME_REJECTION_CODES
                ):
                    continue
                raise ValueError("OutcomeProcessingRecord must resolve its Outcome")
            outcome_bytes = _canonical_json_bytes(outcome.model_dump(mode="json"))
            if (
                len(outcome_bytes) != record.outcome_file_ref.size
                or hashlib.sha256(outcome_bytes).hexdigest() != record.outcome_hash
            ):
                raise ValueError("OutcomeProcessingRecord must hash the canonical saved Outcome")
            if record.disposition in {OutcomeDisposition.APPLIED, OutcomeDisposition.STALE} and (
                outcome.job_id != job.job_id
                or outcome.case_id != self.case.case_id
                or outcome.job_type is not job.job_type
                or outcome.base_state_revision != job.base_state_revision
            ):
                raise ValueError("APPLIED/STALE Outcome headers must match the source Job")
            if record.disposition is OutcomeDisposition.APPLIED:
                from .outcomes import validate_outcome_for_job

                validate_outcome_for_job(job, outcome, self)
                accepted_evidence = [self.evidence.get(ref) for ref in record.accepted_evidence_ids]
                accepted_artifacts = [self.artifacts.get(ref) for ref in record.accepted_artifact_ids]
                generated_artifacts = [
                    self.artifacts.get(ref) for ref in record.generated_artifact_ids
                ]
                if any(
                    item is None
                    for item in accepted_evidence
                    + accepted_artifacts
                    + generated_artifacts
                ):
                    raise ValueError("APPLIED processing IDs must resolve formal resources")
                if any(item.collected_at != outcome.produced_at for item in accepted_evidence if item is not None):
                    raise ValueError("accepted Evidence must use Outcome.produced_at")
                if any(item.created_at != outcome.produced_at for item in accepted_artifacts if item is not None):
                    raise ValueError("accepted Artifact must use Outcome.produced_at")
                if any(
                    item is not None
                    and (
                        item.kind is not ArtifactKind.AUDIT_BUNDLE
                        or item.created_at != record.processed_at
                        or item.created_by_job_id != job.job_id
                    )
                    for item in generated_artifacts
                ):
                    raise ValueError(
                        "generated Artifacts must be audit bundles created during processing"
                    )
                if record.created_job_id is not None:
                    created_job = self.jobs.get(record.created_job_id)
                    if created_job is None or created_job.created_at != outcome.produced_at:
                        raise ValueError("created Job must resolve and use Outcome.produced_at")

        for failure_record in self.execution_failure_records.values():
            job = self.jobs.get(failure_record.job_id)
            if job is None or job.runtime_epoch != failure_record.runtime_epoch:
                raise ValueError("ExecutionFailureRecord must match its Job runtime epoch")

        if self.case.generic_result is not None:
            generic = self.case.generic_result
            source_job = self.jobs.get(generic.source_job_id)
            source_outcome = self.outcomes.get(generic.source_outcome_id)
            processing = self.outcome_processing_records.get(generic.source_outcome_id)
            if (
                source_job is None
                or source_job.job_type is not JobType.DIAGNOSE
                or source_job.diagnosis_mode is not DiagnosisMode.GENERIC
                or source_job.generic_skill_name != generic.skill_name
                or source_job.status is not JobStatus.SUCCEEDED
                or source_outcome is None
                or source_outcome.job_id != source_job.job_id
                or source_outcome.job_type is not JobType.DIAGNOSE
                or source_outcome.result_type is not OutcomeResultType.COMPLETED
                or not isinstance(source_outcome.payload, GenericDiagnosisOutcome)
                or source_outcome.payload.status is not generic.status
                or source_outcome.payload.conclusion != generic.conclusion
                or source_outcome.payload.root_cause_analysis
                != generic.root_cause_analysis
                or source_outcome.payload.skill_name != generic.skill_name
                or source_outcome.produced_at != generic.occurred_at
                or processing is None
                or processing.job_id != source_job.job_id
                or processing.disposition is not OutcomeDisposition.APPLIED
            ):
                raise ValueError(
                    "generic_result must exactly bind its GENERIC Job and Outcome"
                )
        if self.case.status in {
            CaseStatus.RESOLVED,
            CaseStatus.PARTIALLY_RESOLVED,
        } and self.case.generic_result is None:
            assert self.case.final_result is not None
            producing_job = self.jobs.get(
                self.case.final_result.proposed_by_job_id
            )
            if producing_job is None or producing_job.job_type is not JobType.DIAGNOSE:
                raise ValueError("resolved Case requires a DIAGNOSE result Job")
            user_results = [
                artifact
                for artifact in self.artifacts.values()
                if artifact.kind is ArtifactKind.USER_RESULT
                and artifact.created_by_job_id == self.case.final_result.proposed_by_job_id
            ]
            if len(user_results) != 1:
                raise ValueError("resolved Case requires its accepted candidate USER_RESULT")
            result_archives = [
                artifact
                for artifact in self.artifacts.values()
                if artifact.kind is ArtifactKind.USER_RESULT_ARCHIVE
                and artifact.created_by_job_id
                == self.case.final_result.proposed_by_job_id
            ]
            if len(result_archives) != 1:
                raise ValueError(
                    "resolved Case requires its accepted candidate USER_RESULT_ARCHIVE"
                )
        if self.case.status is CaseStatus.UNRESOLVED and self.case.generic_result is None:
            assert self.case.unresolved_result is not None
            unresolved = self.case.unresolved_result
            artifact = self.artifacts.get(unresolved.audit_artifact_id)
            user_result_artifact = self.artifacts.get(
                unresolved.user_result_artifact_id
            )
            if artifact is None or artifact.kind is not ArtifactKind.AUDIT_BUNDLE:
                raise ValueError(
                    "UNRESOLVED Case requires its matching AUDIT_BUNDLE Artifact"
                )
            if (
                user_result_artifact is None
                or user_result_artifact.kind is not ArtifactKind.USER_RESULT
                or user_result_artifact.created_by_job_id != unresolved.source_job_id
            ):
                raise ValueError(
                    "UNRESOLVED Case requires its bound USER_RESULT Artifact"
                )
            source_user_results = [
                item.artifact_id
                for item in self.artifacts.values()
                if item.kind is ArtifactKind.USER_RESULT
                and item.created_by_job_id == unresolved.source_job_id
            ]
            source_archives = [
                item
                for item in self.artifacts.values()
                if item.kind is ArtifactKind.USER_RESULT_ARCHIVE
                and item.created_by_job_id == unresolved.source_job_id
            ]
            if source_user_results != [unresolved.user_result_artifact_id] or source_archives:
                raise ValueError(
                    "UNRESOLVED Case must bind its unique USER_RESULT and forbid an archive"
                )
            metadata = artifact.metadata
            assert isinstance(metadata, AuditBundleMetadata)
            if (
                unresolved.source_job_id != metadata.source_job_id
                or unresolved.source_outcome_id != metadata.source_outcome_id
                or any(ref not in self.evidence for ref in unresolved.evidence_refs)
            ):
                raise ValueError(
                    "UNRESOLVED result must match its audit metadata and Evidence"
                )
        return self


class StateFile(ContractModel):
    schema_version: Literal[5]
    contract_revision: Literal[CONTRACT_REVISION]
    generation: NonNegativeInt
    installation_id: OpaqueId
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    runtime_epochs: list[RuntimeEpochRecord]
    recovery_processing_records: dict[OpaqueId, RecoveryProcessingRecord]
    cases: dict[OpaqueId, CaseAggregate]
    idempotency_records: dict[str, IdempotencyRecord]

    @model_validator(mode="after")
    def validate_state_file(self) -> StateFile:
        if any(key != aggregate.case.case_id for key, aggregate in self.cases.items()):
            raise ValueError("cases map keys must equal Case.case_id")
        _unique([record.runtime_epoch for record in self.runtime_epochs], "runtime epoch IDs")
        _unique(
            [record.recovery_id for record in self.runtime_epochs],
            "runtime epoch recovery IDs",
        )
        if any(
            key != record.recovery_id
            for key, record in self.recovery_processing_records.items()
        ):
            raise ValueError(
                "recovery processing record map keys must equal recovery_id"
            )
        runtime_by_recovery_id = {
            record.recovery_id: record for record in self.runtime_epochs
        }
        if set(runtime_by_recovery_id) != set(self.recovery_processing_records):
            raise ValueError(
                "RuntimeEpochRecords and RecoveryProcessingRecords must form an exact recovery_id pair set"
            )
        for recovery_record in self.recovery_processing_records.values():
            runtime_record = runtime_by_recovery_id.get(recovery_record.recovery_id)
            if (
                runtime_record is None
                or runtime_record.runtime_epoch
                != recovery_record.current_runtime_epoch
            ):
                raise ValueError(
                    "RecoveryProcessingRecord must match its RuntimeEpochRecord"
                )
            if (
                runtime_record.recovery_completed_at
                != recovery_record.completed_at
            ):
                raise ValueError(
                    "recovery completion timestamps must match exactly"
                )
        if sum(
            record.completed_at is None
            for record in self.recovery_processing_records.values()
        ) > 1:
            raise ValueError("at most one recovery may be incomplete")
        if any(
            key != f"{record.operation}:{record.idempotency_key}"
            for key, record in self.idempotency_records.items()
        ):
            raise ValueError("idempotency record map keys must match operation and key")
        for field_name in (
            "jobs",
            "outcomes",
            "outcome_processing_records",
            "execution_failure_records",
            "attachments",
            "evidence",
            "artifacts",
        ):
            ids = [
                object_id
                for aggregate in self.cases.values()
                for object_id in getattr(aggregate, field_name)
            ]
            _unique(ids, f"globally unique {field_name} IDs")
        return self


class StateMutation(ContractModel):
    upsert_case: Case | None
    upsert_runtime_epoch_records: list[RuntimeEpochRecord]
    upsert_recovery_processing_records: list[RecoveryProcessingRecord]
    insert_jobs: list[Job]
    job_lifecycle_updates: list[JobLifecycleUpdate]
    insert_outcomes: list[JobOutcome]
    insert_outcome_processing_records: list[OutcomeProcessingRecord]
    insert_execution_failure_records: list[ExecutionFailureRecord]
    upsert_attachments: list[Attachment]
    insert_evidence: list[Evidence]
    insert_artifacts: list[Artifact]
    insert_idempotency_records: list[IdempotencyRecord]

    @model_validator(mode="after")
    def validate_mutation(self) -> StateMutation:
        identities = {
            "upsert_runtime_epoch_records": (
                self.upsert_runtime_epoch_records,
                lambda item: item.runtime_epoch,
            ),
            "upsert_recovery_processing_records": (
                self.upsert_recovery_processing_records,
                lambda item: item.recovery_id,
            ),
            "insert_jobs": (self.insert_jobs, lambda item: item.job_id),
            "job_lifecycle_updates": (
                self.job_lifecycle_updates,
                lambda item: item.job_id,
            ),
            "insert_outcomes": (self.insert_outcomes, lambda item: item.outcome_id),
            "insert_outcome_processing_records": (
                self.insert_outcome_processing_records,
                lambda item: item.outcome_id,
            ),
            "insert_execution_failure_records": (
                self.insert_execution_failure_records,
                lambda item: item.failure_id,
            ),
            "upsert_attachments": (
                self.upsert_attachments,
                lambda item: item.attachment_id,
            ),
            "insert_evidence": (self.insert_evidence, lambda item: item.evidence_id),
            "insert_artifacts": (self.insert_artifacts, lambda item: item.artifact_id),
            "insert_idempotency_records": (
                self.insert_idempotency_records,
                lambda item: (item.operation, item.idempotency_key),
            ),
        }
        for label, (items, identity) in identities.items():
            values = [identity(item) for item in items]
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must not repeat natural IDs")
        inserted_job_ids = {job.job_id for job in self.insert_jobs}
        if inserted_job_ids & {update.job_id for update in self.job_lifecycle_updates}:
            raise ValueError("a mutation cannot insert and lifecycle-update the same Job")
        case_ids = {
            item.case_id
            for collection in (
                self.insert_jobs,
                self.insert_outcomes,
                self.upsert_attachments,
                self.insert_evidence,
                self.insert_artifacts,
            )
            for item in collection
        }
        if self.upsert_case is not None:
            case_ids.add(self.upsert_case.case_id)
        if len(case_ids) > 1:
            raise ValueError("one StateMutation may touch at most one Case")
        return self


class CommitReceipt(ContractModel):
    generation: NonNegativeInt
    case_revision: PositiveInt | None


class JobSummary(ContractModel):
    job_id: OpaqueId
    job_type: JobType
    diagnosis_mode: DiagnosisMode | None
    status: JobStatus
    goal: NonEmptyText
    base_state_revision: PositiveInt
    created_at: UtcTimestamp
    started_at: UtcTimestamp | None
    finished_at: UtcTimestamp | None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> JobSummary:
        if self.status is JobStatus.PENDING and (
            self.started_at is not None or self.finished_at is not None
        ):
            raise ValueError("PENDING JobSummary forbids execution timestamps")
        if self.status is JobStatus.RUNNING and (
            self.started_at is None or self.finished_at is not None
        ):
            raise ValueError("RUNNING JobSummary requires started_at and forbids finished_at")
        if self.status in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        } and self.finished_at is None:
            raise ValueError("terminal JobSummary requires finished_at")
        return self


class ArtifactSummary(ContractModel):
    artifact_id: OpaqueId
    kind: ArtifactKind
    name: NonEmptyText
    content_type: ContentType
    resource_kind: ResourceKind
    size: NonNegativeInt
    sha256: Sha256
    created_by_job_id: OpaqueId
    created_at: UtcTimestamp
    downloadable: bool

    @model_validator(mode="after")
    def validate_downloadable_kind(self) -> ArtifactSummary:
        if self.kind is ArtifactKind.USER_RESULT and (
            self.resource_kind is not ResourceKind.FILE
            or self.content_type != "application/json"
        ):
            raise ValueError("USER_RESULT summary must describe an application/json FILE")
        if self.kind is ArtifactKind.USER_RESULT_ARCHIVE and (
            self.resource_kind is not ResourceKind.FILE
            or self.content_type != "application/zip"
        ):
            raise ValueError(
                "USER_RESULT_ARCHIVE summary must describe an application/zip FILE"
            )
        if self.kind is ArtifactKind.DIAGNOSTIC_EXPORT and not self.downloadable:
            raise ValueError("DIAGNOSTIC_EXPORT is always downloadable")
        if self.kind is ArtifactKind.LOGPARSE_RUN:
            if self.downloadable:
                raise ValueError("LOGPARSE_RUN is never downloadable")
            if (
                self.resource_kind is not ResourceKind.DIRECTORY
                or self.content_type
                != "application/vnd.problem-locator.logparse-run+directory"
            ):
                raise ValueError("LOGPARSE_RUN summary must use its fixed directory shape")
        if self.kind is ArtifactKind.AUDIT_BUNDLE and (
            self.resource_kind is not ResourceKind.FILE
            or self.content_type != "application/zip"
        ):
            raise ValueError(
                "AUDIT_BUNDLE summary must describe an application/zip FILE"
            )
        return self


class ArtifactView(ContractModel):
    artifact_id: OpaqueId
    kind: ArtifactKind
    name: NonEmptyText
    content_type: ContentType
    size: NonNegativeInt
    sha256: Sha256
    created_at: UtcTimestamp
    download_url: NonEmptyText


class CaseView(ContractModel):
    case_id: OpaqueId
    status: CaseStatus
    case_revision: PositiveInt
    raw_problem_text: NonEmptyText
    diagnosis_state_revision: PositiveInt
    problem_spec: ProblemSpec
    user_facts: list[DiagnosisItem]
    confirmed_facts: list[DiagnosisItem]
    open_questions: list[DiagnosisItem]
    pending_requirements: list[PendingRequirement]
    active_job: JobSummary | None
    selected_skill_ref: VersionedRef | None
    final_result: CandidateConclusion | None
    unresolved_result: UnresolvedResult | None = None
    generic_result: GenericResult | None = None
    failure: CaseFailure | None
    artifacts: list[ArtifactSummary]
    created_at: UtcTimestamp
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_view(self) -> CaseView:
        if (self.status is CaseStatus.FAILED) != (self.failure is not None):
            raise ValueError("failure must be present exactly for FAILED case views")
        if self.generic_result is not None:
            if self.status is not CaseStatus(self.generic_result.status.value):
                raise ValueError("generic_result status must equal CaseView status")
            if (
                self.final_result is not None
                or self.unresolved_result is not None
                or self.selected_skill_ref is not None
            ):
                raise ValueError("generic CaseView forbids specialized result fields")
        if self.status in {CaseStatus.RESOLVED, CaseStatus.PARTIALLY_RESOLVED}:
            if self.generic_result is None and (
                self.final_result is None
                or self.final_result.status is not CandidateStatus.ACCEPTED
            ):
                raise ValueError("resolved case views require an ACCEPTED final result")
            if self.generic_result is None and self.final_result is not None:
                expected = (
                    DiagnosisResolutionStatus.COMPLETE
                    if self.status is CaseStatus.RESOLVED
                    else DiagnosisResolutionStatus.PARTIAL
                )
                if self.final_result.resolution_status is not expected:
                    raise ValueError(
                        "final result resolution_status must equal CaseView status"
                    )
        elif self.final_result is not None:
            raise ValueError("non-resolved case views must have final_result=null")
        if self.status is CaseStatus.UNRESOLVED:
            if self.generic_result is None and self.unresolved_result is None:
                raise ValueError("UNRESOLVED case views require unresolved_result")
        elif self.unresolved_result is not None:
            raise ValueError("only UNRESOLVED case views may carry unresolved_result")
        if self.status in {CaseStatus.RUNNING, CaseStatus.REVIEWING}:
            if self.active_job is None:
                raise ValueError("RUNNING and REVIEWING CaseView require active_job")
            if self.active_job.status not in {JobStatus.PENDING, JobStatus.RUNNING}:
                raise ValueError("CaseView active_job must be PENDING or RUNNING")
            if self.status is CaseStatus.REVIEWING:
                if self.active_job.job_type is not JobType.REVIEW:
                    raise ValueError("REVIEWING CaseView requires a REVIEW Job")
            elif self.active_job.job_type is JobType.REVIEW:
                raise ValueError("RUNNING CaseView requires a ROUTE or DIAGNOSE Job")
        elif self.active_job is not None:
            raise ValueError("waiting and terminal/interrupted CaseView forbid active_job")
        all_items = self.user_facts + self.confirmed_facts + self.open_questions
        _unique([item.item_id for item in all_items], "CaseView DiagnosisItem IDs")
        if any(
            item.provenance.source_type is not DiagnosisProvenanceType.USER_INPUT
            for item in self.user_facts
        ):
            raise ValueError("CaseView user_facts require USER_INPUT provenance")
        if any(
            item.provenance.source_type is not DiagnosisProvenanceType.AGENT_OUTCOME
            for item in self.confirmed_facts + self.open_questions
        ):
            raise ValueError("CaseView Agent-derived items require AGENT_OUTCOME provenance")
        if any(not item.evidence_refs for item in self.confirmed_facts):
            raise ValueError("CaseView confirmed_facts must cite Evidence")
        if any(item.status is not DiagnosisItemStatus.ACTIVE for item in self.open_questions):
            raise ValueError("CaseView open_questions may contain only ACTIVE items")
        open_requirements = [
            item for item in self.pending_requirements if item.status is RequirementStatus.OPEN
        ]
        _unique(
            [item.requirement_id for item in self.pending_requirements],
            "CaseView requirement IDs",
        )
        _unique([item.name for item in open_requirements], "CaseView OPEN requirement names")
        if sum(item.kind is RequirementKind.ATTACHMENT for item in open_requirements) > 1:
            raise ValueError("CaseView may contain at most one OPEN ATTACHMENT requirement")
        if self.final_result is not None:
            mappings = self.final_result.completion_criteria_mapping
            criteria = self.problem_spec.completion_criteria
            if len(mappings) != len(criteria) or any(
                mapping.criterion_index != index or mapping.criterion != criterion
                for index, (mapping, criterion) in enumerate(zip(mappings, criteria, strict=True))
            ):
                raise ValueError("CaseView final result must cover its ProblemSpec criteria")
        if any(not artifact.downloadable for artifact in self.artifacts):
            raise ValueError("CaseView.artifacts may contain only downloadable summaries")
        _unique([artifact.artifact_id for artifact in self.artifacts], "CaseView artifact IDs")
        user_results = [
            artifact for artifact in self.artifacts if artifact.kind is ArtifactKind.USER_RESULT
        ]
        result_archives = [
            artifact
            for artifact in self.artifacts
            if artifact.kind is ArtifactKind.USER_RESULT_ARCHIVE
        ]
        if self.status in {
            CaseStatus.RESOLVED,
            CaseStatus.PARTIALLY_RESOLVED,
        } and self.generic_result is None:
            assert self.final_result is not None
            if len(user_results) != 1 or (
                user_results[0].created_by_job_id != self.final_result.proposed_by_job_id
            ):
                raise ValueError(
                    "resolved CaseView requires the accepted candidate's USER_RESULT"
                )
            if len(result_archives) != 1 or (
                result_archives[0].created_by_job_id
                != self.final_result.proposed_by_job_id
            ):
                raise ValueError(
                    "resolved CaseView requires the accepted candidate's USER_RESULT_ARCHIVE"
                )
        elif self.status is CaseStatus.UNRESOLVED and self.generic_result is None:
            assert self.unresolved_result is not None
            if (
                len(user_results) != 1
                or user_results[0].artifact_id
                != self.unresolved_result.user_result_artifact_id
                or result_archives
            ):
                raise ValueError(
                    "UNRESOLVED CaseView requires exactly its bound USER_RESULT and no archive"
                )
        elif user_results or result_archives:
            raise ValueError("non-terminal CaseView cannot expose user result Artifacts")
        audit_bundles = [
            artifact
            for artifact in self.artifacts
            if artifact.kind is ArtifactKind.AUDIT_BUNDLE
        ]
        if self.status is CaseStatus.UNRESOLVED and self.generic_result is None:
            assert self.unresolved_result is not None
            if (
                len(audit_bundles) != 1
                or audit_bundles[0].artifact_id
                != self.unresolved_result.audit_artifact_id
            ):
                raise ValueError(
                    "UNRESOLVED CaseView requires exactly its matching audit bundle"
                )
        elif audit_bundles:
            raise ValueError(
                "non-UNRESOLVED CaseView cannot expose an AUDIT_BUNDLE"
            )
        return self


WaitSeconds: TypeAlias = Annotated[int, Field(ge=0, le=MAX_WAIT_SECONDS, strict=True)]


class CreateCase(ContractModel):
    model_config = ConfigDict(json_schema_extra={"hash_excluded_fields": ["wait_seconds"]})

    idempotency_key: NonEmptyText
    raw_problem_text: NonEmptyText
    problem_spec: ProblemSpecInput
    initial_user_facts: Annotated[
        list[UserFactInput], Field(max_length=MAX_INITIAL_USER_FACTS)
    ]
    wait_seconds: WaitSeconds

    @model_validator(mode="after")
    def validate_facts(self) -> CreateCase:
        if len(self.initial_user_facts) > MAX_INITIAL_USER_FACTS:
            raise ValueError("initial_user_facts cannot exceed 64 entries")
        _unique([fact.name for fact in self.initial_user_facts], "initial user fact names")
        return self


class PrepareAttachment(ContractModel):
    model_config = ConfigDict(json_schema_extra={"hash_excluded_fields": []})

    idempotency_key: NonEmptyText
    case_id: OpaqueId
    expected_case_revision: PositiveInt
    name: NonEmptyText
    content_type: ContentType
    declared_size: NonNegativeInt | None = None
    declared_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_filename_and_content_type(self) -> PrepareAttachment:
        derive_attachment_filename_suffix(self.name, self.content_type)
        return self


class UploadAttachmentContent(ContractModel):
    model_config = ConfigDict(json_schema_extra={"hash_excluded_fields": ["byte_stream"]})

    idempotency_key: OpaqueId
    attachment_id: OpaqueId
    expected_content_type: ContentType
    expected_size: Annotated[
        int, Field(ge=0, le=MAX_ATTACHMENT_BYTES, strict=True)
    ]
    expected_sha256: Sha256
    byte_stream: Any

    @model_validator(mode="after")
    def validate_upload(self) -> UploadAttachmentContent:
        from .ports import BinaryStream

        if self.idempotency_key != self.attachment_id:
            raise ValueError("upload idempotency_key must equal attachment_id")
        if self.expected_size > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment exceeds the V1 byte limit")
        if not isinstance(self.byte_stream, BinaryStream):
            raise ValueError("byte_stream must implement the BinaryStream protocol")
        return self


class SubmitSupplement(ContractModel):
    model_config = ConfigDict(json_schema_extra={"hash_excluded_fields": ["wait_seconds"]})

    idempotency_key: NonEmptyText
    case_id: OpaqueId
    expected_case_revision: PositiveInt
    inputs: dict[ContractName, NonEmptyText]
    attachment_ids: list[OpaqueId]
    wait_seconds: WaitSeconds

    @model_validator(mode="after")
    def validate_supplement(self) -> SubmitSupplement:
        if not self.inputs and not self.attachment_ids:
            raise ValueError("supplement must contain inputs or attachment_ids")
        _unique(self.attachment_ids, "attachment_ids")
        return self


class ResumeCase(ContractModel):
    model_config = ConfigDict(json_schema_extra={"hash_excluded_fields": ["wait_seconds"]})

    idempotency_key: NonEmptyText
    case_id: OpaqueId
    expected_case_revision: PositiveInt
    wait_seconds: WaitSeconds


class CancelCase(ContractModel):
    model_config = ConfigDict(json_schema_extra={"hash_excluded_fields": []})

    idempotency_key: NonEmptyText
    case_id: OpaqueId
    expected_case_revision: PositiveInt


ApplicationCommand: TypeAlias = (
    CreateCase | PrepareAttachment | UploadAttachmentContent | SubmitSupplement | ResumeCase | CancelCase
)


class GetCase(ContractModel):
    case_id: OpaqueId
    wait_for_job_id: OpaqueId | None = None
    wait_seconds: WaitSeconds = 0


class ListArtifacts(ContractModel):
    case_id: OpaqueId
    include_internal: Annotated[bool, Field(strict=True)] = False


class OpenArtifact(ContractModel):
    case_id: OpaqueId
    artifact_id: OpaqueId


class ClaimJob(ContractModel):
    job_id: OpaqueId
    runtime_epoch: OpaqueId


class SubmitJobOutcome(ContractModel):
    job_outcome: JobOutcome
    outcome_file_ref: ExecutionFileRef

    @model_validator(mode="after")
    def validate_file_ref(self) -> SubmitJobOutcome:
        _validate_execution_file_ref(
            self.job_outcome,
            self.outcome_file_ref,
            job_id=self.job_outcome.job_id,
            filename="job_outcome.json",
        )
        return self


class ReportExecutionInfrastructureFailure(ContractModel):
    job_id: OpaqueId
    runtime_epoch: OpaqueId
    failure_id: OpaqueId
    execution_failure: ExecutionFailure

    @model_validator(mode="after")
    def validate_execution_record_failure(self) -> ReportExecutionInfrastructureFailure:
        if (
            self.execution_failure.stage is not ExecutionStage.EXECUTION_RECORD
            or self.execution_failure.code is not ErrorCode.EXECUTION_RECORD_FAILED
        ):
            raise ValueError(
                "reported infrastructure failure must be EXECUTION_RECORD_FAILED"
            )
        return self


class InterruptPreviousEpoch(ContractModel):
    current_runtime_epoch: OpaqueId
    recovery_id: OpaqueId


class UploadDescriptor(ContractModel):
    attachment_id: OpaqueId
    method: Literal["PUT"]
    url: NonEmptyText
    required_headers: dict[str, str | None]
    max_bytes: Literal[MAX_ATTACHMENT_BYTES]
    expires_at: None

    @model_validator(mode="after")
    def validate_descriptor(self) -> UploadDescriptor:
        expected_keys = {
            "Idempotency-Key",
            "Content-Type",
            "Content-Length",
            "X-Content-SHA256",
        }
        if set(self.required_headers) != expected_keys:
            raise ValueError("required_headers must contain exactly the four frozen keys")
        if self.required_headers["Idempotency-Key"] != self.attachment_id:
            raise ValueError("Idempotency-Key header must equal attachment_id")
        content_type = self.required_headers["Content-Type"]
        if content_type is None or re.fullmatch(CONTENT_TYPE_PATTERN, content_type) is None:
            raise ValueError("Content-Type descriptor header must be canonical")
        length = self.required_headers["Content-Length"]
        if length is not None and (
            re.fullmatch(r"(?:0|[1-9][0-9]*)", length) is None
            or int(length) > MAX_ATTACHMENT_BYTES
        ):
            raise ValueError("Content-Length must be a bounded decimal integer or null")
        digest = self.required_headers["X-Content-SHA256"]
        if digest is not None and re.fullmatch(SHA256_PATTERN, digest) is None:
            raise ValueError("X-Content-SHA256 must be a lowercase SHA-256 or null")
        return self


class ApplicationResponse(ContractModel):
    business_receipt: BusinessReceipt
    case_view: CaseView | None
    wait_timed_out: bool
    dispatch_pending: bool


class CaseQueryResponse(ContractModel):
    case_view: CaseView
    wait_timed_out: bool


class ArtifactListResponse(ContractModel):
    artifacts: list[ArtifactSummary]


class OpenArtifactResult(ContractModel):
    artifact: ArtifactSummary
    stream: Any

    @model_validator(mode="after")
    def validate_open_result(self) -> OpenArtifactResult:
        from .ports import BinaryStream

        if not self.artifact.downloadable:
            raise ValueError("open_artifact requires a downloadable ArtifactSummary")
        if not isinstance(self.stream, BinaryStream):
            raise ValueError("stream must implement the BinaryStream protocol")
        return self


class JobInstructionPayload(ContractModel):
    job_id: OpaqueId
    job_type: JobType
    goal: NonEmptyText
    base_state_revision: PositiveInt


def validate_job_instruction_for_job(
    instruction: JobInstructionPayload,
    job: Job,
) -> JobInstructionPayload:
    """Validate the frozen JOB_INSTRUCTION payload against its source Job."""

    if (
        instruction.job_id != job.job_id
        or instruction.job_type is not job.job_type
        or instruction.goal != job.goal
        or instruction.base_state_revision != job.base_state_revision
    ):
        raise ValueError("JOB_INSTRUCTION fields must exactly match the current Job")
    return instruction


class ContextSection(ContractModel):
    ordinal: NonNegativeInt
    kind: ContextSectionKind
    source_refs: list[OpaqueId]
    required: bool
    utf8_bytes: NonNegativeInt
    content_sha256: Sha256


class BoundedContext(ContractModel):
    job_id: OpaqueId
    job_type: JobType
    body: str
    sections: list[ContextSection]
    utf8_bytes: NonNegativeInt
    limit_bytes: PositiveInt
    body_sha256: Sha256

    @model_validator(mode="after")
    def validate_context(self) -> BoundedContext:
        encoded = self.body.encode("utf-8")
        if len(encoded) != self.utf8_bytes or sum(section.utf8_bytes for section in self.sections) != self.utf8_bytes:
            raise ValueError("bounded context byte accounting mismatch")
        if self.utf8_bytes > self.limit_bytes:
            raise ValueError("bounded context exceeds limit_bytes")
        if hashlib.sha256(encoded).hexdigest() != self.body_sha256:
            raise ValueError("body_sha256 mismatch")
        if [section.ordinal for section in self.sections] != list(range(len(self.sections))):
            raise ValueError("context section ordinals must be contiguous from zero")
        if self.limit_bytes != default_resource_limits(self.job_type).context_bytes:
            raise ValueError("limit_bytes must equal the frozen Job-role context limit")
        for section in self.sections:
            _unique(section.source_refs, "context section source_refs")
        required_kinds = {
            ContextSectionKind.JOB_INSTRUCTION,
            ContextSectionKind.RESOURCE_MANIFEST,
        }
        if any(
            sum(
                section.kind is kind and section.required
                for section in self.sections
            )
            != 1
            for kind in required_kinds
        ):
            raise ValueError(
                "JOB_INSTRUCTION and RESOURCE_MANIFEST must each appear once as required"
            )
        offset = 0
        for section in self.sections:
            section_bytes = encoded[offset : offset + section.utf8_bytes]
            if len(section_bytes) != section.utf8_bytes:
                raise ValueError("context section byte range exceeds the body")
            if hashlib.sha256(section_bytes).hexdigest() != section.content_sha256:
                raise ValueError("context section content_sha256 mismatch")
            offset += section.utf8_bytes
        return self


class ExecutionLogSinks(ContractModel):
    stdout: Any
    stderr: Any
    combined_limit_bytes: Literal[JOB_STDOUT_STDERR_BYTES]

    @model_validator(mode="after")
    def validate_sinks(self) -> ExecutionLogSinks:
        from .ports import AppendOnlyByteSink

        if not isinstance(self.stdout, AppendOnlyByteSink) or not isinstance(
            self.stderr, AppendOnlyByteSink
        ):
            raise ValueError("stdout and stderr must implement AppendOnlyByteSink")
        return self


class RuntimeExecutionReceipt(ContractModel):
    job_outcome: JobOutcome
    outcome_file_ref: ExecutionFileRef

    @model_validator(mode="after")
    def validate_file_ref(self) -> RuntimeExecutionReceipt:
        _validate_execution_file_ref(
            self.job_outcome,
            self.outcome_file_ref,
            job_id=self.job_outcome.job_id,
            filename="job_outcome.json",
        )
        return self


class PublishedJobReceipt(ContractModel):
    job: Job
    job_file_ref: ExecutionFileRef

    @model_validator(mode="after")
    def validate_file_ref(self) -> PublishedJobReceipt:
        if self.job.status is not JobStatus.PENDING:
            raise ValueError("published job.json must contain a PENDING Job")
        _validate_execution_file_ref(
            self.job,
            self.job_file_ref,
            job_id=self.job.job_id,
            filename="job.json",
        )
        return self


class ClaimReceipt(ContractModel):
    claimed: bool
    job: Job | None
    failure_applied: bool
    failure_code: ErrorCode | None

    @model_validator(mode="after")
    def validate_claim(self) -> ClaimReceipt:
        if self.claimed != (self.job is not None):
            raise ValueError("claimed must be true exactly when job is present")
        if self.failure_applied != (self.failure_code is not None):
            raise ValueError("failure_applied must be true exactly when failure_code is present")
        if self.claimed and self.failure_applied:
            raise ValueError("claim and failure application are mutually exclusive")
        if (
            self.failure_applied
            and self.failure_code is not ErrorCode.ASSET_VERSION_UNAVAILABLE
        ):
            raise ValueError(
                "failure_applied is reserved for ASSET_VERSION_UNAVAILABLE"
            )
        return self


class OutcomeReceipt(ContractModel):
    disposition: OutcomeDisposition
    case_view: CaseView | None


class FailureReceipt(ContractModel):
    failure_id: OpaqueId
    disposition: FailureReportDisposition
    case_view: CaseView | None


class RecoveryReceipt(ContractModel):
    recovery_id: OpaqueId
    interrupted_job_ids: list[OpaqueId]
    pending_job_ids: list[OpaqueId]

    @model_validator(mode="after")
    def validate_ids(self) -> RecoveryReceipt:
        for field_name in ("interrupted_job_ids", "pending_job_ids"):
            values = getattr(self, field_name)
            _unique(values, field_name)
            if values != sorted(values):
                raise ValueError(f"{field_name} must be sorted")
        if set(self.interrupted_job_ids) & set(self.pending_job_ids):
            raise ValueError(
                "interrupted_job_ids and pending_job_ids must be disjoint"
            )
        return self


class DispatchReceipt(ContractModel):
    job_id: OpaqueId
    accepted: bool
    duplicate: bool

    @model_validator(mode="after")
    def validate_flags(self) -> DispatchReceipt:
        if self.accepted and self.duplicate:
            raise ValueError("a dispatch cannot be both newly accepted and duplicate")
        return self


class CancelReceipt(ContractModel):
    job_id: OpaqueId
    signalled: bool


class ValidationIssue(ContractModel):
    code: NonEmptyText
    object_type: NonEmptyText
    object_id: OpaqueId | None
    field_path: NonEmptyText | None
    message: NonEmptyText


class StateExportObjectCounts(ContractModel):
    cases: NonNegativeInt
    jobs: NonNegativeInt
    outcomes: NonNegativeInt
    outcome_processing_records: NonNegativeInt
    execution_failure_records: NonNegativeInt
    attachments: NonNegativeInt
    evidence: NonNegativeInt
    artifacts: NonNegativeInt
    idempotency_records: NonNegativeInt
    runtime_epochs: NonNegativeInt
    recovery_processing_records: NonNegativeInt


def _state_object_counts(state: StateFile) -> StateExportObjectCounts:
    return StateExportObjectCounts(
        cases=len(state.cases),
        jobs=sum(len(aggregate.jobs) for aggregate in state.cases.values()),
        outcomes=sum(len(aggregate.outcomes) for aggregate in state.cases.values()),
        outcome_processing_records=sum(
            len(aggregate.outcome_processing_records)
            for aggregate in state.cases.values()
        ),
        execution_failure_records=sum(
            len(aggregate.execution_failure_records)
            for aggregate in state.cases.values()
        ),
        attachments=sum(
            len(aggregate.attachments) for aggregate in state.cases.values()
        ),
        evidence=sum(len(aggregate.evidence) for aggregate in state.cases.values()),
        artifacts=sum(len(aggregate.artifacts) for aggregate in state.cases.values()),
        idempotency_records=len(state.idempotency_records),
        runtime_epochs=len(state.runtime_epochs),
        recovery_processing_records=len(state.recovery_processing_records),
    )


class ValidationReport(ContractModel):
    valid: bool
    schema_version: NonNegativeInt | None
    contract_revision: NonEmptyText | None
    generation: NonNegativeInt | None
    object_counts: StateExportObjectCounts
    errors: list[ValidationIssue]

    @model_validator(mode="after")
    def validate_report(self) -> ValidationReport:
        if self.valid == bool(self.errors):
            raise ValueError("valid must be true exactly when errors is empty")
        expected = sorted(
            self.errors,
            key=lambda issue: (
                issue.object_type,
                issue.object_id or "",
                issue.field_path or "",
                issue.code,
                issue.message,
            ),
        )
        if self.errors != expected:
            raise ValueError("validation issues must use the frozen deterministic order")
        if self.valid and (
            self.schema_version != SCHEMA_VERSION
            or self.contract_revision != CONTRACT_REVISION
            or self.generation is None
        ):
            raise ValueError("valid reports require the frozen StateFile envelope values")
        return self


class ReadinessCheck(ContractModel):
    name: Literal["CONFIG", "INSTANCE_LOCK", "STATE", "DATA_DIRECTORIES", "RECOVERY"]
    passed: bool
    message: NonEmptyText | None


class ReadinessReport(ContractModel):
    ready: bool
    checks: list[ReadinessCheck]
    error: ApplicationError | None

    @model_validator(mode="after")
    def validate_report(self) -> ReadinessReport:
        expected_names = ["CONFIG", "INSTANCE_LOCK", "STATE", "DATA_DIRECTORIES", "RECOVERY"]
        if [check.name for check in self.checks] != expected_names:
            raise ValueError("readiness checks must contain the five frozen checks in order")
        if self.ready:
            if self.error is not None or any(not check.passed for check in self.checks):
                raise ValueError("ready report requires every check to pass and error=null")
        elif self.error is None:
            raise ValueError("non-ready report requires ApplicationError")
        return self


class StateExportResource(ContractModel):
    resource_kind: ResourceKind
    storage_key: RelativePosixPath
    size: NonNegativeInt
    sha256: Sha256


class StateExport(ContractModel):
    export_schema_version: Literal[5]
    schema_version: Literal[5]
    contract_revision: Literal[CONTRACT_REVISION]
    source_generation: NonNegativeInt
    installation_id: OpaqueId
    object_counts: StateExportObjectCounts
    state: StateFile
    resources: list[StateExportResource]

    @model_validator(mode="after")
    def validate_export(self) -> StateExport:
        if self.source_generation != self.state.generation or self.installation_id != self.state.installation_id:
            raise ValueError("StateExport envelope must match the embedded StateFile")
        if self.object_counts != _state_object_counts(self.state):
            raise ValueError("StateExport object_counts must match the embedded StateFile")
        keys = [resource.storage_key for resource in self.resources]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("StateExport resources must have unique ascending storage_key values")
        expected_by_key: dict[str, StateExportResource] = {}

        def include(resource: StateExportResource) -> None:
            existing = expected_by_key.get(resource.storage_key)
            if existing is not None and existing != resource:
                raise ValueError("one storage_key cannot describe conflicting resources")
            expected_by_key[resource.storage_key] = resource

        for aggregate in self.state.cases.values():
            for attachment in aggregate.attachments.values():
                if attachment.status is AttachmentStatus.READY:
                    assert (
                        attachment.storage_key is not None
                        and attachment.size is not None
                        and attachment.sha256 is not None
                    )
                    include(
                        StateExportResource(
                            resource_kind=ResourceKind.FILE,
                            storage_key=attachment.storage_key,
                            size=attachment.size,
                            sha256=attachment.sha256,
                        )
                    )
            for evidence in aggregate.evidence.values():
                if evidence.resource_ref is not None:
                    include(
                        StateExportResource.model_validate(
                            evidence.resource_ref.model_dump(mode="python")
                        )
                    )
            for artifact in aggregate.artifacts.values():
                include(
                    StateExportResource(
                        resource_kind=artifact.resource_kind,
                        storage_key=artifact.storage_key,
                        size=artifact.size,
                        sha256=artifact.sha256,
                    )
                )
        expected_resources = [expected_by_key[key] for key in sorted(expected_by_key)]
        if self.resources != expected_resources:
            raise ValueError("StateExport resources must exactly cover formal State resources")
        return self


class ContractManifestEntry(ContractModel):
    path: RelativePosixPath
    sha256: Sha256


class ContractManifest(ContractModel):
    schema_version: Literal[5]
    contract_revision: Literal[CONTRACT_REVISION]
    generator_version: NonEmptyText
    files: list[ContractManifestEntry]

    @model_validator(mode="after")
    def validate_files(self) -> ContractManifest:
        paths = [entry.path for entry in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("contract manifest paths must be unique and ascending")
        if any(path == "schemas/v2/contract-manifest.json" for path in paths):
            raise ValueError("contract manifest cannot hash itself")
        return self


class FixtureManifestEntry(ContractModel):
    path: RelativePosixPath
    purpose: NonEmptyText
    schema_ref: RelativePosixPath | None
    size: NonNegativeInt
    sha256: Sha256


class FixtureManifest(ContractModel):
    schema_version: Literal[1]
    owner_spec: Literal["S00", "S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"]
    root: RelativePosixPath
    files: list[FixtureManifestEntry]

    @model_validator(mode="after")
    def validate_files(self) -> FixtureManifest:
        paths = [entry.path for entry in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("fixture manifest paths must be unique and ascending")
        if "fixture-manifest.json" in paths:
            raise ValueError("fixture manifest cannot list itself")
        return self


class ExecutorSpec(ContractModel):
    model: Literal["gpt-5.6-sol"]
    reasoning_effort: Literal["ultra"]


class HandoffTestResult(ContractModel):
    command: NonEmptyText
    status: Literal["passed", "failed", "skipped"]
    summary: NonEmptyText | None = None


class DependencyRequest(ContractModel):
    package: NonEmptyText
    version: NonEmptyText
    purpose: NonEmptyText
    license_impact: NonEmptyText


class ContractChangeRequest(ContractModel):
    request_id: NonEmptyText
    requesting_spec: Literal["S00", "S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"]
    current_contract_revision: NonEmptyText
    problem: NonEmptyText
    proposed_change: NonEmptyText
    affected_types_or_codes: list[NonEmptyText]
    affected_specs: list[Literal["S00", "S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"]]
    compatibility: NonEmptyText
    fixture_and_test_changes: list[NonEmptyText]


class HandoffRecord(ContractModel):
    spec_id: Literal["S00", "S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"]
    title: NonEmptyText
    executor: ExecutorSpec
    # Historical S00-S08 handoffs are immutable r3 release records. New
    # contract revisions use a separate amendment/design record instead of
    # rewriting already signed handoff bytes.
    contract_revision: Literal["v1-contract-r3"]
    contract_base_commit: Annotated[str, StringConstraints(pattern=GIT_OBJECT_PATTERN, strict=True)]
    branch: Annotated[str, StringConstraints(pattern=r"^codex/.+", strict=True)]
    head_commit: Annotated[str, StringConstraints(pattern=GIT_OBJECT_PATTERN, strict=True)]
    scope_completed: list[NonEmptyText]
    changed_files: list[RelativePosixPath]
    fixtures_consumed: list[RelativePosixPath]
    fixtures_produced: list[RelativePosixPath]
    tests: list[HandoffTestResult]
    dependency_requests: list[DependencyRequest]
    contract_change_requests: list[ContractChangeRequest]
    known_limitations: list[NonEmptyText]
    risks: list[NonEmptyText]
    integration_notes: list[NonEmptyText]
    forbidden_scope_touched: bool

    @model_validator(mode="after")
    def validate_handoff(self) -> HandoffRecord:
        if self.contract_revision != "v1-contract-r3":
            raise ValueError("handoff contract_revision must equal v1-contract-r3")
        for name in ("changed_files", "fixtures_consumed", "fixtures_produced"):
            values = getattr(self, name)
            _unique(values, name)
            if values != sorted(values):
                raise ValueError(f"{name} must be sorted")
        return self


_CONTRACT_MODEL_TYPES = [
    value
    for value in globals().values()
    if isinstance(value, type)
    and issubclass(value, BaseModel)
    and value.__module__ == __name__
]
for _contract_model_type in _CONTRACT_MODEL_TYPES:
    _contract_model_type.model_rebuild()


__all__ = [model.__name__ for model in _CONTRACT_MODEL_TYPES] + [
    "ApplicationCommand",
    "ArtifactMetadata",
    "CanonicalJsonBytes",
    "CoordinatorPlanResult",
    "Confidence",
    "ContentType",
    "ContractName",
    "DescriptionText",
    "EvidenceLocator",
    "JsonPointer",
    "NonEmptyText",
    "NonNegativeInt",
    "OpaqueId",
    "OUTCOME_REJECTION_CODES",
    "OutcomePayload",
    "AgentOutcomePayload",
    "PositiveInt",
    "RelativePosixPath",
    "RequirementConstraints",
    "Sha256",
    "SkillName",
    "TriggerPayload",
    "UNTRUSTED_OUTCOME_REJECTION_CODES",
    "UtcTimestamp",
    "UserResultArchiveMetadata",
    "UserResultMetadata",
    "UserResultPayload",
    "WaitSeconds",
    "WorkspaceInputEntry",
    "default_resource_limits",
    "derive_attachment_content_type",
    "derive_attachment_filename_suffix",
    "finalize_unresolved_result",
    "validate_job_instruction_for_job",
    "review_required_evidence_refs",
    "validate_workspace_manifest_for_job",
    "workspace_attachment_relative_path",
]
