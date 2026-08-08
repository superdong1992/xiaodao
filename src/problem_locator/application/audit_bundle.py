"""Deterministic, bounded audit-bundle construction for UNRESOLVED Cases."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from typing import Iterable, Mapping

from problem_locator.contracts import canonical_json_bytes


AUDIT_BUNDLE_FORMAT_ID = "problem-locator-audit-bundle-v1"
AUDIT_BUNDLE_MAX_BYTES = 64 * 1024 * 1024
AUDIT_BUNDLE_CORE_MAX_BYTES = 32 * 1024 * 1024
AUDIT_BUNDLE_LOG_MAX_BYTES = 2 * 1024 * 1024
_SAFE_ENTRY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$")
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class AuditBundleSource:
    path: str
    payload: bytes
    required: bool = True
    truncate_as_log: bool = False


@dataclass(frozen=True, slots=True)
class BuiltAuditBundle:
    payload: bytes
    sha256: str
    manifest: dict[str, object]


def _safe_path(path: str) -> str:
    if (
        not isinstance(path, str)
        or _SAFE_ENTRY.fullmatch(path) is None
        or path.startswith("/")
        or "//" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError("audit bundle entry path is unsafe")
    return path


def _trim_log(payload: bytes) -> tuple[bytes, list[dict[str, object]]]:
    if len(payload) <= AUDIT_BUNDLE_LOG_MAX_BYTES:
        return payload, []
    half = AUDIT_BUNDLE_LOG_MAX_BYTES // 2
    retained = payload[:half] + payload[-half:]
    return retained, [
        {
            "kind": "MIDDLE_OMITTED",
            "start": half,
            "end": len(payload) - half,
        }
    ]


def _zip_bytes(entries: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_STORED,
        allowZip64=False,
    ) as archive:
        for path in sorted(entries, key=lambda item: (item != "manifest.json", item)):
            info = zipfile.ZipInfo(path, _ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            info.flag_bits = 0x800
            archive.writestr(info, entries[path])
    return output.getvalue()


def build_audit_bundle(
    *,
    case_id: str,
    source_outcome_id: str,
    sources: Iterable[AuditBundleSource],
) -> BuiltAuditBundle:
    """Build one deterministic bundle, retaining required evidence first."""

    ordered = list(sources)
    paths = [_safe_path(source.path) for source in ordered]
    if len(paths) != len(set(paths)) or "manifest.json" in paths:
        raise ValueError("audit bundle source paths must be unique")
    if any(type(source.payload) is not bytes for source in ordered):
        raise TypeError("audit bundle payloads must be exact bytes")

    entries: dict[str, bytes] = {}
    records: list[dict[str, object]] = []
    core_bytes = 0
    for source in ordered:
        original = source.payload
        retained, omitted_ranges = (
            _trim_log(original) if source.truncate_as_log else (original, [])
        )
        if source.required:
            core_bytes += len(retained)
            if core_bytes > AUDIT_BUNDLE_CORE_MAX_BYTES:
                raise ValueError("required audit material exceeds the core limit")
        entries[source.path] = retained
        records.append(
            {
                "path": source.path,
                "required": source.required,
                "original_size": len(original),
                "original_sha256": hashlib.sha256(original).hexdigest(),
                "retained_size": len(retained),
                "retained_sha256": hashlib.sha256(retained).hexdigest(),
                "omissions": omitted_ranges,
            }
        )

    manifest: dict[str, object] = {
        "schema_version": 1,
        "format_id": AUDIT_BUNDLE_FORMAT_ID,
        "case_id": case_id,
        "source_outcome_id": source_outcome_id,
        "entries": records,
    }
    entries["manifest.json"] = canonical_json_bytes(manifest)
    payload = _zip_bytes(entries)
    if len(payload) > AUDIT_BUNDLE_MAX_BYTES:
        # Optional entries are removed from the end in caller-supplied stable
        # priority order.  Every omission remains explicit in the manifest.
        for source in reversed(ordered):
            if source.required or source.path not in entries:
                continue
            del entries[source.path]
            for record in records:
                if record["path"] == source.path:
                    record["retained_size"] = 0
                    record["retained_sha256"] = hashlib.sha256(b"").hexdigest()
                    record["omissions"] = [
                        {
                            "kind": "ENTRY_OMITTED",
                            "start": 0,
                            "end": len(source.payload),
                        }
                    ]
                    break
            entries["manifest.json"] = canonical_json_bytes(manifest)
            payload = _zip_bytes(entries)
            if len(payload) <= AUDIT_BUNDLE_MAX_BYTES:
                break
    if len(payload) > AUDIT_BUNDLE_MAX_BYTES:
        raise ValueError("audit bundle exceeds its fixed maximum size")
    return BuiltAuditBundle(
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        manifest=manifest,
    )


__all__ = [
    "AUDIT_BUNDLE_CORE_MAX_BYTES",
    "AUDIT_BUNDLE_FORMAT_ID",
    "AUDIT_BUNDLE_LOG_MAX_BYTES",
    "AUDIT_BUNDLE_MAX_BYTES",
    "AuditBundleSource",
    "BuiltAuditBundle",
    "build_audit_bundle",
]
