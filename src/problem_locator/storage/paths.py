"""Filesystem layout and path-boundary checks for the S02 adapters.

Only frozen S00 identifiers and storage keys cross the public Ports.  The
helpers in this module keep their physical representation private and reject
links or path traversal before filesystem content is trusted.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from problem_locator.contracts.enums import ResourceKind
from problem_locator.contracts.models import OpaqueId, ResourceRef

from .atomic import is_reparse_point


_OPAQUE_ID = TypeAdapter(OpaqueId)


@dataclass(frozen=True, slots=True)
class StorageAddress:
    """Parsed form of a formal S00 storage key."""

    case_id: str
    category: str
    resource_id: str
    leaf: str

    @property
    def resource_kind(self) -> ResourceKind:
        return ResourceKind.DIRECTORY if self.leaf == "tree" else ResourceKind.FILE


def validate_data_root(data_root: Path) -> Path:
    """Return an absolute DATA_ROOT without silently accepting a relative path."""

    value = Path(data_root)
    if not value.is_absolute():
        raise ValueError("DATA_ROOT must be an explicit absolute path")
    return value


def ensure_within(root: Path, candidate: Path) -> Path:
    """Resolve *candidate* and prove it remains under *root*."""

    resolved_root = root.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("path escapes its permitted root") from exc
    return resolved_candidate


def ensure_no_symlink_ancestors(root: Path, candidate: Path) -> None:
    """Reject any existing symlink between *root* and *candidate* inclusive."""

    root = Path(os.path.abspath(root))
    absolute = Path(os.path.abspath(candidate))
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes its permitted root") from exc
    current = root
    try:
        root_metadata = current.lstat()
    except FileNotFoundError:
        root_metadata = None
    if root_metadata is not None and (
        stat.S_ISLNK(root_metadata.st_mode) or is_reparse_point(root_metadata)
    ):
        raise ValueError("filesystem roots cannot be links or reparse points")
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or is_reparse_point(metadata):
            raise ValueError("symbolic links are forbidden in storage paths")


def parse_storage_key(storage_key: str) -> StorageAddress:
    """Validate the exact V1 formal-resource key grammar."""

    if not isinstance(storage_key, str) or not storage_key:
        raise ValueError("storage_key must be a non-empty string")
    if storage_key.startswith("/") or "\\" in storage_key:
        raise ValueError("storage_key must be a relative POSIX path")
    parts = storage_key.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("storage_key contains an unsafe path segment")
    if len(parts) != 6 or parts[:2] != ["resources", "cases"]:
        raise ValueError("storage_key does not use the frozen resource layout")
    case_id, category, resource_id, leaf = parts[2], parts[3], parts[4], parts[5]
    _OPAQUE_ID.validate_python(case_id)
    _OPAQUE_ID.validate_python(resource_id)
    if category not in {"attachments", "evidence", "artifacts"}:
        raise ValueError("unknown formal resource category")
    if category == "attachments" and leaf != "payload":
        raise ValueError("attachments must use payload files")
    if category in {"evidence", "artifacts"} and leaf not in {"payload", "tree"}:
        raise ValueError("evidence and artifacts must use payload or tree")
    return StorageAddress(case_id, category, resource_id, leaf)


def formal_storage_key(
    case_id: str,
    category: str,
    resource_id: str,
    resource_kind: ResourceKind,
) -> str:
    """Construct a formal key from already allocated frozen identifiers."""

    _OPAQUE_ID.validate_python(case_id)
    _OPAQUE_ID.validate_python(resource_id)
    if category not in {"attachments", "evidence", "artifacts"}:
        raise ValueError("unknown formal resource category")
    if resource_kind is ResourceKind.DIRECTORY and category == "attachments":
        raise ValueError("attachments cannot be directory resources")
    leaf = "tree" if resource_kind is ResourceKind.DIRECTORY else "payload"
    return f"resources/cases/{case_id}/{category}/{resource_id}/{leaf}"


def resource_path(data_root: Path, resource_ref: ResourceRef) -> Path:
    """Resolve one ResourceRef to a safe formal path under DATA_ROOT/resources."""

    address = parse_storage_key(resource_ref.storage_key)
    if address.resource_kind is not resource_ref.resource_kind:
        raise ValueError("resource kind does not match storage_key leaf")
    resources_root = validate_data_root(data_root) / "resources"
    candidate = validate_data_root(data_root) / Path(resource_ref.storage_key)
    ensure_no_symlink_ancestors(resources_root, candidate)
    ensure_within(resources_root, candidate)
    return candidate


def proposal_directory_name(proposal_key: str) -> str:
    """Map an arbitrary frozen proposal key to one safe, stable path segment.

    S00 intentionally treats proposal keys as opaque non-empty text rather than
    path segments.  Hashing is therefore an internal representation detail; the
    exact original value remains authoritative in ``staged.json``.
    """

    if not isinstance(proposal_key, str) or not proposal_key or proposal_key.isspace():
        raise ValueError("proposal_key must be non-blank text")
    return "p-" + hashlib.sha256(proposal_key.encode("utf-8")).hexdigest()


def proposal_stage_path(data_root: Path, owner_job_id: str, proposal_key: str) -> Path:
    _OPAQUE_ID.validate_python(owner_job_id)
    return (
        validate_data_root(data_root)
        / "tmp"
        / "proposals"
        / owner_job_id
        / proposal_directory_name(proposal_key)
    )


def attachment_stage_path(data_root: Path, attachment_id: str) -> Path:
    _OPAQUE_ID.validate_python(attachment_id)
    return validate_data_root(data_root) / "tmp" / "uploads" / attachment_id


def job_directory(data_root: Path, job_id: str) -> Path:
    _OPAQUE_ID.validate_python(job_id)
    return validate_data_root(data_root) / "jobs" / job_id


__all__ = [
    "StorageAddress",
    "attachment_stage_path",
    "ensure_no_symlink_ancestors",
    "ensure_within",
    "formal_storage_key",
    "job_directory",
    "parse_storage_key",
    "proposal_directory_name",
    "proposal_stage_path",
    "resource_path",
    "validate_data_root",
]
