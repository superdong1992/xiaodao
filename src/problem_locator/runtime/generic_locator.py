"""Isolated black-box execution for one GENERIC DIAGNOSE Job."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass

from problem_locator.contracts import (
    ApplicationPortError,
    CancellationSignal,
    CaseAggregate,
    Clock,
    DiagnosisMode,
    ErrorCode,
    ExecutionLogSinks,
    ExecutionRecordStore,
    ExecutionStage,
    GenericDiagnosisOutcome,
    GenericResultStatus,
    IdGenerator,
    Job,
    JobOutcome,
    JobStatus,
    OutcomeResultType,
    ResourceStore,
)
from problem_locator.journey import (
    record_stage_completed,
    record_stage_started,
)

from .agent_backend import AgentBackend, BackendExecutionLimits
from .context_policy import ResolvedJobAssets
from .failures import RuntimeExecutionError, runtime_failure
from .workspace import PreparedWorkspace, WorkspaceManager


GENERIC_RESULT_FILENAME = "generic_diagnosis_result.txt"
MAX_GENERIC_RESULT_BYTES = 65_536
GENERIC_RESULT_PATTERN = re.compile(
    r"\A<<<GENERIC_DIAGNOSIS_RESULT_V1>>>\r?\n"
    r"STATUS: (?P<status>RESOLVED|UNRESOLVED)\r?\n"
    r"CONCLUSION:\r?\n"
    r"(?P<conclusion>\S(?:[\s\S]*?\S)?)\r?\n"
    r"ROOT_CAUSE_ANALYSIS:\r?\n"
    r"(?P<root_cause_analysis>\S(?:[\s\S]*?\S)?)\r?\n"
    r"<<<END_GENERIC_DIAGNOSIS_RESULT_V1>>>\r?\n?\Z"
)


@dataclass(frozen=True, slots=True)
class GenericLocatorExecution:
    outcome: JobOutcome
    workspace: PreparedWorkspace
    prompt: str


def _invalid_result() -> RuntimeExecutionError:
    return runtime_failure(
        stage=ExecutionStage.OUTCOME_VALIDATE,
        code=ErrorCode.OUTCOME_INVALID,
        message="The generic diagnosis result file is missing or invalid.",
        retryable=False,
    )


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _read_result_bytes(workspace: PreparedWorkspace) -> bytes:
    path = workspace.root / "output" / GENERIC_RESULT_FILENAME
    descriptor = -1
    try:
        output_before = (workspace.root / "output").stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(output_before.st_mode)
            or (output_before.st_dev, output_before.st_ino)
            != (workspace.output_device, workspace.output_inode)
        ):
            raise ValueError("output directory identity changed")
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > MAX_GENERIC_RESULT_BYTES
        ):
            raise ValueError("generic result node is unsafe")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not _same_identity(before, opened)
            or opened.st_size > MAX_GENERIC_RESULT_BYTES
        ):
            raise ValueError("generic result changed before read")
        chunks: list[bytes] = []
        remaining = MAX_GENERIC_RESULT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_GENERIC_RESULT_BYTES:
            raise ValueError("generic result exceeds byte limit")
        after_descriptor = os.fstat(descriptor)
        after_name = path.lstat()
        output_after = (workspace.root / "output").stat(follow_symlinks=False)
        if (
            not _same_identity(opened, after_descriptor)
            or not _same_identity(opened, after_name)
            or after_descriptor.st_size != len(payload)
            or after_name.st_size != len(payload)
            or (output_after.st_dev, output_after.st_ino)
            != (workspace.output_device, workspace.output_inode)
        ):
            raise ValueError("generic result changed during read")
        return payload
    except (OSError, ValueError):
        raise _invalid_result() from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def parse_generic_result(
    workspace: PreparedWorkspace,
    *,
    skill_name: str,
) -> GenericDiagnosisOutcome:
    raw = _read_result_bytes(workspace)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _invalid_result() from None
    if "```" in text:
        raise _invalid_result() from None
    match = GENERIC_RESULT_PATTERN.fullmatch(text)
    if match is None:
        raise _invalid_result() from None
    conclusion = match.group("conclusion")
    root_cause_analysis = match.group("root_cause_analysis")
    reserved = (
        "<<<GENERIC_DIAGNOSIS_RESULT_V1>>>",
        "<<<END_GENERIC_DIAGNOSIS_RESULT_V1>>>",
    )
    if any(marker in conclusion or marker in root_cause_analysis for marker in reserved):
        raise _invalid_result() from None
    return GenericDiagnosisOutcome(
        status=GenericResultStatus(match.group("status")),
        conclusion=conclusion,
        root_cause_analysis=root_cause_analysis,
        # The name is bound by the immutable Job, never trusted from Agent text.
        skill_name=skill_name,
    )


class GenericLocatorExecutor:
    """Run a preinstalled generic Skill as a reusable isolated workflow node."""

    def __init__(
        self,
        *,
        backend: AgentBackend,
        workspace_manager: WorkspaceManager,
        execution_records: ExecutionRecordStore,
        clock: Clock,
        id_generator: IdGenerator,
        backend_test_limits: BackendExecutionLimits | None = None,
    ) -> None:
        self._backend = backend
        self._workspace_manager = workspace_manager
        self._execution_records = execution_records
        self._clock = clock
        self._id_generator = id_generator
        self._backend_test_limits = backend_test_limits

    @staticmethod
    def build_prompt(job: Job, assets: ResolvedJobAssets) -> str:
        if (
            job.diagnosis_mode is not DiagnosisMode.GENERIC
            or job.generic_skill_name is None
            or job.generic_problem_text is None
        ):
            raise ValueError("GenericLocatorExecutor requires a GENERIC DIAGNOSE Job")
        raw_bytes = job.generic_problem_text.encode("utf-8")
        return (
            f"{assets.profile_text.rstrip()}\n\n"
            f"Invoke the preinstalled Skill `${job.generic_skill_name}` now. "
            "Pass the raw problem text below unchanged as its sole problem payload. "
            "The byte-count and framing lines are transport metadata and are not part "
            "of that payload. The Skill owns its complete workflow and may call any "
            "tools available in the Agent environment.\n\n"
            f"<<<RAW_PROBLEM_TEXT_UTF8_BYTES:{len(raw_bytes)}>>>\n"
            f"{job.generic_problem_text}\n"
            "<<<END_RAW_PROBLEM_TEXT>>>\n\n"
            f"{assets.output_contract_text.rstrip()}\n"
        )

    def execute(
        self,
        *,
        job: Job,
        aggregate: CaseAggregate,
        assets: ResolvedJobAssets,
        resource_store: ResourceStore,
        cancellation: CancellationSignal,
    ) -> GenericLocatorExecution:
        if job.status is not JobStatus.RUNNING:
            raise _invalid_result()
        preparing = record_stage_started(ExecutionStage.WORKSPACE_PREPARE)
        workspace = self._workspace_manager.prepare(job, aggregate, resource_store)
        record_stage_completed(
            ExecutionStage.WORKSPACE_PREPARE,
            preparing,
            data={
                "workspace_root": workspace.root,
                "manifest_bytes": len(workspace.manifest_bytes),
                "generic": True,
            },
        )
        building = record_stage_started(ExecutionStage.CONTEXT_BUILD)
        try:
            prompt = self.build_prompt(job, assets)
        except (TypeError, UnicodeEncodeError, ValueError):
            raise _invalid_result() from None
        if len(prompt.encode("utf-8")) > job.resource_limits.context_bytes:
            raise runtime_failure(
                stage=ExecutionStage.CONTEXT_BUILD,
                code=ErrorCode.CONTEXT_LIMIT,
                message="The generic locator prompt exceeds the fixed context budget.",
                retryable=False,
            )
        self._workspace_manager.write_context(workspace, prompt)
        self._publish_audit(job, "context.txt", prompt.encode("utf-8"))
        record_stage_completed(
            ExecutionStage.CONTEXT_BUILD,
            building,
            data={"utf8_bytes": len(prompt.encode("utf-8")), "generic": True},
        )
        self._backend.execute(
            prompt=prompt,
            workspace_root=workspace.root,
            cancellation=cancellation,
            log_sinks=self._open_log_sinks(job),
            resource_limits=job.resource_limits,
            test_limits=self._backend_test_limits,
            diagnosis_mode="GENERIC",
        )
        validating = record_stage_started(ExecutionStage.OUTCOME_VALIDATE)
        assert job.generic_skill_name is not None
        payload = parse_generic_result(
            workspace,
            skill_name=job.generic_skill_name,
        )
        outcome = JobOutcome(
            outcome_id=self._id_generator.new("job_outcome"),
            job_id=job.job_id,
            case_id=job.case_id,
            job_type=job.job_type,
            base_state_revision=job.base_state_revision,
            result_type=OutcomeResultType.COMPLETED,
            payload=payload,
            consumed_evidence_refs=[],
            proposed_evidence=[],
            proposed_artifacts=[],
            error=None,
            produced_at=self._clock.now(),
            decision_audit=None,
        )
        record_stage_completed(
            ExecutionStage.OUTCOME_VALIDATE,
            validating,
            data={"generic_status": payload.status},
        )
        return GenericLocatorExecution(
            outcome=outcome,
            workspace=workspace,
            prompt=prompt,
        )

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
                retryable=False,
                details=exc.error.details,
            ) from None
        except Exception:
            raise runtime_failure(
                stage=ExecutionStage.EXECUTION_RECORD,
                code=ErrorCode.EXECUTION_RECORD_FAILED,
                message="Execution logs could not be opened.",
                retryable=False,
            ) from None
        if (
            not isinstance(sinks, ExecutionLogSinks)
            or sinks.combined_limit_bytes != job.resource_limits.stdout_stderr_bytes
        ):
            raise runtime_failure(
                stage=ExecutionStage.EXECUTION_RECORD,
                code=ErrorCode.EXECUTION_RECORD_FAILED,
                message="Execution log limits do not match the frozen Job.",
                retryable=False,
            )
        return sinks

    def _publish_audit(self, job: Job, filename: str, payload: bytes) -> None:
        try:
            self._execution_records.publish_audit_bytes(job.job_id, filename, payload)
        except ApplicationPortError as exc:
            raise runtime_failure(
                stage=ExecutionStage.EXECUTION_RECORD,
                code=ErrorCode.EXECUTION_RECORD_FAILED,
                message="Generic execution audit material could not be published.",
                retryable=False,
                details=exc.error.details,
            ) from None
        except Exception:
            raise runtime_failure(
                stage=ExecutionStage.EXECUTION_RECORD,
                code=ErrorCode.EXECUTION_RECORD_FAILED,
                message="Generic execution audit material could not be published.",
                retryable=False,
            ) from None


__all__ = [
    "GENERIC_RESULT_FILENAME",
    "GENERIC_RESULT_PATTERN",
    "GenericLocatorExecution",
    "GenericLocatorExecutor",
    "MAX_GENERIC_RESULT_BYTES",
    "parse_generic_result",
]
