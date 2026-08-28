"""Deterministic bounded context construction for one frozen Job.

The builder consumes only S00 DTOs plus already-resolved asset entry text.  It
does not read repositories, choose asset versions, summarize content, or call a
model.  Workspace preparation is responsible for producing the manifest that
is passed here; the exact same canonical manifest bytes become the final
``RESOURCE_MANIFEST`` section.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from problem_locator.contracts.enums import (
    ContextSectionKind,
    JobType,
    RequirementStatus,
    ResourceKind,
)
from problem_locator.contracts.models import (
    AgentJobOutcomeDraftV2,
    AgentJobOutcome,
    BoundedContext,
    ContextSection,
    Evidence,
    Job,
    JobInstructionPayload,
    JobOutcome,
    MethodsReviewMethodCardV2,
    MethodsReviewerInputV2,
    UserResultPayload,
    WorkspaceEvidenceInput,
    WorkspaceInputManifest,
    WorkspacePreviousOutcomeInput,
    review_required_evidence_refs,
    validate_job_instruction_for_job,
    validate_workspace_manifest_for_job,
)
from problem_locator.contracts.methods_v2 import (
    MethodEvidenceGraphV2,
    MethodEvaluationPlanV2,
)
from problem_locator.contracts.serialization import canonical_json_bytes, schema_bundle_bytes


@dataclass(frozen=True, slots=True)
class ContextMaterials:
    """Resolved, immutable inputs needed to construct a Job context.

    Asset strings are the complete entry-file text for the exact versions
    frozen by the Job.  ROUTE uses ``skill_index``; DIAGNOSE and REVIEW use
    ``skill``.  Previous outcomes and Evidence must be in the Job's frozen
    reference order.
    """

    profile: str
    tool_bundle: str
    output_contract: str
    manifest: WorkspaceInputManifest
    previous_outcomes: tuple[JobOutcome, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    skill: str | None = None
    skill_index: str | None = None
    methods_evidence_graph: MethodEvidenceGraphV2 | None = None
    methods_evaluation_plan: MethodEvaluationPlanV2 | None = None
    methods_method_cards: tuple[MethodsReviewMethodCardV2, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "previous_outcomes", tuple(self.previous_outcomes))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "methods_method_cards", tuple(self.methods_method_cards))


class ContextLimitExceeded(ValueError):
    """Raised when the complete required section set exceeds the Job budget."""

    def __init__(self, observed: int, limit: int) -> None:
        self.observed = observed
        self.limit = limit
        super().__init__(
            f"required context uses {observed} UTF-8 bytes; limit is {limit}"
        )


def build_methods_reviewer_manifest_v2(
    job: Job,
    *,
    method_ids: tuple[str, ...],
) -> WorkspaceInputManifest:
    """Build the Candidate-free manifest seam for one Methods V2 REVIEW Job."""

    target = job.methods_review_target
    if (
        job.job_type is not JobType.REVIEW
        or target is None
        or job.review_target is not None
    ):
        raise ValueError("Methods Reviewer manifest requires a Candidate-free REVIEW Job")
    return WorkspaceInputManifest(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=JobType.REVIEW,
        logparse_tool_ref=None,
        logparse_product=None,
        entries=[],
        resolved_logparse_plan=None,
        review_subject=None,
        methods_reviewer_input=MethodsReviewerInputV2(
            schema_version=2,
            review_job_id=job.job_id,
            case_id=job.case_id,
            target=target,
            method_ids=method_ids,
        ),
    )


@dataclass(frozen=True, slots=True)
class _SectionDraft:
    kind: ContextSectionKind
    content: bytes
    source_refs: tuple[str, ...]
    required: bool


def _asset_text_bytes(value: str, label: str) -> bytes:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized or normalized.isspace():
        raise ValueError(f"{label} must not be empty")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 text") from exc
    return encoded.rstrip(b"\n") + b"\n"


_AGENT_OUTCOME_SCHEMA_TOKEN = "{{S00_AGENT_JOB_OUTCOME_SCHEMA_JSON}}"
_AGENT_OUTCOME_DRAFT_SCHEMA_TOKEN = (
    "{{S00_AGENT_JOB_OUTCOME_DRAFT_SCHEMA_JSON}}"
)
_USER_RESULT_SCHEMA_TOKEN = "{{S00_USER_RESULT_SCHEMA_JSON}}"
_OUTPUT_SCHEMA_TOKENS = {
    _AGENT_OUTCOME_DRAFT_SCHEMA_TOKEN: "agent-job-outcome-draft.schema.json",
    _AGENT_OUTCOME_SCHEMA_TOKEN: "agent-job-outcome.schema.json",
    _USER_RESULT_SCHEMA_TOKEN: "user-result.schema.json",
}
_OUTPUT_SCHEMA_MODELS = {
    "agent-job-outcome-draft.schema.json": AgentJobOutcomeDraftV2,
    "agent-job-outcome.schema.json": AgentJobOutcome,
    "user-result.schema.json": UserResultPayload,
}


def _output_contract_bytes(value: str) -> bytes:
    """Expand exact installed S00 schemas inside a trusted output contract."""

    if not isinstance(value, str):
        raise TypeError("output_contract must be text")
    present = {
        token: value.count(token)
        for token in _OUTPUT_SCHEMA_TOKENS
        if token in value
    }
    if not present:
        return _asset_text_bytes(value, "output_contract")
    if any(count != 1 for count in present.values()):
        raise ValueError("output_contract schema markers must be unique")
    if (
        _USER_RESULT_SCHEMA_TOKEN in present
        and _AGENT_OUTCOME_SCHEMA_TOKEN not in present
        and _AGENT_OUTCOME_DRAFT_SCHEMA_TOKEN not in present
    ):
        raise ValueError("the user-result schema requires the Agent Outcome schema")

    bundle = schema_bundle_bytes(
        {
            name: _OUTPUT_SCHEMA_MODELS[name]
            for name in sorted(set(_OUTPUT_SCHEMA_TOKENS[token] for token in present))
        }
    )
    expanded = value
    for token, name in _OUTPUT_SCHEMA_TOKENS.items():
        if token in present:
            expanded = expanded.replace(
                token,
                bundle[name].decode("utf-8").removesuffix("\n"),
            )
    return _asset_text_bytes(expanded, "output_contract")


def _json_section(value: object) -> bytes:
    return canonical_json_bytes(value)


def _frame(ordinal: int, draft: _SectionDraft) -> bytes:
    prefix = f"<<<SECTION {ordinal} {draft.kind.value}>>>\n".encode("ascii")
    content = draft.content.rstrip(b"\n") + b"\n"
    return prefix + content + b"<<<END SECTION>>>\n"


def _render(
    drafts: tuple[_SectionDraft, ...],
) -> tuple[bytes, list[ContextSection]]:
    framed: list[bytes] = []
    sections: list[ContextSection] = []
    for ordinal, draft in enumerate(drafts):
        section_bytes = _frame(ordinal, draft)
        framed.append(section_bytes)
        sections.append(
            ContextSection(
                ordinal=ordinal,
                kind=draft.kind,
                source_refs=list(draft.source_refs),
                required=draft.required,
                utf8_bytes=len(section_bytes),
                content_sha256=hashlib.sha256(section_bytes).hexdigest(),
            )
        )
    return b"".join(framed), sections


def _validate_evidence_manifest(
    evidence: tuple[Evidence, ...],
    manifest: WorkspaceInputManifest,
) -> None:
    entries = [
        entry
        for entry in manifest.entries
        if isinstance(entry, WorkspaceEvidenceInput)
    ]
    if len(entries) != len(evidence):
        raise ValueError("Workspace manifest Evidence entries are incomplete")
    for item, entry in zip(evidence, entries, strict=True):
        if (
            entry.resource_id != item.evidence_id
            or entry.source_type is not item.source_type
            or entry.source_ref != item.source_ref
            or entry.locator != item.locator
            or entry.summary != item.summary
            or entry.content_hash != item.content_hash
        ):
            raise ValueError("Workspace manifest Evidence metadata drifted")
        resource = item.resource_ref
        if resource is None:
            if any(
                value is not None
                for value in (
                    entry.relative_path,
                    entry.resource_kind,
                    entry.size,
                    entry.sha256,
                )
            ):
                raise ValueError("non-resource Evidence has materialization fields")
            continue
        leaf = "payload" if resource.resource_kind is ResourceKind.FILE else "tree"
        if (
            entry.relative_path != f"inputs/evidence/{item.evidence_id}/{leaf}"
            or entry.resource_kind is not resource.resource_kind
            or entry.size != resource.size
            or entry.sha256 != resource.sha256
        ):
            raise ValueError("Workspace manifest Evidence resource metadata drifted")


def _previous_outcome_bytes(
    outcomes: tuple[JobOutcome, ...],
    manifest: WorkspaceInputManifest,
) -> tuple[bytes, ...]:
    entries = [
        entry
        for entry in manifest.entries
        if isinstance(entry, WorkspacePreviousOutcomeInput)
    ]
    if len(entries) != len(outcomes):
        raise ValueError("Workspace manifest previous Outcome entries are incomplete")
    encoded: list[bytes] = []
    for outcome, entry in zip(outcomes, entries, strict=True):
        data = canonical_json_bytes(outcome)
        if (
            entry.resource_id != outcome.outcome_id
            or entry.source_job_id != outcome.job_id
            or entry.result_type is not outcome.result_type
            or entry.size != len(data)
            or entry.sha256 != hashlib.sha256(data).hexdigest()
        ):
            raise ValueError("Workspace manifest previous Outcome metadata drifted")
        encoded.append(data)
    return tuple(encoded)


class ContextBuilder:
    """Build one deterministic ``BoundedContext`` from frozen inputs."""

    def build(self, job: Job, materials: ContextMaterials) -> BoundedContext:
        if not isinstance(job, Job):
            raise TypeError("job must be the frozen S00 Job DTO")
        if not isinstance(materials, ContextMaterials):
            raise TypeError("materials must be ContextMaterials")
        if not isinstance(materials.manifest, WorkspaceInputManifest):
            raise TypeError("manifest must be the frozen S00 WorkspaceInputManifest DTO")

        validate_workspace_manifest_for_job(materials.manifest, job)
        self._validate_role_materials(job, materials)
        self._validate_order_and_ownership(job, materials)
        _validate_evidence_manifest(materials.evidence, materials.manifest)
        previous_bytes = _previous_outcome_bytes(
            materials.previous_outcomes,
            materials.manifest,
        )

        instruction = validate_job_instruction_for_job(
            JobInstructionPayload(
                job_id=job.job_id,
                job_type=job.job_type,
                goal=job.goal,
                base_state_revision=job.base_state_revision,
            ),
            job,
        )
        context_snapshot = job.context_snapshot
        if job.job_type is JobType.REVIEW:
            # Independent review receives the immutable user facts and fixed
            # Candidate, but not the Specialist's intermediate narrative or
            # hypotheses.  Verified mechanical facts arrive separately in the
            # server-built ReviewSubject below.
            hidden: dict[str, object] = {
                "confirmed_facts": [],
                "active_hypotheses": [],
                "rejected_hypotheses": [],
                "open_questions": [],
            }
            if job.methods_review_target is not None:
                hidden.update(
                    {
                        "pending_requirements": [],
                        "evidence_refs": [],
                        "candidate_conclusion": None,
                    }
                )
            context_snapshot = context_snapshot.model_copy(update=hidden)

        open_requirements = [
            requirement
            for requirement in context_snapshot.pending_requirements
            if requirement.status is RequirementStatus.OPEN
        ]

        prefix = [
            _SectionDraft(
                ContextSectionKind.PROFILE,
                _asset_text_bytes(materials.profile, "profile"),
                (),
                True,
            ),
            self._skill_section(job, materials),
            _SectionDraft(
                ContextSectionKind.TOOL_BUNDLE,
                _asset_text_bytes(materials.tool_bundle, "tool_bundle"),
                (),
                True,
            ),
            _SectionDraft(
                ContextSectionKind.JOB_INSTRUCTION,
                _json_section(instruction),
                (job.job_id,),
                True,
            ),
            _SectionDraft(
                ContextSectionKind.CONTEXT_SNAPSHOT,
                _json_section(context_snapshot),
                (job.job_id,),
                True,
            ),
            _SectionDraft(
                ContextSectionKind.OPEN_REQUIREMENTS,
                _json_section(open_requirements),
                tuple(requirement.requirement_id for requirement in open_requirements),
                True,
            ),
        ]
        if job.job_type is JobType.REVIEW:
            if job.methods_review_target is not None:
                reviewer_input = materials.manifest.methods_reviewer_input
                graph = materials.methods_evidence_graph
                plan = materials.methods_evaluation_plan
                if reviewer_input is None or graph is None or plan is None:
                    raise ValueError(
                        "Methods V2 REVIEW context requires Graph, Plan, method cards, and target"
                    )
                review_content: object = {
                    "schema_version": 2,
                    "target": reviewer_input.target.model_dump(
                        mode="json", exclude={"request_json"}
                    ),
                    "request": json.loads(reviewer_input.target.request_json),
                    "evidence_graph": graph.model_dump(mode="json"),
                    "evaluation_plan": plan.model_dump(mode="json"),
                    "method_cards": [
                        item.model_dump(mode="json")
                        for item in materials.methods_method_cards
                    ],
                }
                review_source_refs = (
                    reviewer_input.target.evaluation_id,
                )
            else:
                review_subject = materials.manifest.review_subject
                if review_subject is None:
                    raise ValueError("REVIEW context requires its server-built subject")
                review_content = review_subject
                review_source_refs = (review_subject.candidate.conclusion_id,)
            prefix.append(
                _SectionDraft(
                    ContextSectionKind.REVIEW_TARGET,
                    _json_section(review_content),
                    review_source_refs,
                    True,
                )
            )
        output_contract = _SectionDraft(
            ContextSectionKind.OUTPUT_CONTRACT,
            _output_contract_bytes(materials.output_contract),
            (),
            True,
        )

        previous = tuple(
            _SectionDraft(
                ContextSectionKind.PREVIOUS_OUTCOME,
                data,
                (outcome.outcome_id,),
                True,
            )
            for outcome, data in zip(
                materials.previous_outcomes,
                previous_bytes,
                strict=True,
            )
        )
        required_evidence_ids = self._required_evidence_ids(job)
        evidence_drafts = tuple(
            _SectionDraft(
                ContextSectionKind.EVIDENCE,
                _json_section(item),
                (item.evidence_id,),
                item.evidence_id in required_evidence_ids,
            )
            for item in materials.evidence
        )
        manifest = _SectionDraft(
            ContextSectionKind.RESOURCE_MANIFEST,
            canonical_json_bytes(materials.manifest),
            (job.job_id,),
            True,
        )

        selected = {
            draft.source_refs[0]
            for draft in evidence_drafts
            if draft.required
        }
        required_drafts = self._ordered_drafts(
            tuple(prefix),
            previous,
            evidence_drafts,
            selected,
            output_contract,
            manifest,
        )
        required_body, _ = _render(required_drafts)
        limit = job.resource_limits.context_bytes
        if len(required_body) > limit:
            raise ContextLimitExceeded(len(required_body), limit)

        for draft in evidence_drafts:
            evidence_id = draft.source_refs[0]
            if evidence_id in selected:
                continue
            trial = set(selected)
            trial.add(evidence_id)
            trial_drafts = self._ordered_drafts(
                tuple(prefix),
                previous,
                evidence_drafts,
                trial,
                output_contract,
                manifest,
            )
            trial_body, _ = _render(trial_drafts)
            if len(trial_body) <= limit:
                selected = trial

        final_drafts = self._ordered_drafts(
            tuple(prefix),
            previous,
            evidence_drafts,
            selected,
            output_contract,
            manifest,
        )
        body_bytes, sections = _render(final_drafts)
        return BoundedContext(
            job_id=job.job_id,
            job_type=job.job_type,
            body=body_bytes.decode("utf-8"),
            sections=sections,
            utf8_bytes=len(body_bytes),
            limit_bytes=limit,
            body_sha256=hashlib.sha256(body_bytes).hexdigest(),
        )

    @staticmethod
    def _validate_role_materials(job: Job, materials: ContextMaterials) -> None:
        if job.job_type is JobType.ROUTE:
            if materials.skill is not None or materials.skill_index is None:
                raise ValueError("ROUTE context requires only skill_index")
            return
        if materials.skill is None or materials.skill_index is not None:
            raise ValueError("DIAGNOSE/REVIEW context requires only skill")
        target = job.methods_review_target
        if target is None:
            if (
                materials.methods_evidence_graph is not None
                or materials.methods_evaluation_plan is not None
                or materials.methods_method_cards
            ):
                raise ValueError(
                    "only Methods V2 REVIEW context may carry Graph, Plan, or method cards"
                )
            return
        reviewer_input = materials.manifest.methods_reviewer_input
        graph = materials.methods_evidence_graph
        plan = materials.methods_evaluation_plan
        cards = materials.methods_method_cards
        if reviewer_input is None or graph is None or plan is None or not cards:
            raise ValueError(
                "Methods V2 REVIEW context requires Graph, Plan, and method cards"
            )
        method_ids = tuple(item.method_id for item in cards)
        planned_method_ids = tuple(item.method_id for item in plan.evaluations)
        if (
            reviewer_input.target != target
            or graph.graph_ref != target.graph_ref
            or plan.plan_ref != target.plan_ref
            or plan.evidence_graph_ref != graph.graph_ref
            or graph.skill_sha256 != target.skill_ref.content_hash
            or plan.skill_sha256 != target.skill_ref.content_hash
            or reviewer_input.method_ids != planned_method_ids
            or method_ids != planned_method_ids
        ):
            raise ValueError(
                "Methods V2 REVIEW context inputs do not match one Graph/Plan/Skill method set"
            )

    @staticmethod
    def _validate_order_and_ownership(job: Job, materials: ContextMaterials) -> None:
        expected_previous = () if job.job_type is JobType.REVIEW else tuple(
            job.previous_outcome_refs
        )
        if tuple(outcome.outcome_id for outcome in materials.previous_outcomes) != expected_previous:
            raise ValueError(
                "previous outcomes must follow the role-specific frozen exposure"
            )
        if any(outcome.case_id != job.case_id for outcome in materials.previous_outcomes):
            raise ValueError("previous outcomes must belong to the Job Case")
        if tuple(item.evidence_id for item in materials.evidence) != tuple(
            job.evidence_refs
        ):
            raise ValueError("Evidence must follow Job.evidence_refs")
        if any(item.case_id != job.case_id for item in materials.evidence):
            raise ValueError("Evidence must belong to the Job Case")

    @staticmethod
    def _skill_section(job: Job, materials: ContextMaterials) -> _SectionDraft:
        if job.job_type is JobType.ROUTE:
            assert materials.skill_index is not None
            return _SectionDraft(
                ContextSectionKind.SKILL_INDEX,
                _asset_text_bytes(materials.skill_index, "skill_index"),
                (),
                True,
            )
        assert materials.skill is not None
        return _SectionDraft(
            ContextSectionKind.SKILL,
            _asset_text_bytes(materials.skill, "skill"),
            (),
            True,
        )

    @staticmethod
    def _required_evidence_ids(job: Job) -> frozenset[str]:
        if job.job_type is not JobType.REVIEW:
            return frozenset()
        if job.methods_review_target is not None:
            return frozenset()
        candidate = job.context_snapshot.candidate_conclusion
        if candidate is None:
            raise ValueError("REVIEW context requires its fixed candidate")
        return frozenset(review_required_evidence_refs(candidate))

    @staticmethod
    def _ordered_drafts(
        prefix: tuple[_SectionDraft, ...],
        previous: tuple[_SectionDraft, ...],
        evidence: tuple[_SectionDraft, ...],
        selected_evidence_ids: set[str],
        output_contract: _SectionDraft,
        manifest: _SectionDraft,
    ) -> tuple[_SectionDraft, ...]:
        selected = tuple(
            draft
            for draft in evidence
            if draft.source_refs[0] in selected_evidence_ids
        )
        return prefix + previous + selected + (output_contract, manifest)


def build_bounded_context(job: Job, materials: ContextMaterials) -> BoundedContext:
    """Convenience function for callers that do not need a builder instance."""

    return ContextBuilder().build(job, materials)


__all__ = [
    "ContextBuilder",
    "ContextLimitExceeded",
    "ContextMaterials",
    "build_bounded_context",
    "build_methods_reviewer_manifest_v2",
]
