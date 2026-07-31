from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COPY_MANIFEST = REPOSITORY_ROOT / "tests/fixtures/components/logparse/source-copy.json"
DELIVERED_SKILL = REPOSITORY_ROOT / ".claude/skills/wiki-to-diagnosis-skill"
FIXED_ALIAS_CHECKOUT = REPOSITORY_ROOT.parent / "problem-locator-mcp"
SOURCE_ROOT_RELATIVE = Path(".claude/skills/wiki-to-diagnosis-skill")
EXPECTED_SOURCE_COMMIT = "97d0446580f49e7b1add1c5fc6d6a41c97884884"
EXPECTED_REPOSITORY = "https://github.com/superdong1992/problem-locator-mcp.git"
EXPECTED_CHECKOUT_EVIDENCE = {
    "fixed_alias": "../problem-locator-mcp",
    "fixed_alias_status": "missing_in_isolated_worktree",
    "equivalent_checkout_requirement": "repository_and_commit_verified",
}
EXPECTED_SOURCE_CHANGES = {
    "SKILL.md": (
        "modified_for_s07_v2_contract",
        "Upgrade the generator Skill instructions from the upstream 1.x workflow "
        "to the frozen deterministic 2.0.0 product and S00 four-result contract.",
    ),
    "references/generated-skill-contract.md": (
        "modified_for_s07_v2_contract",
        "Replace the upstream 1.x generated-product rules with the frozen S00 "
        "DTO/schema, deterministic diagnosis-skill.json, four-result, USER_RESULT, "
        "and broker-only logparse contracts.",
    ),
    "references/wiki-template.md": (
        "modified_for_s07_v2_contract",
        "Upgrade the upstream generic wiki template to the non-sensitive S07 "
        "service-takeover fixture shape, fixed parameter groups, evidence rules, "
        "and versioned generation inputs.",
    ),
    "scripts/pack_result_zip.py": (
        "byte_exact",
        "No S07 2.0.0 contract change was required; preserve the frozen upstream "
        "file byte-for-byte.",
    ),
    "scripts/validate_generated_skill.py": (
        "modified_for_s07_v2_contract",
        "Replace upstream 1.x YAML/frontmatter and result.zip validation with "
        "deterministic 2.0.0 product, diagnosis-skill.json, content-type, and S00 "
        "outcome-contract validation.",
    ),
}
EXPECTED_ADDED_FILE = {
    "path": "scripts/generate_diagnosis_skill.py",
    "source": (
        "S07 new implementation; no counterpart exists in the frozen upstream "
        "source tree."
    ),
    "purpose": (
        "Deterministically build, hash, validate, and atomically publish versioned "
        "diagnose-* products from non-sensitive Wiki input under the frozen S00/S07 "
        "contracts."
    ),
}
EXPECTED_EXCLUDED_PATTERNS = (
    "**/__pycache__/**",
    "**/.pytest_cache/**",
    "**/*.pyc",
    "**/.DS_Store",
    "**/.managed",
    "**/.managed.*",
    "**/.codex-managed",
)
EXPECTED_OBSERVED_EXCLUSIONS = (
    "scripts/__pycache__/pack_result_zip.cpython-312.pyc",
    "scripts/__pycache__/validate_generated_skill.cpython-312.pyc",
)


def _run_git(checkout: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", os.fspath(checkout), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _manifest() -> dict[str, Any]:
    value = json.loads(COPY_MANIFEST.read_bytes())
    assert isinstance(value, dict)
    return value


def _candidate_checkouts() -> tuple[Path, ...]:
    explicit = os.environ.get("PROBLEM_LOCATOR_MCP_SOURCE_REPO") or os.environ.get(
        "PROBLEM_LOCATOR_MCP_REPO"
    )
    if explicit:
        return (Path(explicit).expanduser(),)
    return (
        FIXED_ALIAS_CHECKOUT,
        Path.home() / "Documents/problem-locator-mcp",
        Path.home() / "Documents/debug",
    )


def _equivalent_source_checkout() -> Path:
    inspected: list[str] = []
    for candidate in _candidate_checkouts():
        candidate = candidate.resolve()
        inspected.append(os.fspath(candidate))
        if not (candidate / SOURCE_ROOT_RELATIVE).is_dir():
            continue
        try:
            commit = _run_git(candidate, "rev-parse", "HEAD")
        except (OSError, subprocess.CalledProcessError):
            continue
        if commit == EXPECTED_SOURCE_COMMIT:
            return candidate
    raise AssertionError(
        "no equivalent problem-locator-mcp checkout at source commit "
        f"{EXPECTED_SOURCE_COMMIT}; inspected={inspected}; set "
        "PROBLEM_LOCATOR_MCP_SOURCE_REPO to the frozen checkout"
    )


def _matches_exclusion(relative_path: str, patterns: tuple[str, ...]) -> bool:
    path = PurePosixPath(relative_path)
    for pattern in patterns:
        if path.match(pattern):
            return True
        if pattern.startswith("**/") and path.match(pattern[3:]):
            return True
    return False


def _tree_files(root: Path) -> tuple[str, ...]:
    paths: list[str] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise AssertionError(f"Skill tree contains a symlink: {path}")
        if path.is_file():
            paths.append(path.relative_to(root).as_posix())
    return tuple(sorted(paths))


def _file_facts(path: Path) -> dict[str, object]:
    assert path.is_file()
    assert not path.is_symlink()
    payload = path.read_bytes()
    return {
        "mode": f"100{stat.S_IMODE(path.stat().st_mode):03o}",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _assert_file_facts_shape(value: object) -> None:
    assert isinstance(value, dict)
    assert set(value) == {"mode", "size", "sha256"}
    assert value["mode"] == "100644"
    assert isinstance(value["size"], int) and value["size"] > 0
    assert isinstance(value["sha256"], str) and len(value["sha256"]) == 64
    int(value["sha256"], 16)


def test_source_copy_receipt_has_complete_sorted_fields() -> None:
    manifest = _manifest()

    assert set(manifest) == {
        "schema_version",
        "repository",
        "source_commit",
        "source_root",
        "source_checkout_evidence",
        "files",
        "added_files",
        "excluded_patterns",
        "observed_excluded_paths",
    }
    assert manifest["schema_version"] == 1
    assert manifest["repository"] == EXPECTED_REPOSITORY
    assert manifest["source_commit"] == EXPECTED_SOURCE_COMMIT
    assert manifest["source_root"] == SOURCE_ROOT_RELATIVE.as_posix()
    assert manifest["source_checkout_evidence"] == EXPECTED_CHECKOUT_EVIDENCE
    assert tuple(manifest["excluded_patterns"]) == EXPECTED_EXCLUDED_PATTERNS
    assert tuple(manifest["observed_excluded_paths"]) == EXPECTED_OBSERVED_EXCLUSIONS

    entries = manifest["files"]
    assert isinstance(entries, list)
    expected_paths = tuple(EXPECTED_SOURCE_CHANGES)
    assert expected_paths == tuple(sorted(expected_paths))
    assert tuple(entry["path"] for entry in entries) == expected_paths
    assert len({entry["path"] for entry in entries}) == len(entries)
    for entry in entries:
        assert set(entry) == {
            "path",
            "source",
            "delivered",
            "status",
            "change_reason",
        }
        _assert_file_facts_shape(entry["source"])
        _assert_file_facts_shape(entry["delivered"])
        assert (entry["status"], entry["change_reason"]) == (
            EXPECTED_SOURCE_CHANGES[entry["path"]]
        )

    added_files = manifest["added_files"]
    assert isinstance(added_files, list)
    assert tuple(entry["path"] for entry in added_files) == (
        EXPECTED_ADDED_FILE["path"],
    )
    added = added_files[0]
    assert set(added) == {
        "path",
        "source",
        "purpose",
        "mode",
        "size",
        "sha256",
    }
    assert {
        "path": added["path"],
        "source": added["source"],
        "purpose": added["purpose"],
    } == EXPECTED_ADDED_FILE
    _assert_file_facts_shape({key: added[key] for key in ("mode", "size", "sha256")})


def test_receipt_matches_source_and_every_delivered_byte(
    record_property: Any,
) -> None:
    manifest = _manifest()
    checkout = _equivalent_source_checkout()
    source_root = checkout / SOURCE_ROOT_RELATIVE

    # The prescribed sibling alias is absent in this isolated worktree. The
    # receipt and properties retain that environment evidence while the source
    # bytes are verified against an equivalent checkout at the frozen commit.
    record_property("fixed_alias_path", os.fspath(FIXED_ALIAS_CHECKOUT))
    record_property("fixed_alias_exists", FIXED_ALIAS_CHECKOUT.exists())
    record_property("equivalent_source_checkout", os.fspath(checkout))
    if not FIXED_ALIAS_CHECKOUT.exists():
        assert checkout != FIXED_ALIAS_CHECKOUT.resolve()

    assert _run_git(checkout, "rev-parse", "HEAD") == manifest["source_commit"]
    assert _run_git(checkout, "remote", "get-url", "origin") == manifest["repository"]

    patterns = tuple(manifest["excluded_patterns"])
    source_files = _tree_files(source_root)
    included_source_files = tuple(
        path for path in source_files if not _matches_exclusion(path, patterns)
    )
    excluded_source_files = tuple(
        path for path in source_files if _matches_exclusion(path, patterns)
    )
    assert included_source_files == tuple(EXPECTED_SOURCE_CHANGES)
    assert set(excluded_source_files).issubset(EXPECTED_OBSERVED_EXCLUSIONS)

    entries = {entry["path"]: entry for entry in manifest["files"]}
    for relative_path, (
        expected_status,
        expected_reason,
    ) in EXPECTED_SOURCE_CHANGES.items():
        source = source_root / relative_path
        delivered = DELIVERED_SKILL / relative_path
        entry = entries[relative_path]
        source_payload = source.read_bytes()
        delivered_payload = delivered.read_bytes()

        assert entry["source"] == _file_facts(source)
        assert entry["delivered"] == _file_facts(delivered)
        assert entry["status"] == expected_status
        assert entry["change_reason"] == expected_reason
        if expected_status == "byte_exact":
            assert delivered_payload == source_payload
        else:
            assert expected_status == "modified_for_s07_v2_contract"
            assert delivered_payload != source_payload

    added = manifest["added_files"][0]
    added_path = added["path"]
    assert not (source_root / added_path).exists()
    assert {key: added[key] for key in ("mode", "size", "sha256")} == _file_facts(
        DELIVERED_SKILL / added_path
    )

    delivered_files = _tree_files(DELIVERED_SKILL)
    expected_delivered = tuple(
        sorted((*EXPECTED_SOURCE_CHANGES, EXPECTED_ADDED_FILE["path"]))
    )
    assert delivered_files == expected_delivered
    assert not any(_matches_exclusion(path, patterns) for path in delivered_files)
