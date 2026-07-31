from __future__ import annotations

import base64
import json
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast

import pytest

from problem_locator.contracts import canonical_json_bytes
from problem_locator.integrations.logparse import cli
from problem_locator.integrations.logparse.requests import (
    Anchor,
    ParseTargetsRequest,
    TargetLogsRequest,
)


ATTACHMENT_ID = "00000000-0000-0000-0000-000000000001"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000002"
PROBLEM_TIME = "2026-01-03T00:05:00.000Z"
TOKEN = "job-scoped-token-that-must-remain-secret"
REQUEST_PATH = "output/proposals/run-1/request.json"
RESULT_PATH = "output/proposals/run-1/target_logs.json"


@dataclass(frozen=True, slots=True)
class _Record:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class _Server(HTTPServer):
    records: list[_Record]
    response_status: int
    response_body: bytes


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        server = cast(_Server, self.server)
        length = int(self.headers.get("Content-Length", "0"))
        server.records.append(
            _Record(
                method="POST",
                path=self.path,
                headers={key.casefold(): value for key, value in self.headers.items()},
                body=self.rfile.read(length),
            )
        )
        self.send_response(server.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(server.response_body)))
        self.end_headers()
        self.wfile.write(server.response_body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def broker_server() -> Iterator[_Server]:
    server = _Server(("127.0.0.1", 0), _Handler)
    server.records = []
    server.response_status = 200
    server.response_body = canonical_json_bytes({"target_logs": []})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _anchor() -> Anchor:
    return Anchor(
        label="caller",
        module="COMPACT",
        slot="slot1",
        process_name="svc_master",
        pid="100",
    )


def _request_bytes(operation: str) -> bytes:
    if operation == "parse-targets":
        request = ParseTargetsRequest(
            schema_version=1,
            problem_time=PROBLEM_TIME,
            anchors=[_anchor()],
            attachment_id=ATTACHMENT_ID,
            artifact_proposal_key="run-1",
        )
    elif operation == "target-logs":
        request = TargetLogsRequest(
            schema_version=1,
            problem_time=PROBLEM_TIME,
            anchors=[_anchor()],
            artifact_id=ARTIFACT_ID,
        )
    else:  # pragma: no cover - helper misuse
        raise AssertionError(f"unsupported test operation: {operation}")
    return canonical_json_bytes(request)


def _prepare_workspace(
    tmp_path: Path,
    operation: str,
    *,
    request_bytes: bytes | None = None,
) -> tuple[Path, bytes]:
    workspace = tmp_path / "workspace"
    proposal = workspace / "output" / "proposals" / "run-1"
    proposal.mkdir(parents=True)
    payload = request_bytes if request_bytes is not None else _request_bytes(operation)
    (proposal / "request.json").write_bytes(payload)
    return workspace, payload


def _set_capability(
    monkeypatch: pytest.MonkeyPatch,
    server: _Server,
    *,
    token: str = TOKEN,
) -> str:
    host, port = server.server_address
    endpoint = f"http://{host}:{port}/job/logparse"
    monkeypatch.setenv("PROBLEM_LOCATOR_LOGPARSE_ENDPOINT", endpoint)
    monkeypatch.setenv("PROBLEM_LOCATOR_LOGPARSE_TOKEN", token)
    return endpoint


def _main(operation: str) -> int:
    return cli.main(
        [
            operation,
            "--request",
            REQUEST_PATH,
            "--result",
            RESULT_PATH,
        ]
    )


@pytest.mark.parametrize("operation", ["parse-targets", "target-logs"])
def test_cli_relays_only_the_two_fixed_commands_with_an_exact_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    broker_server: _Server,
    capsys: pytest.CaptureFixture[str],
    operation: str,
) -> None:
    workspace, request_bytes = _prepare_workspace(tmp_path, operation)
    monkeypatch.chdir(workspace)
    _set_capability(monkeypatch, broker_server)

    assert _main(operation) == 0

    result_bytes = broker_server.response_body
    assert (workspace / RESULT_PATH).read_bytes() == result_bytes
    assert list((workspace / RESULT_PATH).parent.glob(".target_logs.*.tmp")) == []
    assert len(broker_server.records) == 1
    record = broker_server.records[0]
    assert record.method == "POST"
    assert record.path == "/job/logparse"
    assert record.headers["content-type"] == "application/json"
    assert record.headers["x-problem-locator-logparse-token"] == TOKEN
    assert "authorization" not in record.headers
    assert "cookie" not in record.headers
    assert record.body == canonical_json_bytes(
        {
            "schema_version": 1,
            "operation": operation,
            "request_path": REQUEST_PATH,
            "result_path": RESULT_PATH,
            "request_base64": base64.b64encode(request_bytes).decode("ascii"),
        }
    )
    captured = capsys.readouterr()
    assert captured.out == "problem-locator-logparse: broker request completed\n"
    assert captured.err == ""


def test_cli_rejects_every_other_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "run-command",
                "--request",
                REQUEST_PATH,
                "--result",
                RESULT_PATH,
            ]
        )
    assert raised.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_rejects_noncanonical_request_bytes_without_contacting_the_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    broker_server: _Server,
    capsys: pytest.CaptureFixture[str],
) -> None:
    decoded = json.loads(_request_bytes("parse-targets"))
    noncanonical = json.dumps(decoded, indent=2).encode("utf-8")
    workspace, _payload = _prepare_workspace(
        tmp_path,
        "parse-targets",
        request_bytes=noncanonical,
    )
    monkeypatch.chdir(workspace)
    _set_capability(monkeypatch, broker_server)

    assert _main("parse-targets") == 2

    assert broker_server.records == []
    assert not (workspace / RESULT_PATH).exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "problem-locator-logparse: broker request failed\n"
    assert ATTACHMENT_ID not in captured.err


@pytest.mark.parametrize(
    ("request_path", "result_path"),
    [
        (
            "output/proposals/run-1/request.json",
            "output/proposals/run-2/target_logs.json",
        ),
        (
            "output/proposals/run-1/request.json",
            "output/proposals/run-1/result.json",
        ),
        (
            "input/proposals/run-1/request.json",
            "output/proposals/run-1/target_logs.json",
        ),
    ],
)
def test_cli_requires_the_fixed_paths_in_one_proposal_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    broker_server: _Server,
    request_path: str,
    result_path: str,
) -> None:
    workspace, _payload = _prepare_workspace(tmp_path, "parse-targets")
    monkeypatch.chdir(workspace)
    _set_capability(monkeypatch, broker_server)

    assert cli.main(
        [
            "parse-targets",
            "--request",
            request_path,
            "--result",
            result_path,
        ]
    ) == 2
    assert broker_server.records == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:9443/job/logparse",
        "http://localhost:9443/job/logparse",
        "http://example.com:80/job/logparse",
        "http://user@127.0.0.1:9443/job/logparse",
        "http://127.0.0.1:9443/job/logparse?query=1",
    ],
)
def test_cli_accepts_only_a_plain_http_loopback_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    endpoint: str,
) -> None:
    workspace, _payload = _prepare_workspace(tmp_path, "parse-targets")
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("PROBLEM_LOCATOR_LOGPARSE_ENDPOINT", endpoint)
    monkeypatch.setenv("PROBLEM_LOCATOR_LOGPARSE_TOKEN", TOKEN)

    assert _main("parse-targets") == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "problem-locator-logparse: broker request failed\n"
    assert endpoint not in captured.err
    assert TOKEN not in captured.err


def test_cli_uses_an_atomic_same_directory_replace_for_the_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    broker_server: _Server,
) -> None:
    workspace, _payload = _prepare_workspace(tmp_path, "parse-targets")
    result = workspace / RESULT_PATH
    result.write_bytes(b"old-result\n")
    monkeypatch.chdir(workspace)
    _set_capability(monkeypatch, broker_server)
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def recording_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(cli.os, "replace", recording_replace)

    assert _main("parse-targets") == 0

    assert result.read_bytes() == broker_server.response_body
    assert len(replacements) == 1
    temporary, replaced_target = replacements[0]
    assert temporary.parent == result.parent
    assert temporary.name.startswith(".target_logs.")
    assert temporary.name.endswith(".tmp")
    assert replaced_target == result
    assert not temporary.exists()


def test_cli_http_rejection_is_generic_and_does_not_write_a_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    broker_server: _Server,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, _payload = _prepare_workspace(tmp_path, "parse-targets")
    monkeypatch.chdir(workspace)
    endpoint = _set_capability(monkeypatch, broker_server)
    broker_server.response_status = 403
    broker_server.response_body = b"broker-body-secret"

    assert _main("parse-targets") == 2

    assert len(broker_server.records) == 1
    assert not (workspace / RESULT_PATH).exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "problem-locator-logparse: broker request failed\n"
    for secret in (TOKEN, endpoint, "broker-body-secret"):
        assert secret not in captured.err


@pytest.mark.parametrize(
    "response_body",
    [
        b'{"target_logs":[]}',
        canonical_json_bytes(["not", "an", "object"]),
        b'{"target_logs":[]}\ntrailing',
    ],
)
def test_cli_rejects_noncanonical_or_nonobject_results_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    broker_server: _Server,
    response_body: bytes,
) -> None:
    workspace, _payload = _prepare_workspace(tmp_path, "target-logs")
    monkeypatch.chdir(workspace)
    _set_capability(monkeypatch, broker_server)
    broker_server.response_body = response_body

    assert _main("target-logs") == 2

    assert len(broker_server.records) == 1
    assert not (workspace / RESULT_PATH).exists()


@pytest.mark.parametrize("linked_component", ["request", "result"])
def test_cli_rejects_symlinked_request_or_result_paths_before_network_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    broker_server: _Server,
    linked_component: str,
) -> None:
    workspace, request_bytes = _prepare_workspace(tmp_path, "parse-targets")
    proposal = workspace / "output" / "proposals" / "run-1"
    path = proposal / ("request.json" if linked_component == "request" else "target_logs.json")
    external = tmp_path / f"external-{linked_component}.json"
    external.write_bytes(request_bytes if linked_component == "request" else b"old\n")
    if linked_component == "request":
        path.unlink()
    try:
        path.symlink_to(external)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")
    monkeypatch.chdir(workspace)
    _set_capability(monkeypatch, broker_server)

    assert _main("parse-targets") == 2

    assert broker_server.records == []
    assert path.is_symlink()
    assert external.read_bytes() == (
        request_bytes if linked_component == "request" else b"old\n"
    )


@pytest.mark.parametrize(
    ("missing_name", "present_name", "present_value"),
    [
        (
            "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT",
            "PROBLEM_LOCATOR_LOGPARSE_TOKEN",
            TOKEN,
        ),
        (
            "PROBLEM_LOCATOR_LOGPARSE_TOKEN",
            "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT",
            "http://127.0.0.1:9/job/logparse",
        ),
    ],
)
def test_cli_missing_capability_fails_without_leaking_present_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing_name: str,
    present_name: str,
    present_value: str,
) -> None:
    workspace, _payload = _prepare_workspace(tmp_path, "parse-targets")
    monkeypatch.chdir(workspace)
    monkeypatch.delenv(missing_name, raising=False)
    monkeypatch.setenv(present_name, present_value)

    assert _main("parse-targets") == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "problem-locator-logparse: broker request failed\n"
    assert present_value not in captured.err
    assert not (workspace / RESULT_PATH).exists()
