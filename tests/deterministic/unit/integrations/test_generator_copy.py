from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
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
        "modified_for_v4_deployment_contract",
        "Upgrade the generator workflow to GenerationSpec v5, manifest schema v5, "
        "explicit deployment scope, and server-owned public results.",
    ),
    "references/generated-skill-contract.md": (
        "modified_for_v4_deployment_contract",
        "Document manifest schema v5, deployment scope, generic Logparse bindings, "
        "declarative verification rules, and server-owned public results.",
    ),
    "references/wiki-template.md": (
        "modified_for_v4_deployment_contract",
        "Replace the RPC fixture template with an embedded deterministic "
        "GenerationSpec v5 example with explicit deployment scope.",
    ),
    "scripts/pack_result_zip.py": (
        "removed_for_v4_service_ownership",
        "Remove the source-tree packer because Agents cannot create public result "
        "artifacts; only the validated service finalizer owns them.",
    ),
    "scripts/validate_generated_skill.py": (
        "modified_for_v4_deployment_contract",
        "Validate manifest schema v5, deployment scope, verification contracts, "
        "embedded machine-source consistency, and Agent artifact prohibitions.",
    ),
}
EXPECTED_ADDED_FILE = {
    "path": "scripts/generate_diagnosis_skill.py",
    "source": (
        "S07 new implementation; no counterpart exists in the frozen upstream "
        "source tree."
    ),
    "purpose": (
        "Deterministically build, hash, validate, and atomically publish Diagnosis "
        "Skill products from GenerationSpec v5."
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
        [
            "git",
            "-c",
            f"safe.directory={checkout.resolve().as_posix()}",
            "-C",
            os.fspath(checkout),
            *arguments,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _run_git_bytes(checkout: Path, *arguments: str) -> bytes:
    return subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={checkout.resolve().as_posix()}",
            "-C",
            os.fspath(checkout),
            *arguments,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout


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
            commit_type = _run_git(candidate, "cat-file", "-t", EXPECTED_SOURCE_COMMIT)
        except (OSError, subprocess.CalledProcessError):
            continue
        if commit_type == "commit":
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
        "mode": "100755" if stat.S_IMODE(path.stat().st_mode) & 0o111 else "100644",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _git_file_facts(checkout: Path, relative_path: str) -> dict[str, object]:
    repository_path = (SOURCE_ROOT_RELATIVE / relative_path).as_posix()
    tree_entry = _run_git(
        checkout,
        "ls-tree",
        EXPECTED_SOURCE_COMMIT,
        "--",
        repository_path,
    )
    mode, kind, _remainder = tree_entry.split(maxsplit=2)
    assert kind == "blob"
    payload = _run_git_bytes(
        checkout,
        "show",
        f"{EXPECTED_SOURCE_COMMIT}:{repository_path}",
    )
    return {
        "mode": mode,
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
        if entry["status"] == "removed_for_v4_service_ownership":
            assert entry["delivered"] is None
        else:
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
    # The checkout may have advanced. Source bytes are read directly from the
    # immutable frozen commit's tree/blob objects without changing its HEAD.
    record_property("fixed_alias_path", os.fspath(FIXED_ALIAS_CHECKOUT))
    record_property("fixed_alias_exists", FIXED_ALIAS_CHECKOUT.exists())
    record_property("equivalent_source_checkout", os.fspath(checkout))
    if not FIXED_ALIAS_CHECKOUT.exists():
        assert checkout != FIXED_ALIAS_CHECKOUT.resolve()

    assert _run_git(checkout, "cat-file", "-t", manifest["source_commit"]) == "commit"
    remote = _run_git(checkout, "remote", "get-url", "origin")
    assert remote.replace("git@github.com:", "github.com/").replace(
        "https://github.com/", "github.com/"
    ) == manifest["repository"].replace("https://", "")

    patterns = tuple(manifest["excluded_patterns"])
    prefix = SOURCE_ROOT_RELATIVE.as_posix() + "/"
    source_files = tuple(
        line.removeprefix(prefix)
        for line in _run_git(
            checkout,
            "ls-tree",
            "-r",
            "--name-only",
            EXPECTED_SOURCE_COMMIT,
            "--",
            SOURCE_ROOT_RELATIVE.as_posix(),
        ).splitlines()
    )
    included_source_files = tuple(
        path for path in source_files if not _matches_exclusion(path, patterns)
    )
    assert included_source_files == tuple(EXPECTED_SOURCE_CHANGES)

    entries = {entry["path"]: entry for entry in manifest["files"]}
    for relative_path, (
        expected_status,
        expected_reason,
    ) in EXPECTED_SOURCE_CHANGES.items():
        delivered = DELIVERED_SKILL / relative_path
        entry = entries[relative_path]
        assert entry["source"] == _git_file_facts(checkout, relative_path)
        assert entry["status"] == expected_status
        assert entry["change_reason"] == expected_reason
        if expected_status == "removed_for_v4_service_ownership":
            assert not delivered.exists()
            assert entry["delivered"] is None
        else:
            assert entry["delivered"] == _file_facts(delivered)
            assert expected_status == "modified_for_v4_deployment_contract"
            assert entry["delivered"]["sha256"] != entry["source"]["sha256"]

    added = manifest["added_files"][0]
    added_path = added["path"]
    assert added_path not in source_files
    assert {key: added[key] for key in ("mode", "size", "sha256")} == _file_facts(
        DELIVERED_SKILL / added_path
    )

    delivered_files = _tree_files(DELIVERED_SKILL)
    expected_delivered = tuple(
        sorted(
            path
            for path, (status, _) in EXPECTED_SOURCE_CHANGES.items()
            if status != "removed_for_v4_service_ownership"
        )
        + [EXPECTED_ADDED_FILE["path"]]
    )
    assert delivered_files == tuple(sorted(expected_delivered))
    assert not any(_matches_exclusion(path, patterns) for path in delivered_files)
