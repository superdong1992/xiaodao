from __future__ import annotations

import json
from pathlib import Path

from problem_locator.runtime.methods_skill import (
    ResolvedSpecializedSkillV1,
    load_specialized_skill_registration,
)


_WIKI_SHA256 = "1" * 64


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def load_test_methods_skill(
    parent: Path,
    *,
    name: str,
    methods: tuple[tuple[str, str], ...],
) -> ResolvedSpecializedSkillV1:
    """Write untrusted Skill inputs and resolve them with the production loader."""

    registration_root = parent / name
    package_root = registration_root / "package" / name
    references = package_root / "references"
    references.mkdir(parents=True)
    (package_root / "SKILL.md").write_text(
        f"""---
name: {name}
description: Diagnose test evidence with an exact evaluation plan.
---

# Test locator

Read frozen `request.json` and the compact `evaluation_input` from runtime
context. Use request values for declared inputs. Its `observations` catalog
deduplicated physical log lines, `markers` catalog declared literals, and
ordered `evaluations` contain the `events` available to each method. Log
evidence comes only from `evaluation_input`; do not rescan markers or target
logs. Evaluate every `evaluation_ref` in `evaluation_input.evaluations` order and return only
`evaluation_ref`, `verdict`, `supporting_event_refs`, and `reason`; use
`UNKNOWN` when the evidence cannot decide the rule.
""",
        encoding="utf-8",
    )
    cards: list[dict[str, object]] = []
    for priority, (method_id, marker) in enumerate(methods, start=1):
        reference = f"references/{method_id}.md"
        (package_root / reference).write_text(
            f"""# {method_id}

## 适用条件
Use for the requested operation.

## 所需证据
`{marker}`

## 计算与判断
Use the frozen evidence event.

## 确认条件
The positive marker is present.

## 未知边界
Missing evidence leaves the verdict unknown.

## 输出含义
Return one verdict, the selected supporting event refs, and one reason for the
evaluation reference.
""",
            encoding="utf-8",
        )
        cards.append(
            {
                "id": method_id,
                "title": method_id,
                "reference": reference,
                "priority": priority,
                "evidence_markers": [marker],
                "activation_markers": [marker],
            }
        )
    (references / "source-log-templates.md").write_text(
        "# Source log templates\n\n```text\n"
        + "\n".join(marker for _, marker in methods)
        + "\n```\n",
        encoding="utf-8",
    )
    _write_json(
        package_root / "methods.json",
        {
            "schema_version": 1,
            "skill_name": name,
            "source_wiki_sha256": _WIKI_SHA256,
            "required_user_inputs": [],
            "required_artifacts": [],
            "log_derived_fields": ["request_id"],
            "shared_references": ["references/source-log-templates.md"],
            "methods": cards,
        },
    )
    diagnose_role = {
        "agent_profile_id": "agent-profile/specialist",
        "tool_bundle_id": "tool-bundle/diagnose",
        "context_policy_id": "context-policy/diagnose",
        "output_contract_id": "output-contract/diagnose",
    }
    review_role = {
        "agent_profile_id": "agent-profile/reviewer",
        "tool_bundle_id": "tool-bundle/review",
        "context_policy_id": "context-policy/review",
        "output_contract_id": "output-contract/review",
    }
    _write_json(
        registration_root / "registration-template.json",
        {
            "schema_version": 1,
            "registration_id": name,
            "version": "1.0.0",
            "capability": "test",
            "deployment_scope": "PRODUCTION",
            "summary": "Test Methods V2 runtime primitives.",
            "package": {
                "relative_path": f"package/{name}",
                "skill_name": name,
                "source_wiki_sha256": _WIKI_SHA256,
            },
            "runtime": {
                "diagnose": diagnose_role,
                "review": review_role,
                "preprocessing": {
                    "requires_logparse": False,
                    "logparse_product": None,
                    "roles": [],
                    "logparse_plan": None,
                },
            },
        },
    )
    return load_specialized_skill_registration(registration_root)


__all__ = ["load_test_methods_skill"]
