from __future__ import annotations

import copy
import hashlib
from typing import Any

import pytest
from pydantic import ValidationError

from problem_locator.contracts.enums import (
    ArtifactKind,
    CandidateMutationAction,
    CandidateStatus,
    CaseStatus,
    ErrorCode,
    JobStatus,
    JobType,
    OutcomeDisposition,
    ResourceKind,
    TriggerType,
)
from problem_locator.contracts.limits import JOB_STDOUT_STDERR_BYTES
from problem_locator.contracts.models import (
    AgentJobOutcome,
    ArtifactProposal,
    ArtifactSummary,
    CandidateMutation,
    CandidateTarget,
    ClaimReceipt,
    ContinuationResourceView,
    CreateCaseTriggerPayload,
    DiagnosisStateDelta,
    EvidenceProposal,
    EvidenceSourceBinding,
    ExecutionFileRef,
    ExecutionLogSinks,
    Job,
    JobOutcome,
    JobSpec,
    LogparseEvidenceLocator,
    LogparseParseClaim,
    LogparseParseParameters,
    LogparseRunMetadata,
    OpenArtifactResult,
    PlannedResourceBinding,
    PublishedJobReceipt,
    ReviewTargetBinding,
    RouteOutcomeTriggerPayload,
    RuntimeBindings,
    RuntimeExecutionReceipt,
    StagedResourceRef,
    TransitionPlan,
    TreeManifest,
    TreeManifestEntry,
    UploadAttachmentContent,
    UserResultPayload,
    ValidatedTrigger,
    WorkspaceInputManifest,
    default_resource_limits,
)
from problem_locator.contracts.outcomes import (
    validate_logparse_claim_for_job,
    validate_transition_plan_for_outcome,
    validate_user_result_for_outcome,
)
from problem_locator.contracts.serialization import (
    canonical_json_bytes,
    canonical_json_sha256,
)

from tests.deterministic.contracts._support import FIXTURE_ROOT, load_json
from tests.deterministic.contracts.fakes import (
    InMemoryBinaryStream,
    _CombinedLogCounter,
    _InMemoryAppendOnlyByteSink,
)


OTHER_CASE_ID = "00000000-0000-0000-0000-000000000099"
TRIGGER_ID = "00000000-0000-0000-0000-000000000090"


def _model(name: str, model_type: type[Any]) -> Any:
    return model_type.model_validate(load_json(FIXTURE_ROOT / "positive" / name))


def _runtime_bindings(job: Job) -> RuntimeBindings:
    return RuntimeBindings(
        diagnosis_mode=job.diagnosis_mode,
        generic_skill_name=job.generic_skill_name,
        agent_profile_ref=job.agent_profile_ref,
        available_skill_refs=job.available_skill_refs,
        skill_ref=job.skill_ref,
        tool_bundle_ref=job.tool_bundle_ref,
        context_policy_ref=job.context_policy_ref,
        output_contract_ref=job.output_contract_ref,
        logparse_tool_ref=job.logparse_tool_ref,
        logparse_product=job.logparse_product,
        resource_limits=job.resource_limits,
    )


def _empty_continuation() -> ContinuationResourceView:
    return ContinuationResourceView(
        evidence_refs=[],
        attachment_refs=[],
        artifact_refs=[],
        previous_outcome_refs=[],
    )


def _empty_delta() -> DiagnosisStateDelta:
    return DiagnosisStateDelta(
        problem_spec_patch=None,
        add_user_facts=[],
        proposed_facts=[],
        add_active_hypotheses=[],
        update_hypotheses=[],
        reject_hypotheses=[],
        add_open_questions=[],
        resolve_questions=[],
        add_pending_requirements=[],
        fulfill_requirements=[],
        add_evidence_bindings=[],
    )


def _create_trigger() -> ValidatedTrigger:
    source_job = _model("job-route.json", Job)
    return ValidatedTrigger(
        trigger_id=TRIGGER_ID,
        trigger_type=TriggerType.CREATE_CASE,
        case_id=source_job.case_id,
        expected_case_revision=0,
        idempotency_key="create-case-regression",
        payload=CreateCaseTriggerPayload(
            raw_problem_text=source_job.context_snapshot.problem_spec.statement,
            problem_spec=source_job.context_snapshot.problem_spec,
            initial_user_facts=[],
        ),
        continuation_resources=_empty_continuation(),
        runtime_bindings_by_job_type={},
        occurred_at="2026-07-31T00:00:00.000Z",
    )


def _route_outcome_trigger_payload() -> dict[str, Any]:
    outcome = _model("job-outcome-route.json", JobOutcome)
    route_job = _model("job-route.json", Job)
    return {
        "trigger_id": TRIGGER_ID,
        "trigger_type": TriggerType.ROUTE_OUTCOME,
        "case_id": outcome.case_id,
        "expected_case_revision": outcome.base_state_revision,
        "idempotency_key": "route-outcome-regression",
        "payload": RouteOutcomeTriggerPayload(job_outcome=outcome),
        "continuation_resources": ContinuationResourceView(
            evidence_refs=[],
            attachment_refs=[],
            artifact_refs=[],
            previous_outcome_refs=[outcome.outcome_id],
        ),
        "runtime_bindings_by_job_type": {
            JobType.ROUTE: _runtime_bindings(route_job)
        },
        "occurred_at": outcome.produced_at,
    }


def test_validated_outcome_trigger_binds_the_case_and_event_time() -> None:
    payload = _route_outcome_trigger_payload()
    assert ValidatedTrigger.model_validate(payload).case_id == payload["case_id"]

    wrong_case = copy.deepcopy(payload)
    wrong_case["case_id"] = OTHER_CASE_ID
    with pytest.raises(ValidationError, match="case_id"):
        ValidatedTrigger.model_validate(wrong_case)

    wrong_time = copy.deepcopy(payload)
    wrong_time["occurred_at"] = "2026-07-31T00:00:00.000Z"
    with pytest.raises(ValidationError, match="occurred_at"):
        ValidatedTrigger.model_validate(wrong_time)


def test_outcome_trigger_requires_the_incoming_outcome_as_first_continuation() -> None:
    payload = _route_outcome_trigger_payload()
    outcome_id = payload["payload"].job_outcome.outcome_id
    assert payload["continuation_resources"].previous_outcome_refs == [outcome_id]
    assert ValidatedTrigger.model_validate(payload).payload.job_outcome.outcome_id == outcome_id

    missing = copy.deepcopy(payload)
    missing["continuation_resources"] = _empty_continuation()
    with pytest.raises(ValidationError, match="incoming Outcome"):
        ValidatedTrigger.model_validate(missing)


def test_create_trigger_forbids_all_continuation_resources() -> None:
    valid = _create_trigger()
    assert valid.continuation_resources == _empty_continuation()

    for field_name in (
        "evidence_refs",
        "attachment_refs",
        "artifact_refs",
        "previous_outcome_refs",
    ):
        payload = valid.model_dump(mode="python")
        payload["continuation_resources"][field_name] = [OTHER_CASE_ID]
        with pytest.raises(ValidationError, match="continuation resources"):
            ValidatedTrigger.model_validate(payload)


def test_validated_trigger_rechecks_bindings_for_the_map_role() -> None:
    payload = _route_outcome_trigger_payload()
    route_bindings = next(iter(payload["runtime_bindings_by_job_type"].values()))
    payload["runtime_bindings_by_job_type"] = {
        JobType.DIAGNOSE: route_bindings.model_copy(
            update={"resource_limits": default_resource_limits(JobType.DIAGNOSE)}
        ),
    }

    with pytest.raises(ValidationError, match="DIAGNOSE/REVIEW bindings"):
        ValidatedTrigger.model_validate(payload)


def _transition_plan(
    *,
    disposition: OutcomeDisposition,
    accepted_artifacts: list[str] | None = None,
) -> TransitionPlan:
    return TransitionPlan(
        accepted_state_delta=_empty_delta(),
        target_case_status=CaseStatus.RUNNING,
        job_updates=[],
        outcome_disposition=disposition,
        accepted_evidence_proposal_keys=[],
        accepted_artifact_proposal_keys=accepted_artifacts or [],
        accepted_candidate_proposal_key=None,
        selected_skill_update=None,
        case_failure_update=None,
        candidate_mutation=None,
        next_job_spec=None,
        final_result_target=None,
        clear_active_job=True,
        reason="Keep the stale Outcome side-effect free.",
    )


def test_stale_route_plan_is_not_falsely_rejected_for_omitting_applied_mutations() -> None:
    outcome = _model("job-outcome-route.json", JobOutcome)
    plan = _transition_plan(disposition=OutcomeDisposition.STALE)

    assert validate_transition_plan_for_outcome(plan, outcome) is plan


def test_stale_plan_cannot_accept_even_a_known_proposal() -> None:
    outcome = _model("job-outcome-diagnosis.json", JobOutcome)
    user_result_key = outcome.proposed_artifacts[0].proposal_key
    plan = _transition_plan(
        disposition=OutcomeDisposition.STALE,
        accepted_artifacts=[user_result_key],
    )

    with pytest.raises(ValueError, match="only an APPLIED Outcome"):
        validate_transition_plan_for_outcome(plan, outcome)


def _candidate_plan(outcome: JobOutcome) -> TransitionPlan:
    assert outcome.payload is not None
    candidate = outcome.payload.candidate_conclusion_draft
    assert candidate is not None
    user_result_keys = [
        proposal.proposal_key
        for proposal in outcome.proposed_artifacts
        if proposal.artifact_kind
        in {ArtifactKind.USER_RESULT, ArtifactKind.USER_RESULT_ARCHIVE}
    ]
    return TransitionPlan(
        accepted_state_delta=outcome.payload.state_delta,
        target_case_status=CaseStatus.REVIEWING,
        job_updates=[],
        outcome_disposition=OutcomeDisposition.APPLIED,
        accepted_evidence_proposal_keys=[],
        accepted_artifact_proposal_keys=user_result_keys,
        accepted_candidate_proposal_key=candidate.proposal_key,
        selected_skill_update=None,
        case_failure_update=None,
        candidate_mutation=CandidateMutation(
            action=CandidateMutationAction.INSTALL,
            candidate_binding=ReviewTargetBinding(
                existing_candidate_target=None,
                accepted_candidate_proposal_key=candidate.proposal_key,
            ),
            expected_status=None,
            target_status=CandidateStatus.REVIEWING,
            reason=None,
        ),
        next_job_spec=None,
        final_result_target=None,
        clear_active_job=True,
        reason="Install the candidate for review.",
    )


def _logparse_pair(job: Job) -> tuple[EvidenceProposal, ArtifactProposal]:
    parse_manifest_bytes = b'{"files":[]}\n'
    tree_manifest = TreeManifest(
        version=1,
        entries=[
            TreeManifestEntry(
                path="parse_manifest.json",
                size=len(parse_manifest_bytes),
                sha256=hashlib.sha256(parse_manifest_bytes).hexdigest(),
            )
        ],
    )
    tree_hash = canonical_json_sha256(tree_manifest)
    staged = StagedResourceRef(
        staging_id="00000000-0000-0000-0000-000000000094",
        owner_job_id=job.job_id,
        proposal_key="logparse_run",
        resource_kind=ResourceKind.DIRECTORY,
        size=len(parse_manifest_bytes),
        sha256=tree_hash,
        tree_manifest=tree_manifest,
    )
    assert job.logparse_tool_ref is not None
    assert job.logparse_product is not None
    artifact = ArtifactProposal(
        proposal_key="logparse_run",
        artifact_kind=ArtifactKind.LOGPARSE_RUN,
        name="parsed-log-run",
        content_type="application/vnd.problem-locator.logparse-run+directory",
        resource_kind=ResourceKind.DIRECTORY,
        size=staged.size,
        sha256=staged.sha256,
        staged_resource_ref=staged,
        metadata=LogparseRunMetadata(
            tree_manifest_sha256=tree_hash,
            logparse_version_ref=job.logparse_tool_ref,
            parse_manifest_relative_path="parse_manifest.json",
            source_attachment_id=job.attachment_refs[0],
            source_attachment_sha256="2" * 64,
            parse_parameters=LogparseParseParameters(product=job.logparse_product),
        ),
    )
    evidence = EvidenceProposal(
        proposal_key="parsed_timeout_evidence",
        source_type="LOGPARSE",
        source_binding=EvidenceSourceBinding(
            existing_source_ref=None,
            artifact_proposal_key=artifact.proposal_key,
        ),
        locator=LogparseEvidenceLocator(
            kind="LOGPARSE",
            relative_path="events/timeout.json",
            start_line=None,
            end_line=None,
            start_time=None,
            end_time=None,
        ),
        summary="The parsed run identifies the timed-out request.",
        content_hash=None,
        staged_resource_ref=None,
    )
    return evidence, artifact


def _outcome_with_logparse_pair() -> tuple[Job, JobOutcome, EvidenceProposal, ArtifactProposal]:
    job = _model("job-diagnose.json", Job)
    outcome = _model("job-outcome-diagnosis.json", JobOutcome)
    evidence, artifact = _logparse_pair(job)
    payload = outcome.model_dump(mode="python")
    payload["proposed_evidence"].append(evidence)
    payload["proposed_artifacts"].append(artifact)
    return job, JobOutcome.model_validate(payload), evidence, artifact


@pytest.mark.parametrize("accepted_side", ["evidence", "artifact"])
def test_logparse_evidence_and_artifact_acceptance_is_atomic_both_ways(
    accepted_side: str,
) -> None:
    _, outcome, evidence, artifact = _outcome_with_logparse_pair()
    plan = _candidate_plan(outcome)
    if accepted_side == "evidence":
        plan = plan.model_copy(
            update={"accepted_evidence_proposal_keys": [evidence.proposal_key]}
        )
    else:
        plan = plan.model_copy(
            update={
                "accepted_artifact_proposal_keys": [
                    *plan.accepted_artifact_proposal_keys,
                    artifact.proposal_key,
                ]
            }
        )

    with pytest.raises(ValueError, match="accepted together"):
        validate_transition_plan_for_outcome(plan, outcome)

    accepted = plan.model_copy(
        update={
            "accepted_evidence_proposal_keys": [evidence.proposal_key],
            "accepted_artifact_proposal_keys": [
                *[
                    proposal.proposal_key
                    for proposal in outcome.proposed_artifacts
                    if proposal.artifact_kind
                    in {
                        ArtifactKind.USER_RESULT,
                        ArtifactKind.USER_RESULT_ARCHIVE,
                    }
                ],
                artifact.proposal_key,
            ],
        }
    )
    assert validate_transition_plan_for_outcome(accepted, outcome) is accepted


def _review_pass_plan(
    outcome: JobOutcome,
    target: CandidateTarget,
) -> TransitionPlan:
    return TransitionPlan(
        accepted_state_delta=_empty_delta(),
        target_case_status=CaseStatus.RESOLVED,
        job_updates=[],
        outcome_disposition=OutcomeDisposition.APPLIED,
        accepted_evidence_proposal_keys=[],
        accepted_artifact_proposal_keys=[],
        accepted_candidate_proposal_key=None,
        selected_skill_update=None,
        case_failure_update=None,
        candidate_mutation=CandidateMutation(
            action=CandidateMutationAction.SET_STATUS,
            candidate_binding=ReviewTargetBinding(
                existing_candidate_target=target,
                accepted_candidate_proposal_key=None,
            ),
            expected_status=CandidateStatus.REVIEWING,
            target_status=CandidateStatus.ACCEPTED,
            reason=None,
        ),
        next_job_spec=None,
        final_result_target=target,
        clear_active_job=True,
        reason="Accept the fixed reviewed candidate.",
    )


def test_review_pass_plan_must_accept_the_outcome_fixed_target() -> None:
    outcome = _model("job-outcome-review.json", JobOutcome)
    assert outcome.payload is not None
    target = CandidateTarget(
        candidate_conclusion_id=outcome.payload.candidate_conclusion_id,
        candidate_revision=outcome.payload.candidate_revision,
        candidate_content_hash=outcome.payload.candidate_content_hash,
    )
    valid = _review_pass_plan(outcome, target)
    assert validate_transition_plan_for_outcome(valid, outcome) is valid

    drifted_target = target.model_copy(
        update={"candidate_revision": target.candidate_revision + 1}
    )
    drifted = _review_pass_plan(outcome, drifted_target)
    with pytest.raises(ValueError, match="fixed candidate"):
        validate_transition_plan_for_outcome(drifted, outcome)


def test_installed_candidate_requires_next_review_job_to_bind_the_new_proposal() -> None:
    outcome = _model("job-outcome-diagnosis.json", JobOutcome)
    plan = _candidate_plan(outcome)
    assert plan.accepted_candidate_proposal_key is not None
    review_job = _model("job-review.json", Job)
    assert review_job.review_target is not None

    def next_review_job(review_binding: ReviewTargetBinding) -> JobSpec:
        return JobSpec(
            job_type=JobType.REVIEW,
            diagnosis_mode=review_job.diagnosis_mode,
            generic_skill_name=review_job.generic_skill_name,
            generic_problem_text=review_job.generic_problem_text,
            goal=review_job.goal,
            target_state_revision=review_job.base_state_revision,
            evidence_bindings=[
                PlannedResourceBinding(
                    existing_resource_id=evidence_id,
                    accepted_proposal_key=None,
                )
                for evidence_id in review_job.evidence_refs
            ],
            attachment_refs=review_job.attachment_refs,
            previous_outcome_refs=review_job.previous_outcome_refs,
            artifact_bindings=[
                PlannedResourceBinding(
                    existing_resource_id=artifact_id,
                    accepted_proposal_key=None,
                )
                for artifact_id in review_job.artifact_refs
            ],
            agent_profile_ref=review_job.agent_profile_ref,
            available_skill_refs=review_job.available_skill_refs,
            skill_ref=review_job.skill_ref,
            tool_bundle_ref=review_job.tool_bundle_ref,
            context_policy_ref=review_job.context_policy_ref,
            output_contract_ref=review_job.output_contract_ref,
            logparse_tool_ref=review_job.logparse_tool_ref,
            logparse_product=review_job.logparse_product,
            review_target_binding=review_binding,
            replacement_for_job_id=None,
            resource_limits=review_job.resource_limits,
        )

    correctly_bound = plan.model_copy(
        update={
            "next_job_spec": next_review_job(
                ReviewTargetBinding(
                    existing_candidate_target=None,
                    accepted_candidate_proposal_key=plan.accepted_candidate_proposal_key,
                )
            )
        }
    )
    assert TransitionPlan.model_validate(correctly_bound.model_dump(mode="python"))

    stale_target = plan.model_copy(
        update={
            "next_job_spec": next_review_job(
                ReviewTargetBinding(
                    existing_candidate_target=review_job.review_target,
                    accepted_candidate_proposal_key=None,
                )
            )
        }
    )
    with pytest.raises(ValidationError, match="candidate accepted by this plan"):
        TransitionPlan.model_validate(stale_target.model_dump(mode="python"))


@pytest.mark.parametrize(
    ("outcome_name", "outcome_type"),
    [
        ("agent-job-outcome-diagnosis.json", AgentJobOutcome),
        ("job-outcome-diagnosis.json", JobOutcome),
    ],
)
def test_user_result_accepts_exact_canonical_candidate_bytes(
    outcome_name: str,
    outcome_type: type[AgentJobOutcome] | type[JobOutcome],
) -> None:
    job = _model("job-diagnose.json", Job)
    outcome = _model(outcome_name, outcome_type)
    result_bytes = (FIXTURE_ROOT / "positive" / "user-result.json").read_bytes()
    expected = UserResultPayload.model_validate(
        load_json(FIXTURE_ROOT / "positive" / "user-result.json")
    )

    assert canonical_json_bytes(expected) == result_bytes
    assert validate_user_result_for_outcome(job, outcome, result_bytes) == expected


def test_user_result_rejects_noncanonical_bytes_before_candidate_acceptance() -> None:
    job = _model("job-diagnose.json", Job)
    outcome = _model("job-outcome-diagnosis.json", JobOutcome)
    canonical = (FIXTURE_ROOT / "positive" / "user-result.json").read_bytes()

    with pytest.raises(ValueError, match="not canonical"):
        validate_user_result_for_outcome(job, outcome, b" " + canonical)


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("findings", "findings must exactly reflect"),
        ("rule_evidence", "verification_rules must exactly reflect"),
        ("rule_observed_times", "verification_rules must exactly reflect"),
        ("rule_issues", "verification_rules must exactly reflect"),
        ("recommendations", "recommendations must exactly match"),
    ],
)
def test_user_result_rejects_public_semantic_field_drift(
    drift: str,
    message: str,
) -> None:
    job = _model("job-diagnose.json", Job)
    outcome = _model("job-outcome-diagnosis.json", JobOutcome)
    result = load_json(FIXTURE_ROOT / "positive" / "user-result.json")
    if drift == "findings":
        binding = result["supporting_evidence_bindings"][0]
        result["findings"] = [
            {
                "statement": "A renderer-invented finding.",
                "confidence": 0.9,
                "evidence_bindings": [binding],
                "citations": [
                    {
                        "evidence_binding": binding,
                        "archive_name": None,
                        "line_start": None,
                        "line_end": None,
                        "raw_bytes_sha256": None,
                        "excerpt": None,
                    }
                ],
            }
        ]
    elif drift == "rule_evidence":
        result["verification_rules"][0]["evidence_bindings"] = []
        result["verification_rules"][0]["citations"] = []
    elif drift == "rule_observed_times":
        result["verification_rules"][0]["observed_times"] = [
            "2026-07-31T00:00:00.000Z"
        ]
    elif drift == "rule_issues":
        result["verification_rules"][0]["issues"] = ["Invented limitation."]
    else:
        result["recommendations"] = ["A renderer-invented recommendation."]

    with pytest.raises(ValueError, match=message):
        validate_user_result_for_outcome(
            job,
            outcome,
            canonical_json_bytes(result),
        )


def test_review_user_result_recommendation_is_bound_to_review_assessment() -> None:
    job = _model("job-review.json", Job)
    outcome_value = load_json(FIXTURE_ROOT / "positive" / "job-outcome-review.json")
    outcome_value["payload"].update(
        verdict="REJECT",
        unsupported_findings=["The causal claim is unsupported."],
        recommendation="Collect stronger causal evidence.",
    )
    artifact = copy.deepcopy(
        load_json(FIXTURE_ROOT / "positive" / "job-outcome-diagnosis.json")[
            "proposed_artifacts"
        ][0]
    )
    artifact["proposal_key"] = "review_user_result"
    artifact["staged_resource_ref"]["proposal_key"] = "review_user_result"
    artifact["staged_resource_ref"]["owner_job_id"] = outcome_value["job_id"]
    outcome_value["proposed_artifacts"] = [artifact]

    result = load_json(FIXTURE_ROOT / "positive" / "user-result.json")
    result.update(
        status="INCONCLUSIVE",
        source_job_type="REVIEW",
        root_cause=None,
        findings=[],
        causal_factors=[],
        candidate_factors=[],
        excluded_factors=[],
        evidence_gaps=["The causal claim is unsupported."],
        recommendations=[outcome_value["payload"]["recommendation"]],
    )
    result_bytes = canonical_json_bytes(result)
    artifact["size"] = len(result_bytes)
    artifact["sha256"] = hashlib.sha256(result_bytes).hexdigest()
    artifact["staged_resource_ref"]["size"] = len(result_bytes)
    artifact["staged_resource_ref"]["sha256"] = artifact["sha256"]
    outcome = JobOutcome.model_validate(outcome_value)
    assert validate_user_result_for_outcome(job, outcome, result_bytes)

    result["recommendations"] = ["A renderer-invented recommendation."]
    with pytest.raises(ValueError, match="recommendations must exactly match"):
        validate_user_result_for_outcome(
            job,
            outcome,
            canonical_json_bytes(result),
        )


@pytest.mark.parametrize(
    ("field_name", "replacement", "message"),
    [
        ("size", 623, "canonical bytes"),
        ("sha256", "9" * 64, "canonical bytes"),
    ],
)
def test_user_result_rejects_normalized_artifact_size_or_hash_drift(
    field_name: str,
    replacement: int | str,
    message: str,
) -> None:
    job = _model("job-diagnose.json", Job)
    outcome = _model("job-outcome-diagnosis.json", JobOutcome)
    result_bytes = (FIXTURE_ROOT / "positive" / "user-result.json").read_bytes()
    artifact = outcome.proposed_artifacts[0].model_copy(
        update={field_name: replacement}
    )
    drifted = outcome.model_copy(update={"proposed_artifacts": [artifact]})

    with pytest.raises(ValueError, match=message):
        validate_user_result_for_outcome(job, drifted, result_bytes)


@pytest.mark.parametrize(
    ("field_name", "replacement", "message"),
    [
        ("declared_size", 623, "declared_size"),
        ("declared_sha256", "9" * 64, "declared_sha256"),
    ],
)
def test_user_result_rejects_agent_declared_size_or_hash_drift(
    field_name: str,
    replacement: int | str,
    message: str,
) -> None:
    job = _model("job-diagnose.json", Job)
    outcome = _model("agent-job-outcome-diagnosis.json", AgentJobOutcome)
    result_bytes = (FIXTURE_ROOT / "positive" / "user-result.json").read_bytes()
    draft = outcome.proposed_artifact_drafts[0].model_copy(
        update={field_name: replacement}
    )
    drifted = outcome.model_copy(update={"proposed_artifact_drafts": [draft]})

    with pytest.raises(ValueError, match=message):
        validate_user_result_for_outcome(job, drifted, result_bytes)


def _first_parse_seam() -> tuple[
    Job,
    WorkspaceInputManifest,
    LogparseParseClaim,
    bytes,
]:
    job_payload = load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    manifest_payload = load_json(
        FIXTURE_ROOT / "positive" / "workspace-input-manifest.json"
    )
    claim_payload = load_json(
        FIXTURE_ROOT / "positive" / "logparse-parse-claim.json"
    )
    job_payload["artifact_refs"] = []
    manifest_payload["entries"] = [
        entry
        for entry in manifest_payload["entries"]
        if entry["input_kind"] != "ARTIFACT"
    ]
    manifest_payload["resolved_logparse_plan"].update(
        attachment_id=job_payload["attachment_refs"][0],
        artifact_id=None,
    )
    request_bytes = canonical_json_bytes(
        {
            "product": job_payload["logparse_product"],
            "targets": [{"rpc_method": "GetInventory", "server_service": "inventory"}],
        }
    )
    claim_payload["request_sha256"] = hashlib.sha256(request_bytes).hexdigest()
    return (
        Job.model_validate(job_payload),
        WorkspaceInputManifest.model_validate(manifest_payload),
        LogparseParseClaim.model_validate(claim_payload),
        request_bytes,
    )


def _successful_logparse_outcome(job: Job) -> JobOutcome:
    outcome = _model("job-outcome-diagnosis.json", JobOutcome)
    _, artifact = _logparse_pair(job)
    payload = outcome.model_dump(mode="python")
    payload["proposed_artifacts"].append(artifact)
    return JobOutcome.model_validate(payload)


def _failed_diagnosis_outcome(job: Job) -> JobOutcome:
    payload = load_json(FIXTURE_ROOT / "positive" / "job-outcome-failure.json")
    payload.update(
        {
            "job_id": job.job_id,
            "case_id": job.case_id,
            "job_type": job.job_type,
            "base_state_revision": job.base_state_revision,
        }
    )
    return JobOutcome.model_validate(payload)


def test_logparse_claim_accepts_one_matching_request_and_successful_proposal() -> None:
    job, manifest, claim, request_bytes = _first_parse_seam()
    outcome = _successful_logparse_outcome(job)

    assert (
        validate_logparse_claim_for_job(
            claim,
            job,
            manifest,
            request_bytes,
            outcome,
        )
        is claim
    )
    assert (
        validate_logparse_claim_for_job(None, job, manifest, None, None) is None
    )


def test_logparse_outcome_proposal_requires_a_claim() -> None:
    job, manifest, _, _ = _first_parse_seam()
    outcome = _successful_logparse_outcome(job)

    with pytest.raises(ValueError, match="requires a parse claim"):
        validate_logparse_claim_for_job(None, job, manifest, None, outcome)


@pytest.mark.parametrize("outcome_kind", ["none", "success", "failure"])
def test_broker_accepted_parse_bytes_require_the_create_new_claim(
    outcome_kind: str,
) -> None:
    job, manifest, claim, request_bytes = _first_parse_seam()
    outcome = {
        "none": None,
        "success": _successful_logparse_outcome(job),
        "failure": _failed_diagnosis_outcome(job),
    }[outcome_kind]

    with pytest.raises(ValueError, match="request bytes require a parse claim"):
        validate_logparse_claim_for_job(
            None,
            job,
            manifest,
            request_bytes,
            outcome,
        )

    assert validate_logparse_claim_for_job(None, job, manifest, None, None) is None
    assert (
        validate_logparse_claim_for_job(
            claim,
            job,
            manifest,
            request_bytes,
            _successful_logparse_outcome(job),
        )
        is claim
    )


def test_logparse_claim_is_mutually_exclusive_with_an_existing_manifest_run() -> None:
    job = _model("job-diagnose.json", Job)
    manifest = _model("workspace-input-manifest.json", WorkspaceInputManifest)
    _, _, claim, request_bytes = _first_parse_seam()

    with pytest.raises(ValueError, match="manifest forbids"):
        validate_logparse_claim_for_job(
            claim,
            job,
            manifest,
            request_bytes,
            None,
        )


def test_logparse_claim_requires_exact_canonical_request_bytes_and_hash() -> None:
    job, manifest, claim, request_bytes = _first_parse_seam()

    with pytest.raises(ValueError, match="exact request bytes"):
        validate_logparse_claim_for_job(claim, job, manifest, None, None)
    with pytest.raises(ValueError, match="not canonical"):
        validate_logparse_claim_for_job(
            claim,
            job,
            manifest,
            b" " + request_bytes,
            None,
        )
    with pytest.raises(ValueError, match="Job, Attachment, and request"):
        validate_logparse_claim_for_job(
            claim,
            job,
            manifest,
            canonical_json_bytes({"different": True}),
            None,
        )


def test_successful_claim_requires_exactly_one_matching_logparse_proposal() -> None:
    job, manifest, claim, request_bytes = _first_parse_seam()
    without_run = _model("job-outcome-diagnosis.json", JobOutcome)
    with pytest.raises(ValueError, match="requires one LOGPARSE_RUN"):
        validate_logparse_claim_for_job(
            claim,
            job,
            manifest,
            request_bytes,
            without_run,
        )

    outcome = _successful_logparse_outcome(job)
    proposal = next(
        item
        for item in outcome.proposed_artifacts
        if item.artifact_kind is ArtifactKind.LOGPARSE_RUN
    )
    drifted_proposal = proposal.model_copy(update={"proposal_key": "other-run"})
    drifted = outcome.model_copy(
        update={
            "proposed_artifacts": [
                item
                if item.artifact_kind is not ArtifactKind.LOGPARSE_RUN
                else drifted_proposal
                for item in outcome.proposed_artifacts
            ]
        }
    )
    with pytest.raises(ValueError, match="exactly match"):
        validate_logparse_claim_for_job(
            claim,
            job,
            manifest,
            request_bytes,
            drifted,
        )


def test_failed_claim_allows_no_logparse_proposal_and_forbids_one() -> None:
    job, manifest, claim, request_bytes = _first_parse_seam()
    failure = _failed_diagnosis_outcome(job)
    assert (
        validate_logparse_claim_for_job(
            claim,
            job,
            manifest,
            request_bytes,
            failure,
        )
        is claim
    )

    _, artifact = _logparse_pair(job)
    invalid = failure.model_copy(update={"proposed_artifacts": [artifact]})
    with pytest.raises(ValueError, match="failed claimed execution forbids"):
        validate_logparse_claim_for_job(
            claim,
            job,
            manifest,
            request_bytes,
            invalid,
        )


def _execution_file_ref(value: Job | JobOutcome, filename: str) -> ExecutionFileRef:
    data = canonical_json_bytes(value)
    return ExecutionFileRef(
        relative_key=f"jobs/{value.job_id}/{filename}",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def test_stream_and_sink_contracts_reject_nonconforming_capabilities_and_limit_drift() -> None:
    attachment_id = "00000000-0000-0000-0000-000000000050"
    stream = InMemoryBinaryStream(b"payload")
    upload = UploadAttachmentContent(
        idempotency_key=attachment_id,
        attachment_id=attachment_id,
        expected_content_type="application/octet-stream",
        expected_size=7,
        expected_sha256=hashlib.sha256(b"payload").hexdigest(),
        byte_stream=stream,
    )
    assert upload.byte_stream is stream
    with pytest.raises(ValidationError, match="BinaryStream"):
        UploadAttachmentContent(
            idempotency_key=attachment_id,
            attachment_id=attachment_id,
            expected_content_type="application/octet-stream",
            expected_size=7,
            expected_sha256=hashlib.sha256(b"payload").hexdigest(),
            byte_stream=object(),
        )

    counter = _CombinedLogCounter(JOB_STDOUT_STDERR_BYTES)
    stdout = _InMemoryAppendOnlyByteSink(counter)
    stderr = _InMemoryAppendOnlyByteSink(counter)
    sinks = ExecutionLogSinks(
        stdout=stdout,
        stderr=stderr,
        combined_limit_bytes=JOB_STDOUT_STDERR_BYTES,
    )
    assert sinks.stdout is stdout and sinks.stderr is stderr
    with pytest.raises(ValidationError):
        ExecutionLogSinks(
            stdout=stdout,
            stderr=stderr,
            combined_limit_bytes=JOB_STDOUT_STDERR_BYTES - 1,
        )
    with pytest.raises(ValidationError, match="AppendOnlyByteSink"):
        ExecutionLogSinks(
            stdout=object(),
            stderr=stderr,
            combined_limit_bytes=JOB_STDOUT_STDERR_BYTES,
        )


def test_open_artifact_result_requires_a_downloadable_summary_and_binary_stream() -> None:
    summary = ArtifactSummary(
        artifact_id="00000000-0000-0000-0000-000000000061",
        kind=ArtifactKind.DIAGNOSTIC_EXPORT,
        name="diagnostic.json",
        content_type="application/json",
        resource_kind=ResourceKind.FILE,
        size=2,
        sha256=hashlib.sha256(b"{}\n").hexdigest(),
        created_by_job_id="00000000-0000-0000-0000-000000000011",
        created_at="2026-07-31T00:02:00.000Z",
        downloadable=True,
    )
    stream = InMemoryBinaryStream(b"{}\n")
    assert OpenArtifactResult(artifact=summary, stream=stream).stream is stream
    with pytest.raises(ValidationError, match="BinaryStream"):
        OpenArtifactResult(artifact=summary, stream=object())

    internal = ArtifactSummary(
        artifact_id="00000000-0000-0000-0000-000000000060",
        kind=ArtifactKind.LOGPARSE_RUN,
        name="parsed-run",
        content_type="application/vnd.problem-locator.logparse-run+directory",
        resource_kind=ResourceKind.DIRECTORY,
        size=2,
        sha256="1" * 64,
        created_by_job_id="00000000-0000-0000-0000-000000000011",
        created_at="2026-07-31T00:02:00.000Z",
        downloadable=False,
    )
    with pytest.raises(ValidationError, match="downloadable"):
        OpenArtifactResult(artifact=internal, stream=InMemoryBinaryStream())


def test_execution_receipts_bind_canonical_bytes_to_the_fixed_job_paths() -> None:
    job = _model("job-route.json", Job)
    outcome = _model("job-outcome-route.json", JobOutcome)
    job_ref = _execution_file_ref(job, "job.json")
    outcome_ref = _execution_file_ref(outcome, "job_outcome.json")

    assert PublishedJobReceipt(job=job, job_file_ref=job_ref).job is job
    assert (
        RuntimeExecutionReceipt(
            job_outcome=outcome,
            outcome_file_ref=outcome_ref,
        ).job_outcome
        is outcome
    )

    for field_name, replacement in (
        ("relative_key", f"jobs/{OTHER_CASE_ID}/job_outcome.json"),
        ("size", outcome_ref.size + 1),
        ("sha256", "9" * 64),
    ):
        drifted = outcome_ref.model_copy(update={field_name: replacement})
        with pytest.raises(ValidationError, match="execution file|ExecutionFileRef"):
            RuntimeExecutionReceipt(
                job_outcome=outcome,
                outcome_file_ref=drifted,
            )

    terminal_payload = job.model_dump(mode="python")
    terminal_payload.update(
        {
            "status": JobStatus.SUCCEEDED,
            "started_at": "2026-07-31T00:00:10.000Z",
            "finished_at": "2026-07-31T00:00:20.000Z",
            "runtime_epoch": "00000000-0000-0000-0000-000000000091",
        }
    )
    terminal = Job.model_validate(terminal_payload)
    with pytest.raises(ValidationError, match="PENDING"):
        PublishedJobReceipt(job=terminal, job_file_ref=job_ref)


@pytest.mark.parametrize(
    ("claimed", "include_job", "failure_applied", "failure_code"),
    [
        (False, True, False, None),
        (True, False, False, None),
        (False, False, True, None),
        (False, False, False, ErrorCode.ASSET_VERSION_UNAVAILABLE),
        (True, True, True, ErrorCode.ASSET_VERSION_UNAVAILABLE),
    ],
)
def test_claim_receipt_fields_and_success_failure_branches_are_fixed(
    claimed: bool,
    include_job: bool,
    failure_applied: bool,
    failure_code: ErrorCode | None,
) -> None:
    job = _model("job-route.json", Job) if include_job else None
    with pytest.raises(ValidationError):
        ClaimReceipt(
            claimed=claimed,
            job=job,
            failure_applied=failure_applied,
            failure_code=failure_code,
        )


def test_claim_receipt_accepts_each_nonoverlapping_branch() -> None:
    job = _model("job-route.json", Job)
    assert ClaimReceipt(
        claimed=False,
        job=None,
        failure_applied=False,
        failure_code=None,
    ).job is None
    assert ClaimReceipt(
        claimed=True,
        job=job,
        failure_applied=False,
        failure_code=None,
    ).job is job
    assert ClaimReceipt(
        claimed=False,
        job=None,
        failure_applied=True,
        failure_code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
    ).failure_applied
