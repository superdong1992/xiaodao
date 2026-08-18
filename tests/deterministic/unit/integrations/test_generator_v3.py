from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import importlib.util
import hashlib
import io
import json
from pathlib import Path
import re
import sys
from typing import Any
import zipfile

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
GENERATOR_PATH = (
    REPOSITORY_ROOT
    / ".claude/skills/wiki-to-diagnosis-skill/scripts/generate_diagnosis_skill.py"
)
VALIDATOR_PATH = (
    REPOSITORY_ROOT
    / ".claude/skills/wiki-to-diagnosis-skill/scripts/validate_generated_skill.py"
)
NEUTRAL_REFERENCE_SPEC = (
    REPOSITORY_ROOT
    / ".claude/skills/wiki-to-diagnosis-skill/references/neutral-logparse-generation-spec-v6.json"
)
SPEC_ROOT = REPOSITORY_ROOT / "tests/fixtures/components/diagnosis-generator/specs"
RPC_WIKI = (
    REPOSITORY_ROOT
    / "tests/fixtures/components/logparse/wiki/service-takeover.md"
)
RPC_ARCHIVE_B64 = (
    REPOSITORY_ROOT
    / "tests/fixtures/components/logparse/real/synthetic-rpc-service-takeover.zip.b64"
)
RELEASE_CASE_ROOT = REPOSITORY_ROOT / "tests/cases/release"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator() -> Any:
    return _load(GENERATOR_PATH, "_problem_locator_generate_v6")


@pytest.fixture(scope="module")
def validator() -> Any:
    return _load(VALIDATOR_PATH, "_problem_locator_validate_v6")


def _manifest(skill_dir: Path) -> dict[str, Any]:
    raw = (skill_dir / "diagnosis-skill.json").read_bytes()
    value = json.loads(raw)
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    return value


def test_self_contained_neutral_reference_is_a_valid_generation_spec(
    generator: Any,
) -> None:
    spec = generator.load_generation_spec(NEUTRAL_REFERENCE_SPEC)
    assert spec.skill_id == "diagnose-neutral-event-chain"
    assert spec.requires_logparse is True
    assert len(spec.verification_contract["event_extractors"]) == 2
    assert [
        item["resolution_status"]
        for item in spec.verification_contract["terminal_paths"]
    ] == ["COMPLETE", "PARTIAL", "NONE"]


def test_unknown_event_field_error_reports_only_controlled_location(
    generator: Any,
) -> None:
    payload = json.loads(NEUTRAL_REFERENCE_SPEC.read_bytes())
    sensitive_field = "private_model_field_must_not_leak"
    payload["verification_contract"]["rules"][3]["parameters"]["left"][
        "field"
    ] = sensitive_field

    with pytest.raises(ValueError) as caught:
        generator.GenerationSpec.from_mapping(payload)

    message = str(caught.value)
    assert message == (
        "verification_contract.rules[3] kind NUMERIC_COMPARE reference[0] "
        "names an unknown event field"
    )
    assert sensitive_field not in message


@pytest.mark.parametrize(
    ("spec_name", "expected_names", "expected_product"),
    [
        (
            "rpc-service-takeover.json",
            [
                "problem_time",
                "client_slot",
                "client_process_name",
                "client_pid",
                "server_slot",
                "server_process_name",
                "server_pid",
                "caller_service",
                "server_service",
                "rpc_method",
                "log_archive",
                "order_id",
            ],
            "compact",
        ),
        (
            "database-deadlock.json",
            [
                "problem_time",
                "database_slot",
                "database_process_name",
                "database_pid",
                "log_archive",
                "victim_transaction_id",
            ],
            None,
        ),
        (
            "manual-triage.json",
            [
                "problem_time",
                "affected_component",
                "observed_symptom",
                "reproduction_steps",
            ],
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
    assert manifest["schema_version"] == 6
    assert manifest["version"] == "6.0.0"
    assert manifest["deployment_scope"] == "TEST_ONLY"
    assert manifest["input_profile"]["profile_id"] == "builtin-global-v1"
    assert len(manifest["input_profile_sha256"]) == 64
    assert [item["name"] for item in manifest["requirements"]] == expected_names
    assert all("required" not in item for item in manifest["requirements"])
    assert all(
        item["requiredness"] in {"REQUIRED", "OPTIONAL", "CONDITIONAL"}
        for item in manifest["requirements"]
    )
    assert all(
        item["supplement_policy"] in {"NONE", "MISSING_ONLY"}
        for item in manifest["requirements"]
    )
    assert manifest["verification_contract"]["schema_version"] == 2
    assert all("confirmed" not in item for item in manifest["roles"])
    assert all("confirmed" not in item for item in manifest["requirements"])
    if expected_product is None:
        assert "logparse_product" not in manifest
    else:
        assert manifest["logparse_product"] == expected_product

    markdown = (first.skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "Agent 禁止提出或写入 `USER_RESULT`" in markdown
    assert "problem-locator-pack-result" not in markdown
    assert "服务端立即从已验证的" in markdown
    assert "独立 Review PASS 后开放公开下载" in markdown
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
        "name": "problem_time",
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
    assert [anchor["label"] for anchor in from_spec.logparse_plan["anchors"]] == [
        "client",
        "server",
    ]
    assert all(
        anchor["module"]["source"] == "SKILL_FIXED"
        for anchor in from_spec.logparse_plan["anchors"]
    )


def test_both_author_note_forms_are_conversion_only(generator: Any) -> None:
    wiki = "# title\n正文 A（#全角旁注#）\n正文 B(#ASCII note#)\n"
    stripped = generator.strip_author_notes(wiki)
    assert "正文 A" in stripped and "正文 B" in stripped
    assert "全角旁注" not in stripped and "ASCII note" not in stripped
    assert "(#" not in stripped and "（#" not in stripped

    with pytest.raises(ValueError, match="unterminated author note"):
        generator.strip_author_notes("正文(#未闭合")


@pytest.mark.parametrize(
    "case_root",
    sorted(path.parent for path in RELEASE_CASE_ROOT.glob("*/case.json")),
    ids=lambda path: path.name,
)
def test_release_case_approved_skill_is_the_deterministic_spec_product(
    generator: Any,
    validator: Any,
    case_root: Path,
) -> None:
    descriptor = json.loads((case_root / "case.json").read_text(encoding="utf-8"))
    spec = generator.load_generation_spec(case_root / descriptor["generation_spec"])
    approved = case_root / descriptor["approved_skill_dir"]
    rendered = generator.render_product(spec)
    assert {
        name: (approved / name).read_bytes()
        for name in sorted(rendered)
    } == rendered
    assert validator.validate_skill_directory(approved).ok
    stripped = generator.strip_author_notes(
        (case_root / descriptor["input_wiki"]).read_text(encoding="utf-8")
    )
    assert "(#" not in stripped and "（#" not in stripped


def test_rpc_verification_extractors_match_real_synthetic_lines_and_window(
    generator: Any,
) -> None:
    spec = generator.load_generation_spec(SPEC_ROOT / "rpc-service-takeover.json")
    contract = spec.verification_contract
    archive_bytes = base64.b64decode(RPC_ARCHIVE_B64.read_text(encoding="ascii"))
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        logs = {
            anchor: "\n".join(
                f"[{ordinal:04d}] [diagnostic|{path}] {line}"
                for ordinal, line in enumerate(
                    archive.read(path).decode("utf-8").splitlines(), start=1
                )
            )
            for anchor, path in {
                "client": "boards/slot_1/debug_20260731.log",
                "server": "boards/slot_2/debug_20260731.log",
            }.items()
        }

    observed: dict[str, datetime] = {}
    for extractor in contract["event_extractors"]:
        assert len(extractor["members"]) == 1
        member = extractor["members"][0]
        assert member["match_mode"] == "FULL_LINE"
        matches = [
            re.fullmatch(member["line_pattern"], line)
            for line in logs[extractor["anchor"]].splitlines()
        ]
        matches = [match for match in matches if match is not None]
        assert len(matches) == 1
        assert set(matches[0].groupdict()) == {
            item["name"] for item in extractor["fields"]
        }
        assert extractor["timestamp_field"] is not None
        observed[extractor["id"]] = datetime.strptime(
            matches[0].group(extractor["timestamp_field"]),
            "%Y-%m-%dT%H:%M:%S.%fZ",
        ).replace(tzinfo=timezone.utc)

    problem_time = datetime(2026, 7, 31, 0, 0, 3, tzinfo=timezone.utc)
    windows = [
        rule["parameters"]
        for rule in contract["rules"]
        if rule["kind"] == "EVENT_TIME_WINDOW"
    ]
    assert len(windows) == len(observed)
    for window in windows:
        assert window["before_ms"] == 3500
        assert window["after_ms"] == 500
        assert window["lower_bound"] == window["upper_bound"] == "INCLUSIVE"
        event_time = observed[window["event"]]
        assert problem_time - timedelta(milliseconds=3500) <= event_time
        assert event_time <= problem_time + timedelta(milliseconds=500)
        wrong_time = datetime(2099, 1, 1, tzinfo=timezone.utc)
        assert not (
            wrong_time - timedelta(milliseconds=3500)
            <= event_time
            <= wrong_time + timedelta(milliseconds=500)
        )


def test_verification_contract_rejects_defaults_legacy_suppression_and_bad_remediation(
    generator: Any,
) -> None:
    value = json.loads((SPEC_ROOT / "rpc-service-takeover.json").read_text("utf-8"))
    time_rule = next(
        item
        for item in value["verification_contract"]["rules"]
        if item["kind"] == "EVENT_TIME_WINDOW"
    )
    del time_rule["parameters"]["upper_bound"]
    with pytest.raises(ValueError, match="fields are invalid"):
        generator.GenerationSpec.from_mapping(value)

    value = json.loads((SPEC_ROOT / "rpc-service-takeover.json").read_text("utf-8"))
    value["verification_contract"]["suppression_seconds"] = 75
    with pytest.raises(ValueError, match="verification_contract fields"):
        generator.GenerationSpec.from_mapping(value)

    value = json.loads((SPEC_ROOT / "manual-triage.json").read_text("utf-8"))
    value["requirements"][0]["supplement_policy"] = "NONE"
    value["verification_contract"]["rules"][0]["remediation_requirements"] = [
        "affected_component"
    ]
    with pytest.raises(ValueError, match="MISSING_ONLY"):
        generator.GenerationSpec.from_mapping(value)


def test_legacy_required_field_is_rejected(generator: Any) -> None:
    value = json.loads((SPEC_ROOT / "manual-triage.json").read_text(encoding="utf-8"))
    value["requirements"][0]["required"] = False
    with pytest.raises(ValueError, match="requirement fields"):
        generator.GenerationSpec.from_mapping(value)


def test_unconfirmed_role_or_requirement_is_rejected(generator: Any) -> None:
    value = json.loads((SPEC_ROOT / "rpc-service-takeover.json").read_text("utf-8"))
    value["roles"][0]["confirmed"] = False
    with pytest.raises(ValueError, match="explicitly confirmed"):
        generator.GenerationSpec.from_mapping(value)

    value = json.loads((SPEC_ROOT / "rpc-service-takeover.json").read_text("utf-8"))
    value["requirements"][0]["confirmed"] = False
    with pytest.raises(ValueError, match="explicitly confirmed"):
        generator.GenerationSpec.from_mapping(value)


def test_profile_reserved_requirement_name_is_rejected(generator: Any) -> None:
    value = json.loads((SPEC_ROOT / "manual-triage.json").read_text("utf-8"))
    reserved = json.loads(json.dumps(value["requirements"][0]))
    reserved["name"] = "problem_time"
    value["requirements"].append(reserved)
    with pytest.raises(ValueError, match="profile-reserved"):
        generator.GenerationSpec.from_mapping(value)


@pytest.mark.parametrize("deployment_scope", [None, "DEVELOPMENT", 1])
def test_deployment_scope_is_required_and_exact(
    generator: Any,
    deployment_scope: Any,
) -> None:
    value = json.loads((SPEC_ROOT / "manual-triage.json").read_text(encoding="utf-8"))
    if deployment_scope is None:
        del value["deployment_scope"]
        expected = "field set"
    else:
        value["deployment_scope"] = deployment_scope
        expected = "deployment_scope"
    with pytest.raises((TypeError, ValueError), match=expected):
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
    attachment = {
        "name": "wiki_archive",
        "kind": "ATTACHMENT",
        "stage": "INITIAL",
        "fulfillment_source": "READY_ATTACHMENT",
        "prompt": "Upload logs.",
        "constraints": {
            "allowed_content_types": ["application/octet-stream"],
            "min_count": 1,
            "max_count": 1,
        },
        "supplement_policy": "MISSING_ONLY",
        "requiredness": "REQUIRED",
        "activation_condition": None,
        "source_reference": "Confirmed Wiki attachment definition.",
        "confirmed": True,
    }
    value["requirements"].append(attachment)
    with pytest.raises(
        ValueError,
        match="platform-injected|at most one ATTACHMENT",
    ):
        generator.GenerationSpec.from_mapping(value)


def test_logparse_archive_content_types_are_injected_without_mutating_author_input(
    generator: Any,
) -> None:
    value = json.loads(
        (SPEC_ROOT / "database-deadlock.json").read_text(encoding="utf-8")
    )
    spec = generator.GenerationSpec.from_mapping(value)
    normalized = next(
        item for item in spec.manifest_requirements() if item["name"] == "log_archive"
    )

    assert all(item["kind"] != "ATTACHMENT" for item in value["requirements"])
    assert normalized["constraints"]["allowed_content_types"] == [
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
            "supplement_policy": "MISSING_ONLY",
            "requiredness": "REQUIRED",
            "activation_condition": None,
            "source_reference": "已确认的 Wiki 人工排查附件定义。",
            "confirmed": True,
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
