from __future__ import annotations

import copy
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from problem_locator.contracts.commands import (
    ApplicationCommand,
    ApplicationResponse,
    ArtifactListResponse,
    CaseQueryResponse,
    GetCase,
    ListArtifacts,
    OpenArtifact,
    OpenArtifactResult,
)
from problem_locator.contracts.enums import ErrorCode
from problem_locator.contracts.errors import (
    ERROR_SPECS,
    PORT_ERROR_CODES,
    ApplicationPortError,
)
from problem_locator.contracts.models import (
    ApplicationError,
    ReadinessReport,
    ValidationReport,
)
from problem_locator.contracts.serialization import business_request_sha256
from problem_locator.interfaces.client_access import ClientProtocolError


def _take(queue: deque[Any], label: str, *args: Any) -> Any:
    if not queue:
        raise AssertionError(f"no scripted result remains for {label}")
    item = queue.popleft()
    if isinstance(item, BaseException):
        raise item
    if callable(item):
        return item(*args)
    if hasattr(item, "model_copy"):
        return item.model_copy(deep=True)
    return copy.deepcopy(item)


def _port_error(code: ErrorCode, message: str) -> ApplicationPortError:
    return ApplicationPortError(
        ApplicationError(
            code=code,
            message=message,
            details=[],
            retryable=ERROR_SPECS[code].application_retryable,
        )
    )


def _validate_raw_port_input(
    method_key: str,
    model_type: type[BaseModel],
    payload: Mapping[str, Any],
) -> BaseModel:
    """Rebuild raw Port arguments before recording or consuming a script."""

    try:
        return model_type.model_validate(payload, strict=True)
    except (TypeError, ValueError, ValidationError):
        raise _port_error(
            ErrorCode.VALIDATION_ERROR,
            f"{method_key} received invalid raw input.",
        ) from None


def _validate_scripted_port_error(
    method_key: str,
    error: ApplicationPortError,
) -> None:
    if error.error.code not in PORT_ERROR_CODES[method_key]:
        raise ValueError(
            f"{method_key} does not allow {error.error.code.value}"
        ) from error


def _take_port(
    queue: deque[Any],
    method_key: str,
    label: str,
    *args: Any,
) -> Any:
    try:
        return _take(queue, label, *args)
    except ApplicationPortError as error:
        _validate_scripted_port_error(method_key, error)
        raise


class FakeApplicationService:
    def __init__(
        self,
        responses: Sequence[Any] = (),
        *,
        replay_idempotent: bool = False,
    ) -> None:
        self.responses = deque(responses)
        self.calls: list[ApplicationCommand] = []
        self.replay_idempotent = replay_idempotent
        self._replays: dict[
            tuple[str, str],
            tuple[str, ApplicationResponse],
        ] = {}

    def execute(self, command: ApplicationCommand) -> ApplicationResponse:
        # Preserve stream identity for UploadAttachmentContent while retaining
        # full observability for all immutable scalar command fields.
        self.calls.append(command)
        if not self.replay_idempotent:
            return _take_port(
                self.responses,
                "ApplicationCommandPort.execute",
                "execute",
                command,
            )

        key = (type(command).__name__, command.idempotency_key)
        request_hash = business_request_sha256(command)
        prior = self._replays.get(key)
        if prior is not None:
            prior_hash, prior_response = prior
            if request_hash != prior_hash:
                raise ApplicationPortError(
                    ApplicationError(
                        code=ErrorCode.IDEMPOTENCY_CONFLICT,
                        message="Idempotency key was reused with different content.",
                        details=[],
                        retryable=False,
                    )
                )
            return prior_response.model_copy(deep=True)

        response = _take_port(
            self.responses,
            "ApplicationCommandPort.execute",
            "execute",
            command,
        )
        self._replays[key] = (request_hash, response.model_copy(deep=True))
        return response


class FakeQuery:
    def __init__(self) -> None:
        self.results: dict[str, deque[Any]] = {
            "get_case": deque(),
            "list_artifacts": deque(),
            "open_artifact": deque(),
        }
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def queue(self, method: str, *results: Any) -> None:
        self.results[method].extend(results)

    def get_case(
        self,
        case_id: str,
        wait_for_job_id: str | None = None,
        wait_seconds: int = 0,
    ) -> CaseQueryResponse:
        validated = _validate_raw_port_input(
            "ApplicationQueryPort.get_case",
            GetCase,
            {
                "case_id": case_id,
                "wait_for_job_id": wait_for_job_id,
                "wait_seconds": wait_seconds,
            },
        )
        assert isinstance(validated, GetCase)
        args = (
            validated.case_id,
            validated.wait_for_job_id,
            validated.wait_seconds,
        )
        self.calls.append(("get_case", args))
        return _take_port(
            self.results["get_case"],
            "ApplicationQueryPort.get_case",
            "get_case",
            *args,
        )

    def list_artifacts(
        self,
        case_id: str,
        include_internal: bool = False,
    ) -> ArtifactListResponse:
        validated = _validate_raw_port_input(
            "ApplicationQueryPort.list_artifacts",
            ListArtifacts,
            {"case_id": case_id, "include_internal": include_internal},
        )
        assert isinstance(validated, ListArtifacts)
        args = (validated.case_id, validated.include_internal)
        self.calls.append(("list_artifacts", args))
        return _take_port(
            self.results["list_artifacts"],
            "ApplicationQueryPort.list_artifacts",
            "list_artifacts",
            *args,
        )

    def open_artifact(self, case_id: str, artifact_id: str) -> OpenArtifactResult:
        validated = _validate_raw_port_input(
            "ApplicationQueryPort.open_artifact",
            OpenArtifact,
            {"case_id": case_id, "artifact_id": artifact_id},
        )
        assert isinstance(validated, OpenArtifact)
        args = (validated.case_id, validated.artifact_id)
        self.calls.append(("open_artifact", args))
        queue = self.results["open_artifact"]
        if not queue:
            raise AssertionError("no scripted result remains for open_artifact")
        item = queue.popleft()
        if isinstance(item, BaseException):
            if isinstance(item, ApplicationPortError):
                _validate_scripted_port_error(
                    "ApplicationQueryPort.open_artifact",
                    item,
                )
            raise item
        if callable(item):
            try:
                return item(*args)
            except ApplicationPortError as error:
                _validate_scripted_port_error(
                    "ApplicationQueryPort.open_artifact",
                    error,
                )
                raise
        # OpenArtifactResult owns a live stream and must preserve its identity.
        return item


class FakeStateAdmin:
    def __init__(
        self,
        *,
        readiness: ReadinessReport,
        validations: Sequence[Any] = (),
        exports: Sequence[Any] = (),
    ) -> None:
        self._readiness = readiness
        self.validations = deque(validations)
        self.exports = deque(exports)
        self.calls: list[str] = []

    def readiness(self) -> ReadinessReport:
        self.calls.append("readiness")
        return self._readiness.model_copy(deep=True)

    def validate_state(self) -> ValidationReport:
        self.calls.append("validate_state")
        return _take_port(
            self.validations,
            "StateAdminPort.validate_state",
            "validate_state",
        )

    def export_state(self) -> bytes:
        self.calls.append("export_state")
        return _take_port(
            self.exports,
            "StateAdminPort.export_state",
            "export_state",
        )


class FixedIds:
    def __init__(self, values: Sequence[str]) -> None:
        self.values = deque(values)
        self.calls: list[str] = []

    def new(self, kind: str) -> str:
        self.calls.append(kind)
        return self.values.popleft()

    def derive(self, kind: str, stable_parts: Sequence[str]) -> str:
        raise AssertionError("client workflow must not derive business IDs")


class FakeMcpClient:
    def __init__(self, responses: Sequence[Mapping[str, Any]]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((name, copy.deepcopy(dict(arguments))))
        return copy.deepcopy(self.responses.popleft())


class FakeCurl:
    def __init__(self, responses: Sequence[Mapping[str, Any] | None] = ()) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[list[str], bool]] = []
        self.download_calls: list[tuple[list[str], Path, int]] = []
        self.download_bytes: bytes | None = None

    def run(self, argv: Sequence[str], *, expect_json: bool) -> Mapping[str, Any] | None:
        copied = list(argv)
        self.calls.append((copied, expect_json))
        if not self.responses:
            return None
        return copy.deepcopy(self.responses.popleft())

    def download(
        self,
        argv: Sequence[str],
        *,
        destination: Path,
        max_bytes: int,
    ) -> None:
        copied = list(argv)
        self.download_calls.append((copied, destination, max_bytes))
        if self.download_bytes is None:
            return
        if len(self.download_bytes) > max_bytes:
            destination.write_bytes(self.download_bytes[:max_bytes])
            raise ClientProtocolError("downloaded artifact exceeded its declared size")
        destination.write_bytes(self.download_bytes)


class StreamingUploadFixture:
    """Observable async body source with bounded chunks and fault injection."""

    def __init__(
        self,
        chunks: Sequence[bytes],
        *,
        fail_on_chunk: int | None = None,
    ) -> None:
        if any(not isinstance(chunk, bytes) for chunk in chunks):
            raise TypeError("upload fixture chunks must be bytes")
        self.chunks = tuple(chunks)
        self.fail_on_chunk = fail_on_chunk
        self.read_calls = 0
        self.bytes_yielded = 0
        self.closed = False

    async def __aiter__(self):
        try:
            for index, chunk in enumerate(self.chunks, start=1):
                self.read_calls += 1
                if self.fail_on_chunk == index:
                    raise ConnectionError("injected upload body failure")
                self.bytes_yielded += len(chunk)
                yield chunk
        finally:
            self.closed = True


def envelope(data: Any) -> dict[str, Any]:
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    return {"ok": True, "data": data, "error": None}


def error_envelope(error: ApplicationError) -> dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "error": error.model_dump(mode="json"),
    }
