"""Durable whole-file replacement for the single V1 ``state.json``.

This module contains only the physical write boundary.  State DTO validation,
generation checks, mutation application, and typed Port errors belong to the
repository adapter and are intentionally kept out until the r2 contract shape
is available.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import TypeAdapter

from problem_locator.contracts import IdGenerator, OpaqueId

from .atomic import (
    FileSync,
    Replacer,
    read_stable_file_bytes,
    require_real_directory,
    write_synced_file,
)
from .layout import StorageLayout


_OPAQUE_ID_ADAPTER = TypeAdapter(OpaqueId)


class AtomicStateFileWriter:
    """Apply the prescribed temp/prev/current durability sequence.

    A failed call deliberately leaves any not-yet-replaced temporary file for
    the retention cleaner.  The repository is responsible for reloading the
    authoritative ``state.json`` after every failure before serving another
    operation.
    """

    def __init__(
        self,
        layout: StorageLayout,
        file_sync: FileSync,
        replacer: Replacer,
        id_generator: IdGenerator,
        *,
        read_file: Callable[[Path], bytes] = read_stable_file_bytes,
    ) -> None:
        self._layout = layout
        self._file_sync = file_sync
        self._replacer = replacer
        self._id_generator = id_generator
        self._read_file = read_file

    def _new_temporary_path(self, purpose: str) -> Path:
        token = _OPAQUE_ID_ADAPTER.validate_python(self._id_generator.new(purpose))
        return self._layout.state_temporary / f"{purpose}-{token}.tmp"

    def _require_same_volume(self, path: Path) -> None:
        if path.stat(follow_symlinks=False).st_dev != self._layout.data_root.stat(
            follow_symlinks=False
        ).st_dev:
            raise OSError("state temporary file must share DATA_ROOT's volume")

    def write(self, canonical_bytes: bytes) -> bytes:
        """Replace current state and return the verified final bytes."""

        if not isinstance(canonical_bytes, bytes):
            raise TypeError("state payload must be immutable bytes")
        require_real_directory(self._layout.data_root)
        require_real_directory(self._layout.state_temporary)

        next_temporary = self._new_temporary_path("state-next")
        write_synced_file(next_temporary, canonical_bytes, self._file_sync)
        self._require_same_volume(next_temporary)

        try:
            current_bytes = self._read_file(self._layout.state)
        except FileNotFoundError:
            current_bytes = None

        if current_bytes is not None:
            previous_temporary = self._new_temporary_path("state-prev")
            write_synced_file(previous_temporary, current_bytes, self._file_sync)
            self._require_same_volume(previous_temporary)
            self._replacer.replace(previous_temporary, self._layout.previous_state)

        self._replacer.replace(next_temporary, self._layout.state)
        self._file_sync.sync_directory(self._layout.data_root)
        final_bytes = self._read_file(self._layout.state)
        if final_bytes != canonical_bytes:
            raise OSError("final state.json bytes differ from the committed payload")
        return final_bytes


__all__ = ["AtomicStateFileWriter"]
