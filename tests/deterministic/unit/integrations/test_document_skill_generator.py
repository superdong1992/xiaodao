"""Generated v3 packages and the standalone validator agree with Server admission."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from problem_locator.runtime.document_skill import (
    document_source_sha256,
    load_document_skill_registration,
)
from tests.document_skill_helpers import (
    document_input,
    document_registration,
    write_document_registration,
)


ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".claude/skills/wiki-to-logparse-diagnosis-skill"
VALIDATOR = SKILL / "scripts/validate_generated_skill.py"
spec = importlib.util.spec_from_file_location("_document_generator_validator", VALIDATOR)
assert spec is not None and spec.loader is not None
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "wiki.md"
    source.write_bytes(b"authored wiki\n")
    return source


def _rewrite(root: Path, value: dict) -> None:
    (root / "registration-template.json").write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("mode", ["none", "direct_files", "logparse"])
def test_generated_document_modes_pass_standalone_and_server(tmp_path, mode):
    source = _source(tmp_path)
    root = write_document_registration(tmp_path / "store", mode=mode,
                                       skill_name="diagnose-service")

    result = validator.validate_document(root, source)
    loaded = load_document_skill_registration(root)

    assert result["ok"], result["errors"]
    assert result["preparation_mode"] == loaded.registration.preprocessing.mode == mode
    assert loaded.registration.skill_name == "diagnose-service"
    assert not (loaded.package_root / "methods.json").exists()
    if mode != "logparse":
        assert loaded.registration.preprocessing.logparse_plan is None
        assert not loaded.registration.inputs.user


def test_non_logparse_inputs_are_not_replaced_by_rpc_anchor_fields(tmp_path):
    source = _source(tmp_path)
    user = [document_input("request_key"), document_input("tenant", required=False)]
    value = document_registration(mode="direct_files", user_inputs=user,
                                  attachment_required=False)
    value["inputs"]["fixed"] = [{"name": "time_unit", "value": "ms"}]
    value["inputs"]["derived"] = [{"name": "elapsed", "description": "从日志读取的耗时。"}]
    root = write_document_registration(tmp_path / "store", registration=value)

    result = validator.validate_document(root, source)
    loaded = load_document_skill_registration(root)

    assert result["ok"], result["errors"]
    assert [item.name for item in loaded.registration.inputs.user] == ["request_key", "tenant"]
    assert loaded.registration.inputs.user[1].required is False
    assert loaded.registration.attachment.required is False


def test_logparse_single_required_and_optional_roles_need_no_client_server_template(tmp_path):
    source = _source(tmp_path)
    value = document_registration(mode="logparse")
    value["inputs"]["user"].append(document_input("peer_process", required=False))
    preparation = value["runtime"]["preprocessing"]
    preparation["roles"].append({"label": "peer", "description": "可选对端。",
                                  "presence": "OPTIONAL", "source_reference": "作者补充说明。"})
    preparation["logparse_plan"]["anchors"].append({
        "label": "peer", "module": {"source": "SKILL_FIXED", "value": "peer"},
        "slot": {"source": "SKILL_FIXED", "value": "0"},
        "process_name": {"source": "USER_FACT", "name": "peer_process"}, "pid": None,
    })
    root = write_document_registration(tmp_path / "store", registration=value)

    assert validator.validate_document(root, source)["ok"]
    loaded = load_document_skill_registration(root)
    assert [item["label"] for item in loaded.registration.preprocessing.roles] == ["service", "peer"]


INVALID_DECLARATIONS = [
    ("version-type", lambda v: v.update(schema_version=True)),
    ("legacy-version", lambda v: v.update(schema_version=2)),
    ("semver-leading-zero", lambda v: v.update(version="01.0.0")),
    ("extra-field", lambda v: v.update(custom={})),
    ("empty-routing", lambda v: v["routing"].update(applicability=[])),
    ("duplicate-routing", lambda v: v["routing"]["exclusions"].append(deepcopy(v["routing"]["applicability"][0]))),
    ("wrong-package-format", lambda v: v["package"].update(format="methods-v1")),
    ("package-traversal", lambda v: v["package"].update(relative_path="../outside")),
    ("unknown-mode", lambda v: v["runtime"]["preprocessing"].update(mode="auto")),
    ("none-with-attachment", lambda v: v["runtime"]["preprocessing"].update(mode="none")),
    ("missing-attachment", lambda v: v.update(attachment=None)),
    ("unsupported-attachment", lambda v: v["attachment"].update(allowed_content_types=["application/pdf"])),
    ("attachment-boolean-count", lambda v: v["attachment"].update(min_count=True)),
    ("attachment-count-overflow", lambda v: v["attachment"].update(max_count=21)),
    ("attachment-collision", lambda v: v["attachment"].update(name="request_key")),
    ("non-boolean-required", lambda v: v["inputs"]["user"][0].update(required="true")),
    ("duplicate-input", lambda v: v["inputs"]["fixed"].append({"name": "request_key", "value": "fixed"})),
    ("derived-collision", lambda v: v["inputs"]["derived"].append({"name": "request_key", "description": "派生"})),
    ("non-string-input", lambda v: v["inputs"]["user"][0]["constraints"].update(value_type="OBJECT")),
    ("invalid-regex", lambda v: v["inputs"]["user"][0]["constraints"].update(pattern="[")),
    ("oversized-regex", lambda v: v["inputs"]["user"][0]["constraints"].update(pattern="a" * 4097)),
    ("invalid-enum", lambda v: v["inputs"]["user"][0]["constraints"].update(pattern="[0-9]+", allowed_values=["word"])),
    ("duplicate-enum", lambda v: v["inputs"]["user"][0]["constraints"].update(allowed_values=["x", "x"])),
    ("stale-logparse-binding", lambda v: v["runtime"]["preprocessing"].update(logparse_product="default")),
]


@pytest.mark.parametrize("name,mutate", INVALID_DECLARATIONS, ids=[item[0] for item in INVALID_DECLARATIONS])
def test_standalone_and_server_reject_the_same_invalid_declarations(tmp_path, name, mutate):
    source = _source(tmp_path)
    value = document_registration(mode="direct_files", user_inputs=[document_input("request_key")])
    root = write_document_registration(tmp_path / "store", registration=value)
    mutate(value)
    _rewrite(root, value)

    result = validator.validate_document(root, source)

    assert result["ok"] is False, name
    with pytest.raises(ValueError):
        load_document_skill_registration(root)


@pytest.mark.parametrize("invalid", ["optional-required-anchor", "unknown-user-fact", "different-role-order", "nonarchive-logparse"])
def test_logparse_binding_errors_are_rejected_by_both_validators(tmp_path, invalid):
    source = _source(tmp_path)
    value = document_registration(mode="logparse")
    plan = value["runtime"]["preprocessing"]["logparse_plan"]
    if invalid == "optional-required-anchor":
        value["inputs"]["user"][1]["required"] = False
    elif invalid == "unknown-user-fact":
        plan["anchors"][0]["process_name"]["name"] = "missing"
    elif invalid == "different-role-order":
        plan["anchors"][0]["label"] = "unknown"
    else:
        value["attachment"]["allowed_content_types"] = ["text/plain"]
    root = write_document_registration(tmp_path / "store", registration=value)

    assert validator.validate_document(root, source)["ok"] is False
    with pytest.raises(ValueError):
        load_document_skill_registration(root)


@pytest.mark.parametrize("artifact", ["methods.json", "scripts/parse.py", "references/nested/extra.md"])
def test_document_target_rejects_methods_scripts_and_nested_references(tmp_path, artifact):
    source = _source(tmp_path)
    root = write_document_registration(tmp_path / "store")
    target = root / "package/document-triage" / artifact
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("unsupported\n", encoding="utf-8")

    assert validator.validate_document(root, source)["ok"] is False
    with pytest.raises(ValueError):
        load_document_skill_registration(root)


@pytest.mark.parametrize("reference", ["[丢失](references/missing.md)", "[越界](../../private.md)",
                                        "[编码越界](%2e%2e/private.md)", "`references/missing.md`", "[本地](file:///tmp/data.md)"])
def test_generated_package_references_must_resolve_inside_package(tmp_path, reference):
    source = _source(tmp_path)
    root = write_document_registration(tmp_path / "store")
    skill_path = root / "package/document-triage/SKILL.md"
    skill_path.write_text(skill_path.read_text(encoding="utf-8") + "\n" + reference, encoding="utf-8")

    assert validator.validate_document(root, source)["ok"] is False
    with pytest.raises(ValueError):
        load_document_skill_registration(root)


@pytest.mark.parametrize("nested", [False, True])
def test_existing_skill_source_keeps_original_bytes_and_directory_identity(tmp_path, nested):
    source = tmp_path / "uploaded-copy"
    source.mkdir()
    original = "# 作者的定位步骤\r\n\r\n核对目标请求。\r\n"
    if nested:
        (source / "references/latency").mkdir(parents=True)
        original += "[定位方法](references/latency/rules.md)\r\n"
        (source / "references/latency/rules.md").write_bytes("# 方法\r\n保留原始阈值。\r\n".encode("utf-8"))
    (source / "SKILL.md").write_bytes(original.encode("utf-8"))
    identity = validator.document_source_identity(source)
    value = document_registration()
    value["source"] = identity
    root = write_document_registration(tmp_path / "store", registration=value)

    assert identity == {"kind": "SKILL", "sha256": document_source_sha256(source, "SKILL")}
    assert validator.validate_document(root, source)["ok"]
    assert load_document_skill_registration(root).registration.source.sha256 == identity["sha256"]
    assert (source / "SKILL.md").read_bytes() == original.encode("utf-8")


@pytest.mark.parametrize("kind", ["script", "tool-permission", "dangling-reference"])
def test_existing_skill_source_rejects_unsupported_dependencies(tmp_path, kind):
    source = tmp_path / "original"
    source.mkdir()
    body = "# 定位步骤\n读取作者提供的材料。\n"
    if kind == "script":
        (source / "scripts").mkdir()
        (source / "scripts/parse.py").write_text("print('data')\n", encoding="utf-8")
    elif kind == "tool-permission":
        body = "---\nallowed-tools: Bash\n---\n" + body
    else:
        body += "\n[外部资料](../missing.md)\n"
    (source / "SKILL.md").write_text(body, encoding="utf-8")

    with pytest.raises(ValueError):
        validator.document_source_identity(source)
    with pytest.raises(ValueError):
        document_source_sha256(source, "SKILL")


def test_source_hash_mismatch_cannot_be_repaired_by_validator(tmp_path):
    source = _source(tmp_path)
    root = write_document_registration(tmp_path / "store")
    before = (root / "registration-template.json").read_bytes()
    source.write_bytes(b"changed authored wiki\n")

    result = validator.validate_document(root, source)

    assert result["ok"] is False
    assert "SHA-256" in "\n".join(result["errors"])
    assert (root / "registration-template.json").read_bytes() == before


def test_copied_generator_runs_without_installed_python_packages(tmp_path):
    source = _source(tmp_path)
    root = write_document_registration(tmp_path / "store", mode="direct_files")
    scripts = tmp_path / "copied-skill/scripts"
    shutil.copytree(SKILL / "scripts", scripts, ignore=shutil.ignore_patterns("__pycache__"))
    command = [sys.executable, "-I", "-S", "-B", str(scripts / "validate_generated_skill.py")]

    identity_result = subprocess.run(command + ["--source", str(source), "--print-source-identity", "--json"],
                                     capture_output=True, text=True, encoding="utf-8", check=False)
    assert identity_result.returncode == 0, identity_result.stderr
    assert json.loads(identity_result.stdout)["source"] == {
        "kind": "WIKI", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    result = subprocess.run(command + ["--registration-dir", str(root), "--source", str(source), "--json"],
                            capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["preparation_mode"] == "direct_files"
