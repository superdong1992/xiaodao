"""Best-effort model proposals never change the frozen task or guess input values."""
from __future__ import annotations

import json

import pytest

from problem_locator.agent.intake import (
    INTAKE_MAX_CALLS, ClaudeIntakeEngine, IntakeError, IntakeInput, IntakeMessage, IntakeRequirement,
    build_initial_problem_spec, build_intake_prompt,
    intake_processing_receipt, parse_intake_response, validate_intake_decision,
)
from problem_locator.contracts import InputRequirementConstraints
from problem_locator.runtime.input_profile import load_builtin_input_profile
from tests.deterministic.unit.agent.test_intake import _Backend, _decision, _request, _requirement, _value


def test_one_bad_proposal_does_not_discard_other_independent_values():
    request = _request(requirements=[_requirement("device_model"), _requirement("log_date"),
        _requirement("room", pattern="[0-9]+")])
    result = validate_intake_decision(_decision(user_facts=[
        _value("device_model", "X1", source_quote="设备型号 X1"),
        _value("device_model", "X1", source_quote="设备型号 X1"),
        _value("unknown", "会议室A"), _value("room", "会议室A"),
        _value("untrusted", "网络故障", source_message_id="a1"),
        _value("log_date", "2026-09-07"),
    ]), request)
    assert [(item.name, item.value) for item in result.user_facts] == [
        ("device_model", "X1"), ("log_date", "2026-09-07")]
    assert result.action == "SUBMIT_SUPPLEMENT"
    receipt = intake_processing_receipt(result)
    assert receipt["retained_fact_count"] == 2 and len(receipt["items"]) == 4
    assert len(receipt["source_sha256"]) == len(receipt["effective_sha256"]) == 64
    assert not any(text in json.dumps(receipt, ensure_ascii=False) for text in ["X1", "会议室", "网络", "log_date"])
    assert set(result.model_dump()) == {"schema_version", "action", "message", "problem_fields", "user_facts"}
    assert validate_intake_decision(result, request) == result
    assert intake_processing_receipt(validate_intake_decision(result, request)) == receipt
    receipt["items"].clear()
    assert intake_processing_receipt(result)["items"]


@pytest.mark.parametrize("item", [None, 7, "bad", {}, {"name": "device_model", "value": "X1"}])
def test_invalid_list_item_is_filtered_while_root_contract_stays_typed(item):
    request = _request(requirements=[_requirement("device_model")])
    raw = _decision(user_facts=[_value("device_model", "X1")]).model_dump(mode="json")
    raw["user_facts"].insert(0, item)
    result = parse_intake_response(json.dumps(raw), request)
    assert result.action == "SUBMIT_SUPPLEMENT" and result.user_facts == [_value("device_model", "X1")]
    assert intake_processing_receipt(result)["items"][0] == {
        "field": "user_facts[0]", "reason": "INVALID_ITEM_STRUCTURE"}


def test_problem_substring_does_not_discard_valid_skill_parameters_as_a_correction():
    request = _request(requirements=[_requirement("device_model")])
    result = validate_intake_decision(_decision(problem_fields=[_value("actual_behavior", "画面停顿")],
        user_facts=[_value("device_model", "X1")]), request)
    assert result.action == "SUBMIT_SUPPLEMENT" and result.problem_fields == []
    assert result.user_facts == [_value("device_model", "X1")]
    assert request.frozen_problem_spec == build_initial_problem_spec(request.messages[0].text)


def test_conflicting_unfrozen_values_are_not_arbitrarily_chosen_or_restored_from_draft():
    request = _request(requirements=[_requirement("device_model"), _requirement("log_date")],
        draft_user_facts=[_value("device_model", "X1")],
        messages=[*_request().messages, IntakeMessage(message_id="m2", role="USER", text="X2")])
    result = validate_intake_decision(_decision(user_facts=[_value("device_model", "X1"),
        _value("device_model", "X2", source_message_id="m2"), _value("log_date", "2026-09-07")]), request)
    assert result.user_facts == [_value("log_date", "2026-09-07")]


def _time_request(text, *, name="problem_time", constraints=None):
    template = load_builtin_input_profile()["global_requirements"][0]
    return IntakeInput(conversation_id="time", messages=[IntakeMessage(message_id="m1", role="USER", text=text)],
        frozen_problem_spec=build_initial_problem_spec(text), requirements=[IntakeRequirement(
            requirement_id="time-input", name=name, description="时间", constraints=InputRequirementConstraints.model_validate(
                constraints if constraints is not None else template["constraints"]))])


@pytest.mark.parametrize("original,expected", [
    ("2026-09-15T10:00:00Z", "2026-09-15T10:00:00.000Z"),
    ("2026-09-15T18:00:00+08:00", "2026-09-15T10:00:00.000Z"),
    ("2026-09-15T05:00:00.123-05:00", "2026-09-15T10:00:00.123Z"),
    ("2026-09-15T10:00:00.1Z", "2026-09-15T10:00:00.100Z"),
    ("2026-09-15T10:00:00.123000Z", "2026-09-15T10:00:00.123Z"),
    ("2026-09-16 10:00:00+08:00", "2026-09-16T02:00:00.000Z"),
    ("2026-09-16 02:00:00Z", "2026-09-16T02:00:00.000Z"),
    ("2026-09-15 21:00:00.123000-05:00", "2026-09-16T02:00:00.123Z"),
])
def test_explicit_iso_time_is_normalized_without_changing_the_user_quote(original, expected):
    request = _time_request("发生时间：" + original)
    proposal = _value("problem_time", original, source_quote="发生时间：" + original)
    result = validate_intake_decision(_decision(problem_fields=[], user_facts=[proposal]), request)
    assert result.action == "SUBMIT_SUPPLEMENT"
    assert result.user_facts[0].value == expected
    assert result.user_facts[0].source_quote == proposal.source_quote
    assert validate_intake_decision(result, request) == result
    assert intake_processing_receipt(result)["items"] == [
        {"field": "user_facts[0]", "reason": "UTC_TIME_NORMALIZED"}]
    frozen = request.model_copy(update={"requirements": [], "frozen_user_facts": {"problem_time": expected}})
    settled = validate_intake_decision(result, frozen)
    assert settled.action == "NEED_CLARIFICATION" and settled.user_facts == []


@pytest.mark.parametrize("original", ["2026-09-15", "2026-09-15 10:00:00", "10:00:00Z",
    "2026-09-15T10:00:00", "2026-09-15T10:00:00-00:00", "2026-09-15T10:00:00.1234Z",
    "2026-99-99T99:99:99.000Z", "2026-09-15T10:00:00+24:00",
    "2026-09-16  10:00:00+08:00", "2026-09-16\t10:00:00+08:00",
    "2026-09-16\u00a010:00:00+08:00", "2026-09-16\u300010:00:00+08:00",
    "2026-09-16 10:00:00-00:00", "2026-09-16 10:00:00.1234+08:00",
    "2026-09-16 10:00:00+24:00"])
def test_incomplete_ambiguous_invalid_or_lossy_time_is_left_as_missing(original):
    result = validate_intake_decision(_decision(problem_fields=[], user_facts=[_value("problem_time", original)]),
        _time_request(original))
    assert result.action == "NEED_CLARIFICATION" and result.user_facts == []


@pytest.mark.parametrize("name,changed", [("problem_time", True), ("device_model", False)])
def test_time_normalization_requires_the_exact_builtin_name_and_constraints(name, changed):
    original = "2026-09-15T10:00:00Z"
    constraints = dict(load_builtin_input_profile()["global_requirements"][0]["constraints"])
    if changed:
        constraints["max_utf8_bytes"] = 25
    request = _time_request(original, name=name, constraints=constraints)
    result = validate_intake_decision(_decision(problem_fields=[], user_facts=[_value(name, original)]), request)
    assert result.action == "NEED_CLARIFICATION" and result.user_facts == []


@pytest.mark.parametrize("raw_time", ["2026-09-15T18:00:00+08:00", "2026-09-15 18:00:00+08:00"])
def test_normalized_model_value_requires_a_reproducible_complete_time_in_its_quote(raw_time):
    request = _time_request(raw_time)
    exact = _value("problem_time", "2026-09-15T10:00:00.000Z", source_quote=raw_time)
    assert validate_intake_decision(_decision(problem_fields=[], user_facts=[exact]), request).user_facts == [exact]
    forged = exact.model_copy(update={"value": "2026-09-16T10:00:00.000Z"})
    assert validate_intake_decision(_decision(problem_fields=[], user_facts=[forged]), request).user_facts == []


@pytest.mark.parametrize("original,action", [
    ("2026-09-15T18:00:00+08:00", "NEED_CLARIFICATION"),
    ("2026-09-15T10:00:00Z", "NEED_CLARIFICATION"),
    ("2026-09-15T18:00:01+08:00", "NEW_CASE_REQUIRED"),
    ("2026-09-15 18:00:00+08:00", "NEED_CLARIFICATION"),
    ("2026-09-15 10:00:00Z", "NEED_CLARIFICATION"),
    ("2026-09-15 18:00:01+08:00", "NEW_CASE_REQUIRED"),
])
def test_repeated_frozen_time_compares_instants_using_its_frozen_constraint(original, action):
    request = _time_request(original)
    frozen = request.model_copy(update={"requirements": [],
        "frozen_input_requirements": request.requirements,
        "frozen_user_facts": {"problem_time": "2026-09-15T10:00:00.000Z"}})
    result = validate_intake_decision(_decision(problem_fields=[], user_facts=[_value("problem_time", original)]), frozen)
    assert result.action == action and result.user_facts == []
    assert "frozen_input_requirements" not in build_intake_prompt(frozen)
    assert validate_intake_decision(result, frozen) == result


@pytest.mark.parametrize("known_constraints", [False, True])
def test_frozen_time_equivalence_is_not_inferred_from_a_name_or_custom_constraints(known_constraints):
    original = "2026-09-15T18:00:00+08:00"
    request = _time_request(original)
    custom = request.requirements[0].model_copy(update={"constraints":
        request.requirements[0].constraints.model_copy(update={"max_utf8_bytes": 25})})
    frozen = request.model_copy(update={"requirements": [],
        "frozen_input_requirements": [custom] if known_constraints else [],
        "frozen_user_facts": {"problem_time": "2026-09-15T10:00:00.000Z"}})
    result = validate_intake_decision(_decision(problem_fields=[], user_facts=[_value("problem_time", original)]), frozen)
    assert result.action == "NEW_CASE_REQUIRED" and result.user_facts == []


@pytest.mark.parametrize("quote", [
    "2026-09-16  10:00:00+08:00",
    "2026-09-16\t10:00:00+08:00",
    "2026-09-16\u00a010:00:00+08:00",
    "2026-09-16 10:00:00-00:00",
    "2026-09-16 10:00:00+08:00 或 2026-09-16 11:00:00+08:00",
])
def test_space_time_replay_cannot_guess_from_invalid_or_ambiguous_quotes(quote):
    request = _time_request(quote)
    proposal = _value("problem_time", "2026-09-16T02:00:00.000Z", source_quote=quote)
    result = validate_intake_decision(_decision(problem_fields=[], user_facts=[proposal]), request)
    assert result.action == "NEED_CLARIFICATION" and result.user_facts == []
    assert intake_processing_receipt(result)["items"] == [
        {"field": "user_facts[0]", "reason": "INVALID_USER_SOURCE"}]


def test_space_time_normalization_does_not_expand_to_renamed_fields_or_identifiers():
    original = "2026-09-16 10:00:00+08:00"
    renamed = _time_request(original, name="incident_time")
    result = validate_intake_decision(_decision(problem_fields=[],
        user_facts=[_value("incident_time", original)]), renamed)
    assert result.action == "NEED_CLARIFICATION" and result.user_facts == []
    assert intake_processing_receipt(result)["items"][0]["reason"] == "INPUT_CONSTRAINT_MISMATCH"
    identifier = _time_request(original, name="device_model", constraints={
        "value_type": "STRING", "min_utf8_bytes": 1, "max_utf8_bytes": 64,
        "pattern": None, "allowed_values": [],
    })
    result = validate_intake_decision(_decision(problem_fields=[],
        user_facts=[_value("device_model", original)]), identifier)
    assert result.user_facts[0].value == original
    assert intake_processing_receipt(result) is None


@pytest.mark.parametrize("model_normalized", [False, True])
def test_engine_parser_adopts_space_time_in_one_call_without_losing_source(tmp_path, model_normalized):
    original = "2026-09-16 10:00:00.123+08:00"
    expected = "2026-09-16T02:00:00.123Z"
    text = "问题发生时间为 " + original + "。"
    request = _time_request(text)
    proposal = _value("problem_time", expected if model_normalized else original, source_quote=text)
    backend = _Backend(_decision(problem_fields=[], user_facts=[proposal]).model_dump_json())
    engine = ClaudeIntakeEngine("not-launched", workspace_root=tmp_path, backend=backend)
    result = engine.intake(request)
    assert len(backend.calls) == INTAKE_MAX_CALLS == 1
    assert backend.calls[0]["file_access"] == "none"
    assert backend.calls[0]["broker_environment"] is None
    assert "一个 ASCII 空格" in backend.calls[0]["prompt"]
    assert result.action == "SUBMIT_SUPPLEMENT"
    assert result.user_facts[0].value == expected
    assert result.user_facts[0].source_quote == text
    assert result.user_facts[0].source_message_id == "m1"
    receipt = intake_processing_receipt(result)
    assert receipt["items"] == [{"field": "user_facts[0]", "reason": "UTC_TIME_NORMALIZED"}]
    assert validate_intake_decision(result, request) == result
    assert intake_processing_receipt(validate_intake_decision(result, request)) == receipt


def test_excessive_json_nesting_stays_a_controlled_fatal_output_error():
    with pytest.raises(IntakeError) as error:
        parse_intake_response("[" * 10000 + "0" + "]" * 10000, _request())
    assert error.value.code == "INTAKE_OUTPUT_INVALID"
