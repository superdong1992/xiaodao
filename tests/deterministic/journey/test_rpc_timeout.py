from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from problem_locator.application import build_application_service
from problem_locator.contracts import (
    AttachmentStatus,
    CaseStatus,
    FixtureManifest,
    JobStatus,
    JobType,
    RequirementStatus,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.dispatch import SchedulerService
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.integrations.logparse import build_logparse_runtime
from problem_locator.interfaces.http_app import create_http_app
from problem_locator.interfaces.mcp_server import McpAdapter
from problem_locator.runtime.agent_backend import AgentBackend, BackendExecutionLimits
from problem_locator.runtime.catalog import VersionedAssetCatalog
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime
from problem_locator.runtime.methods_records_v2 import (
    read_method_evaluation_plan_v2,
    read_method_evidence_graph_v2,
    read_method_state_v2,
)
from problem_locator.runtime.workspace import WorkspaceManager
from problem_locator.storage.coordination import (
    AttachmentUploadRegistry,
    InProcessAttachmentUploadGuard,
    InProcessPublicationCommitGuard,
    StorageCoordinationLock,
)
from problem_locator.storage.execution_records import FileExecutionRecordStore
from problem_locator.storage.layout import StorageLayout
from problem_locator.storage.platform import PlatformFileSync
from problem_locator.storage.resource_store import FileResourceStore
from problem_locator.storage.state_repository import JsonFileStateRepository
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
    FakeClock,
    InMemoryStateChangeNotifier,
)
from tests.deterministic.unit.interfaces.fakes import FakeStateAdmin
from tests.deterministic.unit.interfaces.helpers import readiness


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/rpc_timeout"
FAKE_AGENT = FIXTURES / "fake_agent.py"
ARCHIVE = FIXTURES / "payment-inventory-rpc.zip"
EXPECTED_PARSE_COUNTER = FIXTURES / "expected-parse-counter.json"
CROSS_PROJECT_EXPERIENCE = FIXTURES / "cross-project-result-experience.json"
FAKE_LOGPARSE_REPO = ROOT / "tests/fixtures/components/logparse/fake/repo"
FAKE_LOGPARSE_CONFIG = FAKE_LOGPARSE_REPO / "config.yaml"
SKILL_DIR = ROOT / "tests/fixtures/components/runtime-catalog/skill-dir"
RAW_LOGPARSE_ENV = {
    "LOGPARSE_REPO": "s08-raw-logparse-repo-sentinel",
    "LOGPARSE_CONFIG_PATH": "s08-raw-logparse-config-sentinel",
    "LOGPARSE_PYTHON": "s08-raw-logparse-python-sentinel",
    "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT": (
        "http://127.0.0.1:9/s08-stale-broker-endpoint-sentinel"
    ),
    "PROBLEM_LOCATOR_LOGPARSE_TOKEN": "s08-stale-broker-token-sentinel",
}
PARAMETER_GROUP_A = {
    "problem_time": "2026-07-31T00:00:03.000Z",
    "client_slot": "slot_1",
    "client_process_name": "checkout-client",
    "server_slot": "slot_2",
    "server_process_name": "inventory-server",
    "caller_service": "payment-service",
    "server_service": "inventory-service",
    "rpc_method": "ReserveStock",
}
RAW_RPC_CLIENT_LOG = (
    "2026-07-31T00:00:03.000Z COMPACT payment-service "
    "proc=checkout-client-101 slot 1 cpu 0 |No[1] rpc deadline exceeded "
    "after 3000ms server=inventory-service method=ReserveStock "
    "order_id=synthetic-order-0001\n"
)
RAW_RPC_SERVER_LOG = (
    "2026-07-31T00:00:00.100Z COMPACT inventory-service "
    "proc=inventory-server-202 slot 2 cpu 0 |No[2] service takeover active; "
    "rpc request accepted method=ReserveStock order_id=synthetic-order-0001\n"
    "2026-07-31T00:00:02.900Z COMPACT inventory-service "
    "proc=inventory-server-202 slot 2 cpu 0 |No[3] connection pool wait "
    "2800ms complete order_id=synthetic-order-0001\n"
)
RPC_CLIENT_LOG = "[0001] [diagnostic|payment.log] " + RAW_RPC_CLIENT_LOG
RPC_SERVER_LOG = (
    "[0001] [diagnostic|inventory.log] "
    + RAW_RPC_SERVER_LOG.splitlines(keepends=True)[0]
    + "[0002] [diagnostic|inventory.log] "
    + RAW_RPC_SERVER_LOG.splitlines(keepends=True)[1]
)


def _cross_project_result_experience() -> dict[str, Any]:
    value = parse_canonical_json_bytes(CROSS_PROJECT_EXPERIENCE.read_bytes())
    assert isinstance(value, dict)
    assert canonical_json_bytes(value) == CROSS_PROJECT_EXPERIENCE.read_bytes()
    assert value["schema_version"] == 1
    assert value["format_id"] == "cross-project-result-experience-v1"
    return value


def _materialize_fake_logparse_checkout(checkout: Path) -> tuple[Path, Path]:
    """Give fingerprinting a real top-level Git checkout on every platform."""

    if not checkout.exists():
        checkout.mkdir()
        source = (FAKE_LOGPARSE_REPO / "cli.py").read_text(encoding="utf-8")
        prefix, found, remainder = source.partition("CLIENT_LOG = (")
        _, function_marker, body = remainder.partition(
            "\ndef _reserved_environment_present"
        )
        assert found and function_marker
        customized = (
            prefix
            + f"CLIENT_LOG = {RPC_CLIENT_LOG!r}\n"
            + f"SERVER_LOG = {RPC_SERVER_LOG!r}\n\n"
            + function_marker
            + body
        )
        (checkout / "cli.py").write_text(
            customized,
            encoding="utf-8",
            newline="\n",
        )
        shutil.copyfile(FAKE_LOGPARSE_CONFIG, checkout / "config.yaml")
        subprocess.run(
            ["git", "-C", os.fspath(checkout), "init", "--quiet"],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "-C", os.fspath(checkout), "add", "cli.py", "config.yaml"],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    return checkout, checkout / "config.yaml"


def _remove_test_data_root(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(
        root.rglob("*"),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if not path.is_symlink():
            os.chmod(path, stat.S_IRWXU)
    os.chmod(root, stat.S_IRWXU)
    shutil.rmtree(root)


def _windows_extended_path(path: Path) -> Path:
    absolute = os.path.abspath(os.fspath(path))
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def _journey_storage_roots(tmp_path: Path, discriminator: str) -> tuple[Path, Path]:
    """Choose isolated short roots and opt Windows into extended paths."""

    if os.name != "nt":
        return tmp_path / ".s08", tmp_path / ".s08lp"
    compact = tmp_path.parent / discriminator
    return (
        _windows_extended_path(compact),
        _windows_extended_path(compact.with_name(compact.name + "lp")),
    )


class _LateDispatcher:
    """Close the Application↔Scheduler composition cycle exactly once."""

    def __init__(self) -> None:
        self._scheduler: SchedulerService | None = None

    def bind(self, scheduler: SchedulerService) -> None:
        assert self._scheduler is None
        self._scheduler = scheduler

    def submit(self, job_id: str):
        assert self._scheduler is not None
        return self._scheduler.submit(job_id)

    def cancel(self, job_id: str):
        assert self._scheduler is not None
        return self._scheduler.cancel(job_id)


class _CapturingBrokerFactory:
    """Retain real in-memory capabilities solely for the final leak oracle."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.capabilities: list[dict[str, str]] = []
        self.open_errors: list[str] = []

    def open(self, job, workspace_root, workspace_manifest, cancellation):
        try:
            session = self._delegate.open(
                job,
                workspace_root,
                workspace_manifest,
                cancellation,
            )
        except Exception as exc:
            self.open_errors.append(f"{type(exc).__name__}: {exc}")
            raise
        environment = session.agent_environment()
        assert set(environment) == {
            "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT",
            "PROBLEM_LOCATOR_LOGPARSE_TOKEN",
        }
        self.capabilities.append(dict(environment))
        return session


class _RecordingMcpAdapter:
    """Record exact black-box MCP envelopes without changing adapter behavior."""

    def __init__(self, delegate: McpAdapter) -> None:
        self._delegate = delegate
        self.responses: list[bytes] = []

    async def call(self, name: str, arguments: dict[str, object]) -> Any:
        result = await self._delegate.call(name, arguments)
        self.responses.append(canonical_json_bytes(result))
        return result


class _Stack:
    def __init__(
        self,
        data_root: Path,
        *,
        logparse_record: Path,
        agent_record: Path,
        review_entered: Path,
        review_release: Path,
        seed: str,
    ) -> None:
        self.data_root = data_root
        self.clock = FakeClock("2026-07-31T00:10:00.000Z")
        self.ids = DeterministicIdGenerator(seed=seed)
        self.coordination_lock = StorageCoordinationLock()
        self.attachment_registry = AttachmentUploadRegistry()
        self.layout = StorageLayout.at(data_root)
        self.layout.initialize_v2_data_root(PlatformFileSync())
        self.records = FileExecutionRecordStore(
            data_root,
            self.coordination_lock,
        )
        self.repository = JsonFileStateRepository(
            data_root,
            self.coordination_lock,
            self.clock,
            self.ids,
            execution_record_store=self.records,
        )
        self.resources = FileResourceStore(
            self.layout,
            self.coordination_lock,
            self.attachment_registry,
            self.ids,
        )
        self.publication_guard = InProcessPublicationCommitGuard(
            self.coordination_lock
        )
        self.upload_guard = InProcessAttachmentUploadGuard(
            self.attachment_registry
        )
        fake_logparse_repo, fake_logparse_config = _materialize_fake_logparse_checkout(
            data_root.with_name(data_root.name + "lp")
        )
        logparse_asset, broker_factory = build_logparse_runtime(
            fake_logparse_repo,
            fake_logparse_config,
            Path(sys.executable),
        )
        self.broker_factory = _CapturingBrokerFactory(broker_factory)
        self.catalog = VersionedAssetCatalog(
            skill_dir=SKILL_DIR,
            generic_skill_name="generic-problem-locator-smoke",
            logparse_tool=logparse_asset,
            logparse_broker_factory=self.broker_factory,
            allow_test_skills=True,
        )
        environment = dict(os.environ)
        environment.update(
            {
                "S07_FAKE_LOGPARSE_RECORD": os.fspath(logparse_record),
                "S08_FAKE_AGENT_RECORD": os.fspath(agent_record),
                "S08_REVIEW_ENTERED": os.fspath(review_entered),
                "S08_REVIEW_RELEASE": os.fspath(review_release),
            }
        )
        backend = AgentBackend(
            shlex.join((sys.executable, os.fspath(FAKE_AGENT))),
            parent_environment=environment,
        )
        self.runtime = DiagnosisRuntime(
            state_repository=self.repository,
            resource_store=self.resources,
            asset_catalog=self.catalog,
            logparse_broker_factory=self.broker_factory,
            execution_records=self.records,
            clock=self.clock,
            id_generator=self.ids,
            workspace_manager=WorkspaceManager(data_root),
            backend=backend,
            backend_test_limits=BackendExecutionLimits(
                wall_time_seconds=30.0,
                stdout_stderr_bytes=1024 * 1024,
                workspace_bytes=16 * 1024 * 1024,
                poll_interval_seconds=0.01,
                termination_grace_seconds=1.0,
            ),
        )
        late_dispatcher = _LateDispatcher()
        self.application = build_application_service(
            repository=self.repository,
            resource_store=self.resources,
            publication_guard=self.publication_guard,
            upload_guard=self.upload_guard,
            execution_records=self.records,
            coordinator=DomainCoordinator(),
            projector=PureContextSnapshotProjector(),
            asset_catalog=self.catalog,
            dispatcher=late_dispatcher,
            notifier=InMemoryStateChangeNotifier(),
            clock=self.clock,
            ids=self.ids,
        )
        self.scheduler = SchedulerService(
            repository=self.repository,
            execution_records=self.records,
            job_control=self.application,
            runtime=self.runtime,
            id_generator=self.ids,
        )
        late_dispatcher.bind(self.scheduler)
        self.mcp = _RecordingMcpAdapter(
            McpAdapter(
                self.application,
                self.application,
                public_base_url="http://127.0.0.1:18080",
            )
        )
        self.http_app = create_http_app(
            command_port=self.application,
            query_port=self.application,
            state_admin=FakeStateAdmin(readiness=readiness()),
            public_base_url="http://127.0.0.1:18080",
        )

    def start(self) -> None:
        recovery = self.scheduler.start()
        assert recovery.completed is True
        assert self.scheduler.ready is True

    def wait_idle(self) -> None:
        assert self.scheduler.wait_until_idle(30.0)
        assert self.scheduler.fatal_worker_error_type is None

    def shutdown(self) -> None:
        assert self.scheduler.shutdown(10.0)


def _mcp(adapter: Any, name: str, arguments: dict[str, object]) -> Any:
    result = asyncio.run(adapter.call(name, arguments))
    assert result["ok"] is True, result
    assert result["error"] is None
    return result["data"]


def _query(adapter: McpAdapter, case_id: str) -> dict[str, Any]:
    return _mcp(
        adapter,
        "problem_locator_get_case",
        {"case_id": case_id, "wait_for_job_id": None, "wait_seconds": 0},
    )["case_view"]


def _case_failure_diagnostics(stack: _Stack, case_id: str) -> str:
    aggregate = stack.repository.read_snapshot().cases[case_id]
    execution_logs: dict[str, dict[str, str]] = {}
    for job in aggregate.jobs.values():
        job_root = stack.data_root / "jobs" / job.job_id
        job_logs: dict[str, str] = {}
        for filename in (
            "stdout.log",
            "stderr.log",
            "broker_audit.json",
            "agent_job_outcome.draft.json",
            "method-diagnosis.draft.json",
            "method-review.draft.json",
            "methods_preflight.json",
        ):
            log_path = job_root / filename
            if log_path.is_file():
                job_logs[filename] = log_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )[-4096:]
        if job_logs:
            execution_logs[job.job_id] = job_logs
    return json.dumps(
        {
            "case": aggregate.case.model_dump(mode="json"),
            "jobs": [job.model_dump(mode="json") for job in aggregate.jobs.values()],
            "outcomes": [
                outcome.model_dump(mode="json")
                for outcome in aggregate.outcomes.values()
            ],
            "execution_logs": execution_logs,
            "broker_open_errors": stack.broker_factory.open_errors,
            "fatal_worker_error_type": stack.scheduler.fatal_worker_error_type,
        },
        sort_keys=True,
    )


def _wait_for_review_marker(
    stack: _Stack,
    case_id: str,
    path: Path,
    timeout_seconds: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_view: dict[str, Any] | None = None
    while not path.is_file():
        last_view = _query(stack.mcp, case_id)
        if last_view["status"] in {
            CaseStatus.FAILED.value,
            CaseStatus.INTERRUPTED.value,
            CaseStatus.RESOLVED.value,
            CaseStatus.PARTIALLY_RESOLVED.value,
            CaseStatus.UNRESOLVED.value,
        }:
            aggregate = stack.repository.read_snapshot().cases[case_id]
            source_outcome_id = (
                None
                if aggregate.case.failure is None
                else aggregate.case.failure.source_outcome_id
            )
            source_outcome = (
                None
                if source_outcome_id is None
                else aggregate.outcomes.get(source_outcome_id)
            )
            interrupted = [
                job
                for job in aggregate.jobs.values()
                if job.status is JobStatus.INTERRUPTED
            ]
            execution_logs: dict[str, str] = {}
            if interrupted:
                job_root = stack.data_root / "jobs" / interrupted[-1].job_id
                for filename in (
                    "stdout.log",
                    "stderr.log",
                    "broker_audit.json",
                    "agent_job_outcome.draft.json",
                    "method-diagnosis.draft.json",
                    "method-review.draft.json",
                    "methods_preflight.json",
                ):
                    log_path = job_root / filename
                    if log_path.is_file():
                        execution_logs[filename] = log_path.read_text(
                            encoding="utf-8",
                            errors="replace",
                        )[-4096:]
            raise AssertionError(
                json.dumps(
                    {
                        "case_view": last_view,
                        "source_outcome": (
                            None
                            if source_outcome is None
                            else source_outcome.model_dump(mode="json")
                        ),
                        "jobs": [
                            {
                                "job_id": job.job_id,
                                "job_type": job.job_type.value,
                                "status": job.status.value,
                            }
                            for job in aggregate.jobs.values()
                        ],
                        "fatal_worker_error_type": (
                            stack.scheduler.fatal_worker_error_type
                        ),
                        "interrupted_outcomes": [
                            outcome.model_dump(mode="json")
                            for outcome in aggregate.outcomes.values()
                            if interrupted
                            and outcome.job_id == interrupted[-1].job_id
                        ],
                        "execution_logs": execution_logs,
                        "broker_open_errors": stack.broker_factory.open_errors,
                        "stage_tree_failures": getattr(
                            stack,
                            "stage_tree_failures",
                            [],
                        ),
                    },
                    sort_keys=True,
                )
            )
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"timed out waiting for {path.name}: "
                f"{json.dumps(last_view, sort_keys=True)}"
            )
        time.sleep(0.05)


def _record(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _agent_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _canonical_fixture(path: Path) -> object:
    payload = path.read_bytes()
    parsed = parse_canonical_json_bytes(payload)
    assert canonical_json_bytes(parsed) == payload
    return parsed


def _assert_logparse_record(
    path: Path,
    *,
    expected_target_count: int,
) -> dict[str, Any]:
    record = _record(path)
    expected_counter = _canonical_fixture(EXPECTED_PARSE_COUNTER)
    assert isinstance(expected_counter, dict)
    assert record["parse_count"] == expected_counter["parse_count"] == 1
    assert record["target_logs_count"] == expected_target_count
    assert [item["command"] for item in record["invocations"]] == [
        "parse",
        *(["mech-target-logs"] * expected_target_count),
    ]
    assert all(
        item["reserved_environment_present"] is False
        for item in record["invocations"]
    )
    return record


def test_rpc_timeout_fixture_manifest_is_schema_valid_and_exhaustive() -> None:
    manifest_path = FIXTURES / "fixture-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = FixtureManifest.model_validate_json(manifest_bytes)
    assert canonical_json_bytes(manifest) == manifest_bytes
    assert manifest.owner_spec == "S08"
    assert manifest.root == "tests/fixtures/rpc_timeout"
    actual = sorted(
        path.relative_to(FIXTURES).as_posix()
        for path in FIXTURES.rglob("*")
        if path.is_file() and path != manifest_path
    )
    assert [item.path for item in manifest.files] == actual
    for item in manifest.files:
        payload = (FIXTURES / item.path).read_bytes()
        assert item.size == len(payload)
        assert item.sha256 == hashlib.sha256(payload).hexdigest()
        if item.schema_ref is not None:
            assert (ROOT / item.schema_ref).is_file()
        if item.path.endswith(".json"):
            assert canonical_json_bytes(parse_canonical_json_bytes(payload)) == payload


def test_cross_project_result_experience_baseline_is_self_contained() -> None:
    baseline = _cross_project_result_experience()
    assert "no synthetic archive" in baseline["provenance_note"]
    sources = baseline["sources"]
    assert {source["repository"] for source in sources} == {
        "issue-locator",
        "problem-locator-mcp",
    }
    assert {
        source["repository_commit"] for source in sources
    } == {
        "8994c254f37be93d7d605cea73137af6058992d6",
        "994e479976273a989f3716c850e372752fb4b764",
    }
    for source in sources:
        assert source["tracked_result_zip_paths"] == []
        assert source["capture_checkout"].startswith("D:/code/")
        assert source["repository_path"]
        snapshot = FIXTURES / source["snapshot_path"]
        assert snapshot.is_relative_to(FIXTURES)
        payload = snapshot.read_bytes()
        assert len(payload) == source["size"]
        assert hashlib.sha256(payload).hexdigest() == source["sha256"]
        text = payload.decode("utf-8")
        assert all(phrase in text for phrase in source["required_phrases"])

    archive = baseline["archive_expectations"]
    assert archive["flat_entries_only"] is True
    assert archive["all_resolved_plan_targets_required"] is True
    assert archive["target_bytes_must_equal_logparse_sources"] is True
    assert archive["legacy_semantic_log_name_patterns"] == [
        "<label>__<module_name>__slot_<slot>__<process_name>[-<pid>].log",
        "<label>__<module_name>__slot_<slot>__cpu_<cpu_id>__<process_name>[-<pid>].log",
    ]


def test_rpc_timeout_methods_v2_is_one_durable_same_job_path(
    tmp_path: Path,
    monkeypatch,
    request,
) -> None:
    data_root, logparse_checkout = _journey_storage_roots(tmp_path, "v2")
    _remove_test_data_root(data_root)
    _remove_test_data_root(logparse_checkout)
    request.addfinalizer(lambda: _remove_test_data_root(data_root))
    request.addfinalizer(lambda: _remove_test_data_root(logparse_checkout))
    logparse_record = tmp_path / "logparse-invocations.json"
    agent_record = tmp_path / "agent-sessions.jsonl"
    review_entered = tmp_path / "review-entered"
    review_release = tmp_path / "review-release"
    for name, value in RAW_LOGPARSE_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("S07_FAKE_LOGPARSE_RECORD", os.fspath(logparse_record))
    stack = _Stack(
        data_root,
        logparse_record=logparse_record,
        agent_record=agent_record,
        review_entered=review_entered,
        review_release=review_release,
        seed="s08-rpc-timeout-v2-first-process",
    )
    archive = ARCHIVE.read_bytes()
    archive_sha256 = hashlib.sha256(archive).hexdigest()

    created = _mcp(
        stack.mcp,
        "problem_locator_create_case",
        {
            "request_id": "s08-v2-create",
            "raw_problem_text": "A payment service call to inventory times out.",
            "statement": "A payment service call to inventory times out.",
            "expected_behavior": "The payment request completes.",
            "actual_behavior": "The payment request times out.",
            "scope": "payment-to-inventory RPC",
            "goals": ["Locate the timeout cause."],
            "non_goals": [],
            "constraints": [],
            "completion_criteria": ["Identify the timed-out request."],
            "initial_user_fact_names": [],
            "initial_user_fact_values": [],
            "wait_seconds": 0,
        },
    )
    case_id = created["business_receipt"]["case_id"]
    assert created["case_view"]["active_job"]["job_type"] == JobType.ROUTE.value

    stack.start()
    stack.wait_idle()
    waiting = _query(stack.mcp, case_id)
    assert waiting["status"] == CaseStatus.WAITING_INPUT.value
    assert [
        item["name"]
        for item in waiting["pending_requirements"]
        if item["status"] == RequirementStatus.OPEN.value
    ] == [*PARAMETER_GROUP_A, "log_archive"]

    partial = _mcp(
        stack.mcp,
        "problem_locator_submit_supplement",
        {
            "request_id": "s08-v2-partial",
            "case_id": case_id,
            "expected_case_revision": waiting["case_revision"],
            "input_names": ["problem_time", "client_slot"],
            "input_values": [
                PARAMETER_GROUP_A["problem_time"],
                PARAMETER_GROUP_A["client_slot"],
            ],
            "attachment_ids": [],
            "wait_seconds": 0,
        },
    )
    assert partial["business_receipt"]["job_id"] is None
    assert partial["case_view"]["status"] == CaseStatus.WAITING_INPUT.value

    completed = _mcp(
        stack.mcp,
        "problem_locator_submit_supplement",
        {
            "request_id": "s08-v2-complete",
            "case_id": case_id,
            "expected_case_revision": partial["case_view"]["case_revision"],
            "input_names": list(PARAMETER_GROUP_A)[2:],
            "input_values": list(PARAMETER_GROUP_A.values())[2:],
            "attachment_ids": [],
            "wait_seconds": 0,
        },
    )
    assert completed["business_receipt"]["job_id"] is None
    waiting_attachment = _query(stack.mcp, case_id)
    assert waiting_attachment["status"] == CaseStatus.WAITING_ATTACHMENT.value

    prepared = _mcp(
        stack.mcp,
        "problem_locator_prepare_attachment",
        {
            "request_id": "s08-v2-prepare",
            "case_id": case_id,
            "expected_case_revision": waiting_attachment["case_revision"],
            "name": "payment-inventory-rpc.zip",
            "content_type": "application/zip",
            "declared_size": len(archive),
            "declared_sha256": archive_sha256,
        },
    )
    attachment_id = prepared["upload"]["attachment_id"]
    with TestClient(stack.http_app) as http:
        upload = http.put(
            f"/api/v1/attachments/{attachment_id}/content",
            content=archive,
            headers={
                name: value
                for name, value in prepared["upload"]["required_headers"].items()
                if value is not None
            },
        )
    assert upload.status_code == 200, upload.text
    upload_data = upload.json()["data"]
    assert upload_data["status"] == AttachmentStatus.READY.value
    uploaded_attachment = stack.repository.read_snapshot().cases[case_id].attachments[
        attachment_id
    ]
    assert uploaded_attachment.storage_key is not None
    formal_attachment = data_root / uploaded_attachment.storage_key
    formal_attachment_mode = stat.S_IMODE(formal_attachment.stat().st_mode)
    assert formal_attachment_mode & 0o222 == 0

    submitted = _mcp(
        stack.mcp,
        "problem_locator_submit_supplement",
        {
            "request_id": "s08-v2-submit-attachment",
            "case_id": case_id,
            "expected_case_revision": upload_data["case_revision"],
            "input_names": [],
            "input_values": [],
            "attachment_ids": [attachment_id],
            "wait_seconds": 0,
        },
    )
    specialist_job_id = submitted["business_receipt"]["job_id"]
    assert specialist_job_id is not None
    _wait_for_review_marker(stack, case_id, review_entered)

    reviewing = stack.repository.read_snapshot().cases[case_id]
    assert reviewing.case.status is CaseStatus.REVIEWING
    review_job_id = reviewing.case.active_job_id
    assert review_job_id is not None
    review_job = reviewing.jobs[review_job_id]
    assert review_job.job_type is JobType.REVIEW
    assert review_job.methods_review_target is not None
    assert review_job.methods_review_target.source_job_id == specialist_job_id
    specialist_outcome = next(
        item
        for item in reviewing.outcomes.values()
        if item.job_id == specialist_job_id
    )
    assert specialist_outcome.methods_review_target == review_job.methods_review_target
    assert specialist_outcome.methods_terminal_projection is None
    graph = read_method_evidence_graph_v2(stack.records, job_id=specialist_job_id)
    plan = read_method_evaluation_plan_v2(stack.records, job_id=specialist_job_id)
    state = read_method_state_v2(stack.records, job_id=specialist_job_id)
    assert graph is not None and graph.hits
    assert plan is not None and plan.evaluations
    assert state is not None and state.status == "REVIEWER_PENDING"
    assert tuple(item.evaluation_ref for item in plan.evaluations) == (
        state.evaluation_refs
    )
    assert stat.S_IMODE(formal_attachment.stat().st_mode) == formal_attachment_mode
    assert formal_attachment.read_bytes() == archive

    specialist_workspace = data_root / "tmp" / "workspaces" / specialist_job_id
    reviewer_workspace = data_root / "tmp" / "workspaces" / review_job_id
    assert specialist_workspace != reviewer_workspace
    for workspace in (specialist_workspace, reviewer_workspace):
        inputs = workspace / "inputs"
        assert (inputs / "request.json").is_file()
        assert (inputs / "method-evidence-graph.json").is_file()
        assert (inputs / "method-evaluation-plan.json").is_file()
        assert not (inputs / "target_logs.json").exists()
        assert not (inputs / "logparse-receipt.json").exists()
        assert not (inputs / "target-logs").exists()
    assert not (reviewer_workspace / "inputs" / "method-diagnosis.json").exists()

    specialist_draft = parse_canonical_json_bytes(
        (specialist_workspace / "output" / "method-diagnosis.draft.json").read_bytes()
    )
    assert isinstance(specialist_draft, list)
    assert [item["evaluation_ref"] for item in specialist_draft] == [
        item.evaluation_ref for item in plan.evaluations
    ]
    assert all(set(item) == {"evaluation_ref", "verdict", "reason"} for item in specialist_draft)
    assert all(item["verdict"] == "CONFIRMED" for item in specialist_draft)
    _assert_logparse_record(logparse_record, expected_target_count=2)

    entered_records = _agent_records(agent_record)
    assert [item["phase"] for item in entered_records] == [
        "ROUTE",
        "LOGPARSE_PREPROCESS",
        "METHODS_DIAGNOSE",
        "METHODS_REVIEW",
    ]
    assert entered_records[-2]["job_id"] == specialist_job_id
    assert entered_records[-1]["job_id"] == review_job_id

    review_release.write_text("release\n", encoding="utf-8")
    stack.wait_idle()
    resolved = _query(stack.mcp, case_id)
    assert resolved["status"] == CaseStatus.RESOLVED.value, _case_failure_diagnostics(
        stack,
        case_id,
    )
    methods_result = resolved["methods_result"]
    assert methods_result["status"] == "RESOLVED"
    assert methods_result["confirmed_method_ids"] == ["rpc-call-timeout"]
    assert methods_result["confirmed_event_refs"]
    assert methods_result["confirmed_hit_refs"]
    assert resolved["final_result"] is None
    assert resolved["unresolved_result"] is None
    assert resolved["artifacts"] == []
    terminal = stack.repository.read_snapshot().cases[case_id]
    assert terminal.artifacts == {}
    reviewer_outcome = next(
        item for item in terminal.outcomes.values() if item.job_id == review_job_id
    )
    assert reviewer_outcome.methods_reviewer_result is not None
    assert reviewer_outcome.methods_terminal_projection == terminal.case.methods_result

    reviewer_draft = parse_canonical_json_bytes(
        (reviewer_workspace / "output" / "method-review.draft.json").read_bytes()
    )
    assert isinstance(reviewer_draft, list)
    assert [item["evaluation_ref"] for item in reviewer_draft] == [
        item.evaluation_ref for item in plan.evaluations
    ]
    assert all(set(item) == {"evaluation_ref", "verdict", "reason"} for item in reviewer_draft)
    assert all(item["verdict"] == "CONFIRMED" for item in reviewer_draft)

    stack.shutdown()
    restarted = _Stack(
        data_root,
        logparse_record=logparse_record,
        agent_record=agent_record,
        review_entered=review_entered,
        review_release=review_release,
        seed="s08-rpc-timeout-v2-restarted-process",
    )
    restarted.start()
    restarted.wait_idle()
    assert _query(restarted.mcp, case_id) == resolved
    assert stat.S_IMODE(formal_attachment.stat().st_mode) == formal_attachment_mode
    assert _record(logparse_record)["parse_count"] == 1
    assert _agent_records(agent_record) == entered_records
    restarted.shutdown()
