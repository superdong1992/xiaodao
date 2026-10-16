from __future__ import annotations

import json

import pytest

from problem_locator.contracts import CONTRACT_REVISION, StateFile, canonical_json_bytes
from problem_locator.storage.layout import StorageLayout, UnsupportedDataFormatError
from problem_locator.storage.postgres_layout import load_postgres_state, marker_bytes, preflight_postgres_root
from tests.deterministic.unit.application.test_external_commands import _state


def test_postgres_r2_marker_is_read_without_rewriting_and_older_marker_is_rejected(tmp_path):
    layout = StorageLayout.at(tmp_path / "pg-root")
    layout.data_root.mkdir()
    installation = "00000000-0000-0000-0000-000000000921"
    raw = marker_bytes(installation, contract_revision="v11-contract-r2")
    layout.data_format_marker.write_bytes(raw)
    assert preflight_postgres_root(layout)["contract_revision"] == "v11-contract-r2"
    assert layout.data_format_marker.read_bytes() == raw
    layout.data_format_marker.write_bytes(marker_bytes(installation, contract_revision="v11-contract-r1"))
    with pytest.raises(UnsupportedDataFormatError):
        preflight_postgres_root(layout)


def test_postgres_r2_snapshot_projects_only_header_and_keeps_nested_legacy_bytes():
    current = _state().model_dump(mode="json")
    source = {**current, "contract_revision": "v11-contract-r2"}
    raw = canonical_json_bytes(source)
    loaded = load_postgres_state(raw)
    assert loaded.contract_revision == CONTRACT_REVISION
    assert loaded.model_dump(mode="json") == current
    assert canonical_json_bytes(json.loads(raw)["cases"]) == canonical_json_bytes(loaded.model_dump(mode="json")["cases"])
    assert json.loads(raw)["contract_revision"] == "v11-contract-r2"
    with pytest.raises(ValueError):
        StateFile.model_validate_json(raw)


def test_postgres_rejects_mislabeled_new_context_and_unrelated_old_revisions():
    source = _state().model_dump(mode="json")
    source["contract_revision"] = "v11-contract-r2"
    next(iter(source["cases"].values()))["case"]["continuation_context"] = None
    with pytest.raises(ValueError, match="r2"):
        load_postgres_state(canonical_json_bytes(source))
    source["contract_revision"] = "v11-contract-r1"
    with pytest.raises(ValueError):
        load_postgres_state(canonical_json_bytes(source))
