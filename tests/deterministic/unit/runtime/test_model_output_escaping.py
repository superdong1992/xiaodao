"""Keep ROUTE's local compatibility exception out of other JSON boundaries."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from problem_locator.agent.intake import (
    IntakeError,
    IntakeInput,
    IntakeMessage,
    IntakeRequirement,
    build_initial_problem_spec,
    parse_intake_response,
)
from problem_locator.contracts import ErrorCode, InputRequirementConstraints, InvalidJsonBytesError, Job, WorkspaceInputManifest
from problem_locator.integrations.agent_json import parse_agent_json_bytes
from problem_locator.runtime.agent_telemetry import AgentStreamTelemetry, TelemetryTeeSink
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.final_response import parse_route_response, parse_specialist_response
from problem_locator.runtime.generic_locator import GENERIC_RESULT_V2_FILENAME, parse_generic_result
from problem_locator.runtime.model_json import parse_model_json_bytes
from problem_locator.runtime.output_reader import RejectedAgentOutputError, read_agent_output
from problem_locator.runtime.secret_redactor import StreamingSecretRedactor


_CONTRACTS = Path(__file__).parents[3] / "fixtures/contracts/positive"
_TEXT = (
    'rpc_timeout request_id="42"; don\'t change C:\\logs\\new; literal \\n; '
    '中文与 emoji 🧭; actual newline\nnext line'
)
_SECRET = b"unrelated-private-capability"


class _LogSink:
    def __init__(self):
        self.data = bytearray()

    def write(self, chunk):
        self.data.extend(chunk)

    def flush(self):
        pass

    def close(self):
        pass


def _from_stream(inner, *, ascii_only=False, ending=b"\r\n", chunk_size=1):
    """Use the same ordered sinks as AgentBackend, including redactor buffering."""
    events = (
        {"type": "system", "subtype": "init", "session_id": _SECRET.decode()},
        {"type": "result", "subtype": "success", "is_error": False, "result": inner},
    )
    wire = b"".join(
        json.dumps(event, ensure_ascii=ascii_only).encode("utf-8") + ending
        for event in events
    )
    log = _LogSink()
    stream = AgentStreamTelemetry()
    sink = StreamingSecretRedactor((_SECRET,), TelemetryTeeSink(log, stream), close_sink=False)
    width = chunk_size or len(wire)
    for offset in range(0, len(wire), width):
        sink.write(wire[offset:offset + width])
    sink.close()
    assert bytes(log.data) == wire.replace(_SECRET, b"*" * len(_SECRET))
    # An outer JSON decode removes exactly its own layer of string encoding.
    assert stream.final_result == inner
    return stream.final_result


def _intake_request(text):
    return IntakeInput(
        conversation_id="escaping-regression",
        messages=[IntakeMessage(message_id="m1", role="USER", text=text)],
        frozen_problem_spec=build_initial_problem_spec(text),
        requirements=[IntakeRequirement(requirement_id="escaping-device-model", name="device_model",
            description="设备型号", constraints=InputRequirementConstraints(value_type="STRING",
                min_utf8_bytes=1, max_utf8_bytes=65536, pattern=None, allowed_values=[]))],
    )


def _intake_value(text):
    source = {"value": text, "source_message_id": "m1", "source_quote": text}
    return {
        "schema_version": 1,
        "action": "SUBMIT_SUPPLEMENT",
        "message": text,
        "problem_fields": [{"name": "statement", **source}],
        "user_facts": [{"name": "device_model", **source}],
    }


def _specialist_value(text):
    return {
        "schema_version": 1,
        "status": "CONFIRMED",
        "confirmed_methods": ["rpc-call-timeout"],
        "candidate_methods": [],
        "evidence": [{
            "method_id": "rpc-call-timeout", "summary": text,
            "identity_tokens": ['request_id="42"'],
            "sources": [{
                "source_id": "client", "line_number": 1,
                "marker": "rpc_timeout", "line": text.splitlines()[0],
            }],
        }],
        "limitations": [text],
        "safety_notes": [],
    }


def _review_value(text):
    return {
        "schema_version": 1, "verdict": "PASS",
        "findings": [{
            "method_id": "rpc-call-timeout", "identity_tokens": ['request_id="42"'],
            "verdict": "PASS", "reason": text,
        }],
        "limitations": [text],
    }


@pytest.mark.parametrize("entry", ["route", "intake", "specialist"])
@pytest.mark.parametrize("ascii_only,ending,chunk_size", [
    (False, b"\n", None),
    (False, b"\r\n", 1),
    (True, b"\r\n", 7),
], ids=["utf8-whole-lf", "utf8-bytewise-crlf", "unicode-escapes-chunked-crlf"])
def test_nested_json_preserves_strings_through_redaction_telemetry_and_business_parser(
    entry, ascii_only, ending, chunk_size,
):
    job = Job.model_validate_json((_CONTRACTS / "job-route.json").read_bytes())
    value = {
        "route": {"skill_id": None, "reason": _TEXT, "confidence": 0.9},
        "intake": _intake_value(_TEXT),
        "specialist": _specialist_value(_TEXT),
    }[entry]
    inner = json.dumps(value, ensure_ascii=ascii_only)
    final = _from_stream(inner, ascii_only=ascii_only, ending=ending, chunk_size=chunk_size)
    if entry == "route":
        result = parse_route_response(final, job)
        assert result.draft.payload.reason == _TEXT
        assert result.draft.payload.confidence == 0.9
        assert result.draft.payload.skill_ref is None
    elif entry == "intake":
        result = parse_intake_response(final, _intake_request(_TEXT))
        assert result.model_dump(mode="json") == value
    else:
        # Reviewer policy changes evidence selection later, never string decoding.
        for preserve in (False, True):
            result = parse_specialist_response(final, preserve_evidence_items=preserve)
            assert json.loads(result.canonical_bytes) == value
            assert result.raw_bytes == inner.encode("utf-8")
            if not preserve:
                assert result.draft.evidence[0].summary == _TEXT
                assert result.draft.evidence[0].sources[0].line == _TEXT.splitlines()[0]


@pytest.mark.parametrize("entry", ["intake-message", "intake-fact", "specialist-summary", "specialist-source"])
def test_other_stream_entries_do_not_repair_unescaped_quotes_in_facts_or_evidence(entry):
    text = 'rpc_timeout request_id="42"; use "quoted"'
    value = _intake_value(text) if entry.startswith("intake") else _specialist_value(text)
    good = json.dumps(value)
    if entry.startswith("intake"):
        assert parse_intake_response(good, _intake_request(text)).model_dump(mode="json") == value
        field = "message" if entry == "intake-message" else "value"
    else:
        assert json.loads(parse_specialist_response(good).canonical_bytes) == value
        field = "summary" if entry == "specialist-summary" else "line"
    encoded = json.dumps(text)
    broken = encoded.replace(r'\"quoted\"', '"quoted"')
    assert broken != encoded
    bad = good.replace(f'"{field}": {encoded}', f'"{field}": {broken}', 1)
    assert bad != good
    # The event itself is still valid JSON; the embedded document is malformed.
    final = _from_stream(bad)
    if entry.startswith("intake"):
        with pytest.raises(IntakeError) as captured:
            parse_intake_response(final, _intake_request(text))
        assert captured.value.code == "INTAKE_OUTPUT_INVALID"
    else:
        for preserve in (False, True):
            with pytest.raises(RuntimeExecutionError) as captured:
                parse_specialist_response(final, preserve_evidence_items=preserve)
            assert captured.value.failure.code is ErrorCode.OUTCOME_INVALID


def _review_file(tmp_path, raw):
    for relative in ("inputs", "runtime/tool-state", "output"):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    path = tmp_path / "output/method-review.draft.json"
    path.write_bytes(raw)
    job = Job.model_validate_json((_CONTRACTS / "job-review.json").read_bytes())
    manifest = WorkspaceInputManifest.model_validate_json(
        (_CONTRACTS / "workspace-input-manifest-review.json").read_bytes(),
    )
    return path, job, manifest


@pytest.mark.parametrize("wrapped", [False, True], ids=["plain", "bom-fence-crlf"])
def test_reviewer_file_keeps_quote_escapes_and_original_bytes(tmp_path, wrapped):
    value = _review_value(_TEXT)
    raw = json.dumps(value, ensure_ascii=False, indent=2).replace("\n", "\r\n").encode("utf-8")
    if wrapped:
        raw = b"\xef\xbb\xbf```json\r\n" + raw + b"\r\n```"
    path, job, manifest = _review_file(tmp_path, raw)
    result = read_agent_output(tmp_path, job, manifest)
    assert result.draft.findings[0].reason == _TEXT
    assert result.draft.findings[0].identity_tokens == ('request_id="42"',)
    assert json.loads(result.canonical_bytes) == value
    assert result.raw_bytes == path.read_bytes() == raw


def test_reviewer_reason_does_not_inherit_route_reason_recovery(tmp_path):
    value = _review_value('use "quoted"')
    good = json.dumps(value).encode()
    path, job, manifest = _review_file(tmp_path, good)
    assert read_agent_output(tmp_path, job, manifest).draft.findings[0].reason == 'use "quoted"'
    bad = good.replace(b'\\"quoted\\"', b'"quoted"', 1)
    assert bad != good
    path.write_bytes(bad)
    with pytest.raises(RejectedAgentOutputError) as captured:
        read_agent_output(tmp_path, job, manifest)
    assert captured.value.failure.code is ErrorCode.OUTCOME_INVALID
    assert captured.value.raw_outcome_bytes == path.read_bytes() == bad


@pytest.mark.parametrize("parser", [parse_model_json_bytes, parse_agent_json_bytes])
def test_shared_model_and_agent_tool_json_parsers_do_not_gain_route_syntax_repair(parser):
    good = b'{"skill_id":null,"reason":"use \\"quoted\\"","confidence":0.9}'
    assert parser(good).value["reason"] == 'use "quoted"'
    bad = good.replace(b'\\"quoted\\"', b'"quoted"')
    with pytest.raises(InvalidJsonBytesError):
        parser(bad)


@pytest.mark.parametrize("header_ending", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_generic_markdown_preserves_json_examples_and_hash_without_repair(tmp_path, header_ending):
    body = (
        '# 诊断报告\r\n' + _TEXT + '\n\n```json\r\n'
        '{"reason":"use "quoted""}\r\n```\n'
    ).encode("utf-8")
    raw = b"<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>" + header_ending + body
    output = tmp_path / "output"
    output.mkdir()
    path = output / GENERIC_RESULT_V2_FILENAME
    path.write_bytes(raw)
    metadata = output.stat()
    workspace = SimpleNamespace(root=tmp_path, output_device=metadata.st_dev, output_inode=metadata.st_ino)
    result = parse_generic_result(workspace, skill_name="generic-diagnosis")
    assert result.report_markdown.encode("utf-8") == body
    assert result.report_utf8_size == len(body)
    assert result.report_sha256 == hashlib.sha256(body).hexdigest()
    assert path.read_bytes() == raw
