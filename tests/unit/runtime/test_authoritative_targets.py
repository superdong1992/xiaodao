from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from problem_locator.contracts import (
    WorkspaceInputManifest,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.runtime.authoritative_targets import (
    empty_authoritative_targets,
    resolve_authoritative_targets,
)


ROOT = Path(__file__).resolve().parents[3]
MANIFEST_FIXTURE = ROOT / "tests/fixtures/contracts/positive/workspace-input-manifest.json"


def _manifest_value(*, anchors: list[dict[str, object]]) -> dict[str, Any]:
    value = json.loads(MANIFEST_FIXTURE.read_bytes())
    value["resolved_logparse_plan"]["anchors"] = anchors
    return value


def _manifest(*, anchors: list[dict[str, object]]) -> WorkspaceInputManifest:
    return WorkspaceInputManifest.model_validate(_manifest_value(anchors=anchors))


def _target(
    anchor: dict[str, object],
    *,
    path: str | None,
    status: str = "exact",
    module_name: str | None = None,
    slot: str | None = None,
    pid: str | None = None,
    cpu_id: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "label": anchor["label"],
        "module": anchor["module"],
        "module_key": anchor["module"],
        "module_name": module_name or anchor["module"],
        "slot": slot or anchor["slot"],
        "process_name": anchor["process_name"],
        "match_status": status,
        "caveats": [] if status == "exact" else [f"match={status}"],
    }
    discovered_pid = pid if pid is not None else anchor.get("pid")
    if discovered_pid is not None:
        result["pid"] = discovered_pid
    if cpu_id is not None:
        result["cpu_id"] = cpu_id
    if path is not None:
        result["log_path"] = path
    return result


def _audit(
    manifest: WorkspaceInputManifest,
    targets: list[dict[str, object]],
    *,
    duplicate_success: bool = False,
    request_hash: str | None = None,
) -> bytes:
    plan = manifest.resolved_logparse_plan
    assert plan is not None and plan.artifact_id is not None
    request: dict[str, object] = {
        "schema_version": 1,
        "problem_time": plan.problem_time,
        "anchors": [item.model_dump(mode="json") for item in plan.anchors],
        "artifact_id": plan.artifact_id,
    }
    result: dict[str, object] = {
        "schema_version": 1,
        "api_version": 1,
        "target_logs": targets,
    }
    record = {
        "operation": "target-logs",
        "request_sha256": request_hash
        or hashlib.sha256(canonical_json_bytes(request)).hexdigest(),
        "request": request,
        "http_status": 200,
        "result_sha256": hashlib.sha256(canonical_json_bytes(result)).hexdigest(),
        "result": result,
    }
    return canonical_json_bytes(
        {
            "schema_version": 1,
            "job_id": manifest.job_id,
            "operations": [record, deepcopy(record)] if duplicate_success else [record],
        }
    )


def _parse_audit(
    manifest: WorkspaceInputManifest,
    targets: list[dict[str, object]],
) -> bytes:
    plan = manifest.resolved_logparse_plan
    assert plan is not None and plan.attachment_id is not None
    request: dict[str, object] = {
        "schema_version": 1,
        "problem_time": plan.problem_time,
        "anchors": [item.model_dump(mode="json") for item in plan.anchors],
        "attachment_id": plan.attachment_id,
        "artifact_proposal_key": "logparse-run",
    }
    existing = next(
        entry for entry in manifest.entries if entry.input_kind == "ARTIFACT"
    )
    result: dict[str, object] = {
        "schema_version": 1,
        "api_version": 1,
        "target_logs": targets,
        "logparse_run_artifact_draft": {
            "proposal_key": "logparse-run",
            "artifact_kind": "LOGPARSE_RUN",
            "name": "logparse-run",
            "content_type": "application/vnd.problem-locator.logparse-run+directory",
            "resource_kind": "DIRECTORY",
            "workspace_relative_path": "output/proposals/logparse-run/tree",
            "declared_size": None,
            "declared_sha256": None,
            "metadata": existing.metadata.model_dump(mode="json"),
        },
    }
    record = {
        "operation": "parse-targets",
        "request_sha256": hashlib.sha256(canonical_json_bytes(request)).hexdigest(),
        "request": request,
        "http_status": 200,
        "result_sha256": hashlib.sha256(canonical_json_bytes(result)).hexdigest(),
        "result": result,
    }
    return canonical_json_bytes(
        {
            "schema_version": 1,
            "job_id": manifest.job_id,
            "operations": [record],
        }
    )


def test_reused_run_resolves_all_targets_in_plan_order_with_legacy_names() -> None:
    anchors = [
        {
            "label": "caller",
            "module": "payment",
            "slot": "request",
            "process_name": "payment-service",
            "pid": None,
        },
        {
            "label": "server",
            "module": "inventory",
            "slot": "backend",
            "process_name": "inventory-service",
            "pid": "42",
        },
    ]
    manifest = _manifest(anchors=anchors)
    targets = [
        _target(
            anchors[0],
            path="task/logs/caller.log",
            slot="slot_request",
            pid="101",
        ),
        _target(
            anchors[1],
            path="task/logs/server.log",
            status="nearest",
            slot="slot_backend",
            cpu_id="1",
        ),
    ]

    resolved = resolve_authoritative_targets(manifest, _audit(manifest, targets))

    assert [item.label for item in resolved.targets] == ["caller", "server"]
    assert [item.ordinal for item in resolved.require_deliverable()] == [1, 2]
    assert [item.archive_name for item in resolved.targets] == [
        "caller__payment__slot_request__payment-service-101.log",
        "server__inventory__slot_backend__cpu_1__inventory-service-42.log",
    ]
    assert [item.workspace_relative_path for item in resolved.targets] == [
        "inputs/artifacts/00000000-0000-0000-0000-000000000060/tree/task/logs/caller.log",
        "inputs/artifacts/00000000-0000-0000-0000-000000000060/tree/task/logs/server.log",
    ]
    artifact = next(
        entry for entry in manifest.entries if entry.input_kind == "ARTIFACT"
    )
    assert resolved.source_size == artifact.size
    assert resolved.source_sha256 == artifact.sha256


def test_new_parse_source_is_bound_to_broker_proposal_tree() -> None:
    anchors = [
        {
            "label": "caller",
            "module": "payment",
            "slot": "request",
            "process_name": "payment-service",
            "pid": None,
        }
    ]
    value = _manifest_value(anchors=anchors)
    plan = value["resolved_logparse_plan"]
    plan["attachment_id"] = value["entries"][0]["resource_id"]
    plan["artifact_id"] = None
    manifest = WorkspaceInputManifest.model_validate(value)
    targets = [_target(anchors[0], path="task/logs/caller.log")]

    resolved = resolve_authoritative_targets(manifest, _parse_audit(manifest, targets))

    target = resolved.targets[0]
    assert target.source_kind == "OUTPUT_PROPOSAL"
    assert target.source_ref == "logparse-run"
    assert target.workspace_relative_path == (
        "output/proposals/logparse-run/tree/task/logs/caller.log"
    )
    draft = parse_canonical_json_bytes(_parse_audit(manifest, targets))[
        "operations"
    ][0]["result"]["logparse_run_artifact_draft"]
    assert resolved.source_size is None
    assert resolved.source_sha256 == draft["metadata"]["tree_manifest_sha256"]


def test_real_compact_shape_preserves_discovered_identity_and_legacy_names() -> None:
    """Mirror the target_logs saved by real E2E attempts 88 and 94."""

    anchors = [
        {
            "label": "client",
            "module": "compact",
            "slot": "slot_1",
            "process_name": "checkout-client",
            "pid": None,
        },
        {
            "label": "server",
            "module": "compact",
            "slot": "slot_2",
            "process_name": "inventory-server",
            "pid": None,
        },
    ]
    value = _manifest_value(anchors=anchors)
    plan = value["resolved_logparse_plan"]
    plan["attachment_id"] = value["entries"][0]["resource_id"]
    plan["artifact_id"] = None
    manifest = WorkspaceInputManifest.model_validate(value)
    cycle = "20260731000000-20260731000003"
    targets = [
        {
            "board_cycle": cycle,
            "caveats": [],
            "cpu_cycle": None,
            "label": "client",
            "log_path": (
                "payload/mech_modules/COMPACT/slot_1/"
                f"{cycle}/checkout-client-101.log"
            ),
            "match_status": "exact",
            "module_key": "ctrl",
            "module_name": "COMPACT",
            "pid": "101",
            "process_name": "checkout-client",
            "slot": "1",
        },
        {
            "board_cycle": cycle,
            "caveats": [],
            "cpu_cycle": None,
            "label": "server",
            "log_path": (
                "payload/mech_modules/COMPACT/slot_2/"
                f"{cycle}/inventory-server-202.log"
            ),
            "match_status": "exact",
            "module_key": "ctrl",
            "module_name": "COMPACT",
            "pid": "202",
            "process_name": "inventory-server",
            "slot": "2",
        },
    ]

    resolved = resolve_authoritative_targets(
        manifest,
        _parse_audit(manifest, targets),
    )

    assert [
        (
            item.requested_module,
            item.module_key,
            item.module_name,
            item.requested_slot,
            item.slot,
            item.requested_pid,
            item.pid,
            item.cpu_id,
            item.cpu_cycle,
        )
        for item in resolved.targets
    ] == [
        ("compact", "ctrl", "COMPACT", "slot_1", "1", None, "101", None, None),
        ("compact", "ctrl", "COMPACT", "slot_2", "2", None, "202", None, None),
    ]
    assert [item.archive_name for item in resolved.targets] == [
        "client__COMPACT__slot_1__checkout-client-101.log",
        "server__COMPACT__slot_2__inventory-server-202.log",
    ]


def test_missing_and_ambiguous_are_preserved_for_the_upper_layer() -> None:
    anchors = [
        {
            "label": label,
            "module": "payment",
            "slot": "request",
            "process_name": f"{label}-service",
            "pid": None,
        }
        for label in ("missing", "ambiguous")
    ]
    manifest = _manifest(anchors=anchors)
    targets = [
        _target(anchors[0], path=None, status="missing"),
        _target(anchors[1], path=None, status="ambiguous"),
    ]

    resolved = resolve_authoritative_targets(manifest, _audit(manifest, targets))

    assert [item.match_status for item in resolved.unresolved] == [
        "missing",
        "ambiguous",
    ]
    assert all(item.archive_name is None for item in resolved.targets)
    assert all(item.workspace_relative_path is None for item in resolved.targets)
    with pytest.raises(ValueError, match="missing=missing, ambiguous=ambiguous"):
        resolved.require_deliverable()


def test_explicit_empty_target_set_never_requires_a_broker_audit() -> None:
    empty = empty_authoritative_targets()
    assert empty.problem_time is None
    assert empty.targets == ()
    assert empty.source_size is None
    assert empty.source_sha256 is None
    assert empty.require_deliverable() == ()


def test_duplicate_source_path_is_rejected() -> None:
    anchors = [
        {
            "label": label,
            "module": "payment",
            "slot": "request",
            "process_name": f"{label}-service",
            "pid": None,
        }
        for label in ("caller", "server")
    ]
    manifest = _manifest(anchors=anchors)
    targets = [
        _target(anchor, path="task/logs/shared.log") for anchor in anchors
    ]
    with pytest.raises(ValueError, match="multiple resolved anchors"):
        resolve_authoritative_targets(manifest, _audit(manifest, targets))


def test_case_insensitive_semantic_name_collision_is_rejected() -> None:
    anchors = [
        {
            "label": label,
            "module": "payment",
            "slot": "request",
            "process_name": "payment-service",
            "pid": None,
        }
        for label in ("client", "CLIENT")
    ]
    manifest = _manifest(anchors=anchors)
    targets = [
        _target(anchor, path=f"task/logs/{index}.log")
        for index, anchor in enumerate(anchors)
    ]
    with pytest.raises(ValueError, match="collide case-insensitively"):
        resolve_authoritative_targets(manifest, _audit(manifest, targets))


def test_semantic_name_preserves_safe_unicode_without_rewriting() -> None:
    anchor = {
        "label": "client",
        "module": "payment",
        "slot": "slot_1",
        "process_name": "payment-service",
        "pid": "101",
    }
    manifest = _manifest(anchors=[anchor])
    target = _target(
        anchor,
        path="task/logs/client.log",
        module_name="订单模块",
    )

    resolved = resolve_authoritative_targets(
        manifest,
        _audit(manifest, [target]),
    )

    assert (
        resolved.targets[0].archive_name
        == "client__订单模块__slot_1__payment-service-101.log"
    )


@pytest.mark.parametrize(
    "process_name",
    ["bad:name", "CON", "CON.txt", "CONOUT$", "p" * 220],
)
def test_unsafe_or_overlong_semantic_name_is_rejected(process_name: str) -> None:
    anchor = {
        "label": "client",
        "module": "payment",
        "slot": "request",
        "process_name": process_name,
        "pid": None,
    }
    manifest = _manifest(anchors=[anchor])
    target = _target(anchor, path="task/logs/client.log")
    with pytest.raises(ValueError, match="Windows archive name|too long"):
        resolve_authoritative_targets(manifest, _audit(manifest, [target]))


def test_unicode_windows_device_name_component_is_rejected() -> None:
    anchor = {
        "label": "client",
        "module": "payment",
        "slot": "request",
        "process_name": "payment-service",
        "pid": None,
    }
    manifest = _manifest(anchors=[anchor])
    target = _target(
        anchor,
        path="task/logs/client.log",
        module_name="COM¹.log",
    )
    with pytest.raises(ValueError, match="Windows archive name"):
        resolve_authoritative_targets(manifest, _audit(manifest, [target]))


def test_wrong_anchor_order_hash_or_duplicate_success_fails_closed() -> None:
    anchors = [
        {
            "label": label,
            "module": "payment",
            "slot": "request",
            "process_name": f"{label}-service",
            "pid": None,
        }
        for label in ("caller", "server")
    ]
    manifest = _manifest(anchors=anchors)
    ordered = [
        _target(anchor, path=f"task/logs/{anchor['label']}.log")
        for anchor in anchors
    ]
    with pytest.raises(ValueError, match="order or identity"):
        resolve_authoritative_targets(
            manifest,
            _audit(manifest, list(reversed(ordered))),
        )
    with pytest.raises(ValueError, match="content is invalid"):
        resolve_authoritative_targets(
            manifest,
            _audit(manifest, ordered, request_hash="0" * 64),
        )
    with pytest.raises(ValueError, match="exactly one successful"):
        resolve_authoritative_targets(
            manifest,
            _audit(manifest, ordered, duplicate_success=True),
        )
