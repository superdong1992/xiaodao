"""Deterministic helper used by the ``problem-locator-client`` Skill.

The helper owns no service state.  It validates the public envelopes returned
by MCP/HTTP, constructs ``curl`` argv arrays without a shell, and keeps each
write request ID stable for the duration of its logical operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError

from problem_locator.contracts.commands import (
    ApplicationResponse,
    ArtifactView,
    CaseQueryResponse,
    UploadDescriptor,
)
from problem_locator.contracts.enums import ErrorCode
from problem_locator.contracts.models import ApplicationError, OpaqueId, PositiveInt
from problem_locator.contracts.ports import IdGenerator


CLIENT_FILE_CHUNK_BYTES = 1024 * 1024
CURL_READ_CHUNK_BYTES = 64 * 1024
MAX_CURL_JSON_BYTES = 1024 * 1024
CURL_HTTP_ERROR_RETURN_CODE = 22


@runtime_checkable
class McpClientPort(Protocol):
    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...


@runtime_checkable
class CurlPort(Protocol):
    def run(self, argv: Sequence[str], *, expect_json: bool) -> Mapping[str, Any] | None: ...

    def download(
        self,
        argv: Sequence[str],
        *,
        destination: Path,
        max_bytes: int,
    ) -> None: ...


class ClientOperationError(RuntimeError):
    def __init__(self, error: ApplicationError) -> None:
        self.error = error
        super().__init__(error.message)


class ClientProtocolError(RuntimeError):
    pass


class SystemCurl:
    """Invoke the user's existing curl binary with argv and ``shell=False``."""

    def __init__(self, executable: str = "curl") -> None:
        self._executable = executable

    def run(self, argv: Sequence[str], *, expect_json: bool) -> Mapping[str, Any] | None:
        if not expect_json:
            return_code = self._run_without_output(argv)
            if return_code != 0:
                raise ClientProtocolError("curl file transfer failed")
            return None

        process = self._start(argv, capture_stdout=True)
        assert process.stdout is not None
        try:
            stdout = _read_limited(
                process.stdout,
                max_bytes=MAX_CURL_JSON_BYTES,
                overflow_message="curl returned an oversized JSON response",
            )
            return_code = process.wait()
        except ClientProtocolError:
            _kill_and_reap(process)
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            _kill_and_reap(process)
            raise ClientProtocolError("curl transport failed") from exc
        except BaseException:
            _kill_and_reap(process)
            raise
        finally:
            process.stdout.close()

        if return_code not in {0, CURL_HTTP_ERROR_RETURN_CODE}:
            raise ClientProtocolError("curl transport failed")
        try:
            value = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClientProtocolError("curl returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ClientProtocolError("curl returned a non-object JSON response")
        # ``--fail-with-body`` deliberately returns a non-zero curl status for
        # HTTP failures while preserving the S06 JSON error envelope.  Return
        # that object so the caller can consume the frozen ApplicationError;
        # transport failures without a valid envelope remain generic and safe.
        return value

    def download(
        self,
        argv: Sequence[str],
        *,
        destination: Path,
        max_bytes: int,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
            raise ClientProtocolError("download size limit is invalid")

        process = self._start(argv, capture_stdout=True)
        assert process.stdout is not None
        try:
            with destination.open("wb") as output:
                _copy_limited(
                    process.stdout,
                    output,
                    max_bytes=max_bytes,
                )
            return_code = process.wait()
        except ClientProtocolError:
            _kill_and_reap(process)
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            _kill_and_reap(process)
            raise ClientProtocolError("curl file transfer failed") from exc
        except BaseException:
            _kill_and_reap(process)
            raise
        finally:
            process.stdout.close()

        if return_code != 0:
            raise ClientProtocolError("curl file transfer failed")

    def _run_without_output(self, argv: Sequence[str]) -> int:
        process = self._start(argv, capture_stdout=False)
        try:
            return process.wait()
        except (OSError, subprocess.SubprocessError) as exc:
            _kill_and_reap(process)
            raise ClientProtocolError("curl transport failed") from exc
        except BaseException:
            _kill_and_reap(process)
            raise

    def _start(
        self,
        argv: Sequence[str],
        *,
        capture_stdout: bool,
    ) -> subprocess.Popen[bytes]:
        try:
            return subprocess.Popen(
                [self._executable, *argv],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ClientProtocolError("curl could not be started") from exc


def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is None:
            process.kill()
    except (OSError, subprocess.SubprocessError):
        pass
    while True:
        try:
            process.wait()
            return
        except KeyboardInterrupt:
            # A child killed during cancellation still has to be reaped before
            # the original interrupt is allowed to leave the adapter.
            continue
        except (OSError, subprocess.SubprocessError):
            return


def _read_limited(
    source: BinaryIO,
    *,
    max_bytes: int,
    overflow_message: str,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = source.read(min(CURL_READ_CHUNK_BYTES, max_bytes - total + 1))
        if chunk == b"":
            return b"".join(chunks)
        if total + len(chunk) > max_bytes:
            raise ClientProtocolError(overflow_message)
        chunks.append(chunk)
        total += len(chunk)


def _copy_limited(source: BinaryIO, output: BinaryIO, *, max_bytes: int) -> None:
    total = 0
    while True:
        chunk = source.read(min(CURL_READ_CHUNK_BYTES, max_bytes - total + 1))
        if chunk == b"":
            return
        if total + len(chunk) > max_bytes:
            raise ClientProtocolError("downloaded artifact exceeded its declared size")
        output.write(chunk)
        total += len(chunk)


def _success_data(envelope: Mapping[str, Any]) -> Any:
    if set(envelope) != {"ok", "data", "error"}:
        raise ClientProtocolError("response envelope has unexpected fields")
    if envelope["ok"] is True and envelope["data"] is not None and envelope["error"] is None:
        return envelope["data"]
    if envelope["ok"] is False and envelope["data"] is None:
        try:
            error = ApplicationError.model_validate(envelope["error"])
        except ValidationError as exc:
            raise ClientProtocolError("response error does not match S00") from exc
        raise ClientOperationError(error)
    raise ClientProtocolError("response envelope flags are inconsistent")


def _validate_model(model_type: Any, value: Any, message: str) -> Any:
    try:
        return model_type.model_validate(value)
    except ValidationError as exc:
        raise ClientProtocolError(message) from exc


def _validate_type(expected_type: Any, value: Any, message: str) -> Any:
    try:
        return TypeAdapter(expected_type).validate_python(value)
    except ValidationError as exc:
        raise ClientProtocolError(message) from exc


def _file_size_and_sha256(path: Path, *, hash_required: bool) -> tuple[int, str | None]:
    total = 0
    digest = hashlib.sha256() if hash_required else None
    with path.open("rb") as source:
        while True:
            chunk = source.read(CLIENT_FILE_CHUNK_BYTES)
            if chunk == b"":
                break
            total += len(chunk)
            if digest is not None:
                digest.update(chunk)
    return total, None if digest is None else digest.hexdigest()


def _header_argv(headers: Mapping[str, str]) -> list[str]:
    expected = (
        "Idempotency-Key",
        "Content-Type",
        "Content-Length",
        "X-Content-SHA256",
    )
    if set(headers) != set(expected):
        raise ClientProtocolError("upload descriptor headers are incomplete")
    result: list[str] = []
    for name in expected:
        result.extend(("--header", f"{name}: {headers[name]}"))
    return result


def _http_transfer_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ClientProtocolError("service returned an invalid transfer URL") from exc
    if (
        value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port == 0
    ):
        raise ClientProtocolError("service returned an invalid transfer URL")
    return value


class ClientAccessWorkflow:
    def __init__(
        self,
        mcp: McpClientPort,
        curl: CurlPort,
        ids: IdGenerator,
    ) -> None:
        self._mcp = mcp
        self._curl = curl
        self._ids = ids

    def create_case(
        self,
        *,
        problem_spec: Mapping[str, Any],
        initial_user_facts: Sequence[Mapping[str, Any]] = (),
        wait_seconds: int = 0,
    ) -> ApplicationResponse:
        data = _success_data(
            self._mcp.call_tool(
                "problem_locator_create_case",
                {
                    "request_id": self._ids.new("request"),
                    "problem_spec": dict(problem_spec),
                    "initial_user_facts": [dict(item) for item in initial_user_facts],
                    "wait_seconds": wait_seconds,
                },
            )
        )
        return _validate_model(
            ApplicationResponse,
            data,
            "create response does not match S00",
        )

    def get_case(
        self,
        case_id: str,
        *,
        wait_for_job_id: str | None = None,
        wait_seconds: int = 0,
    ) -> CaseQueryResponse:
        data = _success_data(
            self._mcp.call_tool(
                "problem_locator_get_case",
                {
                    "case_id": case_id,
                    "wait_for_job_id": wait_for_job_id,
                    "wait_seconds": wait_seconds,
                },
            )
        )
        return _validate_model(
            CaseQueryResponse,
            data,
            "case response does not match S00",
        )

    def resume_case(
        self,
        case_id: str,
        expected_case_revision: int,
        *,
        wait_seconds: int = 0,
    ) -> ApplicationResponse:
        data = _success_data(
            self._mcp.call_tool(
                "problem_locator_resume_case",
                {
                    "request_id": self._ids.new("request"),
                    "case_id": case_id,
                    "expected_case_revision": expected_case_revision,
                    "wait_seconds": wait_seconds,
                },
            )
        )
        return _validate_model(
            ApplicationResponse,
            data,
            "resume response does not match S00",
        )

    def cancel_case(
        self,
        case_id: str,
        expected_case_revision: int,
    ) -> ApplicationResponse:
        data = _success_data(
            self._mcp.call_tool(
                "problem_locator_cancel_case",
                {
                    "request_id": self._ids.new("request"),
                    "case_id": case_id,
                    "expected_case_revision": expected_case_revision,
                },
            )
        )
        return _validate_model(
            ApplicationResponse,
            data,
            "cancel response does not match S00",
        )

    def submit_supplement(
        self,
        *,
        case_id: str,
        expected_case_revision: int,
        inputs: Mapping[str, str],
        attachment_ids: Sequence[str] = (),
        wait_seconds: int = 0,
    ) -> ApplicationResponse:
        """Submit facts/READY attachments, refreshing one stale revision once."""

        request_id = self._ids.new("request")
        arguments = {
            "request_id": request_id,
            "case_id": case_id,
            "expected_case_revision": expected_case_revision,
            "inputs": dict(inputs),
            "attachment_ids": list(attachment_ids),
            "wait_seconds": wait_seconds,
        }
        try:
            data = _success_data(
                self._mcp.call_tool(
                    "problem_locator_submit_supplement",
                    arguments,
                )
            )
        except ClientOperationError as exc:
            if exc.error.code is not ErrorCode.REVISION_CONFLICT:
                raise
            refreshed = self.get_case(case_id)
            arguments["expected_case_revision"] = refreshed.case_view.case_revision
            data = _success_data(
                self._mcp.call_tool(
                    "problem_locator_submit_supplement",
                    arguments,
                )
            )
        return _validate_model(
            ApplicationResponse,
            data,
            "submit response does not match S00",
        )

    def prepare_upload_and_submit(
        self,
        *,
        case_id: str,
        expected_case_revision: int,
        requirement_inputs: Mapping[str, str],
        local_path: Path,
        content_type: str,
        wait_seconds: int = 0,
    ) -> ApplicationResponse:
        prepare_request_id = self._ids.new("request")
        prepare_data = _success_data(
            self._mcp.call_tool(
                "problem_locator_prepare_attachment",
                {
                    "request_id": prepare_request_id,
                    "case_id": case_id,
                    "expected_case_revision": expected_case_revision,
                    "name": local_path.name,
                    "content_type": content_type,
                    "declared_size": None,
                    "declared_sha256": None,
                },
            )
        )
        if not isinstance(prepare_data, Mapping) or set(prepare_data) != {
            "application_response",
            "upload",
        }:
            raise ClientProtocolError("prepare response has unexpected fields")
        _validate_model(
            ApplicationResponse,
            prepare_data["application_response"],
            "prepare response does not match S00",
        )
        descriptor = _validate_model(
            UploadDescriptor,
            prepare_data["upload"],
            "upload descriptor does not match S00",
        )

        required_headers = dict(descriptor.required_headers)
        if required_headers["Content-Type"] != content_type:
            raise ClientProtocolError("upload descriptor changed the requested Content-Type")
        size, digest = _file_size_and_sha256(local_path, hash_required=True)
        assert digest is not None
        if size > descriptor.max_bytes:
            raise ClientProtocolError("local file exceeds the upload descriptor limit")
        declared_length = required_headers["Content-Length"]
        if declared_length is not None and int(declared_length) != size:
            raise ClientProtocolError("local file size differs from the upload descriptor")
        required_headers["Content-Length"] = str(size)
        declared_digest = required_headers["X-Content-SHA256"]
        if declared_digest is not None and declared_digest != digest:
            raise ClientProtocolError("local file hash differs from the upload descriptor")
        if declared_digest is None:
            required_headers["X-Content-SHA256"] = digest
        typed_headers = {
            name: value
            for name, value in required_headers.items()
            if value is not None
        }
        upload_argv = [
            "--globoff",
            "--fail-with-body",
            "--silent",
            "--show-error",
            "--max-filesize",
            str(MAX_CURL_JSON_BYTES),
            "--request",
            "PUT",
            *_header_argv(typed_headers),
            "--upload-file",
            os.fspath(local_path),
            "--",
            _http_transfer_url(descriptor.url),
        ]
        upload_envelope = self._curl.run(upload_argv, expect_json=True)
        if upload_envelope is None:
            raise ClientProtocolError("upload returned no JSON response")
        upload_data = _success_data(upload_envelope)
        if not isinstance(upload_data, Mapping) or set(upload_data) != {
            "attachment_id",
            "case_id",
            "status",
            "case_revision",
        }:
            raise ClientProtocolError("upload response has unexpected fields")
        if (
            _validate_type(
                OpaqueId,
                upload_data["attachment_id"],
                "upload response identifiers do not match S00",
            )
            != descriptor.attachment_id
            or _validate_type(
                OpaqueId,
                upload_data["case_id"],
                "upload response identifiers do not match S00",
            )
            != case_id
            or upload_data["status"] != "READY"
        ):
            raise ClientProtocolError("upload response does not match the prepared attachment")
        upload_revision = _validate_type(
            PositiveInt,
            upload_data["case_revision"],
            "upload response revision does not match S00",
        )

        return self.submit_supplement(
            case_id=case_id,
            expected_case_revision=upload_revision,
            inputs=requirement_inputs,
            attachment_ids=[descriptor.attachment_id],
            wait_seconds=wait_seconds,
        )

    def list_artifacts(self, case_id: str) -> list[ArtifactView]:
        data = _success_data(
            self._mcp.call_tool(
                "problem_locator_list_artifacts",
                {"case_id": case_id},
            )
        )
        if not isinstance(data, Mapping) or set(data) != {"artifacts"}:
            raise ClientProtocolError("artifact response has unexpected fields")
        return _validate_type(
            list[ArtifactView],
            data["artifacts"],
            "artifact response does not match S00",
        )

    def download_artifact(
        self,
        *,
        case_id: str,
        artifact_id: str,
        destination: Path,
    ) -> ArtifactView:
        if destination.exists():
            raise FileExistsError("download destination already exists")
        artifacts = self.list_artifacts(case_id)
        selected = next(
            (item for item in artifacts if item.artifact_id == artifact_id),
            None,
        )
        if selected is None:
            raise ClientProtocolError("artifact was not returned by the service")

        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            argv = [
                "--globoff",
                "--fail",
                "--silent",
                "--show-error",
                "--max-filesize",
                str(selected.size),
                "--request",
                "GET",
                "--",
                _http_transfer_url(selected.download_url),
            ]
            self._curl.download(
                argv,
                destination=temporary,
                max_bytes=selected.size,
            )
            size, digest = _file_size_and_sha256(temporary, hash_required=True)
            if size != selected.size or digest != selected.sha256:
                raise ClientProtocolError("downloaded artifact failed size or hash validation")
            # The initial existence check is user-friendly; the hard link is
            # the race-safe no-clobber gate.  Because the temporary file lives
            # beside the destination this publication is atomic and never
            # overwrites a path created concurrently.
            os.link(temporary, destination)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return selected


__all__ = [
    "CLIENT_FILE_CHUNK_BYTES",
    "CURL_READ_CHUNK_BYTES",
    "ClientAccessWorkflow",
    "ClientOperationError",
    "ClientProtocolError",
    "CurlPort",
    "McpClientPort",
    "SystemCurl",
]
