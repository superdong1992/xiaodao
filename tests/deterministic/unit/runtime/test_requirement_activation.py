from __future__ import annotations

from copy import deepcopy

import pytest

from problem_locator.runtime.input_profile import (
    builtin_input_profile_sha256,
    canonical_profile_bytes,
    expand_profile_requirements,
    load_builtin_input_profile,
)
from problem_locator.runtime.requirement_activation import (
    resolve_requirements,
    validate_requirement_activation_contract,
)


def _roles() -> list[dict[str, str]]:
    return [
        {
            "label": "client",
            "description": "Calling process.",
            "presence": "REQUIRED",
            "source_reference": "Confirmed client role.",
        },
        {
            "label": "server",
            "description": "Serving process.",
            "presence": "OPTIONAL",
            "source_reference": "Confirmed server role.",
        },
    ]


def _wiki_input(
    name: str,
    *,
    requiredness: str,
    stage: str = "INITIAL",
    condition: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "kind": "INPUT",
        "stage": stage,
        "fulfillment_source": "USER_FACT",
        "prompt": f"Provide {name}.",
        "constraints": {
            "value_type": "STRING",
            "min_utf8_bytes": 1,
            "max_utf8_bytes": 256,
            "pattern": None,
            "allowed_values": [],
        },
        "supplement_policy": "NONE" if requiredness == "OPTIONAL" else "MISSING_ONLY",
        "origin": "WIKI",
        "role": None,
        "requiredness": requiredness,
        "activation_condition": condition,
        "source_reference": f"Confirmed {name} definition.",
    }


def _fact_condition(name: str, value: str) -> dict[str, object]:
    return {
        "any_of": [
            {
                "all_of": [
                    {
                        "source": "USER_FACT",
                        "name": name,
                        "operator": "EQUALS",
                        "value": value,
                    }
                ]
            }
        ]
    }


def _contract(*, rules: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "schema_version": 2,
        "observation_policies": [],
        "event_extractors": [],
        "rules": [] if rules is None else rules,
        "terminal_paths": [],
    }


def test_builtin_profile_is_canonical_and_expands_fixed_role_fields() -> None:
    profile = load_builtin_input_profile()
    assert profile["profile_id"] == "builtin-global-v1"
    assert builtin_input_profile_sha256(profile) == builtin_input_profile_sha256()
    assert canonical_profile_bytes(profile) == canonical_profile_bytes()

    requirements = expand_profile_requirements(_roles(), requires_logparse=True)
    by_name = {item["name"]: item for item in requirements}
    assert by_name["problem_time"]["requiredness"] == "REQUIRED"
    assert by_name["client_slot"]["requiredness"] == "REQUIRED"
    assert by_name["client_process_name"]["requiredness"] == "REQUIRED"
    assert by_name["client_pid"]["requiredness"] == "OPTIONAL"
    assert by_name["client_pid"]["supplement_policy"] == "NONE"
    assert by_name["log_archive"]["kind"] == "ATTACHMENT"


def test_required_role_requests_missing_inputs_and_attachment_but_never_pid() -> None:
    requirements = expand_profile_requirements(_roles(), requires_logparse=True)
    result = resolve_requirements(
        roles=_roles(),
        requirements=requirements,
        facts={},
        attachment_ready=False,
        after_logparse=False,
    )
    assert result.active_roles == ("client",)
    assert result.inactive_roles == ("server",)
    assert [item["name"] for item in result.requested_requirements] == [
        "problem_time",
        "client_slot",
        "client_process_name",
        "log_archive",
    ]
    assert "client_pid" in result.inactive_requirement_names


def test_optional_role_is_dormant_until_any_role_field_appears() -> None:
    requirements = expand_profile_requirements(_roles(), requires_logparse=True)
    base_facts = {
        "problem_time": "2026-08-15T00:00:00.000Z",
        "client_slot": "slot-1",
        "client_process_name": "caller",
    }
    dormant = resolve_requirements(
        roles=_roles(),
        requirements=requirements,
        facts=base_facts,
        attachment_ready=False,
        after_logparse=False,
    )
    assert dormant.active_roles == ("client",)
    assert [item["name"] for item in dormant.requested_requirements] == ["log_archive"]

    activated = resolve_requirements(
        roles=_roles(),
        requirements=requirements,
        facts=base_facts | {"server_pid": "202"},
        attachment_ready=False,
        after_logparse=False,
    )
    assert activated.active_roles == ("client", "server")
    assert [item["name"] for item in activated.requested_requirements] == [
        "server_slot",
        "server_process_name",
        "log_archive",
    ]


def test_required_optional_and_conditional_wiki_parameters_are_deterministic() -> None:
    roles: list[dict[str, str]] = []
    requirements = expand_profile_requirements(roles, requires_logparse=False)
    requirements.extend(
        [
            _wiki_input("service_name", requiredness="REQUIRED"),
            _wiki_input("api_name", requiredness="OPTIONAL"),
            _wiki_input("protocol", requiredness="OPTIONAL"),
            _wiki_input(
                "request_id",
                requiredness="CONDITIONAL",
                stage="AFTER_LOGPARSE",
                condition=_fact_condition("protocol", "standard"),
            ),
        ]
    )
    facts = {
        "problem_time": "2026-08-15T00:00:00.000Z",
        "service_name": "inventory",
    }
    inactive = resolve_requirements(
        roles=roles,
        requirements=requirements,
        facts=facts,
        attachment_ready=True,
        after_logparse=True,
    )
    assert inactive.requested_requirements == ()
    assert {"api_name", "protocol", "request_id"} <= set(
        inactive.inactive_requirement_names
    )

    active = resolve_requirements(
        roles=roles,
        requirements=requirements,
        facts=facts | {"protocol": "standard"},
        attachment_ready=True,
        after_logparse=True,
    )
    assert [item["name"] for item in active.requested_requirements] == ["request_id"]


@pytest.mark.parametrize(
    ("requirements", "message"),
    [
        (
            [
                _wiki_input(
                    "self_ref",
                    requiredness="CONDITIONAL",
                    condition=_fact_condition("self_ref", "yes"),
                )
            ],
            "another non-conditional",
        ),
        (
            [
                _wiki_input(
                    "left",
                    requiredness="CONDITIONAL",
                    condition=_fact_condition("right", "yes"),
                ),
                _wiki_input(
                    "right",
                    requiredness="CONDITIONAL",
                    condition=_fact_condition("left", "yes"),
                ),
            ],
            "another non-conditional",
        ),
        (
            [
                _wiki_input(
                    "unknown_ref",
                    requiredness="CONDITIONAL",
                    condition=_fact_condition("not_declared", "yes"),
                )
            ],
            "another non-conditional",
        ),
    ],
)
def test_fact_activation_rejects_self_cycles_and_illegal_sources(
    requirements: list[dict[str, object]],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_requirement_activation_contract(requirements, _contract())


def test_rule_activation_rejects_semantic_and_target_dependent_rules() -> None:
    condition = {
        "any_of": [
            {
                "all_of": [
                    {
                        "source": "RULE_RESULT",
                        "name": "activation_rule",
                        "operator": "EQUALS",
                        "value": "PASS",
                    }
                ]
            }
        ]
    }
    target = _wiki_input(
        "request_id",
        requiredness="CONDITIONAL",
        stage="AFTER_LOGPARSE",
        condition=condition,
    )
    semantic = {
        "id": "activation_rule",
        "kind": "SEMANTIC_CAUSALITY",
        "depends_on": [],
        "parameters": {},
    }
    with pytest.raises(ValueError, match="independent mechanical"):
        validate_requirement_activation_contract([target], _contract(rules=[semantic]))

    mechanical = deepcopy(semantic)
    mechanical["kind"] = "FACT_FIELD_EQUALS"
    mechanical["parameters"] = {"fact_name": "request_id"}
    with pytest.raises(ValueError, match="independent mechanical"):
        validate_requirement_activation_contract([target], _contract(rules=[mechanical]))
