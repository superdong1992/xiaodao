from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from problem_locator.contracts.enums import AssetKind
from problem_locator.integrations.logparse.broker import build_logparse_runtime
from problem_locator.runtime.catalog import VersionedAssetCatalog


SERVICE_UID = 10001
SERVICE_GID = 10001
LOGPARSE_REPO = Path("/opt/src/logparse")
LOGPARSE_CONFIG = LOGPARSE_REPO / "config.yaml"
LOGPARSE_PYTHON = Path("/opt/venvs/logparse/bin/python")
SKILL_DIR = Path("/opt/e2e-skills")
EXPECTED_SKILL_ID = "diagnosis-skill/diagnose-service-takeover"


def require(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(code)


def root_owned_real_directory(path: Path, code: str) -> None:
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode), code)
    require(not stat.S_ISLNK(info.st_mode), code)
    require(info.st_uid == 0 and info.st_gid == 0, code)
    require(not os.access(path, os.W_OK, effective_ids=True), code)


def verify_tree_boundary(root: Path) -> int:
    root_owned_real_directory(root, "LOGPARSE_REPO_BOUNDARY")
    root_owned_real_directory(root / ".git", "LOGPARSE_GIT_BOUNDARY")
    entries = 0
    for directory, names, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        directory_info = directory_path.lstat()
        require(
            directory_info.st_uid == 0 and directory_info.st_gid == 0,
            "LOGPARSE_TREE_OWNER",
        )
        require(
            not os.access(directory_path, os.W_OK, effective_ids=True),
            "LOGPARSE_TREE_WRITABLE",
        )
        entries += 1
        for name in [*names, *files]:
            entry = directory_path / name
            info = entry.lstat()
            require(info.st_uid == 0 and info.st_gid == 0, "LOGPARSE_TREE_OWNER")
            if not stat.S_ISLNK(info.st_mode):
                require(
                    not os.access(entry, os.W_OK, effective_ids=True),
                    "LOGPARSE_TREE_WRITABLE",
                )
            entries += 1
    require(entries > 2, "LOGPARSE_TREE_EMPTY")
    return entries


def main() -> None:
    require(os.getresuid() == (SERVICE_UID,) * 3, "NONROOT_UID")
    require(os.getresgid() == (SERVICE_GID,) * 3, "NONROOT_GID")
    require(LOGPARSE_CONFIG.is_file(), "LOGPARSE_CONFIG")
    require(LOGPARSE_PYTHON.is_file(), "LOGPARSE_PYTHON")
    require(SKILL_DIR.is_dir(), "SKILL_DIR")
    entries = verify_tree_boundary(LOGPARSE_REPO)

    asset, broker_factory = build_logparse_runtime(
        LOGPARSE_REPO,
        LOGPARSE_CONFIG,
        LOGPARSE_PYTHON,
    )
    require(asset.asset_kind is AssetKind.LOGPARSE_TOOL, "LOGPARSE_ASSET_KIND")
    require(Path(asset.root_path).resolve() == LOGPARSE_REPO, "LOGPARSE_ASSET_ROOT")
    catalog = VersionedAssetCatalog(
        skill_dir=SKILL_DIR,
        logparse_tool=asset,
        logparse_broker_factory=broker_factory,
        allow_test_skills=True,
    )
    route = catalog.route_bindings()
    selected = next(
        (ref for ref in route.available_skill_refs if ref.id == EXPECTED_SKILL_ID),
        None,
    )
    require(selected is not None, "CATALOG_SKILL")
    diagnose = catalog.diagnose_bindings(selected)
    require(diagnose.logparse_tool_ref == asset.ref, "CATALOG_LOGPARSE_PAIR")
    resolved = catalog.resolve(asset.ref)
    require(resolved.asset_kind is AssetKind.LOGPARSE_TOOL, "CATALOG_RESOLVE")
    require(catalog.check([asset.ref, selected]).missing_refs == [], "CATALOG_CHECK")

    payload = {
        "asset_kind": "LOGPARSE_TOOL",
        "asset_runtime_build": "PASS",
        "catalog_logparse_pair": "PASS",
        "catalog_skill_id": EXPECTED_SKILL_ID,
        "catalog_startup_scan": "PASS",
        "logparse_git_owner": "0:0",
        "logparse_repo_owner": "0:0",
        "logparse_tree_entries_scanned": entries,
        "logparse_tree_writable_entries": 0,
        "runtime_gid": SERVICE_GID,
        "runtime_uid": SERVICE_UID,
        "schema_version": 1,
        "status": "PASS",
    }
    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        flush=True,
    )


try:
    main()
except Exception:
    raise SystemExit("NONROOT_LOGPARSE_CATALOG_VERIFICATION_FAILED") from None
