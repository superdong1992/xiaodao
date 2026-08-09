from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from problem_locator.contracts.errors import (
    ERROR_SPECS,
    PORT_ERROR_CODES,
    ApplicationPortError,
    CLI_EXIT_CONFIG_OR_STATE_CORRUPT,
    CLI_EXIT_REQUEST_OR_STATE_CONFLICT,
    CLI_EXIT_SUCCESS,
)
from problem_locator.contracts.enums import ErrorCode
from problem_locator.contracts.limits import CONTRACT_REVISION
from problem_locator.contracts.models import ApplicationError
from problem_locator.contracts.serialization import canonical_json_bytes
from problem_locator.diagnostics import configure_diagnostics
from problem_locator.entrypoints.cli import CliHooks, main, run_uvicorn
from problem_locator.interfaces.error_mapping import cli_exit_for
from problem_locator.journey import JourneyEvent, configure_journey
from tests.deterministic.unit.interfaces.fakes import FakeStateAdmin
from tests.deterministic.unit.interfaces.helpers import invalid_report, readiness, valid_report


_JOURNEY_CASE_ID = "00000000-0000-4000-8000-000000000901"


def _journey_event_bytes() -> bytes:
    event = JourneyEvent.model_validate(
        {
            "schema_version": 1,
            "sequence": 1,
            "timestamp": "2026-08-05T08:00:00.000Z",
            "level": "INFO",
            "event": "case.created",
            "correlation_id": None,
            "request_id": "request-1",
            "case_id": _JOURNEY_CASE_ID,
            "job_id": None,
            "job_type": None,
            "outcome_id": None,
            "duration_ms": None,
            "data": {
                "problem_spec": {"statement": "A request timed out."},
                "case": {"status": "RUNNING"},
            },
        },
        strict=True,
    )
    return canonical_json_bytes(event)


def hooks_for(
    admin: FakeStateAdmin,
    calls: list[tuple[object, ...]] | None = None,
    *,
    atomic_writer=None,
) -> CliHooks:
    recorded = [] if calls is None else calls

    def state_admin_factory(path: Path) -> FakeStateAdmin:
        recorded.append(("state_admin", path))
        return admin

    def app_factory(settings):
        recorded.append(("app", settings))
        return "asgi-app"

    def server_runner(app, host: str, port: int, workers: int) -> None:
        recorded.append(("serve", app, host, port, workers))

    return CliHooks(state_admin_factory, app_factory, server_runner, atomic_writer)


def test_validate_state_writes_canonical_report_and_is_read_only(tmp_path: Path) -> None:
    report = valid_report()
    admin = FakeStateAdmin(readiness=readiness(), validations=[report])
    stdout = io.BytesIO()
    stderr = io.BytesIO()

    exit_code = main(
        ["validate-state", "--data-root", str(tmp_path)],
        hooks=hooks_for(admin),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == CLI_EXIT_SUCCESS
    assert stdout.getvalue() == canonical_json_bytes(report)
    assert stderr.getvalue() == b""
    assert admin.calls == ["validate_state"]


@pytest.mark.parametrize(
    "code",
    [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED],
)
def test_invalid_state_still_prints_report_and_exits_three(
    tmp_path: Path,
    code: ErrorCode,
) -> None:
    report = invalid_report(code)
    admin = FakeStateAdmin(readiness=readiness(), validations=[report])
    stdout = io.BytesIO()

    exit_code = main(
        ["validate-state", "--data-root", str(tmp_path)],
        hooks=hooks_for(admin),
        stdout=stdout,
        stderr=io.BytesIO(),
    )

    assert exit_code == CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    assert stdout.getvalue() == canonical_json_bytes(report)


def test_export_state_writes_port_bytes_atomically(tmp_path: Path) -> None:
    exported = f'{{"contract_revision":"{CONTRACT_REVISION}"}}\n'.encode("utf-8")
    admin = FakeStateAdmin(readiness=readiness(), exports=[exported])
    data_root = tmp_path / "data"
    output = tmp_path / "nested-name.json"

    exit_code = main(
        ["export-state", "--data-root", str(data_root), "--output", str(output)],
        hooks=hooks_for(admin),
        stdout=io.BytesIO(),
        stderr=io.BytesIO(),
    )

    assert exit_code == CLI_EXIT_SUCCESS
    assert output.read_bytes() == exported
    assert admin.calls == ["export_state"]
    assert not list(tmp_path.glob(".nested-name.json.*.tmp"))


def test_export_refuses_to_write_inside_authoritative_data_root(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    protected = data_root / "state.json"
    protected.write_bytes(b"authoritative")
    admin = FakeStateAdmin(readiness=readiness(), exports=[b"export\n"])
    stderr = io.BytesIO()

    exit_code = main(
        [
            "export-state",
            "--data-root",
            str(data_root),
            "--output",
            str(protected),
        ],
        hooks=hooks_for(admin),
        stdout=io.BytesIO(),
        stderr=stderr,
    )

    assert exit_code == CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    assert protected.read_bytes() == b"authoritative"
    assert admin.calls == []
    assert str(protected).encode() not in stderr.getvalue()


def test_export_atomic_writer_failure_is_injected_and_safely_reported(
    tmp_path: Path,
) -> None:
    admin = FakeStateAdmin(readiness=readiness(), exports=[b"export\n"])
    stderr = io.BytesIO()

    def fail_write(_output: Path, _data: bytes) -> None:
        raise OSError(f"secret path: {tmp_path}")

    exit_code = main(
        [
            "export-state",
            "--data-root",
            str(tmp_path / "data"),
            "--output",
            str(tmp_path / "export.json"),
        ],
        hooks=hooks_for(admin, atomic_writer=fail_write),
        stdout=io.BytesIO(),
        stderr=stderr,
    )

    assert exit_code == CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    assert admin.calls == ["export_state"]
    assert str(tmp_path).encode() not in stderr.getvalue()


@pytest.mark.parametrize(
    "code",
    sorted(
        PORT_ERROR_CODES["StateAdminPort.export_state"],
        key=lambda item: item.value,
    ),
)
def test_export_maps_every_frozen_application_port_error_and_exit_code(
    tmp_path: Path,
    code: ErrorCode,
) -> None:
    error = ApplicationError(
        code=code,
        message=f"Safe {code.value} state export failure.",
        details=[],
        retryable=ERROR_SPECS[code].application_retryable,
    )
    admin = FakeStateAdmin(
        readiness=readiness(),
        exports=[ApplicationPortError(error)],
    )
    stdout = io.BytesIO()
    stderr = io.BytesIO()
    output = tmp_path / "export.json"

    exit_code = main(
        [
            "export-state",
            "--data-root",
            str(tmp_path / "data"),
            "--output",
            str(output),
        ],
        hooks=hooks_for(admin),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == cli_exit_for(error)
    assert stdout.getvalue() == b""
    assert stderr.getvalue() == canonical_json_bytes(error)
    assert not output.exists()


def test_render_journey_uses_explicit_log_dir_without_composition(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("DFX_LOG_FILE", raising=False)
    monkeypatch.delenv("DFX_LOG_DIR", raising=False)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "journey.jsonl").write_bytes(_journey_event_bytes())
    stdout = io.BytesIO()
    stderr = io.BytesIO()

    exit_code = main(
        [
            "render-journey",
            "--log-dir",
            str(log_dir),
            "--case-id",
            _JOURNEY_CASE_ID,
        ],
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == CLI_EXIT_SUCCESS
    receipt = json.loads(stdout.getvalue())
    assert receipt["case_id"] == _JOURNEY_CASE_ID
    assert receipt["case_status"] == "RUNNING"
    assert receipt["terminal"] is False
    assert Path(receipt["detailed_log"]).is_file()
    assert Path(receipt["brief_log"]).is_file()
    assert stderr.getvalue() == b""


def test_render_journey_uses_dfx_log_dir_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("DFX_LOG_FILE", raising=False)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "journey.jsonl").write_bytes(_journey_event_bytes())
    monkeypatch.setenv("DFX_LOG_DIR", str(log_dir))

    assert main(
        ["render-journey", "--case-id", _JOURNEY_CASE_ID],
        stdout=io.BytesIO(),
        stderr=io.BytesIO(),
    ) == CLI_EXIT_SUCCESS


@pytest.mark.parametrize("legacy_value", ["", "D:/legacy/service.jsonl"])
def test_render_journey_rejects_legacy_service_log_key(
    monkeypatch,
    tmp_path: Path,
    legacy_value: str,
) -> None:
    monkeypatch.setenv("DFX_LOG_FILE", legacy_value)
    stderr = io.BytesIO()

    exit_code = main(
        [
            "render-journey",
            "--log-dir",
            str(tmp_path),
            "--case-id",
            _JOURNEY_CASE_ID,
        ],
        stdout=io.BytesIO(),
        stderr=stderr,
    )

    assert exit_code == CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    assert b"DFX_LOG_DIR" in stderr.getvalue()


def test_render_journey_maps_bad_source_and_missing_case(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("DFX_LOG_FILE", raising=False)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    source = log_dir / "journey.jsonl"
    source.write_bytes(b"{bad}\n")
    stderr = io.BytesIO()

    assert main(
        [
            "render-journey",
            "--log-dir",
            str(log_dir),
            "--case-id",
            _JOURNEY_CASE_ID,
        ],
        stdout=io.BytesIO(),
        stderr=stderr,
    ) == CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    assert b"journey.jsonl:1" in stderr.getvalue()

    source.write_bytes(_journey_event_bytes())
    stderr = io.BytesIO()
    assert main(
        [
            "render-journey",
            "--log-dir",
            str(log_dir),
            "--case-id",
            "00000000-0000-4000-8000-000000000999",
        ],
        stdout=io.BytesIO(),
        stderr=stderr,
    ) == CLI_EXIT_REQUEST_OR_STATE_CONFLICT
    assert b"not present" in stderr.getvalue()


def test_serve_uses_exactly_one_worker(monkeypatch, tmp_path: Path) -> None:
    values = {
        "DATA_ROOT": str(tmp_path / "data"),
        "PUBLIC_BASE_URL": "http://127.0.0.1:8123",
        "SKILL_DIR": str(tmp_path / "skills"),
        "LOGPARSE_REPO": str(tmp_path / "logparse"),
        "LOGPARSE_CONFIG_PATH": str(tmp_path / "logparse.toml"),
        "PORT": "8123",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    calls: list[tuple[object, ...]] = []
    admin = FakeStateAdmin(readiness=readiness())

    exit_code = main(
        ["serve"],
        hooks=hooks_for(admin, calls),
        stdout=io.BytesIO(),
        stderr=io.BytesIO(),
    )

    assert exit_code == CLI_EXIT_SUCCESS
    assert calls[-1] == ("serve", "asgi-app", "127.0.0.1", 8123, 1)


def test_serve_routes_debug_and_journey_to_configured_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    values = {
        "DATA_ROOT": str(tmp_path / "data"),
        "PUBLIC_BASE_URL": "http://127.0.0.1:8123",
        "SKILL_DIR": str(tmp_path / "skills"),
        "LOGPARSE_REPO": str(tmp_path / "logparse"),
        "LOGPARSE_CONFIG_PATH": str(tmp_path / "logparse.toml"),
        "DFX_LOG_DIR": str(tmp_path / "logs"),
    }
    monkeypatch.delenv("DFX_LOG_FILE", raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    assert main(
        ["serve"],
        hooks=hooks_for(FakeStateAdmin(readiness=readiness())),
        stdout=io.BytesIO(),
        stderr=io.BytesIO(),
    ) == CLI_EXIT_SUCCESS

    assert (tmp_path / "logs" / "debug.jsonl").is_file()
    assert (tmp_path / "logs" / "journey.jsonl").is_file()
    configure_journey()
    configure_diagnostics("INFO")


def test_uvicorn_runner_rejects_multiple_workers_before_startup() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        run_uvicorn("asgi-app", "127.0.0.1", 8000, 2)


def test_cli_argument_and_missing_composition_errors_are_safe(tmp_path: Path) -> None:
    stderr = io.BytesIO()
    assert main(["unknown"], stdout=io.BytesIO(), stderr=stderr) == (
        CLI_EXIT_REQUEST_OR_STATE_CONFLICT
    )
    assert b"traceback" not in stderr.getvalue().lower()
    assert str(tmp_path).encode() not in stderr.getvalue()

    stderr = io.BytesIO()
    assert main(
        ["validate-state", "--data-root", str(tmp_path)],
        stdout=io.BytesIO(),
        stderr=stderr,
    ) == CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    assert b"CLI composition is not configured" in stderr.getvalue()
