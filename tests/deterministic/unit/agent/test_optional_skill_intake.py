"""Optional inputs share the existing grounded intake call and never create questions."""
from __future__ import annotations

from types import SimpleNamespace
import json
from pathlib import Path

from problem_locator.agent.intake import (
    INTAKE_PROFILE_VERSION, INTAKE_OUTPUT_VERSION, build_intake_prompt, validate_intake_decision,
)
from problem_locator.agent.service import AgentConversationService
from problem_locator.contracts import SupplementPolicy
from tests.deterministic.unit.application.test_optional_skill_inputs import _policy
from tests.deterministic.unit.application.test_external_commands import CASE_ID, _waiting_state
from tests.deterministic.unit.agent.test_intake import _request, _requirement, _decision, _value, _RAW


def test_user_supplied_optional_value_is_kept_without_an_open_requirement():
    request = _request(optional_inputs=[_requirement("device_model")])
    result = validate_intake_decision(_decision(user_facts=[_value("device_model", "X1")]), request)
    assert result.action == "SUBMIT_SUPPLEMENT"
    assert [(item.name, item.value) for item in result.user_facts] == [("device_model", "X1")]
    assert request.requirements == []
    assert "缺少时不追问" in build_intake_prompt(request)
    missing = validate_intake_decision(_decision(user_facts=[]), request)
    assert missing.user_facts == []
    assert request.requirements == []


def test_optional_input_still_requires_user_provenance_and_cannot_overwrite():
    request = _request(optional_inputs=[_requirement("device_model")])
    guessed = validate_intake_decision(_decision(user_facts=[_value("device_model", "X2")]), request)
    assert guessed.user_facts == []
    frozen = request.model_copy(update={"frozen_user_facts": {"device_model": "X2"}})
    corrected = validate_intake_decision(_decision(user_facts=[_value("device_model", "X1")]), frozen)
    assert corrected.action == "NEW_CASE_REQUIRED"


def test_agent_projects_pinned_optional_declarations_without_adding_questions():
    case = _waiting_state().cases[CASE_ID].case
    case.diagnosis_state.pending_requirements[0].supplement_policy = SupplementPolicy.MISSING_ONLY
    seen = []
    def policy(ref):
        seen.append(ref)
        return _policy(ref)
    service = object.__new__(AgentConversationService)
    service.application = SimpleNamespace(external_commands=SimpleNamespace(specialized_input_policy=policy))
    case_view = SimpleNamespace(problem_spec=case.diagnosis_state.problem_spec,
        user_facts=case.diagnosis_state.user_facts, pending_requirements=case.diagnosis_state.pending_requirements,
        selected_skill_ref=case.selected_skill_ref)
    view = SimpleNamespace(current_questions=["请提供 RPC 方法。"], conversation_id="conversation")
    request = service._intake_input(view, [SimpleNamespace(message_id="m1", text=_RAW)], {}, case_view, [])
    assert seen == [case.selected_skill_ref]
    assert [item.name for item in request.optional_inputs] == ["device_model"]
    assert [item.name for item in request.requirements] == ["rpc_method"]
    assert view.current_questions == ["请提供 RPC 方法。"]


def test_intake_assets_consistently_allow_optional_inputs():
    from problem_locator.agent import intake
    assets = Path(intake.__file__).resolve().parents[1] / "runtime/assets/intake"
    metadata = json.loads((assets / "asset.json").read_text(encoding="utf-8"))
    prompt = build_intake_prompt(_request(optional_inputs=[_requirement("device_model")]))
    assert metadata["version"] == metadata["profile"]["version"] == INTAKE_PROFILE_VERSION == "1.4.0"
    assert metadata["output_contract"]["version"] == INTAKE_OUTPUT_VERSION == "1.4.0"
    assert "只能补充 OPEN INPUT requirements" not in prompt
    assert "SUBMIT_SUPPLEMENT 仅限已有任务的 OPEN INPUT requirements" not in prompt
    assert "缺少时不追问、不补默认值" in prompt
