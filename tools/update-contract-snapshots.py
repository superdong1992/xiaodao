"""Refresh frozen schema bytes and reviewed fixture manifests reproducibly.

Usage: uv run --frozen python tools/update-contract-snapshots.py [--fixtures]
       Add --advance-fixtures-from N only for an approved state hard cut.
This never generates OpenAPI or assigns metadata to previously unreviewed files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from problem_locator.contracts import CONTRACT_REVISION, SCHEMA_VERSION  # noqa: E402
from problem_locator.contracts.serialization import (  # noqa: E402
    canonical_json_bytes,
    contract_manifest_bytes,
    schema_bundle_bytes,
)


def _write(path: Path, content: bytes) -> None:
    if path.read_bytes() != content:
        path.write_bytes(content)
        print(path.relative_to(ROOT).as_posix())


def _advance(value: object, previous: int) -> object:
    if isinstance(value, list):
        return [_advance(item, previous) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _advance(item, previous) for key, item in value.items()}
    if result.get("contract_revision") == f"v{previous}-contract-r1":
        result["contract_revision"] = CONTRACT_REVISION
        if result.get("schema_version") == previous:
            result["schema_version"] = SCHEMA_VERSION
    return result


def advance_fixtures(previous: int) -> None:
    if not 1 <= previous < SCHEMA_VERSION:
        raise ValueError("源 fixture 版本必须早于当前合同版本。")
    fixture_root = ROOT / "tests" / "fixtures"
    for path in sorted(fixture_root.rglob("*.json")):
        if path.name == "fixture-manifest.json":
            continue
        try:
            original = json.loads(path.read_bytes())
        except (ValueError, UnicodeError):
            # Truncated/invalid JSON is deliberate negative test input.
            continue
        updated = _advance(original, previous)
        if updated != original:
            _write(path, canonical_json_bytes(updated))
    storage = fixture_root / "components" / "storage"
    old = storage / "state" / f"valid-empty-v{previous}.json"
    new = storage / "state" / f"valid-empty-v{SCHEMA_VERSION}.json"
    if old.exists():
        if new.exists():
            raise ValueError("新版本 fixture 已存在，不能覆盖。")
        old.rename(new)
        manifest_path = storage / "fixture-manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        for entry in manifest["files"]:
            if entry["path"] == old.relative_to(storage).as_posix():
                entry["path"] = new.relative_to(storage).as_posix()
        manifest["files"].sort(key=lambda entry: entry["path"])
        _write(manifest_path, canonical_json_bytes(manifest))


def refresh_fixture_manifests() -> None:
    for manifest_path in sorted((ROOT / "tests" / "fixtures").rglob("fixture-manifest.json")):
        fixture_root = manifest_path.parent
        manifest = json.loads(manifest_path.read_bytes())
        paths = sorted(path.relative_to(fixture_root).as_posix() for path in fixture_root.rglob("*")
                       if path.is_file() and path != manifest_path)
        declared = [entry["path"] for entry in manifest["files"]]
        if sorted(declared) != paths:
            raise ValueError(f"fixture 文件集合变化，需先评审 purpose/schema_ref：{manifest_path}")
        for entry in manifest["files"]:
            path = fixture_root / entry["path"]
            if path.is_symlink() or not path.resolve().is_relative_to(fixture_root.resolve()):
                raise ValueError("fixture 路径不安全。")
            content = path.read_bytes()
            entry["size"] = len(content)
            entry["sha256"] = hashlib.sha256(content).hexdigest()
        _write(manifest_path, canonical_json_bytes(manifest))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", action="store_true")
    parser.add_argument("--web", action="store_true", help="同时刷新完整 REST OpenAPI 快照（不启动服务）。")
    parser.add_argument("--advance-fixtures-from", type=int)
    args = parser.parse_args()
    schemas = ROOT / "schemas" / "v2"
    for name, content in schema_bundle_bytes().items():
        _write(schemas / name, content)
    _write(schemas / "contract-manifest.json", contract_manifest_bytes(ROOT))
    if args.web:
        from problem_locator.interfaces.http_app import create_http_app

        # Schema generation only registers routes; no port is invoked and no
        # DATA_ROOT, runtime, network listener or model is initialized.
        app = create_http_app(command_port=None, query_port=None, state_admin=None,
            public_base_url="http://127.0.0.1:8000")
        _write(schemas / "web-api.openapi.snapshot.json", canonical_json_bytes(app.openapi()))
    if args.advance_fixtures_from is not None:
        advance_fixtures(args.advance_fixtures_from)
    if args.fixtures or args.advance_fixtures_from is not None:
        refresh_fixture_manifests()


if __name__ == "__main__":
    main()
