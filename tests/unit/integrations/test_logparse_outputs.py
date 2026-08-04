from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from problem_locator.contracts import canonical_json_bytes
from problem_locator.integrations.logparse.outputs import (
    aggregate_target_results,
    inspect_controlled_run,
    inspect_existing_run,
    normalize_target_result,
)
from problem_locator.integrations.logparse.requests import Anchor


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "components" / "logparse"
FAKE_REPO = FIXTURES / "fake" / "repo"
FAKE_CLI = FAKE_REPO / "cli.py"
FAKE_CONFIG = FAKE_REPO / "config.yaml"


def _generate_fake_run(tmp_path: Path, *, marker: bytes = b"VALID") -> Path:
    workspace = tmp_path / "workspace"
    upload = workspace / "inputs" / "attachments" / "opaque-upload"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(marker)
    output_root = workspace / "output" / "proposals" / "run-1" / "tree"

    result = subprocess.run(
        [
            sys.executable,
            os.fspath(FAKE_CLI),
            "parse",
            os.fspath(upload),
            "-c",
            os.fspath(FAKE_CONFIG),
            "-o",
            os.fspath(output_root),
            "--product",
            "compact",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return output_root


def _manifest_path(root: Path) -> Path:
    return root / "task-synthetic" / "parse_manifest.json"


def _manifest_object(root: Path) -> dict[str, Any]:
    return json.loads(_manifest_path(root).read_text(encoding="utf-8"))


def _replace_manifest(root: Path, value: object) -> None:
    _manifest_path(root).write_bytes(canonical_json_bytes(value))


def _anchor(**updates: object) -> Anchor:
    values: dict[str, object] = {
        "label": "caller",
        "module": "compact",
        "slot": "1",
        "process_name": "checkout-client",
        "pid": "101",
    }
    values.update(updates)
    return Anchor(**values)


def _client_log(root: Path) -> Path:
    return (
        root
        / "task-synthetic"
        / "mech_modules"
        / "COMPACT"
        / "slot_1"
        / "cycle"
        / "checkout-client-101.log"
    )


def _server_log(root: Path) -> Path:
    return (
        root
        / "task-synthetic"
        / "mech_modules"
        / "COMPACT"
        / "slot_2"
        / "cycle"
        / "inventory-server-202.log"
    )


def _target_object(
    root: Path,
    *,
    anchor: Anchor | None = None,
    status: str = "exact",
    log_path: Path | str | None = None,
) -> dict[str, Any]:
    anchor = anchor or _anchor()
    target: dict[str, Any] = {
        "label": anchor.label,
        "module": anchor.module,
        "module_key": "compact",
        "module_name": "COMPACT",
        "slot": "slot_1" if anchor.slot == "1" else anchor.slot,
        "process_name": anchor.process_name,
        "match_status": status,
        "board_cycle": "cycle" if status in {"exact", "nearest"} else None,
        "cpu_cycle": None,
        "caveats": ["nearest interval"] if status == "nearest" else [],
    }
    if anchor.pid is not None:
        target["pid"] = anchor.pid
    if status in {"exact", "nearest"}:
        target["log_path"] = os.fspath(log_path or _client_log(root).absolute())
    elif status == "ambiguous":
        target["caveats"] = ["nearest interval tie"]
    elif status == "missing":
        target["caveats"] = ["process not found for anchor"]
    return target


def _target_payload(target: object) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": 1,
            "api_version": 1,
            "target_logs": [target],
        }
    )


def test_inspect_controlled_run_accepts_fake_output_and_hashes_the_complete_tree(
    tmp_path: Path,
) -> None:
    root = _generate_fake_run(tmp_path)

    run = inspect_controlled_run(root, product="compact")

    assert run.root == root.resolve()
    assert run.task_id == "task-synthetic"
    assert run.parse_manifest_relative_path == "task-synthetic/parse_manifest.json"
    assert [entry.path for entry in run.tree_manifest.entries] == sorted(
        entry.path for entry in run.tree_manifest.entries
    )
    assert "task-synthetic/parse_manifest.json" in {
        entry.path for entry in run.tree_manifest.entries
    }
    assert run.size == sum(entry.size for entry in run.tree_manifest.entries)
    assert run.sha256 == hashlib.sha256(
        canonical_json_bytes(run.tree_manifest)
    ).hexdigest()
    assert inspect_existing_run(
        root,
        product="compact",
        expected_parse_manifest_relative_path=run.parse_manifest_relative_path,
        expected_size=run.size,
        expected_sha256=run.sha256,
    ) == run


@pytest.mark.parametrize(
    ("marker", "message"),
    [
        (b"MISSING_MANIFEST", "manifest is missing"),
        (b"MANIFEST_DIRECTORY", "bounded plain file"),
        (b"SECOND_TASK", "exactly one task directory"),
    ],
)
def test_inspect_controlled_run_requires_one_direct_task_and_plain_manifest(
    tmp_path: Path,
    marker: bytes,
    message: str,
) -> None:
    root = _generate_fake_run(tmp_path, marker=marker)

    with pytest.raises(ValueError, match=message):
        inspect_controlled_run(root, product="compact")


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("schema_version", 2),
        ("artifact_contract_version", 2),
        ("status", "failed"),
        ("product", "default"),
        ("task_id", "another-task"),
        ("created_at", ""),
        ("stages", {}),
        ("artifacts", []),
        ("workspace", {"retained": True}),
    ],
)
def test_inspect_controlled_run_rejects_invalid_parse_manifest_contract(
    tmp_path: Path,
    field: str,
    invalid_value: object,
) -> None:
    root = _generate_fake_run(tmp_path)
    manifest = _manifest_object(root)
    manifest[field] = invalid_value
    _replace_manifest(root, manifest)

    with pytest.raises(ValueError, match="invalid success contract"):
        inspect_controlled_run(root, product="compact")


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json\n",
        b"\xff",
        b'{"schema_version":1,"schema_version":1}\n',
        b"[]\n",
    ],
)
def test_inspect_controlled_run_rejects_noncanonical_manifest_json(
    tmp_path: Path,
    payload: bytes,
) -> None:
    root = _generate_fake_run(tmp_path)
    _manifest_path(root).write_bytes(payload)

    with pytest.raises(ValueError):
        inspect_controlled_run(root, product="compact")


def test_inspect_controlled_run_rejects_a_symlinked_manifest(tmp_path: Path) -> None:
    root = _generate_fake_run(tmp_path)
    manifest = _manifest_path(root)
    external = tmp_path / "external-manifest.json"
    external.write_bytes(manifest.read_bytes())
    manifest.unlink()
    try:
        manifest.symlink_to(external)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")

    with pytest.raises(ValueError, match="bounded plain file"):
        inspect_controlled_run(root, product="compact")


def test_inspect_controlled_run_rejects_a_hardlinked_manifest(tmp_path: Path) -> None:
    root = _generate_fake_run(tmp_path)
    manifest = _manifest_path(root)
    external = tmp_path / "external-manifest.json"
    external.write_bytes(manifest.read_bytes())
    manifest.unlink()
    try:
        os.link(external, manifest)
    except (NotImplementedError, OSError):
        pytest.skip("hard links are unavailable on this platform")

    with pytest.raises(ValueError, match="bounded plain file"):
        inspect_controlled_run(root, product="compact")


def test_inspect_controlled_run_rejects_links_anywhere_in_the_tree(
    tmp_path: Path,
) -> None:
    symlink_root = _generate_fake_run(tmp_path / "symlink")
    symlink = _client_log(symlink_root).with_name("client-alias.log")
    try:
        symlink.symlink_to(_client_log(symlink_root))
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")
    with pytest.raises(ValueError, match="controlled logparse output tree is invalid"):
        inspect_controlled_run(symlink_root, product="compact")

    hardlink_root = _generate_fake_run(tmp_path / "hardlink")
    hardlink = _client_log(hardlink_root).with_name("client-hardlink.log")
    try:
        os.link(_client_log(hardlink_root), hardlink)
    except (NotImplementedError, OSError):
        pytest.skip("hard links are unavailable on this platform")
    with pytest.raises(ValueError, match="controlled logparse output tree is invalid"):
        inspect_controlled_run(hardlink_root, product="compact")


def test_existing_run_rejects_size_hash_and_manifest_path_metadata_drift(
    tmp_path: Path,
) -> None:
    root = _generate_fake_run(tmp_path)
    frozen = inspect_controlled_run(root, product="compact")

    invalid_expectations = [
        {
            "expected_parse_manifest_relative_path": frozen.parse_manifest_relative_path,
            "expected_size": frozen.size + 1,
            "expected_sha256": frozen.sha256,
        },
        {
            "expected_parse_manifest_relative_path": frozen.parse_manifest_relative_path,
            "expected_size": frozen.size,
            "expected_sha256": "0" * 64,
        },
        {
            "expected_parse_manifest_relative_path": "task-synthetic/other.json",
            "expected_size": frozen.size,
            "expected_sha256": frozen.sha256,
        },
    ]
    for expectations in invalid_expectations:
        with pytest.raises(ValueError, match="frozen metadata"):
            inspect_existing_run(root, product="compact", **expectations)


def test_existing_run_rejects_valid_manifest_content_drift(tmp_path: Path) -> None:
    root = _generate_fake_run(tmp_path)
    frozen = inspect_controlled_run(root, product="compact")
    manifest = _manifest_object(root)
    manifest["created_at"] = "2026-07-31T00:00:00.001Z"
    _replace_manifest(root, manifest)

    with pytest.raises(ValueError, match="frozen metadata"):
        inspect_existing_run(
            root,
            product="compact",
            expected_parse_manifest_relative_path=frozen.parse_manifest_relative_path,
            expected_size=frozen.size,
            expected_sha256=frozen.sha256,
        )


@pytest.mark.parametrize("status", ["exact", "nearest", "missing", "ambiguous"])
def test_normalize_target_result_accepts_real_non_explain_shapes(
    tmp_path: Path,
    status: str,
) -> None:
    root = _generate_fake_run(tmp_path)
    anchor = _anchor()
    target = _target_object(root, anchor=anchor, status=status)

    normalized = normalize_target_result(
        _target_payload(target),
        anchor=anchor,
        controlled_root=root,
    )

    assert normalized["match_status"] == status
    assert normalized["label"] == anchor.label
    if status in {"exact", "nearest"}:
        assert normalized["log_path"] == (
            "task-synthetic/mech_modules/COMPACT/slot_1/cycle/"
            "checkout-client-101.log"
        )
    else:
        assert "log_path" not in normalized


@pytest.mark.parametrize(
    "mutation",
    [
        {"label": "server"},
        {"module": "ANOTHER"},
        {"module_key": "another", "module_name": "ANOTHER"},
        {"slot": "slot_9"},
        {"process_name": "inventory-server"},
        {"pid": "999"},
    ],
)
def test_normalize_target_result_rejects_anchor_mismatches(
    tmp_path: Path,
    mutation: dict[str, object],
) -> None:
    root = _generate_fake_run(tmp_path)
    target = _target_object(root)
    target.update(mutation)

    with pytest.raises(ValueError, match="requested anchor"):
        normalize_target_result(
            _target_payload(target),
            anchor=_anchor(),
            controlled_root=root,
        )


@pytest.mark.parametrize(
    "payload",
    [
        canonical_json_bytes(
            {
                "schema_version": 1,
                "api_version": 1,
                "target_logs": [],
                "extra": True,
            }
        ),
        b'{"schema_version":1,"api_version":1,"target_logs":[],"target_logs":[]}\n',
        b"\xff",
        canonical_json_bytes(
            {
                "schema_version": 1,
                "api_version": 1,
                "target_logs": [{}, {}],
            }
        ),
    ],
)
def test_normalize_target_result_rejects_extra_duplicate_non_utf8_and_multiple(
    tmp_path: Path,
    payload: bytes,
) -> None:
    root = _generate_fake_run(tmp_path)

    with pytest.raises(ValueError):
        normalize_target_result(payload, anchor=_anchor(), controlled_root=root)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"unexpected": "field"}, "unknown fields"),
        ({"caveats": "not-an-array"}, "string array"),
        ({"pid": 101}, "requested anchor|pid must be a string"),
        ({"match_status": "guessed"}, "unsupported"),
    ],
)
def test_normalize_target_result_rejects_invalid_target_object_shapes(
    tmp_path: Path,
    mutation: dict[str, object],
    message: str,
) -> None:
    root = _generate_fake_run(tmp_path)
    target = _target_object(root)
    target.update(mutation)

    with pytest.raises(ValueError, match=message):
        normalize_target_result(
            _target_payload(target),
            anchor=_anchor(),
            controlled_root=root,
        )


def test_normalize_target_result_requires_an_absolute_contained_path(
    tmp_path: Path,
) -> None:
    root = _generate_fake_run(tmp_path)
    outside = tmp_path / "outside.log"
    outside.write_text("outside\n", encoding="utf-8")

    for invalid_path, message in [
        ("task-synthetic/relative.log", "must be absolute"),
        (outside.absolute(), "escapes"),
    ]:
        target = _target_object(root, log_path=invalid_path)
        with pytest.raises(ValueError, match=message):
            normalize_target_result(
                _target_payload(target),
                anchor=_anchor(),
                controlled_root=root,
            )


def test_normalize_target_result_rejects_a_symlinked_log_path(tmp_path: Path) -> None:
    root = _generate_fake_run(tmp_path)
    alias = _client_log(root).with_name("client-alias.log")
    try:
        alias.symlink_to(_client_log(root))
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")
    target = _target_object(root, log_path=alias.absolute())

    with pytest.raises(ValueError, match="symbolic link"):
        normalize_target_result(
            _target_payload(target),
            anchor=_anchor(),
            controlled_root=root,
        )


def test_normalize_target_result_rejects_a_hardlinked_log_path(tmp_path: Path) -> None:
    root = _generate_fake_run(tmp_path)
    alias = _client_log(root).with_name("client-hardlink.log")
    try:
        os.link(_client_log(root), alias)
    except (NotImplementedError, OSError):
        pytest.skip("hard links are unavailable on this platform")
    target = _target_object(root, log_path=alias.absolute())

    with pytest.raises(ValueError, match="single-link plain file"):
        normalize_target_result(
            _target_payload(target),
            anchor=_anchor(),
            controlled_root=root,
        )


def test_missing_and_ambiguous_targets_must_not_include_a_path(tmp_path: Path) -> None:
    root = _generate_fake_run(tmp_path)
    for status in ("missing", "ambiguous"):
        target = _target_object(root, status=status)
        target["log_path"] = os.fspath(_client_log(root).absolute())
        with pytest.raises(ValueError, match="cannot name a log path"):
            normalize_target_result(
                _target_payload(target),
                anchor=_anchor(),
                controlled_root=root,
            )


def test_aggregate_target_results_is_canonical_and_preserves_anchor_order(
    tmp_path: Path,
) -> None:
    root = _generate_fake_run(tmp_path)
    server_anchor = _anchor(
        label="server",
        slot="2",
        process_name="inventory-server",
        pid="202",
    )
    server_target = _target_object(
        root,
        anchor=server_anchor,
        log_path=_server_log(root).absolute(),
    )
    server_target["slot"] = "slot_2"
    caller_target = _target_object(root, anchor=_anchor())
    normalized = [
        normalize_target_result(
            _target_payload(server_target),
            anchor=server_anchor,
            controlled_root=root,
        ),
        normalize_target_result(
            _target_payload(caller_target),
            anchor=_anchor(),
            controlled_root=root,
        ),
    ]

    result = aggregate_target_results(normalized)

    assert result == canonical_json_bytes(
        {
            "schema_version": 1,
            "api_version": 1,
            "target_logs": normalized,
        }
    )
    assert result.endswith(b"\n") and not result.endswith(b"\n\n")
    assert [item["label"] for item in json.loads(result)["target_logs"]] == [
        "server",
        "caller",
    ]


def test_aggregate_target_results_can_include_broker_owned_run_draft() -> None:
    draft = {
        "artifact_kind": "LOGPARSE_RUN",
        "proposal_key": "run",
    }

    result = aggregate_target_results(
        [],
        logparse_run_artifact_draft=draft,
    )

    assert result == canonical_json_bytes(
        {
            "schema_version": 1,
            "api_version": 1,
            "target_logs": [],
            "logparse_run_artifact_draft": draft,
        }
    )
