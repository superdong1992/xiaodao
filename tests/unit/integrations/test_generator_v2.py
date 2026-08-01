from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
GENERATOR_PATH = (
    REPOSITORY_ROOT
    / ".claude/skills/wiki-to-diagnosis-skill/scripts/generate_diagnosis_skill.py"
)
WIKI_FIXTURE = (
    REPOSITORY_ROOT / "tests/fixtures/components/logparse/wiki/service-takeover.md"
)
FORMAT_FIXTURE = (
    REPOSITORY_ROOT / "tests/fixtures/components/logparse/supported-formats.json"
)
STATIC_PRODUCT = REPOSITORY_ROOT / ".claude/skills/diagnose-service-takeover"
SKILL_ID = "diagnose-service-takeover"
SUMMARY = "定位合成服务接管场景中的 RPC 超时"
PRODUCT = "compact"
CONTENT_TYPES = (
    "application/gzip",
    "application/zip",
    "application/x-tar",
)
ASSUMPTIONS = ("只使用合成服务名、合成订单号和非敏感日志。",)
EXPECTED_PRODUCT_SHA256 = (
    "66ddd0b345df043b99489e26d9c0b7bc9ac9fa4f7ba3322783f956182ed17ba2"
)
EXPECTED_MANIFEST = {
    "schema_version": 1,
    "id": SKILL_ID,
    "version": "2.0.0",
    "capability": "service-takeover",
    "summary": SUMMARY,
    "entry_document": "SKILL.md",
    "tool_bundle_id": "tool-bundle/diagnose",
    "requires_logparse": True,
    "logparse_product": PRODUCT,
}


@pytest.fixture(scope="module")
def generator() -> Any:
    """Load the delivered standalone generator without creating a pyc cache."""

    module_name = "_problem_locator_s07_generate_diagnosis_skill"
    module = types.ModuleType(module_name)
    module.__file__ = str(GENERATOR_PATH)
    module.__package__ = ""
    sys.modules[module_name] = module
    source = GENERATOR_PATH.read_bytes()
    exec(compile(source, str(GENERATOR_PATH), "exec"), module.__dict__)
    return module


def _build_takeover_spec(generator: Any, **updates: Any) -> Any:
    arguments: dict[str, Any] = {
        "capability": "service-takeover",
        "summary": SUMMARY,
        "version": "2.0.0",
        "requires_logparse": True,
        "logparse_product": PRODUCT,
        "allowed_content_types": CONTENT_TYPES,
        "assumptions": ASSUMPTIONS,
    }
    arguments.update(updates)
    return generator.build_spec_from_wiki(
        WIKI_FIXTURE.read_text(encoding="utf-8"),
        **arguments,
    )


def _product_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def test_generator_is_a_standalone_stdlib_v2_module(generator: Any) -> None:
    syntax = ast.parse(GENERATOR_PATH.read_bytes(), filename=str(GENERATOR_PATH))
    imported_roots: set[str] = set()
    for node in ast.walk(syntax):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.partition(".")[0])

    assert imported_roots <= sys.stdlib_module_names | {"__future__"}
    assert generator.GENERATOR_VERSION == "2.0.0"


def test_wiki_fixture_replays_the_static_product_byte_for_byte_and_idempotently(
    generator: Any,
    tmp_path: Path,
) -> None:
    spec = _build_takeover_spec(generator)
    output_root = tmp_path / "generated"

    first = generator.generate_diagnosis_skill(spec, output_root)
    generated_files = _product_files(first.skill_dir)
    static_files = _product_files(STATIC_PRODUCT)

    assert generated_files == static_files
    assert set(generated_files) == {"SKILL.md", "diagnosis-skill.json"}
    assert first.created is True
    assert first.replaced is False
    assert first.product_sha256 == EXPECTED_PRODUCT_SHA256
    assert generator.product_sha256(generated_files) == EXPECTED_PRODUCT_SHA256
    before = {
        relative_path: hashlib.sha256(payload).hexdigest()
        for relative_path, payload in generated_files.items()
    }

    second = generator.generate_diagnosis_skill(spec, output_root)
    assert second.skill_dir == first.skill_dir
    assert second.product_sha256 == first.product_sha256
    assert second.created is False
    assert second.replaced is False
    assert {
        relative_path: hashlib.sha256(payload).hexdigest()
        for relative_path, payload in _product_files(second.skill_dir).items()
    } == before

    crlf_wiki = "\ufeff" + WIKI_FIXTURE.read_text(encoding="utf-8").replace(
        "\n", "\r\n"
    )
    normalized_spec = generator.build_spec_from_wiki(
        crlf_wiki,
        capability="service-takeover",
        summary=SUMMARY,
        version="2.0.0",
        requires_logparse=True,
        logparse_product=PRODUCT,
        allowed_content_types=CONTENT_TYPES,
        assumptions=ASSUMPTIONS,
    )
    assert generator.render_product(normalized_spec) == generated_files


def test_generated_manifest_is_exact_canonical_and_version_2_0_0(
    generator: Any,
) -> None:
    files = generator.render_product(_build_takeover_spec(generator))

    assert files["diagnosis-skill.json"] == _canonical_json_bytes(EXPECTED_MANIFEST)
    assert json.loads(files["diagnosis-skill.json"]) == EXPECTED_MANIFEST
    assert set(EXPECTED_MANIFEST) == {
        "schema_version",
        "id",
        "version",
        "capability",
        "summary",
        "entry_document",
        "tool_bundle_id",
        "requires_logparse",
        "logparse_product",
    }
    assert b"generator version\xef\xbc\x9a`2.0.0`" in files["SKILL.md"]
    positions = [files["SKILL.md"].index(value.encode("ascii")) for value in CONTENT_TYPES]
    assert positions == sorted(positions)


def test_allowed_content_types_preserve_the_fixed_logparse_extension_order() -> None:
    fixture = json.loads(FORMAT_FIXTURE.read_bytes())
    assert fixture["schema_version"] == 1
    assert [
        item["extension"] for item in fixture["extension_mappings"]
    ] == [".gz", ".zip", ".tar.gz", ".tgz", ".tar"]
    generated_order: list[str] = []
    for item in fixture["extension_mappings"]:
        content_type = item["content_type"]
        if content_type not in generated_order:
            generated_order.append(content_type)
    assert tuple(generated_order) == CONTENT_TYPES
    assert tuple(fixture["content_types"]) == CONTENT_TYPES


def test_same_id_and_version_cannot_overwrite_different_product_bytes(
    generator: Any,
    tmp_path: Path,
) -> None:
    original = _build_takeover_spec(generator)
    output_root = tmp_path / "generated"
    generated = generator.generate_diagnosis_skill(original, output_root)
    original_files = _product_files(generated.skill_dir)
    changed = dataclasses.replace(original, summary="语义已变化的摘要")

    with pytest.raises(FileExistsError, match="same diagnosis Skill id/version"):
        generator.generate_diagnosis_skill(changed, output_root)
    with pytest.raises(FileExistsError, match="same diagnosis Skill id/version"):
        generator.generate_diagnosis_skill(
            changed,
            output_root,
            replace_different_version=True,
        )

    assert _product_files(generated.skill_dir) == original_files


def test_explicit_version_increase_is_required_before_product_replacement(
    generator: Any,
    tmp_path: Path,
) -> None:
    original = _build_takeover_spec(generator)
    output_root = tmp_path / "generated"
    first = generator.generate_diagnosis_skill(original, output_root)
    upgraded = dataclasses.replace(original, version="2.0.1", summary="显式升版后的摘要")

    with pytest.raises(FileExistsError, match="different product version"):
        generator.generate_diagnosis_skill(upgraded, output_root)
    assert json.loads(
        (first.skill_dir / "diagnosis-skill.json").read_bytes()
    )["version"] == "2.0.0"

    replacement = generator.generate_diagnosis_skill(
        upgraded,
        output_root,
        replace_different_version=True,
    )
    assert replacement.created is False
    assert replacement.replaced is True
    assert replacement.product_sha256 != first.product_sha256
    assert json.loads(
        (replacement.skill_dir / "diagnosis-skill.json").read_bytes()
    )["version"] == "2.0.1"


@pytest.mark.parametrize(
    "content_types",
    [
        ("Application/zip",),
        ("application/ZIP",),
        ("application/zip;charset=utf-8",),
        (" application/zip",),
        ("application /zip",),
        ("application/zi\tp",),
        ("application/zip\r\napplication/gzip",),
        ("application/\u538b\u7f29",),
        ("a/" + "b" * 126,),
        ("application/zip", "application/zip"),
    ],
    ids=[
        "uppercase-type",
        "uppercase-subtype",
        "parameter",
        "outer-whitespace",
        "inner-whitespace",
        "control-character",
        "crlf",
        "non-ascii",
        "overlong",
        "duplicate",
    ],
)
def test_noncanonical_or_duplicate_content_types_are_rejected(
    generator: Any,
    content_types: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        _build_takeover_spec(generator, allowed_content_types=content_types)


def test_no_logparse_branch_has_an_exact_null_product_and_no_broker_workflow(
    generator: Any,
    tmp_path: Path,
) -> None:
    spec = _build_takeover_spec(
        generator,
        requires_logparse=False,
        logparse_product=None,
        allowed_content_types=(),
    )
    result = generator.generate_diagnosis_skill(spec, tmp_path / "generated")
    files = _product_files(result.skill_dir)
    manifest = dict(EXPECTED_MANIFEST)
    manifest["requires_logparse"] = False
    manifest["logparse_product"] = None

    assert files["diagnosis-skill.json"] == _canonical_json_bytes(manifest)
    skill = files["SKILL.md"].decode("utf-8")
    assert "此产品 `requires_logparse=false`" in skill
    assert "本产品 `requires_logparse=false`" in skill
    assert "## 先调用 logparse-diagnose Skill" not in skill
    assert "## LOGPARSE_RUN 复用" not in skill

    with pytest.raises(ValueError):
        _build_takeover_spec(
            generator,
            requires_logparse=False,
            logparse_product=PRODUCT,
            allowed_content_types=(),
        )
    with pytest.raises(ValueError):
        _build_takeover_spec(
            generator,
            requires_logparse=False,
            logparse_product=None,
            allowed_content_types=("application/zip",),
        )
