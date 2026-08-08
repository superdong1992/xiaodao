"""S04 Runtime composition for one already-claimed immutable Job."""

from __future__ import annotations

from pathlib import Path

from problem_locator.contracts import (
    ApplicationErrorDetail,
    ApplicationPortError,
    AssetCatalogPort,
    CancellationSignal,
    CaseAggregate,
    Clock,
    BoundedContext,
    ErrorCode,
    ExecutionFailure,
    ExecutionLogSinks,
    ExecutionRecordStore,
    ExecutionStage,
    IdGenerator,
    Job,
    JobStatus,
    JobType,
    OutcomeResultType,
    LogparseBrokerError,
    LogparseBrokerFactory,
    LogparseBrokerSession,
    LogparseParseClaim,
    ResourceStore,
    RuntimeExecutionReceipt,
    RuntimeInfrastructureError,
    ResolvedLogparseAnchor,
    ResolvedLogparsePlanInput,
    StagedResourceRef,
    StateRepository,
    canonical_json_bytes,
    parse_canonical_json_bytes,
    validate_logparse_claim_for_job,
)
from problem_locator.diagnostics import log_event
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
from .outcome_publisher import OutcomePublisher
from .output_reader import (
    RejectedAgentOutputError,
    ValidatedAgentOutput,
    read_agent_output,
)
from .proposal_stager import discard_staged, stage_validated_output
from .resolved_logparse import (
    ResolvedLogparsePlanNotReady,
    compile_resolved_logparse_plan,
)
from .review_subject import compile_review_subject
from .server_outcome_finalizer import finalize_server_outcome
from .server_verifier import verify_agent_draft
from .workspace import PreparedWorkspace, WorkspaceManager


def _unexpected_failure() -> RuntimeExecutionError:
    return runtime_failure(
        stage=ExecutionStage.OUTCOME_VALIDATE,
        code=ErrorCode.OUTCOME_INVALID,
        message="Runtime execution could not be validated safely.",
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
        preparing = record_stage_started(ExecutionStage.WORKSPACE_PREPARE)
        aggregate = self._read_case(job)
        try:
            plan_not_ready = False
            try:
                broker_plan = compile_resolved_logparse_plan(job, aggregate, assets)
            except ResolvedLogparsePlanNotReady:
                broker_plan = None
                plan_not_ready = True
            if (
                job.logparse_tool_ref is not None
                and broker_plan is None
                and not plan_not_ready
            ):
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
            review_subject = compile_review_subject(job, aggregate, assets)
        except (OSError, TypeError, ValueError):
            raise runtime_failure(
                stage=ExecutionStage.ASSET_RESOLUTION,
                code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
                message="The pinned Skill could not produce immutable verification bindings.",
            ) from None
        workspace = self._workspace_manager.prepare(
            job,
            aggregate,
            self._resource_store,
            resolved_logparse_plan=resolved_logparse_plan,
            review_subject=review_subject,
        )
        if review_subject is not None:
            self._publish_audit_bytes(
                job,
                "review_subject.json",
                canonical_json_bytes(review_subject),
            )
        resolved = assets.bind_workspace(workspace)
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
        context_building = record_stage_started(ExecutionStage.CONTEXT_BUILD)
        context = self._build_context(job, resolved.materials)
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

        secrets, parse_request_bytes, claim, broker_audit_bytes = self._execute_backend(
            job,
            workspace,
            cancellation,
            context.body,
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
            )
        except RejectedAgentOutputError as exc:
            self._archive_rejected_agent_output(job, exc)
            raise
        self._publish_audit_bytes(
            job,
            "agent_job_outcome.draft.json",
            validated_draft.canonical_bytes,
        )
        diagnosis_audit = self._diagnosis_audit_for_review(job, aggregate)
        verification = None
        if (
            job.job_type is not JobType.ROUTE
            and validated_draft.draft.result_type is not OutcomeResultType.FAILED
        ):
            if assets.skill is None:
                raise _unexpected_failure()
            for resource in validated_draft.proposal_resources:
                resource.verify_unchanged()
            try:
                verification = verify_agent_draft(
                    workspace_root=workspace.root,
                    job=job,
                    manifest=workspace.manifest,
                    draft=validated_draft.draft,
                    draft_bytes=validated_draft.canonical_bytes,
                    proposal_resources=validated_draft.proposal_resources,
                    skill_root=Path(assets.skill.root_path),
                    broker_audit_bytes=broker_audit_bytes,
                    diagnosis_audit=diagnosis_audit,
                )
            except ValueError:
                raise runtime_failure(
                    stage=ExecutionStage.OUTCOME_VALIDATE,
                    code=ErrorCode.OUTCOME_INVALID,
                    message="Agent outcome violates the pinned verification contract.",
                ) from None
            for resource in validated_draft.proposal_resources:
                resource.verify_unchanged()
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
            draft=validated_draft.draft,
            draft_bytes=validated_draft.canonical_bytes,
            outcome_id=self._id_generator.new("job_outcome"),
            produced_at=self._clock.now(),
            verification=verification,
            user_result_bytes=validated_draft.user_result_bytes,
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
        validated = ValidatedAgentOutput(
            outcome=finalized.outcome,
            canonical_bytes=finalized.canonical_bytes,
            proposal_resources=validated_draft.proposal_resources,
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
