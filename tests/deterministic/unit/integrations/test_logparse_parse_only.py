from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from problem_locator.contracts import (
    ErrorCode,
    ExecutionStage,
    Job,
    JobType,
    ResolvedLogparseParseOnlyPlanInput,
    WorkspaceInputManifest,
    canonical_json_bytes,
)
from problem_locator.integrations.logparse import cli
from problem_locator.integrations.logparse import outputs as outputs_module
from problem_locator.integrations.logparse import tree as tree_module
from problem_locator.integrations.logparse.outputs import (
    generic_parse_result,
    inspect_controlled_run,
    validate_generic_parse_result,
)
from problem_locator.integrations.logparse.paths import validate_proposal_io_paths
from problem_locator.integrations.logparse.requests import (
    ParseOnlyRequest,
    ResolvedParseOnlyPlan,
)
from problem_locator.runtime.failures import RuntimeExecutionError, runtime_failure
from tests.deterministic.contracts.fakes import InMemoryCancellationSignal
from tests.deterministic.unit.integrations.test_logparse_fake_e2e import (
    ATTACHMENT_ID,
    _attachment,
    _factory,
    _job,
    _parse_request,
    _write_read_only,
    _write_request,
    pinned_asset,
)
from tests.deterministic.unit.integrations.test_logparse_outputs import (
    _client_log,
    _generate_fake_run,
    _server_log,
)


SOURCE_SHA256 = hashlib.sha256(b"VALID").hexdigest()


def _result(root: Path) -> bytes:
    return generic_parse_result(
        inspect_controlled_run(root, product="compact"),
        source_attachment_id=ATTACHMENT_ID,
        source_attachment_sha256=SOURCE_SHA256,
    )


def _validate(root: Path, payload: bytes) -> dict:
    return validate_generic_parse_result(
        payload,
        controlled_root=root,
        product="compact",
        source_attachment_id=ATTACHMENT_ID,
        source_attachment_sha256=SOURCE_SHA256,
    )


def test_parse_only_lists_all_verified_logs_without_parameters(tmp_path: Path) -> None:
    root = _generate_fake_run(tmp_path)
    (root / "task-synthetic" / "unrelated.log").write_bytes(b"not a mechanism log")
    payload = _result(root)
    result = _validate(root, payload)
    paths = [item["relative_path"] for item in result["logs"]]
    assert paths == sorted([
        _client_log(root).relative_to(root).as_posix(),
        _server_log(root).relative_to(root).as_posix(),
    ])
    for item in result["logs"]:
        content = (root / item["relative_path"]).read_bytes()
        assert item["size"] == len(content)
        assert item["sha256"] == hashlib.sha256(content).hexdigest()
        assert content not in payload
    assert "problem_time" not in result
    assert "anchors" not in result


def test_parse_only_accepts_an_empty_log_inventory(tmp_path: Path) -> None:
    root = _generate_fake_run(tmp_path)
    _client_log(root).unlink()
    _server_log(root).unlink()
    assert _validate(root, _result(root))["logs"] == []


@pytest.mark.parametrize("content", [b"bad\xff", b"binary\x00content"])
def test_parse_only_rejects_non_text_logs(tmp_path: Path, content: bytes) -> None:
    root = _generate_fake_run(tmp_path)
    _client_log(root).write_bytes(content)
    with pytest.raises(ValueError, match="UTF-8|binary"):
        _result(root)


@pytest.mark.parametrize("mutation", ["omit", "escape", "source", "hash", "extra"])
def test_parse_only_rejects_forged_or_incomplete_inventory(tmp_path: Path, mutation: str) -> None:
    root = _generate_fake_run(tmp_path)
    result = json.loads(_result(root))
    if mutation == "omit":
        result["logs"].pop()
    elif mutation == "escape":
        result["logs"][0]["relative_path"] = "../outside.log"
    elif mutation == "source":
        result["source_attachment_sha256"] = "0" * 64
    elif mutation == "hash":
        result["logs"][0]["sha256"] = "0" * 64
    else:
        result["unexpected"] = True
    with pytest.raises(ValueError, match="differs"):
        _validate(root, canonical_json_bytes(result))


def test_parse_only_rejects_changed_logs_and_oversized_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _generate_fake_run(tmp_path)
    payload = _result(root)
    _client_log(root).write_bytes(b"changed\n")
    with pytest.raises(ValueError, match="differs"):
        _validate(root, payload)
    run = inspect_controlled_run(root, product="compact")
    monkeypatch.setattr(outputs_module, "_MAX_MACHINE_RESULT_BYTES", 100)
    with pytest.raises(ValueError, match="limit"):
        generic_parse_result(run, source_attachment_id=ATTACHMENT_ID, source_attachment_sha256=SOURCE_SHA256)


@pytest.mark.parametrize("reader", ["tree", "text"])
@pytest.mark.parametrize("code", [ErrorCode.BACKEND_CANCELLED, ErrorCode.BACKEND_TIMEOUT])
def test_parse_scans_abort_between_chunks(tmp_path: Path, reader: str, code: ErrorCode) -> None:
    source = tmp_path / "large.log"
    content = b"x" * (3 * 1024 * 1024)
    source.write_bytes(content)
    failure = runtime_failure(stage=ExecutionStage.TOOL_EXECUTE, code=code, message="停止日志扫描。")
    checks = 0

    def abort() -> None:
        nonlocal checks
        checks += 1
        # The text reader checks once before opening and once per chunk.
        if checks == (3 if reader == "text" else 2):
            raise failure

    with pytest.raises(RuntimeExecutionError) as raised:
        if reader == "tree":
            tree_module._file_sha256(source, check_abort=abort)
        else:
            outputs_module._validate_text_log(tmp_path, source.name, len(content),
                hashlib.sha256(content).hexdigest(), check_abort=abort)
    assert raised.value is failure


@pytest.mark.parametrize("stage", ["tree", "inventory", "revalidation"])
def test_parse_scan_entry_points_keep_abort_failure(tmp_path: Path, stage: str) -> None:
    root = _generate_fake_run(tmp_path)
    run = inspect_controlled_run(root, product="compact")
    payload = _result(root)
    failure = runtime_failure(stage=ExecutionStage.TOOL_EXECUTE,
        code=ErrorCode.BACKEND_TIMEOUT, message="日志扫描超时。")

    def abort() -> None:
        raise failure

    with pytest.raises(RuntimeExecutionError) as raised:
        if stage == "tree":
            inspect_controlled_run(root, product="compact", check_abort=abort)
        elif stage == "inventory":
            generic_parse_result(run, source_attachment_id=ATTACHMENT_ID,
                source_attachment_sha256=SOURCE_SHA256, check_abort=abort)
        else:
            validate_generic_parse_result(payload, controlled_root=root, product="compact",
                source_attachment_id=ATTACHMENT_ID, source_attachment_sha256=SOURCE_SHA256,
                check_abort=abort)
    assert raised.value is failure


def test_parse_only_request_has_no_target_or_product_fields(tmp_path: Path) -> None:
    request = ParseOnlyRequest(schema_version=1, attachment_id=ATTACHMENT_ID, artifact_proposal_key="generic")
    plan = ResolvedParseOnlyPlan(schema_version=1, operation="parse-only", attachment_id=ATTACHMENT_ID)
    plan.validate_request(request)
    with pytest.raises(ValueError):
        plan.validate_request(_parse_request("generic"))
    for field, value in (("problem_time", "2026-07-31T00:00:03.000Z"), ("anchors", []), ("logparse_product", "compact")):
        with pytest.raises(ValidationError):
            ParseOnlyRequest.model_validate({**request.model_dump(), field: value})
    request_path = "output/proposals/generic/request.json"
    result_path = "output/proposals/generic/generic_logs.json"
    assert validate_proposal_io_paths(request_path, result_path, operation="parse-only") == "generic"
    with pytest.raises(ValueError):
        validate_proposal_io_paths(request_path, result_path, operation="parse-targets")
    _write_request(tmp_path, "generic", request)
    assert cli._read_request(tmp_path, request_path, "parse-only") == canonical_json_bytes(request)


def test_parse_only_broker_calls_only_parse_and_rejects_repeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned_asset) -> None:
    values = _job(pinned_asset.ref).model_dump(mode="python")
    values.update(diagnosis_mode="GENERIC", generic_skill_name="generic-test", generic_problem_text="分析上传的日志。", context_snapshot=None, skill_ref=None, review_policy=None)
    job = Job.model_validate(values)
    workspace = tmp_path / "workspace"
    source = b"VALID"
    attachment = _attachment(source)
    _write_read_only(workspace / attachment.relative_path, source)
    manifest = WorkspaceInputManifest(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=JobType.DIAGNOSE,
        logparse_tool_ref=job.logparse_tool_ref,
        logparse_product=job.logparse_product,
        entries=[attachment],
        resolved_logparse_plan=ResolvedLogparseParseOnlyPlanInput(schema_version=1, operation="parse-only", attachment_id=ATTACHMENT_ID),
    )
    _write_read_only(workspace / "inputs/manifest.json", canonical_json_bytes(manifest))
    record_path = tmp_path / "invocations.json"
    monkeypatch.setenv("S07_FAKE_LOGPARSE_RECORD", os.fspath(record_path))
    session = _factory(pinned_asset).open(job, workspace, manifest, InMemoryCancellationSignal())
    request = ParseOnlyRequest(schema_version=1, attachment_id=ATTACHMENT_ID, artifact_proposal_key="generic")
    _write_request(workspace, "generic", request)
    try:
        assert session.execute_preprocessing("parse-only", "output/proposals/generic/request.json", "output/proposals/generic/generic_logs.json") is None
        result = _validate(workspace / "output/proposals/generic/tree", (workspace / "output/proposals/generic/generic_logs.json").read_bytes())
        assert len(result["logs"]) == 2
        assert session._server is None
        audit = json.loads(session.audit_bytes())
        assert len(audit["operations"]) == 1
        assert audit["operations"][0]["operation"] == "parse-only"
        assert session.parse_request_bytes() == canonical_json_bytes(request)
        repeat = ParseOnlyRequest(schema_version=1, attachment_id=ATTACHMENT_ID, artifact_proposal_key="repeat")
        _write_request(workspace, "repeat", repeat)
        failure = session.execute_preprocessing("parse-only", "output/proposals/repeat/request.json", "output/proposals/repeat/generic_logs.json")
        assert failure is not None and failure.code is ErrorCode.LOGPARSE_FAILED
        record = json.loads(record_path.read_bytes())
        assert record["parse_count"] == 1
        assert record["target_logs_count"] == 0
        assert [call["command"] for call in record["invocations"]] == ["parse"]
        assert record["invocations"][0]["reserved_environment_present"] is False
    finally:
        session.close()
