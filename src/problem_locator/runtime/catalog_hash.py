"""Deterministic, symlink-safe product-directory hashing."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from problem_locator.contracts import bytes_sha256, canonical_json_sha256


_WINDOWS_DRIVE_PATTERN = re.compile(r"[A-Za-z]:")


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or "//" in value
        or value.endswith("/")
        or _WINDOWS_DRIVE_PATTERN.match(value)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("asset product path must be a safe relative POSIX path")
    return path


def _excluded(path: PurePosixPath) -> bool:
    return (
        path.name == ".DS_Store"
        or path.name.endswith(".pyc")
        or "__pycache__" in path.parts
        or ".pytest_cache" in path.parts
        or path.name == ".managed"
        or path.name.startswith(".managed.")
        or path.name == ".codex-managed"
    )


def _windows_file_identity(path: Path) -> tuple[int, int] | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_file(str(path), 0, 0x7, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), f"cannot open asset product file: {path}")
    try:
        information = _ByHandleFileInformation()
        if not get_information(handle, ctypes.byref(information)):
            raise OSError(ctypes.get_last_error(), f"cannot identify asset product file: {path}")
        return (
            information.volume_serial_number,
            (information.file_index_high << 32) | information.file_index_low,
        )
    finally:
        close_handle(handle)


def product_directory_entries(root: Path) -> tuple[dict[str, Any], ...]:
    root = Path(root)
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise ValueError(f"asset product directory is unavailable: {root}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"asset product root must be a real directory: {root}")

    result: list[dict[str, Any]] = []
    seen_ids: set[tuple[int, int]] = set()

    def visit(directory: Path, prefix: tuple[str, ...]) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"asset product directory cannot be scanned: {directory}") from exc
        for child in children:
            relative_text = "/".join((*prefix, child.name))
            relative = _safe_relative_path(relative_text)
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"asset product node cannot be inspected: {relative_text}") from exc
            if stat.S_ISLNK(child_stat.st_mode):
                raise ValueError(f"asset product links are forbidden: {relative_text}")
            if stat.S_ISDIR(child_stat.st_mode):
                if not _excluded(relative):
                    visit(Path(child.path), (*prefix, child.name))
                continue
            if not stat.S_ISREG(child_stat.st_mode):
                raise ValueError(f"asset product contains a non-ordinary node: {relative_text}")
            file_id = (
                _windows_file_identity(Path(child.path))
                if os.name == "nt"
                else (child_stat.st_dev, child_stat.st_ino)
            )
            if file_id is not None:
                if file_id in seen_ids:
                    raise ValueError(f"asset product hard links are forbidden: {relative_text}")
                seen_ids.add(file_id)
            maximum_links = 2 if os.name == "nt" else 1
            if child_stat.st_nlink > maximum_links:
                raise ValueError(f"asset product hard links are forbidden: {relative_text}")
            if _excluded(relative):
                continue
            try:
                data = Path(child.path).read_bytes()
            except OSError as exc:
                raise ValueError(f"asset product file cannot be read: {relative_text}") from exc
            result.append({"path": relative_text, "size": len(data), "sha256": bytes_sha256(data)})

    visit(root, ())
    result.sort(key=lambda item: item["path"])
    return tuple(result)


def hash_product_directory(root: Path) -> str:
    """Hash a complete product directory using the frozen V1 preimage."""

    return canonical_json_sha256(
        {"version": 1, "entries": list(product_directory_entries(root))}
    )


__all__ = ["hash_product_directory", "product_directory_entries"]
