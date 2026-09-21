from __future__ import annotations

import gc
import shutil
import weakref
from pathlib import Path

import pytest

from problem_locator.runtime import catalog as catalog_module
from problem_locator.runtime.asset_snapshot import snapshot_asset
from problem_locator.runtime.catalog import VersionedAssetCatalog


def _catalog(skill_dir: Path) -> VersionedAssetCatalog:
    skill_dir.mkdir(exist_ok=True)
    return VersionedAssetCatalog(
        skill_dir=skill_dir,
        generic_skill_name="generic-problem-locator-smoke",
        defer_snapshot=True,
    )


def _asset_roots(catalog: VersionedAssetCatalog) -> tuple[Path, ...]:
    return tuple(Path(asset.root_path) for asset in catalog._assets.values())


def test_closed_catalog_gc_cannot_clear_replacement_at_the_same_snapshot_path(tmp_path):
    snapshot = tmp_path / "assets"
    previous = _catalog(tmp_path / "skills")
    previous.freeze_assets(snapshot)
    finalizer = previous._snapshot_finalizer
    previous.close()
    assert finalizer is not None and not finalizer.alive

    replacement = _catalog(tmp_path / "skills")
    replacement.freeze_assets(snapshot)
    try:
        roots = _asset_roots(replacement)
        assert all(snapshot_asset(root) is not None for root in roots)
        previous_ref = weakref.ref(previous)
        del previous
        gc.collect()
        assert previous_ref() is None
        assert all(snapshot_asset(root) is not None for root in roots)
    finally:
        replacement.close()


def test_failed_snapshot_copy_releases_cache_before_replacement_and_late_gc(tmp_path, monkeypatch):
    snapshot = tmp_path / "assets"
    previous = _catalog(tmp_path / "skills")
    source_roots = _asset_roots(previous)
    registered: list[Path] = []
    original_register = catalog_module.register_snapshot
    original_copy = shutil.copytree
    copies = 0

    def register(root, ref, specialized):
        original_register(root, ref, specialized)
        registered.append(root)

    def copy(source, destination, *args, **kwargs):
        nonlocal copies
        if Path(destination).parent.parent == snapshot:
            copies += 1
            if copies == 2:
                raise OSError("snapshot copy failed")
        return original_copy(source, destination, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(catalog_module, "register_snapshot", register)
        patch.setattr(shutil, "copytree", copy)
        with pytest.raises(OSError, match="snapshot copy failed"):
            previous.freeze_assets(snapshot)

    assert registered
    assert all(snapshot_asset(root) is None for root in registered)
    assert previous._snapshot_finalizer is not None
    assert not previous._snapshot_finalizer.alive
    assert _asset_roots(previous) == source_roots
    assert not snapshot.exists()

    replacement = _catalog(tmp_path / "skills")
    replacement.freeze_assets(snapshot)
    try:
        del previous
        gc.collect()
        assert all(snapshot_asset(root) is not None for root in _asset_roots(replacement))
    finally:
        replacement.close()
