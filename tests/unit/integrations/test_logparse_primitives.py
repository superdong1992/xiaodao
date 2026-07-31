from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

import problem_locator.integrations.logparse.fingerprint as fingerprint_module
from problem_locator.contracts import (
    AssetKind,
    CancellationReason,
    canonical_json_bytes,
)
from problem_locator.integrations.logparse.fingerprint import (
    fingerprint_logparse_asset,
    resolve_logparse_configuration,
)
from problem_locator.integrations.logparse.paths import (
    resolve_workspace_path,
    validate_proposal_io_paths,
    validate_relative_path,
)
from problem_locator.integrations.logparse.process import (
    SubprocessExecutor,
    sanitized_logparse_environment,
)
from problem_locator.integrations.logparse.requests import (
    Anchor,
    BrokerEnvelope,
    ParseTargetsRequest,
    TargetLogsRequest,
)
from problem_locator.integrations.logparse.tree import build_tree_manifest


ATTACHMENT_ID = "00000000-0000-0000-0000-000000000001"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000002"
PROBLEM_TIME = "2026-01-03T00:05:00.000Z"


def _anchor(**updates: object) -> Anchor:
    values: dict[str, object] = {
        "label": "caller",
        "module": "COMPACT",
        "slot": "slot1",
        "process_name": "svc_master",
        "pid": "100",
    }
    values.update(updates)
    return Anchor(**values)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "logparse"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _write(repo / ".gitignore", b"ignored/\n")
    _write(repo / "cli.py", b"print('parse')\n")
    _write(repo / "nested" / "alpha.txt", b"alpha\n")
    _write(repo / "unicode-\u03b2.txt", "beta\n".encode())
    _git(repo, "add", ".gitignore", "cli.py", "nested/alpha.txt")
    config = tmp_path / "config.yaml"
    config.write_bytes(b"products:\n  compact: {}\n")
    return repo, config


def _expected_asset_hash(repo: Path, config: Path) -> str:
    entry_paths = [
        ".gitignore",
        "cli.py",
        "nested/alpha.txt",
        "unicode-\u03b2.txt",
    ]
    entries = []
    for relative_path in sorted(entry_paths):
        payload = (repo / relative_path).read_bytes()
        entries.append(
            {
                "path": relative_path,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    repo_hash = hashlib.sha256(
        canonical_json_bytes({"version": 1, "entries": entries})
    ).hexdigest()
    version = subprocess.run(
        [sys.executable, "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    ).stdout.decode("utf-8", errors="strict").strip()
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "repo_tree_sha256": repo_hash,
                "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
                "python_resolved_path": os.fspath(Path(sys.executable).resolve()),
                "python_version": version,
            }
        )
    ).hexdigest()


def test_fingerprint_uses_the_canonical_repo_config_and_python_hashes(
    tmp_path: Path,
) -> None:
    repo, config = _make_repo(tmp_path)

    asset = fingerprint_logparse_asset(repo, config, sys.executable)
    expected_hash = _expected_asset_hash(repo, config)

    assert asset.asset_kind is AssetKind.LOGPARSE_TOOL
    assert asset.root_path == os.fspath(repo.resolve())
    assert asset.ref.id == "logparse-tool/logparse"
    assert asset.ref.content_hash == expected_hash
    assert asset.ref.version == f"sha256-{expected_hash[:16]}"
    assert fingerprint_logparse_asset(repo, config, sys.executable) == asset


def test_configuration_preserves_a_validated_python_launcher_symlink(
    tmp_path: Path,
) -> None:
    repo, config = _make_repo(tmp_path)
    launcher = tmp_path / "venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    try:
        launcher.symlink_to(Path(sys.executable))
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")

    configured_repo, configured_config, configured_python = (
        resolve_logparse_configuration(repo, config, launcher)
    )

    assert configured_repo == repo.resolve()
    assert configured_config == config.resolve()
    assert configured_python == Path(os.path.abspath(launcher))
    assert configured_python.is_symlink()
    assert fingerprint_logparse_asset(
        configured_repo,
        configured_config,
        configured_python,
    ) == fingerprint_logparse_asset(repo, config, Path(sys.executable).resolve())


def test_fingerprint_detects_repo_and_config_drift_but_excludes_ignored_files(
    tmp_path: Path,
) -> None:
    repo, config = _make_repo(tmp_path)
    original = fingerprint_logparse_asset(repo, config, sys.executable)

    _write(repo / "ignored" / "cache.bin", b"not part of the installation")
    assert fingerprint_logparse_asset(repo, config, sys.executable) == original

    _write(repo / "cli.py", b"print('changed')\n")
    repo_changed = fingerprint_logparse_asset(repo, config, sys.executable)
    assert repo_changed.ref.content_hash != original.ref.content_hash

    config.write_bytes(b"products:\n  default: {}\n")
    config_changed = fingerprint_logparse_asset(repo, config, sys.executable)
    assert config_changed.ref.content_hash != repo_changed.ref.content_hash


def test_fingerprint_rejects_an_empty_git_file_list(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    config = tmp_path / "config.yaml"
    config.write_text("products: {}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="file list is empty"):
        fingerprint_logparse_asset(repo, config, sys.executable)


def test_fingerprint_rejects_a_symlinked_repo_entry(tmp_path: Path) -> None:
    repo, config = _make_repo(tmp_path)
    external = tmp_path / "external.txt"
    external.write_text("external\n", encoding="utf-8")
    link = repo / "linked.txt"
    try:
        link.symlink_to(external)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")
    _git(repo, "add", "linked.txt")

    with pytest.raises(ValueError):
        fingerprint_logparse_asset(repo, config, sys.executable)


@pytest.mark.parametrize(
    ("command_kind", "message"),
    [
        ("git", "git cannot enumerate"),
        ("python", "Python cannot be executed"),
    ],
)
def test_fingerprint_helper_processes_have_a_fixed_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_kind: str,
    message: str,
) -> None:
    repo, config = _make_repo(tmp_path)
    real_run = subprocess.run
    observed_timeouts: list[float] = []

    def timeout_selected_command(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = args[0]
        assert isinstance(argv, list)
        timeout = kwargs.get("timeout")
        if (command_kind == "git" and argv[0] == "git") or (
            command_kind == "python" and argv[1:] == ["--version"]
        ):
            assert isinstance(timeout, float)
            observed_timeouts.append(timeout)
            raise subprocess.TimeoutExpired(argv, timeout)
        return real_run(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(fingerprint_module.subprocess, "run", timeout_selected_command)

    with pytest.raises(ValueError, match=message):
        fingerprint_logparse_asset(repo, config, sys.executable)

    assert observed_timeouts == [10.0]


def test_requests_accept_only_the_fixed_wire_fields() -> None:
    anchor = _anchor()
    parse_request = ParseTargetsRequest(
        schema_version=1,
        problem_time=PROBLEM_TIME,
        anchors=[anchor],
        attachment_id=ATTACHMENT_ID,
        artifact_proposal_key="logparse-run_1",
    )
    target_request = TargetLogsRequest(
        schema_version=1,
        problem_time=PROBLEM_TIME,
        anchors=[anchor],
        artifact_id=ARTIFACT_ID,
    )

    assert parse_request.model_dump(mode="json") == {
        "schema_version": 1,
        "problem_time": PROBLEM_TIME,
        "anchors": [
            {
                "label": "caller",
                "module": "COMPACT",
                "slot": "slot1",
                "process_name": "svc_master",
                "pid": "100",
            }
        ],
        "attachment_id": ATTACHMENT_ID,
        "artifact_proposal_key": "logparse-run_1",
    }
    assert target_request.artifact_id == ARTIFACT_ID

    with pytest.raises(ValidationError, match="extra_forbidden"):
        ParseTargetsRequest(
            **parse_request.model_dump(),
            logparse_product="compact",
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"label": " caller"}, "canonical ASCII"),
        ({"module": "caller\nserver"}, "canonical ASCII"),
        ({"slot": "\u670d\u52a1"}, "canonical ASCII"),
        ({"process_name": ""}, "string_too_short"),
    ],
)
def test_anchor_rejects_noncanonical_values(
    updates: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _anchor(**updates)


def test_requests_reject_unsafe_keys_duplicate_anchors_and_noncanonical_time() -> None:
    with pytest.raises(ValidationError, match="safe path segment"):
        ParseTargetsRequest(
            schema_version=1,
            problem_time=PROBLEM_TIME,
            anchors=[_anchor()],
            attachment_id=ATTACHMENT_ID,
            artifact_proposal_key="../escape",
        )
    with pytest.raises(ValidationError, match="anchors must be unique"):
        TargetLogsRequest(
            schema_version=1,
            problem_time=PROBLEM_TIME,
            anchors=[_anchor(), _anchor()],
            artifact_id=ARTIFACT_ID,
        )
    with pytest.raises(ValidationError):
        TargetLogsRequest(
            schema_version=1,
            problem_time="2026-01-03T00:05:00Z",
            anchors=[_anchor()],
            artifact_id=ARTIFACT_ID,
        )


def test_broker_envelope_is_strict_and_bounded() -> None:
    envelope = BrokerEnvelope(
        schema_version=1,
        operation="parse-targets",
        request_path="output/proposals/run/request.json",
        result_path="output/proposals/run/target_logs.json",
        request_base64="e30K",
    )
    assert envelope.operation == "parse-targets"

    with pytest.raises(ValidationError):
        BrokerEnvelope(
            schema_version=1,
            operation="arbitrary-command",
            request_path="output/proposals/run/request.json",
            result_path="output/proposals/run/target_logs.json",
            request_base64="e30K",
        )


@pytest.mark.parametrize(
    "value",
    [
        "/absolute",
        "../escape",
        "output/../escape",
        "output//file",
        "output\\file",
        "C:/drive",
        " leading",
        "trailing ",
        "line\nbreak",
    ],
)
def test_validate_relative_path_rejects_noncanonical_or_escaping_values(
    value: str,
) -> None:
    with pytest.raises((ValueError, ValidationError)):
        validate_relative_path(value)


def test_workspace_resolution_contains_existing_and_future_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    existing = workspace / "output" / "result.json"
    _write(existing, b"{}\n")

    assert resolve_workspace_path(
        workspace,
        "output/result.json",
        must_exist=True,
    ) == existing.resolve()
    assert resolve_workspace_path(
        workspace,
        "output/future.json",
        must_exist=False,
    ) == (workspace / "output" / "future.json").resolve()
    with pytest.raises(ValueError, match="unavailable"):
        resolve_workspace_path(workspace, "output/missing.json", must_exist=True)


def test_workspace_resolution_rejects_a_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.txt").write_text("secret\n", encoding="utf-8")
    link = workspace / "linked"
    try:
        link.symlink_to(external, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")

    with pytest.raises(ValueError):
        resolve_workspace_path(workspace, "linked/secret.txt", must_exist=True)


def test_proposal_io_paths_must_share_the_exact_proposal_directory() -> None:
    assert (
        validate_proposal_io_paths(
            "output/proposals/run-1/request.json",
            "output/proposals/run-1/target_logs.json",
        )
        == "run-1"
    )
    invalid_pairs = [
        (
            "input/proposals/run-1/request.json",
            "output/proposals/run-1/target_logs.json",
        ),
        (
            "output/proposals/run-1/request.json",
            "output/proposals/run-2/target_logs.json",
        ),
        (
            "output/proposals/run-1/request.json",
            "output/proposals/run-1/result.json",
        ),
        (
            "output/proposals/nested/run-1/request.json",
            "output/proposals/nested/run-1/target_logs.json",
        ),
    ]
    for request_path, result_path in invalid_pairs:
        with pytest.raises(ValueError, match="broker I/O"):
            validate_proposal_io_paths(request_path, result_path)


def test_tree_manifest_is_sorted_canonical_and_content_sensitive(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    _write(root / "z.log", b"z\n")
    _write(root / "nested" / "a.log", b"alpha\n")

    manifest, size, digest = build_tree_manifest(root)

    assert [entry.path for entry in manifest.entries] == ["nested/a.log", "z.log"]
    assert size == len(b"alpha\n") + len(b"z\n")
    assert digest == hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    assert build_tree_manifest(root) == (manifest, size, digest)

    (root / "z.log").write_bytes(b"changed\n")
    changed_manifest, changed_size, changed_digest = build_tree_manifest(root)
    assert changed_manifest != manifest
    assert changed_size != size
    assert changed_digest != digest


def test_tree_manifest_rejects_nested_symlinks_and_hardlinks(tmp_path: Path) -> None:
    symlink_root = tmp_path / "symlink-tree"
    _write(symlink_root / "real.log", b"log\n")
    try:
        (symlink_root / "linked.log").symlink_to(symlink_root / "real.log")
    except (NotImplementedError, OSError):
        pytest.skip("links are unavailable on this platform")
    with pytest.raises(ValueError, match="controlled logparse output tree is invalid"):
        build_tree_manifest(symlink_root)

    hardlink_root = tmp_path / "hardlink-tree"
    _write(hardlink_root / "first.log", b"log\n")
    try:
        os.link(hardlink_root / "first.log", hardlink_root / "second.log")
    except (NotImplementedError, OSError):
        pytest.skip("hard links are unavailable on this platform")
    with pytest.raises(ValueError, match="controlled logparse output tree is invalid"):
        build_tree_manifest(hardlink_root)


def test_tree_manifest_rejects_missing_or_nondirectory_roots(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="controlled logparse output tree is invalid"):
        build_tree_manifest(tmp_path / "missing")
    regular_file = tmp_path / "file.log"
    regular_file.write_bytes(b"log\n")
    with pytest.raises(ValueError, match="controlled logparse output tree is invalid"):
        build_tree_manifest(regular_file)


class _Cancellation:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: CancellationReason | None = None

    @property
    def reason(self) -> CancellationReason | None:
        return self._reason

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout_seconds: float | None) -> bool:
        return self._event.wait(timeout_seconds)

    def cancel(self, reason: CancellationReason) -> None:
        self._reason = reason
        self._event.set()


def _executor(
    stopping: threading.Event | None = None,
) -> tuple[SubprocessExecutor, list[object], list[object]]:
    registered: list[object] = []
    unregistered: list[object] = []
    executor = SubprocessExecutor(
        register=registered.append,
        unregister=unregistered.append,
        session_stopping=stopping or threading.Event(),
    )
    return executor, registered, unregistered


def test_sanitized_environment_removes_configuration_and_broker_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGPARSE_REPO", "/secret/repo")
    monkeypatch.setenv("logparse_config_path", "/secret/config")
    monkeypatch.setenv("Logparse_Python", "/secret/python")
    monkeypatch.setenv("PROBLEM_LOCATOR_LOGPARSE_TOKEN", "secret-token")
    monkeypatch.setenv("PROBLEM_LOCATOR_LOGPARSE_ENDPOINT", "secret-endpoint")
    monkeypatch.setenv("S07_VISIBLE", "retained")

    environment = sanitized_logparse_environment()

    assert environment["S07_VISIBLE"] == "retained"
    assert not any(key.casefold() == "logparse_repo" for key in environment)
    assert not any(key.casefold() == "logparse_config_path" for key in environment)
    assert not any(key.casefold() == "logparse_python" for key in environment)
    assert not any(
        key.casefold().startswith("problem_locator_logparse_") for key in environment
    )


def test_subprocess_executor_passes_arguments_without_a_shell(tmp_path: Path) -> None:
    executor, registered, unregistered = _executor()
    literal = "$(printf shell-injection); semi; `uname`"

    result = executor.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.buffer.write(sys.argv[1].encode()); "
                "sys.stderr.buffer.write(b'stderr')"
            ),
            literal,
        ],
        cwd=tmp_path,
        cancellation=_Cancellation(),
    )

    assert result.returncode == 0
    assert result.stdout.decode() == literal
    assert result.stderr == b"stderr"
    assert not result.cancelled
    assert not result.start_failed
    assert not result.output_limited
    assert len(registered) == 1
    assert unregistered == registered


def test_subprocess_executor_drains_and_bounds_both_output_streams(
    tmp_path: Path,
) -> None:
    stopping = threading.Event()
    executor, registered, unregistered = _executor(stopping)
    safety_timer = threading.Timer(10.0, stopping.set)
    safety_timer.start()
    started_at = time.monotonic()
    try:
        result = executor.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys\n"
                    "stdout_chunk = b'O' * 65536\n"
                    "stderr_chunk = b'E' * 65536\n"
                    "for _ in range(33):\n"
                    "    sys.stdout.buffer.write(stdout_chunk)\n"
                    "    sys.stdout.buffer.flush()\n"
                    "    sys.stderr.buffer.write(stderr_chunk)\n"
                    "    sys.stderr.buffer.flush()\n"
                ),
            ],
            cwd=tmp_path,
            cancellation=_Cancellation(),
        )
    finally:
        safety_timer.cancel()

    assert time.monotonic() - started_at < 10
    assert result.returncode == 0
    assert result.stdout == b"O" * 2_000_000
    assert result.stderr == b"E" * 2_000_000
    assert result.output_limited
    assert not result.cancelled
    assert not result.start_failed
    assert len(registered) == 1
    assert unregistered == registered


def test_subprocess_executor_reports_start_failure_without_registration(
    tmp_path: Path,
) -> None:
    executor, registered, unregistered = _executor()

    result = executor.run(
        [os.fspath(tmp_path / "missing-executable")],
        cwd=tmp_path,
        cancellation=_Cancellation(),
    )

    assert result.returncode is None
    assert result.stdout == b""
    assert result.stderr == b""
    assert not result.cancelled
    assert result.start_failed
    assert not result.output_limited
    assert registered == []
    assert unregistered == []


def test_subprocess_executor_does_not_start_after_session_stop(tmp_path: Path) -> None:
    stopping = threading.Event()
    stopping.set()
    executor, registered, unregistered = _executor(stopping)

    result = executor.run(
        [sys.executable, "-c", "raise SystemExit(99)"],
        cwd=tmp_path,
        cancellation=_Cancellation(),
    )

    assert result.returncode is None
    assert result.cancelled
    assert result.cancellation_reason is None
    assert not result.start_failed
    assert not result.output_limited
    assert registered == []
    assert unregistered == []


def test_subprocess_executor_terminates_an_inflight_cancelled_process(
    tmp_path: Path,
) -> None:
    executor, registered, unregistered = _executor()
    cancellation = _Cancellation()
    timer = threading.Timer(
        0.15,
        cancellation.cancel,
        args=(CancellationReason.USER_CANCEL,),
    )
    timer.start()
    started_at = time.monotonic()
    try:
        result = executor.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            cancellation=cancellation,
        )
    finally:
        timer.cancel()

    assert time.monotonic() - started_at < 5
    assert result.cancelled
    assert result.cancellation_reason is CancellationReason.USER_CANCEL
    assert not result.start_failed
    assert not result.output_limited
    assert result.returncode is not None
    assert len(registered) == 1
    assert unregistered == registered


def test_subprocess_executor_rejects_an_invalid_argv(tmp_path: Path) -> None:
    executor, registered, unregistered = _executor()
    with pytest.raises(ValueError, match="non-empty strings"):
        executor.run([], cwd=tmp_path, cancellation=_Cancellation())
    with pytest.raises(ValueError, match="non-empty strings"):
        executor.run(
            [sys.executable, ""],
            cwd=tmp_path,
            cancellation=_Cancellation(),
        )
    assert registered == []
    assert unregistered == []
