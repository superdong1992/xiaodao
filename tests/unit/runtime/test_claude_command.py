from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from problem_locator.contracts import ErrorCode, FixtureManifest
from problem_locator.runtime.claude_command import (
    ClaudeCommandError,
    parse_command_tokens,
    prepare_claude_command,
    sanitize_environment,
)
from problem_locator.runtime.secret_redactor import StreamingSecretRedactor


FIXTURE_ROOT = (
    Path(__file__).parents[2] / "fixtures" / "components" / "runtime-command"
)


class RecordingSink:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.flush_calls = 0
        self.close_calls = 0
        self.closed = False

    @property
    def data(self) -> bytes:
        return b"".join(self.writes)

    def write(self, chunk: bytes) -> None:
        if self.closed:
            raise ValueError("sink is closed")
        if not isinstance(chunk, bytes) or not chunk:
            raise ValueError("write requires non-empty bytes")
        self.writes.append(chunk)

    def flush(self) -> None:
        self.flush_calls += 1

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _golden_cases() -> list[dict[str, Any]]:
    payload = json.loads((FIXTURE_ROOT / "claude-command-golden.json").read_text())
    assert payload["schema_version"] == 1
    return payload["cases"]


@pytest.mark.parametrize("case", _golden_cases(), ids=lambda case: case["name"])
def test_claude_command_golden(case: dict[str, Any]) -> None:
    calls: list[list[str | None]] = []

    def resolve(command: str, *, path: str | None = None) -> str | None:
        calls.append([command, path])
        return case["which_results"].get(command)

    prepared = prepare_claude_command(
        case["command"],
        parent_environment=case["parent_environment"],
        broker_environment=case["broker_environment"],
        os_name=case["os_name"],
        which=resolve,
    )

    assert list(prepared.argv) == case["expected_argv"]
    assert prepared.environment == case["expected_environment"]
    assert calls == case["expected_which_calls"]


def test_leading_assignments_are_continuous_allow_empty_and_last_value_wins() -> None:
    argv, assignments = parse_command_tokens(
        "ONE=first EMPTY= ONE=last claude TWO=argument",
        os_name="posix",
    )

    assert argv == ("claude", "TWO=argument")
    assert assignments == {"ONE": "last", "EMPTY": ""}


def test_assignment_like_leading_token_with_invalid_name_is_rejected() -> None:
    with pytest.raises(ClaudeCommandError) as captured:
        parse_command_tokens("not-valid=value claude", os_name="posix")

    assert captured.value.code is ErrorCode.CONFIG_INVALID
    assert "value" not in str(captured.value)


def test_equals_token_after_executable_remains_an_argument() -> None:
    argv, assignments = parse_command_tokens(
        "claude not-valid=value --flag",
        os_name="posix",
    )

    assert argv == ("claude", "not-valid=value", "--flag")
    assert assignments == {}


def test_assignment_value_gets_issue_locator_matching_quote_cleanup() -> None:
    argv, assignments = parse_command_tokens(
        "VALUE='\"quoted\"' claude",
        os_name="posix",
    )

    assert argv == ("claude",)
    assert assignments == {"VALUE": "quoted"}


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        "ONLY=value EMPTY=",
        '""',
        'claude "unterminated',
    ],
)
def test_invalid_or_assignment_only_command_is_config_invalid(command: str) -> None:
    with pytest.raises(ClaudeCommandError) as captured:
        parse_command_tokens(command, os_name="posix")

    assert captured.value.code is ErrorCode.CONFIG_INVALID


@pytest.mark.parametrize(
    "name",
    [
        "LOGPARSE_REPO",
        "logparse_config_path",
        "LogParse_Python",
        "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT",
        "problem_locator_logparse_token",
        "Problem_Locator_Logparse_Anything",
    ],
)
def test_command_prefix_cannot_assign_reserved_logparse_keys(name: str) -> None:
    with pytest.raises(ClaudeCommandError) as captured:
        prepare_claude_command(
            f"{name}=not-allowed claude",
            parent_environment={},
            os_name="posix",
        )

    assert captured.value.code is ErrorCode.CONFIG_INVALID
    assert "not-allowed" not in str(captured.value)


def test_environment_is_copied_sanitized_case_insensitively_and_not_mutated() -> None:
    parent = {
        "KEEP": "yes",
        "LOGPARSE_REPO": "repo-value",
        "LogParse_Config_Path": "config-value",
        "logparse_python": "python-value",
        "problem_locator_logparse_old": "old-value",
    }
    snapshot = dict(parent)

    result = sanitize_environment(parent, {"KEEP": "overridden", "NEW": "value"})

    assert result == {"KEEP": "overridden", "NEW": "value"}
    assert parent == snapshot


def test_windows_command_environment_overrides_parent_case_insensitively() -> None:
    result = sanitize_environment(
        {"Path": "C:\\Parent", "KEEP": "yes"},
        {"PATH": "C:\\Command"},
        os_name="nt",
    )

    assert result == {"KEEP": "yes", "PATH": "C:\\Command"}


def test_only_current_broker_endpoint_and_token_are_injected() -> None:
    endpoint = "ipc://job-scoped-endpoint"
    token = "job-scoped-token"
    parent = {
        "KEEP": "parent",
        "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT": "stale-endpoint",
        "problem_locator_logparse_token": "stale-token",
    }

    prepared = prepare_claude_command(
        "KEEP=command claude --no-extra-arguments",
        parent_environment=parent,
        broker_environment={
            "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT": endpoint,
            "PROBLEM_LOCATOR_LOGPARSE_TOKEN": token,
        },
        os_name="posix",
    )

    assert prepared.argv == ("claude", "--no-extra-arguments")
    assert prepared.environment == {
        "KEEP": "command",
        "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT": endpoint,
        "PROBLEM_LOCATOR_LOGPARSE_TOKEN": token,
    }
    assert endpoint not in repr(prepared)
    assert token not in repr(prepared)


@pytest.mark.parametrize(
    "broker_environment",
    [
        {},
        {"PROBLEM_LOCATOR_LOGPARSE_ENDPOINT": "endpoint"},
        {
            "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT": "endpoint",
            "PROBLEM_LOCATOR_LOGPARSE_TOKEN": "",
        },
        {
            "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT": "endpoint",
            "PROBLEM_LOCATOR_LOGPARSE_TOKEN": "token",
            "PROBLEM_LOCATOR_LOGPARSE_EXTRA": "extra",
        },
    ],
)
def test_invalid_broker_environment_is_rejected(
    broker_environment: dict[str, str],
) -> None:
    with pytest.raises(ClaudeCommandError) as captured:
        prepare_claude_command(
            "claude",
            parent_environment={},
            broker_environment=broker_environment,
            os_name="posix",
        )

    assert captured.value.code is ErrorCode.CONFIG_INVALID


@pytest.mark.parametrize(
    "command",
    [
        "tools/claude --flag",
        "C:\\Tools\\claude --flag",
        "claude.exe --flag",
        "claude.cmd --flag",
        ".claude --flag",
    ],
)
def test_windows_explicit_path_or_extension_does_not_resolve_shim(command: str) -> None:
    def unexpected_resolver(command: str, *, path: str | None = None) -> str:
        raise AssertionError(f"unexpected resolver call: {command!r}, {path!r}")

    prepared = prepare_claude_command(
        command,
        parent_environment={"PATH": "C:\\Tools"},
        os_name="nt",
        which=unexpected_resolver,
    )

    assert prepared.argv[-1] == "--flag"


def test_windows_missing_shim_keeps_original_token_and_uses_casefolded_path() -> None:
    calls: list[tuple[str, str | None]] = []

    def missing(command: str, *, path: str | None = None) -> None:
        calls.append((command, path))
        return None

    prepared = prepare_claude_command(
        "claude --flag",
        parent_environment={"Path": "C:\\One;C:\\Two"},
        os_name="nt",
        which=missing,
    )

    assert prepared.argv == ("claude", "--flag")
    assert calls == [("claude", "C:\\One;C:\\Two")]


def test_no_broker_job_has_no_broker_keys() -> None:
    prepared = prepare_claude_command(
        "claude",
        parent_environment={
            "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT": "stale",
            "PROBLEM_LOCATOR_LOGPARSE_TOKEN": "stale",
        },
        broker_environment=None,
        os_name="posix",
    )

    assert prepared.environment == {}


def test_streaming_redactor_masks_multiple_secrets_across_chunk_boundaries() -> None:
    endpoint = b"ipc://job/endpoint"
    token = b"job-token-456"
    source = b"before " + endpoint + b" middle " + token + b" after"
    expected = (
        b"before "
        + b"*" * len(endpoint)
        + b" middle "
        + b"*" * len(token)
        + b" after"
    )
    sink = RecordingSink()
    redactor = StreamingSecretRedactor([endpoint, token], sink)

    boundaries = [5, 12, 19, 27, 34, 43, len(source)]
    start = 0
    for end in boundaries:
        redactor.write(source[start:end])
        start = end
    redactor.close()

    assert sink.data == expected
    assert len(sink.data) == len(source)
    assert endpoint not in sink.data
    assert token not in sink.data
    assert sink.flush_calls == 1
    assert sink.close_calls == 1


def test_streaming_redactor_masks_overlapping_secrets() -> None:
    sink = RecordingSink()
    redactor = StreamingSecretRedactor([b"aba", b"bab"], sink)

    redactor.write(b"ab")
    redactor.write(b"aba")
    redactor.close()

    assert sink.data == b"*****"


def test_flush_retains_possible_secret_tail_until_close() -> None:
    sink = RecordingSink()
    redactor = StreamingSecretRedactor([b"abcdef"], sink)

    redactor.write(b"prefix-abc")
    redactor.flush()
    assert sink.data == b"prefi"
    redactor.write(b"def-suffix")
    redactor.close()

    assert sink.data == b"prefix-******-suffix"
    assert sink.flush_calls == 2


def test_unicode_secret_can_cross_inside_a_utf8_code_point_chunk() -> None:
    secret = "令牌"
    encoded = secret.encode("utf-8")
    source = b"left-" + encoded + b"-right"
    sink = RecordingSink()
    redactor = StreamingSecretRedactor([secret], sink)

    split = source.index(encoded) + 1
    redactor.write(source[:split])
    redactor.write(source[split:])
    redactor.close()

    assert sink.data == b"left-" + b"*" * len(encoded) + b"-right"
    assert len(sink.data) == len(source)


def test_empty_secret_set_is_passthrough_and_close_is_idempotent() -> None:
    sink = RecordingSink()
    redactor = StreamingSecretRedactor([], sink)

    redactor.write(b"ordinary bytes")
    redactor.flush()
    redactor.close()
    redactor.close()
    redactor.flush()

    assert sink.data == b"ordinary bytes"
    assert sink.flush_calls == 2
    assert sink.close_calls == 1
    assert redactor.closed
    with pytest.raises(ValueError, match="closed"):
        redactor.write(b"later")


def test_redactor_repr_and_own_errors_do_not_reveal_secret() -> None:
    secret = b"do-not-print-this-token"
    sink = RecordingSink()
    redactor = StreamingSecretRedactor([secret], sink)

    assert secret.decode() not in repr(redactor)
    with pytest.raises(ValueError) as captured:
        redactor.write(b"")
    assert secret.decode() not in str(captured.value)
    redactor.close()


def test_runtime_command_fixture_manifest_is_complete_and_hash_valid() -> None:
    manifest_path = FIXTURE_ROOT / "fixture-manifest.json"
    manifest = FixtureManifest.model_validate_json(manifest_path.read_bytes())
    assert manifest.owner_spec == "S04"
    assert manifest.root == "tests/fixtures/components/runtime-command"

    actual = {
        path.relative_to(FIXTURE_ROOT).as_posix(): path
        for path in FIXTURE_ROOT.rglob("*")
        if path.is_file() and path != manifest_path
    }
    assert [entry.path for entry in manifest.files] == sorted(actual)
    for entry in manifest.files:
        data = actual[entry.path].read_bytes()
        assert entry.size == len(data)
        assert entry.sha256 == hashlib.sha256(data).hexdigest()
