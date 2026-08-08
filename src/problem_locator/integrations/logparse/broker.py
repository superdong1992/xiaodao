"""Job-scoped service-side broker for the pinned logparse installation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import secrets
import stat
import subprocess
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final

from problem_locator.contracts import (
    AgentArtifactProposalDraft,
    ArtifactKind,
    CancellationSignal,
    ErrorCode,
    ExecutionFailure,
    ExecutionStage,
    Job,
    LogparseBrokerError,
    LogparseBrokerFactory,
    LogparseBrokerSession,
    LogparseParseClaim,
    LogparseParseParameters,
    LogparseRunMetadata,
    ResolvedAsset,
    ResourceKind,
    WorkspaceInputManifest,
    canonical_json_bytes,
    parse_canonical_json_bytes,
    validate_workspace_manifest_for_job,
)

from .claim import FaultPoint, create_parse_claim
from .fingerprint import (
    fingerprint_logparse_asset,
    resolve_logparse_configuration,
)
from .outputs import (
    aggregate_target_results,
    inspect_controlled_run,
    normalize_target_result,
)
from .paths import resolve_workspace_path, validate_proposal_io_paths
from .process import ProcessResult, SubprocessExecutor, terminate_process_tree
from .requests import (
    Anchor,
    BrokerEnvelope,
    ParseTargetsRequest,
    ResolvedLogparsePlan,
    TargetLogsRequest,
)
from .workspace import (
    bind_attachment,
    bind_logparse_run,
    has_logparse_run,
    load_workspace_manifest,
)


_ENDPOINT_ENV: Final = "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT"
_TOKEN_ENV: Final = "PROBLEM_LOCATOR_LOGPARSE_TOKEN"
_TOKEN_HEADER: Final = "X-Problem-Locator-Logparse-Token"
_MAX_ENVELOPE_BYTES: Final = 3_000_000
_MAX_REQUEST_BYTES: Final = 2_000_000
_SAFE_CAPABILITY = re.compile(r"^[A-Za-z0-9_-]{8,512}$")
ExecutorFactory = Callable[..., SubprocessExecutor]


def _product_argv(product: str) -> list[str]:
    """Map the effective product to upstream argv without forcing defaults."""

    return [] if product == "default" else ["--product", product]


def _no_fault(_name: str) -> None:
    return


def _default_token() -> str:
    return secrets.token_urlsafe(32)


def _default_session_id() -> str:
    return secrets.token_hex(16)


def _asset_failure() -> ExecutionFailure:
    return ExecutionFailure(
        stage=ExecutionStage.ASSET_RESOLUTION,
        code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
        message="Pinned logparse asset is unavailable.",
        retryable=False,
        details=[],
    )


def _tool_failure(
    code: ErrorCode,
    *,
    retryable: bool = False,
) -> ExecutionFailure:
    if code is ErrorCode.LOGPARSE_FAILED:
        message = "Logparse execution failed."
    elif code is ErrorCode.LOGPARSE_OUTPUT_INVALID:
        message = "Logparse output validation failed."
    else:  # pragma: no cover - internal construction guard
        raise ValueError("invalid logparse failure code")
    return ExecutionFailure(
        stage=ExecutionStage.TOOL_EXECUTE,
        code=code,
        message=message,
        retryable=retryable,
        details=[],
    )


def _plain_workspace_root(value: Path) -> Path:
    supplied = Path(value)
    try:
        lexical = Path(os.path.abspath(supplied))
        if any(component.is_symlink() for component in (lexical, *lexical.parents)):
            raise ValueError("Workspace path cannot contain symbolic links")
        metadata = supplied.lstat()
        root = supplied.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("Job Workspace is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Job Workspace must be a plain directory")
    return root


def _resolved_plan_from_manifest(
    manifest: WorkspaceInputManifest,
) -> ResolvedLogparsePlan:
    """Convert the frozen public Workspace value into the broker-private plan."""

    plan = manifest.resolved_logparse_plan
    if plan is None:
        raise ValueError("logparse Workspace is missing its resolved plan")
    return ResolvedLogparsePlan(
        schema_version=1,
        attachment_id=plan.attachment_id,
        artifact_id=plan.artifact_id,
        problem_time=plan.problem_time,
        anchors=[
            Anchor(
                label=item.label,
                module=item.module,
                slot=item.slot,
                process_name=item.process_name,
                pid=item.pid,
            )
            for item in plan.anchors
        ],
    )


def _read_exact_request(workspace_root: Path, relative_path: str) -> bytes:
    path = resolve_workspace_path(workspace_root, relative_path, must_exist=True)
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValueError("broker request file is unavailable") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size <= 0
        or before.st_size > _MAX_REQUEST_BYTES
    ):
        raise ValueError("broker request file is invalid")
    payload = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    stable = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
    if (
        len(payload) != after.st_size
        or any(getattr(before, field) != getattr(after, field) for field in stable)
    ):
        raise ValueError("broker request file changed while it was read")
    return payload


def _claim_area_is_empty(workspace_root: Path) -> bool:
    runtime_root = workspace_root / "runtime"
    state_root = runtime_root / "tool-state"
    for path in (runtime_root, state_root):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return False
    try:
        with os.scandir(state_root) as entries:
            return next(entries, None) is None
    except OSError:
        return False


class _BrokerHttpServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False

    session: PinnedLogparseBrokerSession


class _BrokerRequestHandler(BaseHTTPRequestHandler):
    server_version = "ProblemLocatorLogparse/1"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(1.0)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        server = self.server
        if not isinstance(server, _BrokerHttpServer):  # pragma: no cover
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        status, payload = server.session._handle_http_request(self)
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionError, OSError):
            return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    def log_message(self, _format: str, *args: object) -> None:
        del args


class PinnedLogparseBrokerFactory:
    """Factory paired with exactly one startup-time ``ResolvedAsset``."""

    def __init__(
        self,
        resolved_asset: ResolvedAsset,
        logparse_repo: Path,
        logparse_config_path: Path,
        logparse_python: Path,
        *,
        token_factory: Callable[[], str] = _default_token,
        session_id_factory: Callable[[], str] = _default_session_id,
        fault_point: FaultPoint = _no_fault,
        executor_factory: ExecutorFactory = SubprocessExecutor,
    ) -> None:
        self._resolved_asset = resolved_asset
        self._repo = Path(logparse_repo)
        self._config = Path(logparse_config_path)
        self._python = Path(logparse_python)
        self._token_factory = token_factory
        self._session_id_factory = session_id_factory
        self._fault_point = fault_point
        self._executor_factory = executor_factory

    @property
    def resolved_asset(self) -> ResolvedAsset:
        return self._resolved_asset

    def _asset_is_current(self) -> bool:
        try:
            current = fingerprint_logparse_asset(
                self._repo,
                self._config,
                self._python,
            )
        except ValueError:
            return False
        return current == self._resolved_asset

    def open(
        self,
        job: Job,
        workspace_root: Path,
        workspace_manifest: WorkspaceInputManifest,
        cancellation: CancellationSignal,
    ) -> LogparseBrokerSession:
        expected_ref = self._resolved_asset.ref
        if (
            job.logparse_tool_ref != expected_ref
            or workspace_manifest.logparse_tool_ref != expected_ref
            or job.logparse_product is None
            or workspace_manifest.logparse_product is None
        ):
            raise LogparseBrokerError(_asset_failure())
        try:
            validate_workspace_manifest_for_job(workspace_manifest, job)
        except ValueError:
            raise
        if not self._asset_is_current():
            raise LogparseBrokerError(_asset_failure())
        root = _plain_workspace_root(workspace_root)
        self._fault_point("before_endpoint")
        return PinnedLogparseBrokerSession(
            job=job,
            workspace_root=root,
            workspace_manifest=workspace_manifest,
            cancellation=cancellation,
            resolved_asset=self._resolved_asset,
            logparse_repo=self._repo,
            logparse_config_path=self._config,
            logparse_python=self._python,
            token=self._token_factory(),
            session_id=self._session_id_factory(),
            fault_point=self._fault_point,
            executor_factory=self._executor_factory,
        )


class PinnedLogparseBrokerSession:
    """One loopback capability and all logparse children for one Job."""

    def __init__(
        self,
        *,
        job: Job,
        workspace_root: Path,
        workspace_manifest: WorkspaceInputManifest,
        cancellation: CancellationSignal,
        resolved_asset: ResolvedAsset,
        logparse_repo: Path,
        logparse_config_path: Path,
        logparse_python: Path,
        token: str,
        session_id: str,
        fault_point: FaultPoint = _no_fault,
        executor_factory: ExecutorFactory = SubprocessExecutor,
    ) -> None:
        if (
            _SAFE_CAPABILITY.fullmatch(token) is None
            or _SAFE_CAPABILITY.fullmatch(session_id) is None
        ):
            raise ValueError("broker capability source returned an unsafe value")
        self._job = job
        self._workspace_root = Path(workspace_root)
        self._workspace_manifest = workspace_manifest
        self._resolved_plan = _resolved_plan_from_manifest(workspace_manifest)
        self._cancellation = cancellation
        self._resolved_asset = resolved_asset
        self._repo = Path(logparse_repo)
        self._config = Path(logparse_config_path)
        self._python = Path(logparse_python)
        self._token = token
        self._path = f"/v1/logparse/{session_id}"
        self._fault_point = fault_point
        self._state_lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._children_lock = threading.Lock()
        self._children: set[subprocess.Popen[bytes]] = set()
        self._stopping = threading.Event()
        self._close_complete = threading.Event()
        self._closing = False
        self._closed = False
        self._token_valid = True
        self._accepted_parse_request_bytes: bytes | None = None
        self._operation_audit: list[dict[str, object]] = []
        self._server: _BrokerHttpServer | None = None
        self._server_thread: threading.Thread | None = None
        self._executor = executor_factory(
            register=self._register_child,
            unregister=self._unregister_child,
            session_stopping=self._stopping,
        )

        server: _BrokerHttpServer | None = None
        thread: threading.Thread | None = None
        try:
            server = _BrokerHttpServer(("127.0.0.1", 0), _BrokerRequestHandler)
            server.session = self
            server.timeout = 0.05
            thread = threading.Thread(
                target=self._serve_endpoint,
                name="problem-locator-logparse-broker",
                daemon=False,
            )
            self._server = server
            self._server_thread = thread
            thread.start()
            self._fault_point("endpoint_started")
        except BaseException:
            self._stopping.set()
            self._token_valid = False
            if server is not None:
                if thread is not None:
                    thread.join(timeout=2.0)
                server.server_close()
            raise

    def _serve_endpoint(self) -> None:
        server = self._server
        if server is None:  # pragma: no cover - constructor invariant
            return
        while not self._stopping.is_set():
            server.handle_request()

    def agent_environment(self) -> dict[str, str]:
        with self._state_lock:
            if self._closed or not self._token_valid or self._server is None:
                raise RuntimeError("logparse broker session is closed")
            host, port = self._server.server_address[:2]
            endpoint = f"http://{host}:{port}{self._path}"
            return {_ENDPOINT_ENV: endpoint, _TOKEN_ENV: self._token}

    def parse_request_bytes(self) -> bytes | None:
        with self._state_lock:
            return self._accepted_parse_request_bytes

    def audit_bytes(self) -> bytes:
        """Return the bounded canonical transcript of accepted broker operations."""

        with self._state_lock:
            operations = [dict(item) for item in self._operation_audit]
        return canonical_json_bytes(
            {
                "schema_version": 1,
                "job_id": self._job.job_id,
                "operations": operations,
            }
        )

    def _record_operation(
        self,
        operation: str,
        request_bytes: bytes,
        status: int,
        result_bytes: bytes,
    ) -> None:
        try:
            request_value = parse_canonical_json_bytes(request_bytes)
            result_value = parse_canonical_json_bytes(result_bytes)
        except ValueError:
            return
        record: dict[str, object] = {
            "operation": operation,
            "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
            "request": request_value,
            "http_status": int(status),
            "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
            "result": result_value,
        }
        with self._state_lock:
            if len(self._operation_audit) >= 8:
                raise RuntimeError("logparse operation audit limit exceeded")
            self._operation_audit.append(record)

    def _register_child(self, process: subprocess.Popen[bytes]) -> None:
        terminate_now = False
        with self._children_lock:
            if self._stopping.is_set():
                terminate_now = True
            else:
                self._children.add(process)
        if terminate_now:
            terminate_process_tree(process)

    def _unregister_child(self, process: subprocess.Popen[bytes]) -> None:
        with self._children_lock:
            self._children.discard(process)

    def _current_asset_failure(self) -> ExecutionFailure | None:
        try:
            current = fingerprint_logparse_asset(
                self._repo,
                self._config,
                self._python,
            )
        except ValueError:
            return _asset_failure()
        if current != self._resolved_asset:
            return _asset_failure()
        return None

    def _authenticated(self, handler: BaseHTTPRequestHandler) -> bool:
        with self._state_lock:
            valid = not self._closed and self._token_valid
        supplied = handler.headers.get_all(_TOKEN_HEADER, failobj=[])
        return (
            valid
            and len(supplied) == 1
            and secrets.compare_digest(supplied[0], self._token)
        )

    def _handle_http_request(
        self,
        handler: BaseHTTPRequestHandler,
    ) -> tuple[int, bytes]:
        empty = canonical_json_bytes({})
        if handler.path != self._path or not self._authenticated(handler):
            return HTTPStatus.FORBIDDEN, empty
        if handler.headers.get("Transfer-Encoding") is not None:
            return HTTPStatus.BAD_REQUEST, empty
        content_types = handler.headers.get_all("Content-Type", failobj=[])
        lengths = handler.headers.get_all("Content-Length", failobj=[])
        if content_types != ["application/json"] or len(lengths) != 1:
            return HTTPStatus.BAD_REQUEST, empty
        try:
            if re.fullmatch(r"[0-9]+", lengths[0]) is None:
                raise ValueError
            length = int(lengths[0], 10)
        except ValueError:
            return HTTPStatus.BAD_REQUEST, empty
        if length <= 0 or length > _MAX_ENVELOPE_BYTES:
            return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, empty
        try:
            body = handler.rfile.read(length)
        except (TimeoutError, OSError):
            return HTTPStatus.REQUEST_TIMEOUT, empty
        if len(body) != length:
            return HTTPStatus.BAD_REQUEST, empty
        with self._operation_lock:
            status, result = self._dispatch(body)
        return status, result

    def _dispatch(self, body: bytes) -> tuple[int, bytes]:
        if self._stopping.is_set() or self._cancellation.is_cancelled():
            return HTTPStatus.SERVICE_UNAVAILABLE, canonical_json_bytes({})
        try:
            envelope = parse_canonical_json_bytes(body, BrokerEnvelope)
            proposal_key = validate_proposal_io_paths(
                envelope.request_path,
                envelope.result_path,
            )
            request_bytes = base64.b64decode(
                envelope.request_base64,
                validate=True,
            )
            if not request_bytes or len(request_bytes) > _MAX_REQUEST_BYTES:
                raise ValueError("broker request bytes are invalid")
            if _read_exact_request(self._workspace_root, envelope.request_path) != request_bytes:
                raise ValueError("broker request differs from its Workspace file")
            manifest = load_workspace_manifest(
                self._workspace_root,
                self._workspace_manifest,
            )
        except (ValueError, binascii.Error):
            failure = _tool_failure(ErrorCode.LOGPARSE_FAILED)
            return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(failure)

        asset_failure = self._current_asset_failure()
        if asset_failure is not None:
            return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(asset_failure)

        if envelope.operation == "parse-targets":
            try:
                request = parse_canonical_json_bytes(request_bytes, ParseTargetsRequest)
                self._resolved_plan.validate_request(request)
            except ValueError:
                failure = _tool_failure(ErrorCode.LOGPARSE_FAILED)
                return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(failure)
            if proposal_key != request.artifact_proposal_key:
                failure = _tool_failure(ErrorCode.LOGPARSE_FAILED)
                return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(failure)
            status, result = self._parse_targets(request, request_bytes, manifest)
            self._record_operation(
                envelope.operation,
                request_bytes,
                status,
                result,
            )
            return status, result

        try:
            request = parse_canonical_json_bytes(request_bytes, TargetLogsRequest)
            self._resolved_plan.validate_request(request)
        except ValueError:
            failure = _tool_failure(ErrorCode.LOGPARSE_FAILED)
            return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(failure)
        status, result = self._target_logs(request, manifest)
        self._record_operation(
            envelope.operation,
            request_bytes,
            status,
            result,
        )
        return status, result

    def _run_process(self, argv: list[str]) -> tuple[ProcessResult, ExecutionFailure | None]:
        self._fault_point("before_process")
        result = self._executor.run(
            argv,
            cwd=self._workspace_root,
            cancellation=self._cancellation,
        )
        self._fault_point("process_finished")
        if result.start_failed:
            return result, _tool_failure(ErrorCode.LOGPARSE_FAILED, retryable=True)
        if result.cancelled:
            return result, None
        if result.output_limited or result.returncode != 0:
            return result, _tool_failure(ErrorCode.LOGPARSE_FAILED)
        return result, None

    def _target_argv(
        self,
        *,
        task_id: str,
        output_root: Path,
        problem_time: str,
        anchor: Anchor,
    ) -> list[str]:
        argv = [
            os.fspath(self._python),
            os.fspath(self._repo / "cli.py"),
            "mech-target-logs",
            task_id,
            "--output",
            os.fspath(output_root),
            "--problem-time",
            problem_time,
            "--module",
            anchor.module,
            "--slot",
            anchor.slot,
            "--process-name",
            anchor.process_name,
        ]
        if anchor.pid is not None:
            argv.extend(("--pid", anchor.pid))
        if anchor.label:
            argv.extend(("--label", anchor.label))
        return argv

    def _target_results(
        self,
        *,
        task_id: str,
        output_root: Path,
        problem_time: str,
        anchors: list[Anchor],
        logparse_run_artifact_draft: AgentArtifactProposalDraft | None = None,
    ) -> tuple[bytes | None, ExecutionFailure | None, bool]:
        targets: list[dict[str, object]] = []
        for anchor in anchors:
            asset_failure = self._current_asset_failure()
            if asset_failure is not None:
                return None, asset_failure, False
            result, failure = self._run_process(
                self._target_argv(
                    task_id=task_id,
                    output_root=output_root,
                    problem_time=problem_time,
                    anchor=anchor,
                )
            )
            if result.cancelled:
                return None, None, True
            if failure is not None:
                return None, failure, False
            try:
                targets.append(
                    normalize_target_result(
                        result.stdout,
                        anchor=anchor,
                        controlled_root=output_root,
                    )
                )
            except ValueError:
                return (
                    None,
                    _tool_failure(ErrorCode.LOGPARSE_OUTPUT_INVALID),
                    False,
                )
        return (
            aggregate_target_results(
                targets,
                logparse_run_artifact_draft=(
                    None
                    if logparse_run_artifact_draft is None
                    else logparse_run_artifact_draft.model_dump(mode="json")
                ),
            ),
            None,
            False,
        )

    def _parse_targets(
        self,
        request: ParseTargetsRequest,
        request_bytes: bytes,
        manifest: WorkspaceInputManifest,
    ) -> tuple[int, bytes]:
        with self._state_lock:
            already_accepted = self._accepted_parse_request_bytes is not None
        if already_accepted or has_logparse_run(manifest) or not _claim_area_is_empty(
            self._workspace_root
        ):
            failure = _tool_failure(ErrorCode.LOGPARSE_FAILED)
            return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(failure)
        try:
            attachment = bind_attachment(
                self._workspace_root,
                manifest,
                request.attachment_id,
            )
            tree_relative = (
                f"output/proposals/{request.artifact_proposal_key}/tree"
            )
            tree_root = resolve_workspace_path(
                self._workspace_root,
                tree_relative,
                must_exist=False,
            )
            if tree_root.exists():
                raise ValueError("parse output root already exists")
        except ValueError:
            failure = _tool_failure(ErrorCode.LOGPARSE_FAILED)
            return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(failure)
        if manifest.logparse_tool_ref is None or manifest.logparse_product is None:
            failure = _asset_failure()
            return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(failure)

        claim = LogparseParseClaim(
            schema_version=1,
            job_id=manifest.job_id,
            attachment_id=attachment.entry.resource_id,
            attachment_sha256=attachment.entry.sha256,
            artifact_proposal_key=request.artifact_proposal_key,
            logparse_tool_ref=manifest.logparse_tool_ref,
            request_sha256=hashlib.sha256(request_bytes).hexdigest(),
        )
        with self._state_lock:
            if (
                self._closed
                or self._stopping.is_set()
                or self._accepted_parse_request_bytes is not None
            ):
                return HTTPStatus.SERVICE_UNAVAILABLE, canonical_json_bytes({})

        accepted_request_bytes = bytes(request_bytes)

        def claim_fault_point(point: str) -> None:
            if point == "claim_written":
                # create_parse_claim emits this only after the exact public claim
                # bytes have been written and fsynced. Publish the matching audit
                # bytes first so an injected post-persistence fault cannot split
                # the public claim/request seam.
                with self._state_lock:
                    if self._accepted_parse_request_bytes is None:
                        self._accepted_parse_request_bytes = accepted_request_bytes
            self._fault_point(point)

        try:
            create_parse_claim(
                self._workspace_root,
                claim,
                fault_point=claim_fault_point,
            )
        except (OSError, ValueError):
            failure = _tool_failure(ErrorCode.LOGPARSE_FAILED)
            return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(failure)
        with self._state_lock:
            if self._accepted_parse_request_bytes is None:
                self._accepted_parse_request_bytes = accepted_request_bytes
            elif self._accepted_parse_request_bytes != accepted_request_bytes:
                failure = _tool_failure(ErrorCode.LOGPARSE_FAILED)
                return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(failure)
            stopped = self._closed or self._stopping.is_set()
        if stopped:
            return HTTPStatus.SERVICE_UNAVAILABLE, canonical_json_bytes({})

        argv = [
            os.fspath(self._python),
            os.fspath(self._repo / "cli.py"),
            "parse",
            os.fspath(attachment.path),
            "-c",
            os.fspath(self._config),
            "-o",
            os.fspath(tree_root),
        ]
        argv.extend(_product_argv(manifest.logparse_product))
        asset_failure = self._current_asset_failure()
        if asset_failure is not None:
            return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(
                asset_failure
            )
        result, failure = self._run_process(argv)
        if result.cancelled:
            return HTTPStatus.SERVICE_UNAVAILABLE, canonical_json_bytes({})
        if failure is not None:
            return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(failure)
        try:
            run = inspect_controlled_run(
                tree_root,
                product=manifest.logparse_product,
            )
        except ValueError:
            failure = _tool_failure(ErrorCode.LOGPARSE_OUTPUT_INVALID)
            return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(failure)
        payload, failure, cancelled = self._target_results(
            task_id=run.task_id,
            output_root=run.root,
            problem_time=request.problem_time,
            anchors=list(request.anchors),
            logparse_run_artifact_draft=AgentArtifactProposalDraft(
                proposal_key=request.artifact_proposal_key,
                artifact_kind=ArtifactKind.LOGPARSE_RUN,
                name=request.artifact_proposal_key,
                content_type=(
                    "application/vnd.problem-locator.logparse-run+directory"
                ),
                resource_kind=ResourceKind.DIRECTORY,
                workspace_relative_path=tree_relative,
                declared_size=None,
                declared_sha256=None,
                metadata=LogparseRunMetadata(
                    tree_manifest_sha256=run.sha256,
                    logparse_version_ref=manifest.logparse_tool_ref,
                    parse_manifest_relative_path=run.parse_manifest_relative_path,
                    source_attachment_id=attachment.entry.resource_id,
                    source_attachment_sha256=attachment.entry.sha256,
                    parse_parameters=LogparseParseParameters(
                        product=manifest.logparse_product
                    ),
                ),
            ),
        )
        if cancelled:
            return HTTPStatus.SERVICE_UNAVAILABLE, canonical_json_bytes({})
        if failure is not None or payload is None:
            assert failure is not None
            return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(failure)
        return HTTPStatus.OK, payload

    def _target_logs(
        self,
        request: TargetLogsRequest,
        manifest: WorkspaceInputManifest,
    ) -> tuple[int, bytes]:
        try:
            bound = bind_logparse_run(
                self._workspace_root,
                manifest,
                request.artifact_id,
            )
        except ValueError:
            failure = _tool_failure(ErrorCode.LOGPARSE_OUTPUT_INVALID)
            return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(failure)
        payload, failure, cancelled = self._target_results(
            task_id=bound.run.task_id,
            output_root=bound.run.root,
            problem_time=request.problem_time,
            anchors=list(request.anchors),
        )
        if cancelled:
            return HTTPStatus.SERVICE_UNAVAILABLE, canonical_json_bytes({})
        if failure is not None or payload is None:
            assert failure is not None
            return HTTPStatus.UNPROCESSABLE_ENTITY, canonical_json_bytes(failure)
        return HTTPStatus.OK, payload

    def close(self) -> None:
        while True:
            with self._state_lock:
                if self._closed:
                    return
                if not self._closing:
                    self._closing = True
                    self._close_complete.clear()
                    self._token_valid = False
                    self._stopping.set()
                    server = self._server
                    thread = self._server_thread
                    break
                completion = self._close_complete
            completion.wait()

        failures: list[BaseException] = []

        def preserve_failure(action: Callable[[], None]) -> None:
            try:
                action()
            except BaseException as exc:  # cleanup continues under every fault
                failures.append(exc)

        preserve_failure(lambda: self._fault_point("close_started"))

        with self._children_lock:
            children = tuple(self._children)
        for process in children:
            preserve_failure(lambda process=process: terminate_process_tree(process))

        if thread is not None:
            preserve_failure(lambda: thread.join(timeout=2.0))
        server_closed = server is None
        if server is not None:
            try:
                server.server_close()
                server_closed = True
            except BaseException as exc:  # allow a later close to retry
                failures.append(exc)
        if thread is not None and thread.is_alive():
            preserve_failure(lambda: thread.join(timeout=2.0))

        with self._children_lock:
            remaining = tuple(self._children)
        for process in remaining:
            preserve_failure(lambda process=process: terminate_process_tree(process))

        endpoint_stopped = thread is None or not thread.is_alive()
        with self._children_lock:
            live_children = tuple(
                process for process in self._children if process.poll() is None
            )
            if not live_children:
                self._children.clear()
        cleanup_complete = endpoint_stopped and not live_children and server_closed
        if not endpoint_stopped:
            failures.append(RuntimeError("logparse broker endpoint did not stop"))
        if live_children:
            failures.append(RuntimeError("logparse broker child process did not stop"))

        with self._state_lock:
            if cleanup_complete:
                self._server = None
                self._server_thread = None
                self._closed = True
            self._closing = False
            self._close_complete.set()

        if failures:
            raise failures[0]


def build_logparse_runtime(
    logparse_repo: str | os.PathLike[str],
    logparse_config_path: str | os.PathLike[str],
    logparse_python: str | os.PathLike[str],
) -> tuple[ResolvedAsset, LogparseBrokerFactory]:
    """Build one inseparable pinned asset/factory pair for the composition root."""

    repo, config, python = resolve_logparse_configuration(
        logparse_repo,
        logparse_config_path,
        logparse_python,
    )
    asset = fingerprint_logparse_asset(repo, config, python)
    factory = PinnedLogparseBrokerFactory(asset, repo, config, python)
    return asset, factory


__all__ = [
    "PinnedLogparseBrokerFactory",
    "PinnedLogparseBrokerSession",
    "build_logparse_runtime",
]
