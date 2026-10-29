"""Native session wiring is deterministic; these tests never call a model."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from problem_locator.contracts import CancellationReason, ErrorCode, default_resource_limits
from problem_locator.runtime import agent_backend as backend_module
from problem_locator.runtime.agent_backend import AgentBackend, BackendExecution
from problem_locator.runtime.agent_telemetry import AgentStreamTelemetry
from problem_locator.runtime.claude_command import (
    ClaudeCommand, ClaudeCommandError, apply_final_response_policy, supports_native_sessions,
)
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.native_session import NativeSession
from problem_locator.runtime.process_tree import spawn_managed_process
from tests.deterministic.unit.runtime.test_agent_backend import _Signal, _limits, _sinks, _workspace


SESSION_ID = "10203040-5060-4070-8090-a0b0c0d0e0f0"
OTHER_SESSION_ID = "10203040-5060-4070-8090-a0b0c0d0e0f1"


def _line(payload: dict) -> bytes:
    return json.dumps(payload).encode() + b"\n"


def _init(identifier=SESSION_ID) -> dict:
    return {"type": "system", "subtype": "init", "session_id": identifier}


def _result(**fields) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, "result": "answer", **fields}


@pytest.mark.parametrize("executable", ["claude", "codeagent", "codeagent.exe", "codeagent.cmd"])
@pytest.mark.parametrize("resume_id", [None, SESSION_ID])
def test_native_launcher_isolates_storage_after_reading_provider_and_owns_resume(tmp_path, executable, resume_id):
    provider = tmp_path / "provider"
    provider.mkdir()
    (provider / "settings.json").write_text(json.dumps({
        "model": "configured-model", "env": {"ANTHROPIC_BASE_URL": "https://provider.invalid"},
        "hooks": {"SessionStart": [{"command": "ambient-hook"}]},
    }))
    invocation = ClaudeCommand((executable, "--continue", "-c", "--resume", OTHER_SESSION_ID,
        "-r=" + OTHER_SESSION_ID, "--session-id", OTHER_SESSION_ID, "--no-session-persistence",
        "--bare", "--dangerously-skip-permissions"), {"CLAUDE_CONFIG_DIR": str(provider)})
    config_dir = tmp_path / "conversation-config"
    result = apply_final_response_policy(invocation, file_access="read-search",
        workspace_root=tmp_path, phase="REPORT_FOLLOWUP",
        native_session=NativeSession(config_dir, resume_id))

    policy = json.loads(Path(result.argv[result.argv.index("--settings") + 1]).read_text())
    assert policy["model"] == "configured-model"
    assert policy["env"] == {"ANTHROPIC_BASE_URL": "https://provider.invalid"}
    assert set(policy["hooks"]) == {"PreToolUse"}
    assert result.environment["CLAUDE_CONFIG_DIR"] == str(config_dir.resolve())
    assert config_dir.is_dir()
    assert OTHER_SESSION_ID not in " ".join(result.argv)
    for option in ("--continue", "-c", "-r", "--session-id", "--no-session-persistence",
                   "--bare", "--dangerously-skip-permissions"):
        assert option not in result.argv
    assert result.argv[result.argv.index("--tools") + 1] == "Read,Grep"
    assert result.argv[result.argv.index("--permission-mode") + 1] == "dontAsk"
    assert result.argv[result.argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert result.argv.count("--resume") == (1 if resume_id else 0)
    if resume_id:
        assert result.argv[result.argv.index("--resume") + 1] == resume_id


@pytest.mark.parametrize("command,supported", [
    ("claude --model test", True), ("codeagent --bare", True),
    ("custom-agent", False), ("node cli.js", True),
    ("node isolated-agent-wrapper.mjs --workflow report-followup", True), ("", False),
])
def test_only_known_launchers_support_native_sessions(command, supported):
    assert supports_native_sessions(command) is supported


@pytest.mark.parametrize("argv,phase,file_access", [
    (("custom-agent",), "REPORT_FOLLOWUP", "none"),
    (("node", "custom-cli.js"), "REPORT_FOLLOWUP", "read-search"),
    (("node", "isolated-agent-wrapper.mjs", "--workflow", "job"), "REPORT_FOLLOWUP", "read-search"),
    (("codeagent",), "MEMORY_CORRECTION", "read-search"),
    (("codeagent",), "REPORT_FOLLOWUP", None),
])
def test_native_session_rejects_unsupported_phase_or_launcher_before_creating_files(tmp_path, argv, phase, file_access):
    config_dir = tmp_path / "native-config"
    with pytest.raises(ClaudeCommandError):
        apply_final_response_policy(ClaudeCommand(argv, {}), file_access=file_access,
            workspace_root=tmp_path, phase=phase, native_session=NativeSession(config_dir))
    assert not config_dir.exists()


@pytest.mark.parametrize("resume_id", [None, SESSION_ID])
def test_owned_wrapper_preserves_native_storage_and_exact_resume(tmp_path, resume_id):
    settings = tmp_path / "provider.json"
    settings.write_text('{"model":"configured-model","env":{}}', encoding="utf-8")
    argv = ("node", "isolated-agent-wrapper.mjs", "--workflow", "report-followup",
        "--settings", str(settings), "--model", "configured-model", "--resume", OTHER_SESSION_ID)
    config = tmp_path / "session-config"
    result = apply_final_response_policy(ClaudeCommand(argv, {}), file_access="read-search",
        workspace_root=tmp_path, phase="REPORT_FOLLOWUP", native_session=NativeSession(config, resume_id))
    assert result.environment["CLAUDE_CONFIG_DIR"] == str(config.resolve())
    policy = Path(result.environment["PROBLEM_LOCATOR_FOLLOWUP_SETTINGS"])
    assert json.loads(policy.read_text())["model"] == "configured-model"
    assert OTHER_SESSION_ID not in result.argv
    assert result.argv.count("--resume") == (1 if resume_id else 0)
    if resume_id:
        assert result.argv[result.argv.index("--resume") + 1] == resume_id
    assert result.argv[result.argv.index("--workflow") + 1] == "report-followup"


@pytest.mark.parametrize("identifier", ["private-session", SESSION_ID.replace("-", ""), "../session", 1, ""])
def test_invalid_resume_identifier_never_reaches_launcher(tmp_path, identifier):
    with pytest.raises(ValueError, match="原生会话标识无效"):
        NativeSession(tmp_path, identifier)


def test_session_init_is_saved_immediately_once_and_excluded_from_diagnostics(tmp_path):
    saved = []
    native = NativeSession(tmp_path, on_session_id=saved.append)
    telemetry = AgentStreamTelemetry(native_session=native)
    frame = _line(_init())
    telemetry.write(frame[:13])
    assert saved == []
    telemetry.write(frame[13:])
    assert saved == [SESSION_ID]  # Persistence does not wait for the answer.
    telemetry.write(frame)
    assert saved == [SESSION_ID]
    telemetry.write(_line(_result(session_id=SESSION_ID)))
    assert telemetry.has_valid_native_session()
    assert telemetry.session_id == SESSION_ID
    assert telemetry.final_result == "answer"
    assert SESSION_ID not in json.dumps(telemetry.snapshot(diagnosis_mode="GENERIC", backend_status="SUCCESS"))
    assert SESSION_ID not in repr(NativeSession(tmp_path, SESSION_ID))
    assert SESSION_ID not in repr(BackendExecution(0, 0, 0, 0, session_id=SESSION_ID))


@pytest.mark.parametrize("event", [
    {"type": "system", "subtype": "status", "session_id": SESSION_ID},
    {"type": "assistant", "session_id": SESSION_ID, "message": {"content": []}},
    _result(session_id=SESSION_ID),
])
def test_only_system_init_can_establish_session(tmp_path, event):
    saved = []
    telemetry = AgentStreamTelemetry(native_session=NativeSession(tmp_path, on_session_id=saved.append))
    telemetry.write(_line(event))
    assert saved == []
    assert not telemetry.has_valid_native_session()


@pytest.mark.parametrize("first,second,resume_id", [
    (_init(None), None, None),
    (_init("private-session"), None, None),
    (_init(OTHER_SESSION_ID), None, SESSION_ID),
    (_init(), _init(OTHER_SESSION_ID), None),
    (_init(), _result(session_id=OTHER_SESSION_ID), None),
    (_init(), _result(session_id="invalid"), None),
])
def test_native_session_requires_valid_consistent_identity(tmp_path, first, second, resume_id):
    telemetry = AgentStreamTelemetry(native_session=NativeSession(tmp_path, resume_id))
    telemetry.write(_line(first))
    if second:
        telemetry.write(_line(second))
    assert telemetry.native_session_failed
    assert not telemetry.has_valid_native_session()
    assert telemetry.final_result is None


def test_native_resume_accepts_matching_init_and_result_without_id(tmp_path):
    saved = []
    telemetry = AgentStreamTelemetry(native_session=NativeSession(tmp_path, SESSION_ID, saved.append))
    telemetry.write(_line(_init()))
    telemetry.write(_line(_result()))
    assert saved == [SESSION_ID]
    assert telemetry.has_valid_native_session()
    assert telemetry.final_result == "answer"


def test_session_persistence_failure_rejects_answer(tmp_path):
    def fail(identifier):
        raise OSError("private persistence failure " + identifier)
    telemetry = AgentStreamTelemetry(native_session=NativeSession(tmp_path, on_session_id=fail))
    telemetry.write(_line(_init()))
    telemetry.write(_line(_result()))
    assert telemetry.native_session_failed
    assert not telemetry.has_valid_native_session()
    assert telemetry.final_result is None
    assert SESSION_ID not in json.dumps(telemetry.snapshot(diagnosis_mode="GENERIC", backend_status="FAILED"))


def _fake_native_backend(tmp_path, events, *, hang=False, observed=None):
    script = tmp_path / "fake_native.py"
    script.write_text("import json, sys, time\nsys.stdin.buffer.read()\n"
        + f"events = {events!r}\n"
        + "for event in events:\n    print(json.dumps(event), flush=True)\n"
        + ("while True:\n    time.sleep(0.1)\n" if hang else ""), encoding="utf-8")
    def spawn(argv, *, cwd, environment):
        if observed is not None:
            observed.append((argv, environment))
        return spawn_managed_process((sys.executable, str(script)), cwd=cwd, environment=environment)
    return AgentBackend("codeagent --bare", parent_environment={"CLAUDE_CONFIG_DIR": str(tmp_path / "provider")},
        process_factory=spawn)


def _execute_native(backend, root, session, *, signal=None):
    return backend.execute(prompt="follow-up", workspace_root=root, cancellation=signal or _Signal(),
        log_sinks=_sinks()[0], resource_limits=default_resource_limits("DIAGNOSE"),
        test_limits=_limits(wall_time=5), file_access="read-search", backend_phase="REPORT_FOLLOWUP",
        native_session=session)


def test_backend_returns_native_identity_and_redacts_resume_from_journey(tmp_path, monkeypatch):
    launches, saved, journey = [], [], []
    monkeypatch.setattr(backend_module, "record_stage_completed", lambda *args, **kwargs: journey.append(kwargs))
    backend = _fake_native_backend(tmp_path, [_init(), _result(session_id=SESSION_ID)], observed=launches)
    result = _execute_native(backend, _workspace(tmp_path), NativeSession(tmp_path / "config", SESSION_ID, saved.append))
    assert result.session_id == SESSION_ID
    assert result.final_result == "answer"
    assert saved == [SESSION_ID]
    argv, environment = launches[0]
    assert argv[argv.index("--resume") + 1] == SESSION_ID
    assert environment["CLAUDE_CONFIG_DIR"] == str((tmp_path / "config").resolve())
    assert SESSION_ID not in json.dumps(journey)


@pytest.mark.parametrize("events", [[_result()], [_init(OTHER_SESSION_ID), _result()]])
def test_backend_rejects_missing_or_wrong_native_session(tmp_path, events):
    backend = _fake_native_backend(tmp_path, events)
    with pytest.raises(RuntimeExecutionError) as caught:
        _execute_native(backend, _workspace(tmp_path), NativeSession(tmp_path / "config", SESSION_ID))
    assert caught.value.failure.code is ErrorCode.OUTCOME_INVALID


def test_backend_saves_init_before_cancelled_process_exits(tmp_path):
    signal, saved = _Signal(), []
    def remember(identifier):
        saved.append(identifier)
        signal.cancel(CancellationReason.USER_CANCEL)
    backend = _fake_native_backend(tmp_path, [_init()], hang=True)
    with pytest.raises(RuntimeExecutionError) as caught:
        _execute_native(backend, _workspace(tmp_path),
            NativeSession(tmp_path / "config", on_session_id=remember), signal=signal)
    assert saved == [SESSION_ID]
    assert caught.value.failure.code is ErrorCode.BACKEND_CANCELLED
