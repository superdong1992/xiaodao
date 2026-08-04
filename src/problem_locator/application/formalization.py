"""Pure proposal formalization and TransitionPlan application helpers.

The application service owns the mechanical work that starts after S01 has
accepted a plan: proposal placeholders become deterministic IDs, accepted
state deltas are applied in the frozen order, explicit field mutations are
honoured, and a ``JobSpec`` becomes an immutable pending ``Job``.  Nothing in
this module publishes resources or reads a repository.  Published
``ResourceRef`` values and deterministic IDs are supplied by the caller.

All failures raised here are internal invariant failures.  The command service
maps them to the frozen S00 failure contract at its orchestration boundary.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import TypeVar

from problem_locator.contracts.enums import (
    ArtifactKind,
    CandidateMutationAction,
    CandidateStatus,
    DiagnosisItemStatus,
    FieldUpdateAction,
    JobStatus,
    RequirementStatus,
    ResourceType,
)
from problem_locator.contracts.models import (
    Artifact,
    ArtifactProposal,
    CandidateConclusion,
    CandidateConclusionDraft,
    CandidateMutation,
    CandidateTarget,
    CaseFailure,
    CaseFailureUpdate,
    CompletionCriterionMapping,
    ContextSnapshot,
    DiagnosisItem,
    DiagnosisItemChange,
    DiagnosisItemDraft,
    DiagnosisState,
    DiagnosisStateDelta,
    Evidence,
    EvidenceBinding,
    EvidenceProposal,
    Job,
    JobSpec,
    PendingRequirement,
    PlannedResourceBinding,
    PlannedResourceTarget,
    ResourceRef,
    ReviewTargetBinding,
    SelectedSkillUpdate,
    VersionedRef,
)
from problem_locator.contracts.outcomes import apply_problem_spec_patch
from problem_locator.contracts.ports import ContextSnapshotProjector
from problem_locator.contracts.serialization import canonical_json_sha256


_T = TypeVar("_T")


def _stable_unique(values: Sequence[_T]) -> list[_T]:
    result: list[_T] = []
    seen: set[_T] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _unique(values: Sequence[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} contains duplicate values")


def _require(mapping: Mapping[str, _T], key: str, label: str) -> _T:
    try:
        return mapping[key]
    except KeyError as exc:
        raise ValueError(f"unresolved {label}: {key}") from exc


def _index_proposals(
    proposals: Sequence[_T],
    *,
    label: str,
) -> dict[str, _T]:
    indexed: dict[str, _T] = {}
    for proposal in proposals:
        key = getattr(proposal, "proposal_key", None)
        if not isinstance(key, str) or not key:
            raise ValueError(f"{label} has no valid proposal_key")
        if key in indexed:
            raise ValueError(f"duplicate {label} proposal_key: {key}")
        indexed[key] = proposal
    return indexed


def _candidate_target(candidate: CandidateConclusion) -> CandidateTarget:
    return CandidateTarget(
        candidate_conclusion_id=candidate.conclusion_id,
        candidate_revision=candidate.revision,
        candidate_content_hash=candidate.content_hash,
    )


def _target_matches(candidate: CandidateConclusion, target: CandidateTarget) -> bool:
    return _candidate_target(candidate) == target


def resolve_evidence_binding(
    binding: EvidenceBinding,
    *,
    existing_evidence_ids: Collection[str],
    evidence_ids_by_proposal_key: Mapping[str, str],
) -> str:
    """Resolve one frozen EvidenceBinding without accepting implicit inputs."""

    if binding.existing_evidence_id is not None:
        if binding.existing_evidence_id not in existing_evidence_ids:
            raise ValueError(
                f"unknown existing Evidence ID: {binding.existing_evidence_id}"
            )
        return binding.existing_evidence_id
    if binding.evidence_proposal_key is None:
        raise ValueError("EvidenceBinding has no populated branch")
    return _require(
        evidence_ids_by_proposal_key,
        binding.evidence_proposal_key,
        "Evidence proposal binding",
    )


def resolve_planned_resource_binding(
    binding: PlannedResourceBinding,
    *,
    existing_resource_ids: Collection[str],
    resource_ids_by_proposal_key: Mapping[str, str],
) -> str:
    """Resolve an Evidence or Artifact placeholder from a JobSpec."""

    if binding.existing_resource_id is not None:
        if binding.existing_resource_id not in existing_resource_ids:
            raise ValueError(
                f"unknown existing resource ID: {binding.existing_resource_id}"
            )
        return binding.existing_resource_id
    if binding.accepted_proposal_key is None:
        raise ValueError("PlannedResourceBinding has no populated branch")
    return _require(
        resource_ids_by_proposal_key,
        binding.accepted_proposal_key,
        "accepted resource proposal binding",
    )


def resolve_review_target_binding(
    binding: ReviewTargetBinding,
    *,
    existing_candidate: CandidateConclusion | None,
    candidates_by_proposal_key: Mapping[str, CandidateConclusion],
) -> CandidateTarget:
    """Resolve a review target while checking the complete ID/revision/hash."""

    if binding.existing_candidate_target is not None:
        if existing_candidate is None or not _target_matches(
            existing_candidate, binding.existing_candidate_target
        ):
            raise ValueError("existing review target does not match the candidate")
        return binding.existing_candidate_target.model_copy(deep=True)
    if binding.accepted_candidate_proposal_key is None:
        raise ValueError("ReviewTargetBinding has no populated branch")
    candidate = _require(
        candidates_by_proposal_key,
        binding.accepted_candidate_proposal_key,
        "accepted candidate proposal binding",
    )
    return _candidate_target(candidate)


def _resource_matches_staged(
    resource_ref: ResourceRef,
    *,
    planned_target: PlannedResourceTarget,
    case_id: str,
    resource_type: ResourceType,
    resource_id: str,
    resource_kind: object,
    size: int,
    sha256: str,
) -> bool:
    return (
        planned_target.case_id == case_id
        and planned_target.resource_type is resource_type
        and planned_target.resource_id == resource_id
        and planned_target.resource_kind == resource_kind
        and planned_target.size == size
        and planned_target.sha256 == sha256
        and resource_ref.storage_key == planned_target.final_storage_key
        and resource_ref.resource_kind == resource_kind
        and resource_ref.size == size
        and resource_ref.sha256 == sha256
    )


def formalize_accepted_artifacts(
    proposals: Sequence[ArtifactProposal],
    accepted_proposal_keys: Sequence[str],
    *,
    case_id: str,
    created_by_job_id: str,
    artifact_ids_by_proposal_key: Mapping[str, str],
    planned_targets_by_proposal_key: Mapping[str, PlannedResourceTarget],
    published_resources_by_proposal_key: Mapping[str, ResourceRef],
    occurred_at: str,
) -> dict[str, Artifact]:
    """Build formal Artifact metadata from already-published resources."""

    keys = list(accepted_proposal_keys)
    _unique(keys, "accepted Artifact proposal keys")
    indexed = _index_proposals(proposals, label="Artifact")
    result: dict[str, Artifact] = {}
    used_ids: set[str] = set()
    for key in keys:
        proposal = _require(indexed, key, "accepted Artifact proposal")
        artifact_id = _require(
            artifact_ids_by_proposal_key, key, "deterministic Artifact ID"
        )
        if artifact_id in used_ids:
            raise ValueError("deterministic Artifact IDs are not unique")
        used_ids.add(artifact_id)
        resource_ref = _require(
            published_resources_by_proposal_key,
            key,
            "published Artifact ResourceRef",
        )
        planned_target = _require(
            planned_targets_by_proposal_key,
            key,
            "planned Artifact resource target",
        )
        if not _resource_matches_staged(
            resource_ref,
            planned_target=planned_target,
            case_id=case_id,
            resource_type=ResourceType.ARTIFACT,
            resource_id=artifact_id,
            resource_kind=proposal.resource_kind,
            size=proposal.size,
            sha256=proposal.sha256,
        ):
            raise ValueError(
                f"published Artifact resource does not match proposal: {key}"
            )
        result[key] = Artifact(
            artifact_id=artifact_id,
            case_id=case_id,
            kind=proposal.artifact_kind,
            name=proposal.name,
            content_type=proposal.content_type,
            resource_kind=proposal.resource_kind,
            size=proposal.size,
            sha256=proposal.sha256,
            storage_key=resource_ref.storage_key,
            metadata=proposal.metadata,
            created_by_job_id=created_by_job_id,
            created_at=occurred_at,
        )
    return result


def formalize_accepted_evidence(
    proposals: Sequence[EvidenceProposal],
    accepted_proposal_keys: Sequence[str],
    *,
    case_id: str,
    evidence_ids_by_proposal_key: Mapping[str, str],
    existing_source_refs: Collection[str],
    artifacts_by_proposal_key: Mapping[str, Artifact],
    planned_targets_by_proposal_key: Mapping[str, PlannedResourceTarget],
    published_resources_by_proposal_key: Mapping[str, ResourceRef],
    occurred_at: str,
) -> dict[str, Evidence]:
    """Build formal Evidence and resolve LOGPARSE Artifact source bindings."""

    keys = list(accepted_proposal_keys)
    _unique(keys, "accepted Evidence proposal keys")
    indexed = _index_proposals(proposals, label="Evidence")
    result: dict[str, Evidence] = {}
    used_ids: set[str] = set()
    for key in keys:
        proposal = _require(indexed, key, "accepted Evidence proposal")
        evidence_id = _require(
            evidence_ids_by_proposal_key, key, "deterministic Evidence ID"
        )
        if evidence_id in used_ids:
            raise ValueError("deterministic Evidence IDs are not unique")
        used_ids.add(evidence_id)

        source_binding = proposal.source_binding
        if source_binding.existing_source_ref is not None:
            if source_binding.existing_source_ref not in existing_source_refs:
                raise ValueError(
                    "Evidence existing source_ref is not part of the validated case"
                )
            source_ref = source_binding.existing_source_ref
        elif source_binding.artifact_proposal_key is not None:
            source_artifact = _require(
                artifacts_by_proposal_key,
                source_binding.artifact_proposal_key,
                "Evidence Artifact source proposal binding",
            )
            if (
                source_artifact.case_id != case_id
                or source_artifact.kind is not ArtifactKind.LOGPARSE_RUN
            ):
                raise ValueError(
                    "LOGPARSE Evidence source must be an accepted LOGPARSE_RUN Artifact"
                )
            source_ref = source_artifact.artifact_id
        else:
            raise ValueError("EvidenceSourceBinding has no populated branch")

        resource_ref: ResourceRef | None
        if proposal.staged_resource_ref is None:
            if key in published_resources_by_proposal_key:
                raise ValueError(
                    f"non-resource Evidence unexpectedly has a ResourceRef: {key}"
                )
            resource_ref = None
        else:
            resource_ref = _require(
                published_resources_by_proposal_key,
                key,
                "published Evidence ResourceRef",
            )
            planned_target = _require(
                planned_targets_by_proposal_key,
                key,
                "planned Evidence resource target",
            )
            staged = proposal.staged_resource_ref
            if not _resource_matches_staged(
                resource_ref,
                planned_target=planned_target,
                case_id=case_id,
                resource_type=ResourceType.EVIDENCE,
                resource_id=evidence_id,
                resource_kind=staged.resource_kind,
                size=staged.size,
                sha256=staged.sha256,
            ):
                raise ValueError(
                    f"published Evidence resource does not match proposal: {key}"
                )

        result[key] = Evidence(
            evidence_id=evidence_id,
            case_id=case_id,
            source_type=proposal.source_type,
            source_ref=source_ref,
            locator=proposal.locator,
            summary=proposal.summary,
            collected_at=occurred_at,
            content_hash=proposal.content_hash,
            resource_ref=resource_ref,
        )
    return result


def _resolve_evidence_bindings(
    bindings: Sequence[EvidenceBinding],
    *,
    existing_evidence_ids: Collection[str],
    evidence_ids_by_proposal_key: Mapping[str, str],
) -> list[str]:
    return _stable_unique(
        [
            resolve_evidence_binding(
                binding,
                existing_evidence_ids=existing_evidence_ids,
                evidence_ids_by_proposal_key=evidence_ids_by_proposal_key,
            )
            for binding in bindings
        ]
    )


def formalize_accepted_candidate(
    draft: CandidateConclusionDraft | None,
    accepted_proposal_key: str | None,
    *,
    current_candidate: CandidateConclusion | None,
    problem_completion_criteria: Sequence[str],
    existing_evidence_ids: Collection[str],
    evidence_ids_by_proposal_key: Mapping[str, str],
    candidate_ids_by_proposal_key: Mapping[str, str],
    proposed_by_job_id: str,
) -> CandidateConclusion | None:
    """Resolve a complete candidate and compute only the frozen hash preimage."""

    if accepted_proposal_key is None:
        return None
    if draft is None or draft.proposal_key != accepted_proposal_key:
        raise ValueError("accepted candidate proposal does not match the Outcome draft")

    if draft.existing_conclusion_id is None:
        conclusion_id = _require(
            candidate_ids_by_proposal_key,
            accepted_proposal_key,
            "deterministic Candidate ID",
        )
        revision = 1
    else:
        if (
            current_candidate is None
            or current_candidate.conclusion_id != draft.existing_conclusion_id
        ):
            raise ValueError("candidate draft references a non-current conclusion")
        conclusion_id = current_candidate.conclusion_id
        revision = current_candidate.revision + 1

    supporting_evidence_refs = _resolve_evidence_bindings(
        draft.supporting_evidence_bindings,
        existing_evidence_ids=existing_evidence_ids,
        evidence_ids_by_proposal_key=evidence_ids_by_proposal_key,
    )
    criteria = list(problem_completion_criteria)
    if len(draft.completion_criteria_mapping) != len(criteria):
        raise ValueError("candidate mapping does not cover every completion criterion")

    mappings: list[CompletionCriterionMapping] = []
    for index, entry in enumerate(draft.completion_criteria_mapping):
        if entry.criterion_index != index or entry.criterion != criteria[index]:
            raise ValueError(
                "candidate mapping does not exactly match the target ProblemSpec"
            )
        mappings.append(
            CompletionCriterionMapping(
                criterion_index=entry.criterion_index,
                criterion=entry.criterion,
                satisfied=entry.satisfied,
                evidence_refs=_resolve_evidence_bindings(
                    entry.evidence_bindings,
                    existing_evidence_ids=existing_evidence_ids,
                    evidence_ids_by_proposal_key=evidence_ids_by_proposal_key,
                ),
                explanation=entry.explanation,
            )
        )

    preimage = {
        "statement": draft.statement,
        "supporting_evidence_refs": supporting_evidence_refs,
        "completion_criteria_mapping": [
            mapping.model_dump(mode="json") for mapping in mappings
        ],
    }
    return CandidateConclusion(
        conclusion_id=conclusion_id,
        revision=revision,
        content_hash=canonical_json_sha256(preimage),
        statement=draft.statement,
        supporting_evidence_refs=supporting_evidence_refs,
        completion_criteria_mapping=mappings,
        proposed_by_job_id=proposed_by_job_id,
        status=CandidateStatus.PROPOSED,
    )


def apply_selected_skill_update(
    current: VersionedRef | None,
    update: SelectedSkillUpdate | None,
) -> VersionedRef | None:
    """Apply only the selected-skill mutation explicitly present in a plan."""

    if update is None:
        return current.model_copy(deep=True) if current is not None else None
    if update.action is FieldUpdateAction.CLEAR:
        if update.value is not None:
            raise ValueError("selected-skill CLEAR unexpectedly carries a value")
        return None
    if update.action is not FieldUpdateAction.SET or update.value is None:
        raise ValueError("selected-skill SET is missing its VersionedRef")
    return update.value.model_copy(deep=True)


def apply_case_failure_update(
    current: CaseFailure | None,
    update: CaseFailureUpdate | None,
) -> CaseFailure | None:
    """Apply only the CaseFailure mutation explicitly present in a plan."""

    if update is None:
        return current.model_copy(deep=True) if current is not None else None
    if update.action is FieldUpdateAction.CLEAR:
        if update.value is not None:
            raise ValueError("CaseFailure CLEAR unexpectedly carries a value")
        return None
    if update.action is not FieldUpdateAction.SET or update.value is None:
        raise ValueError("CaseFailure SET is missing its value")
    return update.value.model_copy(deep=True)


def apply_candidate_mutation(
    current: CandidateConclusion | None,
    mutation: CandidateMutation | None,
    *,
    candidates_by_proposal_key: Mapping[str, CandidateConclusion],
) -> CandidateConclusion | None:
    """Apply INSTALL/SET_STATUS exactly as declared, with no verdict inference."""

    if mutation is None:
        return current.model_copy(deep=True) if current is not None else None

    if mutation.action is CandidateMutationAction.INSTALL:
        key = mutation.candidate_binding.accepted_candidate_proposal_key
        if key is None:
            raise ValueError("candidate INSTALL has no accepted proposal binding")
        candidate = _require(
            candidates_by_proposal_key, key, "candidate INSTALL proposal binding"
        )
        if candidate.status is not CandidateStatus.PROPOSED:
            raise ValueError("formalized candidate must be PROPOSED before INSTALL")
        if current is None or current.conclusion_id != candidate.conclusion_id:
            if candidate.revision != 1:
                raise ValueError("a newly installed candidate must start at revision 1")
        elif candidate.revision != current.revision + 1:
            raise ValueError("an updated candidate must increment revision exactly once")
        payload = candidate.model_dump(mode="python")
        payload["status"] = mutation.target_status
        return CandidateConclusion.model_validate(payload)

    if mutation.action is not CandidateMutationAction.SET_STATUS:
        raise ValueError("unknown CandidateMutation action")
    target = mutation.candidate_binding.existing_candidate_target
    if current is None or target is None or not _target_matches(current, target):
        raise ValueError("candidate status mutation does not match the current target")
    if current.status is not mutation.expected_status:
        raise ValueError("candidate status mutation expected_status does not match")
    payload = current.model_dump(mode="python")
    payload["status"] = mutation.target_status
    return CandidateConclusion.model_validate(payload)


def resolve_final_result(
    candidate: CandidateConclusion | None,
    final_result_target: CandidateTarget | None,
) -> CandidateConclusion | None:
    """Resolve an explicit final_result_target to the complete ACCEPTED candidate."""

    if final_result_target is None:
        return None
    if (
        candidate is None
        or candidate.status is not CandidateStatus.ACCEPTED
        or not _target_matches(candidate, final_result_target)
    ):
        raise ValueError("final_result_target does not resolve the ACCEPTED candidate")
    return candidate.model_copy(deep=True)


def _item_payload(item: DiagnosisItem, **updates: object) -> DiagnosisItem:
    payload = item.model_dump(mode="python")
    payload.update(updates)
    return DiagnosisItem.model_validate(payload)


def _draft_item(
    draft: DiagnosisItemDraft,
    *,
    status: DiagnosisItemStatus,
    created_revision: int,
    existing_evidence_ids: Collection[str],
    evidence_ids_by_proposal_key: Mapping[str, str],
) -> DiagnosisItem:
    return DiagnosisItem(
        item_id=draft.item_id,
        statement=draft.statement,
        status=status,
        provenance=draft.provenance,
        evidence_refs=_resolve_evidence_bindings(
            draft.evidence_bindings,
            existing_evidence_ids=existing_evidence_ids,
            evidence_ids_by_proposal_key=evidence_ids_by_proposal_key,
        ),
        created_revision=created_revision,
        supersedes=sorted(draft.supersedes),
    )


def _merge_refs(existing: Sequence[str], incoming: Sequence[str]) -> list[str]:
    existing_list = list(existing)
    existing_set = set(existing_list)
    return [*existing_list, *sorted(set(incoming) - existing_set)]


def _changed_item(
    item: DiagnosisItem,
    change: DiagnosisItemChange,
    *,
    status: DiagnosisItemStatus,
    existing_evidence_ids: Collection[str],
    evidence_ids_by_proposal_key: Mapping[str, str],
) -> DiagnosisItem:
    resolved = _resolve_evidence_bindings(
        change.evidence_bindings,
        existing_evidence_ids=existing_evidence_ids,
        evidence_ids_by_proposal_key=evidence_ids_by_proposal_key,
    )
    return _item_payload(
        item,
        statement=change.statement if change.statement is not None else item.statement,
        status=status,
        evidence_refs=_merge_refs(item.evidence_refs, resolved),
    )


def _all_state_items(state: DiagnosisState) -> list[DiagnosisItem]:
    return [
        *state.user_facts,
        *state.confirmed_facts,
        *state.active_hypotheses,
        *state.rejected_hypotheses,
        *state.open_questions,
    ]


def _validate_supersedes(
    current_items: Sequence[DiagnosisItem],
    drafts: Sequence[DiagnosisItemDraft],
) -> None:
    known_ids = {item.item_id for item in current_items} | {
        draft.item_id for draft in drafts
    }
    for draft in drafts:
        unknown = set(draft.supersedes) - known_ids
        if unknown:
            raise ValueError(
                f"DiagnosisItem supersedes unknown IDs: {sorted(unknown)!r}"
            )

    graph = {item.item_id: list(item.supersedes) for item in current_items}
    graph.update({draft.item_id: list(draft.supersedes) for draft in drafts})
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visiting:
            raise ValueError("DiagnosisItem supersedes graph contains a cycle")
        if item_id in visited:
            return
        visiting.add(item_id)
        for target in graph.get(item_id, []):
            if target in graph:
                visit(target)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in graph:
        visit(item_id)


def _append_items(
    existing: Sequence[DiagnosisItem],
    new_items: Sequence[DiagnosisItem],
) -> list[DiagnosisItem]:
    return [
        *(item.model_copy(deep=True) for item in existing),
        *sorted(
            (item.model_copy(deep=True) for item in new_items),
            key=lambda item: item.item_id,
        ),
    ]


def apply_diagnosis_state_delta(
    current: DiagnosisState,
    delta: DiagnosisStateDelta,
    *,
    evidence_ids_by_proposal_key: Mapping[str, str],
    candidate_mutation: CandidateMutation | None = None,
    candidates_by_proposal_key: Mapping[str, CandidateConclusion] | None = None,
    expected_target_revision: int | None = None,
) -> DiagnosisState:
    """Apply the accepted delta in the exact S01 order.

    The function also performs the explicit candidate step because S01 places
    it before stable ordering and the single semantic-revision decision.  Thus
    a plan that both changes ordinary state and installs/updates a candidate
    increments ``DiagnosisState.revision`` exactly once.
    """

    candidate_mapping = candidates_by_proposal_key or {}
    existing_evidence_ids = set(current.evidence_refs)
    target_revision = current.revision + 1

    new_drafts = [
        *delta.proposed_facts,
        *delta.add_active_hypotheses,
        *delta.add_open_questions,
    ]
    current_items = _all_state_items(current)
    current_item_ids = {item.item_id for item in current_items}
    new_item_ids = [
        *(item.item_id for item in delta.add_user_facts),
        *(draft.item_id for draft in new_drafts),
    ]
    _unique(new_item_ids, "new DiagnosisItem IDs")
    if current_item_ids.intersection(new_item_ids):
        raise ValueError("accepted delta attempts to add an existing DiagnosisItem")

    changed_item_ids = [
        *(item.item_id for item in delta.update_hypotheses),
        *(item.item_id for item in delta.reject_hypotheses),
        *(item.item_id for item in delta.resolve_questions),
    ]
    _unique(changed_item_ids, "changed DiagnosisItem IDs")
    if set(changed_item_ids).intersection(new_item_ids):
        raise ValueError("one DiagnosisItem ID appears in multiple delta operations")
    _validate_supersedes(current_items, new_drafts)

    # 1. Whole-field ProblemSpec replacement, including its independent rev.
    problem_spec = current.problem_spec.model_copy(deep=True)
    if delta.problem_spec_patch is not None:
        problem_spec, _ = apply_problem_spec_patch(
            current.problem_spec, delta.problem_spec_patch
        )

    # 2. Validated user facts are already formal DiagnosisItem values.
    for item in delta.add_user_facts:
        if item.created_revision != target_revision:
            raise ValueError("new user facts must use the target state revision")
    user_facts = _append_items(current.user_facts, delta.add_user_facts)

    # 3. Accepted proposed facts become evidence-backed confirmed facts.
    proposed_facts = [
        _draft_item(
            draft,
            status=DiagnosisItemStatus.ACTIVE,
            created_revision=target_revision,
            existing_evidence_ids=existing_evidence_ids,
            evidence_ids_by_proposal_key=evidence_ids_by_proposal_key,
        )
        for draft in delta.proposed_facts
    ]
    if any(not item.evidence_refs for item in proposed_facts):
        raise ValueError("accepted proposed facts must cite Evidence")
    confirmed_facts = _append_items(current.confirmed_facts, proposed_facts)

    # 4. Update, reject, then add hypotheses.
    active_hypotheses = [
        item.model_copy(deep=True) for item in current.active_hypotheses
    ]
    active_by_id = {item.item_id: index for index, item in enumerate(active_hypotheses)}
    for change in delta.update_hypotheses:
        if change.item_id not in active_by_id:
            raise ValueError("update_hypotheses targets a non-active hypothesis")
        index = active_by_id[change.item_id]
        active_hypotheses[index] = _changed_item(
            active_hypotheses[index],
            change,
            status=DiagnosisItemStatus.ACTIVE,
            existing_evidence_ids=existing_evidence_ids,
            evidence_ids_by_proposal_key=evidence_ids_by_proposal_key,
        )

    rejected_now: list[DiagnosisItem] = []
    rejected_ids = {change.item_id for change in delta.reject_hypotheses}
    for change in delta.reject_hypotheses:
        if change.item_id not in active_by_id:
            raise ValueError("reject_hypotheses targets a non-active hypothesis")
        rejected_now.append(
            _changed_item(
                active_hypotheses[active_by_id[change.item_id]],
                change,
                status=DiagnosisItemStatus.REJECTED,
                existing_evidence_ids=existing_evidence_ids,
                evidence_ids_by_proposal_key=evidence_ids_by_proposal_key,
            )
        )
    active_hypotheses = [
        item for item in active_hypotheses if item.item_id not in rejected_ids
    ]
    active_hypotheses = _append_items(
        active_hypotheses,
        [
            _draft_item(
                draft,
                status=DiagnosisItemStatus.ACTIVE,
                created_revision=target_revision,
                existing_evidence_ids=existing_evidence_ids,
                evidence_ids_by_proposal_key=evidence_ids_by_proposal_key,
            )
            for draft in delta.add_active_hypotheses
        ],
    )
    rejected_hypotheses = _append_items(
        current.rejected_hypotheses, rejected_now
    )

    # 5. Resolve old questions, then append new questions.
    current_question_ids = {item.item_id for item in current.open_questions}
    resolved_question_ids = {
        change.item_id for change in delta.resolve_questions
    }
    if not resolved_question_ids.issubset(current_question_ids):
        raise ValueError("resolve_questions targets a non-open question")
    for change in delta.resolve_questions:
        # Resolved questions leave the active projection, but every binding in
        # the accepted delta must still resolve.  Silently dropping an unknown
        # proposal here would allow an invalid TransitionPlan to commit.
        _resolve_evidence_bindings(
            change.evidence_bindings,
            existing_evidence_ids=existing_evidence_ids,
            evidence_ids_by_proposal_key=evidence_ids_by_proposal_key,
        )
    open_questions = _append_items(
        [
            item
            for item in current.open_questions
            if item.item_id not in resolved_question_ids
        ],
        [
            _draft_item(
                draft,
                status=DiagnosisItemStatus.ACTIVE,
                created_revision=target_revision,
                existing_evidence_ids=existing_evidence_ids,
                evidence_ids_by_proposal_key=evidence_ids_by_proposal_key,
            )
            for draft in delta.add_open_questions
        ],
    )

    # 6. Fulfil old requirements, then append newly accepted requirements.
    requirements = [
        requirement.model_copy(deep=True)
        for requirement in current.pending_requirements
    ]
    requirement_by_id = {
        requirement.requirement_id: index
        for index, requirement in enumerate(requirements)
    }
    fulfillment_ids = [item.requirement_id for item in delta.fulfill_requirements]
    _unique(fulfillment_ids, "requirement fulfillment IDs")
    for fulfillment in delta.fulfill_requirements:
        if fulfillment.requirement_id not in requirement_by_id:
            raise ValueError("fulfill_requirements targets an unknown requirement")
        index = requirement_by_id[fulfillment.requirement_id]
        requirement = requirements[index]
        if requirement.status is not RequirementStatus.OPEN:
            raise ValueError("fulfill_requirements targets a non-OPEN requirement")
        payload = requirement.model_dump(mode="python")
        payload.update(
            status=RequirementStatus.FULFILLED,
            fulfilled_by_refs=list(fulfillment.fulfilled_by_refs),
        )
        requirements[index] = PendingRequirement.model_validate(payload)
    current_requirement_ids = set(requirement_by_id)
    new_requirement_ids = [
        requirement.requirement_id for requirement in delta.add_pending_requirements
    ]
    if current_requirement_ids.intersection(new_requirement_ids):
        raise ValueError("accepted delta attempts to add an existing requirement")
    requirements.extend(
        sorted(
            (
                requirement.model_copy(deep=True)
                for requirement in delta.add_pending_requirements
            ),
            key=lambda requirement: requirement.requirement_id,
        )
    )

    # 7. Formal Evidence IDs enter state after every binding resolves.
    added_evidence_ids = _resolve_evidence_bindings(
        delta.add_evidence_bindings,
        existing_evidence_ids=existing_evidence_ids,
        evidence_ids_by_proposal_key=evidence_ids_by_proposal_key,
    )
    evidence_refs = _merge_refs(current.evidence_refs, added_evidence_ids)

    # 8. Candidate changes are only those explicitly stated by the plan.
    candidate = apply_candidate_mutation(
        current.candidate_conclusion,
        candidate_mutation,
        candidates_by_proposal_key=candidate_mapping,
    )

    # 9. Existing order was retained above; each new group was ID-sorted.
    payload = {
        "revision": current.revision,
        "problem_spec": problem_spec,
        "user_facts": user_facts,
        "confirmed_facts": confirmed_facts,
        "active_hypotheses": active_hypotheses,
        "rejected_hypotheses": rejected_hypotheses,
        "open_questions": open_questions,
        "pending_requirements": requirements,
        "evidence_refs": evidence_refs,
        "candidate_conclusion": candidate,
    }
    provisional = DiagnosisState.model_validate(payload)

    # 10. Every semantic change in this plan shares one state revision bump.
    before = current.model_dump(mode="json", exclude={"revision"})
    after = provisional.model_dump(mode="json", exclude={"revision"})
    actual_revision = current.revision + 1 if before != after else current.revision
    payload["revision"] = actual_revision
    result = DiagnosisState.model_validate(payload)
    if (
        expected_target_revision is not None
        and result.revision != expected_target_revision
    ):
        raise ValueError(
            "applied DiagnosisState revision does not match the planned target"
        )
    return result


def build_job(
    spec: JobSpec,
    *,
    job_id: str,
    case_id: str,
    created_at: str,
    target_diagnosis_state: DiagnosisState,
    projector: ContextSnapshotProjector,
    existing_evidence_ids: Collection[str],
    evidence_ids_by_proposal_key: Mapping[str, str],
    existing_artifact_ids: Collection[str],
    artifact_ids_by_proposal_key: Mapping[str, str],
    existing_candidate: CandidateConclusion | None,
    candidates_by_proposal_key: Mapping[str, CandidateConclusion],
) -> Job:
    """Project the final target state and turn a JobSpec into a PENDING Job."""

    if spec.target_state_revision != target_diagnosis_state.revision:
        raise ValueError("JobSpec target revision does not match DiagnosisState")
    snapshot = projector.project(target_diagnosis_state.model_copy(deep=True))
    if not isinstance(snapshot, ContextSnapshot):
        raise ValueError("ContextSnapshotProjector returned a non-contract value")
    if snapshot.diagnosis_state_revision != spec.target_state_revision:
        raise ValueError("projected ContextSnapshot has the wrong revision")

    resolved_evidence_refs = [
        resolve_planned_resource_binding(
            binding,
            existing_resource_ids=existing_evidence_ids,
            resource_ids_by_proposal_key=evidence_ids_by_proposal_key,
        )
        for binding in spec.evidence_bindings
    ]
    if len(resolved_evidence_refs) != len(set(resolved_evidence_refs)):
        raise ValueError("JobSpec evidence bindings resolve to duplicate Evidence IDs")
    requested_evidence_refs = set(resolved_evidence_refs)
    evidence_refs = [
        evidence_id
        for evidence_id in snapshot.evidence_refs
        if evidence_id in requested_evidence_refs
    ]
    if len(evidence_refs) != len(requested_evidence_refs):
        raise ValueError("JobSpec evidence bindings are absent from the target snapshot")
    artifact_refs = [
        resolve_planned_resource_binding(
            binding,
            existing_resource_ids=existing_artifact_ids,
            resource_ids_by_proposal_key=artifact_ids_by_proposal_key,
        )
        for binding in spec.artifact_bindings
    ]
    review_target = (
        resolve_review_target_binding(
            spec.review_target_binding,
            existing_candidate=existing_candidate,
            candidates_by_proposal_key=candidates_by_proposal_key,
        )
        if spec.review_target_binding is not None
        else None
    )

    return Job(
        job_id=job_id,
        case_id=case_id,
        job_type=spec.job_type,
        status=JobStatus.PENDING,
        goal=spec.goal,
        base_state_revision=spec.target_state_revision,
        context_snapshot=snapshot,
        evidence_refs=evidence_refs,
        attachment_refs=list(spec.attachment_refs),
        previous_outcome_refs=list(spec.previous_outcome_refs),
        artifact_refs=artifact_refs,
        agent_profile_ref=spec.agent_profile_ref,
        available_skill_refs=list(spec.available_skill_refs),
        skill_ref=spec.skill_ref,
        tool_bundle_ref=spec.tool_bundle_ref,
        context_policy_ref=spec.context_policy_ref,
        output_contract_ref=spec.output_contract_ref,
        logparse_tool_ref=spec.logparse_tool_ref,
        logparse_product=spec.logparse_product,
        review_target=review_target,
        replacement_for_job_id=spec.replacement_for_job_id,
        resource_limits=spec.resource_limits,
        created_at=created_at,
        started_at=None,
        finished_at=None,
        runtime_epoch=None,
    )


__all__ = [
    "apply_candidate_mutation",
    "apply_case_failure_update",
    "apply_diagnosis_state_delta",
    "apply_selected_skill_update",
    "build_job",
    "formalize_accepted_artifacts",
    "formalize_accepted_candidate",
    "formalize_accepted_evidence",
    "resolve_evidence_binding",
    "resolve_final_result",
    "resolve_planned_resource_binding",
    "resolve_review_target_binding",
]
