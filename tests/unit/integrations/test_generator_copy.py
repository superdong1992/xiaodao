from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COPY_MANIFEST = (
    REPOSITORY_ROOT / "tests/fixtures/components/logparse/source-copy.json"
)
DELIVERED_SKILL = (
    REPOSITORY_ROOT / ".claude/skills/wiki-to-diagnosis-skill"
)
FIXED_ALIAS_CHECKOUT = REPOSITORY_ROOT.parent / "problem-locator-mcp"
SOURCE_ROOT_RELATIVE = Path(".claude/skills/wiki-to-diagnosis-skill")
EXPECTED_SOURCE_COMMIT = "97d0446580f49e7b1add1c5fc6d6a41c97884884"
EXPECTED_REPOSITORY = "https://github.com/superdong1992/problem-locator-mcp.git"
EXPECTED_SOURCE_FILES = (
    "SKILL.md",
    "references/generated-skill-contract.md",
    "references/wiki-template.md",
    "scripts/pack_result_zip.py",
    "scripts/validate_generated_skill.py",
)
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
            raise AssertionError(f"copied Skill tree contains a symlink: {path}")
        if path.is_file():
            paths.append(path.relative_to(root).as_posix())
    return tuple(sorted(paths))


def test_source_copy_manifest_freezes_the_exact_upstream_inventory() -> None:
    manifest = _manifest()

    assert set(manifest) == {
        "repository",
        "source_commit",
        "source_root",
        "files",
        "excluded_patterns",
        "observed_excluded_paths",
    }
    assert manifest["repository"] == EXPECTED_REPOSITORY
    assert manifest["source_commit"] == EXPECTED_SOURCE_COMMIT
    assert manifest["source_root"] == SOURCE_ROOT_RELATIVE.as_posix()
    assert tuple(manifest["excluded_patterns"]) == EXPECTED_EXCLUDED_PATTERNS
    assert tuple(manifest["observed_excluded_paths"]) == EXPECTED_OBSERVED_EXCLUSIONS

    entries = manifest["files"]
    assert isinstance(entries, list)
    assert len(entries) == 5
    assert tuple(entry["path"] for entry in entries) == EXPECTED_SOURCE_FILES
    assert len({entry["path"] for entry in entries}) == len(entries)
    for entry in entries:
        assert set(entry) == {"path", "mode", "size", "sha256"}
        assert entry["mode"] == "100644"
        assert isinstance(entry["size"], int) and entry["size"] > 0
        assert isinstance(entry["sha256"], str) and len(entry["sha256"]) == 64
        int(entry["sha256"], 16)


def test_copy_is_complete_and_pack_result_zip_is_byte_exact(
    record_property: Any,
) -> None:
    manifest = _manifest()
    checkout = _equivalent_source_checkout()
    source_root = checkout / SOURCE_ROOT_RELATIVE

    # The prescribed sibling alias is absent in this isolated worktree.  These
    # properties preserve that environment evidence while accepting a checkout
    # explicitly supplied by env or found at the same frozen commit.
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
    assert included_source_files == tuple(sorted(EXPECTED_SOURCE_FILES))
    assert set(excluded_source_files).issubset(
        set(manifest["observed_excluded_paths"])
    )

    entries = {entry["path"]: entry for entry in manifest["files"]}
    for relative_path in EXPECTED_SOURCE_FILES:
        source = source_root / relative_path
        entry = entries[relative_path]
        payload = source.read_bytes()
        assert len(payload) == entry["size"]
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
        assert f"100{stat.S_IMODE(source.stat().st_mode):03o}" == entry["mode"]
        assert (DELIVERED_SKILL / relative_path).is_file()

    delivered_files = _tree_files(DELIVERED_SKILL)
    assert set(delivered_files) == {
        *EXPECTED_SOURCE_FILES,
        "scripts/generate_diagnosis_skill.py",
    }
    assert not any(_matches_exclusion(path, patterns) for path in delivered_files)
    assert (
        DELIVERED_SKILL / "scripts/pack_result_zip.py"
    ).read_bytes() == (source_root / "scripts/pack_result_zip.py").read_bytes()
