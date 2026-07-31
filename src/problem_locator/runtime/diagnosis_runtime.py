"""S04 Runtime composition for one already-claimed immutable Job."""

from __future__ import annotations

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
    LogparseBrokerError,
    LogparseBrokerFactory,
    LogparseBrokerSession,
    LogparseParseClaim,
    ResourceStore,
    RuntimeExecutionReceipt,
    RuntimeInfrastructureError,
    StagedResourceRef,
    StateRepository,
    validate_logparse_claim_for_job,
)

from .agent_backend import AgentBackend, BackendExecutionLimits
from .context_builder import ContextBuilder, ContextLimitExceeded, ContextMaterials
from .context_policy import ResolvedJobAssets, RuntimeAssetResolver
from .failures import RuntimeExecutionError, runtime_failure
from .outcome_publisher import OutcomePublisher
from .output_reader import read_agent_output
from .proposal_stager import discard_staged, stage_validated_output
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
        self._publisher = OutcomePublisher(execution_records, clock, id_generator)

    def execute(
        self,
        job: Job,
        cancellation: CancellationSignal,
    ) -> RuntimeExecutionReceipt:
        failure: ExecutionFailure | None = None
        try:
            return self._execute(job, cancellation)
        except RuntimeInfrastructureError:
            raise
        except RuntimeExecutionError as exc:
            failure = exc.failure
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            failure = _unexpected_failure().failure
        assert failure is not None
        # Publish outside the handler so the public infrastructure exception
        # cannot retain an unsafe parser, OS, or adapter exception as context.
        return self._publisher.publish_failure(job, failure)

    def _execute(
        self,
        job: Job,
        cancellation: CancellationSignal,
    ) -> RuntimeExecutionReceipt:
        assets = self._resolve_assets(job)
        if job.status is not JobStatus.RUNNING:
            raise runtime_failure(
                stage=ExecutionStage.OUTCOME_VALIDATE,
                code=ErrorCode.OUTCOME_INVALID,
                message="Runtime requires an already-claimed RUNNING Job.",
            )
        aggregate = self._read_case(job)
        workspace = self._workspace_manager.prepare(
            job,
            aggregate,
            self._resource_store,
        )
        resolved = assets.bind_workspace(workspace)
        context = self._build_context(job, resolved.materials)
        self._workspace_manager.write_context(workspace, context.body)

        secrets, parse_request_bytes, claim = self._execute_backend(
            job,
            workspace,
            cancellation,
            context.body,
        )
        try:
            validated = read_agent_output(
                workspace,
                job,
                workspace.manifest,
                secrets=secrets,
            )
        except RuntimeExecutionError as exc:
            if secrets:
                self._workspace_manager.purge_agent_output(workspace)
            raise

        staged = stage_validated_output(
            job=job,
            workspace_manifest=workspace.manifest,
            validated=validated,
            resource_store=self._resource_store,
            claim=claim,
            parse_request_bytes=parse_request_bytes,
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
        if receipt.job_outcome != staged.outcome:
            _discard_unreferenced_staged(
                self._resource_store,
                staged.staged_refs,
                receipt,
            )
        return receipt

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
    ) -> tuple[tuple[str, ...], bytes | None, LogparseParseClaim | None]:
        if job.logparse_tool_ref is None:
            self._backend.execute(
                prompt=prompt,
                workspace_root=workspace.root,
                cancellation=cancellation,
                log_sinks=self._open_log_sinks(job),
                resource_limits=job.resource_limits,
                test_limits=self._backend_test_limits,
            )
            return (), None, None

        if self._logparse_broker_factory is None:
            raise runtime_failure(
                stage=ExecutionStage.ASSET_RESOLUTION,
                code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
                message="The fixed logparse broker asset is unavailable.",
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
            primary, request_bytes = self._close_and_audit_broker(
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
            if secrets:
                self._workspace_manager.purge_agent_output(workspace)
            raise RuntimeExecutionError(primary)
        return secrets, request_bytes, claim

    @staticmethod
    def _close_and_audit_broker(
        session: LogparseBrokerSession,
        primary: ExecutionFailure | None,
    ) -> tuple[ExecutionFailure | None, bytes | None]:
        close_failed = False
        request_failed = False
        request_bytes: bytes | None = None
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
        return primary, request_bytes


__all__ = ["DiagnosisRuntime"]
