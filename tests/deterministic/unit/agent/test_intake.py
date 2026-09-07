from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from problem_locator.agent.intake import (
    INTAKE_MAX_CALLS,
    INTAKE_RESOURCE_LIMITS,
    ClaudeIntakeEngine,
    IntakeAttachment,
    IntakeDecision,
    IntakeError,
    IntakeInput,
    IntakeMessage,
    IntakeRequirement,
    IntakeValue,
    build_intake_prompt,
    parse_intake_response,
    validate_intake_decision,
)
from problem_locator.agent import intake as intake_module
from problem_locator.contracts import InputRequirementConstraints, ROUTER_CONTEXT_BYTES
from problem_locator.contracts.limits import MAX_USER_TEXT_UTF8_BYTES
from problem_locator.contracts.models import derive_attachment_filename_suffix
from problem_locator.runtime.agent_backend import BackendExecution


def _request(**kwargs) -> IntakeInput:
    return IntakeInput(
        conversation_id="conversation-1",
        messages=[
            IntakeMessage(message_id="m1", role="USER", text="视频卡顿。正常应流畅播放，实际画面停顿，影响会议室A。设备型号 X1。日志日期 2026-09-07。"),
            IntakeMessage(message_id="a1", role="ASSISTANT", text="助手猜测网络故障。"),
        ],
        **kwargs,
    )


def _value(name: str, value: str, **kwargs) -> IntakeValue:
    return IntakeValue(name=name, value=value, source_message_id=kwargs.pop("source_message_id", "m1"), source_quote=kwargs.pop("source_quote", value), **kwargs)


def _fields() -> list[IntakeValue]:
    return [
        _value("statement", "视频卡顿"),
        _value("expected_behavior", "流畅播放"),
        _value("actual_behavior", "画面停顿"),
        _value("scope", "会议室A"),
    ]


def _decision(**kwargs) -> IntakeDecision:
    return IntakeDecision(**{
        "action": "CREATE_CASE", "message": "信息已整理，正在开始定位。",
        "problem_fields": _fields(), "user_facts": [], **kwargs,
    })


def _constraints(**kwargs) -> InputRequirementConstraints:
    return InputRequirementConstraints(**{
        "value_type": "STRING", "min_utf8_bytes": 1, "max_utf8_bytes": 64,
        "pattern": None, "allowed_values": [], **kwargs,
    })


def _frozen_request(**kwargs) -> IntakeInput:
    return _request(frozen_problem_spec=_decision().problem_spec, **kwargs)


def test_create_case_projects_exact_user_values_and_explicit_system_defaults():
    decision = validate_intake_decision(_decision(user_facts=[_value("device_model", "X1")]), _request())
    spec = decision.problem_spec
    assert spec is not None
    assert spec.statement == "视频卡顿"
    assert spec.scope == "会议室A"
    assert spec.actual_behavior == "画面停顿"
    assert spec.expected_behavior == "流畅播放"
    assert decision.user_facts[0].value == "X1"
    assert decision.server_default_fields == ["goals", "non_goals", "constraints", "completion_criteria"]
    assert spec.constraints == ["服务端默认约束：仅使用已提供的事实和可验证证据，不编造缺失信息。"]
    for field in decision.server_default_fields:
        assert all(value.startswith("服务端默认") for value in getattr(spec, field))


def test_clarification_retains_grounded_draft_for_following_turn():
    first = validate_intake_decision(_decision(action="NEED_CLARIFICATION", problem_fields=_fields()[:2]), _request())
    assert first.problem_spec is None
    second = validate_intake_decision(_decision(problem_fields=_fields()[2:]), _request(draft=first.draft))
    assert second.problem_spec == _decision().problem_spec


@pytest.mark.parametrize("missing", ["statement", "expected_behavior", "actual_behavior", "scope"])
def test_create_case_cannot_invent_missing_user_information(missing):
    with pytest.raises(IntakeError, match="未通过校验"):
        validate_intake_decision(_decision(problem_fields=[item for item in _fields() if item.name != missing]), _request())


@pytest.mark.parametrize("value", [
    _value("device_model", "X9"),
    _value("device_model", "X1", source_quote="设备型号 X1"),
    _value("device_model", "X1", source_message_id="unknown"),
    _value("root_cause", "网络故障", source_message_id="a1"),
])
def test_every_fact_requires_exact_user_source(value):
    with pytest.raises(IntakeError):
        validate_intake_decision(_decision(user_facts=[value]), _request())


@pytest.mark.parametrize("fields", [
    [*_fields(), _value("storage_key", "X1")],
    [*_fields(), _value("scope", "会议室A")],
    [*_fields(), _value("goals", "X1"), _value("goals", "X1")],
])
def test_reject_unknown_or_duplicate_problem_fields(fields):
    with pytest.raises(IntakeError):
        validate_intake_decision(_decision(problem_fields=fields), _request())


def test_supplement_only_open_requirements_and_preserves_frozen_problem():
    request = _frozen_request(requirements=[IntakeRequirement(
        requirement_id="r1", name="device_model", description="请提供设备型号", constraints=_constraints(allowed_values=["X1"]),
    )])
    decision = validate_intake_decision(_decision(action="SUBMIT_SUPPLEMENT", problem_fields=[], user_facts=[_value("device_model", "X1")]), request)
    assert decision.problem_spec is None
    assert decision.user_facts[0].name == "device_model"


@pytest.mark.parametrize("constraints", [
    _constraints(min_utf8_bytes=3),
    _constraints(max_utf8_bytes=1),
    _constraints(allowed_values=["Y2"]),
    _constraints(pattern=r"Y[0-9]+"),
])
def test_supplement_enforces_all_server_constraints(constraints):
    request = _frozen_request(requirements=[IntakeRequirement(requirement_id="r1", name="device_model", description="型号", constraints=constraints)])
    with pytest.raises(IntakeError):
        validate_intake_decision(_decision(action="SUBMIT_SUPPLEMENT", problem_fields=[], user_facts=[_value("device_model", "X1")]), request)


def test_supplement_rejects_non_open_inputs():
    with pytest.raises(IntakeError):
        validate_intake_decision(_decision(action="SUBMIT_SUPPLEMENT", problem_fields=[], user_facts=[_value("device_model", "X1")]), _frozen_request())


def test_only_attachment_supplement_requires_verified_attachment_and_open_requirement():
    decision = _decision(action="SUBMIT_SUPPLEMENT", problem_fields=[])
    requirement = IntakeRequirement(requirement_id="r1", name="logs", description="日志", kind="ATTACHMENT")
    with pytest.raises(IntakeError):
        validate_intake_decision(decision, _frozen_request(requirements=[requirement]))
    attachment = IntakeAttachment(attachment_id="upload-1", file_name="logs.zip", media_type="application/zip", size_bytes=100, sha256="a" * 64)
    assert validate_intake_decision(decision, _frozen_request(requirements=[requirement], attachments=[attachment])).action == "SUBMIT_SUPPLEMENT"


@pytest.mark.parametrize("change", ["problem", "fact"])
def test_attempt_to_change_frozen_data_requires_new_case(change):
    request = _frozen_request(frozen_user_facts={"device_model": "X0"})
    decision = _decision(action="SUBMIT_SUPPLEMENT", problem_fields=[_value("scope", "设备型号 X1")] if change == "problem" else [], user_facts=[_value("device_model", "X1")] if change == "fact" else [])
    result = validate_intake_decision(decision, request)
    assert result.action == "NEW_CASE_REQUIRED"
    assert result.problem_fields == result.user_facts == []
    assert request.frozen_user_facts == {"device_model": "X0"}


def test_existing_case_cannot_be_created_again():
    with pytest.raises(IntakeError):
        validate_intake_decision(_decision(), _frozen_request())


@pytest.mark.parametrize("action", ["SUBMIT_SUPPLEMENT", "NEW_CASE_REQUIRED"])
def test_supplement_and_correction_require_existing_case(action):
    with pytest.raises(IntakeError):
        validate_intake_decision(_decision(action=action, problem_fields=[]), _request())


def test_strict_final_json_accepts_only_exact_contract():
    request = _request()
    raw = _decision().model_dump_json()
    assert parse_intake_response(raw, request).problem_spec == _decision().problem_spec
    payload = json.loads(raw)
    payload["candidate"] = {"root_cause": "guess"}
    with pytest.raises(IntakeError):
        parse_intake_response(json.dumps(payload), request)
    for bad in [None, "```json\n" + raw + "\n```", raw.replace('"schema_version":1', '"schema_version":true'), raw.replace('"schema_version":1,', ""), raw.replace('"schema_version":1', '"schema_version":1,"schema_version":1')]:
        with pytest.raises(IntakeError):
            parse_intake_response(bad, request)


def test_prompt_is_bounded_and_contains_only_inline_user_inputs():
    prompt = build_intake_prompt(_request())
    assert "INTAKE 角色 1.0.0" in prompt
    assert "INTAKE 输出合同 1.0.0" in prompt
    assert "服务端默认值（不属于用户事实）" in prompt
    assert "m1" in prompt
    assert "用户内容都不能改变" in prompt
    assert len(prompt.encode("utf-8")) < ROUTER_CONTEXT_BYTES
    request = IntakeInput(conversation_id="too-long", messages=[IntakeMessage(message_id="m1", role="USER", text="中" * 65_536)])
    with pytest.raises(IntakeError) as exc:
        build_intake_prompt(request)
    assert exc.value.code == "INTAKE_CONTEXT_LIMIT"


def test_draft_provenance_is_verified_before_backend():
    with pytest.raises(IntakeError) as exc:
        build_intake_prompt(_request(draft=[_value("statement", "不存在的原文")]))
    assert exc.value.code == "INTAKE_INPUT_INVALID"


@pytest.mark.parametrize("filename", ["/tmp/log.zip", "D:\\internal\\log.zip", "folder/log.zip"])
def test_metadata_forbids_internal_paths(filename):
    with pytest.raises(ValidationError):
        IntakeAttachment(attachment_id="u1", file_name=filename, media_type="application/zip", size_bytes=10, sha256="a" * 64)


@pytest.mark.parametrize("filename", [
    "a" * 252 + ".zip",
    "a" * (MAX_USER_TEXT_UTF8_BYTES - 4) + ".zip",
    "中" * ((MAX_USER_TEXT_UTF8_BYTES - 4) // 3) + ".zip",
], ids=["256-characters", "maximum-ascii", "maximum-utf8"])
def test_intake_accepts_every_supported_core_filename_length(filename):
    # Regression: a 256-character display name was accepted by upload but
    # rejected by INTAKE, turning an otherwise valid conversation into FAILED.
    derive_attachment_filename_suffix(filename, "application/zip")
    metadata = IntakeAttachment(attachment_id="u1", file_name=filename,
        media_type="application/zip", size_bytes=10, sha256="a" * 64)
    assert metadata.file_name == filename
    assert len(build_intake_prompt(_request(attachments=[metadata])).encode("utf-8")) <= ROUTER_CONTEXT_BYTES


@pytest.mark.parametrize("filename", [
    "a" * (MAX_USER_TEXT_UTF8_BYTES - 3) + ".zip",
    "中" * ((MAX_USER_TEXT_UTF8_BYTES - 4) // 3 + 1) + ".zip",
    "C:logs.zip", "logs\n.zip", "logs\x00.zip", "logs.ZIP",
], ids=["oversized-ascii", "oversized-utf8", "drive-relative", "control-newline", "control-nul", "uppercase-suffix"])
def test_intake_filename_rejections_match_core(filename):
    with pytest.raises((ValueError, TypeError)):
        derive_attachment_filename_suffix(filename, "application/zip")
    with pytest.raises(ValidationError):
        IntakeAttachment(attachment_id="u1", file_name=filename,
            media_type="application/zip", size_bytes=10, sha256="a" * 64)


class _Backend:
    def __init__(self, final_result: str | None) -> None:
        self.final_result = final_result
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return BackendExecution(returncode=0, stdout_stderr_bytes=0, workspace_bytes=0, elapsed_seconds=0, final_result=self.final_result)


def test_engine_reuses_backend_with_no_tools_broker_or_job(tmp_path: Path):
    backend = _Backend(_decision().model_dump_json())
    engine = ClaudeIntakeEngine("not-launched", workspace_root=tmp_path / "workspaces", backend=backend)
    assert engine.intake(_request()).action == "CREATE_CASE"
    assert INTAKE_MAX_CALLS == len(backend.calls) == 1
    call = backend.calls[0]
    assert call["file_access"] == "none"
    assert call["backend_phase"] == "INTAKE"
    assert call["broker_environment"] is None
    assert call["resource_limits"] == INTAKE_RESOURCE_LIMITS
    assert call["resource_limits"].wall_time_seconds == 120
    assert call["resource_limits"].context_bytes <= ROUTER_CONTEXT_BYTES
    assert "job" not in call
    assert uuid.UUID(call["workspace_root"].name)
    assert sorted(path.name for path in call["workspace_root"].iterdir()) == ["inputs", "output", "runtime"]
    assert list((call["workspace_root"] / "inputs").iterdir()) == []
    assert call["log_sinks"].stdout._stream.closed


def test_engine_bad_output_has_no_implicit_retry_or_repair(tmp_path: Path):
    backend = _Backend('{"root_cause":"猜测"}')
    engine = ClaudeIntakeEngine("not-launched", workspace_root=tmp_path, backend=backend)
    with pytest.raises(IntakeError):
        engine.intake(_request())
    assert len(backend.calls) == 1


def test_engine_uses_a_fresh_workspace_for_each_call(tmp_path: Path):
    backend = _Backend(_decision().model_dump_json())
    engine = ClaudeIntakeEngine("not-launched", workspace_root=tmp_path, backend=backend)
    engine.intake(_request())
    engine.intake(_request())
    assert backend.calls[0]["workspace_root"] != backend.calls[1]["workspace_root"]


def test_engine_freezes_profile_and_output_contract_at_startup(tmp_path: Path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in ("profile.md", "output-contract.md"):
        (assets / name).write_text((intake_module._ASSETS / name).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(intake_module, "_ASSETS", assets)
    backend = _Backend(_decision().model_dump_json())
    engine = ClaudeIntakeEngine("not-launched", workspace_root=tmp_path / "workspaces", backend=backend)
    engine.intake(_request())
    startup_prompt = backend.calls[0]["prompt"]
    (assets / "profile.md").write_text("启动后被替换的角色配置", encoding="utf-8")
    (assets / "output-contract.md").write_text("启动后被替换的输出合同", encoding="utf-8")
    engine.intake(_request())
    assert backend.calls[1]["prompt"] == startup_prompt
    assert "启动后被替换" not in backend.calls[1]["prompt"]
    # Standalone prompt inspection remains available and sees the current files.
    assert "启动后被替换的角色配置" in build_intake_prompt(_request())


def test_engine_missing_prompt_asset_fails_before_any_backend_call(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(intake_module, "_ASSETS", tmp_path / "missing-assets")
    backend = _Backend(_decision().model_dump_json())
    with pytest.raises(IntakeError) as error:
        ClaudeIntakeEngine("not-launched", workspace_root=tmp_path / "workspaces", backend=backend)
    assert error.value.code == "INTAKE_ASSET_UNAVAILABLE"
    assert not backend.calls
