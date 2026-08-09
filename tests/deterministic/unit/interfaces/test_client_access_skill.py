from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import BinaryIO

import pytest

from problem_locator.contracts.commands import (
    ArtifactView,
    CaseQueryResponse,
    UploadDescriptor,
)
from problem_locator.contracts.enums import ArtifactKind, ErrorCode
from problem_locator.contracts.limits import MAX_ATTACHMENT_BYTES
from problem_locator.contracts.models import ApplicationError
from problem_locator.interfaces import client_access
from problem_locator.interfaces.client_access import (
    ClientAccessWorkflow,
    ClientProtocolError,
    SystemCurl,
)
from tests.deterministic.unit.interfaces.fakes import (
    FakeCurl,
    FakeMcpClient,
    FixedIds,
    envelope,
    error_envelope,
)
from tests.deterministic.unit.interfaces.helpers import (
    ARTIFACT_ID,
    ATTACHMENT_ID,
    CASE_ID,
    FIXED_TIME,
    application_response,
    case_view,
    problem_spec_input,
)


REQUEST_1 = "10000000-0000-0000-0000-000000000001"
REQUEST_2 = "10000000-0000-0000-0000-000000000002"


class _GeneratedPipe:
    def __init__(self, total_bytes: int, *, byte: bytes = b"x") -> None:
        self.remaining = total_bytes
        self.byte = byte
        self.bytes_read = 0
        self.closed = False

    def read(self, max_bytes: int = -1) -> bytes:
        if self.remaining == 0:
            return b""
        amount = self.remaining if max_bytes < 0 else min(self.remaining, max_bytes)
        self.remaining -= amount
        self.bytes_read += amount
        return self.byte * amount

    def close(self) -> None:
        self.closed = True


class _BytesPipe(_GeneratedPipe):
    def __init__(self, value: bytes) -> None:
        self._value = value
        self._offset = 0
        super().__init__(len(value))

    def read(self, max_bytes: int = -1) -> bytes:
        if self._offset == len(self._value):
            return b""
        end = len(self._value) if max_bytes < 0 else self._offset + max_bytes
        chunk = self._value[self._offset : end]
        self._offset += len(chunk)
        self.remaining -= len(chunk)
        self.bytes_read += len(chunk)
        return chunk


class _FakeCurlProcess:
    def __init__(self, stdout: BinaryIO, *, return_code: int = 0) -> None:
        self.stdout = stdout
        self._planned_return_code = return_code
        self.returncode: int | None = None
        self.killed = False
        self.wait_calls = 0

    def wait(self) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = self._planned_return_code
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _InterruptingPipe(_GeneratedPipe):
    def __init__(self) -> None:
        super().__init__(1)

    def read(self, max_bytes: int = -1) -> bytes:
        raise KeyboardInterrupt


class _InterruptingWaitProcess(_FakeCurlProcess):
    def __init__(self, stdout: BinaryIO) -> None:
        super().__init__(stdout)
        self._interrupted = False

    def wait(self) -> int:
        self.wait_calls += 1
        if not self._interrupted:
            self._interrupted = True
            raise KeyboardInterrupt
        if self.returncode is None:
            self.returncode = self._planned_return_code
        return self.returncode


def _upload_descriptor(
    *,
    length: str | None = None,
    digest: str | None = None,
    url: str | None = None,
) -> UploadDescriptor:
    return UploadDescriptor(
        attachment_id=ATTACHMENT_ID,
        method="PUT",
        url=url
        or f"https://service.example.test/api/v1/attachments/{ATTACHMENT_ID}/content",
        required_headers={
            "Idempotency-Key": ATTACHMENT_ID,
            "Content-Type": "application/zip",
            "Content-Length": length,
            "X-Content-SHA256": digest,
        },
        max_bytes=MAX_ATTACHMENT_BYTES,
        expires_at=None,
    )


def test_skill_document_names_tools_and_safety_invariants() -> None:
    skill = (
        Path(__file__).parents[4]
        / ".claude"
        / "skills"
        / "problem-locator-client"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    tool_names = (
        "problem_locator_create_case",
        "problem_locator_prepare_attachment",
        "problem_locator_submit_supplement",
        "problem_locator_get_case",
        "problem_locator_resume_case",
        "problem_locator_cancel_case",
        "problem_locator_list_artifacts",
    )
    for tool in tool_names:
        assert skill.count(f"`{tool}`") >= 1
    assert "READY" in skill and 'not “adopted' in skill
    assert "Never overwrite automatically" in skill
    assert "storage keys" in skill
    assert "argument array" in skill
    assert "durable business receipt" in skill
    assert "`case_view` is null" in skill
    assert ".tar.gz" in skill and "uppercase archive suffixes" in skill
    assert "do not ask for a Logparse archive Content-Type" in skill
    assert "Derive it from the canonical lowercase filename suffix" in skill
    assert "run a local MCP server or proxy" in skill
    assert "does not install the `problem-locator` package" in skill
    assert "Version 1.0.5 exposes only flat MCP input schemas" in skill
    assert '"input_names": ["order_id"]' in skill
    assert '"input_values": ["order-1"]' in skill
    assert "`name` and `declared_size`" in skill
    assert "`attachment_name` or `declared_byte_count`" in skill
    assert '"statement": "<problem statement>"' in skill
    assert '"initial_user_fact_names": ["<requirement_name>"]' in skill
    assert '"initial_user_fact_values": ["<exact string value>"]' in skill
    assert '"problem_spec": {' not in skill
    assert '"wait_for_job_id": null' in skill
    assert "Only `declared_size`, `declared_sha256`, and `wait_for_job_id`" in skill

    config = (
        Path(__file__).parents[4]
        / ".claude"
        / "skills"
        / "problem-locator-client"
        / "references"
        / "client-mcp-config.json"
    )
    assert config.is_file()
    parsed_config = json.loads(config.read_text(encoding="utf-8"))
    remote = parsed_config["mcpServers"]["problem-locator"]
    assert remote == {"type": "http", "url": "${PROBLEM_LOCATOR_MCP_URL}"}

    hooks_path = config.with_name("client-hooks-settings.json")
    assert not hooks_path.exists()
    scripts = config.parent.parent / "scripts"
    assert not list(scripts.glob("problem-locator-client-*.ps1"))

    readme = (Path(__file__).parents[4] / "README.md").read_text(encoding="utf-8")
    assert "客户端远端 MCP 配置" in readme
    assert "客户端不安装 `problem-locator`" in readme
    assert '"type": "http"' in readme
    assert '"url": "${PROBLEM_LOCATOR_MCP_URL}"' in readme
    assert "NO_PROXY" in readme
    assert "七个公开 MCP input schema 全部扁平化" in readme
    assert "initial_user_fact_names/initial_user_fact_values" in readme
    assert "input_names/input_values" in readme
    assert "PROBLEM_LOCATOR_NATIVE_CLIENT_LINUX_GATE" in readme
    assert "PROBLEM_LOCATOR_REAL_HOST_FLAT_GATE" in readme
    assert "executable hash" in readme
    assert "版本不在文档中写死" in readme
    assert "不得新增 `$ref/$defs`" in readme
    assert "PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE" not in readme
    assert "client.hook.tool.started" not in readme
    assert "服务端日志不需要安装额外组件" in readme
    assert "tail -f /var/log/problem-locator/debug.jsonl" in readme


def test_create_case_uses_one_stable_generated_request_id() -> None:
    response = application_response(with_case_view=False)
    mcp = FakeMcpClient([envelope(response)])
    workflow = ClientAccessWorkflow(mcp, FakeCurl(), FixedIds([REQUEST_1]))

    actual = workflow.create_case(
        problem_spec=problem_spec_input(),
        initial_user_facts=(
            {"name": "host", "value": "node-1"},
            {"name": "region", "value": "华北"},
        ),
        wait_seconds=30,
    )

    assert actual == response
    assert actual.case_view is None
    assert actual.business_receipt.case_revision == 1
    assert mcp.calls == [
        (
            "problem_locator_create_case",
            {
                "request_id": REQUEST_1,
                **problem_spec_input(),
                "initial_user_fact_names": ["host", "region"],
                "initial_user_fact_values": ["node-1", "华北"],
                "wait_seconds": 30,
            },
        )
    ]


def test_upload_uses_safe_argv_latest_revision_and_explicit_submit(tmp_path: Path) -> None:
    local_path = tmp_path / "日志 ;$() 'quoted'.zip"
    payload = b"safe fixture bytes"
    local_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    prepare_response = application_response(
        operation="PrepareAttachment",
        primary_resource_id=ATTACHMENT_ID,
        revision=2,
    )
    descriptor = _upload_descriptor()
    submit_response = application_response(
        operation="SubmitSupplement",
        revision=4,
    )
    mcp = FakeMcpClient(
        [
            envelope(
                {
                    "application_response": prepare_response.model_dump(mode="json"),
                    "upload": descriptor.model_dump(mode="json"),
                }
            ),
            envelope(submit_response),
        ]
    )
    curl = FakeCurl(
        [
            envelope(
                {
                    "attachment_id": ATTACHMENT_ID,
                    "case_id": CASE_ID,
                    "status": "READY",
                    "case_revision": 3,
                }
            )
        ]
    )
    workflow = ClientAccessWorkflow(mcp, curl, FixedIds([REQUEST_1, REQUEST_2]))

    actual = workflow.prepare_upload_and_submit(
        case_id=CASE_ID,
        expected_case_revision=1,
        requirement_inputs={},
        local_path=local_path,
    )

    assert actual == submit_response
    assert [name for name, _ in mcp.calls] == [
        "problem_locator_prepare_attachment",
        "problem_locator_submit_supplement",
    ]
    assert mcp.calls[0][1]["content_type"] == "application/zip"
    submit_arguments = mcp.calls[-1][1]
    assert submit_arguments["request_id"] == REQUEST_2
    assert submit_arguments["expected_case_revision"] == 3
    assert submit_arguments["attachment_ids"] == [ATTACHMENT_ID]

    argv, expect_json = curl.calls[0]
    assert expect_json is True
    assert "--upload-file" in argv
    assert argv[argv.index("--upload-file") + 1] == str(local_path)
    assert f"Idempotency-Key: {ATTACHMENT_ID}" in argv
    assert f"Content-Length: {len(payload)}" in argv
    assert f"X-Content-SHA256: {digest}" in argv
    assert "--globoff" in argv
    assert argv[argv.index("--max-filesize") + 1] == str(
        client_access.MAX_CURL_JSON_BYTES
    )
    assert argv[-2:] == ["--", descriptor.url]
    assert not any("curl " in argument for argument in argv)


def test_unsupported_archive_suffix_stops_before_prepare(tmp_path: Path) -> None:
    local_path = tmp_path / "logs.rar"
    local_path.write_bytes(b"archive bytes")
    mcp = FakeMcpClient([])
    workflow = ClientAccessWorkflow(mcp, FakeCurl(), FixedIds([]))

    with pytest.raises(ValueError, match="unsupported attachment suffix"):
        workflow.prepare_upload_and_submit(
            case_id=CASE_ID,
            expected_case_revision=1,
            requirement_inputs={},
            local_path=local_path,
        )

    assert mcp.calls == []


def test_remote_archive_name_validation_stops_before_upload(tmp_path: Path) -> None:
    local_path = tmp_path / "logs.ZIP"
    local_path.write_bytes(b"archive bytes")
    error = ApplicationError(
        code=ErrorCode.VALIDATION_ERROR,
        message="Archive filename suffix does not match Content-Type.",
        details=[],
        retryable=False,
    )
    mcp = FakeMcpClient([error_envelope(error)])
    curl = FakeCurl()
    workflow = ClientAccessWorkflow(mcp, curl, FixedIds([REQUEST_1]))

    with pytest.raises(client_access.ClientOperationError) as caught:
        workflow.prepare_upload_and_submit(
            case_id=CASE_ID,
            expected_case_revision=1,
            requirement_inputs={},
            local_path=local_path,
            content_type="application/zip",
        )

    assert caught.value.error == error
    assert mcp.calls[0][1]["name"] == "logs.ZIP"
    assert curl.calls == []


def test_submit_revision_conflict_refreshes_and_reuses_submit_request_id(
    tmp_path: Path,
) -> None:
    local_path = tmp_path / "logs.zip"
    local_path.write_bytes(b"one chunk")
    conflict = ApplicationError(
        code=ErrorCode.REVISION_CONFLICT,
        message="Case revision changed.",
        details=[],
        retryable=True,
    )
    submit_response = application_response(operation="SubmitSupplement", revision=6)
    mcp = FakeMcpClient(
        [
            envelope(
                {
                    "application_response": application_response(
                        operation="PrepareAttachment",
                        primary_resource_id=ATTACHMENT_ID,
                        revision=2,
                    ).model_dump(mode="json"),
                    "upload": _upload_descriptor().model_dump(mode="json"),
                }
            ),
            error_envelope(conflict),
            envelope(
                CaseQueryResponse(
                    case_view=case_view(revision=5),
                    wait_timed_out=False,
                )
            ),
            envelope(submit_response),
        ]
    )
    curl = FakeCurl(
        [
            envelope(
                {
                    "attachment_id": ATTACHMENT_ID,
                    "case_id": CASE_ID,
                    "status": "READY",
                    "case_revision": 3,
                }
            )
        ]
    )
    workflow = ClientAccessWorkflow(mcp, curl, FixedIds([REQUEST_1, REQUEST_2]))

    actual = workflow.prepare_upload_and_submit(
        case_id=CASE_ID,
        expected_case_revision=1,
        requirement_inputs={"order_id": "order-1"},
        local_path=local_path,
        content_type="application/zip",
    )

    assert actual == submit_response
    assert [name for name, _ in mcp.calls] == [
        "problem_locator_prepare_attachment",
        "problem_locator_submit_supplement",
        "problem_locator_get_case",
        "problem_locator_submit_supplement",
    ]
    first_submit = mcp.calls[1][1]
    retried_submit = mcp.calls[3][1]
    assert first_submit["request_id"] == retried_submit["request_id"] == REQUEST_2
    assert first_submit["expected_case_revision"] == 3
    assert retried_submit["expected_case_revision"] == 5


def test_get_resume_and_cancel_use_only_frozen_tools_and_fresh_write_ids() -> None:
    get_response = CaseQueryResponse(case_view=case_view(revision=2), wait_timed_out=True)
    resume_response = application_response(operation="ResumeCase", revision=3)
    cancel_response = application_response(operation="CancelCase", revision=4)
    mcp = FakeMcpClient(
        [envelope(get_response), envelope(resume_response), envelope(cancel_response)]
    )
    workflow = ClientAccessWorkflow(
        mcp,
        FakeCurl(),
        FixedIds([REQUEST_1, REQUEST_2]),
    )

    assert workflow.get_case(CASE_ID, wait_seconds=30) == get_response
    assert workflow.resume_case(CASE_ID, 2) == resume_response
    assert workflow.cancel_case(CASE_ID, 3) == cancel_response

    assert mcp.calls == [
        (
            "problem_locator_get_case",
            {"case_id": CASE_ID, "wait_for_job_id": None, "wait_seconds": 30},
        ),
        (
            "problem_locator_resume_case",
            {
                "request_id": REQUEST_1,
                "case_id": CASE_ID,
                "expected_case_revision": 2,
                "wait_seconds": 0,
            },
        ),
        (
            "problem_locator_cancel_case",
            {
                "request_id": REQUEST_2,
                "case_id": CASE_ID,
                "expected_case_revision": 3,
            },
        ),
    ]


def test_submit_structured_facts_uses_one_stable_write_request() -> None:
    response = application_response(operation="SubmitSupplement", revision=3)
    mcp = FakeMcpClient([envelope(response)])
    workflow = ClientAccessWorkflow(mcp, FakeCurl(), FixedIds([REQUEST_1]))

    actual = workflow.submit_supplement(
        case_id=CASE_ID,
        expected_case_revision=2,
        inputs={"order_id": "order-1", "region": "华北"},
        wait_seconds=30,
    )

    assert actual == response
    assert mcp.calls == [
        (
            "problem_locator_submit_supplement",
            {
                "request_id": REQUEST_1,
                "case_id": CASE_ID,
                "expected_case_revision": 2,
                "input_names": ["order_id", "region"],
                "input_values": ["order-1", "华北"],
                "attachment_ids": [],
                "wait_seconds": 30,
            },
        )
    ]


def test_non_revision_service_error_is_preserved_without_retry() -> None:
    error = ApplicationError(
        code=ErrorCode.INVALID_CASE_STATE,
        message="Case does not accept supplements.",
        details=[],
        retryable=False,
    )
    mcp = FakeMcpClient([error_envelope(error)])
    workflow = ClientAccessWorkflow(mcp, FakeCurl(), FixedIds([REQUEST_1]))

    with pytest.raises(client_access.ClientOperationError) as caught:
        workflow.submit_supplement(
            case_id=CASE_ID,
            expected_case_revision=2,
            inputs={"order_id": "order-1"},
        )

    assert caught.value.error == error
    assert len(mcp.calls) == 1


def test_upload_rejects_declared_hash_mismatch_before_curl(tmp_path: Path) -> None:
    local_path = tmp_path / "logs.zip"
    local_path.write_bytes(b"actual")
    prepare = {
        "application_response": application_response(
            operation="PrepareAttachment",
            primary_resource_id=ATTACHMENT_ID,
            revision=2,
        ).model_dump(mode="json"),
        "upload": _upload_descriptor(
            length=str(len(b"actual")),
            digest="a" * 64,
        ).model_dump(mode="json"),
    }
    curl = FakeCurl()
    workflow = ClientAccessWorkflow(
        FakeMcpClient([envelope(prepare)]),
        curl,
        FixedIds([REQUEST_1]),
    )

    with pytest.raises(ClientProtocolError, match="hash"):
        workflow.prepare_upload_and_submit(
            case_id=CASE_ID,
            expected_case_revision=1,
            requirement_inputs={},
            local_path=local_path,
            content_type="application/zip",
        )
    assert curl.calls == []


def test_upload_rejects_counted_size_above_exact_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_path = tmp_path / "logical-large.zip"
    local_path.write_bytes(b"fixture")
    prepare = {
        "application_response": application_response(
            operation="PrepareAttachment",
            primary_resource_id=ATTACHMENT_ID,
            revision=2,
        ).model_dump(mode="json"),
        "upload": _upload_descriptor().model_dump(mode="json"),
    }
    monkeypatch.setattr(
        client_access,
        "_file_size_and_sha256",
        lambda _path, *, hash_required: (MAX_ATTACHMENT_BYTES + 1, "a" * 64),
    )
    curl = FakeCurl()
    workflow = ClientAccessWorkflow(
        FakeMcpClient([envelope(prepare)]),
        curl,
        FixedIds([REQUEST_1]),
    )

    with pytest.raises(ClientProtocolError, match="limit"):
        workflow.prepare_upload_and_submit(
            case_id=CASE_ID,
            expected_case_revision=1,
            requirement_inputs={},
            local_path=local_path,
            content_type="application/zip",
        )
    assert curl.calls == []


def test_download_refuses_existing_target_before_curl(tmp_path: Path) -> None:
    target = tmp_path / "diagnosis.json"
    target.write_text("keep me", encoding="utf-8")
    curl = FakeCurl()
    workflow = ClientAccessWorkflow(FakeMcpClient([]), curl, FixedIds([]))

    with pytest.raises(FileExistsError):
        workflow.download_artifact(
            case_id=CASE_ID,
            artifact_id=ARTIFACT_ID,
            destination=target,
        )
    assert target.read_text(encoding="utf-8") == "keep me"
    assert curl.calls == []
    assert curl.download_calls == []


def test_download_uses_only_listed_url_and_verifies_bytes(tmp_path: Path) -> None:
    payload = b"result\n"
    view = ArtifactView(
        artifact_id=ARTIFACT_ID,
        kind=ArtifactKind.USER_RESULT,
        name="diagnosis.json",
        content_type="application/json",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        created_at=FIXED_TIME,
        download_url="https://download.example.test/opaque/result?case=fixed",
    )
    mcp = FakeMcpClient([envelope({"artifacts": [view.model_dump(mode="json")]})])
    curl = FakeCurl([None])
    curl.download_bytes = payload
    workflow = ClientAccessWorkflow(mcp, curl, FixedIds([]))
    destination = tmp_path / "new result.json"

    selected = workflow.download_artifact(
        case_id=CASE_ID,
        artifact_id=ARTIFACT_ID,
        destination=destination,
    )

    assert selected == view
    assert destination.read_bytes() == payload
    argv, output_path, max_bytes = curl.download_calls[0]
    assert output_path.parent == destination.parent
    assert max_bytes == len(payload)
    assert "--globoff" in argv
    assert argv[argv.index("--max-filesize") + 1] == str(len(payload))
    assert argv[-1] == view.download_url
    assert ARTIFACT_ID not in " ".join(argv[:-1])


def test_download_hash_failure_leaves_no_destination_or_temporary_file(
    tmp_path: Path,
) -> None:
    view = ArtifactView(
        artifact_id=ARTIFACT_ID,
        kind=ArtifactKind.USER_RESULT,
        name="diagnosis.json",
        content_type="application/json",
        size=4,
        sha256=hashlib.sha256(b"good").hexdigest(),
        created_at=FIXED_TIME,
        download_url="https://download.example.test/result",
    )
    curl = FakeCurl([None])
    curl.download_bytes = b"evil"
    workflow = ClientAccessWorkflow(
        FakeMcpClient([envelope({"artifacts": [view.model_dump(mode="json")]})]),
        curl,
        FixedIds([]),
    )
    destination = tmp_path / "result.json"

    with pytest.raises(ClientProtocolError, match="hash"):
        workflow.download_artifact(
            case_id=CASE_ID,
            artifact_id=ARTIFACT_ID,
            destination=destination,
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".result.json.*.tmp")) == []


def test_download_rejects_non_http_service_url_before_curl(tmp_path: Path) -> None:
    view = ArtifactView(
        artifact_id=ARTIFACT_ID,
        kind=ArtifactKind.USER_RESULT,
        name="diagnosis.json",
        content_type="application/json",
        size=0,
        sha256=hashlib.sha256(b"").hexdigest(),
        created_at=FIXED_TIME,
        download_url="file:///private/internal-result.json",
    )
    curl = FakeCurl()
    workflow = ClientAccessWorkflow(
        FakeMcpClient([envelope({"artifacts": [view.model_dump(mode="json")]})]),
        curl,
        FixedIds([]),
    )

    with pytest.raises(ClientProtocolError, match="transfer URL"):
        workflow.download_artifact(
            case_id=CASE_ID,
            artifact_id=ARTIFACT_ID,
            destination=tmp_path / "result.json",
        )
    assert curl.calls == []


def test_download_publication_does_not_clobber_concurrent_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"result"
    view = ArtifactView(
        artifact_id=ARTIFACT_ID,
        kind=ArtifactKind.USER_RESULT,
        name="diagnosis.json",
        content_type="application/json",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        created_at=FIXED_TIME,
        download_url="https://download.example.test/result",
    )
    curl = FakeCurl([None])
    curl.download_bytes = payload
    destination = tmp_path / "result.json"

    def concurrent_link(_source: Path, target: Path) -> None:
        Path(target).write_bytes(b"other writer")
        raise FileExistsError(target)

    monkeypatch.setattr(client_access.os, "link", concurrent_link)
    workflow = ClientAccessWorkflow(
        FakeMcpClient([envelope({"artifacts": [view.model_dump(mode="json")]})]),
        curl,
        FixedIds([]),
    )

    with pytest.raises(FileExistsError):
        workflow.download_artifact(
            case_id=CASE_ID,
            artifact_id=ARTIFACT_ID,
            destination=destination,
        )
    assert destination.read_bytes() == b"other writer"


def test_system_curl_returns_http_error_envelope_without_exposing_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ApplicationError(
        code=ErrorCode.RESOURCE_HASH_MISMATCH,
        message="Resource publication validation failed.",
        details=[],
        retryable=False,
    )
    body = error_envelope(error)
    process = _FakeCurlProcess(
        _BytesPipe(json.dumps(body).encode("utf-8")),
        return_code=client_access.CURL_HTTP_ERROR_RETURN_CODE,
    )
    start_kwargs: dict[str, object] = {}

    def start(*args: object, **kwargs: object) -> _FakeCurlProcess:
        start_kwargs.update(kwargs)
        return process

    monkeypatch.setattr(
        client_access.subprocess,
        "Popen",
        start,
    )

    assert SystemCurl().run(["--fail-with-body", "https://example.test"], expect_json=True) == body
    assert start_kwargs["stderr"] is client_access.subprocess.DEVNULL


@pytest.mark.parametrize("return_code", [1, 7, 63])
def test_system_curl_rejects_valid_json_from_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
    return_code: int,
) -> None:
    body = envelope({"status": "unexpected"})
    process = _FakeCurlProcess(
        _BytesPipe(json.dumps(body).encode("utf-8")),
        return_code=return_code,
    )
    monkeypatch.setattr(client_access.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(ClientProtocolError, match="transport failed"):
        SystemCurl().run(["https://example.test"], expect_json=True)


def test_system_curl_stops_reading_unknown_length_oversized_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipe = _GeneratedPipe(client_access.MAX_CURL_JSON_BYTES * 100)
    process = _FakeCurlProcess(pipe)
    monkeypatch.setattr(client_access.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(ClientProtocolError, match="oversized JSON"):
        SystemCurl().run(["https://example.test"], expect_json=True)

    assert pipe.bytes_read == client_access.MAX_CURL_JSON_BYTES + 1
    assert process.killed is True
    assert pipe.closed is True


def test_system_curl_reaps_child_when_json_read_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipe = _InterruptingPipe()
    process = _InterruptingWaitProcess(pipe)
    monkeypatch.setattr(client_access.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(KeyboardInterrupt):
        SystemCurl().run(["https://example.test"], expect_json=True)

    assert process.killed is True
    assert process.wait_calls == 2
    assert pipe.closed is True


def test_system_curl_reaps_child_when_no_output_wait_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _InterruptingWaitProcess(_BytesPipe(b""))
    monkeypatch.setattr(client_access.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(KeyboardInterrupt):
        SystemCurl().run(["https://example.test"], expect_json=False)

    assert process.killed is True
    assert process.wait_calls == 2


@pytest.mark.parametrize(
    ("payload", "max_bytes"),
    [(b"", 0), (b"four", 4)],
)
def test_system_curl_download_accepts_exact_declared_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    max_bytes: int,
) -> None:
    process = _FakeCurlProcess(_BytesPipe(payload))
    monkeypatch.setattr(client_access.subprocess, "Popen", lambda *args, **kwargs: process)
    temporary = tmp_path / "bounded.tmp"

    SystemCurl().download(
        ["https://example.test/result"],
        destination=temporary,
        max_bytes=max_bytes,
    )

    assert temporary.read_bytes() == payload
    assert process.killed is False


def test_system_curl_reaps_child_when_download_wait_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipe = _BytesPipe(b"four")
    process = _InterruptingWaitProcess(pipe)
    monkeypatch.setattr(client_access.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(KeyboardInterrupt):
        SystemCurl().download(
            ["https://example.test/result"],
            destination=tmp_path / "bounded.tmp",
            max_bytes=4,
        )

    assert process.killed is True
    assert process.wait_calls == 2
    assert pipe.closed is True


def test_system_curl_download_stops_before_writing_past_declared_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipe = _GeneratedPipe(client_access.CLIENT_FILE_CHUNK_BYTES * 100)
    process = _FakeCurlProcess(pipe)
    monkeypatch.setattr(client_access.subprocess, "Popen", lambda *args, **kwargs: process)
    temporary = tmp_path / "bounded.tmp"

    with pytest.raises(ClientProtocolError, match="declared size"):
        SystemCurl().download(
            ["--max-filesize", "4", "https://example.test/result"],
            destination=temporary,
            max_bytes=4,
        )

    assert temporary.stat().st_size <= 4
    assert pipe.bytes_read == 5
    assert process.killed is True
    assert pipe.closed is True


def test_download_aborts_when_fake_service_exceeds_listed_size(tmp_path: Path) -> None:
    view = ArtifactView(
        artifact_id=ARTIFACT_ID,
        kind=ArtifactKind.USER_RESULT,
        name="diagnosis.json",
        content_type="application/json",
        size=4,
        sha256=hashlib.sha256(b"good").hexdigest(),
        created_at=FIXED_TIME,
        download_url="https://download.example.test/result[1]",
    )
    curl = FakeCurl()
    curl.download_bytes = b"five!"
    workflow = ClientAccessWorkflow(
        FakeMcpClient([envelope({"artifacts": [view.model_dump(mode="json")]})]),
        curl,
        FixedIds([]),
    )
    destination = tmp_path / "result.json"

    with pytest.raises(ClientProtocolError, match="declared size"):
        workflow.download_artifact(
            case_id=CASE_ID,
            artifact_id=ARTIFACT_ID,
            destination=destination,
        )

    assert not destination.exists()
    argv, _, max_bytes = curl.download_calls[0]
    assert "--globoff" in argv
    assert max_bytes == view.size
    assert list(tmp_path.glob(".result.json.*.tmp")) == []
