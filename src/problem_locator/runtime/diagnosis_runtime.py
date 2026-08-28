"""S04 Runtime composition for one already-claimed immutable Job."""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from problem_locator.contracts import (
    ApplicationErrorDetail,
    ApplicationPortError,
    ArtifactKind,
    AttachmentRequirementConstraints,
    AssetCatalogPort,
    CancellationSignal,
    CaseAggregate,
    Clock,
    BoundedContext,
    DiagnosisOutcome,
    DiagnosisMode,
    DiagnosisStateDelta,
    ErrorCode,
    ExecutionFailure,
    ExecutionLogSinks,
    ExecutionRecordStore,
    ExecutionStage,
    IdGenerator,
    InputRequirementConstraints,
    Job,
    JobOutcome,
    JobStatus,
    JobType,
    OutcomeResultType,
    LogparseBrokerError,
    LogparseBrokerFactory,
    LogparseBrokerSession,
    LogparseParseClaim,
    MechanicalFact,
    PendingRequirement,
    RequirementKind,
    RequirementStatus,
    ResourceStore,
    ReviewCausalAssertion,
    ReviewSubjectV2,
    RouteDecision,
    RouteKind,
    RuntimeExecutionReceipt,
    RuntimeInfrastructureError,
    ResolvedLogparseAnchor,
    ResolvedLogparsePlanInput,
    StagedResourceRef,
    StateRepository,
    SupplementPolicy,
    bytes_sha256,
    canonical_json_bytes,
    canonical_json_sha256,
    parse_canonical_json_bytes,
    review_required_evidence_refs,
    validate_logparse_claim_for_job,
)
from problem_locator.contracts.enums import MethodsValidationReasonCode
from problem_locator.diagnostics import log_event
from problem_locator.integrations.logparse.requests import (
    Anchor,
    ParseTargetsRequest,
    TargetLogsRequest,
)
from problem_locator.journey import (
    record_journey_event,
    record_stage_completed,
    record_stage_failed,
    record_stage_started,
)

from .agent_backend import AgentBackend, BackendExecutionLimits
from .context_builder import ContextBuilder, ContextLimitExceeded, ContextMaterials
from .context_policy import ResolvedJobAssets, RuntimeAssetResolver
from .failures import RuntimeExecutionError, runtime_failure
from .generic_locator import GenericLocatorExecutor
from .input_profile import expand_profile_requirements
from .outcome_publisher import OutcomePublisher
from .output_reader import (
    RejectedAgentOutputError,
    ValidatedAgentDraft,
    ValidatedAgentOutput,
    ValidatedMethodDiagnosisDraft,
    ValidatedMethodReviewDraft,
    ValidatedMethodsPreprocessing,
    ValidatedProposalResource,
    read_agent_output,
    read_methods_preprocessing,
)
from .methods_outcome import map_verified_methods_draft
from .proposal_stager import discard_staged, stage_validated_output
from .resolved_logparse import (
    ResolvedLogparsePlanNotReady,
    compile_resolved_logparse_plan,
)
from .methods_grounding import (
    MethodGroundingAuditV1,
    MethodDiagnosisDraftV1,
    MethodReviewV1,
    SkillLoadReceiptV1,
    VerifiedMethodDiagnosisV1,
    scan_method_markers,
    verify_method_diagnosis,
    verify_method_review,
)
from .methods_skill import (
    ResolvedSpecializedSkillV1,
    load_specialized_skill_registration,
)
from .server_outcome_finalizer import finalize_server_outcome
from .workspace import (
    FrozenMethodsWorkspaceInputs,
    PreparedWorkspace,
    WorkspaceManager,
)


def _unexpected_failure() -> RuntimeExecutionError:
    return runtime_failure(
        stage=ExecutionStage.OUTCOME_VALIDATE,
        code=ErrorCode.OUTCOME_INVALID,
        message="Runtime execution could not be validated safely.",
    )


_METHOD_VALIDATION_REASON_CODES: dict[str, MethodsValidationReasonCode] = {
    "evidence marker is not indexed by its method": (
        MethodsValidationReasonCode.EVIDENCE_MARKER_NOT_INDEXED
    ),
    "every confirmed method must have grounded evidence": (
        MethodsValidationReasonCode.CONFIRMED_EVIDENCE_MISSING
    ),
    "confirmed method has no positive marker in the full target-log scan": (
        MethodsValidationReasonCode.CONFIRMED_MARKER_SCAN_MISS
    ),
    "grounded Methods source changed before Outcome mapping": (
        MethodsValidationReasonCode.EVIDENCE_SOURCE_CHANGED
    ),
}


def _method_validation_reason_code(
    error: TypeError | ValueError,
) -> MethodsValidationReasonCode:
    """Classify known Methods validation failures into a closed public reason set."""

    return _METHOD_VALIDATION_REASON_CODES.get(
        str(error),
        MethodsValidationReasonCode.VALIDATION_FAILED,
    )


def _broker_failure(*, retryable: bool = True) -> ExecutionFailure:
    return ExecutionFailure(
        stage=ExecutionStage.TOOL_EXECUTE,
        code=ErrorCode.LOGPARSE_FAILED,
        message="The job-scoped logparse broker failed.",
        retryable=retryable,
        details=[],
    )


def _append_diagnostic(
    failure: ExecutionFailure,
    *,
    field: str,
    actual: str,
) -> ExecutionFailure:
    if any(
        detail.field == field
        and detail.resource_type == "LOGPARSE_BROKER"
        and detail.actual == actual
        for detail in failure.details
    ):
        return failure
    return ExecutionFailure(
        stage=failure.stage,
        code=failure.code,
        message=failure.message,
        retryable=failure.retryable,
        details=[
            *failure.details,
            ApplicationErrorDetail(
                field=field,
                resource_type="LOGPARSE_BROKER",
                resource_id=None,
                resource_ref=None,
                expected="safe",
                actual=actual,
                limit=None,
                observed=None,
            ),
        ],
    )


def _staged_identity(staged: StagedResourceRef) -> tuple[str, str, str]:
    return staged.staging_id, staged.owner_job_id, staged.proposal_key


def _discard_unreferenced_staged(
    resource_store: ResourceStore,
    staged_refs: tuple[StagedResourceRef, ...],
    receipt: RuntimeExecutionReceipt,
) -> None:
    authoritative = {
        _staged_identity(proposal.staged_resource_ref)
        for proposal in receipt.job_outcome.proposed_evidence
        if proposal.staged_resource_ref is not None
    }
    authoritative.update(
        _staged_identity(proposal.staged_resource_ref)
        for proposal in receipt.job_outcome.proposed_artifacts
    )
    discard_staged(
        resource_store,
        [
            staged
            for staged in staged_refs
            if _staged_identity(staged) not in authoritative
        ],
    )


class _BorrowedLogSink:
    """Let each Backend pass close its view without closing the shared sink."""

    def __init__(self, sink: Any) -> None:
        self._sink = sink

    def write(self, chunk: bytes) -> None:
        self._sink.write(chunk)

    def flush(self) -> None:
        self._sink.flush()

    def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class MethodsPreprocessingExecution:
    """All Pass-A values needed by verification, staging, and mapping."""

    validated: ValidatedMethodsPreprocessing
    frozen: FrozenMethodsWorkspaceInputs
    claim: LogparseParseClaim | None
    secrets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MethodsPreflightState:
    """Product-owned requirement projection before either Methods pass."""

    missing_user_inputs: tuple[str, ...]
    missing_log_archive: bool
    input_templates: dict[str, dict[str, Any]]
    log_archive_template: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MethodsUserInputProjection:
    """Declared Methods inputs that are active for the current Job facts."""

    active_required_names: tuple[str, ...]
    input_templates: dict[str, dict[str, Any]]
    log_archive_template: dict[str, Any]


def _binding_user_fact_name(binding: object) -> str | None:
    if binding is None:
        return None
    if not isinstance(binding, dict):
        raise ValueError("Methods preflight binding must be an object")
    if set(binding) == {"source", "name"} and binding.get("source") == "USER_FACT":
        name = binding.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Methods preflight USER_FACT name is invalid")
        return name
    if set(binding) == {"source", "value"} and binding.get("source") == "SKILL_FIXED":
        return None
    raise ValueError("Methods preflight binding shape is invalid")


def _methods_user_input_projection(
    skill: ResolvedSpecializedSkillV1,
    supplied_names: set[str],
) -> MethodsUserInputProjection:
    """Activate required roles and only the optional roles represented by facts."""

    expanded = expand_profile_requirements(
        skill.registration.preprocessing.roles,
        requires_logparse=True,
    )
    profile_by_name = {item["name"]: item for item in expanded}
    archive_template = profile_by_name.get("log_archive")
    if not isinstance(archive_template, dict):
        raise ValueError("built-in log archive requirement is unavailable")

    declared = set(skill.methods.required_user_inputs)
    plan_names: set[str] = set()
    mandatory_names: set[str] = set()
    input_templates: dict[str, dict[str, Any]] = {}
    preprocessing = skill.registration.preprocessing
    plan = preprocessing.logparse_plan
    if preprocessing.requires_logparse:
        if not isinstance(plan, dict):
            raise ValueError("Methods Logparse registration lacks a plan")

        problem_name = _binding_user_fact_name(plan.get("problem_time_binding"))
        if problem_name is not None:
            plan_names.add(problem_name)
            mandatory_names.add(problem_name)
            template = profile_by_name.get("problem_time")
            if not isinstance(template, dict):
                raise ValueError("built-in problem_time requirement is unavailable")
            input_templates[problem_name] = {
                **deepcopy(template),
                "name": problem_name,
            }

        anchors = plan.get("anchors")
        if not isinstance(anchors, list) or len(anchors) != len(preprocessing.roles):
            raise ValueError("Methods Logparse anchors do not match roles")
        for role, anchor in zip(preprocessing.roles, anchors, strict=True):
            if not isinstance(anchor, dict) or anchor.get("label") != role["label"]:
                raise ValueError("Methods Logparse anchor order is invalid")
            role_bindings: list[tuple[str, str]] = []
            for field in ("module", "slot", "process_name", "pid"):
                actual_name = _binding_user_fact_name(anchor.get(field))
                if actual_name is None:
                    continue
                plan_names.add(actual_name)
                role_bindings.append((field, actual_name))
                if field in {"slot", "process_name", "pid"}:
                    template = profile_by_name.get(f"{role['label']}_{field}")
                    if not isinstance(template, dict):
                        raise ValueError("built-in role requirement is unavailable")
                    input_templates.setdefault(
                        actual_name,
                        {**deepcopy(template), "name": actual_name},
                    )
            role_is_active = role["presence"] == "REQUIRED" or any(
                name in supplied_names for _, name in role_bindings
            )
            if role_is_active:
                mandatory_names.update(
                    name for field, name in role_bindings if field != "pid"
                )
    elif plan is not None:
        raise ValueError("non-Logparse Methods registration unexpectedly has a plan")

    # Package-only inputs are still server-preflight material, but are not
    # allowed to weaken the product profile or invent attachment contracts.
    mandatory_names.update(declared - plan_names)
    if not mandatory_names.issubset(declared):
        raise ValueError("Methods plan input is absent from required_user_inputs")
    for name in declared:
        input_templates.setdefault(
            name,
            {
                "name": name,
                "prompt": f"Provide the required Methods input '{name}'.",
                "constraints": {
                    "value_type": "STRING",
                    "min_utf8_bytes": 1,
                    "max_utf8_bytes": 4096,
                    "pattern": None,
                    "allowed_values": [],
                },
                "supplement_policy": "MISSING_ONLY",
            },
        )

    return MethodsUserInputProjection(
        active_required_names=tuple(
            name
            for name in skill.methods.required_user_inputs
            if name in mandatory_names
        ),
        input_templates=input_templates,
        log_archive_template=deepcopy(archive_template),
    )


def _methods_preflight_state(
    job: Job,
    aggregate: CaseAggregate,
    skill: ResolvedSpecializedSkillV1,
) -> MethodsPreflightState:
    """Project the registration onto product-owned PendingRequirements.

    The built-in profile remains authoritative for the global timestamp,
    role slot/process fields, and archive.  A generated package may declare
    additional string inputs; those receive a deliberately narrower generic
    bound instead of inheriting the old unconstrained 64 KiB envelope.
    """

    supplied_names: set[str] = set()
    for item in job.context_snapshot.user_facts:
        name = item.provenance.input_name
        if name is None or name in supplied_names:
            raise ValueError("Methods user facts must have unique input names")
        supplied_names.add(name)

    projection = _methods_user_input_projection(skill, supplied_names)

    unsupported_artifacts = set(skill.methods.required_artifacts) - {"log_archive"}
    if unsupported_artifacts:
        raise ValueError("Methods preflight cannot request an unsupported artifact")
    has_log_source = bool(job.attachment_refs) or any(
        artifact_id in aggregate.artifacts
        and aggregate.artifacts[artifact_id].kind is ArtifactKind.LOGPARSE_RUN
        for artifact_id in job.artifact_refs
    )
    missing_log_archive = (
        "log_archive" in skill.methods.required_artifacts and not has_log_source
    )
    return MethodsPreflightState(
        missing_user_inputs=tuple(
            name
            for name in projection.active_required_names
            if name not in supplied_names
        ),
        missing_log_archive=missing_log_archive,
        input_templates=projection.input_templates,
        log_archive_template=projection.log_archive_template,
    )


def _borrow_log_sinks(sinks: ExecutionLogSinks) -> ExecutionLogSinks:
    return ExecutionLogSinks(
        stdout=_BorrowedLogSink(sinks.stdout),
        stderr=_BorrowedLogSink(sinks.stderr),
        combined_limit_bytes=sinks.combined_limit_bytes,
    )


def _close_log_sinks(sinks: ExecutionLogSinks) -> None:
    closed: set[int] = set()
    failure: BaseException | None = None
    for sink in (sinks.stdout, sinks.stderr):
        identity = id(sink)
        if identity in closed:
            continue
        closed.add(identity)
        try:
            sink.close()
        except BaseException as exc:
            failure = failure or exc
    if failure is not None:
        raise failure


def _method_grounding_audit_from_bytes(data: bytes) -> MethodGroundingAuditV1:
    value = parse_canonical_json_bytes(data)
    fields = {
        "schema_version",
        "registration_id",
        "registration_sha256",
        "package_tree_sha256",
        "combined_sha256",
        "logparse_receipt_sha256",
        "status",
        "confirmed_methods",
        "evidence_count",
        "checked_source_count",
        "skill_load",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Methods grounding audit fields are invalid")
    load = value["skill_load"]
    if not isinstance(load, dict) or set(load) != {
        "package_tree_sha256",
        "scanned_source_ids",
        "marker_hits",
        "loaded_method_ids",
    }:
        raise ValueError("Methods skill-load receipt fields are invalid")
    marker_hits_raw = load["marker_hits"]
    if (
        not isinstance(marker_hits_raw, list)
        or any(
            not isinstance(item, list)
            or len(item) != 3
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
            or type(item[2]) is not int
            or item[2] < 1
            for item in marker_hits_raw
        )
    ):
        raise ValueError("Methods marker hits are invalid")
    string_arrays = (
        value["confirmed_methods"],
        load["scanned_source_ids"],
        load["loaded_method_ids"],
    )
    if any(
        not isinstance(items, list)
        or any(not isinstance(item, str) or not item for item in items)
        for items in string_arrays
    ):
        raise ValueError("Methods grounding audit string arrays are invalid")
    if (
        value["schema_version"] != 1
        or type(value["evidence_count"]) is not int
        or value["evidence_count"] < 0
        or type(value["checked_source_count"]) is not int
        or value["checked_source_count"] < 0
    ):
        raise ValueError("Methods grounding audit counters are invalid")
    skill_load = SkillLoadReceiptV1(
        package_tree_sha256=load["package_tree_sha256"],
        scanned_source_ids=tuple(load["scanned_source_ids"]),
        marker_hits=tuple(
            (item[0], item[1], item[2]) for item in marker_hits_raw
        ),
        loaded_method_ids=tuple(load["loaded_method_ids"]),
    )
    return MethodGroundingAuditV1(
        schema_version=1,
        registration_id=value["registration_id"],
        registration_sha256=value["registration_sha256"],
        package_tree_sha256=value["package_tree_sha256"],
        combined_sha256=value["combined_sha256"],
        logparse_receipt_sha256=value["logparse_receipt_sha256"],
        status=value["status"],
        confirmed_methods=tuple(value["confirmed_methods"]),
        evidence_count=value["evidence_count"],
        checked_source_count=value["checked_source_count"],
        skill_load=skill_load,
    )


class DiagnosisRuntime:
    """Execute one frozen Job without mutating Case or Diagnosis state."""

    def __init__(
        self,
        *,
        state_repository: StateRepository,
        resource_store: ResourceStore,
        asset_catalog: AssetCatalogPort,
        logparse_broker_factory: LogparseBrokerFactory | None,
        execution_records: ExecutionRecordStore,
        clock: Clock,
        id_generator: IdGenerator,
        workspace_manager: WorkspaceManager,
        backend: AgentBackend,
        context_builder: ContextBuilder | None = None,
        backend_test_limits: BackendExecutionLimits | None = None,
        generic_locator_executor: GenericLocatorExecutor | None = None,
    ) -> None:
        self._state_repository = state_repository
        self._resource_store = resource_store
        self._asset_resolver = RuntimeAssetResolver(asset_catalog)
        self._logparse_broker_factory = logparse_broker_factory
        self._execution_records = execution_records
        self._workspace_manager = workspace_manager
        self._backend = backend
        self._context_builder = context_builder or ContextBuilder()
        self._backend_test_limits = backend_test_limits
        self._clock = clock
        self._id_generator = id_generator
        self._publisher = OutcomePublisher(execution_records, clock, id_generator)
        self._generic_locator_executor = generic_locator_executor or GenericLocatorExecutor(
            backend=backend,
            workspace_manager=workspace_manager,
            execution_records=execution_records,
            clock=clock,
            id_generator=id_generator,
            backend_test_limits=backend_test_limits,
        )

    def execute(
        self,
        job: Job,
        cancellation: CancellationSignal,
    ) -> RuntimeExecutionReceipt:
        failure: ExecutionFailure | None = None
        try:
            return self._execute(job, cancellation)
        except RuntimeInfrastructureError as exc:
            record_stage_failed(exc.execution_failure)
            raise
        except ApplicationPortError as exc:
            if exc.error.code in {
                ErrorCode.STATE_CORRUPT,
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            }:
                raise
            failure = _unexpected_failure().failure
        except RuntimeExecutionError as exc:
            failure = exc.failure
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            failure = _unexpected_failure().failure
        assert failure is not None
        if job.diagnosis_mode is DiagnosisMode.GENERIC and failure.retryable:
            failure = ExecutionFailure(
                stage=failure.stage,
                code=failure.code,
                message=failure.message,
                retryable=False,
                details=list(failure.details),
            )
        record_stage_failed(failure)
        # Publish outside the handler so the public infrastructure exception
        # cannot retain an unsafe parser, OS, or adapter exception as context.
        publishing = record_stage_started(
            ExecutionStage.EXECUTION_RECORD,
            data={"operation": "publish_failure"},
        )
        try:
            receipt = self._publisher.publish_failure(job, failure)
        except RuntimeInfrastructureError as exc:
            record_stage_failed(exc.execution_failure)
            raise
        record_stage_completed(
            ExecutionStage.EXECUTION_RECORD,
            publishing,
            data={
                "operation": "publish_failure",
                "outcome_file_ref": receipt.outcome_file_ref,
            },
        )
        self._record_produced_outcome(receipt)
        return receipt

    def _execute(
        self,
        job: Job,
        cancellation: CancellationSignal,
    ) -> RuntimeExecutionReceipt:
        resolving = record_stage_started(ExecutionStage.ASSET_RESOLUTION)
        assets = self._resolve_assets(job)
        record_stage_completed(
            ExecutionStage.ASSET_RESOLUTION,
            resolving,
            data={
                "agent_profile_ref": job.agent_profile_ref,
                "diagnosis_skill_ref": job.skill_ref,
                "tool_bundle_ref": job.tool_bundle_ref,
                "context_policy_ref": job.context_policy_ref,
                "output_contract_ref": job.output_contract_ref,
                "logparse_tool_ref": job.logparse_tool_ref,
            },
        )
        if job.status is not JobStatus.RUNNING:
            raise runtime_failure(
                stage=ExecutionStage.OUTCOME_VALIDATE,
                code=ErrorCode.OUTCOME_INVALID,
                message="Runtime requires an already-claimed RUNNING Job.",
        )
        if job.diagnosis_mode is DiagnosisMode.GENERIC:
            aggregate = self._read_case(job)
            execution = self._generic_locator_executor.execute(
                job=job,
                aggregate=aggregate,
                assets=assets,
                resource_store=self._resource_store,
                cancellation=cancellation,
            )
            publishing = record_stage_started(
                ExecutionStage.EXECUTION_RECORD,
                data={"operation": "publish_generic_success"},
            )
            try:
                receipt = self._publisher.publish_success(
                    job,
                    execution.outcome,
                    execution.workspace.manifest,
                )
            except (TypeError, ValueError):
                raise _unexpected_failure() from None
            record_stage_completed(
                ExecutionStage.EXECUTION_RECORD,
                publishing,
                data={
                    "operation": "publish_generic_success",
                    "outcome_file_ref": receipt.outcome_file_ref,
                },
            )
            self._record_produced_outcome(receipt)
            return receipt
        if job.job_type is JobType.ROUTE and not job.available_skill_refs:
            return self._publish_no_capability(job)
        aggregate = self._read_case(job)
        prior_methods_diagnosis: VerifiedMethodDiagnosisV1 | None = None
        prior_methods_diagnosis_bytes: bytes | None = None
        prior_methods_audit_bytes: bytes | None = None
        try:
            methods_skill: ResolvedSpecializedSkillV1 | None = None
            if (
                job.job_type is JobType.DIAGNOSE
                and job.diagnosis_mode is DiagnosisMode.SPECIALIZED
            ):
                methods_skill = self._resolved_methods_skill(assets)
                preflight = _methods_preflight_state(job, aggregate, methods_skill)
                if preflight.missing_user_inputs or preflight.missing_log_archive:
                    return self._publish_methods_preflight(
                        job,
                        aggregate,
                        methods_skill,
                        preflight=preflight,
                    )
            try:
                broker_plan = compile_resolved_logparse_plan(job, aggregate, assets)
            except ResolvedLogparsePlanNotReady as exc:
                raise ValueError(
                    "complete Methods preflight inputs did not resolve a Logparse plan"
                ) from exc
            if job.logparse_tool_ref is not None and broker_plan is None:
                raise ValueError(
                    "logparse compiler omitted a plan without a missing binding"
                )
            resolved_logparse_plan = (
                None
                if broker_plan is None
                else ResolvedLogparsePlanInput(
                    schema_version=2,
                    attachment_id=broker_plan.attachment_id,
                    artifact_id=broker_plan.artifact_id,
                    problem_time=broker_plan.problem_time,
                    anchors=[
                        ResolvedLogparseAnchor(
                            label=item.label,
                            module=item.module,
                            slot=item.slot,
                            process_name=item.process_name,
                            pid=item.pid,
                        )
                        for item in broker_plan.anchors
                    ],
                )
            )
            if job.job_type is JobType.REVIEW:
                methods_skill = self._resolved_methods_skill(assets)
                (
                    prior_methods_diagnosis,
                    prior_methods_diagnosis_bytes,
                    prior_methods_audit_bytes,
                ) = self._prior_methods_diagnosis(job, aggregate, methods_skill)
                review_subject = self._methods_review_subject(
                    job,
                    prior_methods_diagnosis,
                )
            else:
                review_subject = None
        except (OSError, TypeError, ValueError):
            raise runtime_failure(
                stage=ExecutionStage.ASSET_RESOLUTION,
                code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
                message="The pinned Skill could not produce immutable verification bindings.",
            ) from None
        preparing = record_stage_started(ExecutionStage.WORKSPACE_PREPARE)
        workspace = self._workspace_manager.prepare(
            job,
            aggregate,
            self._resource_store,
            resolved_logparse_plan=resolved_logparse_plan,
            review_subject=review_subject,
        )
        if job.job_type is JobType.REVIEW:
            assert prior_methods_diagnosis_bytes is not None
            assert prior_methods_audit_bytes is not None
            self._workspace_manager.freeze_methods_review_inputs(
                workspace,
                diagnosis_bytes=prior_methods_diagnosis_bytes,
                grounding_audit_bytes=prior_methods_audit_bytes,
            )
        if review_subject is not None:
            self._publish_audit_bytes(
                job,
                "review_subject.json",
                canonical_json_bytes(review_subject),
            )
        review_loaded_method_ids = (
            prior_methods_diagnosis.audit.skill_load.loaded_method_ids
            if prior_methods_diagnosis is not None
            else None
        )
        resolved = assets.bind_workspace(
            workspace,
            loaded_method_ids=review_loaded_method_ids,
        )
        record_stage_completed(
            ExecutionStage.WORKSPACE_PREPARE,
            preparing,
            data={
                "workspace_root": workspace.root,
                "manifest_bytes": len(workspace.manifest_bytes),
                "attachment_count": len(workspace.attachments),
                "evidence_count": len(workspace.evidence),
                "artifact_count": len(workspace.artifacts),
                "previous_outcome_count": len(workspace.previous_outcomes),
            },
        )
        methods_preprocessing: MethodsPreprocessingExecution | None = None
        methods_skill_load: SkillLoadReceiptV1 | None = None
        if (
            job.job_type is JobType.DIAGNOSE
            and job.diagnosis_mode is DiagnosisMode.SPECIALIZED
            and workspace.manifest.resolved_logparse_plan is not None
        ):
            with self._shared_backend_log_sinks(job) as shared_log_sinks:
                methods_preprocessing = self._run_methods_preprocessing(
                    job=job,
                    aggregate=aggregate,
                    main_workspace=workspace,
                    assets=assets,
                    cancellation=cancellation,
                    shared_log_sinks=shared_log_sinks,
                )
                methods_skill = self._resolved_methods_skill(assets)
                methods_skill_load = scan_method_markers(
                    skill=methods_skill,
                    target_logs=methods_preprocessing.frozen.target_logs,
                )
                resolved = assets.bind_workspace(
                    workspace,
                    loaded_method_ids=methods_skill_load.loaded_method_ids,
                )
                context = self._materialize_context(
                    job,
                    workspace,
                    resolved.materials,
                )
                methods_prompt = (
                    context.body
                    + "\n\n<<<METHODS_FROZEN_EXECUTION_BOUNDARY>>>\n"
                    "Logparse preprocessing is complete and its capability has been revoked. "
                    "Read inputs/request.json, inputs/target_logs.json, only the log_path files "
                    "listed there, and inputs/logparse-receipt.json. Do not invoke Logparse, do "
                    "not traverse output/, and write only output/method-diagnosis.draft.json.\n"
                    "<<<END METHODS_FROZEN_EXECUTION_BOUNDARY>>>\n"
                )
                self._backend.execute(
                    prompt=methods_prompt,
                    workspace_root=workspace.root,
                    cancellation=cancellation,
                    log_sinks=_borrow_log_sinks(shared_log_sinks),
                    resource_limits=job.resource_limits,
                    broker_environment=None,
                    test_limits=self._backend_test_limits,
                )
            secrets = methods_preprocessing.secrets
            parse_request_bytes = methods_preprocessing.validated.request_bytes
            claim = methods_preprocessing.claim
            broker_audit_bytes = methods_preprocessing.validated.broker_audit_bytes
        else:
            context = self._materialize_context(
                job,
                workspace,
                resolved.materials,
            )
            backend_prompt = context.body
            if job.job_type is JobType.REVIEW:
                backend_prompt += (
                    "\n\n<<<METHODS_REVIEW_BOUNDARY>>>\n"
                    "Independently review inputs/method-diagnosis.json against the fixed "
                    "Candidate, Evidence, and inputs/method-grounding-audit.json. Preserve "
                    "each exact (method_id, identity_tokens) pair. Write only "
                    "output/method-review.draft.json. Logparse is unavailable.\n"
                    "<<<END METHODS_REVIEW_BOUNDARY>>>\n"
                )
            secrets, parse_request_bytes, claim, broker_audit_bytes = self._execute_backend(
                job,
                workspace,
                cancellation,
                backend_prompt,
            )
        if broker_audit_bytes is not None:
            self._publish_audit_bytes(job, "broker_audit.json", broker_audit_bytes)
        validating = record_stage_started(ExecutionStage.OUTCOME_VALIDATE)
        try:
            validated_draft = read_agent_output(
                workspace,
                job,
                workspace.manifest,
                secrets=secrets,
                broker_audit_bytes=broker_audit_bytes,
            )
        except RejectedAgentOutputError as exc:
            self._archive_rejected_agent_output(job, exc)
            raise
        diagnosis_audit = self._diagnosis_audit_for_review(job, aggregate)
        verification = None
        if isinstance(validated_draft, ValidatedMethodDiagnosisDraft):
            if methods_preprocessing is None or methods_skill_load is None:
                raise _unexpected_failure()
            try:
                skill = self._resolved_methods_skill(assets)
                for resource in methods_preprocessing.validated.proposal_resources:
                    resource.verify_unchanged()
                verified_diagnosis = verify_method_diagnosis(
                    skill=skill,
                    draft=validated_draft.draft,
                    target_logs=methods_preprocessing.frozen.target_logs,
                    logparse_receipt_sha256=methods_preprocessing.frozen.receipt_sha256,
                    skill_load=methods_skill_load,
                )
                mapped = map_verified_methods_draft(
                    job=job,
                    manifest=workspace.manifest,
                    source_draft_bytes=validated_draft.canonical_bytes,
                    verified_diagnosis=verified_diagnosis,
                    preprocessing=methods_preprocessing,
                )
                for resource in mapped.proposal_resources:
                    resource.verify_unchanged()
            except (TypeError, ValueError) as exc:
                reason_code = _method_validation_reason_code(exc)
                diagnostic_id = self._id_generator.new("diagnostic")
                raise RuntimeExecutionError(
                    ExecutionFailure(
                        stage=ExecutionStage.OUTCOME_VALIDATE,
                        code=ErrorCode.OUTCOME_INVALID,
                        message=(
                            "Methods diagnosis draft is not grounded in the frozen inputs."
                        ),
                        retryable=False,
                        details=[],
                        reason_code=reason_code,
                        diagnostic_id=diagnostic_id,
                    )
                ) from None
            self._publish_audit_bytes(
                job,
                "method-diagnosis.draft.json",
                validated_draft.canonical_bytes,
            )
            self._publish_audit_bytes(
                job,
                "method-grounding-audit.json",
                canonical_json_bytes(asdict(verified_diagnosis.audit)),
            )
            final_draft = mapped.draft
            final_draft_bytes = mapped.draft_bytes
            verification = mapped.verification
            proposal_resources = mapped.proposal_resources
            authoritative_targets = mapped.authoritative_targets
            target_logs = mapped.target_logs
        elif isinstance(validated_draft, ValidatedMethodReviewDraft):
            if prior_methods_diagnosis is None or diagnosis_audit is None:
                raise _unexpected_failure()
            try:
                verified_review = verify_method_review(
                    prior_methods_diagnosis,
                    validated_draft.draft,
                )
                mapped = map_verified_methods_draft(
                    job=job,
                    manifest=workspace.manifest,
                    source_draft_bytes=validated_draft.canonical_bytes,
                    verified_diagnosis=prior_methods_diagnosis,
                    verified_review=verified_review,
                    diagnosis_audit=diagnosis_audit,
                )
            except (TypeError, ValueError):
                raise runtime_failure(
                    stage=ExecutionStage.OUTCOME_VALIDATE,
                    code=ErrorCode.OUTCOME_INVALID,
                    message="Methods review does not cover the exact grounded diagnosis.",
                ) from None
            self._publish_audit_bytes(
                job,
                "method-review.draft.json",
                validated_draft.canonical_bytes,
            )
            final_draft = mapped.draft
            final_draft_bytes = mapped.draft_bytes
            verification = mapped.verification
            proposal_resources = mapped.proposal_resources
            authoritative_targets = mapped.authoritative_targets
            target_logs = mapped.target_logs
        else:
            assert isinstance(validated_draft, ValidatedAgentDraft)
            if job.job_type is not JobType.ROUTE:
                raise _unexpected_failure()
            self._publish_audit_bytes(
                job,
                "agent_job_outcome.draft.json",
                validated_draft.canonical_bytes,
            )
            final_draft = validated_draft.draft
            final_draft_bytes = validated_draft.canonical_bytes
            proposal_resources = validated_draft.proposal_resources
            authoritative_targets = validated_draft.authoritative_targets
            target_logs = validated_draft.target_logs
        if verification is not None:
            self._publish_audit_bytes(
                job,
                "decision_audit.json",
                canonical_json_bytes(verification.audit),
            )
            self._publish_audit_bytes(
                job,
                "decision_evidence.jsonl",
                verification.decision_evidence_bytes,
            )
        finalized = finalize_server_outcome(
            workspace_root=workspace.root,
            job=job,
            manifest=workspace.manifest,
            draft=final_draft,
            draft_bytes=final_draft_bytes,
            outcome_id=self._id_generator.new("job_outcome"),
            produced_at=self._clock.now(),
            verification=verification,
            authoritative_targets=authoritative_targets,
            target_logs=target_logs,
        )
        self._publish_audit_bytes(
            job,
            "agent_job_outcome.json",
            finalized.canonical_bytes,
        )
        self._publish_audit_bytes(
            job,
            "finalization_manifest.json",
            canonical_json_bytes(finalized.marker),
        )
        server_resources = tuple(
            ValidatedProposalResource(
                draft=item.draft,
                proposal_key=item.draft.proposal_key,
                workspace_relative_path=item.draft.workspace_relative_path,
                path=workspace.root / item.draft.workspace_relative_path,
                resource_kind=item.draft.resource_kind,
                size=len(item.content),
                sha256=bytes_sha256(item.content),
                tree_manifest=None,
                source_snapshot=None,
                inline_bytes=item.content,
            )
            for item in finalized.generated_result_files
        )
        validated = ValidatedAgentOutput(
            outcome=finalized.outcome,
            canonical_bytes=finalized.canonical_bytes,
            proposal_resources=(
                *proposal_resources,
                *server_resources,
            ),
            user_result=finalized.user_result,
        )
        record_stage_completed(
            ExecutionStage.OUTCOME_VALIDATE,
            validating,
            data={
                "result_type": validated.outcome.result_type,
                "proposal_resource_count": len(validated.proposal_resources),
                "canonical_bytes": len(validated.canonical_bytes),
                "has_user_result": validated.user_result is not None,
                "workspace_outcome_path": workspace.outcome_path,
            },
        )

        staging = record_stage_started(ExecutionStage.RESOURCE_STAGE)
        staged = stage_validated_output(
            job=job,
            workspace_manifest=workspace.manifest,
            validated=validated,
            resource_store=self._resource_store,
            claim=claim,
            parse_request_bytes=parse_request_bytes,
        )
        record_stage_completed(
            ExecutionStage.RESOURCE_STAGE,
            staging,
            data={
                "staged_refs": staged.staged_refs,
                "outcome_id": staged.outcome.outcome_id,
                "proposed_evidence_count": len(staged.outcome.proposed_evidence),
                "proposed_artifact_count": len(staged.outcome.proposed_artifacts),
            },
        )
        publishing = record_stage_started(
            ExecutionStage.EXECUTION_RECORD,
            data={"operation": "publish_success"},
        )
        try:
            receipt = self._publisher.publish_success(
                job,
                staged.outcome,
                workspace.manifest,
            )
        except (TypeError, ValueError):
            # This is the publisher's pre-I/O validation boundary.  Store
            # failures and ambiguous commits are contained inside the
            # publisher and surface only as RuntimeInfrastructureError, where
            # cleanup is intentionally forbidden.
            discard_staged(self._resource_store, staged.staged_refs)
            raise _unexpected_failure() from None
        record_stage_completed(
            ExecutionStage.EXECUTION_RECORD,
            publishing,
            data={
                "operation": "publish_success",
                "outcome_file_ref": receipt.outcome_file_ref,
            },
        )
        self._record_produced_outcome(receipt)
        if receipt.job_outcome != staged.outcome:
            _discard_unreferenced_staged(
                self._resource_store,
                staged.staged_refs,
                receipt,
            )
        return receipt

    def _publish_methods_preflight(
        self,
        job: Job,
        aggregate: CaseAggregate,
        skill: ResolvedSpecializedSkillV1,
        *,
        preflight: MethodsPreflightState | None = None,
    ) -> RuntimeExecutionReceipt:
        """Publish missing Methods inputs without starting an Agent session.

        Methods drafts are evidence-only and cannot request user material.  This
        product-owned preflight is therefore the sole no-plan specialized path;
        a Backend is reached only after every required input and log source can
        be frozen into a resolved Logparse plan.
        """

        if (
            job.job_type is not JobType.DIAGNOSE
            or job.diagnosis_mode is not DiagnosisMode.SPECIALIZED
            or job.skill_ref is None
            or skill.combined_sha256 != job.skill_ref.content_hash
        ):
            raise ValueError("Methods preflight requires a pinned specialized Job")
        open_requirements = [
            item
            for item in job.context_snapshot.pending_requirements
            if item.status is RequirementStatus.OPEN
        ]
        if open_requirements:
            raise ValueError("an executing Methods Job cannot inherit OPEN requirements")
        current = preflight or _methods_preflight_state(job, aggregate, skill)
        missing_inputs = list(current.missing_user_inputs)
        missing_log_archive = current.missing_log_archive
        if not missing_inputs and not missing_log_archive:
            raise ValueError("Methods plan is not ready despite complete required inputs")

        requirements: list[PendingRequirement] = []
        requested_input: list[str] = []
        requested_attachments: list[str] = []
        for name in missing_inputs:
            template = current.input_templates[name]
            requirement_id = self._id_generator.derive(
                "pending_requirement",
                (job.job_id, "INPUT", name),
            )
            requirements.append(
                PendingRequirement(
                    requirement_id=requirement_id,
                    kind=RequirementKind.INPUT,
                    name=name,
                    prompt=template["prompt"],
                    required=True,
                    constraints=InputRequirementConstraints.model_validate(
                        template["constraints"]
                    ),
                    status=RequirementStatus.OPEN,
                    requested_by_job_id=job.job_id,
                    fulfilled_by_refs=[],
                    supplement_policy=SupplementPolicy(
                        template["supplement_policy"]
                    ),
                )
            )
            requested_input.append(requirement_id)
        if missing_log_archive:
            template = current.log_archive_template
            requirement_id = self._id_generator.derive(
                "pending_requirement",
                (job.job_id, "ATTACHMENT", "log_archive"),
            )
            requirements.append(
                PendingRequirement(
                    requirement_id=requirement_id,
                    kind=RequirementKind.ATTACHMENT,
                    name="log_archive",
                    prompt=template["prompt"],
                    required=True,
                    constraints=AttachmentRequirementConstraints.model_validate(
                        template["constraints"]
                    ),
                    status=RequirementStatus.OPEN,
                    requested_by_job_id=job.job_id,
                    fulfilled_by_refs=[],
                    supplement_policy=SupplementPolicy(
                        template["supplement_policy"]
                    ),
                )
            )
            requested_attachments.append(requirement_id)

        result_type = (
            OutcomeResultType.NEED_INPUT
            if requested_input
            else OutcomeResultType.NEED_ATTACHMENT
        )
        validating = record_stage_started(
            ExecutionStage.OUTCOME_VALIDATE,
            data={"operation": "methods_server_preflight"},
        )
        outcome = JobOutcome(
            outcome_id=self._id_generator.new("job_outcome"),
            job_id=job.job_id,
            case_id=job.case_id,
            job_type=job.job_type,
            base_state_revision=job.base_state_revision,
            result_type=result_type,
            payload=DiagnosisOutcome(
                findings=[],
                state_delta=DiagnosisStateDelta(
                    problem_spec_patch=None,
                    add_user_facts=[],
                    proposed_facts=[],
                    add_active_hypotheses=[],
                    update_hypotheses=[],
                    reject_hypotheses=[],
                    add_open_questions=[],
                    resolve_questions=[],
                    add_pending_requirements=requirements,
                    fulfill_requirements=[],
                    add_evidence_bindings=[],
                ),
                requested_input=requested_input,
                requested_attachments=requested_attachments,
                candidate_conclusion_draft=None,
                recommended_next_step=(
                    "Supply the required Methods inputs and log archive."
                    if requested_input and requested_attachments
                    else (
                        "Supply the required Methods inputs."
                        if requested_input
                        else "Upload and submit the required log archive."
                    )
                ),
            ),
            consumed_evidence_refs=[],
            proposed_evidence=[],
            proposed_artifacts=[],
            error=None,
            produced_at=self._clock.now(),
            decision_audit=None,
        )
        self._publish_audit_bytes(
            job,
            "methods_preflight.json",
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "job_id": job.job_id,
                    "registration_id": skill.registration_id,
                    "result_type": result_type,
                    "missing_user_inputs": missing_inputs,
                    "missing_artifacts": (
                        ["log_archive"] if missing_log_archive else []
                    ),
                }
            ),
        )
        record_stage_completed(
            ExecutionStage.OUTCOME_VALIDATE,
            validating,
            data={
                "operation": "methods_server_preflight",
                "result_type": result_type,
                "requirement_count": len(requirements),
            },
        )
        publishing = record_stage_started(
            ExecutionStage.EXECUTION_RECORD,
            data={"operation": "publish_methods_preflight"},
        )
        try:
            receipt = self._publisher.publish_success(job, outcome)
        except (TypeError, ValueError):
            raise _unexpected_failure() from None
        record_stage_completed(
            ExecutionStage.EXECUTION_RECORD,
            publishing,
            data={
                "operation": "publish_methods_preflight",
                "outcome_file_ref": receipt.outcome_file_ref,
            },
        )
        self._record_produced_outcome(receipt)
        return receipt

    def _publish_no_capability(self, job: Job) -> RuntimeExecutionReceipt:
        outcome = JobOutcome(
            outcome_id=self._id_generator.new("job_outcome"),
            job_id=job.job_id,
            case_id=job.case_id,
            job_type=job.job_type,
            base_state_revision=job.base_state_revision,
            result_type=OutcomeResultType.NO_CAPABILITY,
            payload=RouteDecision(
                kind=RouteKind.NO_CAPABILITY,
                skill_ref=None,
                reason="No diagnosis skill is available in the production catalog.",
                confidence=1.0,
            ),
            consumed_evidence_refs=[],
            proposed_evidence=[],
            proposed_artifacts=[],
            error=None,
            produced_at=self._clock.now(),
        )
        publishing = record_stage_started(
            ExecutionStage.EXECUTION_RECORD,
            data={"operation": "publish_no_capability"},
        )
        try:
            receipt = self._publisher.publish_success(job, outcome)
        except (TypeError, ValueError):
            raise _unexpected_failure() from None
        record_stage_completed(
            ExecutionStage.EXECUTION_RECORD,
            publishing,
            data={
                "operation": "publish_no_capability",
                "outcome_file_ref": receipt.outcome_file_ref,
            },
        )
        self._record_produced_outcome(receipt)
        return receipt

    @staticmethod
    def _diagnosis_audit_for_review(
        job: Job,
        aggregate: CaseAggregate,
    ):
        if job.job_type is not JobType.REVIEW:
            return None
        matches = [
            aggregate.outcomes[outcome_id].decision_audit
            for outcome_id in job.previous_outcome_refs
            if outcome_id in aggregate.outcomes
            and aggregate.outcomes[outcome_id].job_type is JobType.DIAGNOSE
            and aggregate.outcomes[outcome_id].decision_audit is not None
        ]
        if len(matches) != 1:
            raise ValueError(
                "REVIEW requires exactly one private diagnosis DecisionAudit"
            )
        return matches[0]

    def _prior_methods_diagnosis(
        self,
        job: Job,
        aggregate: CaseAggregate,
        skill: ResolvedSpecializedSkillV1,
    ) -> tuple[VerifiedMethodDiagnosisV1, bytes, bytes]:
        matches = [
            aggregate.outcomes[outcome_id]
            for outcome_id in job.previous_outcome_refs
            if outcome_id in aggregate.outcomes
            and aggregate.outcomes[outcome_id].job_type is JobType.DIAGNOSE
        ]
        if len(matches) != 1:
            raise ValueError("Methods REVIEW requires one prior DIAGNOSE Outcome")
        source_job_id = matches[0].job_id
        diagnosis_bytes = self._execution_records.read_audit_bytes(
            source_job_id,
            "method-diagnosis.draft.json",
        )
        audit_bytes = self._execution_records.read_audit_bytes(
            source_job_id,
            "method-grounding-audit.json",
        )
        receipt_bytes = self._execution_records.read_audit_bytes(
            source_job_id,
            "methods_logparse_receipt.json",
        )
        if diagnosis_bytes is None or audit_bytes is None or receipt_bytes is None:
            raise ValueError("prior Methods diagnosis audit material is unavailable")
        diagnosis_value = parse_canonical_json_bytes(diagnosis_bytes)
        diagnosis = MethodDiagnosisDraftV1.from_mapping(diagnosis_value)
        audit = _method_grounding_audit_from_bytes(audit_bytes)
        if (
            audit.registration_id != skill.registration_id
            or audit.registration_sha256 != skill.registration_sha256
            or audit.package_tree_sha256 != skill.package_tree_sha256
            or audit.combined_sha256 != skill.combined_sha256
            or audit.skill_load.package_tree_sha256 != skill.package_tree_sha256
            or audit.logparse_receipt_sha256 != bytes_sha256(receipt_bytes)
            or audit.status != diagnosis.status
            or audit.confirmed_methods != diagnosis.confirmed_methods
            or audit.evidence_count != len(diagnosis.evidence)
        ):
            raise ValueError("prior Methods diagnosis identity is invalid")
        return (
            VerifiedMethodDiagnosisV1(draft=diagnosis, audit=audit),
            diagnosis_bytes,
            audit_bytes,
        )

    @staticmethod
    def _methods_review_subject(
        job: Job,
        diagnosis: VerifiedMethodDiagnosisV1,
    ) -> ReviewSubjectV2:
        if job.review_target is None or job.skill_ref is None:
            raise ValueError("Methods REVIEW lacks its immutable target")
        candidate = job.context_snapshot.candidate_conclusion
        if candidate is None:
            raise ValueError("Methods REVIEW lacks its immutable Candidate")
        candidate_required_evidence = set(review_required_evidence_refs(candidate))
        required_evidence = tuple(
            evidence_ref
            for evidence_ref in job.evidence_refs
            if evidence_ref in candidate_required_evidence
        )
        if set(required_evidence) != candidate_required_evidence:
            raise ValueError("Methods REVIEW required Evidence is not fixed by its Job")
        if not required_evidence or not diagnosis.draft.evidence:
            raise ValueError("Methods REVIEW requires grounded evidence")
        rule_ids: list[str] = []
        assertions: list[ReviewCausalAssertion] = []
        mechanical: list[MechanicalFact] = []
        for ordinal, evidence in enumerate(diagnosis.draft.evidence, start=1):
            identity_hash = canonical_json_sha256(
                {
                    "schema_version": 1,
                    "method_id": evidence.method_id,
                    "identity_tokens": sorted(evidence.identity_tokens),
                }
            )[:16]
            rule_id = f"methods:{evidence.method_id}:{identity_hash}"
            rule_ids.append(rule_id)
            assertions.append(
                ReviewCausalAssertion(
                    rule_id=rule_id,
                    statement=(
                        f"Independently review grounded method {evidence.method_id} "
                        "for the exact frozen evidence identity."
                    ),
                )
            )
            mechanical.append(
                MechanicalFact(
                    fact_id=f"methods-grounding-{ordinal}",
                    name="methods_grounding",
                    value="SERVER_GROUNDED",
                    source_rule_id=rule_id,
                    evidence_refs=list(required_evidence),
                )
            )
        for ordinal, method_id in enumerate(
            diagnosis.draft.candidate_methods,
            start=len(diagnosis.draft.evidence) + 1,
        ):
            rule_id = f"methods:candidate:{method_id}"
            rule_ids.append(rule_id)
            mechanical.append(
                MechanicalFact(
                    fact_id=f"methods-grounding-{ordinal}",
                    name="methods_grounding",
                    value="UNGROUNDED",
                    source_rule_id=rule_id,
                    evidence_refs=list(required_evidence),
                )
            )
        preimage = {
            "schema_version": 2,
            "review_job_id": job.job_id,
            "case_id": job.case_id,
            "reviewed_state_revision": job.base_state_revision,
            "skill_ref": job.skill_ref.model_dump(mode="json"),
            "candidate": candidate.model_dump(mode="json"),
            "causal_assertions": [item.model_dump(mode="json") for item in assertions],
            "required_rule_ids": rule_ids,
            "required_evidence_refs": list(required_evidence),
            "mechanical_facts": [item.model_dump(mode="json") for item in mechanical],
        }
        return ReviewSubjectV2.model_validate(
            {**preimage, "subject_hash": canonical_json_sha256(preimage)}
        )

    def _publish_audit_bytes(
        self,
        job: Job,
        filename: str,
        payload: bytes,
    ) -> None:
        """Persist one observable runtime input without exposing workspace paths."""

        try:
            self._execution_records.publish_audit_bytes(
                job.job_id,
                filename,
                payload,
            )
        except ApplicationPortError as exc:
            raise runtime_failure(
                stage=ExecutionStage.EXECUTION_RECORD,
                code=ErrorCode.EXECUTION_RECORD_FAILED,
                message="Execution audit material could not be published.",
                retryable=True,
                details=exc.error.details,
            ) from None
        except Exception as exc:
            raise runtime_failure(
                stage=ExecutionStage.EXECUTION_RECORD,
                code=ErrorCode.EXECUTION_RECORD_FAILED,
                message="Execution audit material could not be published.",
                retryable=True,
            ) from exc

    def _archive_rejected_agent_output(
        self,
        job: Job,
        rejection: RejectedAgentOutputError,
    ) -> None:
        raw_bytes = rejection.raw_outcome_bytes
        if raw_bytes is None:
            return
        try:
            file_ref = self._execution_records.publish_rejected_agent_output_bytes(
                job.job_id,
                raw_bytes,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            try:
                log_event(
                    "runtime.agent_output.archive_failed",
                    job_id=job.job_id,
                    job_type=job.job_type,
                    failure_category=rejection.failure_category,
                    raw_bytes=len(raw_bytes),
                    error=exc,
                )
            except Exception:
                pass
            return
        try:
            log_event(
                "runtime.agent_output.archived",
                job_id=job.job_id,
                job_type=job.job_type,
                failure_category=rejection.failure_category,
                archive_file_ref=file_ref.relative_key,
                archive_size=file_ref.size,
                archive_sha256=file_ref.sha256,
            )
        except Exception:
            pass

    @staticmethod
    def _record_produced_outcome(receipt: RuntimeExecutionReceipt) -> None:
        outcome = receipt.job_outcome
        record_journey_event(
            "job.outcome.produced",
            timestamp=outcome.produced_at,
            case_id=outcome.case_id,
            job_id=outcome.job_id,
            job_type=outcome.job_type,
            outcome_id=outcome.outcome_id,
            data={
                "state_applied": False,
                "outcome": outcome,
                "outcome_file_ref": receipt.outcome_file_ref,
                "stdout_ref": f"jobs/{outcome.job_id}/stdout.log",
                "stderr_ref": f"jobs/{outcome.job_id}/stderr.log",
            },
        )

    def _resolve_assets(self, job: Job) -> ResolvedJobAssets:
        try:
            return self._asset_resolver.resolve_job(job)
        except RuntimeExecutionError:
            raise
        except Exception:
            raise runtime_failure(
                stage=ExecutionStage.ASSET_RESOLUTION,
                code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
                message="A fixed runtime asset version is unavailable.",
            ) from None

    def _read_case(self, job: Job) -> CaseAggregate:
        try:
            aggregate = self._state_repository.read_case(job.case_id)
        except ApplicationPortError as exc:
            if exc.error.code is ErrorCode.CASE_NOT_FOUND:
                raise runtime_failure(
                    stage=ExecutionStage.WORKSPACE_PREPARE,
                    code=ErrorCode.RESOURCE_NOT_FOUND,
                    message="The fixed Job Case is unavailable.",
                    details=exc.error.details,
                ) from None
            if exc.error.code in {
                ErrorCode.STATE_CORRUPT,
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            }:
                raise
            raise runtime_failure(
                stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.WORKSPACE_PREPARE_FAILED,
                message="The fixed Job Case could not be read.",
                retryable=True,
                details=exc.error.details,
            ) from None
        except Exception as exc:
            raise runtime_failure(
                stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.WORKSPACE_PREPARE_FAILED,
                message="The fixed Job Case could not be read.",
                retryable=True,
            ) from exc
        if not isinstance(aggregate, CaseAggregate):
            raise runtime_failure(
                stage=ExecutionStage.OUTCOME_VALIDATE,
                code=ErrorCode.OUTCOME_INVALID,
                message="StateRepository returned an invalid fixed Case view.",
            )
        return aggregate

    def _build_context(
        self,
        job: Job,
        materials: ContextMaterials,
    ) -> BoundedContext:
        try:
            return self._context_builder.build(job, materials)
        except ContextLimitExceeded as exc:
            raise runtime_failure(
                stage=ExecutionStage.CONTEXT_BUILD,
                code=ErrorCode.CONTEXT_LIMIT,
                message="Required Agent context exceeds the fixed role budget.",
                details=[
                    ApplicationErrorDetail(
                        field="context_bytes",
                        resource_type="JOB",
                        resource_id=job.job_id,
                        resource_ref=None,
                        expected=exc.limit,
                        actual=exc.observed,
                        limit=exc.limit,
                        observed=exc.observed,
                    )
                ],
            ) from None
        except RuntimeExecutionError:
            raise
        except Exception:
            raise runtime_failure(
                stage=ExecutionStage.OUTCOME_VALIDATE,
                code=ErrorCode.OUTCOME_INVALID,
                message="The fixed Agent context is internally inconsistent.",
            ) from None

    def _materialize_context(
        self,
        job: Job,
        workspace: PreparedWorkspace,
        materials: ContextMaterials,
    ) -> BoundedContext:
        context_building = record_stage_started(ExecutionStage.CONTEXT_BUILD)
        context = self._build_context(job, materials)
        self._workspace_manager.write_context(workspace, context.body)
        self._publish_audit_bytes(
            job,
            "context.txt",
            context.body.encode("utf-8"),
        )
        record_stage_completed(
            ExecutionStage.CONTEXT_BUILD,
            context_building,
            data={
                "context_path": workspace.context_path,
                "utf8_bytes": context.utf8_bytes,
                "limit_bytes": context.limit_bytes,
                "body_sha256": context.body_sha256,
                "sections": context.sections,
            },
        )
        return context

    def _open_log_sinks(self, job: Job) -> ExecutionLogSinks:
        try:
            sinks = self._execution_records.open_log_sinks(
                job.job_id,
                job.resource_limits.stdout_stderr_bytes,
            )
        except ApplicationPortError as exc:
            raise runtime_failure(
                stage=ExecutionStage.EXECUTION_RECORD,
                code=ErrorCode.EXECUTION_RECORD_FAILED,
                message="Execution logs could not be opened.",
                retryable=True,
                details=exc.error.details,
            ) from None
        except Exception as exc:
            raise runtime_failure(
                stage=ExecutionStage.EXECUTION_RECORD,
                code=ErrorCode.EXECUTION_RECORD_FAILED,
                message="Execution logs could not be opened.",
                retryable=True,
            ) from exc
        if (
            not isinstance(sinks, ExecutionLogSinks)
            or sinks.combined_limit_bytes
            != job.resource_limits.stdout_stderr_bytes
        ):
            raise runtime_failure(
                stage=ExecutionStage.EXECUTION_RECORD,
                code=ErrorCode.EXECUTION_RECORD_FAILED,
                message="Execution log limits do not match the frozen Job.",
                retryable=True,
            )
        return sinks

    @contextmanager
    def _shared_backend_log_sinks(
        self,
        job: Job,
    ) -> Iterator[ExecutionLogSinks]:
        """Keep one aggregate byte budget across the two isolated passes."""

        sinks = self._open_log_sinks(job)
        primary: BaseException | None = None
        try:
            yield sinks
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                _close_log_sinks(sinks)
            except BaseException as exc:
                if primary is None:
                    raise runtime_failure(
                        stage=ExecutionStage.EXECUTION_RECORD,
                        code=ErrorCode.EXECUTION_RECORD_FAILED,
                        message="Execution log could not be finalized.",
                        retryable=True,
                    ) from exc

    @staticmethod
    def _resolved_methods_skill(
        assets: ResolvedJobAssets,
    ) -> ResolvedSpecializedSkillV1:
        if assets.skill is None:
            raise ValueError("Methods execution requires a pinned Skill")
        skill = load_specialized_skill_registration(Path(assets.skill.root_path))
        if skill.combined_sha256 != assets.skill.ref.content_hash:
            raise ValueError("registered Methods Skill differs from the pinned ref")
        return skill

    @staticmethod
    def _preprocessing_request(
        workspace: PreparedWorkspace,
    ) -> tuple[str, bytes]:
        plan = workspace.manifest.resolved_logparse_plan
        if plan is None:
            raise ValueError("Methods Pass A requires a resolved Logparse plan")
        anchors = [
            Anchor(
                label=item.label,
                module=item.module,
                slot=item.slot,
                process_name=item.process_name,
                pid=item.pid,
            )
            for item in plan.anchors
        ]
        if plan.attachment_id is not None:
            request = ParseTargetsRequest(
                schema_version=1,
                problem_time=str(plan.problem_time),
                anchors=anchors,
                attachment_id=plan.attachment_id,
                artifact_proposal_key="methods-preprocess",
            )
            return "parse-targets", canonical_json_bytes(request)
        assert plan.artifact_id is not None
        request = TargetLogsRequest(
            schema_version=1,
            problem_time=str(plan.problem_time),
            anchors=anchors,
            artifact_id=plan.artifact_id,
        )
        return "target-logs", canonical_json_bytes(request)

    @staticmethod
    def _methods_request_value(
        job: Job,
        workspace: PreparedWorkspace,
        skill: ResolvedSpecializedSkillV1,
    ) -> dict[str, Any]:
        by_name = {}
        for item in job.context_snapshot.user_facts:
            name = item.provenance.input_name
            if name is None:
                continue
            if name in by_name:
                raise ValueError("Methods user facts must have unique input names")
            by_name[name] = item
        projection = _methods_user_input_projection(skill, set(by_name))
        missing = [
            name
            for name in projection.active_required_names
            if name not in by_name
        ]
        if missing:
            raise ValueError(
                "Methods request is missing active required user inputs: "
                + ", ".join(missing)
            )
        user_inputs: list[dict[str, str]] = []
        for name in skill.methods.required_user_inputs:
            item = by_name.get(name)
            if item is None:
                # Inputs bound only to an inactive OPTIONAL role are not part of
                # this immutable execution request.  Any supplied binding would
                # activate the role as a group during server preflight above.
                continue
            user_inputs.append(
                {
                    "name": name,
                    "value": item.statement,
                    "source_fact_id": item.item_id,
                }
            )
        consumed_artifacts: list[dict[str, Any]] = []
        plan = workspace.manifest.resolved_logparse_plan
        if "log_archive" in skill.methods.required_artifacts:
            if plan is None:
                raise ValueError("Methods log_archive requires completed preprocessing")
            if plan.attachment_id is not None:
                attachment = next(
                    item
                    for item in workspace.attachments
                    if item.attachment_id == plan.attachment_id
                )
                consumed_artifacts.append(
                    {
                        "name": "log_archive",
                        "resource_type": "ATTACHMENT",
                        "resource_id": attachment.attachment_id,
                        "size": attachment.size,
                        "sha256": attachment.sha256,
                    }
                )
            else:
                assert plan.artifact_id is not None
                artifact = next(
                    item
                    for item in workspace.artifacts
                    if item.artifact_id == plan.artifact_id
                )
                consumed_artifacts.append(
                    {
                        "name": "log_archive",
                        "resource_type": "LOGPARSE_RUN",
                        "resource_id": artifact.artifact_id,
                        "size": artifact.size,
                        "sha256": artifact.sha256,
                    }
                )
        return {
            "schema_version": 1,
            "job_id": job.job_id,
            "case_id": job.case_id,
            "registration_id": skill.registration_id,
            "skill_name": skill.methods.skill_name,
            "source_wiki_sha256": skill.methods.source_wiki_sha256,
            "user_inputs": user_inputs,
            "consumed_artifacts": consumed_artifacts,
        }

    def _run_methods_preprocessing(
        self,
        *,
        job: Job,
        aggregate: CaseAggregate,
        main_workspace: PreparedWorkspace,
        assets: ResolvedJobAssets,
        cancellation: CancellationSignal,
        shared_log_sinks: ExecutionLogSinks,
    ) -> MethodsPreprocessingExecution:
        if self._logparse_broker_factory is None:
            raise runtime_failure(
                stage=ExecutionStage.ASSET_RESOLUTION,
                code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
                message="The fixed logparse broker asset is unavailable.",
            )
        skill = self._resolved_methods_skill(assets)
        preprocessing_workspace = self._workspace_manager.prepare(
            job,
            aggregate,
            self._resource_store,
            resolved_logparse_plan=main_workspace.manifest.resolved_logparse_plan,
            review_subject=None,
            workspace_phase="logparse-preprocess",
        )
        operation, product_request_bytes = self._preprocessing_request(
            preprocessing_workspace
        )
        request_path, result_path = (
            self._workspace_manager.write_logparse_preprocessing_request(
                preprocessing_workspace,
                request_bytes=product_request_bytes,
                operation=operation,
            )
        )
        prompt = (
            "You are the product-owned Logparse preprocessing pass in "
            "SERVER_PREPROCESS mode.\n"
            "Your first action must be exactly one Skill tool call: "
            "Skill(logparse-diagnose)\n"
            "If that Helper is unavailable, rejected, or fails to load, stop "
            "immediately. Do not invoke the broker directly or use any fallback.\n"
            "Do not load or execute any other Skill, including the selected business "
            "diagnosis Skill. Do not read the request, broker result, or target logs; "
            "do not diagnose; and do not write a diagnosis or review draft.\n"
            "Only after the Helper loads successfully, follow its SERVER_PREPROCESS "
            "contract and run exactly this one job-scoped broker request:\n"
            f"problem-locator-logparse {operation} --request {request_path} "
            f"--result {result_path}\n"
            "The Runtime prewrote the request path. Do not edit or replace it. Wait "
            "for the one request to finish successfully, then exit without reading "
            "the result. Any failure ends this pass; never retry.\n"
        )
        tool_started = record_stage_started(
            ExecutionStage.TOOL_EXECUTE,
            data={"tool": "logparse", "pass": "PREPROCESS"},
        )
        try:
            session = self._logparse_broker_factory.open(
                job,
                preprocessing_workspace.root,
                preprocessing_workspace.manifest,
                cancellation,
            )
        except LogparseBrokerError as exc:
            raise RuntimeExecutionError(exc.failure) from None
        except Exception:
            raise RuntimeExecutionError(_broker_failure()) from None

        primary: ExecutionFailure | None = None
        secrets: tuple[str, ...] = ()
        accepted_request_bytes: bytes | None = None
        broker_audit_bytes: bytes | None = None
        try:
            broker_environment = session.agent_environment()
            secrets = tuple(broker_environment.values())
            self._backend.execute(
                prompt=prompt,
                workspace_root=preprocessing_workspace.root,
                cancellation=cancellation,
                log_sinks=_borrow_log_sinks(shared_log_sinks),
                resource_limits=job.resource_limits,
                broker_environment=broker_environment,
                test_limits=self._backend_test_limits,
            )
        except RuntimeExecutionError as exc:
            primary = exc.failure
        except Exception:
            primary = _broker_failure()
        finally:
            primary, accepted_request_bytes, broker_audit_bytes = (
                self._close_and_audit_broker(session, primary)
            )
        claim: LogparseParseClaim | None = None
        claim_failure: ExecutionFailure | None = None
        try:
            claim = self._workspace_manager.read_claim(preprocessing_workspace)
            validate_logparse_claim_for_job(
                claim,
                job,
                preprocessing_workspace.manifest,
                accepted_request_bytes,
                None,
            )
        except RuntimeExecutionError as exc:
            claim_failure = exc.failure
        except Exception:
            claim_failure = ExecutionFailure(
                stage=ExecutionStage.TOOL_EXECUTE,
                code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
                message="Logparse execution evidence is invalid.",
                retryable=False,
                details=[],
            )
        if claim_failure is not None:
            primary = claim_failure if primary is None else _append_diagnostic(
                primary,
                field="logparse_claim",
                actual="audit_failed",
            )
        if primary is not None:
            raise RuntimeExecutionError(primary)
        assert broker_audit_bytes is not None
        try:
            validated = read_methods_preprocessing(
                preprocessing_workspace,
                job,
                preprocessing_workspace.manifest,
                broker_audit_bytes=broker_audit_bytes,
                request_bytes=(
                    accepted_request_bytes
                    if accepted_request_bytes is not None
                    else product_request_bytes
                ),
                secrets=secrets,
            )
            request = self._methods_request_value(job, main_workspace, skill)
            record = parse_canonical_json_bytes(broker_audit_bytes)
            operations = record["operations"]
            if (
                len(operations) != 1
                or not isinstance(operations[0], dict)
                or operations[0].get("http_status") != 200
            ):
                raise ValueError(
                    "Methods preprocessing audit must contain one successful operation"
                )
            successful = operations[0]
            receipt_context = {
                "job_id": job.job_id,
                "case_id": job.case_id,
                "registration_id": skill.registration_id,
                "operation": successful["operation"],
                "broker_request_sha256": bytes_sha256(validated.request_bytes),
                "broker_audit_sha256": bytes_sha256(broker_audit_bytes),
            }
            frozen = self._workspace_manager.freeze_methods_inputs(
                main_workspace,
                request=request,
                target_logs=[
                    (item.target.label, item.target.label, item.content)
                    for item in validated.target_logs
                ],
                receipt_context=receipt_context,
            )
        except RuntimeExecutionError:
            raise
        except (OSError, TypeError, ValueError):
            raise runtime_failure(
                stage=ExecutionStage.OUTCOME_VALIDATE,
                code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
                message="Frozen Logparse preprocessing evidence is invalid.",
            ) from None
        for resource in validated.proposal_resources:
            resource.verify_unchanged()
        self._publish_audit_bytes(job, "logparse_broker_audit.json", broker_audit_bytes)
        self._publish_audit_bytes(job, "methods_request.json", frozen.request_bytes)
        self._publish_audit_bytes(
            job,
            "methods_target_logs.json",
            frozen.target_logs_bytes,
        )
        self._publish_audit_bytes(
            job,
            "methods_logparse_receipt.json",
            frozen.receipt_bytes,
        )
        record_stage_completed(
            ExecutionStage.TOOL_EXECUTE,
            tool_started,
            data={
                "tool": "logparse",
                "pass": "PREPROCESS",
                "operation": operation,
                "target_log_count": len(frozen.target_logs),
                "receipt_sha256": frozen.receipt_sha256,
            },
        )
        return MethodsPreprocessingExecution(
            validated=validated,
            frozen=frozen,
            claim=claim,
            secrets=secrets,
        )

    def _execute_backend(
        self,
        job: Job,
        workspace: PreparedWorkspace,
        cancellation: CancellationSignal,
        prompt: str,
    ) -> tuple[
        tuple[str, ...],
        bytes | None,
        LogparseParseClaim | None,
        bytes | None,
    ]:
        if workspace.manifest.resolved_logparse_plan is None:
            self._backend.execute(
                prompt=prompt,
                workspace_root=workspace.root,
                cancellation=cancellation,
                log_sinks=self._open_log_sinks(job),
                resource_limits=job.resource_limits,
                test_limits=self._backend_test_limits,
            )
            return (), None, None, None

        if self._logparse_broker_factory is None:
            raise runtime_failure(
                stage=ExecutionStage.ASSET_RESOLUTION,
                code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
                message="The fixed logparse broker asset is unavailable.",
            )
        tool_started = record_stage_started(
            ExecutionStage.TOOL_EXECUTE,
            data={"tool": "logparse"},
        )
        try:
            session = self._logparse_broker_factory.open(
                job,
                workspace.root,
                workspace.manifest,
                cancellation,
            )
        except LogparseBrokerError as exc:
            raise RuntimeExecutionError(exc.failure) from None
        except Exception:
            raise RuntimeExecutionError(_broker_failure()) from None

        primary: ExecutionFailure | None = None
        secrets: tuple[str, ...] = ()
        request_bytes: bytes | None = None
        audit_bytes: bytes | None = None
        try:
            broker_environment = session.agent_environment()
            secrets = tuple(broker_environment.values())
            self._backend.execute(
                prompt=prompt,
                workspace_root=workspace.root,
                cancellation=cancellation,
                log_sinks=self._open_log_sinks(job),
                resource_limits=job.resource_limits,
                broker_environment=broker_environment,
                test_limits=self._backend_test_limits,
            )
        except RuntimeExecutionError as exc:
            primary = exc.failure
        except Exception:
            primary = _broker_failure()
        finally:
            primary, request_bytes, audit_bytes = self._close_and_audit_broker(
                session,
                primary,
            )
        claim = None
        claim_failure: ExecutionFailure | None = None
        try:
            claim = self._workspace_manager.read_claim(workspace)
            validate_logparse_claim_for_job(
                claim,
                job,
                workspace.manifest,
                request_bytes,
                None,
            )
        except RuntimeExecutionError as exc:
            claim_failure = exc.failure
        except Exception:
            claim_failure = ExecutionFailure(
                stage=ExecutionStage.TOOL_EXECUTE,
                code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
                message="Logparse execution evidence is invalid.",
                retryable=False,
                details=[],
            )
        if claim_failure is not None:
            if primary is None:
                primary = claim_failure
            else:
                primary = _append_diagnostic(
                    primary,
                    field="logparse_claim",
                    actual="audit_failed",
                )
        if primary is not None:
            raise RuntimeExecutionError(primary)
        record_stage_completed(
            ExecutionStage.TOOL_EXECUTE,
            tool_started,
            data={
                "tool": "logparse",
                "claim": claim,
                "request_bytes": None if request_bytes is None else len(request_bytes),
            },
        )
        return secrets, request_bytes, claim, audit_bytes

    @staticmethod
    def _close_and_audit_broker(
        session: LogparseBrokerSession,
        primary: ExecutionFailure | None,
    ) -> tuple[ExecutionFailure | None, bytes | None, bytes | None]:
        close_failed = False
        request_failed = False
        request_bytes: bytes | None = None
        audit_bytes: bytes | None = None
        try:
            session.close()
        except Exception:
            close_failed = True
        try:
            captured = session.parse_request_bytes()
            if captured is not None and type(captured) is not bytes:
                raise TypeError("parse_request_bytes must return exact bytes")
            request_bytes = captured
        except Exception:
            request_failed = True
        try:
            captured_audit = session.audit_bytes()
            if type(captured_audit) is not bytes:
                raise TypeError("audit_bytes must return exact bytes")
            # Parse once at the trust boundary; the immutable ExecutionRecord
            # stores these exact canonical bytes for verification and replay.
            audit_value = parse_canonical_json_bytes(captured_audit)
            if not isinstance(audit_value, dict):
                raise TypeError("broker audit must be one JSON object")
            audit_bytes = captured_audit
        except Exception:
            request_failed = True

        if primary is None and close_failed:
            primary = _broker_failure()
        if primary is None and request_failed:
            primary = ExecutionFailure(
                stage=ExecutionStage.TOOL_EXECUTE,
                code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
                message="Logparse request evidence could not be audited.",
                retryable=False,
                details=[],
            )
        if primary is not None and close_failed:
            primary = _append_diagnostic(
                primary,
                field="broker_session",
                actual="cleanup_failed",
            )
        if primary is not None and request_failed:
            primary = _append_diagnostic(
                primary,
                field="parse_request_bytes",
                actual="audit_failed",
            )
        return primary, request_bytes, audit_bytes


__all__ = ["DiagnosisRuntime"]
