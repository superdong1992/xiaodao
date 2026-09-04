from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Sequence

from fastapi.testclient import TestClient

from problem_locator.application import build_application_service
from problem_locator.contracts import (
    ArtifactKind,
    AttachmentStatus,
    CandidateStatus,
    CaseStatus,
    FixtureManifest,
    JobStatus,
    JobType,
    OutcomeResultType,
    RequirementStatus,
    ReviewVerdict,
    UserResultPayload,
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
from problem_locator.runtime.context_policy import RuntimeAssetResolver
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime
from problem_locator.runtime.methods_grounding import (
    MethodDiagnosisDraftV1,
    MethodReviewV1,
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
from problem_locator.storage.staging import StagedObjectWriter
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
EXPECTED_PARSE_MANIFEST = FIXTURES / "expected-parse-manifest.json"
EXPECTED_TARGET_LOGS = FIXTURES / "expected-target-logs.json"
EXPECTED_PARSE_COUNTER = FIXTURES / "expected-parse-counter.json"
CROSS_PROJECT_EXPERIENCE = FIXTURES / "cross-project-result-experience.json"
FAKE_LOGPARSE_REPO = ROOT / "tests/fixtures/components/logparse/fake/repo"
FAKE_LOGPARSE_CONFIG = FAKE_LOGPARSE_REPO / "config.yaml"
SKILL_DIR = ROOT / "tests/fixtures/components/runtime-catalog/skill-dir"
EVIDENCE_IDS = [
    "00000000-0000-0000-0000-000000000040",
    "00000000-0000-0000-0000-000000000041",
]
ARCHIVE_BYTES_MARKER = b"synthetic payment-to-inventory RPC timeout archive"
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


def _golden_target_archive_names() -> tuple[list[dict[str, Any]], list[str]]:
    """Derive the legacy semantic names from the frozen broker response."""

    value = parse_canonical_json_bytes(EXPECTED_TARGET_LOGS.read_bytes())
    assert isinstance(value, dict)
    targets = value["target_logs"]
    assert isinstance(targets, list)
    names: list[str] = []
    for target in targets:
        assert isinstance(target, dict)
        slot = str(target["slot"])
        if slot.casefold().startswith("slot_"):
            slot = slot[5:]
        process = str(target["process_name"])
        if target.get("pid") is not None:
            process = f"{process}-{target['pid']}"
        parts = [
            str(target["label"]),
            str(target["module_name"]),
            f"slot_{slot}",
        ]
        if target.get("cpu_id") is not None:
            parts.append(f"cpu_{target['cpu_id']}")
        parts.append(process)
        names.append("__".join(parts) + ".log")
    return targets, names


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


class _E2EIds(DeterministicIdGenerator):
    """Keep every ID deterministic and pin the golden Evidence identity."""

    def derive(self, kind: str, stable_parts: Sequence[str]) -> str:
        evidence_ids_by_key = {
            "rpc-timeout-evidence": EVIDENCE_IDS[0],
            "rpc-timeout-server-evidence": EVIDENCE_IDS[1],
            "methods-target-1": EVIDENCE_IDS[0],
            "methods-target-2": EVIDENCE_IDS[1],
        }
        if kind == "evidence" and tuple(stable_parts)[-1] in evidence_ids_by_key:
            self.derive_calls.append((kind, tuple(stable_parts)))
            return evidence_ids_by_key[tuple(stable_parts)[-1]]
        return super().derive(kind, stable_parts)


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
        self.ids = _E2EIds(seed=seed)
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
            specialized_reviewer_enabled=True,
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
            specialized_reviewer_enabled=True,
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


def _wait_for_file(path: Path, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path.name}")
        time.sleep(0.02)


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
            failed_jobs = [
                job
                for job in aggregate.jobs.values()
                if job.status in {JobStatus.FAILED, JobStatus.INTERRUPTED}
            ]
            execution_logs: dict[str, str] = {}
            if failed_jobs:
                job_root = stack.data_root / "jobs" / failed_jobs[-1].job_id
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
                        "failed_outcomes": [
                            outcome.model_dump(mode="json")
                            for outcome in aggregate.outcomes.values()
                            if failed_jobs
                            and outcome.job_id == failed_jobs[-1].job_id
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


def _assert_no_sensitive_surfaces(
    *,
    data_root: Path,
    excluded_attachment: Path,
    public_responses: Sequence[bytes],
    broker_capabilities: Sequence[dict[str, str]],
    archive: bytes,
) -> None:
    needles: list[tuple[str, bytes]] = [
        ("DATA_ROOT", os.fspath(data_root).encode("utf-8")),
        ("archive marker", ARCHIVE_BYTES_MARKER),
        ("archive body", archive),
    ]
    needles.extend(
        (f"raw key {name}", name.encode("ascii")) for name in RAW_LOGPARSE_ENV
    )
    needles.extend(
        (f"raw sentinel {name}", value.encode("utf-8"))
        for name, value in RAW_LOGPARSE_ENV.items()
    )
    for capability_index, capability in enumerate(broker_capabilities):
        needles.extend(
            (f"broker capability {capability_index}/{name}", value.encode("utf-8"))
            for name, value in capability.items()
        )

    surfaces: list[tuple[str, bytes]] = [
        (f"public response {index}", payload)
        for index, payload in enumerate(public_responses)
    ]
    excluded = excluded_attachment.resolve(strict=True)
    for path in sorted(data_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.resolve(strict=True) == excluded:
            continue
        relative_path = path.relative_to(data_root)
        relative = relative_path.as_posix()
        parts = relative_path.parts
        is_execution_surface = (
            len(parts) >= 3
            and parts[0] == "jobs"
            and path.name
            in {
                "stdout.log",
                "stderr.log",
                "job_outcome.json",
                "agent_job_outcome.json",
                "method-diagnosis.draft.json",
                "method-review.draft.json",
                "method-grounding-audit.json",
                "methods_logparse_receipt.json",
                "methods_preflight.json",
                "methods_request.json",
                "methods_target_logs.json",
            }
        )
        is_published_proposal = bool(parts) and parts[0] == "resources"
        is_workspace_proposal = (
            len(parts) >= 5
            and parts[0:2] == ("tmp", "workspaces")
            and parts[3:5] == ("output", "proposals")
        )
        is_staged_proposal = len(parts) >= 2 and parts[0:2] == (
            "tmp",
            "proposals",
        )
        is_workspace_methods_output = (
            len(parts) >= 5
            and parts[0:2] == ("tmp", "workspaces")
            and parts[3] == "output"
            and path.name
            in {"method-diagnosis.draft.json", "method-review.draft.json"}
        )
        if not (
            is_execution_surface
            or is_published_proposal
            or is_workspace_proposal
            or is_staged_proposal
            or is_workspace_methods_output
        ):
            continue
        payload = path.read_bytes()
        surfaces.append((relative, payload))

    for surface, payload in surfaces:
        for label, needle in needles:
            assert needle not in payload, f"{label} leaked into {surface}"


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


def test_r01_r14_rpc_timeout_is_one_durable_cross_module_path(
    tmp_path: Path,
    monkeypatch,
    request,
) -> None:
    # Keep the staged LOGPARSE_RUN below the legacy Windows MAX_PATH boundary.
    # On POSIX, use pytest's native temporary filesystem so a Docker Desktop
    # bind mount cannot destabilize inode-based workspace safety checks.
    data_root, logparse_checkout = _journey_storage_roots(tmp_path, "r")
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
        seed="s08-rpc-timeout-first-process",
    )
    archive = ARCHIVE.read_bytes()
    archive_sha256 = hashlib.sha256(archive).hexdigest()
    assert ARCHIVE_BYTES_MARKER in archive
    with zipfile.ZipFile(ARCHIVE) as fixture_archive:
        assert fixture_archive.testzip() is None
        assert fixture_archive.namelist() == [
            "payment.log",
            "inventory.log",
            "archive-marker.txt",
        ]
        assert fixture_archive.read("payment.log") == RAW_RPC_CLIENT_LOG.encode("utf-8")
        assert fixture_archive.read("inventory.log") == RAW_RPC_SERVER_LOG.encode("utf-8")
        assert ARCHIVE_BYTES_MARKER in fixture_archive.read("archive-marker.txt")
    # R01: queue the fixed ROUTE Job while recovery still keeps claiming paused.
    created = _mcp(
        stack.mcp,
        "problem_locator_create_case",
        {
            "request_id": "s08-r01-create",
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
    route_job_id = created["business_receipt"]["job_id"]
    assert created["case_view"]["status"] == CaseStatus.RUNNING.value
    assert created["case_view"]["active_job"]["job_type"] == JobType.ROUTE.value
    assert created["case_view"]["active_job"]["job_id"] == route_job_id
    queued_route = stack.repository.read_snapshot().cases[case_id].jobs[route_job_id]
    queued_route_refs = [
        queued_route.agent_profile_ref,
        queued_route.tool_bundle_ref,
        queued_route.context_policy_ref,
        queued_route.output_contract_ref,
        *queued_route.available_skill_refs,
    ]
    queued_route_assets = stack.catalog.check(queued_route_refs)
    assert queued_route_assets.available, queued_route_assets.model_dump(mode="json")
    RuntimeAssetResolver(stack.catalog).resolve_job(queued_route)

    # R02/R03: Runtime accepts MATCHED, then requests group A and logs together.
    stack.start()
    stack.wait_idle()
    waiting_a = _query(stack.mcp, case_id)
    assert waiting_a["status"] == CaseStatus.WAITING_INPUT.value, json.dumps(
        waiting_a,
        sort_keys=True,
    )
    open_a = [
        item
        for item in waiting_a["pending_requirements"]
        if item["status"] == RequirementStatus.OPEN.value
    ]
    assert [item["name"] for item in open_a] == [
        *PARAMETER_GROUP_A,
        "log_archive",
    ]
    first_snapshot = stack.repository.read_snapshot()
    aggregate = first_snapshot.cases[case_id]
    route_job = aggregate.jobs[route_job_id]
    assert route_job.status is JobStatus.SUCCEEDED
    route_outcome = next(
        item for item in aggregate.outcomes.values() if item.job_id == route_job_id
    )
    assert route_outcome.result_type is OutcomeResultType.COMPLETED
    assert route_outcome.payload.kind.value == "MATCHED"
    initial_diagnose = next(
        job
        for job in aggregate.jobs.values()
        if job.job_type is JobType.DIAGNOSE
    )
    initial_outcome = next(
        item
        for item in aggregate.outcomes.values()
        if item.job_id == initial_diagnose.job_id
    )
    assert initial_outcome.result_type is OutcomeResultType.NEED_INPUT
    assert initial_outcome.decision_audit is None
    preflight_bytes = stack.records.read_audit_bytes(
        initial_diagnose.job_id,
        "methods_preflight.json",
    )
    assert preflight_bytes is not None
    preflight = parse_canonical_json_bytes(preflight_bytes)
    assert canonical_json_bytes(preflight) == preflight_bytes
    assert preflight["job_id"] == initial_diagnose.job_id
    assert preflight["result_type"] == OutcomeResultType.NEED_INPUT
    assert [item["phase"] for item in _agent_records(agent_record)] == ["ROUTE"]
    assert not logparse_record.exists()
    initial_requirements = {
        item.requirement_id: item
        for item in initial_outcome.payload.state_delta.add_pending_requirements
    }
    assert [
        initial_requirements[item].name
        for item in initial_outcome.payload.requested_input
    ] == list(PARAMETER_GROUP_A)
    assert [
        initial_requirements[item].name
        for item in initial_outcome.payload.requested_attachments
    ] == ["log_archive"]

    # R04: a strict partial supplement persists facts but creates no Job.
    r04_revision = waiting_a["case_revision"]
    partial = _mcp(
        stack.mcp,
        "problem_locator_submit_supplement",
        {
            "request_id": "s08-r04-partial-a",
            "case_id": case_id,
            "expected_case_revision": r04_revision,
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
    assert {
        item["provenance"]["input_name"] for item in partial["case_view"]["user_facts"]
    } == {"problem_time", "client_slot"}
    job_count_after_partial = len(stack.repository.read_snapshot().cases[case_id].jobs)

    # R05/R06: completing group A moves directly to the existing attachment wait.
    completed_a = _mcp(
        stack.mcp,
        "problem_locator_submit_supplement",
        {
            "request_id": "s08-r05-complete-a",
            "case_id": case_id,
            "expected_case_revision": partial["case_view"]["case_revision"],
            "input_names": list(PARAMETER_GROUP_A)[2:],
            "input_values": list(PARAMETER_GROUP_A.values())[2:],
            "attachment_ids": [],
            "wait_seconds": 0,
        },
    )
    assert completed_a["business_receipt"]["job_id"] is None
    waiting_attachment = _query(stack.mcp, case_id)
    assert waiting_attachment["status"] == CaseStatus.WAITING_ATTACHMENT.value, json.dumps(
        waiting_attachment,
        sort_keys=True,
    )
    assert len(stack.repository.read_snapshot().cases[case_id].jobs) == (
        job_count_after_partial
    )
    open_attachment = [
        item
        for item in waiting_attachment["pending_requirements"]
        if item["status"] == RequirementStatus.OPEN.value
    ]
    assert [(item["name"], item["kind"]) for item in open_attachment] == [
        ("log_archive", "ATTACHMENT")
    ]

    # R07: prepare through MCP and upload through the real streaming HTTP route.
    prepared = _mcp(
        stack.mcp,
        "problem_locator_prepare_attachment",
        {
            "request_id": "s08-r07-prepare-log",
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
    http_responses = [
        upload.content,
        canonical_json_bytes(dict(upload.headers)),
    ]
    upload_data = upload.json()["data"]
    assert upload_data["status"] == AttachmentStatus.READY.value
    ready_snapshot = stack.repository.read_snapshot()
    ready_aggregate = ready_snapshot.cases[case_id]
    ready_attachment = ready_aggregate.attachments[attachment_id]
    assert ready_attachment.status is AttachmentStatus.READY
    assert ready_attachment.size == len(archive)
    assert ready_attachment.sha256 == archive_sha256
    assert ready_aggregate.case.status is CaseStatus.WAITING_ATTACHMENT
    assert ready_aggregate.case.active_job_id is None

    # R08/R09/R10: explicit reference dispatches one two-pass Methods Job;
    # product-owned Logparse executes exactly once before the Methods pass.
    submitted_attachment = _mcp(
        stack.mcp,
        "problem_locator_submit_supplement",
        {
            "request_id": "s08-r08-submit-log",
            "case_id": case_id,
            "expected_case_revision": upload_data["case_revision"],
            "input_names": [],
            "input_values": [],
            "attachment_ids": [attachment_id],
            "wait_seconds": 0,
        },
    )
    assert submitted_attachment["business_receipt"]["job_id"] is not None
    candidate_job_id = submitted_attachment["business_receipt"]["job_id"]
    _wait_for_review_marker(stack, case_id, review_entered)
    reviewing_snapshot = stack.repository.read_snapshot()
    reviewing = reviewing_snapshot.cases[case_id]
    candidate_job = reviewing.jobs[candidate_job_id]
    assert candidate_job.attachment_refs == [attachment_id]
    assert candidate_job.evidence_refs == []
    assert candidate_job.artifact_refs == []
    assert reviewing.case.status is CaseStatus.REVIEWING
    _assert_logparse_record(
        logparse_record,
        expected_target_count=2,
    )
    logparse_runs = [
        item
        for item in reviewing.artifacts.values()
        if item.kind is ArtifactKind.LOGPARSE_RUN
    ]
    assert len(logparse_runs) == 1
    logparse_run = logparse_runs[0]
    assert logparse_run.created_by_job_id == candidate_job_id
    assert list(reviewing.evidence) == EVIDENCE_IDS
    assert all(
        reviewing.evidence[evidence_id].source_type.value == "LOGPARSE"
        and reviewing.evidence[evidence_id].source_ref == logparse_run.artifact_id
        for evidence_id in EVIDENCE_IDS
    )
    candidate_outcome = next(
        outcome
        for outcome in reviewing.outcomes.values()
        if outcome.job_id == candidate_job_id
    )
    assert candidate_outcome.result_type is OutcomeResultType.COMPLETED
    assert candidate_outcome.decision_audit is not None

    # R11/R12: the same immutable Job freezes the Pass-A receipt/target bytes,
    # emits only the Methods draft, and publishes the server-mapped Candidate.
    candidate = reviewing.case.diagnosis_state.candidate_conclusion
    assert candidate is not None
    assert candidate.status is CandidateStatus.REVIEWING
    assert candidate.proposed_by_job_id == candidate_job_id
    assert candidate.supporting_evidence_refs == EVIDENCE_IDS
    assert candidate.completion_criteria_mapping[0].evidence_refs == EVIDENCE_IDS
    preprocessing_target_result = (
        data_root
        / "tmp"
        / "workspaces"
        / f"{candidate_job_id}.logparse-preprocess"
        / "output"
        / "proposals"
        / "methods-preprocess"
        / "target_logs.json"
    ).read_bytes()
    preprocessing_target_payload = parse_canonical_json_bytes(
        preprocessing_target_result
    )
    assert isinstance(preprocessing_target_payload, dict)
    assert set(preprocessing_target_payload) == {
        "api_version",
        "logparse_run_artifact_draft",
        "schema_version",
        "target_logs",
    }
    preprocessing_projection = {
        name: preprocessing_target_payload[name]
        for name in ("api_version", "schema_version", "target_logs")
    }
    assert canonical_json_bytes(preprocessing_projection) == (
        EXPECTED_TARGET_LOGS.read_bytes()
    )
    frozen_target_path = (
        data_root
        / "tmp"
        / "workspaces"
        / candidate_job_id
        / "inputs"
        / "target_logs.json"
    )
    frozen_target_bytes = frozen_target_path.read_bytes()
    frozen_targets = parse_canonical_json_bytes(frozen_target_bytes)
    assert canonical_json_bytes(frozen_targets) == frozen_target_bytes
    assert [item["source_id"] for item in frozen_targets["target_logs"]] == [
        "client",
        "server",
    ]
    receipt_path = frozen_target_path.with_name("logparse-receipt.json")
    receipt_bytes = receipt_path.read_bytes()
    receipt = parse_canonical_json_bytes(receipt_bytes)
    assert canonical_json_bytes(receipt) == receipt_bytes
    assert receipt["job_id"] == candidate_job_id
    assert receipt["operation"] == "parse-targets"
    assert receipt["target_logs"] == frozen_targets["target_logs"]
    user_results = [
        item for item in reviewing.artifacts.values() if item.kind is ArtifactKind.USER_RESULT
    ]
    assert len(user_results) == 1
    user_result = user_results[0]
    assert user_result.created_by_job_id == candidate_job_id
    assert user_result.metadata.schema_version == 3
    assert user_result.metadata.format_id == "problem-locator-diagnosis-v3"
    result_archives = [
        item
        for item in reviewing.artifacts.values()
        if item.kind is ArtifactKind.USER_RESULT_ARCHIVE
    ]
    assert len(result_archives) == 1
    result_archive = result_archives[0]
    assert result_archive.created_by_job_id == candidate_job_id
    assert result_archive.metadata.schema_version == 3
    assert result_archive.metadata.format_id == "problem-locator-result-archive-v3"
    assert result_archive.metadata.user_result_proposal_key == "server-user-result"
    assert result_archive.metadata.target_log_count == 2
    assert {
        proposal.proposal_key: proposal.artifact_kind
        for proposal in candidate_outcome.proposed_artifacts
        if proposal.artifact_kind
        in {ArtifactKind.USER_RESULT, ArtifactKind.USER_RESULT_ARCHIVE}
    } == {
        "server-user-result": ArtifactKind.USER_RESULT,
        "server-user-result-archive": ArtifactKind.USER_RESULT_ARCHIVE,
    }
    methods_draft_path = (
        data_root
        / "tmp"
        / "workspaces"
        / candidate_job_id
        / "output"
        / "method-diagnosis.draft.json"
    )
    methods_draft_bytes = methods_draft_path.read_bytes()
    methods_draft = MethodDiagnosisDraftV1.from_mapping(
        parse_canonical_json_bytes(methods_draft_bytes)
    )
    assert canonical_json_bytes(parse_canonical_json_bytes(methods_draft_bytes)) == (
        methods_draft_bytes
    )
    assert methods_draft.status == "CONFIRMED"
    assert methods_draft.confirmed_methods == ("rpc-call-timeout",)
    assert methods_draft.candidate_methods == ()
    assert len(methods_draft.evidence) == 1
    assert methods_draft.evidence[0].identity_tokens == (
        "order_id=synthetic-order-0001",
    )
    assert not (
        methods_draft_path.parent / "job_outcome.draft.json"
    ).exists()
    candidate_processing = reviewing.outcome_processing_records[
        candidate_outcome.outcome_id
    ]
    assert candidate_processing.job_id == candidate_job_id
    assert candidate_processing.accepted_artifact_ids == sorted(
        [
            logparse_run.artifact_id,
            user_result.artifact_id,
            result_archive.artifact_id,
        ]
    )
    assert candidate_processing.accepted_evidence_ids == EVIDENCE_IDS
    assert candidate_processing.created_job_id == reviewing.case.active_job_id
    _assert_logparse_record(
        logparse_record,
        expected_target_count=2,
    )

    # R13: capture the independent RUNNING review session, then release PASS.
    review_job_id = review_entered.read_text(encoding="utf-8")
    review_job = reviewing.jobs[review_job_id]
    assert review_job.job_type is JobType.REVIEW
    assert review_job.status is JobStatus.RUNNING
    assert review_job.review_target is not None
    assert review_job.review_target.candidate_conclusion_id == candidate.conclusion_id
    assert review_job.review_target.candidate_revision == candidate.revision
    assert review_job.review_target.candidate_content_hash == candidate.content_hash
    assert review_job.evidence_refs == EVIDENCE_IDS
    assert set(review_job.evidence_refs) == {
        *candidate.supporting_evidence_refs,
        *(
            evidence_ref
            for mapping in candidate.completion_criteria_mapping
            for evidence_ref in mapping.evidence_refs
        ),
    }
    review_release.write_text("pass\n", encoding="utf-8")
    stack.wait_idle()
    resolved_view = _query(stack.mcp, case_id)
    assert resolved_view["status"] == CaseStatus.RESOLVED.value
    assert resolved_view["final_result"]["status"] == CandidateStatus.ACCEPTED.value
    resolved_snapshot = stack.repository.read_snapshot()
    resolved = resolved_snapshot.cases[case_id]
    review_outcome = next(
        outcome
        for outcome in resolved.outcomes.values()
        if outcome.job_id == review_job_id
    )
    assert review_outcome.payload.verdict is ReviewVerdict.PASS
    assert review_outcome.payload.unsupported_findings == []
    assert review_outcome.payload.evidence_conflicts == []
    assert review_outcome.payload.missing_evidence == []
    assert review_outcome.payload.stale_references == []
    review_draft_path = (
        data_root
        / "tmp"
        / "workspaces"
        / review_job_id
        / "output"
        / "method-review.draft.json"
    )
    review_draft_bytes = review_draft_path.read_bytes()
    review_draft = MethodReviewV1.from_mapping(
        parse_canonical_json_bytes(review_draft_bytes)
    )
    assert canonical_json_bytes(parse_canonical_json_bytes(review_draft_bytes)) == (
        review_draft_bytes
    )
    assert review_draft.verdict == "PASS"
    assert [item.identity_tokens for item in review_draft.findings] == [
        ("order_id=synthetic-order-0001",)
    ]
    assert not (review_draft_path.parent / "job_outcome.draft.json").exists()
    sessions = _agent_records(agent_record)
    assert [item["job_type"] for item in sessions] == [
        "ROUTE",
        "DIAGNOSE",
        "DIAGNOSE",
        "REVIEW",
    ]
    assert [item["phase"] for item in sessions] == [
        "ROUTE",
        "LOGPARSE_PREPROCESS",
        "METHODS_DIAGNOSE",
        "METHODS_REVIEW",
    ]
    assert len({item["pid"] for item in sessions}) == len(sessions)

    # R14: restart every file adapter/service, query, and stream the exact bytes.
    stack.shutdown()
    restarted = _Stack(
        data_root,
        logparse_record=logparse_record,
        agent_record=agent_record,
        review_entered=review_entered,
        review_release=review_release,
        seed="s08-rpc-timeout-restarted-process",
    )
    restarted.start()
    restarted.wait_idle()
    restarted_view = _query(restarted.mcp, case_id)
    assert restarted_view == resolved_view
    restarted_aggregate = restarted.repository.read_snapshot().cases[case_id]
    for field_name in (
        "jobs",
        "outcomes",
        "outcome_processing_records",
        "execution_failure_records",
        "evidence",
        "attachments",
        "artifacts",
    ):
        assert getattr(restarted_aggregate, field_name) == getattr(resolved, field_name)
    listed = _mcp(
        restarted.mcp,
        "problem_locator_list_artifacts",
        {"case_id": case_id},
    )
    assert [item["artifact_id"] for item in listed["artifacts"]] == sorted(
        [user_result.artifact_id, result_archive.artifact_id]
    )
    with TestClient(restarted.http_app) as http:
        download = http.get(
            f"/api/v1/artifacts/{user_result.artifact_id}/content",
            params={"case_id": case_id},
        )
        archive_download = http.get(
            f"/api/v1/artifacts/{result_archive.artifact_id}/content",
            params={"case_id": case_id},
        )
        hidden = http.get(
            f"/api/v1/artifacts/{logparse_run.artifact_id}/content",
            params={"case_id": case_id},
        )
    assert download.status_code == 200
    assert len(download.content) == user_result.size
    assert hashlib.sha256(download.content).hexdigest() == user_result.sha256
    assert download.headers["content-length"] == str(user_result.size)
    assert download.headers["x-content-sha256"] == user_result.sha256
    assert archive_download.status_code == 200
    assert hashlib.sha256(archive_download.content).hexdigest() == result_archive.sha256
    experience = _cross_project_result_experience()
    payload = UserResultPayload.model_validate_json(download.content)
    assert canonical_json_bytes(payload) == download.content
    assert payload.schema_version == 3
    assert (
        experience["result_json_expectations"]["format_id"]
        == "problem-locator-diagnosis-v2"
    )
    assert payload.format_id == "problem-locator-diagnosis-v3"
    assert set(experience["result_json_expectations"]["required_fields"]) <= set(
        payload.model_dump(mode="json")
    )
    assert payload.status == "COMPLETED"
    assert payload.source_job_type is JobType.DIAGNOSE
    assert payload.problem_statement == restarted_view["problem_spec"]["statement"]
    assert payload.root_cause == restarted_view["final_result"]["statement"]
    assert len(payload.findings) == 1
    assert payload.findings[0].evidence_bindings
    assert payload.findings[0].citations
    assert [item.factor_id for item in payload.causal_factors] == [
        "rpc_call_timeout"
    ]
    assert len(payload.verification_rules) == 1
    assert payload.verification_rules[0].rule_id.startswith(
        "methods:rpc-call-timeout:"
    )
    assert all(not item.issues for item in payload.verification_rules)
    assert payload.time_relevance.problem_time == PARAMETER_GROUP_A["problem_time"]
    assert payload.time_relevance.assessment == "UNKNOWN"
    assert payload.time_relevance.observations == []
    assert payload.evidence_gaps == []
    assert payload.limitations == []
    assert payload.recommendations == [
        "请根据已确认的定位方法处理对应异常；实施变更前先核对安全说明，修复后按完成条件复验。"
    ]
    golden_targets, golden_target_names = _golden_target_archive_names()
    archive_expectations = experience["archive_expectations"]
    assert golden_target_names == archive_expectations["entry_order"][2:]
    with zipfile.ZipFile(io.BytesIO(archive_download.content)) as result_zip:
        names = result_zip.namelist()
        assert names == archive_expectations["entry_order"]
        assert archive_expectations["flat_entries_only"] is True
        assert all("/" not in name and "\\" not in name for name in names)
        archive_manifest_bytes = result_zip.read("archive-manifest.json")
        archive_manifest = parse_canonical_json_bytes(archive_manifest_bytes)
        assert canonical_json_bytes(archive_manifest) == archive_manifest_bytes
        assert archive_manifest["schema_version"] == 3
        assert archive_manifest["format_id"] == "problem-locator-result-archive-v3"
        assert archive_manifest["problem_time"] == PARAMETER_GROUP_A["problem_time"]
        assert archive_manifest["diagnosis_result_sha256"] == user_result.sha256
        assert archive_manifest["target_log_count"] == len(golden_targets)
        assert [
            item["archive_name"] for item in archive_manifest["target_logs"]
        ] == result_zip.namelist()[2:]
        assert [
            (
                item["label"],
                item["module_name"],
                item["slot"],
                item["process_name"],
                item["pid"],
            )
            for item in archive_manifest["target_logs"]
        ] == [
            (
                item["label"],
                item["module_name"],
                item["slot"],
                item["process_name"],
                item.get("pid"),
            )
            for item in golden_targets
        ]
        result_text_bytes = result_zip.read("result.txt")
        assert archive_manifest["result_txt_sha256"] == hashlib.sha256(
            result_text_bytes
        ).hexdigest()
        result_text = result_text_bytes.decode("utf-8")
        report_expectations = experience["report_expectations"]
        sections = report_expectations["section_order"]
        section_offsets = [
            result_text.index(section) for section in sections
        ]
        assert section_offsets == sorted(section_offsets)
        section_ends = [*section_offsets[1:], len(result_text)]
        assert all(
            result_text[offset + len(section) : end].strip()
            for section, offset, end in zip(
                sections,
                section_offsets,
                section_ends,
                strict=True,
            )
        )
        assert report_expectations["required_information"] == [
            "problem_statement",
            "root_cause",
            "verified_evidence_with_original_log_lines",
            "completion_criteria_mapping",
            "time_relevance",
            "evidence_gaps_and_limitations",
            "recommendations",
            "target_log_inventory",
        ]
        assert payload.root_cause in result_text
        assert payload.problem_statement in result_text
        assert all(
            rule.rule_id in result_text and rule.explanation in result_text
            for rule in payload.verification_rules
        )
        citations = [
            citation
            for rule in payload.verification_rules
            for citation in rule.citations
        ]
        assert citations
        assert all(
            citation.archive_name in names
            and citation.excerpt
            and citation.excerpt in result_text
            for citation in citations
        )
        assert all(
            item.criterion in result_text and item.explanation in result_text
            for item in payload.completion_criteria_mapping
        )
        assert payload.time_relevance.problem_time in result_text
        assert payload.time_relevance.assessment in result_text
        assert payload.time_relevance.explanation in result_text
        assert all(
            observation.rule_id in result_text
            and observation.event_time in result_text
            and f"{observation.offset_ms} ms" in result_text
            for observation in payload.time_relevance.observations
        )
        assert "证据缺口：" in result_text
        assert "限制：" in result_text
        assert all(item in result_text for item in payload.recommendations)
        assert all(
            item["archive_name"] in result_text
            for item in archive_manifest["target_logs"]
        )
        client_log = result_zip.read(result_zip.namelist()[2])
        server_log = result_zip.read(result_zip.namelist()[3])
        assert client_log == RPC_CLIENT_LOG.encode("utf-8")
        assert server_log == RPC_SERVER_LOG.encode("utf-8")
        assert b"synthetic-order-0001" in client_log
        assert b"synthetic-order-0001" in server_log
    expected_proposal_keys = ["methods-target-1", "methods-target-2"]
    assert all(
        item.existing_evidence_id is None
        for item in payload.supporting_evidence_bindings
    )
    assert [
        item.evidence_proposal_key for item in payload.supporting_evidence_bindings
    ] == expected_proposal_keys
    assert all(
        binding.existing_evidence_id is None
        for item in payload.completion_criteria_mapping
        for binding in item.evidence_bindings
    )
    assert [
        [binding.evidence_proposal_key for binding in item.evidence_bindings]
        for item in payload.completion_criteria_mapping
    ] == [expected_proposal_keys]
    assert restarted_view["final_result"]["supporting_evidence_refs"] == EVIDENCE_IDS
    assert [
        item["evidence_refs"]
        for item in restarted_view["final_result"]["completion_criteria_mapping"]
    ] == [EVIDENCE_IDS]
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"
    _assert_logparse_record(logparse_record, expected_target_count=2)
    assert len(_agent_records(agent_record)) == 4
    restarted.shutdown()
    http_responses.extend(
        [
            download.content,
            canonical_json_bytes(dict(download.headers)),
            hidden.content,
            canonical_json_bytes(dict(hidden.headers)),
        ]
    )
    capabilities = stack.broker_factory.capabilities
    assert len(capabilities) == 1
    assert len(
        {
            item["PROBLEM_LOCATOR_LOGPARSE_ENDPOINT"]
            for item in capabilities
        }
    ) == 1
    assert len(
        {
            item["PROBLEM_LOCATOR_LOGPARSE_TOKEN"]
            for item in capabilities
        }
    ) == 1
    assert len(restarted.broker_factory.capabilities) == 0
    attachment_path = data_root / ready_attachment.storage_key
    assert attachment_path.read_bytes() == archive
    _assert_no_sensitive_surfaces(
        data_root=data_root,
        excluded_attachment=attachment_path,
        public_responses=[
            *stack.mcp.responses,
            *restarted.mcp.responses,
            *http_responses,
        ],
        broker_capabilities=[
            *stack.broker_factory.capabilities,
            *restarted.broker_factory.capabilities,
        ],
        archive=archive,
    )


def test_same_job_uses_initial_order_fact_and_survives_restart(
    tmp_path: Path,
    monkeypatch,
    request,
) -> None:
    """Prove the cheap SameJob path without spending a real-model call."""

    # Reuse the compact Windows-root strategy exercised by the full
    # cross-module journey; each process/test pair owns a distinct exact path.
    data_root, logparse_checkout = _journey_storage_roots(tmp_path, "s")
    _remove_test_data_root(data_root)
    _remove_test_data_root(logparse_checkout)
    request.addfinalizer(lambda: _remove_test_data_root(data_root))
    request.addfinalizer(lambda: _remove_test_data_root(logparse_checkout))
    logparse_record = tmp_path / "same-job-logparse.json"
    agent_record = tmp_path / "same-job-agent.jsonl"
    review_entered = tmp_path / "same-job-review-entered"
    review_release = tmp_path / "same-job-review-release"
    for name, value in RAW_LOGPARSE_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("S07_FAKE_LOGPARSE_RECORD", os.fspath(logparse_record))

    stage_tree_failures: list[dict[str, int | str | None]] = []
    original_stage_tree_content = StagedObjectWriter.stage_tree_content

    def record_stage_tree_failure(
        self: StagedObjectWriter,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        try:
            return original_stage_tree_content(self, *args, **kwargs)
        except BaseException as error:
            filename = getattr(error, "filename", None)
            stage_tree_failures.append(
                {
                    "error_type": type(error).__name__,
                    "errno": getattr(error, "errno", None),
                    "winerror": getattr(error, "winerror", None),
                    "filename_length": (
                        len(os.fspath(filename)) if filename is not None else None
                    ),
                }
            )
            raise

    monkeypatch.setattr(
        StagedObjectWriter,
        "stage_tree_content",
        record_stage_tree_failure,
    )
    stack = _Stack(
        data_root,
        logparse_record=logparse_record,
        agent_record=agent_record,
        review_entered=review_entered,
        review_release=review_release,
        seed="s08-same-job-first-process",
    )
    stack.stage_tree_failures = stage_tree_failures
    archive = ARCHIVE.read_bytes()
    archive_sha256 = hashlib.sha256(archive).hexdigest()
    created = _mcp(
        stack.mcp,
        "problem_locator_create_case",
        {
            "request_id": "s08-same-job-create",
            "raw_problem_text": "A payment service call to inventory times out.",
            "statement": "A payment service call to inventory times out.",
            "expected_behavior": "The payment request completes.",
            "actual_behavior": "The payment request times out.",
            "scope": "payment-to-inventory RPC",
            "goals": ["Locate the timeout cause."],
            "non_goals": [],
            "constraints": [],
            "completion_criteria": ["Identify the timed-out request."],
            "initial_user_fact_names": ["order_id"],
            "initial_user_fact_values": ["synthetic-order-0001"],
            "wait_seconds": 0,
        },
    )
    case_id = created["business_receipt"]["case_id"]
    stack.start()
    stack.wait_idle()
    waiting_a = _query(stack.mcp, case_id)
    assert waiting_a["status"] == CaseStatus.WAITING_INPUT.value
    assert [
        item["name"]
        for item in waiting_a["pending_requirements"]
        if item["status"] == RequirementStatus.OPEN.value
    ] == [*PARAMETER_GROUP_A, "log_archive"]
    job_count_before_inputs = len(
        stack.repository.read_snapshot().cases[case_id].jobs
    )

    submitted_a = _mcp(
        stack.mcp,
        "problem_locator_submit_supplement",
        {
            "request_id": "s08-same-job-submit-a",
            "case_id": case_id,
            "expected_case_revision": waiting_a["case_revision"],
            "input_names": list(PARAMETER_GROUP_A),
            "input_values": list(PARAMETER_GROUP_A.values()),
            "attachment_ids": [],
            "wait_seconds": 0,
        },
    )
    assert submitted_a["business_receipt"]["job_id"] is None
    waiting_attachment = _query(stack.mcp, case_id)
    assert waiting_attachment["status"] == CaseStatus.WAITING_ATTACHMENT.value
    assert len(stack.repository.read_snapshot().cases[case_id].jobs) == (
        job_count_before_inputs
    )
    assert {
        item["provenance"]["input_name"]
        for item in waiting_attachment["user_facts"]
    } == {*PARAMETER_GROUP_A, "order_id"}

    prepared = _mcp(
        stack.mcp,
        "problem_locator_prepare_attachment",
        {
            "request_id": "s08-same-job-prepare",
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
    submitted_attachment = _mcp(
        stack.mcp,
        "problem_locator_submit_supplement",
        {
            "request_id": "s08-same-job-submit-attachment",
            "case_id": case_id,
            "expected_case_revision": upload_data["case_revision"],
            "input_names": [],
            "input_values": [],
            "attachment_ids": [attachment_id],
            "wait_seconds": 0,
        },
    )
    diagnose_job_id = submitted_attachment["business_receipt"]["job_id"]
    _wait_for_review_marker(stack, case_id, review_entered)
    reviewing_view = _query(stack.mcp, case_id)
    assert reviewing_view["status"] == CaseStatus.REVIEWING.value
    assert not any(
        item["name"] == "order_id"
        and item["status"] == RequirementStatus.OPEN.value
        for item in reviewing_view["pending_requirements"]
    )
    reviewing = stack.repository.read_snapshot().cases[case_id]
    candidate = reviewing.case.diagnosis_state.candidate_conclusion
    assert candidate is not None
    assert candidate.proposed_by_job_id == diagnose_job_id
    assert list(reviewing.evidence) == EVIDENCE_IDS
    logparse_runs = [
        item for item in reviewing.artifacts.values()
        if item.kind is ArtifactKind.LOGPARSE_RUN
    ]
    assert len(logparse_runs) == 1
    assert logparse_runs[0].created_by_job_id == diagnose_job_id
    _assert_logparse_record(logparse_record, expected_target_count=2)

    review_release.write_text("pass\n", encoding="utf-8")
    stack.wait_idle()
    resolved_view = _query(stack.mcp, case_id)
    assert resolved_view["status"] == CaseStatus.RESOLVED.value
    assert resolved_view["final_result"]["status"] == CandidateStatus.ACCEPTED.value
    sessions = _agent_records(agent_record)
    assert [item["job_type"] for item in sessions] == [
        "ROUTE",
        "DIAGNOSE",
        "DIAGNOSE",
        "REVIEW",
    ]
    assert [item["phase"] for item in sessions] == [
        "ROUTE",
        "LOGPARSE_PREPROCESS",
        "METHODS_DIAGNOSE",
        "METHODS_REVIEW",
    ]
    assert len({item["pid"] for item in sessions}) == len(sessions)
    resolved = stack.repository.read_snapshot().cases[case_id]
    public_artifacts = [
        item for item in resolved.artifacts.values()
        if item.kind in {ArtifactKind.USER_RESULT, ArtifactKind.USER_RESULT_ARCHIVE}
    ]
    assert len(public_artifacts) == 2

    stack.shutdown()
    restarted = _Stack(
        data_root,
        logparse_record=logparse_record,
        agent_record=agent_record,
        review_entered=review_entered,
        review_release=review_release,
        seed="s08-same-job-restarted-process",
    )
    restarted.stage_tree_failures = stage_tree_failures
    restarted.start()
    restarted.wait_idle()
    assert _query(restarted.mcp, case_id) == resolved_view
    listed = _mcp(
        restarted.mcp,
        "problem_locator_list_artifacts",
        {"case_id": case_id},
    )
    assert [item["artifact_id"] for item in listed["artifacts"]] == sorted(
        item.artifact_id for item in public_artifacts
    )
    with TestClient(restarted.http_app) as http:
        downloads = [
            http.get(
                f"/api/v1/artifacts/{item.artifact_id}/content",
                params={"case_id": case_id},
            )
            for item in public_artifacts
        ]
    assert all(response.status_code == 200 for response in downloads)
    for artifact, response in zip(public_artifacts, downloads, strict=True):
        assert hashlib.sha256(response.content).hexdigest() == artifact.sha256
    archive_download = next(
        response.content
        for artifact, response in zip(public_artifacts, downloads, strict=True)
        if artifact.kind is ArtifactKind.USER_RESULT_ARCHIVE
    )
    with zipfile.ZipFile(io.BytesIO(archive_download)) as result_zip:
        assert result_zip.namelist() == _cross_project_result_experience()[
            "archive_expectations"
        ]["entry_order"]
    restarted.shutdown()
