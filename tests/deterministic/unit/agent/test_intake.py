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
    build_initial_problem_spec,
    build_intake_prompt,
    parse_intake_response,
    validate_intake_decision,
    intake_processing_receipt,
)
from problem_locator.agent import intake as intake_module
from problem_locator.contracts import InputRequirementConstraints, ProblemSpecInput, ROUTER_CONTEXT_BYTES
from problem_locator.contracts.limits import MAX_USER_TEXT_UTF8_BYTES
from problem_locator.contracts.models import derive_attachment_filename_suffix
from problem_locator.runtime.agent_backend import BackendExecution


_RAW = "视频卡顿。正常应流畅播放，实际画面停顿，影响会议室A。设备型号 X1。日志日期 2026-09-07。"


def _request(**kwargs) -> IntakeInput:
    return IntakeInput(**{
        "conversation_id": "conversation-1",
        "messages": [
            IntakeMessage(message_id="m1", role="USER", text=_RAW),
            IntakeMessage(message_id="a1", role="ASSISTANT", text="助手猜测网络故障。"),
        ],
        "frozen_problem_spec": build_initial_problem_spec(_RAW),
        **kwargs,
    })


def _value(name: str, value: str, **kwargs) -> IntakeValue:
    return IntakeValue(name=name, value=value, source_message_id=kwargs.pop("source_message_id", "m1"), source_quote=kwargs.pop("source_quote", value), **kwargs)


def _fields() -> list[IntakeValue]:
    return [
        _value("statement", _RAW),
        _value("actual_behavior", _RAW),
    ]


def _decision(**kwargs) -> IntakeDecision:
    return IntakeDecision(**{
        "action": "NEED_CLARIFICATION", "message": "请提供设备型号。",
        "problem_fields": _fields(), "user_facts": [], **kwargs,
    })


def _constraints(**kwargs) -> InputRequirementConstraints:
    return InputRequirementConstraints(**{
        "value_type": "STRING", "min_utf8_bytes": 1, "max_utf8_bytes": 64,
        "pattern": None, "allowed_values": [], **kwargs,
    })


def _frozen_request(**kwargs) -> IntakeInput:
    return _request(**kwargs)


def _requirement(name, **constraints) -> IntakeRequirement:
    return IntakeRequirement(requirement_id="requirement-" + name, name=name,
        description="请提供 " + name + "。", constraints=_constraints(**constraints))


@pytest.mark.parametrize("raw_problem_text", ["视频卡顿", "  视频卡顿。\n设备型号 X1。  ", _RAW])
def test_initial_problem_spec_matches_current_mcp_client_create_example(raw_problem_text):
    skill = (Path(__file__).resolve().parents[4] / ".claude/skills/problem-locator-client/SKILL.md").read_text(encoding="utf-8")
    create_section = skill.split("`problem_locator_create_case`:\n", 1)[1]
    template = json.loads(create_section.split("```json\n", 1)[1].split("```", 1)[0])
    expected = {
        name: raw_problem_text if template[name] == "<raw_problem_text>" else template[name]
        for name in ProblemSpecInput.model_fields
    }
    assert build_initial_problem_spec(raw_problem_text).model_dump() == expected
    assert template["raw_problem_text"] == "<raw_problem_text>"
    assert template["initial_user_fact_names"] == template["initial_user_fact_values"] == []


@pytest.mark.parametrize("raw_problem_text", ["", " ", "\t\r\n", "\u3000"])
def test_initial_problem_spec_rejects_empty_or_whitespace_problem(raw_problem_text):
    with pytest.raises((IntakeError, ValidationError, ValueError)):
        build_initial_problem_spec(raw_problem_text)


def test_initial_defaults_cannot_be_submitted_as_user_facts():
    request = _request(requirements=[IntakeRequirement(
        requirement_id="r1", name="expected_behavior", description="预期表现", constraints=_constraints(max_utf8_bytes=256),
    )])
    default = request.frozen_problem_spec.expected_behavior
    assert default not in _RAW
    decision = _decision(action="SUBMIT_SUPPLEMENT", user_facts=[_value("expected_behavior", default)])
    result = validate_intake_decision(decision, request)
    assert result.action == "NEED_CLARIFICATION" and result.user_facts == []


def test_supplement_projects_exact_user_values_without_invented_facts():
    request = _request(requirements=[IntakeRequirement(
        requirement_id="r1", name="device_model", description="设备型号", constraints=_constraints(),
    )])
    decision = validate_intake_decision(_decision(action="SUBMIT_SUPPLEMENT", user_facts=[_value("device_model", "X1")]), request)
    assert decision.user_facts == [_value("device_model", "X1")]
    assert "problem_spec" not in decision.model_dump()
    assert "server_default_fields" not in decision.model_dump()


def test_clarification_retains_grounded_draft_for_following_turn():
    first = validate_intake_decision(_decision(problem_fields=_fields()[:1]), _request())
    assert first.draft == _fields()[:1]
    second = validate_intake_decision(_decision(problem_fields=_fields()[1:]), _request(draft=first.draft))
    assert second.draft == _fields()


@pytest.mark.parametrize("value", [
    _value("device_model", "X9"),
    _value("device_model", "X1", source_message_id="unknown"),
    _value("root_cause", "网络故障", source_message_id="a1"),
])
def test_unverified_fact_is_filtered_without_ending_intake(value):
    result = validate_intake_decision(_decision(user_facts=[value]),
        _request(requirements=[_requirement(value.name)]))
    assert result.action == "NEED_CLARIFICATION" and result.user_facts == []


def test_exact_value_can_be_selected_inside_a_longer_user_quote():
    fact = _value("device_model", "X1", source_quote="设备型号 X1")
    result = validate_intake_decision(_decision(user_facts=[fact]),
        _request(requirements=[_requirement("device_model")]))
    assert result.action == "SUBMIT_SUPPLEMENT" and result.user_facts == [fact]


@pytest.mark.parametrize("fields", [
    [*_fields(), _value("storage_key", "X1")],
    [*_fields(), _value("statement", _RAW)],
    [*_fields(), _value("goals", "X1"), _value("goals", "X1")],
])
def test_ignores_unknown_duplicate_or_reconstructed_problem_fields(fields):
    result = validate_intake_decision(_decision(problem_fields=fields), _request())
    assert result.action == "NEED_CLARIFICATION" and result.problem_fields == _fields()


def test_supplement_only_open_requirements_and_preserves_frozen_problem():
    request = _frozen_request(requirements=[IntakeRequirement(
        requirement_id="r1", name="device_model", description="请提供设备型号", constraints=_constraints(allowed_values=["X1"]),
    )])
    decision = validate_intake_decision(_decision(action="SUBMIT_SUPPLEMENT", problem_fields=[], user_facts=[_value("device_model", "X1")]), request)
    assert "problem_spec" not in decision.model_dump()
    assert decision.user_facts[0].name == "device_model"


@pytest.mark.parametrize("constraints", [
    _constraints(min_utf8_bytes=3),
    _constraints(max_utf8_bytes=1),
    _constraints(allowed_values=["Y2"]),
    _constraints(pattern=r"Y[0-9]+"),
])
@pytest.mark.parametrize("action", ["NEED_CLARIFICATION", "SUBMIT_SUPPLEMENT"])
def test_supplement_enforces_all_server_constraints(constraints, action):
    request = _frozen_request(requirements=[IntakeRequirement(requirement_id="r1", name="device_model", description="型号", constraints=constraints)])
    result = validate_intake_decision(_decision(action=action, problem_fields=[], user_facts=[_value("device_model", "X1")]), request)
    assert result.action == "NEED_CLARIFICATION" and result.user_facts == []


@pytest.mark.parametrize("action", ["NEED_CLARIFICATION", "SUBMIT_SUPPLEMENT"])
def test_supplement_rejects_non_open_inputs(action):
    result = validate_intake_decision(_decision(action=action, problem_fields=[], user_facts=[_value("device_model", "X1")]), _frozen_request())
    assert result.action == "NEED_CLARIFICATION" and result.user_facts == []


@pytest.mark.parametrize("action", ["NEED_CLARIFICATION", "SUBMIT_SUPPLEMENT"])
@pytest.mark.parametrize("complete", [False, True], ids=["partial-inputs", "complete-inputs"])
def test_valid_facts_submit_even_when_model_asks_again_or_other_inputs_are_missing(action, complete):
    request = _request(requirements=[_requirement("device_model"), _requirement("log_date")])
    facts = [_value("device_model", "X1")]
    if complete:
        facts.append(_value("log_date", "2026-09-07"))
    result = validate_intake_decision(_decision(action=action, user_facts=facts), request)
    assert result.action == "SUBMIT_SUPPLEMENT"
    assert result.user_facts == facts
    assert len(request.requirements) == 2
    # The same frozen request is used by engine and service validation/replay.
    assert validate_intake_decision(result, request) == result


@pytest.mark.parametrize("action", ["NEED_CLARIFICATION", "SUBMIT_SUPPLEMENT"])
@pytest.mark.parametrize("new_fact", [False, True], ids=["same-value-only", "same-value-plus-new-input"])
def test_frozen_same_value_is_removed_without_an_empty_supplement(action, new_fact):
    request = _request(frozen_user_facts={"device_model": "X1"},
        requirements=[_requirement("log_date")])
    facts = [_value("device_model", "X1")]
    if new_fact:
        facts.append(_value("log_date", "2026-09-07"))
    result = validate_intake_decision(_decision(action=action, user_facts=facts), request)
    assert result.action == ("SUBMIT_SUPPLEMENT" if new_fact else "NEED_CLARIFICATION")
    assert result.user_facts == (facts[1:] if new_fact else [])
    assert validate_intake_decision(result, request) == result


@pytest.mark.parametrize("origin", ["current", "draft"])
def test_frozen_conflict_discards_other_valid_inputs_instead_of_partially_submitting(origin):
    conflict = _value("device_model", "X1")
    new_fact = _value("log_date", "2026-09-07")
    request = _request(frozen_user_facts={"device_model": "X0"},
        requirements=[_requirement("log_date")],
        draft_user_facts=[conflict] if origin == "draft" else [])
    result = validate_intake_decision(_decision(
        user_facts=([conflict] if origin == "current" else []) + [new_fact]), request)
    assert result.action == "NEW_CASE_REQUIRED"
    assert result.user_facts == result.problem_fields == []
    assert request.frozen_user_facts == {"device_model": "X0"}


def test_explicit_new_case_action_never_submits_other_valid_facts():
    request = _request(requirements=[_requirement("device_model")])
    result = validate_intake_decision(_decision(action="NEW_CASE_REQUIRED",
        user_facts=[_value("device_model", "X1")]), request)
    assert result.action == "NEW_CASE_REQUIRED"
    assert result.user_facts == result.problem_fields == []


def test_prior_fact_draft_is_merged_when_model_only_returns_the_next_input():
    previous = _value("device_model", "X1")
    current = _value("log_date", "2026-09-07")
    request = _request(requirements=[_requirement("device_model"), _requirement("log_date")],
        draft_user_facts=[previous])
    result = validate_intake_decision(_decision(user_facts=[current]), request)
    assert result.action == "SUBMIT_SUPPLEMENT"
    assert result.user_facts == [previous, current]
    assert request.draft_user_facts == [previous]
    assert validate_intake_decision(result, request) == result


def test_current_value_replaces_same_name_unfrozen_draft_before_current_constraint_check():
    request = _request(requirements=[_requirement("device_model", allowed_values=["X2"])],
        messages=[*_request().messages, IntakeMessage(message_id="m2", role="USER", text="型号应为 X2。")],
        draft_user_facts=[_value("device_model", "X1")])
    replacement = _value("device_model", "X2", source_message_id="m2")
    result = validate_intake_decision(_decision(user_facts=[replacement]), request)
    assert result.action == "SUBMIT_SUPPLEMENT"
    assert result.user_facts == [replacement]
    assert request.draft_user_facts[0].value == "X1"


@pytest.mark.parametrize("constraints", [
    {"min_utf8_bytes": 3}, {"max_utf8_bytes": 1},
    {"allowed_values": ["X2"]}, {"pattern": "X2"},
])
def test_unreplaced_legacy_fact_draft_is_revalidated_against_current_constraints(constraints):
    request = _request(requirements=[_requirement("device_model", **constraints)],
        draft_user_facts=[_value("device_model", "X1")])
    result = validate_intake_decision(_decision(), request)
    assert result.action == "NEED_CLARIFICATION" and result.user_facts == []


@pytest.mark.parametrize("action", ["NEED_CLARIFICATION", "SUBMIT_SUPPLEMENT"])
def test_closed_and_already_frozen_legacy_drafts_are_removed(action):
    request = _request(frozen_user_facts={"device_model": "X1"},
        requirements=[_requirement("log_date")],
        draft_user_facts=[_value("device_model", "X1"), _value("closed_input", "会议室A")])
    result = validate_intake_decision(_decision(action=action), request)
    assert result.action == "NEED_CLARIFICATION" and result.user_facts == []
    prompt_data = json.loads(build_intake_prompt(request).splitlines()[-2])
    assert prompt_data["draft_user_facts"] == []
    assert prompt_data["frozen_user_facts"] == {"device_model": "X1"}


def test_mixed_valid_and_unknown_new_facts_keep_valid_subset_with_receipt():
    request = _request(requirements=[_requirement("device_model")])
    result = validate_intake_decision(_decision(user_facts=[
        _value("device_model", "X1"), _value("unknown_name", "会议室A")]), request)
    assert result.action == "SUBMIT_SUPPLEMENT" and result.user_facts == [_value("device_model", "X1")]
    assert intake_processing_receipt(result)["items"] == [
        {"field": "user_facts[1]", "reason": "UNKNOWN_OR_CLOSED_INPUT"}]


def test_fact_draft_has_a_separate_default_field_and_preserves_user_sources_in_prompt():
    original = _request().model_dump(mode="json")
    del original["draft_user_facts"]
    assert IntakeInput.model_validate(original).draft_user_facts == []
    fact = _value("device_model", "X1")
    request = _request(requirements=[_requirement("device_model")], draft_user_facts=[fact])
    prompt = build_intake_prompt(request)
    payload = json.loads(prompt.splitlines()[-2])
    assert payload["draft_user_facts"] == [fact.model_dump(mode="json")]
    assert payload["frozen_user_facts"] == {}
    assert payload["messages"] == request.model_dump(mode="json")["messages"]
    assert "第一条完整描述" in prompt and "部分有效参数" in prompt


def test_current_fact_draft_provenance_is_checked_before_model_and_before_adoption():
    request = _request(requirements=[_requirement("device_model")],
        draft_user_facts=[_value("device_model", "X9")])
    payload = json.loads(build_intake_prompt(request).splitlines()[-2])
    assert payload["draft_user_facts"] == []
    assert validate_intake_decision(_decision(), request).user_facts == []


def test_time_format_normalization_cannot_bypass_exact_user_quote():
    request = _request(requirements=[_requirement("problem_time", min_utf8_bytes=24,
        max_utf8_bytes=24, pattern=r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")])
    guessed = _value("problem_time", "2026-09-07T00:00:00.000Z", source_quote="2026-09-07")
    result = validate_intake_decision(_decision(user_facts=[guessed]), request)
    assert result.action == "NEED_CLARIFICATION" and result.user_facts == []


def test_only_attachment_supplement_requires_verified_attachment_and_open_requirement():
    decision = _decision(action="SUBMIT_SUPPLEMENT", problem_fields=[])
    requirement = IntakeRequirement(requirement_id="r1", name="logs", description="日志", kind="ATTACHMENT")
    assert validate_intake_decision(decision, _frozen_request(requirements=[requirement])).action == "NEED_CLARIFICATION"
    attachment = IntakeAttachment(attachment_id="upload-1", file_name="logs.zip", media_type="application/zip", size_bytes=100, sha256="a" * 64)
    assert validate_intake_decision(decision, _frozen_request(requirements=[requirement], attachments=[attachment])).action == "SUBMIT_SUPPLEMENT"


@pytest.mark.parametrize("change", ["problem", "fact"])
def test_attempt_to_change_frozen_data_requires_new_case(change):
    request = _frozen_request(frozen_user_facts={"device_model": "X0"})
    decision = _decision(action="SUBMIT_SUPPLEMENT", problem_fields=[_value("scope", "设备型号 X1")] if change == "problem" else [], user_facts=[_value("device_model", "X1")] if change == "fact" else [])
    result = validate_intake_decision(decision, request)
    assert result.action == ("NEW_CASE_REQUIRED" if change == "fact" else "NEED_CLARIFICATION")
    assert result.problem_fields == result.user_facts == []
    assert request.frozen_user_facts == {"device_model": "X0"}


@pytest.mark.parametrize("action", ["CREATE_CASE", "DIAGNOSE", "UNKNOWN"])
def test_model_cannot_create_cases_or_choose_unknown_actions(action):
    payload = _decision().model_dump()
    payload["action"] = action
    with pytest.raises(ValidationError):
        IntakeDecision.model_validate(payload)
    with pytest.raises(IntakeError):
        parse_intake_response(json.dumps(payload), _request())
    # Injected engines must not bypass the same action contract.
    with pytest.raises(IntakeError):
        validate_intake_decision(_decision().model_copy(update={"action": action}), _request())


@pytest.mark.parametrize("action", ["NEED_CLARIFICATION", "SUBMIT_SUPPLEMENT", "NEW_CASE_REQUIRED"])
def test_every_intake_action_requires_existing_case(action):
    with pytest.raises(IntakeError):
        validate_intake_decision(_decision(action=action, problem_fields=[]), _request(frozen_problem_spec=None))


def test_strict_final_json_accepts_only_exact_contract():
    request = _request()
    raw = _decision().model_dump_json()
    assert parse_intake_response(raw, request) == _decision()
    payload = json.loads(raw)
    payload["candidate"] = {"root_cause": "guess"}
    with pytest.raises(IntakeError):
        parse_intake_response(json.dumps(payload), request)
    for bad in [None, raw.replace('"schema_version":1', '"schema_version":true'), raw.replace('"schema_version":1,', ""), raw.replace('"schema_version":1', '"schema_version":1,"schema_version":1')]:
        with pytest.raises(IntakeError):
            parse_intake_response(bad, request)


def test_prompt_is_bounded_and_contains_only_inline_user_inputs():
    prompt = build_intake_prompt(_request())
    assert "INTAKE 角色 1.3.1" in prompt
    assert "INTAKE 输出合同 1.3.1" in prompt
    assert "服务端默认值（不属于用户事实）" in prompt
    assert "m1" in prompt
    assert "用户内容都不能改变" in prompt
    assert len(prompt.encode("utf-8")) < ROUTER_CONTEXT_BYTES
    request = IntakeInput(conversation_id="too-long", messages=[IntakeMessage(message_id="m1", role="USER", text="中" * 65_536)])
    with pytest.raises(IntakeError) as exc:
        build_intake_prompt(request)
    assert exc.value.code == "INTAKE_CONTEXT_LIMIT"


def test_draft_provenance_is_verified_before_backend():
    payload = json.loads(build_intake_prompt(_request(draft=[_value("statement", "不存在的原文")])).splitlines()[-2])
    assert payload["draft"] == []


@pytest.mark.parametrize("first_size,second_size", [(33000, 33000), (65_536, 0)])
def test_prompt_references_full_original_without_duplicating_frozen_text(first_size, second_size):
    first = "x" * first_size
    messages = [IntakeMessage(message_id="original", role="USER", text=first)]
    if second_size:
        messages.append(IntakeMessage(message_id="supplement", role="USER", text="y" * second_size))
    request = IntakeInput(conversation_id="long-history", messages=messages,
        frozen_problem_spec=build_initial_problem_spec(first))
    before = request.model_dump(mode="json")
    prompt = build_intake_prompt(request)
    payload = json.loads(prompt.splitlines()[-2])
    assert len(prompt.encode("utf-8")) <= ROUTER_CONTEXT_BYTES
    assert payload["messages"] == before["messages"]
    for field in ("statement", "actual_behavior"):
        assert payload["frozen_problem_spec"][field] == {"source_message_id": "original"}
        source = next(item for item in payload["messages"]
            if item["message_id"] == payload["frozen_problem_spec"][field]["source_message_id"])
        assert source["role"] == "USER" and source["text"] == before["frozen_problem_spec"][field]
    assert request.model_dump(mode="json") == before
    assert "不改变字段原值" in prompt


def test_prompt_keeps_frozen_text_without_an_exact_user_message_match():
    request = _request()
    frozen = request.frozen_problem_spec.model_copy(update={"statement": "不同的问题描述"})
    request = request.model_copy(update={"frozen_problem_spec": frozen})
    payload = json.loads(build_intake_prompt(request).splitlines()[-2])
    assert payload["frozen_problem_spec"]["statement"] == "不同的问题描述"


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


@pytest.mark.parametrize("prefix,suffix,newline", [
    ("", "", "\n"),
    ("\ufeff", "", "\n"),
    ("```json\n", "\n```", "\n"),
    ("```\n", "\n```", "\n"),
    ("```json\r\n", "\r\n```", "\r\n"),
    ("\ufeff```json\r\n", "\r\n```", "\r\n"),
], ids=["plain", "bom", "whole-json-fence", "whole-bare-fence", "crlf", "bom-fence-crlf"])
def test_engine_reuses_backend_with_no_tools_broker_or_job(tmp_path: Path, prefix, suffix, newline):
    raw = _decision().model_dump_json(indent=2).replace("\n", newline)
    backend = _Backend(prefix + raw + suffix)
    engine = ClaudeIntakeEngine("not-launched", workspace_root=tmp_path / "workspaces", backend=backend)
    assert engine.intake(_request()) == _decision()
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


def test_engine_adopts_partial_historical_facts_without_another_model_call(tmp_path: Path):
    fact = _value("device_model", "X1")
    request = _request(requirements=[_requirement("device_model"), _requirement("log_date")])
    backend = _Backend(_decision(user_facts=[fact]).model_dump_json())
    engine = ClaudeIntakeEngine("not-launched", workspace_root=tmp_path, backend=backend)
    result = engine.intake(request)
    assert result.action == "SUBMIT_SUPPLEMENT"
    assert result.user_facts == [fact]
    assert len(backend.calls) == INTAKE_MAX_CALLS == 1
    assert validate_intake_decision(result, request) == result
    assert len(backend.calls) == 1


@pytest.mark.parametrize("shape", ["double-bom", "double-fence", "duplicate-key", "nan"])
def test_intake_model_output_keeps_strict_json_rejections_after_normalization(tmp_path: Path, shape):
    raw = _decision().model_dump_json()
    malformed = {
        "double-bom": "\ufeff\ufeff" + raw,
        "double-fence": "```json\n```json\n" + raw + "\n```\n```",
        "duplicate-key": "```json\n" + raw.replace('"schema_version":1', '"schema_version":1,"schema_version":1') + "\n```",
        "nan": "\ufeff" + raw.replace('"schema_version":1', '"schema_version":NaN'),
    }[shape]
    with pytest.raises(IntakeError) as parsed:
        parse_intake_response(malformed, _request())
    assert parsed.value.code == "INTAKE_OUTPUT_INVALID"
    backend = _Backend(malformed)
    engine = ClaudeIntakeEngine("not-launched", workspace_root=tmp_path, backend=backend)
    with pytest.raises(IntakeError) as executed:
        engine.intake(_request())
    assert executed.value.code == "INTAKE_OUTPUT_INVALID"
    assert INTAKE_MAX_CALLS == len(backend.calls) == 1


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
