"""Two-phase quarantine primitives for the S02 retention cleaner."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

from pydantic import TypeAdapter

from problem_locator.contracts import OpaqueId

from .atomic import FileSync, Replacer, is_reparse_point, require_real_directory
from .layout import StorageLayout
from .paths import ensure_no_symlink_ancestors


_OPAQUE_ID_ADAPTER = TypeAdapter(OpaqueId)


class _CoordinationLock(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def held_by_current_thread(self) -> bool: ...


class QuarantineMover:
    """Atomically isolate exact cleanup candidates after an in-lock recheck."""

    @staticmethod
    def _is_candidate_shape(parts: tuple[str, ...]) -> bool:
        return (
            len(parts) == 3 and parts[:2] == ("tmp", "uploads")
            or len(parts) == 4 and parts[:2] == ("tmp", "proposals")
            or len(parts) == 3 and parts[:2] == ("tmp", "workspaces")
            or len(parts) == 3 and parts[:2] == ("tmp", "state")
            or len(parts) == 6 and parts[:2] == ("resources", "cases")
            or len(parts) == 2 and parts[0] == "jobs"
        )

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

    def _candidate_relative_path(self, candidate: Path) -> Path:
        root = Path(os.path.abspath(self._layout.data_root))
        absolute = Path(os.path.abspath(candidate))
        try:
            relative = absolute.relative_to(root)
        except ValueError as exc:
            raise ValueError("cleanup candidate escapes DATA_ROOT") from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("cleanup candidate has an unsafe relative path")

        parts = relative.parts
        if not self._is_candidate_shape(parts):
            raise ValueError("path is not an exact S02 cleanup candidate")
        ensure_no_symlink_ancestors(self._layout.data_root, absolute)
        return relative

    @staticmethod
    def _require_candidate_node(candidate: Path) -> os.stat_result:
        metadata = candidate.lstat()
        if is_reparse_point(metadata) or not (
            stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
        ):
            raise ValueError("cleanup candidate must be a real file or directory")
        return metadata

    def _ensure_directory_chain(self, destination_parent: Path) -> None:
        quarantine_root = self._layout.quarantine
        require_real_directory(quarantine_root)
        relative = destination_parent.relative_to(quarantine_root)
        current = quarantine_root
        for part in relative.parts:
            child = current / part
            try:
                require_real_directory(child)
            except FileNotFoundError:
                child.mkdir(mode=0o700)
                require_real_directory(child)
            self._file_sync.sync_directory(current)
            current = child

    def move_if(
        self,
        cleanup_id: OpaqueId,
        candidate: Path,
        revalidate: Callable[[], bool],
    ) -> Path | None:
        """Revalidate and move one candidate without releasing the shared lock."""

        validated_cleanup_id = _OPAQUE_ID_ADAPTER.validate_python(cleanup_id)
        if not callable(revalidate):
            raise TypeError("cleanup revalidation must be callable")
        candidate = Path(os.path.abspath(candidate))

        with self._coordination_lock:
            relative = self._candidate_relative_path(candidate)
            destination = self._layout.quarantine / validated_cleanup_id / relative
            destination_present = destination.exists() or destination.is_symlink()
            candidate_present = candidate.exists() or candidate.is_symlink()
            if not candidate_present:
                if not destination_present:
                    raise FileNotFoundError(candidate)
                ensure_no_symlink_ancestors(self._layout.quarantine, destination)
                self._require_candidate_node(destination)
                self._ensure_directory_chain(destination.parent)
                self._file_sync.sync_directory(candidate.parent)
                self._file_sync.sync_directory(destination.parent)
                return destination
            if not revalidate():
                return None
            candidate_metadata = self._require_candidate_node(candidate)
            self._ensure_directory_chain(destination.parent)
            if destination_present:
                raise FileExistsError("quarantine destination already exists")
            original_mode: int | None = None
            if stat.S_ISDIR(candidate_metadata.st_mode) and not (
                candidate_metadata.st_mode & stat.S_IWUSR
            ):
                # macOS refuses to rename a read-only directory even when its
                # parent is writable.  Make only the orphan's top-level node
                # owner-writable while the coordination lock excludes adoption.
                original_mode = stat.S_IMODE(candidate_metadata.st_mode)
                os.chmod(
                    candidate,
                    original_mode | stat.S_IWUSR,
                    follow_symlinks=False,
                )
            try:
                self._replacer.replace(candidate, destination)
            except BaseException:
                if original_mode is not None and candidate.exists():
                    os.chmod(candidate, original_mode, follow_symlinks=False)
                raise
            self._file_sync.sync_directory(candidate.parent)
            self._file_sync.sync_directory(destination.parent)
            return destination

    def discover(self) -> tuple[Path, ...]:
        """Enumerate exact isolated candidates left by earlier cleanup cycles."""

        require_real_directory(self._layout.quarantine)
        discovered: list[Path] = []
        for cleanup_entry in sorted(
            os.scandir(self._layout.quarantine),
            key=lambda item: item.name,
        ):
            _OPAQUE_ID_ADAPTER.validate_python(cleanup_entry.name)
            cleanup_metadata = cleanup_entry.stat(follow_symlinks=False)
            if not stat.S_ISDIR(cleanup_metadata.st_mode) or is_reparse_point(
                cleanup_metadata
            ):
                raise ValueError("quarantine cleanup root must be a real directory")
            cleanup_root = Path(cleanup_entry.path)
            pending = [cleanup_root]
            while pending:
                current = pending.pop()
                for entry in sorted(os.scandir(current), key=lambda item: item.name):
                    path = Path(entry.path)
                    relative = path.relative_to(cleanup_root)
                    metadata = entry.stat(follow_symlinks=False)
                    if self._is_candidate_shape(relative.parts):
                        if is_reparse_point(metadata) or not (
                            stat.S_ISREG(metadata.st_mode)
                            or stat.S_ISDIR(metadata.st_mode)
                        ):
                            raise ValueError("quarantine candidate has an unsafe node type")
                        discovered.append(path)
                        continue
                    if stat.S_ISDIR(metadata.st_mode) and not is_reparse_point(metadata):
                        pending.append(path)
                        continue
                    raise ValueError("quarantine contains an unrecognized isolated path")
        return tuple(sorted(discovered))

    def delete(self, quarantined_path: Path) -> None:
        """Delete one already-isolated node while no coordination lock is held."""

        if self._coordination_lock.held_by_current_thread():
            raise RuntimeError("quarantine deletion must run outside the coordination lock")
        quarantine_root = Path(os.path.abspath(self._layout.quarantine))
        target = Path(os.path.abspath(quarantined_path))
        try:
            relative = target.relative_to(quarantine_root)
        except ValueError as exc:
            raise ValueError("delete target escapes the quarantine root") from exc
        if len(relative.parts) < 2 or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise ValueError("delete target is not one isolated cleanup candidate")
        _OPAQUE_ID_ADAPTER.validate_python(relative.parts[0])
        if not self._is_candidate_shape(relative.parts[1:]):
            raise ValueError("delete target is not one isolated cleanup candidate")
        # Deletion is deliberately outside the coordination lock, so never
        # follow an adopted/replaced link anywhere under the quarantine root.
        ensure_no_symlink_ancestors(quarantine_root, target)
        try:
            target_metadata = target.lstat()
        except FileNotFoundError:
            self._complete_parent_deletion(target.parent, quarantine_root)
            return
        if is_reparse_point(target_metadata):
            raise ValueError("quarantine delete target must not be a reparse point")
        if stat.S_ISREG(target_metadata.st_mode):
            try:
                target.unlink()
            except PermissionError:
                os.chmod(target, stat.S_IRUSR | stat.S_IWUSR, follow_symlinks=False)
                target.unlink()
        elif stat.S_ISDIR(target_metadata.st_mode):
            for current, directory_names, _ in os.walk(
                target,
                topdown=True,
                followlinks=False,
            ):
                os.chmod(current, stat.S_IRWXU, follow_symlinks=False)
                current_path = Path(current)
                for directory_name in directory_names:
                    child = current_path / directory_name
                    child_metadata = child.lstat()
                    if stat.S_ISDIR(child_metadata.st_mode) and not is_reparse_point(
                        child_metadata
                    ):
                        os.chmod(child, stat.S_IRWXU, follow_symlinks=False)

            def make_writable_and_retry(
                function: Callable[[str], object],
                path: str,
                error: BaseException,
            ) -> None:
                if not isinstance(error, PermissionError):
                    raise error
                parent = Path(path).parent
                if parent != Path(path):
                    os.chmod(parent, stat.S_IRWXU, follow_symlinks=False)
                os.chmod(path, stat.S_IRWXU, follow_symlinks=False)
                function(path)

            shutil.rmtree(target, onexc=make_writable_and_retry)
        else:
            raise FileNotFoundError(target)

        self._complete_parent_deletion(target.parent, quarantine_root)

    def _complete_parent_deletion(
        self,
        target_parent: Path,
        quarantine_root: Path,
    ) -> None:
        """Idempotently persist a deletion and prune its empty receipt path."""

        existing_parent = target_parent
        while existing_parent != quarantine_root and not existing_parent.exists():
            existing_parent = existing_parent.parent
        require_real_directory(existing_parent)
        self._file_sync.sync_directory(existing_parent)

        parent = target_parent
        while parent != quarantine_root:
            try:
                parent.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                break
            self._file_sync.sync_directory(parent.parent)
            parent = parent.parent


__all__ = ["QuarantineMover"]
