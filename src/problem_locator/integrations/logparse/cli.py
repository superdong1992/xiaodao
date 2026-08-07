"""Narrow Agent-side client for the job-scoped logparse broker."""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from problem_locator.contracts import (
    ErrorCode,
    ExecutionFailure,
    ExecutionStage,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.integrations.agent_json import (
    AgentJsonSurface,
    normalize_agent_json_file,
)

from .paths import resolve_workspace_path, validate_proposal_io_paths
from .requests import BrokerEnvelope, ParseTargetsRequest, TargetLogsRequest


_ENDPOINT_ENV = "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT"
_TOKEN_ENV = "PROBLEM_LOCATOR_LOGPARSE_TOKEN"
_TOKEN_HEADER = "X-Problem-Locator-Logparse-Token"
_MAX_REQUEST_BYTES = 2_000_000
_MAX_RESULT_BYTES = 2_000_000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="problem-locator-logparse")
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation in ("parse-targets", "target-logs"):
        command = commands.add_parser(operation)
        command.add_argument("--request", required=True)
        command.add_argument("--result", required=True)
    return parser


def _endpoint() -> tuple[SplitResult, str]:
    raw_endpoint = os.environ.get(_ENDPOINT_ENV, "")
    token = os.environ.get(_TOKEN_ENV, "")
    if (
        not raw_endpoint
        or not token
        or len(raw_endpoint) > 2048
        or len(token) > 1024
        or any(character in raw_endpoint + token for character in "\r\n\x00")
    ):
        raise ValueError("job-scoped broker capability is unavailable")
    parsed = urlsplit(raw_endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("job-scoped broker endpoint is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ValueError("job-scoped broker endpoint is invalid")
    return parsed, token


def _read_request(
    workspace_root: Path,
    request_path: str,
    operation: str,
) -> bytes:
    request_file = resolve_workspace_path(
        workspace_root,
        request_path,
        must_exist=True,
    )
    metadata = request_file.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > _MAX_REQUEST_BYTES
    ):
        raise ValueError("broker request file is invalid")
    model = ParseTargetsRequest if operation == "parse-targets" else TargetLogsRequest
    document = normalize_agent_json_file(
        request_file,
        surface=AgentJsonSurface.LOGPARSE_REQUEST,
        max_bytes=_MAX_REQUEST_BYTES,
        validate=model.model_validate,
    )
    return document.canonical_bytes


def _invoke_broker(
    endpoint: SplitResult,
    token: str,
    envelope: BrokerEnvelope,
) -> tuple[bytes | None, ExecutionFailure | None]:
    body = canonical_json_bytes(envelope)
    connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port)
    try:
        connection.request(
            "POST",
            endpoint.path,
            body=body,
            headers={
                "Content-Type": "application/json",
                _TOKEN_HEADER: token,
            },
        )
        response = connection.getresponse()
        result = response.read(_MAX_RESULT_BYTES + 1)
    finally:
        connection.close()
    if len(result) > _MAX_RESULT_BYTES:
        raise RuntimeError("logparse broker rejected the request")
    if response.status != 200:
        try:
            failure = parse_canonical_json_bytes(result, ExecutionFailure)
        except ValueError as exc:
            raise RuntimeError("logparse broker rejected the request") from exc
        allowed = (
            failure.stage is ExecutionStage.ASSET_RESOLUTION
            and failure.code is ErrorCode.ASSET_VERSION_UNAVAILABLE
            and not failure.retryable
        ) or (
            failure.stage is ExecutionStage.TOOL_EXECUTE
            and failure.code
            in {ErrorCode.LOGPARSE_FAILED, ErrorCode.LOGPARSE_OUTPUT_INVALID}
        )
        if not allowed:
            raise RuntimeError("logparse broker returned an invalid failure")
        return None, failure
    parsed = parse_canonical_json_bytes(result)
    if not isinstance(parsed, dict):
        raise ValueError("logparse broker result must be one JSON object")
    return result, None


def _atomic_write_result(target: Path, payload: bytes) -> None:
    parent = target.parent
    parent_metadata = parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise ValueError("broker result parent is invalid")
    try:
        target_metadata = target.lstat()
    except FileNotFoundError:
        target_metadata = None
    if target_metadata is not None and not stat.S_ISREG(target_metadata.st_mode):
        raise ValueError("broker result target is invalid")

    temporary: Path | None = None
    try:
        for _attempt in range(16):
            candidate = parent / f".target_logs.{secrets.token_hex(16)}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                continue
            temporary = candidate
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
            return
        raise OSError("cannot reserve a broker result temporary file")
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def run(
    operation: str,
    request_path: str,
    result_path: str,
) -> ExecutionFailure | None:
    """Validate one fixed request and relay it to the current broker session."""

    validate_proposal_io_paths(request_path, result_path)
    workspace_root = Path.cwd()
    request_bytes = _read_request(workspace_root, request_path, operation)
    result_file = resolve_workspace_path(
        workspace_root,
        result_path,
        must_exist=False,
    )
    endpoint, token = _endpoint()
    envelope = BrokerEnvelope(
        schema_version=1,
        operation=operation,
        request_path=request_path,
        result_path=result_path,
        request_base64=base64.b64encode(request_bytes).decode("ascii"),
    )
    result_bytes, failure = _invoke_broker(endpoint, token, envelope)
    if failure is not None:
        _atomic_write_result(result_file, canonical_json_bytes(failure))
        return failure
    assert result_bytes is not None
    _atomic_write_result(result_file, result_bytes)
    return None


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        failure = run(arguments.operation, arguments.request, arguments.result)
    except (OSError, ValueError, RuntimeError, http.client.HTTPException, json.JSONDecodeError):
        sys.stderr.write("problem-locator-logparse: broker request failed\n")
        return 2
    if failure is not None:
        sys.stderr.write(
            "problem-locator-logparse: "
            f"{failure.stage.value}/{failure.code.value}\n"
        )
        return 2
    sys.stdout.write("problem-locator-logparse: broker request completed\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main", "run"]
