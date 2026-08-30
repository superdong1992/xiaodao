"""Physical formal-resource operations below the r3 ResourceStore adapter."""

from __future__ import annotations

import os
import re
import shutil
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self, Sequence

from pydantic import TypeAdapter

from problem_locator.contracts import (
    AttachmentFilenameSuffix,
    BinaryStream,
    OpaqueId,
    ResourceKind,
    ResourceRef,
    Sha256,
    TreeManifest,
    workspace_attachment_relative_path,
)
from problem_locator.contracts.limits import MAX_CASE_RESOURCE_BYTES

from .atomic import (
    FileSync,
    Replacer,
    finalize_read_only_file,
    finalize_read_only_tree,
    is_reparse_point,
    is_read_only,
    require_ordinary_file,
    require_real_directory,
)
from .layout import StorageLayout
from .paths import ensure_no_symlink_ancestors, parse_storage_key, resource_path
from .platform import PlatformReplaceOperation, chmod_no_follow
from .streams import FileBinaryStream, copy_binary_stream, hash_file
from .tree import TreeInspection, inspect_tree, verify_tree


_OPAQUE_ID_ADAPTER = TypeAdapter(OpaqueId)
_SHA256_ADAPTER = TypeAdapter(Sha256)
_PROPOSAL_DIRECTORY_PATTERN = re.compile(r"^p-[0-9a-f]{64}$")
_TEMP_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LOGPARSE_PREPROCESS_WORKSPACE_SUFFIX = ".logparse-preprocess"


def _validate_workspace_segment(segment: str) -> None:
    """Accept only a Job UUID or its one product-owned Pass A workspace."""

    job_id = (
        segment[: -len(_LOGPARSE_PREPROCESS_WORKSPACE_SUFFIX)]
        if segment.endswith(_LOGPARSE_PREPROCESS_WORKSPACE_SUFFIX)
        else segment
    )
    _OPAQUE_ID_ADAPTER.validate_python(job_id)
    if segment not in {
        job_id,
        f"{job_id}{_LOGPARSE_PREPROCESS_WORKSPACE_SUFFIX}",
    }:
        raise ValueError("workspace segment is not a fixed product workspace identity")


class _CoordinationLock(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def publication_held_by_current_thread(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class _ObservedFormalResource:
    storage_key: str
    resource_kind: ResourceKind
    size: int
    sha256: str
    path: Path


def _inspect_physical_resource(
    path: Path,
    resource_kind: ResourceKind,
) -> tuple[int, str, TreeInspection | None]:
    if resource_kind is ResourceKind.FILE:
        require_ordinary_file(path)
        observation = hash_file(path)
        return observation.size, observation.sha256, None
    require_real_directory(path)
    tree = inspect_tree(path, reject_hardlinks=True)
    return tree.size, tree.sha256, tree


def _require_tree_read_only(root: Path) -> None:
    for current, directory_names, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        require_real_directory(current_path)
        if not is_read_only(current_path):
            raise ValueError("formal resource directory is not read-only")
        for directory_name in directory_names:
            directory = current_path / directory_name
            require_real_directory(directory)
            if not is_read_only(directory):
                raise ValueError("formal resource directory is not read-only")
        for filename in filenames:
            file_path = current_path / filename
            require_ordinary_file(file_path)
            if not is_read_only(file_path):
                raise ValueError("formal resource file is not read-only")


def validate_formal_resource(
    data_root: Path,
    resource_ref: ResourceRef,
    *,
    require_read_only: bool,
) -> Path:
    """Validate one public ResourceRef against its complete physical bytes."""

    path = resource_path(data_root, resource_ref)
    size, sha256, _ = _inspect_physical_resource(path, resource_ref.resource_kind)
    if size != resource_ref.size:
        raise ValueError("formal resource size does not match ResourceRef")
    if sha256 != resource_ref.sha256:
        raise ValueError("formal resource hash does not match ResourceRef")
    if require_read_only:
        if resource_ref.resource_kind is ResourceKind.FILE:
            if not is_read_only(path):
                raise ValueError("formal resource file is not read-only")
        else:
            _require_tree_read_only(path)
    return path


def scan_case_resources(
    layout: StorageLayout,
    case_id: OpaqueId,
) -> dict[str, _ObservedFormalResource]:
    """Strictly enumerate every formal target for one Case, including orphans."""

    validated_case_id = _OPAQUE_ID_ADAPTER.validate_python(case_id)
    case_root = layout.cases_resources / validated_case_id
    try:
        require_real_directory(case_root)
    except FileNotFoundError:
        return {}
    observations: dict[str, _ObservedFormalResource] = {}
    expected_categories = {"attachments", "evidence", "artifacts"}
    for category_entry in sorted(os.scandir(case_root), key=lambda item: item.name):
        category_metadata = category_entry.stat(follow_symlinks=False)
        if (
            category_entry.name not in expected_categories
            or not stat.S_ISDIR(category_metadata.st_mode)
            or is_reparse_point(category_metadata)
        ):
            raise ValueError("Case resources contain an invalid category node")
        category = category_entry.name
        category_path = Path(category_entry.path)
        for resource_entry in sorted(
            os.scandir(category_path),
            key=lambda item: item.name,
        ):
            _OPAQUE_ID_ADAPTER.validate_python(resource_entry.name)
            resource_metadata = resource_entry.stat(follow_symlinks=False)
            if not stat.S_ISDIR(resource_metadata.st_mode) or is_reparse_point(
                resource_metadata
            ):
                raise ValueError("formal resource ID node must be a real directory")
            resource_id_path = Path(resource_entry.path)
            leaves = sorted(os.scandir(resource_id_path), key=lambda item: item.name)
            if not leaves:
                continue
            if len(leaves) != 1:
                raise ValueError("formal resource ID node must have exactly one leaf")
            leaf = leaves[0]
            if category == "attachments" and leaf.name != "payload":
                raise ValueError("attachment resource leaf must be payload")
            if category in {"evidence", "artifacts"} and leaf.name not in {
                "payload",
                "tree",
            }:
                raise ValueError("evidence/artifact resource leaf must be payload or tree")
            kind = ResourceKind.DIRECTORY if leaf.name == "tree" else ResourceKind.FILE
            storage_key = (
                f"resources/cases/{validated_case_id}/{category}/"
                f"{resource_entry.name}/{leaf.name}"
            )
            path = Path(leaf.path)
            size, sha256, _ = _inspect_physical_resource(path, kind)
            observations[storage_key] = _ObservedFormalResource(
                storage_key=storage_key,
                resource_kind=kind,
                size=size,
                sha256=sha256,
                path=path,
            )
    return observations


def scan_all_resources(layout: StorageLayout) -> dict[str, _ObservedFormalResource]:
    """Strictly enumerate formal resources for startup/global validation."""

    require_real_directory(layout.cases_resources)
    result: dict[str, _ObservedFormalResource] = {}
    for case_entry in sorted(
        os.scandir(layout.cases_resources),
        key=lambda item: item.name,
    ):
        _OPAQUE_ID_ADAPTER.validate_python(case_entry.name)
        metadata = case_entry.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or is_reparse_point(metadata):
            raise ValueError("formal Case resource root must be a real directory")
        result.update(scan_case_resources(layout, case_entry.name))
    return result


def calculate_case_usage(
    layout: StorageLayout,
    case_id: OpaqueId,
    planned_targets: Sequence[tuple[str, ResourceKind, int, str]],
) -> tuple[int, int, int]:
    """Return current/new/total bytes using unique formal storage keys."""

    validated_case_id = _OPAQUE_ID_ADAPTER.validate_python(case_id)
    existing = scan_case_resources(layout, validated_case_id)
    planned_by_key: dict[str, tuple[ResourceKind, int, str]] = {}
    for storage_key, resource_kind, size, sha256 in planned_targets:
        address = parse_storage_key(storage_key)
        if address.case_id != validated_case_id:
            raise ValueError("planned resource target belongs to another Case")
        if address.resource_kind is not resource_kind:
            raise ValueError("planned resource kind does not match its storage key")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("planned resource size must be a non-negative integer")
        validated_sha256 = _SHA256_ADAPTER.validate_python(sha256)
        value = (resource_kind, size, validated_sha256)
        previous = planned_by_key.setdefault(storage_key, value)
        if previous != value:
            raise ValueError("one formal target has conflicting planned content")

    current_bytes = sum(resource.size for resource in existing.values())
    new_bytes = 0
    for storage_key, (kind, size, sha256) in planned_by_key.items():
        present = existing.get(storage_key)
        if present is None:
            new_bytes += size
            continue
        if (
            present.resource_kind is not kind
            or present.size != size
            or present.sha256 != sha256
        ):
            raise ValueError("formal target exists with different content")
    total_bytes = current_bytes + new_bytes
    if total_bytes > MAX_CASE_RESOURCE_BYTES:
        raise OverflowError("Case formal resource capacity would be exceeded")
    return current_bytes, new_bytes, total_bytes


class FormalResourcePublisher:
    """Publish/adopt one already validated staged object under the shared lock."""

    def __init__(
        self,
        layout: StorageLayout,
        coordination_lock: _CoordinationLock,
        file_sync: FileSync,
        replacer: Replacer,
    ) -> None:
        self._layout = layout
        self._coordination_lock = coordination_lock
        self._file_sync = file_sync
        self._replacer = replacer

    def _ensure_formal_parent(self, final_path: Path) -> None:
        require_real_directory(self._layout.resources)
        relative_parent = final_path.parent.relative_to(self._layout.resources)
        current = self._layout.resources
        for part in relative_parent.parts:
            child = current / part
            try:
                require_real_directory(child)
            except FileNotFoundError:
                child.mkdir(mode=0o700)
                require_real_directory(child)
            # Idempotently close the durability boundary even when adopting a
            # directory created by an earlier attempt whose parent sync failed.
            self._file_sync.sync_directory(current)
            current = child

    def _validated_stage_path(
        self,
        staged_content_path: Path,
        expected_kind: ResourceKind,
    ) -> Path:
        path = Path(os.path.abspath(staged_content_path))
        root = Path(os.path.abspath(self._layout.data_root))
        try:
            parts = path.relative_to(root).parts
        except ValueError as exc:
            raise ValueError("staged content escapes DATA_ROOT") from exc
        valid_upload = (
            len(parts) == 4
            and parts[:2] == ("tmp", "uploads")
            and parts[3] == "payload"
        )
        valid_proposal = (
            len(parts) == 5
            and parts[:2] == ("tmp", "proposals")
            and _PROPOSAL_DIRECTORY_PATTERN.fullmatch(parts[3]) is not None
            and parts[4] in {"payload", "tree"}
        )
        if not (valid_upload or valid_proposal):
            raise ValueError("path is not completed S02 staged content")
        _OPAQUE_ID_ADAPTER.validate_python(parts[2])
        observed_kind = (
            ResourceKind.DIRECTORY if parts[-1] == "tree" else ResourceKind.FILE
        )
        if observed_kind is not expected_kind:
            raise ValueError("staged content kind does not match the planned target")
        ensure_no_symlink_ancestors(self._layout.temporary, path)
        require_ordinary_file(path.parent / "staged.json")
        return path

    @staticmethod
    def _validate_expected_content(
        path: Path,
        expected_kind: ResourceKind,
        expected_size: int,
        expected_sha256: str,
        expected_tree_manifest: TreeManifest | None,
    ) -> None:
        if expected_kind is ResourceKind.DIRECTORY:
            if expected_tree_manifest is None:
                raise ValueError("directory publication requires its TreeManifest")
            verify_tree(
                path,
                expected_manifest=expected_tree_manifest,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
            return
        if expected_tree_manifest is not None:
            raise ValueError("file publication cannot carry a TreeManifest")
        observed = hash_file(path)
        if observed.size != expected_size:
            raise ValueError("resource size does not match its staged reference")
        if observed.sha256 != expected_sha256:
            raise ValueError("resource hash does not match its staged reference")

    def publish(
        self,
        staged_content_path: Path,
        final_storage_key: str,
        *,
        expected_kind: ResourceKind,
        expected_size: int,
        expected_sha256: str,
        expected_tree_manifest: TreeManifest | None,
    ) -> _ObservedFormalResource:
        address = parse_storage_key(final_storage_key)
        if address.resource_kind is not expected_kind:
            raise ValueError("formal target kind does not match the staged reference")
        _SHA256_ADAPTER.validate_python(expected_sha256)
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            raise ValueError("resource size must be a non-negative integer")
        final_path = self._layout.data_root / Path(final_storage_key)

        with self._coordination_lock:
            if not self._coordination_lock.publication_held_by_current_thread():
                raise RuntimeError("formal publication requires the publication lease")
            ensure_no_symlink_ancestors(self._layout.resources, final_path)
            try:
                final_metadata = final_path.lstat()
            except FileNotFoundError:
                final_metadata = None
            if final_metadata is None:
                staged_path = self._validated_stage_path(
                    staged_content_path,
                    expected_kind,
                )
                self._validate_expected_content(
                    staged_path,
                    expected_kind,
                    expected_size,
                    expected_sha256,
                    expected_tree_manifest,
                )
                self._ensure_formal_parent(final_path)
                if staged_path.stat(follow_symlinks=False).st_dev != final_path.parent.stat(
                    follow_symlinks=False
                ).st_dev:
                    raise OSError("staged and formal resource paths must share a volume")
                self._replacer.replace(staged_path, final_path)
            self._validate_expected_content(
                final_path,
                expected_kind,
                expected_size,
                expected_sha256,
                expected_tree_manifest,
            )
            if expected_kind is ResourceKind.FILE:
                finalize_read_only_file(final_path, self._file_sync)
            else:
                finalize_read_only_tree(final_path, self._file_sync)
            self._file_sync.sync_directory(final_path.parent)
            self._validate_expected_content(
                final_path,
                expected_kind,
                expected_size,
                expected_sha256,
                expected_tree_manifest,
            )
            return _ObservedFormalResource(
                storage_key=final_storage_key,
                resource_kind=expected_kind,
                size=expected_size,
                sha256=expected_sha256,
                path=final_path,
            )


class FormalResourceReader:
    """Strict reads and read-only workspace materialization."""

    def __init__(
        self,
        layout: StorageLayout,
        file_sync: FileSync,
        replacer: Replacer | None = None,
        *,
        temp_token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._layout = layout
        self._file_sync = file_sync
        self._replacer = replacer or PlatformReplaceOperation()
        self._temp_token_factory = temp_token_factory or (lambda: uuid.uuid4().hex)

    def open_file(self, resource_ref: ResourceRef) -> BinaryStream:
        if resource_ref.resource_kind is not ResourceKind.FILE:
            raise ValueError("directory resources cannot be opened as byte streams")
        path = validate_formal_resource(
            self._layout.data_root,
            resource_ref,
            require_read_only=True,
        )
        return FileBinaryStream(path)

    def _validate_destination(self, resource_ref: ResourceRef, destination: Path) -> Path:
        lexical_destination = Path(destination)
        if ".." in lexical_destination.parts:
            raise ValueError("materialization destination contains path traversal")
        destination = Path(os.path.abspath(lexical_destination))
        root = Path(os.path.abspath(self._layout.data_root))
        try:
            parts = destination.relative_to(root).parts
        except ValueError as exc:
            raise ValueError("materialization destination escapes DATA_ROOT") from exc
        if (
            len(parts) != 7
            or parts[:2] != ("tmp", "workspaces")
            or parts[3] != "inputs"
        ):
            raise ValueError("destination is not the frozen workspace input path")
        _validate_workspace_segment(parts[2])

        address = parse_storage_key(resource_ref.storage_key)
        workspace_relative_path = "/".join(parts[3:])
        if address.category == "attachments":
            if (
                resource_ref.resource_kind is not ResourceKind.FILE
                or address.leaf != "payload"
            ):
                raise ValueError("Attachment materialization requires a FILE payload")
            allowed_paths = {
                workspace_attachment_relative_path(address.resource_id, suffix)
                for suffix in (None, *tuple(AttachmentFilenameSuffix))
            }
            if workspace_relative_path not in allowed_paths:
                raise ValueError(
                    "destination is not the frozen attachment workspace path"
                )
        elif parts[4:] != (address.category, address.resource_id, address.leaf):
            # Evidence and Artifact paths retain the formal payload/tree leaf;
            # DIRECTORY resources in particular are always an unsuffixed tree.
            raise ValueError("destination is not the frozen workspace input path")
        return destination

    def _ensure_materialization_parent(self, destination: Path) -> None:
        require_real_directory(self._layout.workspaces)
        relative = destination.parent.relative_to(self._layout.workspaces)
        current = self._layout.workspaces
        for part in relative.parts:
            child = current / part
            try:
                require_real_directory(child)
            except FileNotFoundError:
                child.mkdir(mode=0o700)
                require_real_directory(child)
            self._file_sync.sync_directory(current)
            current = child

    def _new_materialization_temp(self, destination: Path) -> Path:
        token = self._temp_token_factory()
        if not isinstance(token, str) or _TEMP_TOKEN_PATTERN.fullmatch(token) is None:
            raise ValueError("materialization temp token must be safe ASCII")
        return destination.parent / f".{destination.name}.{token}.materializing"

    @staticmethod
    def _discard_materialization_temp(path: Path) -> None:
        """Best-effort removal of one private, never-published temp node."""

        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if is_reparse_point(metadata) or stat.S_ISLNK(metadata.st_mode):
            return
        if stat.S_ISREG(metadata.st_mode):
            if os.name == "nt" and not bool(stat.S_IMODE(metadata.st_mode) & 0o222):
                chmod_no_follow(path, stat.S_IRUSR | stat.S_IWUSR)
            path.unlink()
            return
        if not stat.S_ISDIR(metadata.st_mode):
            return
        for current, directory_names, filenames in os.walk(
            path,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            require_real_directory(current_path)
            for name in directory_names:
                require_real_directory(current_path / name)
            for name in filenames:
                require_ordinary_file(current_path / name)
        shutil.rmtree(path)

    def materialize(self, resource_ref: ResourceRef, destination: Path) -> Path:
        source = validate_formal_resource(
            self._layout.data_root,
            resource_ref,
            require_read_only=True,
        )
        destination = self._validate_destination(resource_ref, destination)
        self._ensure_materialization_parent(destination)
        ensure_no_symlink_ancestors(self._layout.workspaces, destination)

        source_tree: TreeInspection | None = None
        if resource_ref.resource_kind is ResourceKind.DIRECTORY:
            source_tree = inspect_tree(source, reject_hardlinks=True)
            if (
                source_tree.size != resource_ref.size
                or source_tree.sha256 != resource_ref.sha256
            ):
                raise ValueError("formal directory bytes changed before materialization")

        try:
            destination.lstat()
        except FileNotFoundError:
            destination_exists = False
        else:
            destination_exists = True

        if not destination_exists:
            if resource_ref.resource_kind is ResourceKind.FILE:
                temporary = self._new_materialization_temp(destination)
                try:
                    with FileBinaryStream(source) as stream:
                        copy_binary_stream(
                            stream,
                            temporary,
                            file_sync=self._file_sync,
                            byte_limit=resource_ref.size,
                        )
                    observed = hash_file(temporary)
                    if (
                        observed.size != resource_ref.size
                        or observed.sha256 != resource_ref.sha256
                    ):
                        raise OSError(
                            "temporary materialized bytes differ from ResourceRef"
                        )
                    finalize_read_only_file(temporary, self._file_sync)
                    self._replacer.replace(temporary, destination)
                finally:
                    try:
                        self._discard_materialization_temp(temporary)
                    except (OSError, ValueError):
                        pass
            else:
                assert source_tree is not None
                temporary = self._new_materialization_temp(destination)
                try:
                    inspect_tree(
                        source,
                        copy_to=temporary,
                        byte_limit=resource_ref.size,
                        reject_hardlinks=True,
                    )
                    finalize_read_only_tree(temporary, self._file_sync)
                    verify_tree(
                        temporary,
                        expected_manifest=source_tree.manifest,
                        expected_size=resource_ref.size,
                        expected_sha256=resource_ref.sha256,
                    )
                    self._replacer.replace(temporary, destination)
                finally:
                    try:
                        self._discard_materialization_temp(temporary)
                    except (OSError, ValueError):
                        pass

        if resource_ref.resource_kind is ResourceKind.FILE:
            require_ordinary_file(destination)
            finalize_read_only_file(destination, self._file_sync)
            observed = hash_file(destination)
            if (
                observed.size != resource_ref.size
                or observed.sha256 != resource_ref.sha256
            ):
                raise OSError("materialized file bytes differ from ResourceRef")
        else:
            assert source_tree is not None
            require_real_directory(destination)
            finalize_read_only_tree(destination, self._file_sync)
            verify_tree(
                destination,
                expected_manifest=source_tree.manifest,
                expected_size=resource_ref.size,
                expected_sha256=resource_ref.sha256,
            )
        self._file_sync.sync_directory(destination.parent)
        validate_formal_resource(
            self._layout.data_root,
            resource_ref,
            require_read_only=True,
        )
        return destination


__all__ = [
    "FormalResourcePublisher",
    "FormalResourceReader",
    "calculate_case_usage",
    "scan_all_resources",
    "scan_case_resources",
    "validate_formal_resource",
]
