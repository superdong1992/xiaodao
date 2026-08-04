from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
GENERATOR_PATH = (
    REPOSITORY_ROOT
    / ".claude/skills/wiki-to-diagnosis-skill/scripts/generate_diagnosis_skill.py"
)
VALIDATOR_PATH = (
    REPOSITORY_ROOT
    / ".claude/skills/wiki-to-diagnosis-skill/scripts/validate_generated_skill.py"
)
SPEC_ROOT = REPOSITORY_ROOT / "tests/fixtures/components/diagnosis-generator/specs"
RPC_WIKI = (
    REPOSITORY_ROOT
    / "tests/fixtures/components/logparse/wiki/service-takeover.md"
)


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator() -> Any:
    return _load(GENERATOR_PATH, "_problem_locator_generate_v3")


@pytest.fixture(scope="module")
def validator() -> Any:
    return _load(VALIDATOR_PATH, "_problem_locator_validate_v3")


def _manifest(skill_dir: Path) -> dict[str, Any]:
    raw = (skill_dir / "diagnosis-skill.json").read_bytes()
    value = json.loads(raw)
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    return value


@pytest.mark.parametrize(
    ("spec_name", "expected_names", "expected_product"),
    [
        (
            "rpc-service-takeover.json",
            [
                "caller_service",
                "server_service",
                "rpc_method",
                "problem_time",
                "log_archive",
                "order_id",
            ],
            "compact",
        ),
        (
            "database-deadlock.json",
            [
                "database_instance",
                "database_process",
                "incident_time",
                "database_logs",
                "victim_transaction_id",
            ],
            None,
        ),
        (
            "manual-triage.json",
            ["affected_component", "observed_symptom", "reproduction_steps"],
            None,
        ),
    ],
)
def test_three_heterogeneous_specs_generate_deterministically(
    generator: Any,
    validator: Any,
    tmp_path: Path,
    spec_name: str,
    expected_names: list[str],
    expected_product: str | None,
) -> None:
    spec = generator.load_generation_spec(SPEC_ROOT / spec_name)
    first = generator.generate_diagnosis_skill(spec, tmp_path)
    second = generator.generate_diagnosis_skill(spec, tmp_path)
    assert first.product_sha256 == second.product_sha256
    assert first.created is True
    assert second.created is False and second.replaced is False
    assert validator.validate_skill_directory(first.skill_dir).ok

    manifest = _manifest(first.skill_dir)
    assert manifest["schema_version"] == 2
    assert manifest["version"] == "3.0.5"
    assert [item["name"] for item in manifest["requirements"]] == expected_names
    assert all("required" not in item for item in manifest["requirements"])
    if expected_product is None:
        assert "logparse_product" not in manifest
    else:
        assert manifest["logparse_product"] == expected_product

    markdown = (first.skill_dir / "SKILL.md").read_text(encoding="utf-8")
    has_after_logparse = any(
        item["stage"] == "AFTER_LOGPARSE" for item in manifest["requirements"]
    )
    if has_after_logparse:
        assert "state_delta.add_evidence_bindings" in markdown
        assert "existing_evidence_id=null" in markdown
        assert "evidence_proposal_key" in markdown
        assert "续跑必须复用正式 Evidence 与 LOGPARSE_RUN" in markdown
    else:
        assert "续跑必须复用正式 Evidence 与 LOGPARSE_RUN" not in markdown
        assert "不存在 Logparse 或 parse 后补参阶段" in markdown


def test_default_product_is_effective_runtime_default_not_manifest_field(
    generator: Any,
) -> None:
    spec = generator.load_generation_spec(SPEC_ROOT / "database-deadlock.json")
    manifest = generator.diagnosis_skill_manifest(spec)
    assert spec.requires_logparse is True
    assert "logparse_product" not in manifest
    assert manifest["logparse_plan"]["problem_time_binding"] == {
        "source": "USER_FACT",
        "name": "incident_time",
    }


def test_parameter_isolation_keeps_rpc_fixture_out_of_other_products(
    generator: Any,
) -> None:
    forbidden = ("caller_service", "server_service", "rpc_method", "order_id")
    for name in ("database-deadlock.json", "manual-triage.json"):
        product = generator.render_product(generator.load_generation_spec(SPEC_ROOT / name))
        combined = b"\n".join(product.values()).decode("utf-8")
        assert not any(field in combined for field in forbidden)


def test_wiki_fence_is_the_same_rpc_machine_source(generator: Any) -> None:
    from_wiki = generator.build_spec_from_wiki(RPC_WIKI.read_text(encoding="utf-8"))
    from_spec = generator.load_generation_spec(SPEC_ROOT / "rpc-service-takeover.json")
    assert generator.render_product(from_wiki) == generator.render_product(from_spec)
    assert [anchor["slot"]["value"] for anchor in from_spec.logparse_plan["anchors"]] == [
        "slot_1",
        "slot_2",
    ]
    assert all(
        anchor["process_name"]["source"] == "SKILL_FIXED"
        for anchor in from_spec.logparse_plan["anchors"]
    )


def test_optional_requirement_is_rejected(generator: Any) -> None:
    value = json.loads((SPEC_ROOT / "manual-triage.json").read_text(encoding="utf-8"))
    value["requirements"][0]["required"] = False
    with pytest.raises(ValueError, match="requirement fields"):
        generator.GenerationSpec.from_mapping(value)


def test_non_logparse_forbids_after_stage_and_plan(generator: Any) -> None:
    value = json.loads((SPEC_ROOT / "manual-triage.json").read_text(encoding="utf-8"))
    value["requirements"][0]["stage"] = "AFTER_LOGPARSE"
    with pytest.raises(ValueError, match="ordered by stage|AFTER_LOGPARSE"):
        generator.GenerationSpec.from_mapping(value)


def test_logparse_archive_content_types_are_platform_fixed(generator: Any) -> None:
    value = json.loads(
        (SPEC_ROOT / "database-deadlock.json").read_text(encoding="utf-8")
    )
    attachment = next(item for item in value["requirements"] if item["kind"] == "ATTACHMENT")
    attachment["constraints"]["allowed_content_types"] = ["application/octet-stream"]
    with pytest.raises(ValueError, match="platform-fixed"):
        generator.GenerationSpec.from_mapping(value)


def test_logparse_archive_content_types_are_injected_without_mutating_author_input(
    generator: Any,
) -> None:
    value = json.loads(
        (SPEC_ROOT / "database-deadlock.json").read_text(encoding="utf-8")
    )
    attachment = next(item for item in value["requirements"] if item["kind"] == "ATTACHMENT")
    assert "allowed_content_types" not in attachment["constraints"]

    spec = generator.GenerationSpec.from_mapping(value)
    normalized = next(item for item in spec.requirements if item.kind == "ATTACHMENT")

    assert "allowed_content_types" not in attachment["constraints"]
    assert normalized.constraints["allowed_content_types"] == [
        "application/gzip",
        "application/zip",
        "application/x-tar",
    ]


def test_non_logparse_attachment_still_requires_business_content_types(
    generator: Any,
) -> None:
    value = json.loads((SPEC_ROOT / "manual-triage.json").read_text(encoding="utf-8"))
    value["requirements"].append(
        {
            "name": "manual_attachment",
            "kind": "ATTACHMENT",
            "stage": "INITIAL",
            "fulfillment_source": "READY_ATTACHMENT",
            "prompt": "请上传人工排查附件。",
            "constraints": {"min_count": 1, "max_count": 1},
        }
    )
    with pytest.raises(ValueError, match="ATTACHMENT constraints"):
        generator.GenerationSpec.from_mapping(value)


def test_same_version_semantic_overwrite_is_rejected(
    generator: Any,
    tmp_path: Path,
) -> None:
    spec = generator.load_generation_spec(SPEC_ROOT / "manual-triage.json")
    generator.generate_diagnosis_skill(spec, tmp_path)
    value = json.loads((SPEC_ROOT / "manual-triage.json").read_text(encoding="utf-8"))
    value["summary"] = "Changed semantics"
    changed = generator.GenerationSpec.from_mapping(value)
    with pytest.raises(ValueError, match="same Skill version"):
        generator.generate_diagnosis_skill(
            changed,
            tmp_path,
            replace_different_version=True,
        )


def test_generator_fixture_manifest_covers_all_specs() -> None:
    root = SPEC_ROOT.parent
    manifest = json.loads((root / "fixture-manifest.json").read_text(encoding="utf-8"))
    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and path.name != "fixture-manifest.json"
    }
    assert [entry["path"] for entry in manifest["files"]] == sorted(actual)
    for entry in manifest["files"]:
        data = actual[entry["path"]].read_bytes()
        assert entry["size"] == len(data)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
