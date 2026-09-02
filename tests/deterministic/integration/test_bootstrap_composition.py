from __future__ import annotations

import io
import re
import sys
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from problem_locator.bootstrap import (
    ProductionClock,
    StandaloneStateAdmin,
    ThreadStateChangeNotifier,
    UuidIdGenerator,
    build_service,
    create_app,
    main,
)
from problem_locator.contracts import (
    CLI_EXIT_CONFIG_OR_STATE_CORRUPT,
    ErrorCode,
    StateExport,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.entrypoints.settings import Settings
from problem_locator.storage.layout import StorageLayout
from problem_locator.storage.platform import FileInstanceLock


ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = ROOT / "tests/fixtures/components/runtime-catalog/skill-dir"
FAKE_LOGPARSE_REPO = ROOT / "tests/fixtures/components/logparse/fake/repo"
FAKE_LOGPARSE_CONFIG = FAKE_LOGPARSE_REPO / "config.yaml"
CASE_ID = "00000000-0000-0000-0000-000000000801"


def _settings(
    data_root: Path,
    *,
    skill_dir: Path = SKILL_DIR,
    reviewer_enabled: bool = False,
) -> Settings:
    return Settings.load(
        environ={
            "DATA_ROOT": str(data_root),
            "PUBLIC_BASE_URL": "http://127.0.0.1:8000",
            "SKILL_DIR": str(skill_dir),
            "GENERIC_SKILL_NAME": "generic-problem-locator-smoke",
            "LOGPARSE_REPO": str(FAKE_LOGPARSE_REPO),
            "LOGPARSE_CONFIG_PATH": str(FAKE_LOGPARSE_CONFIG),
            "LOGPARSE_PYTHON": sys.executable,
            "CLAUDE_COMMAND": "claude",
            "EVIDENCE_V2_REVIEWER_ENABLED": (
                "true" if reviewer_enabled else "false"
            ),
        }
    )


def test_public_create_app_does_not_expose_the_test_skill_override(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match="allow_test_skills"):
        create_app(  # type: ignore[call-arg]
            _settings(tmp_path / "data"),
            allow_test_skills=True,
        )


def test_production_app_starts_with_empty_diagnosis_skill_catalog(
    tmp_path: Path,
) -> None:
    empty_skill_dir = tmp_path / "empty-skills"
    empty_skill_dir.mkdir()
    app = create_app(
        _settings(
            tmp_path / "data",
            skill_dir=empty_skill_dir,
        )
    )
    graph = app.state.problem_locator_composition

    assert graph is not None
    assert graph.asset_catalog.route_bindings().available_skill_refs == []
    assert (
        graph.asset_catalog.generic_diagnose_bindings().generic_skill_name
        == "generic-problem-locator-smoke"
    )
    with TestClient(app) as client:
        readiness = client.get("/ready")
        assert readiness.status_code == 200
        assert readiness.json()["data"]["ready"] is True

    assert graph.closed is True
    assert graph.instance_lock.is_acquired() is False


def test_production_clock_ids_and_notifier_follow_frozen_shapes() -> None:
    timestamp = ProductionClock().now()
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z",
        timestamp,
    )

    ids = UuidIdGenerator()
    fresh = ids.new("case")
    assert str(uuid.UUID(fresh)) == fresh
    assert uuid.UUID(fresh).version == 4
    derived = ids.derive("artifact", [CASE_ID, "diagnosis-result"])
    assert derived == ids.derive("artifact", [CASE_ID, "diagnosis-result"])
    assert derived != ids.derive("artifact", [CASE_ID, "other-result"])
    assert uuid.UUID(derived).version == 5
    assert ids.derive(
        "artifact",
        ["installation", "case", "outcome", "proposal"],
    ) == "b66defc5-4b59-5161-b1be-04724b9c20ed"
    with pytest.raises(ValueError):
        ids.derive("artifact", [])
    with pytest.raises(ValueError):
        ids.derive("artifact", ["installation", ""])

    notifier = ThreadStateChangeNotifier()
    notifier.notify(CASE_ID, 4)
    assert notifier.wait_for_change(CASE_ID, 3, 0) is True
    assert notifier.wait_for_change(CASE_ID, 4, 0) is False

    waiting = threading.Event()
    observed: list[bool] = []

    def wait() -> None:
        waiting.set()
        observed.append(notifier.wait_for_change(CASE_ID, 4, 1.0))

    thread = threading.Thread(target=wait)
    thread.start()
    assert waiting.wait(1.0)
    notifier.notify(CASE_ID, 5)
    thread.join(1.0)
    assert not thread.is_alive()
    assert observed == [True]


def test_unique_object_graph_recovery_export_and_shutdown_lock_order(
    tmp_path: Path,
) -> None:
    graph = build_service(_settings(tmp_path / "data"))
    try:
        assert str(tmp_path) not in repr(graph)
        assert graph.instance_lock.is_acquired()
        assert graph.dispatcher.target is graph.scheduler
        assert graph.publication_guard.coordination_lock is graph.coordination_lock
        assert graph.repository._coordination_lock is graph.coordination_lock
        assert graph.execution_records._coordination_lock is graph.coordination_lock
        assert graph.resource_store.coordination_lock is graph.coordination_lock
        assert graph.resource_store.attachment_registry is graph.attachment_registry
        assert graph.upload_guard.registry is graph.attachment_registry
        assert graph.repository._file_sync is graph.file_sync
        assert graph.execution_records._file_sync is graph.file_sync
        assert graph.resource_store._file_sync is graph.file_sync
        assert graph.repository._replacer is graph.replacer
        assert graph.execution_records._replacer is graph.replacer
        assert graph.resource_store._replacer is graph.replacer
        assert graph.retention.cleaner._coordination_lock is graph.coordination_lock
        assert graph.retention.cleaner._stage_registry is (
            graph.resource_store.stage_registry
        )
        assert graph.retention.cleaner._attachment_registry is (
            graph.attachment_registry
        )
        assert graph.asset_catalog._logparse_broker_factory is (
            graph.logparse_broker_factory
        )
        assert graph.runtime._logparse_broker_factory is graph.logparse_broker_factory
        assert graph.runtime._asset_resolver._catalog is graph.asset_catalog
        assert graph.runtime._evidence_v2_reviewer_enabled is False

        before = graph.state_admin.readiness()
        assert [check.name for check in before.checks] == [
            "CONFIG",
            "INSTANCE_LOCK",
            "STATE",
            "DATA_DIRECTORIES",
            "RECOVERY",
        ]
        assert [check.passed for check in before.checks] == [
            True,
            True,
            True,
            True,
            False,
        ]
        assert before.error is not None
        assert before.error.code is ErrorCode.DISPATCH_REJECTED

        recovery = graph.start()
        assert recovery.completed is True
        assert graph.retention.started is True
        assert graph.retention.thread_alive is True
        assert graph.state_admin.readiness().ready is True
        exported = parse_canonical_json_bytes(
            graph.state_admin.export_state(),
            StateExport,
        )
        assert exported.source_generation == exported.state.generation
        assert exported.resources == []
        assert exported.object_counts.runtime_epochs == 1
        assert exported.object_counts.recovery_processing_records == 1
    finally:
        graph.close()

    assert graph.closed is True
    assert graph.instance_lock.is_acquired() is False
    assert graph.retention.thread_alive is False
    replacement = FileInstanceLock(graph.layout.instance_lock).acquire()
    replacement.release()


def test_production_composition_injects_enabled_evidence_v2_reviewer(
    tmp_path: Path,
) -> None:
    graph = build_service(
        _settings(tmp_path / "data", reviewer_enabled=True)
    )
    try:
        assert graph.settings.evidence_v2_reviewer_enabled is True
        assert graph.runtime._evidence_v2_reviewer_enabled is True
    finally:
        graph.close()


def test_asgi_lifespan_runs_recovery_before_ingress_and_releases_lock(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path / "data"))
    graph = app.state.problem_locator_composition
    assert graph is not None
    assert graph.started is False
    assert graph.instance_lock.is_acquired() is True

    with TestClient(app) as client:
        assert graph.started is True
        assert client.get("/live").status_code == 200
        readiness = client.get("/ready")
        assert readiness.status_code == 200
        assert readiness.json()["data"]["ready"] is True

    assert graph.closed is True
    assert graph.instance_lock.is_acquired() is False


def test_second_service_fails_closed_on_the_same_instance_lock(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "data")
    first = build_service(settings)
    try:
        second_app = create_app(settings)
        assert second_app.state.problem_locator_composition is None
        owner = second_app.state.problem_locator_owner
        assert owner.error.code is ErrorCode.INSTANCE_LOCKED
        assert owner.instance_lock is None
        with TestClient(second_app) as client:
            assert client.get("/live").status_code == 200
            readiness = client.get("/ready")
            assert readiness.status_code == 503
            assert readiness.json()["error"]["code"] == (
                ErrorCode.INSTANCE_LOCKED.value
            )
        assert first.instance_lock.is_acquired() is True
        assert first.scheduler.recovery_result is None
        assert first.retention.started is False
    finally:
        first.close()


def test_standalone_admin_is_lock_scoped_and_exports_one_canonical_generation(
    tmp_path: Path,
) -> None:
    graph = build_service(_settings(tmp_path / "data"))
    graph.start()
    graph.close()
    original_state = graph.layout.state.read_bytes()

    admin = StandaloneStateAdmin(graph.layout.data_root)
    report = admin.validate_state()
    assert report.valid is True
    assert graph.layout.state.read_bytes() == original_state

    exported_bytes = admin.export_state()
    exported = parse_canonical_json_bytes(exported_bytes, StateExport)
    assert exported.source_generation == report.generation
    assert exported.object_counts == report.object_counts
    assert canonical_json_bytes(exported) == exported_bytes
    assert graph.layout.state.read_bytes() == original_state

    # Both operations released the process lock before returning.
    lock = FileInstanceLock(graph.layout.instance_lock).acquire()
    try:
        blocked = admin.validate_state()
        assert blocked.valid is False
        assert blocked.errors[0].code == ErrorCode.INSTANCE_LOCKED.value
    finally:
        lock.release()


def test_unmarked_legacy_state_is_rejected_without_any_data_root_mutation(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    layout = StorageLayout.at(data_root)
    r2_bytes = canonical_json_bytes(
        {"contract_revision": "v1-contract-r2", "schema_version": 1}
    )
    layout.state.write_bytes(r2_bytes)
    original_entries = tuple(data_root.iterdir())

    admin = StandaloneStateAdmin(data_root)
    report = admin.validate_state()
    assert report.valid is False
    assert report.errors[0].code == ErrorCode.STATE_SCHEMA_UNSUPPORTED.value
    assert layout.state.read_bytes() == r2_bytes
    assert tuple(data_root.iterdir()) == original_entries == (layout.state,)

    stdout = io.BytesIO()
    stderr = io.BytesIO()
    assert main(
        ["validate-state", "--data-root", str(data_root)],
        stdout=stdout,
        stderr=stderr,
    ) == CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    assert parse_canonical_json_bytes(stdout.getvalue(), type(report)) == report
    assert stderr.getvalue() == b""
    assert layout.state.read_bytes() == r2_bytes
    assert tuple(data_root.iterdir()) == original_entries

    app = create_app(_settings(data_root))
    owner = app.state.problem_locator_owner
    assert app.state.problem_locator_composition is None
    assert owner.error.code is ErrorCode.STATE_SCHEMA_UNSUPPORTED
    assert owner.instance_lock is None
    with TestClient(app) as client:
        assert client.get("/live").status_code == 200
        readiness = client.get("/ready")
        assert readiness.status_code == 503
        assert readiness.json()["error"]["code"] == (
            ErrorCode.STATE_SCHEMA_UNSUPPORTED.value
        )
    assert layout.state.read_bytes() == r2_bytes
    assert tuple(data_root.iterdir()) == original_entries
