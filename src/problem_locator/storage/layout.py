"""Fixed DATA_ROOT layout creation and empty-installation detection."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from problem_locator.contracts.limits import CONTRACT_REVISION, SCHEMA_VERSION

from .atomic import (
    FileSync,
    is_reparse_point,
    read_stable_file_bytes,
    require_real_directory,
    write_synced_file,
)
from .paths import validate_data_root


DATA_FORMAT_MARKER_BYTES = (
    json.dumps(
        {
            "contract_revision": CONTRACT_REVISION,
            "format_id": "problem-locator-data-v2",
            "schema_version": 2,
            "state_schema_version": SCHEMA_VERSION,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    + "\n"
).encode("utf-8")


class UnsupportedDataFormatError(ValueError):
    """The DATA_ROOT belongs to an installation outside this hard cut."""


_EMPTY = frozenset()


def _directory_has_only(directory: Path, allowed_names: frozenset[str]) -> bool:
    return all(entry.name in allowed_names for entry in directory.iterdir())


def _is_empty_fixed_layout(
    layout: "StorageLayout",
    *,
    allowed_root_files: frozenset[str],
) -> bool:
    """Recognize only the fixed empty layout plus explicitly allowed root files."""

    if not _directory_has_only(
        layout.data_root,
        frozenset({"resources", "jobs", "tmp"}) | allowed_root_files,
    ):
        return False
    if not _directory_has_only(layout.resources, frozenset({"cases"})):
        return False
    if not _directory_has_only(
        layout.temporary,
        frozenset({"uploads", "proposals", "workspaces", "quarantine", "state"}),
    ):
        return False
    return all(
        _directory_has_only(directory, _EMPTY)
        for directory in (
            layout.cases_resources,
            layout.jobs,
            layout.uploads,
            layout.proposals,
            layout.workspaces,
            layout.quarantine,
            layout.state_temporary,
        )
    )


@dataclass(frozen=True, slots=True)
class StorageLayout:
    data_root: Path

    @classmethod
    def at(cls, data_root: Path) -> StorageLayout:
        return cls(validate_data_root(Path(data_root)))

    @property
    def instance_lock(self) -> Path:
        return self.data_root / ".instance.lock"

    @property
    def state(self) -> Path:
        return self.data_root / "state.json"

    @property
    def data_format_marker(self) -> Path:
        return self.data_root / "data-format.json"

    @property
    def previous_state(self) -> Path:
        return self.data_root / "state.json.prev"

    @property
    def resources(self) -> Path:
        return self.data_root / "resources"

    @property
    def cases_resources(self) -> Path:
        return self.resources / "cases"

    @property
    def jobs(self) -> Path:
        return self.data_root / "jobs"

    @property
    def temporary(self) -> Path:
        return self.data_root / "tmp"

    @property
    def uploads(self) -> Path:
        return self.temporary / "uploads"

    @property
    def proposals(self) -> Path:
        return self.temporary / "proposals"

    @property
    def workspaces(self) -> Path:
        return self.temporary / "workspaces"

    @property
    def quarantine(self) -> Path:
        return self.temporary / "quarantine"

    @property
    def state_temporary(self) -> Path:
        return self.temporary / "state"

    def ensure_directories(self, file_sync: FileSync | None = None) -> None:
        """Create only the directories permitted by the frozen S02 layout."""

        if file_sync is None:
            from .platform import PlatformFileSync

            file_sync = PlatformFileSync()

        ordered = (
            self.data_root,
            self.resources,
            self.cases_resources,
            self.jobs,
            self.temporary,
            self.uploads,
            self.proposals,
            self.workspaces,
            self.quarantine,
            self.state_temporary,
        )
        for directory in ordered:
            require_real_directory(directory.parent)
            try:
                value = directory.lstat()
            except FileNotFoundError:
                directory.mkdir(mode=0o700)
                value = directory.lstat()
            if not stat.S_ISDIR(value.st_mode) or is_reparse_point(value):
                raise ValueError("DATA_ROOT layout nodes must be real directories")
            # Re-apply on adoption so a retry completes a create whose parent
            # sync failed after the directory entry became visible.
            file_sync.sync_directory(directory.parent)

    def has_business_content_without_state(self) -> bool:
        """Return whether initializing a new StateFile would hide existing data."""

        return not _is_empty_fixed_layout(
            self,
            allowed_root_files=frozenset(
                {self.instance_lock.name, self.data_format_marker.name}
            ),
        )

    def _validate_v2_marker(self, metadata: os.stat_result) -> None:
        marker = self.data_format_marker
        temporary = self.data_root / "data-format.json.tmp"
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or is_reparse_point(metadata)
            or metadata.st_size != len(DATA_FORMAT_MARKER_BYTES)
            or read_stable_file_bytes(marker) != DATA_FORMAT_MARKER_BYTES
            or temporary.exists()
            or temporary.is_symlink()
        ):
            raise ValueError("DATA_ROOT data-format marker is invalid")

    def validate_v2_data_format(self) -> None:
        """Read-only validation for an already initialized Result V2 root."""

        require_real_directory(self.data_root)
        try:
            metadata = self.data_format_marker.lstat()
        except FileNotFoundError as exc:
            raise UnsupportedDataFormatError(
                "DATA_ROOT is not marked for Result V2; configure a fresh DATA_ROOT"
            ) from exc
        self._validate_v2_marker(metadata)

    def initialize_v2_data_root(self, file_sync: FileSync | None = None) -> None:
        """Initialize only an absent/empty root, then create its fixed layout.

        Marker preflight deliberately happens before layout or instance-lock
        creation.  An unmarked non-empty root is therefore rejected without
        modifying any historical byte or directory entry.
        """

        if file_sync is None:
            from .platform import PlatformFileSync

            file_sync = PlatformFileSync()

        try:
            root_metadata = self.data_root.lstat()
        except FileNotFoundError:
            require_real_directory(self.data_root.parent)
            self.data_root.mkdir(mode=0o700)
            root_metadata = self.data_root.lstat()
            file_sync.sync_directory(self.data_root.parent)
        if not stat.S_ISDIR(root_metadata.st_mode) or is_reparse_point(root_metadata):
            raise ValueError("DATA_ROOT must be a real directory")

        marker = self.data_format_marker
        temporary = self.data_root / "data-format.json.tmp"
        try:
            marker_metadata = marker.lstat()
        except FileNotFoundError:
            try:
                next(self.data_root.iterdir())
            except StopIteration:
                pass
            else:
                raise UnsupportedDataFormatError(
                    "DATA_ROOT contains unmarked pre-V2 content; configure a fresh DATA_ROOT"
                )
        else:
            self._validate_v2_marker(marker_metadata)
            # Re-apply on retry when marker publication became visible but its
            # parent-directory sync failed.
            file_sync.sync_directory(self.data_root)
            self.ensure_directories(file_sync)
            return

        if temporary.exists() or temporary.is_symlink():
            raise ValueError("DATA_ROOT data-format marker staging file already exists")
        try:
            write_synced_file(temporary, DATA_FORMAT_MARKER_BYTES, file_sync)
            os.replace(temporary, marker)
            file_sync.sync_directory(self.data_root)
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        self.ensure_directories(file_sync)


__all__ = [
    "DATA_FORMAT_MARKER_BYTES",
    "StorageLayout",
    "UnsupportedDataFormatError",
]
