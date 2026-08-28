from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[4]
VALIDATOR = Path(
    os.environ.get(
        "TEST_WIKI_DIAGNOSIS_VALIDATOR",
        ROOT
        / ".agents"
        / "skills"
        / "wiki-to-diagnosis-skill"
        / "scripts"
        / "validate_generated_skill.py",
    )
)
LAN_VALIDATOR = Path(
    os.environ.get(
        "TEST_LAN_DIAGNOSIS_VALIDATOR",
        ROOT
        / ".claude"
        / "skills"
        / "wiki-to-logparse-diagnosis-skill"
        / "scripts"
        / "validate_generated_skill.py",
    )
)


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_deterministic_methods_validator", VALIDATOR
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_two_generators_share_the_methods_v2_agent_surface() -> None:
    package_validator = _load_validator()
    registration_validator = _load_module(
        LAN_VALIDATOR,
        "_deterministic_registration_validator",
    )

    assert package_validator.REQUIRED_SKILL_PHRASES == (
        registration_validator.REQUIRED_SKILL_PHRASES
    )
    for contract in (
        ROOT
        / ".agents/skills/wiki-to-diagnosis-skill/references/output-contract.md",
        ROOT
        / ".claude/skills/wiki-to-logparse-diagnosis-skill/references/output-contract.md",
    ):
        text = contract.read_text(encoding="utf-8")
        for phrase in package_validator.REQUIRED_SKILL_PHRASES:
            assert phrase in text
        assert "INSUFFICIENT_EVIDENCE" not in text
        for old_json_field in ('"target_logs":', '"identity_tokens":', '"sources":'):
            assert old_json_field not in text


def _write_package(
    root: Path,
    *,
    wiki_sha256: str,
    required_user_inputs: list[str] | None = None,
    log_derived_fields: list[str] | None = None,
    evidence_marker: str = "RPC timeout",
    reference_log_template: str | None = None,
    source_log_templates: list[str] | None = None,
) -> Path:
    package = root / "diagnose-rpc-timeout"
    references = package / "references"
    references.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        """---
name: diagnose-rpc-timeout
description: Diagnose one RPC timeout from frozen evidence.
---

Read request.json, method-evidence-graph.json, and method-evaluation-plan.json.
Use request values for declared inputs. Log evidence comes only from the
Evidence Graph and Evaluation Plan; do not rescan logs. Evaluate every
evaluation_ref in plan order and return only verdict and reason; use UNKNOWN
when the evidence cannot decide the method rule.
Server-produced evidence sources may originate from target_logs and retain
identity_tokens internally.
""",
        encoding="utf-8",
    )
    templates = (
        list(source_log_templates)
        if source_log_templates is not None
        else ([reference_log_template] if reference_log_template is not None else [])
    )
    (package / "methods.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skill_name": "diagnose-rpc-timeout",
                "source_wiki_sha256": wiki_sha256,
                "required_user_inputs": required_user_inputs or [],
                "required_artifacts": [],
                "log_derived_fields": log_derived_fields or [],
                "shared_references": ["references/source-log-templates.md"],
                "methods": [
                    {
                        "id": "rpc-timeout",
                        "title": "RPC timeout",
                        "reference": "references/rpc-timeout.md",
                        "priority": 1,
                        "evidence_markers": [evidence_marker],
                    }
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    reference_body = """# RPC timeout

## 适用条件
RPC timeout.
## 所需证据
Frozen log line.
{reference_log_template}
## 计算与判断
Use only the line.
## 确认条件
The marker is present.
## 未知边界
Missing logs remain unknown.
## 输出含义
Return one event with its source.
""".format(reference_log_template=reference_log_template or "")
    (references / "rpc-timeout.md").write_text(
        reference_body,
        encoding="utf-8",
    )
    (references / "source-log-templates.md").write_text(
        _load_validator()._render_source_log_templates(templates),
        encoding="utf-8",
    )
    return package


def test_canonical_validator_independently_recomputes_source_wiki_identity(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki.md"
    template = "RPC_TIMEOUT request_id={request_id}"
    wiki_bytes = (
        "# Authored Wiki\n\nRPC timeout is a positive marker.\n\n"
        f"```text\n{template}\n```\n"
    ).encode()
    wiki.write_bytes(wiki_bytes)
    expected = hashlib.sha256(wiki_bytes).hexdigest()
    package = _write_package(
        tmp_path,
        wiki_sha256=expected,
        log_derived_fields=["request_id"],
        evidence_marker="RPC_TIMEOUT request_id=",
        reference_log_template=template,
    )
    validator = _load_validator()

    accepted = validator.validate(package, wiki)
    assert accepted["ok"] is True
    assert accepted["source_wiki_sha256"] == expected

    manifest_path = package / "methods.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_wiki_sha256"] = "0" * 64
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    rejected = validator.validate(package, wiki)
    assert rejected["ok"] is False
    assert rejected["source_wiki_sha256"] == expected
    assert rejected["errors"] == [
        "source_wiki_sha256 does not match the supplied Wiki"
    ]


def test_generated_v2_skill_reads_request_for_required_user_inputs(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki.md"
    template = "RPC_TIMEOUT service={service} request_id={request_id}"
    wiki_bytes = (
        "# Authored Wiki\n\n"
        "The service value is a required user input used by the rule.\n\n"
        f"```text\n{template}\n```\n"
    ).encode()
    wiki.write_bytes(wiki_bytes)
    package = _write_package(
        tmp_path,
        wiki_sha256=hashlib.sha256(wiki_bytes).hexdigest(),
        required_user_inputs=["service"],
        log_derived_fields=["request_id"],
        evidence_marker="RPC_TIMEOUT service=",
        reference_log_template=template,
    )

    result = _load_validator().validate(package, wiki)
    skill_text = (package / "SKILL.md").read_text(encoding="utf-8")

    assert result["ok"] is True
    assert "request.json" in skill_text
    assert "method-evidence-graph.json" in skill_text
    assert "method-evaluation-plan.json" in skill_text
    assert all(field in skill_text for field in ("evaluation_ref", "verdict", "reason"))


def test_validator_requires_canonical_markers_and_named_field_order(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki.md"
    template = "RPC_TIMEOUT service={service} request_id={request_id} elapsed_us={elapsed_us}"
    wiki_bytes = f"""# Authored Wiki

RPC timeout is caused by the following positive log.

```text
{template}
```
""".encode()
    wiki.write_bytes(wiki_bytes)
    package = _write_package(
        tmp_path,
        wiki_sha256=hashlib.sha256(wiki_bytes).hexdigest(),
        required_user_inputs=["service"],
        log_derived_fields=["request_id", "elapsed_us"],
        evidence_marker="RPC_TIMEOUT service=",
        reference_log_template=template,
    )
    validator = _load_validator()
    assert validator.validate(package, wiki)["ok"] is True

    manifest_path = package / "methods.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for invalid_fields in (
        ["elapsed_us", "request_id"],
        ["request_id"],
        ["request_id", "elapsed_us", "invented_us"],
    ):
        manifest["log_derived_fields"] = invalid_fields
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        invalid = validator.validate(package, wiki)
        assert invalid["ok"] is False
        assert (
            "log_derived_fields must be the named Wiki log fields in first-appearance order, excluding required_user_inputs"
            in invalid["errors"]
        )

    manifest["log_derived_fields"] = ["request_id", "elapsed_us"]
    manifest["methods"][0]["evidence_markers"] = ["RPC_TIMEOUT"]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    shortened = validator.validate(package, wiki)
    assert shortened["ok"] is False
    assert shortened["errors"] == [
        "method 1 evidence marker is not a canonical stable Wiki log marker: RPC_TIMEOUT"
    ]


def test_validator_rejects_marker_from_another_method_reference(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki.md"
    templates = ["FIRST id={first_id}", "SECOND id={second_id}"]
    wiki_bytes = (
        "# Authored Wiki\n\n```text\n" + "\n".join(templates) + "\n```\n"
    ).encode("utf-8")
    wiki.write_bytes(wiki_bytes)
    package = _write_package(
        tmp_path,
        wiki_sha256=hashlib.sha256(wiki_bytes).hexdigest(),
        log_derived_fields=["first_id", "second_id"],
        evidence_marker="FIRST id=",
        reference_log_template=templates[0],
        source_log_templates=templates,
    )
    methods_path = package / "methods.json"
    manifest = json.loads(methods_path.read_text(encoding="utf-8"))
    manifest["methods"].append(
        {
            "id": "second-method",
            "title": "Second method",
            "reference": "references/second-method.md",
            "priority": 2,
            "evidence_markers": ["SECOND id="],
        }
    )
    methods_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    first_reference = (package / "references/rpc-timeout.md").read_text(
        encoding="utf-8"
    )
    (package / "references/second-method.md").write_text(
        first_reference.replace("# RPC timeout", "# Second method", 1).replace(
            templates[0], templates[1]
        ),
        encoding="utf-8",
    )
    validator = _load_validator()
    assert validator.validate(package, wiki)["ok"] is True

    manifest["methods"][0]["evidence_markers"] = ["SECOND id="]
    methods_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    rejected = validator.validate(package, wiki)
    assert rejected["ok"] is False
    assert rejected["errors"] == [
        "method 1 evidence marker is absent from its method reference: SECOND id="
    ]


def test_canonical_marker_fallback_uses_longest_literal_and_first_tie() -> None:
    validator = _load_validator()
    assert (
        validator._canonical_evidence_marker(
            "%s rpc %s call unsuccess, reqid(%u), timeout %u"
        )
        == "call unsuccess, reqid("
    )
    assert validator._canonical_evidence_marker("%s alpha %u bravo %s") == "alpha"


def test_source_identity_v2_mechanically_preserves_template_order_and_duplicates() -> None:
    validator = _load_validator()
    wiki_bytes = (
        "# Wiki\r\n\r\n"
        "  ```text  \r\n"
        "  API_COMPLETE service={service} cost_us={cost_us}  \r\n"
        "not a template\r\n"
        "%s legacy request %u\r\n"
        "API_COMPLETE service={service} cost_us={cost_us}\r\n"
        "```\r\n"
        "```json\r\nIGNORED field={field}\r\n```\r\n"
    ).encode("utf-8")
    templates = [
        "API_COMPLETE service={service} cost_us={cost_us}",
        "%s legacy request %u",
        "API_COMPLETE service={service} cost_us={cost_us}",
    ]

    identity = validator.build_source_wiki_identity(wiki_bytes, "inputs/wiki.md")

    assert identity == {
        "schema_version": 2,
        "algorithm": "sha256",
        "source_path": "inputs/wiki.md",
        "sha256": hashlib.sha256(wiki_bytes).hexdigest(),
        "log_template_extraction_version": 1,
        "log_templates": templates,
        "log_template_inventory_sha256": validator._log_template_inventory_sha256(
            templates
        ),
    }
    assert validator._render_source_log_templates(templates) == (
        "# Source log templates\n\n```text\n"
        + "\n".join(templates)
        + "\n```\n"
    )


def test_validator_reproduces_release_failure_when_three_full_templates_are_lost(
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    templates = [
        "RPC_TIMEOUT service={service} api={api}",
        "CLIENT_REQUEST request_id={request_id} start_us={start_us}",
        "LATE_RESPONSE request_id={request_id} cost_us={cost_us}",
        "API_COMPLETE service={service} api={api} start_us={start_us} end_us={end_us} cost_us={cost_us}",
        "QUEUE_HISTORY print_time_ms={print_time_ms} ordinal={ordinal} service={service} api={api} end_us={end_us} cost_us={cost_us} queue_us={queue_us} timeout_ms={timeout_ms}",
        "DEADLOOP_DETECTED service={service} api={api} start_us={start_us} current_us={current_us} request_us={request_us} timeout_ms={timeout_ms}",
    ]
    wiki = tmp_path / "wiki.md"
    wiki_bytes = ("# Wiki\n\n```text\n" + "\n".join(templates) + "\n```\n").encode()
    wiki.write_bytes(wiki_bytes)
    named_fields = validator._wiki_named_log_fields(templates)
    package = _write_package(
        tmp_path,
        wiki_sha256=hashlib.sha256(wiki_bytes).hexdigest(),
        log_derived_fields=named_fields,
        evidence_marker="RPC_TIMEOUT service=",
        reference_log_template=templates[0],
        source_log_templates=templates[:3],
    )

    rejected = validator.validate(package, wiki)

    assert rejected["ok"] is False
    assert (
        "references/source-log-templates.md must exactly match the mechanically extracted Wiki log template inventory"
        in rejected["errors"]
    )
    assert [
        error
        for error in rejected["errors"]
        if error.startswith("generated package lost Wiki log template:")
    ] == [
        f"generated package lost Wiki log template: {template}"
        for template in templates[3:]
    ]

    (package / "references/source-log-templates.md").write_text(
        validator._render_source_log_templates(templates), encoding="utf-8"
    )
    assert validator.validate(package, wiki)["ok"] is True


def test_validator_requires_fixed_inventory_bytes_first_and_shared_only(
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    templates = ["FIRST id={id}", "SECOND id={id}"]
    wiki = tmp_path / "wiki.md"
    wiki_bytes = ("# Wiki\n\n```text\n" + "\n".join(templates) + "\n```\n").encode()
    wiki.write_bytes(wiki_bytes)
    package = _write_package(
        tmp_path,
        wiki_sha256=hashlib.sha256(wiki_bytes).hexdigest(),
        log_derived_fields=["id"],
        evidence_marker="FIRST id=",
        reference_log_template=templates[0],
        source_log_templates=templates,
    )
    manifest_path = package / "methods.json"

    for invalid_templates in (
        list(reversed(templates)),
        [templates[0]],
        [*templates, "INVENTED value={value}"],
        [templates[0], "SECOND request_id={id}"],
    ):
        (package / "references/source-log-templates.md").write_text(
            validator._render_source_log_templates(invalid_templates), encoding="utf-8"
        )
        invalid = validator.validate(package, wiki)
        assert invalid["ok"] is False
        assert (
            "references/source-log-templates.md must exactly match the mechanically extracted Wiki log template inventory"
            in invalid["errors"]
        )

    (package / "references/source-log-templates.md").write_text(
        validator._render_source_log_templates(templates), encoding="utf-8"
    )
    (package / "references/shared-boundaries.md").write_text(
        "# Shared boundaries\n", encoding="utf-8"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["shared_references"] = [
        "references/shared-boundaries.md",
        "references/source-log-templates.md",
    ]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    wrong_order = validator.validate(package, wiki)
    assert (
        "shared_references must start with references/source-log-templates.md"
        in wrong_order["errors"]
    )

    manifest["shared_references"] = [
        "references/source-log-templates.md",
        "references/shared-boundaries.md",
    ]
    manifest["methods"][0]["reference"] = "references/source-log-templates.md"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    method_reference = validator.validate(package, wiki)
    assert (
        "method 1 must not use references/source-log-templates.md as its reference"
        in method_reference["errors"]
    )


def test_fixed_inventory_renderer_has_canonical_empty_bytes() -> None:
    validator = _load_validator()
    assert (
        validator._render_source_log_templates([])
        == "# Source log templates\n\n```text\n\n```\n"
    )


def test_release_semantic_oracle_matches_mechanical_wiki_extraction() -> None:
    validator = _load_validator()
    case_root = ROOT / "tests/cases/release/rpc-timeout-anonymized"
    wiki_text = (case_root / "input/wiki.md").read_text(encoding="utf-8")
    oracle = json.loads((case_root / "oracle.json").read_text(encoding="utf-8"))
    expected = oracle["expected_package"]
    templates = validator._wiki_log_templates(wiki_text)
    named_fields = validator._wiki_named_log_fields(templates)
    canonical_markers = set(validator._wiki_canonical_evidence_markers(templates))

    assert expected["required_log_derived_fields"] == [
        field
        for field in named_fields
        if field not in expected["required_user_inputs"]
    ]
    assert all(
        marker in canonical_markers
        for marker_set in expected["method_marker_sets"]
        for marker in marker_set["all_markers"]
    )
