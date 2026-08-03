from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import os
import stat
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
    "4ce37124b5fb97233188150e074e3b71d995e27bd3941a51a05aa1d5cd2251e7"
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


def _assert_posix_publish_modes(root: Path) -> None:
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    for name in ("SKILL.md", "diagnosis-skill.json"):
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o644


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
    skill_markdown = generated_files["SKILL.md"].decode("utf-8")
    assert "slot=`1`、process_name=`checkout-client`、pid=`101`" in skill_markdown
    assert "slot=`2`、process_name=`inventory-server`、pid=`202`" in skill_markdown
    assert "即使 `slot` 或 `pid` 只含数字" in skill_markdown
    assert "也不得写成 JSON number" in skill_markdown
    assert "在写 request 与执行该命令之间不得继续分析" in skill_markdown
    assert (
        '`content_type="application/vnd.problem-locator.logparse-run+directory"`'
        in skill_markdown
    )
    assert "不得使用 `application/octet-stream`" in skill_markdown
    assert "目录 hash 不是 `parse_manifest.json` 文件的 hash" in skill_markdown
    assert "`workspace_relative_path=null`、`declared_size=null`" in skill_markdown
    assert "也不得据此满足参数 B" in skill_markdown
    assert 'proposal key=`logparse-client-evidence`' in skill_markdown
    assert '"evidence_proposal_key":"logparse-client-evidence"' in skill_markdown
    assert "不得只把它们放在 proposal arrays 中" in skill_markdown
    assert "复用分支不得再次提出 `LOGPARSE_RUN` 或 client Evidence" in skill_markdown
    assert 'proposal key=`logparse-server-evidence`' in skill_markdown
    assert "existing client Evidence ID 在前" in skill_markdown
    assert "禁止用 `caller_service` 或 `server_service` 替代" in skill_markdown
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


@pytest.mark.skipif(os.name != "posix", reason="POSIX publish modes only")
def test_generator_publishes_stable_posix_modes_under_restrictive_umask(
    generator: Any,
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "generated"
    original = _build_takeover_spec(generator)
    upgraded = dataclasses.replace(original, version="2.0.1")
    previous_umask = os.umask(0o077)
    try:
        first = generator.generate_diagnosis_skill(original, output_root)
        _assert_posix_publish_modes(first.skill_dir)
        replacement = generator.generate_diagnosis_skill(
            upgraded,
            output_root,
            replace_different_version=True,
        )
        _assert_posix_publish_modes(replacement.skill_dir)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(output_root.stat().st_mode) == 0o755
    assert first.skill_dir == replacement.skill_dir
    replacement_files = _product_files(replacement.skill_dir)
    assert replacement_files == generator.render_product(upgraded)
    assert generator.product_sha256(replacement_files) == replacement.product_sha256


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
