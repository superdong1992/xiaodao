"""Registered role context reaches public requirements and the real Intake prompt."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from problem_locator.agent.intake import INTAKE_RESOURCE_LIMITS, build_initial_problem_spec, build_intake_prompt
from problem_locator.agent.models import AgentMessage
from problem_locator.agent.service import AgentConversationService
from problem_locator.contracts import InputRequirementConstraints, PendingRequirement
from problem_locator.runtime.diagnosis_runtime import _methods_user_input_projection
from problem_locator.runtime.input_profile import canonical_profile_bytes, expand_profile_requirements
from problem_locator.runtime.methods_skill import load_specialized_skill_registration


_SKILL = Path(__file__).resolve().parents[4] / "tests/fixtures/components/runtime-catalog/skill-dir/rpc-log-analysis"
_JOB_ID = "00000000-0000-0000-0000-000000000001"


def _skill(descriptions=("发起 RPC 请求、等待响应的调用方。", "接收 RPC 请求并返回响应的服务方。")):
    loaded = load_specialized_skill_registration(_SKILL)
    preprocessing = loaded.registration.preprocessing
    roles = tuple({**role, "description": description}
                  for role, description in zip(preprocessing.roles, descriptions, strict=True))
    return replace(loaded, registration=replace(loaded.registration,
        preprocessing=replace(preprocessing, roles=roles, logparse_plan=deepcopy(preprocessing.logparse_plan))))


def _intake(projection, *, with_questions=False):
    requirements = []
    for index, name in enumerate(projection.active_required_names, start=2):
        template = projection.input_templates[name]
        requirements.append(PendingRequirement(
            requirement_id=f"00000000-0000-0000-0000-{index:012d}", kind="INPUT", name=name,
            prompt=template["prompt"], required=True,
            constraints=InputRequirementConstraints.model_validate(template["constraints"]),
            status="OPEN", requested_by_job_id=_JOB_ID, fulfilled_by_refs=[],
            supplement_policy=template["supplement_policy"],
        ))
    message = AgentMessage(message_id="original", request_id="first", text="调用方请求服务方时超时。",
        attachment_ids=[], status="APPLIED", created_at="2026-09-16T00:00:00Z")
    view = SimpleNamespace(conversation_id="role-context", attachments=[],
        current_questions=[item.prompt for item in requirements] if with_questions else [])
    case = SimpleNamespace(problem_spec=build_initial_problem_spec(message.text), user_facts=[],
                           pending_requirements=requirements)
    # This projection method has no I/O or mutable service dependencies.
    service = object.__new__(AgentConversationService)
    request = service._intake_input(view, [message], {}, case, [])
    prompt = build_intake_prompt(request)
    return requirements, request, prompt


def test_registered_role_descriptions_reach_intake_without_changing_profile_or_skill():
    skill = _skill()
    before_profile = canonical_profile_bytes()
    before_registration = deepcopy(skill.registration.preprocessing)
    projection = _methods_user_input_projection(skill, set())
    requirements, request, prompt = _intake(projection)
    by_name = {item.name: item for item in request.requirements}
    public = {item.name: item.prompt for item in requirements}
    prompt_data = json.loads(prompt.splitlines()[-2])
    assert {item["name"]: item["description"] for item in prompt_data["requirements"]} == public
    for role in skill.registration.preprocessing.roles:
        for field in ("slot", "process_name"):
            assert by_name[f"{role['label']}_{field}"].description == (
                f"请提供 {role['label']} 角色的 {field}。角色说明：{role['description']}"
            )
    assert tuple(by_name) == skill.methods.required_user_inputs
    assert by_name["caller_service"].description == "Provide the required Methods input 'caller_service'."
    assert "角色说明" not in by_name["rpc_method"].description
    assert not any(name.endswith("_module") for name in by_name)
    assert canonical_profile_bytes() == before_profile
    assert skill.registration.preprocessing == before_registration
    assert len(prompt.encode("utf-8")) < INTAKE_RESOURCE_LIMITS.context_bytes


def test_role_context_follows_user_fact_aliases_and_keeps_constraints():
    skill = _skill()
    aliases = {"client_slot": "requester_slot", "server_process_name": "responder_process"}
    preprocessing = skill.registration.preprocessing
    plan = deepcopy(preprocessing.logparse_plan)
    for anchor in plan["anchors"]:
        for binding in anchor.values():
            if isinstance(binding, dict) and binding.get("source") == "USER_FACT":
                binding["name"] = aliases.get(binding["name"], binding["name"])
    renamed = replace(skill, methods=replace(skill.methods,
        required_user_inputs=tuple(aliases.get(name, name) for name in skill.methods.required_user_inputs)),
        registration=replace(skill.registration, preprocessing=replace(preprocessing, logparse_plan=plan)))
    original = _methods_user_input_projection(skill, set())
    projection = _methods_user_input_projection(renamed, set())
    _, request, _ = _intake(projection)
    by_name = {item.name: item for item in request.requirements}
    for source, actual in aliases.items():
        assert source not in by_name
        assert by_name[actual].description == original.input_templates[source]["prompt"]
        assert by_name[actual].constraints.model_dump() == original.input_templates[source]["constraints"]
    assert tuple(by_name) == renamed.methods.required_user_inputs


def test_explicit_user_fact_module_gets_context_but_fixed_modules_add_no_input():
    skill = _skill()
    preprocessing = skill.registration.preprocessing
    plan = deepcopy(preprocessing.logparse_plan)
    plan["anchors"][0]["module"] = {"source": "USER_FACT", "name": "request_module"}
    changed = replace(skill, methods=replace(skill.methods,
        required_user_inputs=(*skill.methods.required_user_inputs, "request_module")),
        registration=replace(skill.registration, preprocessing=replace(preprocessing, logparse_plan=plan)))
    projection = _methods_user_input_projection(changed, set())
    _, request, _ = _intake(projection)
    by_name = {item.name: item for item in request.requirements}
    assert by_name["request_module"].description == (
        "请提供 client 角色的 module。角色说明：" + preprocessing.roles[0]["description"]
    )
    assert by_name["request_module"].constraints.max_utf8_bytes == 4096
    assert "client_module" not in by_name and "server_module" not in by_name
    assert plan["anchors"][1]["module"] == {"source": "SKILL_FIXED", "value": "compact"}


@pytest.mark.parametrize("shared_name", ["client_slot", "server_process_name", "problem_time"])
def test_shared_module_binding_preserves_the_winning_time_or_role_template(shared_name):
    skill = _skill()
    preprocessing = skill.registration.preprocessing
    plan = deepcopy(preprocessing.logparse_plan)
    plan["anchors"][0]["module"] = {"source": "USER_FACT", "name": shared_name}
    changed = replace(skill, registration=replace(skill.registration,
        preprocessing=replace(preprocessing, logparse_plan=plan)))
    baseline = _methods_user_input_projection(skill, set())
    projection = _methods_user_input_projection(changed, set())
    assert projection.input_templates[shared_name] == baseline.input_templates[shared_name]
    assert projection.active_required_names == baseline.active_required_names
    _, request, _ = _intake(projection)
    actual = next(item for item in request.requirements if item.name == shared_name)
    assert actual.description == baseline.input_templates[shared_name]["prompt"]
    assert "module" not in actual.description
    if shared_name == "problem_time":
        assert actual.description == "请提供毫秒精度 UTC 问题时间。"
        assert actual.constraints.min_utf8_bytes == actual.constraints.max_utf8_bytes == 24


@pytest.mark.parametrize("description", ["调用方负责发送请求。" * 1000, "🔍调用方🧭等待响应。" * 1000],
                         ids=["long-chinese", "long-emoji"])
def test_long_role_descriptions_are_bounded_without_splitting_utf8(description):
    skill = _skill((description, description))
    projection = _methods_user_input_projection(skill, set())
    _, request, prompt = _intake(projection)
    for requirement in request.requirements:
        if "角色说明：" not in requirement.description:
            continue
        shortened = requirement.description.split("角色说明：", 1)[1]
        assert shortened.endswith("…") and description.startswith(shortened[:-1])
        assert "\ufffd" not in shortened
        assert len(shortened.encode("utf-8")) <= 256
        assert len(requirement.description.encode("utf-8")) <= 512
    assert len(prompt.encode("utf-8")) < INTAKE_RESOURCE_LIMITS.context_bytes


@pytest.mark.parametrize("with_questions", [False, True], ids=["requirements-only", "repeated-public-questions"])
def test_twenty_roles_have_bounded_total_intake_prompt_growth(record_property, with_questions):
    skill = _skill()
    preprocessing = skill.registration.preprocessing
    roles, anchors, names = [], [], ["problem_time"]
    for index in range(20):
        label = f"role_{index}"
        roles.append({**preprocessing.roles[0], "label": label, "description": "🔍请求角色说明。" * 1000})
        anchors.append({"label": label, "module": {"source": "SKILL_FIXED", "value": "compact"},
            "slot": {"source": "USER_FACT", "name": label + "_slot"},
            "process_name": {"source": "USER_FACT", "name": label + "_process_name"}, "pid": None})
        names.extend([label + "_slot", label + "_process_name"])
    plan = {**preprocessing.logparse_plan, "anchors": anchors}
    changed = replace(skill, methods=replace(skill.methods, required_user_inputs=tuple(names)),
        registration=replace(skill.registration, preprocessing=replace(preprocessing, roles=tuple(roles), logparse_plan=plan)))
    projection = _methods_user_input_projection(changed, set())
    _, request, prompt = _intake(projection, with_questions=with_questions)
    base_templates = {item["name"]: item for item in expand_profile_requirements(roles, requires_logparse=True)}
    baseline_requirements = [item.model_copy(update={
        "description": base_templates[item.name]["prompt"],
    }) for item in request.requirements]
    baseline_messages = [item.model_copy(update={"text": "\n".join(
        requirement.description for requirement in baseline_requirements
    )}) if item.role == "ASSISTANT" else item for item in request.messages]
    baseline = request.model_copy(update={"requirements": baseline_requirements, "messages": baseline_messages})
    growth = len(prompt.encode("utf-8")) - len(build_intake_prompt(baseline).encode("utf-8"))
    assert len(request.requirements) == 41
    assert 0 < growth <= (2 if with_questions else 1) * 40 * (256 + len("角色说明：".encode("utf-8")))
    assert len(prompt.encode("utf-8")) < INTAKE_RESOURCE_LIMITS.context_bytes
    record_property("role_count", 20)
    record_property("input_requirement_count", 41)
    record_property("public_questions_repeated", with_questions)
    record_property("intake_prompt_growth_bytes", growth)
