"""Clock-driven physical candidate discovery for the S02 cleaner.

Business-reference, active-publication, and durable-outbox decisions are made
by the r2 cleaner adapter.  This module only enumerates exact filesystem nodes
and applies the frozen strict age thresholds.
"""

from __future__ import annotations

import calendar
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from problem_locator.contracts import Clock, OpaqueId, UtcTimestamp
from problem_locator.contracts.limits import (
    ORPHAN_RESOURCE_RETENTION_SECONDS,
    PROPOSAL_STAGING_RETENTION_SECONDS,
    UPLOAD_TEMP_RETENTION_SECONDS,
    WORKSPACE_RETENTION_SECONDS,
)

from .atomic import require_ordinary_file, require_real_directory
from .layout import StorageLayout
from .resource_files import scan_all_resources


_OPAQUE_ID_ADAPTER = TypeAdapter(OpaqueId)
_UTC_TIMESTAMP_ADAPTER = TypeAdapter(UtcTimestamp)

_RetentionKind = Literal[
    "UPLOAD",
    "PROPOSAL",
    "WORKSPACE",
    "STATE_TEMP",
    "FORMAL_RESOURCE",
    "JOB",
]


@dataclass(frozen=True, slots=True)
class _RetentionCandidate:
    kind: _RetentionKind
    path: Path
    age_seconds: float
    retention_seconds: int


def _timestamp_ns(value: UtcTimestamp) -> int:
    validated = _UTC_TIMESTAMP_ADAPTER.validate_python(value)
    parsed = datetime.strptime(validated, "%Y-%m-%dT%H:%M:%S.%fZ")
    return calendar.timegm(parsed.utctimetuple()) * 1_000_000_000 + parsed.microsecond * 1_000


def _node_age_ns(path: Path, now_ns: int) -> int:
    metadata = path.lstat()
    return now_ns - metadata.st_mtime_ns


def _is_strictly_expired(age_ns: int, retention_seconds: int) -> bool:
    return age_ns > retention_seconds * 1_000_000_000


class RetentionScanner:
    """Discover old exact candidates without deciding whether they are referenced."""

    def __init__(self, layout: StorageLayout, clock: Clock) -> None:
        self._layout = layout
        self._clock = clock

    def _candidate(
        self,
        kind: _RetentionKind,
        path: Path,
        retention_seconds: int,
        now_ns: int,
        *,
        timestamp_path: Path | None = None,
    ) -> _RetentionCandidate | None:
        age_ns = _node_age_ns(timestamp_path or path, now_ns)
        if not _is_strictly_expired(age_ns, retention_seconds):
            return None
        return _RetentionCandidate(
            kind,
            path,
            age_ns / 1_000_000_000,
            retention_seconds,
        )

    @staticmethod
    def _real_directory_entries(root: Path) -> list[os.DirEntry[str]]:
        require_real_directory(root)
        entries = sorted(os.scandir(root), key=lambda item: item.name)
        for entry in entries:
            metadata = entry.stat(follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("retention root contains a non-directory candidate")
        return entries

    def discover(self) -> tuple[_RetentionCandidate, ...]:
        """Return all candidates strictly older than their frozen threshold."""

        now_ns = _timestamp_ns(self._clock.now())
        candidates: list[_RetentionCandidate] = []

        for entry in self._real_directory_entries(self._layout.uploads):
            _OPAQUE_ID_ADAPTER.validate_python(entry.name)
            path = Path(entry.path)
            marker = path / "staged.json"
            try:
                require_ordinary_file(marker)
                timestamp_path = marker
            except FileNotFoundError:
                timestamp_path = path
            candidate = self._candidate(
                "UPLOAD",
                path,
                UPLOAD_TEMP_RETENTION_SECONDS,
                now_ns,
                timestamp_path=timestamp_path,
            )
            if candidate is not None:
                candidates.append(candidate)

        for job_entry in self._real_directory_entries(self._layout.proposals):
            _OPAQUE_ID_ADAPTER.validate_python(job_entry.name)
            for proposal_entry in self._real_directory_entries(Path(job_entry.path)):
                path = Path(proposal_entry.path)
                if re.fullmatch(r"p-[0-9a-f]{64}", proposal_entry.name) is None:
                    raise ValueError("proposal retention path is not canonical")
                marker = path / "staged.json"
                try:
                    require_ordinary_file(marker)
                    timestamp_path = marker
                except FileNotFoundError:
                    timestamp_path = path
                candidate = self._candidate(
                    "PROPOSAL",
                    path,
                    PROPOSAL_STAGING_RETENTION_SECONDS,
                    now_ns,
                    timestamp_path=timestamp_path,
                )
                if candidate is not None:
                    candidates.append(candidate)

        for entry in self._real_directory_entries(self._layout.workspaces):
            _OPAQUE_ID_ADAPTER.validate_python(entry.name)
            path = Path(entry.path)
            candidate = self._candidate(
                "WORKSPACE",
                path,
                WORKSPACE_RETENTION_SECONDS,
                now_ns,
            )
            if candidate is not None:
                candidates.append(candidate)

        require_real_directory(self._layout.state_temporary)
        for entry in sorted(
            os.scandir(self._layout.state_temporary),
            key=lambda item: item.name,
        ):
            path = Path(entry.path)
            require_ordinary_file(path)
            candidate = self._candidate(
                "STATE_TEMP",
                path,
                UPLOAD_TEMP_RETENTION_SECONDS,
                now_ns,
            )
            if candidate is not None:
                candidates.append(candidate)

        for observation in scan_all_resources(self._layout).values():
            candidate = self._candidate(
                "FORMAL_RESOURCE",
                observation.path,
                ORPHAN_RESOURCE_RETENTION_SECONDS,
                now_ns,
                timestamp_path=observation.path.parent,
            )
            if candidate is not None:
                candidates.append(candidate)

        for entry in self._real_directory_entries(self._layout.jobs):
            _OPAQUE_ID_ADAPTER.validate_python(entry.name)
            path = Path(entry.path)
            candidate = self._candidate(
                "JOB",
                path,
                ORPHAN_RESOURCE_RETENTION_SECONDS,
                now_ns,
            )
            if candidate is not None:
                candidates.append(candidate)

        return tuple(sorted(candidates, key=lambda value: (value.kind, str(value.path))))


__all__ = ["RetentionScanner"]
