from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from problem_locator.contracts import canonical_json_bytes, is_canonical_json_bytes


ROOT = Path(__file__).resolve().parents[4]
ADAPTER_ROOT = ROOT / ".claude" / "skills" / "adapt-lan-generic-locator-v2"
ADAPTER_SCRIPT = ADAPTER_ROOT / "scripts" / "verify_generic_locator_v2.py"
FIXTURE_ROOT = (
    ROOT
    / "tests"
    / "fixtures"
    / "components"
    / "generic-problem-locator-dual-mode"
)
FIXTURE_DRIVER = FIXTURE_ROOT / "scripts" / "run_fixture_modes.py"
ORACLE = FIXTURE_ROOT / "references" / "native-report.md"
V1_ORACLE = FIXTURE_ROOT / "references" / "v1-result.txt"
PRIVATE_TOKEN = "LAN_FIXTURE_PRIVATE_7f91c4"
FIXTURE_VERSION = "fixture-v2.0.0"
PROBLEM_BYTES = (
    "订单支付成功后页面仍显示“处理中”。\n"
    "request-id: 订单-α-42\n"
    "已确认：刷新三次仍复现"
).encode("utf-8")
V2_RESOLVED = b"<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\n"
IDENTITY_MANIFEST = {
    "schema_version": 1,
    "manifest_kind": "problem-locator-generic-lan-run-identity-v1",
    "service_account_sha256": "1" * 64,
    "agent_executable_sha256": "2" * 64,
    "agent_version_sha256": "3" * 64,
    "settings_sha256": "4" * 64,
    "model_identity_sha256": "5" * 64,
    "tool_inventory_sha256": "6" * 64,
}


def _run(script: Path, *arguments: object) -> subprocess.CompletedProcess[bytes]:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, os.fspath(script), *(os.fspath(item) for item in arguments)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        timeout=15,
    )


def _run_adapter(*arguments: object) -> subprocess.CompletedProcess[bytes]:
    return _run(ADAPTER_SCRIPT, *arguments)


def _run_fixture(*arguments: object) -> subprocess.CompletedProcess[bytes]:
    return _run(FIXTURE_DRIVER, *arguments)


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n")
    header, _ = text[4:].split("\n---\n", 1)
    return {
        key.strip(): value.strip()
        for key, value in (line.split(":", 1) for line in header.splitlines())
    }


def _execute_fixture_modes(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path]:
    problem_input = tmp_path / "problem.txt"
    direct = tmp_path / "direct-response.md"
    v1 = tmp_path / "generic_diagnosis_result.txt"
    v2 = tmp_path / "generic_diagnosis_result.md"
    problem_input.write_bytes(PROBLEM_BYTES)
    for mode, output in (
        ("DIRECT_MODE", direct),
        ("FRAMEWORK_V1", v1),
        ("FRAMEWORK_V2", v2),
    ):
        result = _run_fixture(
            "--mode",
            mode,
            "--problem-input",
            problem_input.resolve(),
            "--output",
            output.resolve(),
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        summary = json.loads(result.stdout)
        assert summary["status"] == "RESOLVED"
        assert summary["mode"] == mode
        assert summary["content_included"] is False
        assert summary["output_utf8_size"] == output.stat().st_size
        assert summary["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
        assert PRIVATE_TOKEN.encode("utf-8") not in result.stdout
        assert os.fspath(tmp_path).encode("utf-8") not in result.stdout
    return problem_input, direct, v1, v2


def _write_identity_pair(
    tmp_path: Path,
    *,
    framework_manifest: dict[str, object] | None = None,
) -> tuple[Path, Path, bytes]:
    direct = tmp_path / "direct-identity.json"
    framework = tmp_path / "framework-identity.json"
    direct_bytes = canonical_json_bytes(IDENTITY_MANIFEST)
    direct.write_bytes(direct_bytes)
    framework.write_bytes(
        canonical_json_bytes(
            IDENTITY_MANIFEST
            if framework_manifest is None
            else framework_manifest
        )
    )
    return direct, framework, direct_bytes


def _ab_arguments(
    *,
    problem_input: Path,
    direct: Path,
    v2: Path,
    direct_identity: Path,
    framework_identity: Path,
    semantic_verdict: str,
    receipt: Path,
    direct_status: str = "RESOLVED",
) -> tuple[object, ...]:
    return (
        "ab-receipt",
        "--skill-root",
        FIXTURE_ROOT.resolve(),
        "--skill-version",
        FIXTURE_VERSION,
        "--problem-input",
        problem_input.resolve(),
        "--direct-report",
        direct.resolve(),
        "--direct-status",
        direct_status,
        "--direct-identity-manifest",
        direct_identity.resolve(),
        "--framework-result",
        v2.resolve(),
        "--framework-identity-manifest",
        framework_identity.resolve(),
        "--semantic-verdict",
        semantic_verdict,
        "--receipt",
        receipt.resolve(),
    )


def test_adapter_skill_is_minimal_and_never_scores_random_report_text() -> None:
    files = sorted(
        path.relative_to(ADAPTER_ROOT).as_posix()
        for path in ADAPTER_ROOT.rglob("*")
        if path.is_file()
    )
    assert files == [
        "SKILL.md",
        "references/framework-mode.md",
        "scripts/verify_generic_locator_v2.py",
    ]
    skill_text = (ADAPTER_ROOT / "SKILL.md").read_text(encoding="utf-8")
    normalized_skill_text = " ".join(skill_text.split())
    script_text = ADAPTER_SCRIPT.read_text(encoding="utf-8")
    fields = _frontmatter(skill_text)
    assert fields["name"] == ADAPTER_ROOT.name
    assert "private LAN generic-diagnosis Skill" in fields["description"]
    assert "references/framework-mode.md" in skill_text
    assert "scripts/verify_generic_locator_v2.py" in skill_text
    assert "not expected to be byte-identical" in normalized_skill_text
    assert "never infers semantic equivalence" in normalized_skill_text
    assert "MARKDOWN_HEADINGS" not in script_text
    assert "heading_sha256" not in script_text
    assert "--comparison" not in script_text
    assert not (ADAPTER_ROOT / "README.md").exists()


def test_dual_mode_fixture_is_rich_executable_and_manifest_bound() -> None:
    skill_text = (FIXTURE_ROOT / "SKILL.md").read_text(encoding="utf-8")
    fields = _frontmatter(skill_text)
    assert fields["name"] == FIXTURE_ROOT.name
    assert skill_text.count("problem-locator-generic-v2-adapter:start") == 1
    assert skill_text.count("problem-locator-generic-v2-adapter:end") == 1
    for token in (
        "DIRECT_MODE",
        "FRAMEWORK_V1",
        "FRAMEWORK_V2",
        "AMBIGUOUS_FRAMEWORK_OUTPUT",
        "output/generic_diagnosis_result.txt",
        "output/generic_diagnosis_result.md",
        "scripts/run_fixture_modes.py",
    ):
        assert token in skill_text

    oracle = ORACLE.read_text(encoding="utf-8")
    assert oracle.startswith("# 通用定位报告")
    assert "| 检查项 | 观测 | 判断 |" in oracle
    assert "```text" in oracle
    assert "订单-α-42" in oracle
    assert oracle.endswith("\n")
    assert PRIVATE_TOKEN not in oracle

    v1 = V1_ORACLE.read_text(encoding="utf-8")
    assert v1.startswith("<<<GENERIC_DIAGNOSIS_RESULT_V1>>>\n")
    assert v1.endswith("<<<END_GENERIC_DIAGNOSIS_RESULT_V1>>>\n")
    assert "STATUS: RESOLVED" in v1
    assert "```" not in v1
    assert PRIVATE_TOKEN not in v1

    manifest_path = FIXTURE_ROOT / "fixture-manifest.json"
    raw_manifest = manifest_path.read_bytes()
    manifest = json.loads(raw_manifest)
    assert is_canonical_json_bytes(raw_manifest)
    assert manifest["owner_spec"] == "S08"
    assert manifest["root"] == (
        "tests/fixtures/components/generic-problem-locator-dual-mode"
    )
    actual = {
        path.relative_to(FIXTURE_ROOT).as_posix(): path
        for path in FIXTURE_ROOT.rglob("*")
        if path.is_file() and path != manifest_path
    }
    assert [item["path"] for item in manifest["files"]] == sorted(actual)
    for item in manifest["files"]:
        payload = actual[item["path"]].read_bytes()
        assert item["size"] == len(payload)
        assert item["sha256"] == hashlib.sha256(payload).hexdigest()


def test_validator_receipt_has_only_tree_digest_and_declared_version(
    tmp_path: Path,
) -> None:
    first = _run_adapter(
        "validate-skill",
        "--skill-root",
        FIXTURE_ROOT.resolve(),
        "--skill-version",
        FIXTURE_VERSION,
    )
    assert first.returncode == 0, first.stderr.decode("utf-8", "replace")
    first_receipt = json.loads(first.stdout)
    assert first_receipt == {
        "schema_version": 2,
        "receipt_kind": "problem-locator-generic-skill-validation-v2",
        "status": "PASS",
        "skill": {
            "tree_sha256": first_receipt["skill"]["tree_sha256"],
            "version": FIXTURE_VERSION,
        },
        "content_included": False,
    }
    assert len(first_receipt["skill"]["tree_sha256"]) == 64
    assert b"name" not in first.stdout
    assert b"file_count" not in first.stdout
    assert PRIVATE_TOKEN.encode("utf-8") not in first.stdout
    assert os.fspath(FIXTURE_ROOT).encode("utf-8") not in first.stdout

    copied = tmp_path / FIXTURE_ROOT.name
    shutil.copytree(FIXTURE_ROOT, copied)
    copied_skill = copied / "SKILL.md"
    copied_skill.write_bytes(copied_skill.read_bytes() + b"\n")
    second = _run_adapter(
        "validate-skill",
        "--skill-root",
        copied.resolve(),
        "--skill-version",
        FIXTURE_VERSION,
    )
    assert second.returncode == 0, second.stderr.decode("utf-8", "replace")
    second_receipt = json.loads(second.stdout)
    assert (
        second_receipt["skill"]["tree_sha256"]
        != first_receipt["skill"]["tree_sha256"]
    )


def test_validator_rejects_an_adapter_that_drops_v1_compatibility(
    tmp_path: Path,
) -> None:
    copied = tmp_path / FIXTURE_ROOT.name
    shutil.copytree(FIXTURE_ROOT, copied)
    skill_path = copied / "SKILL.md"
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8").replace(
            "<<<END_GENERIC_DIAGNOSIS_RESULT_V1>>>",
            "V1-END-REMOVED",
        ),
        encoding="utf-8",
        newline="\n",
    )

    result = _run_adapter(
        "validate-skill",
        "--skill-root",
        copied.resolve(),
        "--skill-version",
        FIXTURE_VERSION,
    )

    assert result.returncode == 2
    assert json.loads(result.stderr) == {
        "code": "ADAPTER_BLOCK_INCOMPLETE",
        "content_included": False,
        "schema_version": 1,
        "status": "ERROR",
    }
    assert PRIVATE_TOKEN.encode("utf-8") not in result.stderr


def test_fixture_driver_executes_direct_v1_v2_from_the_frozen_oracles(
    tmp_path: Path,
) -> None:
    _, direct, v1, v2 = _execute_fixture_modes(tmp_path)

    oracle = ORACLE.read_bytes()
    assert direct.read_bytes() == oracle
    assert v1.read_bytes() == V1_ORACLE.read_bytes()
    assert v2.read_bytes() == V2_RESOLVED + oracle


@pytest.mark.parametrize(
    ("semantic_verdict", "expected_status", "expected_exit"),
    [
        ("equivalent", "PASS", 0),
        ("different", "FAIL", 1),
        ("not-reviewed", "REVIEW_REQUIRED", 3),
    ],
)
def test_ab_receipt_status_comes_only_from_the_explicit_human_verdict(
    tmp_path: Path,
    semantic_verdict: str,
    expected_status: str,
    expected_exit: int,
) -> None:
    problem_input, direct, _, v2 = _execute_fixture_modes(tmp_path)
    direct_identity, framework_identity, identity_bytes = _write_identity_pair(tmp_path)
    receipt_path = tmp_path / "receipt.json"

    result = _run_adapter(
        *_ab_arguments(
            problem_input=problem_input,
            direct=direct,
            v2=v2,
            direct_identity=direct_identity,
            framework_identity=framework_identity,
            semantic_verdict=semantic_verdict,
            receipt=receipt_path,
        )
    )

    assert result.returncode == expected_exit, result.stderr.decode("utf-8", "replace")
    raw_receipt = receipt_path.read_bytes()
    receipt = json.loads(raw_receipt)
    oracle = ORACLE.read_bytes()
    oracle_sha = hashlib.sha256(oracle).hexdigest()
    identity_sha = hashlib.sha256(identity_bytes).hexdigest()
    assert is_canonical_json_bytes(raw_receipt)
    assert receipt == {
        "schema_version": 2,
        "receipt_kind": "problem-locator-generic-lan-ab-v2",
        "status": expected_status,
        "semantic_verdict": semantic_verdict,
        "skill": {
            "tree_sha256": receipt["skill"]["tree_sha256"],
            "version": FIXTURE_VERSION,
        },
        "problem_input": {
            "sha256": hashlib.sha256(PROBLEM_BYTES).hexdigest(),
            "utf8_size": len(PROBLEM_BYTES),
        },
        "direct_report": {
            "result_status": "RESOLVED",
            "sha256": oracle_sha,
            "utf8_size": len(oracle),
        },
        "framework_report": {
            "result_status": "RESOLVED",
            "sha256": oracle_sha,
            "utf8_size": len(oracle),
        },
        "run_identity": {
            "direct_manifest_sha256": identity_sha,
            "framework_manifest_sha256": identity_sha,
        },
        "content_included": False,
    }
    assert len(receipt["skill"]["tree_sha256"]) == 64
    for forbidden in (
        PRIVATE_TOKEN.encode("utf-8"),
        "订单支付成功".encode("utf-8"),
        "通用定位报告".encode("utf-8"),
        os.fspath(tmp_path).encode("utf-8"),
        FIXTURE_ROOT.name.encode("utf-8"),
        b"file_count",
        b"heading",
        b"agent_executable_sha256",
        b"service_account_sha256",
    ):
        assert forbidden not in raw_receipt


def test_ab_receipt_fails_closed_when_run_identity_manifests_differ(
    tmp_path: Path,
) -> None:
    problem_input, direct, _, v2 = _execute_fixture_modes(tmp_path)
    changed = dict(IDENTITY_MANIFEST)
    changed["tool_inventory_sha256"] = "7" * 64
    direct_identity, framework_identity, _ = _write_identity_pair(
        tmp_path,
        framework_manifest=changed,
    )
    receipt_path = tmp_path / "receipt.json"

    result = _run_adapter(
        *_ab_arguments(
            problem_input=problem_input,
            direct=direct,
            v2=v2,
            direct_identity=direct_identity,
            framework_identity=framework_identity,
            semantic_verdict="equivalent",
            receipt=receipt_path,
        )
    )

    assert result.returncode == 2
    assert not receipt_path.exists()
    assert json.loads(result.stderr) == {
        "schema_version": 1,
        "status": "ERROR",
        "code": "RUN_IDENTITY_MISMATCH",
        "content_included": False,
    }
    assert os.fspath(tmp_path).encode("utf-8") not in result.stderr


def test_ab_receipt_requires_two_distinct_identity_manifest_files(
    tmp_path: Path,
) -> None:
    problem_input, direct, _, v2 = _execute_fixture_modes(tmp_path)
    direct_identity, _, _ = _write_identity_pair(tmp_path)
    receipt_path = tmp_path / "receipt.json"

    result = _run_adapter(
        *_ab_arguments(
            problem_input=problem_input,
            direct=direct,
            v2=v2,
            direct_identity=direct_identity,
            framework_identity=direct_identity,
            semantic_verdict="not-reviewed",
            receipt=receipt_path,
        )
    )

    assert result.returncode == 2
    assert not receipt_path.exists()
    assert json.loads(result.stderr)["code"] == (
        "RUN_IDENTITY_MANIFEST_PATHS_NOT_DISTINCT"
    )


def test_equivalent_verdict_rejects_conflicting_declared_result_statuses(
    tmp_path: Path,
) -> None:
    problem_input, direct, _, v2 = _execute_fixture_modes(tmp_path)
    direct_identity, framework_identity, _ = _write_identity_pair(tmp_path)
    receipt_path = tmp_path / "receipt.json"

    result = _run_adapter(
        *_ab_arguments(
            problem_input=problem_input,
            direct=direct,
            v2=v2,
            direct_identity=direct_identity,
            framework_identity=framework_identity,
            semantic_verdict="equivalent",
            receipt=receipt_path,
            direct_status="UNRESOLVED",
        )
    )

    assert result.returncode == 2
    assert not receipt_path.exists()
    assert json.loads(result.stderr)["code"] == (
        "SEMANTIC_VERDICT_STATUS_CONFLICT"
    )


def test_ab_receipt_rejects_noncanonical_identity_manifest_without_receipt(
    tmp_path: Path,
) -> None:
    problem_input, direct, _, v2 = _execute_fixture_modes(tmp_path)
    direct_identity, framework_identity, _ = _write_identity_pair(tmp_path)
    direct_identity.write_text(
        json.dumps(IDENTITY_MANIFEST, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    receipt_path = tmp_path / "receipt.json"

    result = _run_adapter(
        *_ab_arguments(
            problem_input=problem_input,
            direct=direct,
            v2=v2,
            direct_identity=direct_identity,
            framework_identity=framework_identity,
            semantic_verdict="not-reviewed",
            receipt=receipt_path,
        )
    )

    assert result.returncode == 2
    assert not receipt_path.exists()
    assert json.loads(result.stderr)["code"] == (
        "DIRECT_IDENTITY_MANIFEST_INVALID_CANONICAL_JSON_REQUIRED"
    )
    assert os.fspath(tmp_path).encode("utf-8") not in result.stderr


def test_ab_receipt_rejects_canonical_identity_manifest_with_extra_field(
    tmp_path: Path,
) -> None:
    problem_input, direct, _, v2 = _execute_fixture_modes(tmp_path)
    direct_identity, framework_identity, _ = _write_identity_pair(tmp_path)
    extra = dict(IDENTITY_MANIFEST)
    extra["host_path"] = "must-not-enter-the-contract"
    direct_identity.write_bytes(canonical_json_bytes(extra))
    receipt_path = tmp_path / "receipt.json"

    result = _run_adapter(
        *_ab_arguments(
            problem_input=problem_input,
            direct=direct,
            v2=v2,
            direct_identity=direct_identity,
            framework_identity=framework_identity,
            semantic_verdict="not-reviewed",
            receipt=receipt_path,
        )
    )

    assert result.returncode == 2
    assert not receipt_path.exists()
    assert json.loads(result.stderr)["code"] == (
        "DIRECT_IDENTITY_MANIFEST_INVALID_FIELDS_INVALID"
    )
    assert b"host_path" not in result.stderr


@pytest.mark.parametrize(
    "framework_bytes",
    [
        b"<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\r\n# report\n",
        V2_RESOLVED,
        V2_RESOLVED + b"\xff",
        V2_RESOLVED + b" \t\n",
        V2_RESOLVED + b"\xef\xbb\xbf# report\n",
        V2_RESOLVED + b"x" * 65_537,
        b"<<<GENERIC_DIAGNOSIS_RESULT_V1>>>\nSTATUS: RESOLVED\n",
    ],
    ids=(
        "crlf-marker",
        "empty-body",
        "invalid-utf8",
        "whitespace-body",
        "bom-body",
        "oversize-body",
        "v1-is-not-v2",
    ),
)
def test_ab_entry_rejects_invalid_framework_result_without_content_or_receipt(
    tmp_path: Path,
    framework_bytes: bytes,
) -> None:
    problem_input = tmp_path / "problem.txt"
    direct = tmp_path / "direct.md"
    framework = tmp_path / "framework.md"
    receipt_path = tmp_path / "receipt.json"
    problem_input.write_bytes(PROBLEM_BYTES)
    direct.write_text("# report\n", encoding="utf-8", newline="\n")
    framework.write_bytes(framework_bytes)
    direct_identity, framework_identity, _ = _write_identity_pair(tmp_path)

    result = _run_adapter(
        *_ab_arguments(
            problem_input=problem_input,
            direct=direct,
            v2=framework,
            direct_identity=direct_identity,
            framework_identity=framework_identity,
            semantic_verdict="not-reviewed",
            receipt=receipt_path,
        )
    )

    assert result.returncode == 2
    assert not receipt_path.exists()
    error = json.loads(result.stderr)
    assert error["status"] == "ERROR"
    assert error["content_included"] is False
    assert "# report" not in result.stderr.decode("utf-8", "replace")
    assert os.fspath(tmp_path).encode("utf-8") not in result.stderr
