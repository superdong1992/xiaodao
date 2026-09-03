from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from problem_locator.application import build_application_service
from problem_locator.contracts import (
    BoundedContext,
    CaseAggregate,
    ContextSection,
    ContextSectionKind,
    ErrorCode,
    ExecutionLogSinks,
    ExecutionStage,
    Job,
    JobStatus,
    OutcomeResultType,
    ResumeCase,
    StateFile,
    parse_canonical_json_bytes,
)
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.runtime.agent_backend import BackendExecution
from problem_locator.runtime.diagnosis_runtime import (
    METHODS_CONSENSUS_ATTRIBUTION_V2_FILENAME,
    DiagnosisRuntime,
)
from problem_locator.runtime.failures import runtime_failure
from problem_locator.runtime.methods_records_v2 import (
    METHODS_EVALUATION_PLAN_V2_FILENAME,
    METHODS_LIMITATIONS_V2_FILENAME,
    METHODS_STATE_V2_FILENAME,
    build_method_limitations_record_v2,
    publish_method_evaluation_plan_v2,
    publish_method_evidence_graph_v2,
    publish_method_limitations_record_v2,
    publish_method_rejected_attempt_v2,
    publish_method_state_v2,
    read_method_evaluation_plan_v2,
    read_method_evidence_graph_v2,
    read_method_limitations_record_v2,
    read_method_prompt_v2,
    read_method_rejected_attempt_v2,
    read_method_state_v2,
    method_prompt_filename_v2,
)
from problem_locator.domain.methods_state_v2 import (
    accept_specialist_evaluation_v2,
    interrupt_method_state_v2,
    record_protocol_error_v2,
    resume_method_state_v2,
    start_method_state_v2,
)
from problem_locator.runtime.methods_evaluation_v2 import evaluate_method_role_v2
from problem_locator.runtime.workspace import WorkspaceManager
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
    FakeClock,
    FakeLogparseBrokerFactory,
    InMemoryAttachmentUploadGuard,
    InMemoryCancellationSignal,
    InMemoryExecutionRecordStore,
    InMemoryPublicationCommitGuard,
    InMemoryStateChangeNotifier,
    InMemoryStateRepository,
    RecordingDispatcher,
)
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    _Clock,
    _MethodsTwoPassBackend,
    _StateView,
    _TooLargeContext,
    _claimed_logparse_job_state_and_resources,
    _logparse_catalog,
)
from tests.deterministic.unit.runtime.test_methods_workspace_context_v2 import (
    PRIVATE_STATE_SENTINEL,
    _ResourceStore,
    _UnusedResourceStore,
    _aggregate,
    _jobs,
)


def _assert_model_minimal_methods_prompt(prompt: str) -> None:
    assert '"user_facts"' not in prompt
    assert '"problem_spec"' not in prompt
    for state_field in (
        "diagnosis_state_revision",
        "confirmed_facts",
        "active_hypotheses",
        "rejected_hypotheses",
        "open_questions",
        "pending_requirements",
        "evidence_refs",
        "candidate_conclusion",
    ):
        assert f'"{state_field}"' not in prompt


def _evaluation_input_from_prompt(prompt: str) -> dict[str, Any]:
    for line in prompt.splitlines():
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("evaluation_input"), dict):
            return value["evaluation_input"]
    raise AssertionError("Methods role prompt has no compact evaluation_input")


class _EvidenceV2SpecialistBackend(_MethodsTwoPassBackend):
    def __init__(
        self,
        factory: FakeLogparseBrokerFactory,
        job: Any,
        responses: tuple[object, ...],
    ) -> None:
        super().__init__(factory, job, "completed")
        self.responses = list(responses)
        self.role_prompts: list[str] = []
        self.role_workspace_files: list[set[str]] = []

    def _run_methods(self, kwargs: dict[str, Any]) -> BackendExecution:
        workspace_root = Path(kwargs["workspace_root"])
        inputs = workspace_root / "inputs"
        names = {
            path.relative_to(workspace_root).as_posix()
            for path in workspace_root.rglob("*")
            if path.is_file()
        }
        self.role_workspace_files.append(names)
        assert {path.name for path in inputs.iterdir()} == {
            "manifest.json",
            "request.json",
        }
        assert {
            path.name for path in (workspace_root / "runtime").iterdir()
        } == {"tool-state"}
        assert "inputs/request.json" in names
        assert "inputs/method-evidence-graph.json" not in names
        assert "inputs/method-evaluation-plan.json" not in names
        assert "runtime/context.txt" not in names
        assert "inputs/target_logs.json" not in names
        assert "inputs/logparse-receipt.json" not in names
        assert not any(name.startswith("inputs/target-logs/") for name in names)
        assert not any(name.startswith("inputs/attachments/") for name in names)
        assert not any(name.startswith("inputs/evidence/") for name in names)
        assert not any(name.startswith("inputs/artifacts/") for name in names)
        assert not any(name.startswith("inputs/outcomes/") for name in names)

        request = parse_canonical_json_bytes((inputs / "request.json").read_bytes())
        assert request["job"]["job_id"] == self.job.job_id
        assert request["user_facts"]
        assert request["user_facts"][0]["value"] not in kwargs["prompt"]
        prompt = kwargs["prompt"]
        self.role_prompts.append(prompt)
        _assert_model_minimal_methods_prompt(prompt)
        assert "Evidence Graph" in prompt
        assert "evaluation_input" in prompt
        assert "evaluation_ref" in prompt
        assert "supporting_event_refs" in prompt
        assert '"events"' in prompt
        assert "hit refs" in prompt

        response = self.responses.pop(0)
        if response == "RAISE_TYPE_ERROR":
            raise TypeError("injected backend contract bug")
        if response == "MODEL_FAILURE":
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.BACKEND_EXIT_FAILED,
                message="injected model execution failure",
            )
        if response == "CANCELLED":
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.BACKEND_CANCELLED,
                message="injected cancellation",
                retryable=True,
            )
        if isinstance(response, str) and (
            response in {"VALID", "VALID_SECRET"}
            or response in {"VALID_UNKNOWN", "VALID_REJECTED"}
        ):
            evaluation_input = _evaluation_input_from_prompt(prompt)
            verdict = {
                "VALID_UNKNOWN": "UNKNOWN",
                "VALID_REJECTED": "REJECTED",
            }.get(response, "CONFIRMED")
            response = [
                {
                    "evaluation_ref": item["evaluation_ref"],
                    "verdict": verdict,
                    "supporting_event_refs": (
                        [event["event_ref"] for event in item["events"]]
                        if verdict == "CONFIRMED"
                        else []
                    ),
                    "reason": (
                        "contract-test-token-1"
                        if response == "VALID_SECRET"
                        else "The frozen evidence satisfies this method card."
                    ),
                }
                for item in evaluation_input["evaluations"]
            ]
        (workspace_root / "output/method-diagnosis.draft.json").write_bytes(
            json.dumps(response, ensure_ascii=False).encode("utf-8")
        )


class _ContextBodyAtRoleLimit:
    def build(self, job: Job, materials: Any) -> BoundedContext:
        del materials
        body = "x" * job.resource_limits.context_bytes
        encoded = body.encode("utf-8")
        return BoundedContext(
            job_id=job.job_id,
            job_type=job.job_type,
            body=body,
            sections=[
                ContextSection(
                    ordinal=0,
                    kind=ContextSectionKind.JOB_INSTRUCTION,
                    source_refs=[job.job_id],
                    required=True,
                    utf8_bytes=len(encoded),
                    content_sha256=hashlib.sha256(encoded).hexdigest(),
                ),
                ContextSection(
                    ordinal=1,
                    kind=ContextSectionKind.RESOURCE_MANIFEST,
                    source_refs=[job.job_id],
                    required=True,
                    utf8_bytes=0,
                    content_sha256=hashlib.sha256(b"").hexdigest(),
                ),
            ],
            utf8_bytes=len(encoded),
            limit_bytes=job.resource_limits.context_bytes,
            body_sha256=hashlib.sha256(encoded).hexdigest(),
        )


class _ObservedRecords(InMemoryExecutionRecordStore):
    def __init__(self, events: list[str], source_job_id: str) -> None:
        super().__init__()
        self.events = events
        self.source_job_id = source_job_id

    def read_audit_bytes(self, job_id: str, filename: str) -> bytes | None:
        if filename == METHODS_STATE_V2_FILENAME and job_id == self.source_job_id:
            self.events.append("source-state-read")
        return super().read_audit_bytes(job_id, filename)


class _CrashBeforeRepairRecords(_ObservedRecords):
    def __init__(
        self,
        events: list[str],
        source_job_id: str,
        crash_filename: str,
    ) -> None:
        super().__init__(events, source_job_id)
        self.crash_filename = crash_filename
        self.crash_once = True

    def publish_audit_bytes(self, job_id: str, filename: str, raw_bytes: bytes):
        if self.crash_once and filename == self.crash_filename:
            self.crash_once = False
            raise KeyboardInterrupt("injected restart boundary")
        return super().publish_audit_bytes(job_id, filename, raw_bytes)


class _AfterWriteAuditRecords(InMemoryExecutionRecordStore):
    def __init__(self, filename: str) -> None:
        super().__init__()
        self.filename = filename
        self.failed_once = False

    def publish_audit_bytes(self, job_id: str, filename: str, raw_bytes: bytes):
        receipt = super().publish_audit_bytes(job_id, filename, raw_bytes)
        if not self.failed_once and filename == self.filename:
            self.failed_once = True
            raise OSError("injected after-write failure")
        return receipt


class _RejectContextAuditRecords(InMemoryExecutionRecordStore):
    def __init__(self) -> None:
        super().__init__()
        self.context_publish_attempts = 0

    def publish_audit_bytes(self, job_id: str, filename: str, raw_bytes: bytes):
        if filename == "context.txt":
            self.context_publish_attempts += 1
            raise OSError("injected context audit archive failure")
        return super().publish_audit_bytes(job_id, filename, raw_bytes)


class _CrashBeforeOutcomeRecords(_ObservedRecords):
    def __init__(self, events: list[str], source_job_id: str) -> None:
        super().__init__(events, source_job_id)
        self.crash_once = True

    def publish_outcome_bytes(self, job_id: str, canonical_bytes: bytes):
        if self.crash_once:
            self.crash_once = False
            raise KeyboardInterrupt("injected outcome boundary")
        return super().publish_outcome_bytes(job_id, canonical_bytes)


class _RejectLimitationsArchiveRecords(_ObservedRecords):
    def __init__(self, events: list[str], source_job_id: str) -> None:
        super().__init__(events, source_job_id)
        self.block_limitations = False

    def publish_audit_bytes(self, job_id: str, filename: str, raw_bytes: bytes):
        if self.block_limitations and filename == METHODS_LIMITATIONS_V2_FILENAME:
            raise OSError("injected limitations archive failure")
        return super().publish_audit_bytes(job_id, filename, raw_bytes)

    def read_audit_bytes(self, job_id: str, filename: str) -> bytes | None:
        if self.block_limitations and filename == METHODS_LIMITATIONS_V2_FILENAME:
            return None
        return super().read_audit_bytes(job_id, filename)


class _RejectConsensusAttributionRecords(_ObservedRecords):
    def publish_audit_bytes(self, job_id: str, filename: str, raw_bytes: bytes):
        if filename == METHODS_CONSENSUS_ATTRIBUTION_V2_FILENAME:
            raise OSError("injected consensus attribution archive failure")
        return super().publish_audit_bytes(job_id, filename, raw_bytes)


class _EvidenceV2ReviewerBackend:
    def __init__(self, responses: tuple[object, ...], events: list[str]) -> None:
        self.responses = list(responses)
        self.events = events
        self.calls: list[dict[str, Any]] = []
        self.prompts: list[str] = []

    def execute(self, **kwargs: Any) -> BackendExecution:
        self.calls.append(kwargs)
        self.events.append("backend")
        prompt = kwargs["prompt"]
        self.prompts.append(prompt)
        _assert_model_minimal_methods_prompt(prompt)
        assert PRIVATE_STATE_SENTINEL not in prompt
        assert "private specialist reason" not in prompt
        assert "specialist_evaluation" not in prompt
        workspace_root = Path(kwargs["workspace_root"])
        assert {
            path.name for path in (workspace_root / "inputs").iterdir()
        } == {"manifest.json", "request.json"}
        assert {
            path.name for path in (workspace_root / "runtime").iterdir()
        } == {"tool-state"}
        assert not (workspace_root / "runtime/context.txt").exists()
        evaluation_input = _evaluation_input_from_prompt(prompt)
        response = self.responses.pop(0)
        if response == "MODEL_FAILURE":
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.BACKEND_EXIT_FAILED,
                message="injected reviewer model failure",
            )
        if isinstance(response, str) and response.startswith("VALID_"):
            verdict = response.removeprefix("VALID_")
            event_selection = "ALL"
            if verdict == "CONFIRMED_LAST_EVENT":
                verdict = "CONFIRMED"
                event_selection = "LAST"
            response = [
                {
                    "evaluation_ref": item["evaluation_ref"],
                    "verdict": verdict,
                    "supporting_event_refs": (
                        (
                            [item["events"][-1]["event_ref"]]
                            if event_selection == "LAST"
                            else [event["event_ref"] for event in item["events"]]
                        )
                        if verdict == "CONFIRMED"
                        else []
                    ),
                    "reason": "Independent blind review of the frozen plan.",
                }
                for item in evaluation_input["evaluations"]
            ]
        (workspace_root / "output/method-review.draft.json").write_bytes(
            json.dumps(response, ensure_ascii=False).encode("utf-8")
        )
        sinks: ExecutionLogSinks = kwargs["log_sinks"]
        for sink in {id(sinks.stdout): sinks.stdout, id(sinks.stderr): sinks.stderr}.values():
            sink.flush()
            sink.close()
        return BackendExecution(
            returncode=0,
            stdout_stderr_bytes=0,
            workspace_bytes=0,
            elapsed_seconds=0.01,
        )
        self._close_sinks(kwargs)
        return BackendExecution(
            returncode=0,
            stdout_stderr_bytes=0,
            workspace_bytes=0,
            elapsed_seconds=0.01,
        )


def _runtime(
    tmp_path: Path,
    responses: tuple[object, ...],
    *,
    records: InMemoryExecutionRecordStore | None = None,
    workspace_name: str = "runtime-data",
    reviewer_enabled: bool = True,
) -> tuple[
    DiagnosisRuntime,
    Any,
    _EvidenceV2SpecialistBackend,
    InMemoryExecutionRecordStore,
]:
    factory = FakeLogparseBrokerFactory()
    catalog = _logparse_catalog(tmp_path, factory)
    job, aggregate, resources = _claimed_logparse_job_state_and_resources(catalog)
    records = records or InMemoryExecutionRecordStore()
    backend = _EvidenceV2SpecialistBackend(factory, job, responses)

    def execute_preprocessing(
        session: object,
        operation: str,
        request_path: str,
        result_path: str,
    ) -> None:
        assert operation == "parse-targets"
        assert request_path == "output/proposals/methods-preprocess/request.json"
        assert result_path == "output/proposals/methods-preprocess/target_logs.json"
        backend._run_preprocessing(  # noqa: SLF001 - production-port test fixture
            {"workspace_root": getattr(session, "workspace_root")}
        )
        return None

    factory.preprocessing_executor = execute_preprocessing
    runtime = DiagnosisRuntime(
        state_repository=_StateView(aggregate),
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=factory,
        execution_records=records,
        clock=_Clock(),
        id_generator=DeterministicIdGenerator(seed="methods-v2-runtime"),
        workspace_manager=WorkspaceManager(tmp_path / workspace_name),
        backend=backend,
        evidence_v2_reviewer_enabled=reviewer_enabled,
    )
    return runtime, job, backend, records


def _running(job: Job) -> Job:
    value = job.model_dump(mode="json")
    value.update(
        {
            "status": "RUNNING",
            "started_at": "2026-07-31T00:02:10.000Z",
            "finished_at": None,
            "runtime_epoch": "00000000-0000-0000-0000-000000000098",
        }
    )
    return Job.model_validate(value)


def _interrupted_aggregate(
    job: Job,
    *,
    source_job: Job | None = None,
) -> CaseAggregate:
    aggregate = _aggregate(job, source_job=source_job)
    value = aggregate.model_dump(mode="json")
    value["case"].update(
        {
            "active_job_id": None,
            "status": "INTERRUPTED",
            "case_revision": aggregate.case.case_revision + 1,
            "updated_at": "2026-07-31T00:03:00.000Z",
        }
    )
    value["jobs"][job.job_id].update(
        {
            "status": "INTERRUPTED",
            "started_at": "2026-07-31T00:02:10.000Z",
            "finished_at": "2026-07-31T00:03:00.000Z",
            "runtime_epoch": "00000000-0000-0000-0000-000000000098",
        }
    )
    return CaseAggregate.model_validate(value)


def _interrupt_active_job(
    aggregate: CaseAggregate,
    job: Job,
) -> CaseAggregate:
    value = aggregate.model_dump(mode="json")
    value["case"].update(
        {
            "active_job_id": None,
            "status": "INTERRUPTED",
            "case_revision": aggregate.case.case_revision + 1,
            "updated_at": "2026-07-31T00:06:00.000Z",
        }
    )
    value["jobs"][job.job_id].update(
        {
            "status": "INTERRUPTED",
            "finished_at": "2026-07-31T00:06:00.000Z",
        }
    )
    return CaseAggregate.model_validate(value)


def _resume_and_claim_replacement(
    *,
    aggregate: CaseAggregate,
    resources: object,
    catalog: object,
    records: InMemoryExecutionRecordStore,
    replacement_job_id: str,
) -> tuple[InMemoryStateRepository, Job]:
    fixture = Path(__file__).resolve().parents[3] / "fixtures/contracts/positive/state.json"
    base = parse_canonical_json_bytes(fixture.read_bytes(), StateFile)
    repository = InMemoryStateRepository(
        base.model_copy(
            update={
                "generation": base.generation + 1,
                "cases": {aggregate.case.case_id: aggregate},
            }
        )
    )
    guard = InMemoryPublicationCommitGuard()
    service = build_application_service(
        repository=repository,
        resource_store=resources,  # type: ignore[arg-type]
        publication_guard=guard,
        upload_guard=InMemoryAttachmentUploadGuard(),
        execution_records=records,
        coordinator=DomainCoordinator(),
        projector=PureContextSnapshotProjector(),
        asset_catalog=catalog,  # type: ignore[arg-type]
        dispatcher=RecordingDispatcher(),
        notifier=InMemoryStateChangeNotifier(),
        clock=FakeClock("2026-07-31T00:04:00.000Z"),
        ids=DeterministicIdGenerator(
            scripted_ids={
                "trigger": ["00000000-0000-0000-0000-000000000096"],
                "job": [replacement_job_id],
            }
        ),
    )
    response = service.execute(
        ResumeCase(
            idempotency_key=f"resume-{replacement_job_id}",
            case_id=aggregate.case.case_id,
            expected_case_revision=aggregate.case.case_revision,
            wait_seconds=0,
        )
    )
    assert response.business_receipt.job_id == replacement_job_id
    claim = service.claim_job(
        replacement_job_id,
        "00000000-0000-0000-0000-000000000097",
    )
    assert claim.claimed is True and claim.job is not None
    assert claim.job.status is JobStatus.RUNNING
    replaced_ids = {
        item.replacement_for_job_id
        for item in aggregate.jobs.values()
        if item.replacement_for_job_id is not None
    }
    interrupted_ids = [
        item.job_id
        for item in aggregate.jobs.values()
        if item.status is JobStatus.INTERRUPTED and item.job_id not in replaced_ids
    ]
    assert interrupted_ids == [claim.job.replacement_for_job_id]
    return repository, claim.job


def _review_runtime(
    tmp_path: Path,
    responses: tuple[object, ...],
    *,
    records: _ObservedRecords | None = None,
    workspace_name: str = "review-runtime-data",
    include_limitations: bool = True,
    specialist_verdict: str = "CONFIRMED",
    specialist_event_selection: str = "ALL",
    methods: tuple[tuple[str, str], ...] = (("slow-execution", "API_COMPLETE"),),
    target_bytes: bytes | None = None,
) -> tuple[
    DiagnosisRuntime,
    Job,
    _EvidenceV2ReviewerBackend,
    _ObservedRecords,
    list[str],
]:
    catalog, _, specialist, reviewer, _, graph, plan, _ = _jobs(
        tmp_path,
        methods=methods,
        target_bytes=target_bytes,
    )
    reviewer = _running(reviewer)
    aggregate = _aggregate(reviewer, source_job=specialist)
    events: list[str] = [] if records is None else records.events
    records = records or _ObservedRecords(events, specialist.job_id)
    specialist_evaluation = evaluate_method_role_v2(
        role="SPECIALIST",
        plan=plan,
        response=[
            {
                "evaluation_ref": item.evaluation_ref,
                "verdict": specialist_verdict,
                "supporting_event_refs": (
                    (
                        [item.evidence_event_refs[0]]
                        if specialist_event_selection == "FIRST"
                        else list(item.evidence_event_refs)
                    )
                    if specialist_verdict == "CONFIRMED"
                    else []
                ),
                "reason": "private specialist reason",
            }
            for item in plan.evaluations
        ],
        attempt="PRIMARY",
    )
    target = reviewer.methods_review_target
    assert target is not None
    pending_state = accept_specialist_evaluation_v2(
        state=start_method_state_v2(
            case_id=reviewer.case_id,
            source_job_id=specialist.job_id,
            evaluation_id=target.evaluation_id,
            plan=plan,
        ),
        evaluation=specialist_evaluation,
    )
    publish_method_evidence_graph_v2(
        records,
        job_id=specialist.job_id,
        graph=graph,
    )
    publish_method_evaluation_plan_v2(
        records,
        job_id=specialist.job_id,
        plan=plan,
    )
    if include_limitations:
        publish_method_limitations_record_v2(
            records,
            job_id=specialist.job_id,
            record=build_method_limitations_record_v2(
                case_id=reviewer.case_id,
                source_job_id=specialist.job_id,
                graph=graph,
                plan=plan,
                limitations=("Only the frozen target set was evaluated.",),
            ),
        )
    publish_method_state_v2(
        records,
        job_id=specialist.job_id,
        state=pending_state,
    )
    events.clear()
    backend = _EvidenceV2ReviewerBackend(responses, events)
    runtime = DiagnosisRuntime(
        state_repository=_StateView(aggregate),
        resource_store=_UnusedResourceStore(),  # type: ignore[arg-type]
        asset_catalog=catalog,
        logparse_broker_factory=None,
        execution_records=records,
        clock=_Clock(),
        id_generator=DeterministicIdGenerator(seed="methods-v2-review-runtime"),
        workspace_manager=WorkspaceManager(tmp_path / workspace_name),
        backend=backend,
    )
    return runtime, reviewer, backend, records, events


def test_specialist_scans_once_hard_cuts_logs_and_publishes_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, job, backend, records = _runtime(tmp_path, ("VALID",))
    from problem_locator.runtime import diagnosis_runtime as runtime_module

    production_scan = runtime_module.scan_method_evidence_v2
    calls = 0

    def legacy_methods_v1(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Evidence V2 must not enter Methods V1 grounding")

    monkeypatch.setattr(runtime_module, "scan_method_markers", legacy_methods_v1)
    monkeypatch.setattr(runtime_module, "verify_method_diagnosis", legacy_methods_v1)

    def counted_scan(**kwargs: Any):
        nonlocal calls
        calls += 1
        return production_scan(**kwargs)

    monkeypatch.setattr(runtime_module, "scan_method_evidence_v2", counted_scan)

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert calls == 1
    assert len(backend.calls) == 1
    assert len(backend.role_prompts) == 1
    assert len(backend.factory.sessions) == 1
    broker_session = backend.factory.sessions[0]
    assert getattr(broker_session, "deterministic_execute_calls") == [
        (
            "parse-targets",
            "output/proposals/methods-preprocess/request.json",
            "output/proposals/methods-preprocess/target_logs.json",
        )
    ]
    assert backend.session_closed_at_call == [True]
    assert backend.factory.sessions[0].closed is True  # type: ignore[attr-defined]
    assert records.read_audit_bytes(job.job_id, "logparse_broker_audit.json")
    assert receipt.job_outcome.result_type is OutcomeResultType.COMPLETED
    assert receipt.job_outcome.methods_review_target is not None
    assert receipt.job_outcome.methods_terminal_projection is None
    graph = read_method_evidence_graph_v2(records, job_id=job.job_id)
    plan = read_method_evaluation_plan_v2(records, job_id=job.job_id)
    state = read_method_state_v2(records, job_id=job.job_id)
    assert graph is not None and plan is not None and state is not None
    assert state.status == "REVIEWER_PENDING"
    assert state.specialist_evaluation is not None
    assert tuple(item.evaluation_ref for item in plan.evaluations) == state.evaluation_refs
    prompt = read_method_prompt_v2(
        records,
        job_id=job.job_id,
        role="SPECIALIST",
        attempt="PRIMARY",
    )
    assert prompt == backend.role_prompts[0].encode("utf-8")


def test_specialist_context_limit_preserves_classified_failure_without_terminal_projection(
    tmp_path: Path,
) -> None:
    runtime, job, backend, _ = _runtime(tmp_path, ("VALID",))
    runtime._context_builder = _TooLargeContext()  # noqa: SLF001 - failure injection

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    error = receipt.job_outcome.error
    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert error is not None
    assert error.stage is ExecutionStage.CONTEXT_BUILD
    assert error.code is ErrorCode.CONTEXT_LIMIT
    assert error.reason_code is None
    assert error.details[0].observed == 131073
    assert error.details[0].limit == 131072
    assert receipt.job_outcome.methods_terminal_projection is None
    assert receipt.job_outcome.methods_reviewer_result is None
    assert backend.calls == []


def test_specialist_final_role_prompt_is_included_in_context_byte_limit(
    tmp_path: Path,
) -> None:
    runtime, job, backend, records = _runtime(tmp_path, ("VALID",))
    runtime._context_builder = _ContextBodyAtRoleLimit()  # noqa: SLF001

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    error = receipt.job_outcome.error
    assert error is not None
    assert error.stage is ExecutionStage.CONTEXT_BUILD
    assert error.code is ErrorCode.CONTEXT_LIMIT
    assert error.details[0].limit == job.resource_limits.context_bytes
    assert error.details[0].observed > error.details[0].limit
    assert receipt.job_outcome.methods_terminal_projection is None
    assert backend.calls == []
    assert read_method_prompt_v2(
        records,
        job_id=job.job_id,
        role="SPECIALIST",
        attempt="PRIMARY",
    ) is None


def test_specialist_context_audit_failure_remains_audit_terminal(
    tmp_path: Path,
) -> None:
    records = _RejectContextAuditRecords()
    runtime, job, backend, _ = _runtime(
        tmp_path,
        ("VALID",),
        records=records,
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    outcome = receipt.job_outcome
    projection = outcome.methods_terminal_projection
    assert records.context_publish_attempts == 1
    assert backend.calls == []
    assert outcome.result_type is OutcomeResultType.FAILED
    assert outcome.error is not None
    assert outcome.error.stage is ExecutionStage.EXECUTION_RECORD
    assert outcome.error.code is ErrorCode.EXECUTION_RECORD_FAILED
    assert outcome.error.retryable is False
    assert outcome.error.reason_code == "AUDIT_ARCHIVE_FAILED"
    assert projection is not None
    assert projection.status == "FAILED"
    assert projection.reason_code == "AUDIT_ARCHIVE_FAILED"
    assert projection.evidence_graph_ref is not None
    assert projection.plan_ref is not None


@pytest.mark.parametrize(
    ("response", "expected_status", "expected_reason"),
    [
        ("VALID", "RESOLVED", None),
        ("VALID_UNKNOWN", "UNRESOLVED", "INCOMPLETE_EVALUATION"),
        ("VALID_REJECTED", "UNRESOLVED", "NO_CONFIRMED_METHOD"),
    ],
)
def test_reviewer_disabled_finishes_from_specialist_without_review_artifacts(
    tmp_path: Path,
    response: str,
    expected_status: str,
    expected_reason: str | None,
) -> None:
    runtime, job, backend, records = _runtime(
        tmp_path,
        (response,),
        reviewer_enabled=False,
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None
    assert projection.status == expected_status
    assert projection.reason_code == expected_reason
    assert receipt.job_outcome.methods_review_target is None
    assert receipt.job_outcome.methods_reviewer_result is None
    assert len(backend.calls) == 1
    state = read_method_state_v2(records, job_id=job.job_id)
    assert state is not None
    assert state.status == expected_status
    assert state.specialist_evaluation is not None
    assert state.reviewer_evaluation is None
    assert state.consensus is None
    assert state.reviewer_protocol_failures == 0
    assert read_method_prompt_v2(
        records,
        job_id=job.job_id,
        role="REVIEWER",
        attempt="PRIMARY",
    ) is None
    assert not tuple((tmp_path / "runtime-data").rglob("method-review.draft.json"))


def test_specialist_uses_one_repair_then_stops(
    tmp_path: Path,
) -> None:
    runtime, job, backend, records = _runtime(tmp_path, ({"bad": True}, {"bad": 2}))

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert len(backend.calls) == 2
    assert len(backend.role_prompts) == 2
    assert "supporting_event_refs" in backend.role_prompts[1]
    assert "Every item must again contain exactly evaluation_ref, verdict, " in (
        backend.role_prompts[1]
    )
    assert receipt.job_outcome.result_type is OutcomeResultType.INCONCLUSIVE
    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None
    assert projection.status == "UNRESOLVED"
    assert projection.reason_code == "SPECIALIST_PROTOCOL_REPAIR_EXHAUSTED"
    state = read_method_state_v2(records, job_id=job.job_id)
    assert state is not None
    assert state.specialist_protocol_failures == 2
    assert read_method_rejected_attempt_v2(
        records,
        job_id=job.job_id,
        role="SPECIALIST",
        attempt="PRIMARY",
    ) == json.dumps({"bad": True}).encode("utf-8")
    assert read_method_rejected_attempt_v2(
        records,
        job_id=job.job_id,
        role="SPECIALIST",
        attempt="REPAIR",
    ) == json.dumps({"bad": 2}).encode("utf-8")


def test_specialist_output_io_failure_is_failed_without_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, job, backend, _ = _runtime(tmp_path, ("VALID",))
    from problem_locator.runtime import diagnosis_runtime as runtime_module

    def unreadable(*args: Any, **kwargs: Any):
        del args, kwargs
        raise PermissionError("injected unreadable output")

    monkeypatch.setattr(runtime_module, "read_method_role_attempt_v2", unreadable)

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert len(backend.calls) == 1
    projection = receipt.job_outcome.methods_terminal_projection
    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert projection is not None
    assert projection.status == "FAILED"
    assert projection.reason_code == "SERVER_INVARIANT_VIOLATION"


@pytest.mark.parametrize("accept_request", [False, True])
def test_specialist_preprocessing_requires_claim_and_matching_request(
    tmp_path: Path,
    accept_request: bool,
) -> None:
    runtime, job, backend, _ = _runtime(tmp_path, ("VALID",))
    backend.accept_request = accept_request
    backend.emit_claim = False

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    if accept_request:
        assert len(backend.calls) == 0
        assert receipt.job_outcome.error is not None
        assert receipt.job_outcome.error.code is ErrorCode.LOGPARSE_OUTPUT_INVALID
    else:
        assert len(backend.calls) == 1
        assert receipt.job_outcome.methods_review_target is not None


def test_specialist_private_reason_is_not_published_in_handoff(
    tmp_path: Path,
) -> None:
    runtime, job, backend, records = _runtime(tmp_path, ("VALID_SECRET",))

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.methods_review_target is not None
    published = records.publish_outcome_calls[0][1]
    assert b"contract-test-token-1" not in published
    state = read_method_state_v2(records, job_id=job.job_id)
    assert state is not None and state.specialist_evaluation is not None
    assert state.specialist_evaluation.evaluations[0].reason == "contract-test-token-1"
    workspace_root = Path(backend.calls[0]["workspace_root"])
    assert b"contract-test-token-1" in (
        workspace_root / "output/method-diagnosis.draft.json"
    ).read_bytes()


def test_reviewer_is_blind_and_reads_pending_state_after_model(
    tmp_path: Path,
) -> None:
    runtime, job, backend, records, events = _review_runtime(
        tmp_path,
        ("VALID_CONFIRMED",),
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert len(backend.calls) == 1
    assert events.index("backend") < events.index("source-state-read")
    assert receipt.job_outcome.result_type is OutcomeResultType.COMPLETED
    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None
    assert projection.status == "RESOLVED"
    assert projection.confirmed_method_ids
    assert projection.limitations == ("Only the frozen target set was evaluated.",)
    terminal_state = read_method_state_v2(records, job_id=job.job_id)
    assert terminal_state is not None
    assert terminal_state.status == "RESOLVED"
    assert terminal_state.reviewer_evaluation is not None
    assert "private specialist reason" not in backend.prompts[0]
    attribution_bytes = records.read_audit_bytes(
        job.job_id,
        METHODS_CONSENSUS_ATTRIBUTION_V2_FILENAME,
    )
    assert attribution_bytes is not None
    attribution = parse_canonical_json_bytes(attribution_bytes)
    assert attribution["terminal_status"] == "RESOLVED"
    assert attribution["reason_code"] is None
    assert attribution["consensus_subreason"] is None


def test_reviewer_context_limit_preserves_classified_failure_without_terminal_projection(
    tmp_path: Path,
) -> None:
    runtime, job, backend, _, _ = _review_runtime(
        tmp_path,
        ("VALID_CONFIRMED",),
    )
    runtime._context_builder = _TooLargeContext()  # noqa: SLF001 - failure injection

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    error = receipt.job_outcome.error
    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert error is not None
    assert error.stage is ExecutionStage.CONTEXT_BUILD
    assert error.code is ErrorCode.CONTEXT_LIMIT
    assert error.reason_code is None
    assert error.details[0].observed == 131073
    assert error.details[0].limit == 131072
    assert receipt.job_outcome.methods_terminal_projection is None
    assert receipt.job_outcome.methods_reviewer_result is None
    assert backend.calls == []


def test_reviewer_final_role_prompt_is_included_in_context_byte_limit(
    tmp_path: Path,
) -> None:
    runtime, job, backend, records, events = _review_runtime(
        tmp_path,
        ("VALID_CONFIRMED",),
    )
    runtime._context_builder = _ContextBodyAtRoleLimit()  # noqa: SLF001

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    error = receipt.job_outcome.error
    assert error is not None
    assert error.stage is ExecutionStage.CONTEXT_BUILD
    assert error.code is ErrorCode.CONTEXT_LIMIT
    assert error.details[0].limit == job.resource_limits.context_bytes
    assert error.details[0].observed > error.details[0].limit
    assert receipt.job_outcome.methods_terminal_projection is None
    assert backend.calls == []
    assert "source-state-read" not in events
    assert read_method_prompt_v2(
        records,
        job_id=job.job_id,
        role="REVIEWER",
        attempt="PRIMARY",
    ) is None


@pytest.mark.parametrize(
    ("response", "reason_code"),
    [
        ("VALID_REJECTED", "SPECIALIST_REVIEWER_DISAGREEMENT"),
        ("VALID_UNKNOWN", "INCOMPLETE_EVALUATION"),
    ],
)
def test_reviewer_disagreement_and_unknown_are_unresolved(
    tmp_path: Path,
    response: str,
    reason_code: str,
) -> None:
    runtime, job, backend, _, _ = _review_runtime(tmp_path, (response,))

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert len(backend.calls) == 1
    assert receipt.job_outcome.result_type is OutcomeResultType.INCONCLUSIVE
    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None
    assert projection.status == "UNRESOLVED"
    assert projection.reason_code == reason_code
    assert projection.limitations == ("Only the frozen target set was evaluated.",)


@pytest.mark.parametrize(
    (
        "specialist_verdict",
        "specialist_event_selection",
        "reviewer_response",
        "public_reason_code",
        "consensus_subreason",
    ),
    [
        (
            "CONFIRMED",
            "ALL",
            "VALID_UNKNOWN",
            "INCOMPLETE_EVALUATION",
            "UNKNOWN_PRESENT",
        ),
        (
            "CONFIRMED",
            "ALL",
            "VALID_REJECTED",
            "SPECIALIST_REVIEWER_DISAGREEMENT",
            "VERDICT_MISMATCH",
        ),
        (
            "CONFIRMED",
            "FIRST",
            "VALID_CONFIRMED_LAST_EVENT",
            "SPECIALIST_REVIEWER_DISAGREEMENT",
            "EVIDENCE_SET_MISMATCH",
        ),
        (
            "REJECTED",
            "ALL",
            "VALID_REJECTED",
            "NO_CONFIRMED_METHOD",
            "NO_CONFIRMED",
        ),
    ],
)
def test_reviewer_execution_record_distinguishes_consensus_subreasons(
    tmp_path: Path,
    specialist_verdict: str,
    specialist_event_selection: str,
    reviewer_response: str,
    public_reason_code: str,
    consensus_subreason: str,
) -> None:
    target_bytes = (
        b"API_COMPLETE request_id=req-1\n"
        b"API_COMPLETE request_id=req-2\n"
    )
    runtime, job, _, records, _ = _review_runtime(
        tmp_path,
        (reviewer_response,),
        specialist_verdict=specialist_verdict,
        specialist_event_selection=specialist_event_selection,
        methods=(
            ("slow-execution", "API_COMPLETE"),
            ("inactive-method", "NEVER_SEEN"),
        ),
        target_bytes=target_bytes,
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None
    assert projection.status == "UNRESOLVED"
    assert projection.reason_code == public_reason_code
    assert "consensus_subreason" not in projection.model_dump(mode="json")
    attribution_bytes = records.read_audit_bytes(
        job.job_id,
        METHODS_CONSENSUS_ATTRIBUTION_V2_FILENAME,
    )
    assert attribution_bytes is not None
    attribution = parse_canonical_json_bytes(attribution_bytes)
    assert attribution["terminal_status"] == "UNRESOLVED"
    assert attribution["reason_code"] == public_reason_code
    assert attribution["consensus_subreason"] == consensus_subreason
    assert attribution["evaluation_count"] == 1
    assert attribution["evaluation_event_counts"] == [
        {
            "evaluation_ref": attribution["evaluation_event_counts"][0][
                "evaluation_ref"
            ],
            "event_count": 2,
        }
    ]
    assert attribution["activated_method_count"] == 1
    assert attribution["package_method_count"] == 2
    published_outcome = records.publish_outcome_calls[0][1]
    assert b"consensus_subreason" not in published_outcome


def test_specialist_unknown_is_attributed_without_changing_public_reason(
    tmp_path: Path,
) -> None:
    runtime, job, _, records, _ = _review_runtime(
        tmp_path,
        ("VALID_REJECTED",),
        specialist_verdict="UNKNOWN",
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None
    assert projection.reason_code == "SPECIALIST_REVIEWER_DISAGREEMENT"
    attribution_bytes = records.read_audit_bytes(
        job.job_id,
        METHODS_CONSENSUS_ATTRIBUTION_V2_FILENAME,
    )
    assert attribution_bytes is not None
    attribution = parse_canonical_json_bytes(attribution_bytes)
    assert attribution["reason_code"] == "SPECIALIST_REVIEWER_DISAGREEMENT"
    assert attribution["consensus_subreason"] == "UNKNOWN_PRESENT"


def test_reviewer_attribution_archive_failure_is_not_silently_lost(
    tmp_path: Path,
) -> None:
    _, _, specialist, _, _, _, _, _ = _jobs(tmp_path / "preview")
    events: list[str] = []
    records = _RejectConsensusAttributionRecords(events, specialist.job_id)
    runtime, job, backend, _, _ = _review_runtime(
        tmp_path / "runtime",
        ("VALID_CONFIRMED",),
        records=records,
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert len(backend.calls) == 1
    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None
    assert projection.status == "FAILED"
    assert projection.reason_code == "AUDIT_ARCHIVE_FAILED"
    assert records.read_audit_bytes(
        job.job_id,
        METHODS_CONSENSUS_ATTRIBUTION_V2_FILENAME,
    ) is None


def test_reviewer_uses_at_most_one_repair(
    tmp_path: Path,
) -> None:
    runtime, job, backend, records, _ = _review_runtime(
        tmp_path,
        ({"invalid": 1}, {"invalid": 2}),
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert len(backend.calls) == 2
    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None
    assert projection.reason_code == "REVIEWER_PROTOCOL_REPAIR_EXHAUSTED"
    assert projection.limitations == ("Only the frozen target set was evaluated.",)
    state = read_method_state_v2(records, job_id=job.job_id)
    assert state is not None
    assert state.reviewer_protocol_failures == 2


def test_reviewer_model_failure_is_unresolved_and_preserves_limitations(
    tmp_path: Path,
) -> None:
    runtime, job, backend, _, _ = _review_runtime(
        tmp_path,
        ("MODEL_FAILURE",),
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert len(backend.calls) == 1
    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None
    assert projection.status == "UNRESOLVED"
    assert projection.reason_code == "REVIEWER_MODEL_EXECUTION_FAILED"
    assert projection.limitations == ("Only the frozen target set was evaluated.",)


def test_reviewer_output_io_failure_is_failed_and_preserves_limitations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, job, backend, _, _ = _review_runtime(
        tmp_path,
        ("VALID_CONFIRMED",),
    )
    from problem_locator.runtime import diagnosis_runtime as runtime_module

    def unreadable(*args: Any, **kwargs: Any):
        del args, kwargs
        raise PermissionError("injected reviewer output failure")

    monkeypatch.setattr(runtime_module, "read_method_role_attempt_v2", unreadable)

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert len(backend.calls) == 1
    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None
    assert projection.status == "FAILED"
    assert projection.reason_code == "SERVER_INVARIANT_VIOLATION"
    assert projection.limitations == ("Only the frozen target set was evaluated.",)


def test_specialist_restart_reuses_graph_and_runs_only_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, preview_job, _, _ = _runtime(tmp_path / "preview", ("VALID",))
    records = _CrashBeforeRepairRecords(
        [],
        preview_job.job_id,
        method_prompt_filename_v2(role="SPECIALIST", attempt="REPAIR"),
    )
    first, first_job, first_backend, _ = _runtime(
        tmp_path / "first",
        ({"invalid": True},),
        records=records,
    )

    with pytest.raises(KeyboardInterrupt, match="restart boundary"):
        first.execute(first_job, InMemoryCancellationSignal())

    assert len(first_backend.calls) == 1
    assert read_method_rejected_attempt_v2(
        records,
        job_id=first_job.job_id,
        role="SPECIALIST",
        attempt="PRIMARY",
    ) is not None

    from problem_locator.runtime import diagnosis_runtime as runtime_module

    def forbidden_scan(**kwargs: Any):
        del kwargs
        raise AssertionError("restart must reuse the production Graph")

    monkeypatch.setattr(runtime_module, "scan_method_evidence_v2", forbidden_scan)
    restarted, restart_job, restart_backend, _ = _runtime(
        tmp_path / "restart",
        ("VALID",),
        records=records,
    )

    receipt = restarted.execute(restart_job, InMemoryCancellationSignal())

    assert len(restart_backend.calls) == 1
    assert receipt.job_outcome.methods_review_target is not None
    state = read_method_state_v2(records, job_id=restart_job.job_id)
    assert state is not None and state.specialist_evaluation is not None
    assert state.specialist_evaluation.repair_used is True


def test_specialist_restart_rebuilds_missing_plan_from_recorded_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, preview_job, _, _ = _runtime(tmp_path / "preview", ("VALID",))
    records = _CrashBeforeRepairRecords(
        [],
        preview_job.job_id,
        METHODS_EVALUATION_PLAN_V2_FILENAME,
    )
    first, first_job, first_backend, _ = _runtime(
        tmp_path / "first",
        ("VALID",),
        records=records,
    )

    with pytest.raises(KeyboardInterrupt, match="restart boundary"):
        first.execute(first_job, InMemoryCancellationSignal())

    assert len(first_backend.calls) == 0
    assert read_method_evidence_graph_v2(records, job_id=first_job.job_id) is not None
    assert read_method_evaluation_plan_v2(records, job_id=first_job.job_id) is None

    from problem_locator.runtime import diagnosis_runtime as runtime_module

    def forbidden_scan(**kwargs: Any):
        del kwargs
        raise AssertionError("recorded Graph must be the restart source of truth")

    monkeypatch.setattr(runtime_module, "scan_method_evidence_v2", forbidden_scan)
    restarted, restart_job, restart_backend, _ = _runtime(
        tmp_path / "restart",
        ("VALID",),
        records=records,
    )

    receipt = restarted.execute(restart_job, InMemoryCancellationSignal())

    assert len(restart_backend.calls) == 1
    assert receipt.job_outcome.methods_review_target is not None
    assert read_method_evaluation_plan_v2(records, job_id=restart_job.job_id) is not None


def test_reviewer_restart_runs_only_repair_and_reads_source_state_after_model(
    tmp_path: Path,
) -> None:
    _, _, specialist, _, _, _, _, _ = _jobs(tmp_path / "preview")
    events: list[str] = []
    records = _CrashBeforeRepairRecords(
        events,
        specialist.job_id,
        method_prompt_filename_v2(role="REVIEWER", attempt="REPAIR"),
    )
    first, first_job, first_backend, _, _ = _review_runtime(
        tmp_path / "first",
        ({"invalid": True},),
        records=records,
    )

    with pytest.raises(KeyboardInterrupt, match="restart boundary"):
        first.execute(first_job, InMemoryCancellationSignal())

    assert len(first_backend.calls) == 1
    restarted, restart_job, restart_backend, _, restart_events = _review_runtime(
        tmp_path / "restart",
        ("VALID_CONFIRMED",),
        records=records,
    )

    receipt = restarted.execute(restart_job, InMemoryCancellationSignal())

    assert len(restart_backend.calls) == 1
    assert restart_events.index("backend") < restart_events.index("source-state-read")
    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None and projection.status == "RESOLVED"
    assert projection.limitations == ("Only the frozen target set was evaluated.",)
    state = read_method_state_v2(records, job_id=restart_job.job_id)
    assert state is not None and state.reviewer_evaluation is not None
    assert state.reviewer_evaluation.repair_used is True


def _specialist_replacement_source(tmp_path: Path):
    catalog, _, specialist, _, _, graph, plan, _ = _jobs(tmp_path / "source")
    source_value = specialist.model_dump(mode="json")
    source_value["previous_outcome_refs"] = []
    source_value["context_snapshot"]["candidate_conclusion"] = None
    source_job = _running(Job.model_validate(source_value))
    aggregate = _interrupted_aggregate(source_job)
    records = InMemoryExecutionRecordStore()
    evaluation_id = "00000000-0000-0000-0000-000000000072"
    interrupted = interrupt_method_state_v2(
        state=record_protocol_error_v2(
            state=start_method_state_v2(
                case_id=source_job.case_id,
                source_job_id=source_job.job_id,
                evaluation_id=evaluation_id,
                plan=plan,
            ),
            role="SPECIALIST",
            reason="The primary Specialist response did not match the contract.",
        )
    )
    publish_method_evidence_graph_v2(records, job_id=source_job.job_id, graph=graph)
    publish_method_evaluation_plan_v2(records, job_id=source_job.job_id, plan=plan)
    publish_method_limitations_record_v2(
        records,
        job_id=source_job.job_id,
        record=build_method_limitations_record_v2(
            case_id=source_job.case_id,
            source_job_id=source_job.job_id,
            graph=graph,
            plan=plan,
            limitations=graph.limitations,
        ),
    )
    publish_method_rejected_attempt_v2(
        records,
        job_id=source_job.job_id,
        role="SPECIALIST",
        attempt="PRIMARY",
        raw_bytes=b'{"invalid":true}',
    )
    publish_method_state_v2(records, job_id=source_job.job_id, state=interrupted)
    return (
        catalog,
        source_job,
        aggregate,
        records,
        graph,
        plan,
        evaluation_id,
        _ResourceStore(),
    )


def test_specialist_replacement_resumes_old_repair_without_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        catalog,
        _,
        aggregate,
        records,
        graph,
        plan,
        evaluation_id,
        resources,
    ) = _specialist_replacement_source(tmp_path)

    repository, replacement = _resume_and_claim_replacement(
        aggregate=aggregate,
        resources=resources,
        catalog=catalog,
        records=records,
        replacement_job_id="00000000-0000-0000-0000-000000000073",
    )
    factory = FakeLogparseBrokerFactory()
    backend = _EvidenceV2SpecialistBackend(factory, replacement, ("VALID",))
    runtime = DiagnosisRuntime(
        state_repository=repository,
        resource_store=resources,  # type: ignore[arg-type]
        asset_catalog=catalog,
        logparse_broker_factory=factory,
        execution_records=records,
        clock=FakeClock("2026-07-31T00:05:00.000Z"),
        id_generator=DeterministicIdGenerator(seed="specialist-replacement"),
        workspace_manager=WorkspaceManager(tmp_path / "replacement"),
        backend=backend,
        evidence_v2_reviewer_enabled=True,
    )
    from problem_locator.runtime import diagnosis_runtime as runtime_module

    def forbidden_scan(**kwargs: Any):
        del kwargs
        raise AssertionError("replacement must reuse the predecessor Evidence Graph")

    monkeypatch.setattr(runtime_module, "scan_method_evidence_v2", forbidden_scan)

    receipt = runtime.execute(replacement, InMemoryCancellationSignal())

    assert receipt.job_outcome.methods_review_target is not None, receipt.job_outcome.error
    assert len(backend.calls) == 1
    assert len(backend.role_prompts) == 1
    assert read_method_evidence_graph_v2(records, job_id=replacement.job_id) == graph
    assert read_method_evaluation_plan_v2(records, job_id=replacement.job_id) == plan
    limitations = read_method_limitations_record_v2(
        records,
        job_id=replacement.job_id,
    )
    assert limitations is not None
    assert limitations.limitations == graph.limitations
    assert read_method_rejected_attempt_v2(
        records,
        job_id=replacement.job_id,
        role="SPECIALIST",
        attempt="PRIMARY",
    ) == b'{"invalid":true}'
    state = read_method_state_v2(records, job_id=replacement.job_id)
    assert state is not None and state.specialist_evaluation is not None
    assert state.source_job_id == replacement.job_id
    assert state.evaluation_id == evaluation_id
    assert state.specialist_protocol_failures == 1
    assert state.specialist_evaluation.repair_used is True


def test_specialist_replacement_resource_drift_keeps_old_evaluation_lineage(
    tmp_path: Path,
) -> None:
    (
        catalog,
        _,
        aggregate,
        records,
        graph,
        plan,
        evaluation_id,
        resources,
    ) = _specialist_replacement_source(tmp_path)
    repository, replacement = _resume_and_claim_replacement(
        aggregate=aggregate,
        resources=resources,
        catalog=catalog,
        records=records,
        replacement_job_id="00000000-0000-0000-0000-000000000075",
    )
    skill_file = next((tmp_path / "source" / "skills").rglob("SKILL.md"))
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8") + "\nreplacement drift\n",
        encoding="utf-8",
    )
    factory = FakeLogparseBrokerFactory()
    backend = _EvidenceV2SpecialistBackend(factory, replacement, ("VALID",))
    runtime = DiagnosisRuntime(
        state_repository=repository,
        resource_store=resources,  # type: ignore[arg-type]
        asset_catalog=catalog,
        logparse_broker_factory=factory,
        execution_records=records,
        clock=FakeClock("2026-07-31T00:05:00.000Z"),
        id_generator=DeterministicIdGenerator(seed="specialist-replacement-drift"),
        workspace_manager=WorkspaceManager(tmp_path / "replacement-drift"),
        backend=backend,
        evidence_v2_reviewer_enabled=True,
    )

    receipt = runtime.execute(replacement, InMemoryCancellationSignal())

    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None
    assert projection.status == "FAILED"
    assert projection.reason_code == "RESOURCE_SNAPSHOT_DRIFT"
    assert projection.evaluation_id == evaluation_id
    assert backend.calls == []
    assert read_method_evidence_graph_v2(records, job_id=replacement.job_id) == graph
    assert read_method_evaluation_plan_v2(records, job_id=replacement.job_id) == plan
    assert read_method_rejected_attempt_v2(
        records,
        job_id=replacement.job_id,
        role="SPECIALIST",
        attempt="PRIMARY",
    ) == b'{"invalid":true}'
    state = read_method_state_v2(records, job_id=replacement.job_id)
    assert state is not None
    assert state.source_job_id == replacement.job_id
    assert state.evaluation_id == evaluation_id
    assert state.specialist_protocol_failures == 1


def test_specialist_replacement_lineage_resumes_from_immediate_predecessor(
    tmp_path: Path,
) -> None:
    (
        catalog,
        source_job,
        aggregate,
        records,
        graph,
        plan,
        evaluation_id,
        resources,
    ) = _specialist_replacement_source(tmp_path)
    first_repository, first_replacement = _resume_and_claim_replacement(
        aggregate=aggregate,
        resources=resources,
        catalog=catalog,
        records=records,
        replacement_job_id="00000000-0000-0000-0000-000000000076",
    )
    source_state = read_method_state_v2(records, job_id=source_job.job_id)
    assert source_state is not None
    first_interrupted_state = interrupt_method_state_v2(
        state=resume_method_state_v2(
            state=source_state,
            source_job_id=first_replacement.job_id,
        )
    )
    publish_method_evidence_graph_v2(
        records,
        job_id=first_replacement.job_id,
        graph=graph,
    )
    publish_method_evaluation_plan_v2(
        records,
        job_id=first_replacement.job_id,
        plan=plan,
    )
    publish_method_limitations_record_v2(
        records,
        job_id=first_replacement.job_id,
        record=build_method_limitations_record_v2(
            case_id=first_replacement.case_id,
            source_job_id=first_replacement.job_id,
            graph=graph,
            plan=plan,
            limitations=graph.limitations,
        ),
    )
    publish_method_rejected_attempt_v2(
        records,
        job_id=first_replacement.job_id,
        role="SPECIALIST",
        attempt="PRIMARY",
        raw_bytes=b'{"invalid":true}',
    )
    publish_method_state_v2(
        records,
        job_id=first_replacement.job_id,
        state=first_interrupted_state,
    )
    first_aggregate = _interrupt_active_job(
        first_repository.read_case(first_replacement.case_id),
        first_replacement,
    )
    second_repository, second_replacement = _resume_and_claim_replacement(
        aggregate=first_aggregate,
        resources=resources,
        catalog=catalog,
        records=records,
        replacement_job_id="00000000-0000-0000-0000-000000000077",
    )
    factory = FakeLogparseBrokerFactory()
    backend = _EvidenceV2SpecialistBackend(factory, second_replacement, ("VALID",))
    runtime = DiagnosisRuntime(
        state_repository=second_repository,
        resource_store=resources,  # type: ignore[arg-type]
        asset_catalog=catalog,
        logparse_broker_factory=factory,
        execution_records=records,
        clock=FakeClock("2026-07-31T00:07:00.000Z"),
        id_generator=DeterministicIdGenerator(seed="specialist-second-replacement"),
        workspace_manager=WorkspaceManager(tmp_path / "second-replacement"),
        backend=backend,
        evidence_v2_reviewer_enabled=True,
    )

    receipt = runtime.execute(second_replacement, InMemoryCancellationSignal())

    assert receipt.job_outcome.methods_review_target is not None
    assert len(backend.calls) == 1
    state = read_method_state_v2(records, job_id=second_replacement.job_id)
    assert state is not None and state.specialist_evaluation is not None
    assert state.source_job_id == second_replacement.job_id
    assert state.evaluation_id == evaluation_id
    assert state.specialist_protocol_failures == 1
    assert state.specialist_evaluation.repair_used is True


def test_reviewer_replacement_inherits_old_rejection_and_interrupted_state(
    tmp_path: Path,
) -> None:
    catalog, _, specialist, reviewer, _, graph, plan, _ = _jobs(tmp_path / "source")
    source_job = _running(reviewer)
    aggregate = _interrupted_aggregate(source_job, source_job=specialist)
    records = InMemoryExecutionRecordStore()
    target = source_job.methods_review_target
    assert target is not None
    specialist_evaluation = evaluate_method_role_v2(
        role="SPECIALIST",
        plan=plan,
        response=[
            {
                "evaluation_ref": item.evaluation_ref,
                "verdict": "CONFIRMED",
                "supporting_event_refs": list(item.evidence_event_refs),
                "reason": "private specialist reason",
            }
            for item in plan.evaluations
        ],
        attempt="PRIMARY",
    )
    reviewer_pending = accept_specialist_evaluation_v2(
        state=start_method_state_v2(
            case_id=source_job.case_id,
            source_job_id=target.source_job_id,
            evaluation_id=target.evaluation_id,
            plan=plan,
        ),
        evaluation=specialist_evaluation,
    )
    interrupted = interrupt_method_state_v2(
        state=record_protocol_error_v2(
            state=reviewer_pending,
            role="REVIEWER",
            reason="The primary Reviewer response did not match the contract.",
        )
    )
    publish_method_evidence_graph_v2(
        records,
        job_id=target.source_job_id,
        graph=graph,
    )
    publish_method_evaluation_plan_v2(
        records,
        job_id=target.source_job_id,
        plan=plan,
    )
    publish_method_limitations_record_v2(
        records,
        job_id=target.source_job_id,
        record=build_method_limitations_record_v2(
            case_id=source_job.case_id,
            source_job_id=target.source_job_id,
            graph=graph,
            plan=plan,
            limitations=graph.limitations,
        ),
    )
    publish_method_state_v2(
        records,
        job_id=target.source_job_id,
        state=reviewer_pending,
    )
    publish_method_rejected_attempt_v2(
        records,
        job_id=source_job.job_id,
        role="REVIEWER",
        attempt="PRIMARY",
        raw_bytes=b'{"invalid":true}',
    )
    publish_method_state_v2(records, job_id=source_job.job_id, state=interrupted)

    resources = _UnusedResourceStore()
    repository, replacement = _resume_and_claim_replacement(
        aggregate=aggregate,
        resources=resources,
        catalog=catalog,
        records=records,
        replacement_job_id="00000000-0000-0000-0000-000000000074",
    )
    events: list[str] = []
    backend = _EvidenceV2ReviewerBackend(("VALID_CONFIRMED",), events)
    runtime = DiagnosisRuntime(
        state_repository=repository,
        resource_store=resources,  # type: ignore[arg-type]
        asset_catalog=catalog,
        logparse_broker_factory=None,
        execution_records=records,
        clock=FakeClock("2026-07-31T00:05:00.000Z"),
        id_generator=DeterministicIdGenerator(seed="reviewer-replacement"),
        workspace_manager=WorkspaceManager(tmp_path / "replacement"),
        backend=backend,
    )

    receipt = runtime.execute(replacement, InMemoryCancellationSignal())

    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None and projection.status == "RESOLVED"
    assert len(backend.calls) == 1
    assert read_method_rejected_attempt_v2(
        records,
        job_id=replacement.job_id,
        role="REVIEWER",
        attempt="PRIMARY",
    ) == b'{"invalid":true}'
    state = read_method_state_v2(records, job_id=replacement.job_id)
    assert state is not None and state.reviewer_evaluation is not None
    assert state.source_job_id == target.source_job_id
    assert state.evaluation_id == target.evaluation_id
    assert state.reviewer_protocol_failures == 1
    assert state.reviewer_evaluation.repair_used is True


def test_reviewer_pinned_skill_drift_produces_methods_failed_result(
    tmp_path: Path,
) -> None:
    runtime, job, backend, _, _ = _review_runtime(
        tmp_path,
        ("VALID_CONFIRMED",),
    )
    card = next((tmp_path / "skills").rglob("slow-execution.md"))
    card.write_text(
        card.read_text(encoding="utf-8") + "\nresource drift\n",
        encoding="utf-8",
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert backend.calls == []
    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None
    assert projection.status == "FAILED"
    assert projection.reason_code == "RESOURCE_SNAPSHOT_DRIFT"
    assert projection.limitations == ("Only the frozen target set was evaluated.",)


def test_specialist_pending_state_wins_over_later_asset_drift(
    tmp_path: Path,
) -> None:
    _, preview_job, _, _ = _runtime(tmp_path / "preview", ("VALID",))
    records = _CrashBeforeOutcomeRecords([], preview_job.job_id)
    first, first_job, first_backend, _ = _runtime(
        tmp_path / "first",
        ("VALID",),
        records=records,
    )

    with pytest.raises(KeyboardInterrupt, match="outcome boundary"):
        first.execute(first_job, InMemoryCancellationSignal())

    pending = read_method_state_v2(records, job_id=first_job.job_id)
    assert pending is not None and pending.status == "REVIEWER_PENDING"
    restarted, restart_job, restart_backend, _ = _runtime(
        tmp_path / "restart",
        ("VALID",),
        records=records,
        reviewer_enabled=False,
    )
    skill_file = next((tmp_path / "restart" / "logparse-skills").rglob("SKILL.md"))
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8") + "\nasset drift\n",
        encoding="utf-8",
    )

    receipt = restarted.execute(restart_job, InMemoryCancellationSignal())

    assert restart_backend.calls == []
    assert receipt.job_outcome.methods_review_target is not None
    assert receipt.job_outcome.methods_terminal_projection is None


def test_reviewer_terminal_state_wins_over_later_asset_drift(
    tmp_path: Path,
) -> None:
    _, _, specialist, _, _, _, _, _ = _jobs(tmp_path / "preview")
    records = _CrashBeforeOutcomeRecords([], specialist.job_id)
    first, first_job, first_backend, _, _ = _review_runtime(
        tmp_path / "first",
        ("VALID_CONFIRMED",),
        records=records,
    )

    with pytest.raises(KeyboardInterrupt, match="outcome boundary"):
        first.execute(first_job, InMemoryCancellationSignal())

    terminal = read_method_state_v2(records, job_id=first_job.job_id)
    assert terminal is not None and terminal.status == "RESOLVED"
    restarted, restart_job, restart_backend, _, _ = _review_runtime(
        tmp_path / "restart",
        ("VALID_CONFIRMED",),
        records=records,
    )
    skill_file = next((tmp_path / "restart" / "skills").rglob("SKILL.md"))
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8") + "\nasset drift\n",
        encoding="utf-8",
    )

    receipt = restarted.execute(restart_job, InMemoryCancellationSignal())

    assert restart_backend.calls == []
    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None and projection.status == "RESOLVED"
    assert projection.reason_code is None


def test_reviewer_rebuilds_missing_limitations_record_from_graph(
    tmp_path: Path,
) -> None:
    runtime, job, backend, _, _ = _review_runtime(
        tmp_path,
        ("VALID_CONFIRMED",),
        include_limitations=False,
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert len(backend.calls) == 1
    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None and projection.status == "RESOLVED"
    assert projection.limitations == ("Only the frozen target set was evaluated.",)


def test_reviewer_missing_limitations_archive_is_failed_without_model(
    tmp_path: Path,
) -> None:
    _, _, specialist, _, _, _, _, _ = _jobs(tmp_path / "preview")
    records = _RejectLimitationsArchiveRecords([], specialist.job_id)
    runtime, job, backend, _, _ = _review_runtime(
        tmp_path,
        ("VALID_CONFIRMED",),
        records=records,
        include_limitations=False,
    )
    records.block_limitations = True

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert backend.calls == []
    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None
    assert projection.status == "FAILED"
    assert projection.reason_code == "AUDIT_ARCHIVE_FAILED"
    assert projection.limitations == ()


@pytest.mark.parametrize(
    "filename",
    [
        METHODS_STATE_V2_FILENAME,
        method_prompt_filename_v2(role="SPECIALIST", attempt="PRIMARY"),
    ],
)
def test_specialist_adopts_exact_audit_record_after_ambiguous_write(
    tmp_path: Path,
    filename: str,
) -> None:
    records = _AfterWriteAuditRecords(filename)
    runtime, job, backend, _ = _runtime(
        tmp_path,
        ("VALID",),
        records=records,
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert records.failed_once is True
    assert len(backend.calls) == 1
    assert receipt.job_outcome.methods_review_target is not None
    assert receipt.job_outcome.methods_terminal_projection is None
    state = read_method_state_v2(records, job_id=job.job_id)
    assert state is not None and state.status == "REVIEWER_PENDING"


def test_terminal_contract_is_validated_before_state_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, job, _, records = _runtime(
        tmp_path,
        ({"invalid": 1}, {"invalid": 2}),
    )
    from problem_locator.runtime import diagnosis_runtime as runtime_module

    def invalid_terminal(*args: Any, **kwargs: Any):
        del args, kwargs
        raise ValueError("injected terminal contract failure")

    monkeypatch.setattr(runtime_module, "build_method_terminal_result_v2", invalid_terminal)

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert receipt.job_outcome.result_type is OutcomeResultType.FAILED
    assert receipt.job_outcome.methods_terminal_projection is None
    assert read_method_state_v2(records, job_id=job.job_id) is None


def test_unknown_backend_exception_is_server_invariant_not_audit_failure(
    tmp_path: Path,
) -> None:
    runtime, job, backend, _ = _runtime(tmp_path, ("RAISE_TYPE_ERROR",))

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert len(backend.calls) == 1
    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None
    assert projection.status == "FAILED"
    assert projection.reason_code == "SERVER_INVARIANT_VIOLATION"


def test_model_execution_failure_is_unresolved_without_repair(
    tmp_path: Path,
) -> None:
    runtime, job, backend, records = _runtime(tmp_path, ("MODEL_FAILURE",))

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert len(backend.calls) == 1
    assert receipt.job_outcome.result_type is OutcomeResultType.INCONCLUSIVE
    projection = receipt.job_outcome.methods_terminal_projection
    assert projection is not None
    assert projection.reason_code == "SPECIALIST_MODEL_EXECUTION_FAILED"
    state = read_method_state_v2(records, job_id=job.job_id)
    assert state is not None and state.specialist_protocol_failures == 0


def test_cancellation_persists_interrupted_state_without_terminal_projection(
    tmp_path: Path,
) -> None:
    runtime, job, backend, records = _runtime(tmp_path, ("CANCELLED",))

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    assert len(backend.calls) == 1
    assert receipt.job_outcome.methods_terminal_projection is None
    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.BACKEND_CANCELLED
    state = read_method_state_v2(records, job_id=job.job_id)
    assert state is not None and state.status == "INTERRUPTED"
