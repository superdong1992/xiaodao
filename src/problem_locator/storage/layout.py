"""Fixed DATA_ROOT layout creation and empty-installation detection."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from .paths import validate_data_root


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

    def ensure_directories(self) -> None:
        """Create only the directories permitted by the frozen S02 layout."""

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
            try:
                value = directory.lstat()
            except FileNotFoundError:
                directory.mkdir(
                    mode=0o700,
                    parents=directory == self.data_root,
                )
                continue
            if not stat.S_ISDIR(value.st_mode):
                raise ValueError("DATA_ROOT layout nodes must be real directories")

    def has_business_content_without_state(self) -> bool:
        """Return whether initializing a new StateFile would hide existing data."""

        if self.previous_state.exists() or self.previous_state.is_symlink():
            return True
        for entry in self.resources.iterdir():
            if entry != self.cases_resources:
                return True
        for root in (self.cases_resources, self.jobs):
            try:
                next(root.iterdir())
            except StopIteration:
                continue
            return True
        return False


__all__ = ["StorageLayout"]
