"""Content-only staging mechanics shared by the r3 ResourceStore adapter.

The public adapter constructs and validates the frozen staged-reference DTOs.
This module owns only streaming/copying, expected-byte checks, and the rule
that ``staged.json`` is the final atomic completion marker.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from pydantic import TypeAdapter

from problem_locator.contracts import BinaryStream, IdGenerator, OpaqueId

from .atomic import (
    FileSync,
    Replacer,
    atomic_write_bytes,
    is_reparse_point,
    read_stable_file_bytes,
    require_ordinary_file,
    require_real_directory,
)
from .layout import StorageLayout
from .streams import StreamCopyReceipt, copy_binary_stream, hash_file
from .tree import TreeInspection, inspect_tree, verify_tree


_OPAQUE_ID_ADAPTER = TypeAdapter(OpaqueId)
_PROPOSAL_DIRECTORY_PATTERN = re.compile(r"^p-[0-9a-f]{64}$")


class StagedObjectWriter:
    """Persist staged bytes without defining the public ResourceStore shape."""

    def __init__(
        self,
        layout: StorageLayout,
        file_sync: FileSync,
        replacer: Replacer,
        id_generator: IdGenerator,
    ) -> None:
        self._layout = layout
        self._file_sync = file_sync
        self._replacer = replacer
        self._id_generator = id_generator

    def _normalize_stage_directory(self, directory: Path) -> Path:
        root = Path(os.path.abspath(self._layout.data_root))
        absolute = Path(os.path.abspath(directory))
        try:
            parts = absolute.relative_to(root).parts
        except ValueError as exc:
            raise ValueError("staging directory escapes DATA_ROOT") from exc
        if len(parts) == 3 and parts[:2] == ("tmp", "uploads"):
            _OPAQUE_ID_ADAPTER.validate_python(parts[2])
        elif len(parts) == 4 and parts[:2] == ("tmp", "proposals"):
            _OPAQUE_ID_ADAPTER.validate_python(parts[2])
            if _PROPOSAL_DIRECTORY_PATTERN.fullmatch(parts[3]) is None:
                raise ValueError("proposal staging directory is not canonical")
        else:
            raise ValueError("path is not an exact S02 staging directory")
        return absolute

    def _ensure_directory(self, directory: Path) -> Path:
        directory = self._normalize_stage_directory(directory)
        if directory.is_relative_to(self._layout.uploads):
            first_missing = self._layout.uploads
        else:
            first_missing = self._layout.proposals
        require_real_directory(first_missing)
        relative = directory.relative_to(first_missing)
        current = first_missing
        for part in relative.parts:
            child = current / part
            try:
                require_real_directory(child)
            except FileNotFoundError:
                child.mkdir(mode=0o700)
                require_real_directory(child)
            # A retry must complete a parent sync that may have failed after
            # the directory entry itself was created by the prior attempt.
            self._file_sync.sync_directory(current)
            current = child
        return directory

    def _new_temporary_path(self, directory: Path, purpose: str) -> Path:
        token = _OPAQUE_ID_ADAPTER.validate_python(self._id_generator.new(purpose))
        return directory / f".{purpose}-{token}.tmp"

    @staticmethod
    def _reject_if_completed(directory: Path) -> None:
        marker = directory / "staged.json"
        try:
            require_ordinary_file(marker)
        except FileNotFoundError:
            return
        raise FileExistsError("staging directory already has a completion marker")

    @staticmethod
    def _reject_opposite_content(directory: Path, opposite_name: str) -> None:
        opposite = directory / opposite_name
        if opposite.exists() or opposite.is_symlink():
            raise ValueError("staging directory contains a conflicting resource kind")

    def stage_file_content(
        self,
        directory: Path,
        stream: BinaryStream,
        *,
        byte_limit: int,
        expected_size: int | None,
        expected_sha256: str | None,
    ) -> StreamCopyReceipt:
        """Consume one stream and atomically install its staged payload."""

        directory = self._ensure_directory(directory)
        self._reject_if_completed(directory)
        self._reject_opposite_content(directory, "tree")
        temporary = self._new_temporary_path(directory, "payload")
        receipt = copy_binary_stream(
            stream,
            temporary,
            file_sync=self._file_sync,
            byte_limit=byte_limit,
        )
        if expected_size is not None and receipt.size != expected_size:
            raise ValueError("staged resource size does not match its declaration")
        if expected_sha256 is not None and receipt.sha256 != expected_sha256:
            raise ValueError("staged resource hash does not match its declaration")

        payload = directory / "payload"
        try:
            require_ordinary_file(payload)
        except FileNotFoundError:
            pass
        self._replacer.replace(temporary, payload)
        self._file_sync.sync_directory(directory)
        observed = hash_file(payload)
        if observed != receipt:
            raise OSError("staged payload changed during atomic publication")
        return observed

    def _sync_tree(self, root: Path) -> None:
        directories: list[Path] = []
        for current, directory_names, filenames in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            require_real_directory(current_path)
            directories.append(current_path)
            for directory_name in directory_names:
                require_real_directory(current_path / directory_name)
            for filename in filenames:
                path = current_path / filename
                require_ordinary_file(path)
                self._file_sync.sync_file(path)
        for directory in sorted(
            directories,
            key=lambda value: len(value.relative_to(root).parts),
            reverse=True,
        ):
            self._file_sync.sync_directory(directory)

    def stage_tree_content(
        self,
        directory: Path,
        source_root: Path,
        *,
        byte_limit: int,
        expected_manifest_hash: str | None,
    ) -> TreeInspection:
        """Controlled-copy and atomically install one staged directory tree."""

        directory = self._ensure_directory(directory)
        self._reject_if_completed(directory)
        self._reject_opposite_content(directory, "payload")
        temporary = self._new_temporary_path(directory, "tree")
        inspection = inspect_tree(
            Path(source_root),
            copy_to=temporary,
            byte_limit=byte_limit,
            reject_hardlinks=True,
        )
        if (
            expected_manifest_hash is not None
            and inspection.sha256 != expected_manifest_hash
        ):
            raise ValueError("tree manifest hash does not match its declaration")
        self._sync_tree(temporary)

        tree = directory / "tree"
        try:
            tree_metadata = tree.lstat()
        except FileNotFoundError:
            tree_metadata = None
        if tree_metadata is not None:
            if not stat.S_ISDIR(tree_metadata.st_mode) or is_reparse_point(
                tree_metadata
            ):
                raise ValueError("existing staged tree is not a real directory")
            abandoned = self._new_temporary_path(directory, "tree-abandoned")
            self._replacer.replace(tree, abandoned)
        self._replacer.replace(temporary, tree)
        self._file_sync.sync_directory(directory)
        return verify_tree(
            tree,
            expected_manifest=inspection.manifest,
            expected_size=inspection.size,
            expected_sha256=inspection.sha256,
        )

    def publish_marker(self, directory: Path, canonical_bytes: bytes) -> None:
        """Publish the immutable completion marker after content is durable."""

        directory = self._normalize_stage_directory(directory)
        require_real_directory(directory)
        payload = directory / "payload"
        tree = directory / "tree"
        payload_present = payload.exists() or payload.is_symlink()
        tree_present = tree.exists() or tree.is_symlink()
        if payload_present == tree_present:
            raise ValueError("staging marker requires exactly one completed content node")
        if payload_present:
            require_ordinary_file(payload)
        else:
            require_real_directory(tree)
        marker = directory / "staged.json"
        try:
            existing = read_stable_file_bytes(marker)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing != canonical_bytes:
                raise FileExistsError("different staging marker already exists")
            self._file_sync.sync_file(marker)
            self._file_sync.sync_directory(directory)
            return

        temporary = self._new_temporary_path(directory, "staged-marker")
        atomic_write_bytes(
            marker,
            canonical_bytes,
            temporary_path=temporary,
            file_sync=self._file_sync,
            replacer=self._replacer,
        )
        if read_stable_file_bytes(marker) != canonical_bytes:
            raise OSError("staging completion marker changed during publication")

    def read_marker(self, directory: Path) -> bytes | None:
        """Read only the final marker; temporary markers are ignored."""

        directory = self._normalize_stage_directory(directory)
        try:
            require_real_directory(directory)
            return read_stable_file_bytes(directory / "staged.json")
        except FileNotFoundError:
            return None


__all__ = ["StagedObjectWriter"]
