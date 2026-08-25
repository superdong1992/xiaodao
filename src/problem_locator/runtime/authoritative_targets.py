"""Recover the server-authoritative Logparse target set from its audit trail.

The resolved plan owns target identity and order.  The broker audit owns the
mechanically selected result and its LOGPARSE_RUN source.  Agent-authored paths
are deliberately not an input to this module.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from problem_locator.contracts import (
    AgentArtifactProposalDraft,
    ArtifactKind,
    LogparseRunMetadata,
    ResourceKind,
    WorkspaceArtifactInput,
    WorkspaceAttachmentInput,
    WorkspaceInputManifest,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.integrations.logparse.paths import validate_relative_path
from problem_locator.integrations.logparse.requests import (
    ParseTargetsRequest,
    TargetLogsRequest,
)


_TARGET_FIELDS = frozenset(
    {
        "label",
        "module",
        "module_key",
        "module_name",
        "slot",
        "process_name",
        "pid",
        "match_status",
        "board_cycle",
        "cpu_id",
        "cpu_cycle",
        "caveats",
        "log_path",
    }
)
_REQUIRED_TARGET_FIELDS = frozenset(
    {
        "label",
        "module_key",
        "module_name",
        "slot",
        "process_name",
        "match_status",
        "caveats",
    }
)
_DELIVERABLE_STATUSES = frozenset({"exact", "nearest"})
_UNDELIVERABLE_STATUSES = frozenset({"missing", "ambiguous"})
_WINDOWS_FORBIDDEN = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED = re.compile(
    (
        r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|"
        r"COM(?:[1-9]|[¹²³])|LPT(?:[1-9]|[¹²³]))(?:\..*)?$"
    ),
    flags=re.IGNORECASE,
)
_MAX_ARCHIVE_NAME_UTF8 = 240


@dataclass(frozen=True, slots=True)
class AuthoritativeTargetLog:
    """One target in resolved-plan order, including non-deliverable matches."""

    ordinal: int
    label: str
    requested_module: str
    requested_slot: str
    requested_process_name: str
    requested_pid: str | None
    module_key: str
    module_name: str
    slot: str
    process_name: str
    pid: str | None
    match_status: Literal["exact", "nearest", "missing", "ambiguous"]
    board_cycle: str | None
    cpu_id: str | None
    cpu_cycle: str | None
    caveats: tuple[str, ...]
    source_kind: Literal["INPUT_ARTIFACT", "OUTPUT_PROPOSAL"]
    source_ref: str
    source_root: str
    log_path: str | None
    archive_name: str | None

    @property
    def deliverable(self) -> bool:
        return self.match_status in _DELIVERABLE_STATUSES

    @property
    def workspace_relative_path(self) -> str | None:
        if self.log_path is None:
            return None
        return PurePosixPath(self.source_root, self.log_path).as_posix()


@dataclass(frozen=True, slots=True)
class AuthoritativeTargetSet:
    """The complete ordered target result for one resolved Logparse plan."""

    problem_time: str | None
    targets: tuple[AuthoritativeTargetLog, ...]
    source_size: int | None
    source_sha256: str | None

    @property
    def unresolved(self) -> tuple[AuthoritativeTargetLog, ...]:
        return tuple(target for target in self.targets if not target.deliverable)

    def require_deliverable(self) -> tuple[AuthoritativeTargetLog, ...]:
        unresolved = self.unresolved
        if unresolved:
            labels = ", ".join(
                f"{target.label}={target.match_status}" for target in unresolved
            )
            raise ValueError(f"authoritative target logs are not deliverable: {labels}")
        return self.targets


def _single_line_text(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"broker target {field} must be canonical non-empty text")
    return value


def _optional_single_line_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _single_line_text(value, field=field)


def _normalized_slot(value: str) -> str:
    return value[5:] if value.casefold().startswith("slot_") else value


def _filename_component(value: str, *, field: str) -> str:
    value = _single_line_text(value, field=field)
    if (
        value in {".", ".."}
        or value.endswith((" ", "."))
        or any(character in _WINDOWS_FORBIDDEN for character in value)
        or _WINDOWS_RESERVED.fullmatch(value) is not None
    ):
        raise ValueError(f"broker target {field} is unsafe in a Windows archive name")
    return value


def _archive_name(target: dict[str, Any]) -> str:
    label = _filename_component(target["label"], field="label")
    module_name = _filename_component(target["module_name"], field="module_name")
    slot = _normalized_slot(target["slot"])
    slot = _filename_component(slot, field="slot")
    process_name = _filename_component(target["process_name"], field="process_name")
    pid = target.get("pid")
    if pid is not None:
        pid = _filename_component(pid, field="pid")
        process_name = f"{process_name}-{pid}"
    parts = [label, module_name, f"slot_{slot}"]
    cpu_id = target.get("cpu_id")
    if cpu_id is not None:
        parts.append(f"cpu_{_filename_component(cpu_id, field='cpu_id')}")
    parts.append(process_name)
    name = "__".join(parts) + ".log"
    if len(name.encode("utf-8")) > _MAX_ARCHIVE_NAME_UTF8:
        raise ValueError("broker target semantic archive name is too long")
    return name


def semantic_archive_name(target: AuthoritativeTargetLog) -> str:
    """Recompute the frozen legacy semantic filename from target metadata."""

    if not isinstance(target, AuthoritativeTargetLog):
        raise TypeError("target must be an AuthoritativeTargetLog")
    return _archive_name(
        {
            "label": target.label,
            "module_name": target.module_name,
            "slot": target.slot,
            "process_name": target.process_name,
            "pid": target.pid,
            "cpu_id": target.cpu_id,
        }
    )


def empty_authoritative_targets() -> AuthoritativeTargetSet:
    """Return the explicit no-Logparse target set without reading an audit."""

    return AuthoritativeTargetSet(
        problem_time=None,
        targets=(),
        source_size=None,
        source_sha256=None,
    )


def _request_matches_plan(
    request: ParseTargetsRequest | TargetLogsRequest,
    *,
    manifest: WorkspaceInputManifest,
) -> None:
    plan = manifest.resolved_logparse_plan
    if plan is None:
        raise ValueError("workspace has no resolved Logparse plan")
    if request.problem_time != str(plan.problem_time) or len(request.anchors) != len(
        plan.anchors
    ):
        raise ValueError("broker request differs from the resolved Logparse plan")
    for requested, resolved in zip(request.anchors, plan.anchors, strict=True):
        if (
            requested.label,
            requested.module,
            requested.slot,
            requested.process_name,
            requested.pid,
        ) != (
            resolved.label,
            resolved.module,
            resolved.slot,
            resolved.process_name,
            resolved.pid,
        ):
            raise ValueError("broker anchor order differs from the resolved Logparse plan")
    if isinstance(request, ParseTargetsRequest):
        if plan.attachment_id is None or request.attachment_id != plan.attachment_id:
            raise ValueError("broker parse source differs from the resolved Logparse plan")
    elif plan.artifact_id is None or request.artifact_id != plan.artifact_id:
        raise ValueError("broker artifact source differs from the resolved Logparse plan")


def _source_from_success(
    *,
    manifest: WorkspaceInputManifest,
    operation: str,
    request_value: dict[str, Any],
    result_value: dict[str, Any],
) -> tuple[
    Literal["INPUT_ARTIFACT", "OUTPUT_PROPOSAL"],
    str,
    str,
    int | None,
    str,
]:
    plan = manifest.resolved_logparse_plan
    if plan is None:
        raise ValueError("workspace has no resolved Logparse plan")
    if plan.attachment_id is not None:
        if operation != "parse-targets":
            raise ValueError("successful broker operation differs from the resolved source")
        request = ParseTargetsRequest.model_validate(request_value)
        _request_matches_plan(request, manifest=manifest)
        draft_value = result_value.get("logparse_run_artifact_draft")
        if not isinstance(draft_value, dict):
            raise ValueError("successful parse result lacks its LOGPARSE_RUN draft")
        draft = AgentArtifactProposalDraft.model_validate(draft_value)
        expected_root = f"output/proposals/{request.artifact_proposal_key}/tree"
        attachments = [
            entry
            for entry in manifest.entries
            if isinstance(entry, WorkspaceAttachmentInput)
            and entry.resource_id == request.attachment_id
        ]
        if (
            len(attachments) != 1
            or draft.proposal_key != request.artifact_proposal_key
            or draft.artifact_kind is not ArtifactKind.LOGPARSE_RUN
            or draft.resource_kind is not ResourceKind.DIRECTORY
            or str(draft.workspace_relative_path) != expected_root
            or draft.declared_size is not None
            or draft.declared_sha256 is not None
            or not isinstance(draft.metadata, LogparseRunMetadata)
            or draft.metadata.source_attachment_id != request.attachment_id
            or draft.metadata.source_attachment_sha256 != attachments[0].sha256
            or draft.metadata.logparse_version_ref != manifest.logparse_tool_ref
            or draft.metadata.parse_parameters.product != manifest.logparse_product
        ):
            raise ValueError("successful parse result has an invalid LOGPARSE_RUN source")
        return (
            "OUTPUT_PROPOSAL",
            request.artifact_proposal_key,
            expected_root,
            None,
            draft.metadata.tree_manifest_sha256,
        )

    if operation != "target-logs":
        raise ValueError("successful broker operation differs from the resolved source")
    request = TargetLogsRequest.model_validate(request_value)
    _request_matches_plan(request, manifest=manifest)
    if "logparse_run_artifact_draft" in result_value:
        raise ValueError("reused LOGPARSE_RUN result cannot propose another run")
    matches = [
        entry
        for entry in manifest.entries
        if isinstance(entry, WorkspaceArtifactInput)
        and entry.resource_id == request.artifact_id
    ]
    if len(matches) != 1:
        raise ValueError("resolved LOGPARSE_RUN artifact is not uniquely materialized")
    artifact = matches[0]
    if (
        artifact.artifact_kind is not ArtifactKind.LOGPARSE_RUN
        or artifact.resource_kind is not ResourceKind.DIRECTORY
        or str(artifact.relative_path)
        != f"inputs/artifacts/{request.artifact_id}/tree"
    ):
        raise ValueError("resolved artifact is not a materialized LOGPARSE_RUN")
    return (
        "INPUT_ARTIFACT",
        request.artifact_id,
        str(artifact.relative_path),
        artifact.size,
        artifact.sha256,
    )


def _target_for_anchor(
    value: object,
    *,
    ordinal: int,
    anchor: Any,
    source_kind: Literal["INPUT_ARTIFACT", "OUTPUT_PROPOSAL"],
    source_ref: str,
    source_root: str,
) -> AuthoritativeTargetLog:
    if not isinstance(value, dict) or not _REQUIRED_TARGET_FIELDS <= set(value):
        raise ValueError("broker target object is missing required fields")
    if not set(value) <= _TARGET_FIELDS:
        raise ValueError("broker target object contains unknown fields")
    target = dict(value)
    label = _single_line_text(target.get("label"), field="label")
    module_key = _single_line_text(target.get("module_key"), field="module_key")
    module_name = _single_line_text(target.get("module_name"), field="module_name")
    slot = _single_line_text(target.get("slot"), field="slot")
    process_name = _single_line_text(
        target.get("process_name"), field="process_name"
    )
    status = _single_line_text(target.get("match_status"), field="match_status")
    if status not in _DELIVERABLE_STATUSES | _UNDELIVERABLE_STATUSES:
        raise ValueError("broker target match_status is unsupported")
    module = target.get("module")
    if "module" in target:
        module = _single_line_text(module, field="module")
    pid = target.get("pid")
    if "pid" in target:
        pid = _single_line_text(pid, field="pid")
    board_cycle = _optional_single_line_text(
        target.get("board_cycle"), field="board_cycle"
    )
    cpu_id = target.get("cpu_id")
    if "cpu_id" in target:
        cpu_id = _single_line_text(cpu_id, field="cpu_id")
    cpu_cycle = _optional_single_line_text(target.get("cpu_cycle"), field="cpu_cycle")
    caveats = target.get("caveats")
    if not isinstance(caveats, list) or any(not isinstance(item, str) for item in caveats):
        raise ValueError("broker target caveats must be a string array")
    if any("\r" in item or "\x00" in item for item in caveats):
        raise ValueError("broker target caveats contain forbidden control characters")
    if (
        label != anchor.label
        or process_name.casefold() != anchor.process_name.casefold()
        or _normalized_slot(slot) != _normalized_slot(anchor.slot)
        or anchor.module.casefold()
        not in {module_key.casefold(), module_name.casefold()}
        or (module is not None and module.casefold() != anchor.module.casefold())
        or (anchor.pid is not None and pid != anchor.pid)
    ):
        raise ValueError("broker target order or identity differs from the resolved anchor")

    raw_path = target.get("log_path")
    if status in _DELIVERABLE_STATUSES:
        if not isinstance(raw_path, str):
            raise ValueError("deliverable broker target has no log_path")
        log_path = validate_relative_path(raw_path).as_posix()
        name = _archive_name(target)
    else:
        if "log_path" in target:
            raise ValueError("missing or ambiguous broker target cannot name a log_path")
        log_path = None
        name = None

    return AuthoritativeTargetLog(
        ordinal=ordinal,
        label=label,
        requested_module=anchor.module,
        requested_slot=anchor.slot,
        requested_process_name=anchor.process_name,
        requested_pid=anchor.pid,
        module_key=module_key,
        module_name=module_name,
        slot=_normalized_slot(slot),
        process_name=process_name,
        pid=pid,
        match_status=status,
        board_cycle=board_cycle,
        cpu_id=cpu_id,
        cpu_cycle=cpu_cycle,
        caveats=tuple(caveats),
        source_kind=source_kind,
        source_ref=source_ref,
        source_root=source_root,
        log_path=log_path,
        archive_name=name,
    )


def validated_successful_broker_record(
    broker_audit_bytes: bytes,
    *,
    job_id: str,
) -> dict[str, Any]:
    """Parse one product-owned broker audit and return its unique success."""

    audit = parse_canonical_json_bytes(broker_audit_bytes)
    if (
        not isinstance(audit, dict)
        or set(audit) != {"schema_version", "job_id", "operations"}
        or audit.get("schema_version") != 1
        or audit.get("job_id") != job_id
        or not isinstance(audit.get("operations"), list)
        or len(audit["operations"]) > 8
    ):
        raise ValueError("broker audit shape or identity is invalid")

    successful: list[dict[str, Any]] = []
    expected_record_fields = {
        "operation",
        "request_sha256",
        "request",
        "http_status",
        "result_sha256",
        "result",
    }
    for record in audit["operations"]:
        if not isinstance(record, dict) or set(record) != expected_record_fields:
            raise ValueError("broker operation audit shape is invalid")
        operation = record.get("operation")
        request_value = record.get("request")
        result_value = record.get("result")
        status = record.get("http_status")
        if (
            operation not in {"parse-targets", "target-logs"}
            or not isinstance(request_value, dict)
            or not isinstance(result_value, dict)
            or not isinstance(status, int)
            or isinstance(status, bool)
            or record.get("request_sha256")
            != hashlib.sha256(canonical_json_bytes(request_value)).hexdigest()
            or record.get("result_sha256")
            != hashlib.sha256(canonical_json_bytes(result_value)).hexdigest()
        ):
            raise ValueError("broker operation audit content is invalid")
        if status == 200:
            successful.append(record)
    if len(successful) != 1:
        raise ValueError("broker audit must contain exactly one successful target operation")
    return successful[0]


def resolve_authoritative_targets(
    manifest: WorkspaceInputManifest,
    broker_audit_bytes: bytes,
) -> AuthoritativeTargetSet:
    """Resolve all target statuses from one unique successful broker operation."""

    if not isinstance(manifest, WorkspaceInputManifest):
        raise TypeError("manifest must be a WorkspaceInputManifest")
    plan = manifest.resolved_logparse_plan
    if plan is None:
        raise ValueError("workspace has no resolved Logparse plan")

    successful = validated_successful_broker_record(
        broker_audit_bytes,
        job_id=manifest.job_id,
    )
    operation = successful["operation"]
    request_value = successful["request"]
    result_value = successful["result"]
    expected_result_fields = {"schema_version", "api_version", "target_logs"}
    if plan.attachment_id is not None:
        expected_result_fields.add("logparse_run_artifact_draft")
    if (
        set(result_value) != expected_result_fields
        or result_value.get("schema_version") != 1
        or result_value.get("api_version") != 1
        or not isinstance(result_value.get("target_logs"), list)
        or len(result_value["target_logs"]) != len(plan.anchors)
    ):
        raise ValueError("successful broker target result shape is invalid")
    source_kind, source_ref, source_root, source_size, source_sha256 = (
        _source_from_success(
            manifest=manifest,
            operation=operation,
            request_value=request_value,
            result_value=result_value,
        )
    )
    targets = tuple(
        _target_for_anchor(
            target,
            ordinal=index,
            anchor=anchor,
            source_kind=source_kind,
            source_ref=source_ref,
            source_root=source_root,
        )
        for index, (anchor, target) in enumerate(
            zip(plan.anchors, result_value["target_logs"], strict=True),
            start=1,
        )
    )

    source_paths: set[str] = set()
    archive_names: set[str] = set()
    for target in targets:
        if not target.deliverable:
            continue
        assert target.workspace_relative_path is not None
        assert target.archive_name is not None
        source_key = target.workspace_relative_path.casefold()
        name_key = target.archive_name.casefold()
        if source_key in source_paths:
            raise ValueError("one target log path is bound to multiple resolved anchors")
        if name_key in archive_names:
            raise ValueError("semantic target log archive names collide case-insensitively")
        source_paths.add(source_key)
        archive_names.add(name_key)
    return AuthoritativeTargetSet(
        problem_time=str(plan.problem_time),
        targets=targets,
        source_size=source_size,
        source_sha256=source_sha256,
    )


__all__ = [
    "AuthoritativeTargetLog",
    "AuthoritativeTargetSet",
    "empty_authoritative_targets",
    "resolve_authoritative_targets",
    "semantic_archive_name",
    "validated_successful_broker_record",
]
