"""Prepare bounded, verified text inputs without a product-specific parser.

Archive members are streamed into generated filenames, never extracted using
their supplied paths. The original names remain evidence labels in the receipt.
"""
from __future__ import annotations

import codecs
import gzip
import hashlib
import os
import stat
import struct
import tarfile
import time
import unicodedata
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from problem_locator.contracts import Attachment, CancellationSignal, ErrorCode, ExecutionStage, canonical_json_bytes
from problem_locator.contracts.models import GENERIC_DIRECT_LOG_CONTENT_TYPES, GENERIC_LEGACY_LOG_CONTENT_TYPES
from problem_locator.storage.capacity import StorageCapacityError, capacity_reservation
from .failures import runtime_failure
from .workspace import PreparedWorkspace, _atomic_write, _set_inputs_read_only

_CHUNK = 1024 * 1024
_ARCHIVE_METADATA_BYTES = 16 * _CHUNK
_TAR_METADATA_BYTES = 1024 * 1024
_ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".gz", ".bz2", ".xz", ".7z", ".rar")


class DirectGenericLogError(ValueError):
    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True)
class DirectGenericLogLimits:
    max_expanded_bytes: int = 1024 ** 3
    max_readable_bytes: int = 1024 ** 3
    max_entries: int = 10_000
    max_depth: int = 32
    max_manifest_bytes: int = 2_000_000
    max_compression_ratio: int = 1000

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("日志准备限额必须是正整数。")


class _Budget:
    def __init__(self, limits, cancellation, deadline):
        self.limits, self.cancellation, self.deadline = limits, cancellation, deadline
        self.expanded = self.readable = self.entries = 0

    def check(self):
        if self.cancellation.is_cancelled():
            raise runtime_failure(stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.BACKEND_CANCELLED, message="日志准备已取消。")
        if time.monotonic() >= self.deadline:
            raise runtime_failure(stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.BACKEND_TIMEOUT, message="日志准备超时。", retryable=True)

    def expand(self, amount):
        self.check()
        self.expanded += amount
        if self.expanded > self.limits.max_expanded_bytes:
            raise DirectGenericLogError("EXPANDED_BYTES_LIMIT", "日志展开后的总大小超出限制。")

    def entry(self):
        self.check()
        self.entries += 1
        if self.entries > self.limits.max_entries:
            raise DirectGenericLogError("ENTRY_COUNT_LIMIT", "日志包中的文件或目录数量超出限制。")


class _CheckedReader:
    """Bound library metadata reads and observe cancellation on every read."""
    def __init__(self, stream, budget):
        self.stream, self.budget = stream, budget

    def read(self, size=-1):
        self.budget.check()
        if size < 0:
            position = self.stream.tell()
            size = self.stream.seek(0, os.SEEK_END) - position
            self.stream.seek(position)
        if size > _ARCHIVE_METADATA_BYTES:
            raise DirectGenericLogError("ARCHIVE_METADATA_LIMIT", "日志包的目录或扩展元数据超出限制。")
        result = self.stream.read(size)
        self.budget.check()
        return result

    def seek(self, *args):
        self.budget.check()
        return self.stream.seek(*args)

    def tell(self):
        return self.stream.tell()

    def seekable(self):
        return True


class _ExpandedReader:
    def __init__(self, stream, budget, compressed_size=None):
        self.stream, self.budget, self.compressed_size = stream, budget, compressed_size
        self.observed = 0

    def read(self, size):
        self.budget.check()
        if size < 0 or size > _ARCHIVE_METADATA_BYTES:
            raise DirectGenericLogError("ARCHIVE_METADATA_LIMIT", "日志包的扩展元数据超出限制。")
        # Refuse the next byte beyond the limit instead of buffering a large
        # malicious member merely because a library supplied its claimed size.
        size = min(size, self.budget.limits.max_expanded_bytes - self.budget.expanded + 1)
        chunk = self.stream.read(size)
        self.observed += len(chunk)
        self.budget.expand(len(chunk))
        if (self.compressed_size is not None and self.observed >
                max(_CHUNK, self.compressed_size * self.budget.limits.max_compression_ratio)):
            raise DirectGenericLogError("COMPRESSION_RATIO_LIMIT", "日志包的压缩比超出限制。")
        return chunk


class _BoundedTarInfo(tarfile.TarInfo):
    @classmethod
    def frombuf(cls, buf, encoding, errors):
        member = super().frombuf(buf, encoding, errors)
        if member.type == tarfile.GNUTYPE_SPARSE:
            raise DirectGenericLogError("UNSAFE_ARCHIVE_ENTRY", "日志包包含稀疏文件。")
        if (member.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME,
                tarfile.GNUTYPE_LONGLINK) and member.size > _TAR_METADATA_BYTES):
            raise DirectGenericLogError("ARCHIVE_METADATA_LIMIT", "日志包的扩展元数据超出限制。")
        return member


def _check_zip_directory(reader, budget):
    """Bound central-directory allocation before ZipFile constructs its list."""
    end = reader.seek(0, os.SEEK_END)
    reader.seek(max(0, end - 65557))
    tail = reader.read()
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise DirectGenericLogError("ARCHIVE_INVALID", "日志 ZIP 包的目录不完整。")
    _, disk, central_disk, disk_entries, entries, central_bytes, central_offset, comment = struct.unpack(
        "<4s4H2IH", tail[offset:offset + 22])
    if offset + 22 + comment != len(tail) or disk != 0 or central_disk != 0:
        raise DirectGenericLogError("ARCHIVE_INVALID", "不支持分卷或目录损坏的日志 ZIP 包。")
    if entries == 0xFFFF or central_bytes == 0xFFFFFFFF:
        end_record_offset = max(0, end - 65557) + offset
        if end_record_offset < 20:
            raise DirectGenericLogError("ARCHIVE_INVALID", "日志 ZIP64 包的目录不完整。")
        reader.seek(end_record_offset - 20)
        locator = reader.read(20)
        signature, disk_number, record_offset, disks = struct.unpack("<4sIQI", locator)
        if signature != b"PK\x06\x07" or disk_number != 0 or disks != 1:
            raise DirectGenericLogError("ARCHIVE_INVALID", "不支持分卷或目录损坏的日志 ZIP64 包。")
        reader.seek(record_offset)
        record = reader.read(56)
        if len(record) != 56 or record[:4] != b"PK\x06\x06":
            raise DirectGenericLogError("ARCHIVE_INVALID", "日志 ZIP64 包的目录不完整。")
        _, record_size, _, _, disk, central_disk, disk_entries, entries, central_bytes, central_offset = struct.unpack(
            "<4sQ2H2I4Q", record)
        if record_size != 44 or disk or central_disk:
            raise DirectGenericLogError("ARCHIVE_INVALID", "不支持扩展或分卷日志 ZIP64 包。")
    if disk_entries != entries:
        raise DirectGenericLogError("ARCHIVE_INVALID", "日志 ZIP 包的目录数量不一致。")
    if entries > budget.limits.max_entries - budget.entries:
        raise DirectGenericLogError("ENTRY_COUNT_LIMIT", "日志包中的文件或目录数量超出限制。")
    if central_bytes > _ARCHIVE_METADATA_BYTES:
        raise DirectGenericLogError("ARCHIVE_METADATA_LIMIT", "日志包的目录超出大小限制。")
    if central_offset + central_bytes > end:
        raise DirectGenericLogError("ARCHIVE_INVALID", "日志 ZIP 包的目录位置无效。")
    reader.seek(central_offset)
    remaining, observed_entries = central_bytes, 0
    while remaining:
        header = reader.read(min(46, remaining))
        if len(header) != 46 or header[:4] != b"PK\x01\x02":
            raise DirectGenericLogError("ARCHIVE_INVALID", "日志 ZIP 包的目录结构无效。")
        variable_bytes = sum(struct.unpack_from("<3H", header, 28))
        remaining -= 46 + variable_bytes
        if remaining < 0:
            raise DirectGenericLogError("ARCHIVE_INVALID", "日志 ZIP 包的目录长度无效。")
        observed_entries += 1
        if observed_entries > budget.limits.max_entries - budget.entries:
            raise DirectGenericLogError("ENTRY_COUNT_LIMIT", "日志包中的文件或目录数量超出限制。")
        reader.seek(variable_bytes, os.SEEK_CUR)
    if observed_entries != entries:
        raise DirectGenericLogError("ARCHIVE_INVALID", "日志 ZIP 包的目录数量不一致。")
    reader.seek(0)


def _member_path(name, budget):
    try:
        encoded_size = len(name.encode("utf-8"))
    except (AttributeError, UnicodeEncodeError) as exc:
        raise DirectGenericLogError("UNSAFE_ARCHIVE_PATH", "日志包的文件名编码无效。") from exc
    if (not isinstance(name, str) or not name or encoded_size > 4096
            or "\\" in name or name.startswith("/") or ":" in name
            or any(unicodedata.category(char) == "Cc" for char in name)):
        raise DirectGenericLogError("UNSAFE_ARCHIVE_PATH", "日志包包含不安全的文件路径。")
    parts = name.rstrip("/").split("/")
    if ".." in parts or "" in parts:
        raise DirectGenericLogError("UNSAFE_ARCHIVE_PATH", "日志包包含不安全的文件路径。")
    parts = [part for part in parts if part != "."]
    if len(parts) > budget.limits.max_depth:
        raise DirectGenericLogError("PATH_DEPTH_LIMIT", "日志包的目录层级超出限制。")
    return "/".join(parts)


def _register_path(seen, label, is_directory):
    if (not label or label in seen
            or any(seen.get("/".join(label.split("/")[:index])) is False
                for index in range(1, len(label.split("/"))))
            or not is_directory and any(path.startswith(label + "/") for path in seen)):
        raise DirectGenericLogError("ARCHIVE_PATH_CONFLICT", "日志包包含重复或冲突的成员路径。")
    seen[label] = is_directory


@contextmanager
def _verified_source(path, attachment, budget):
    budget.check()
    # The runtime supplies a controlled copy. Check each ancestor as well as
    # the final fd so a changed workspace cannot redirect this input.
    if any(candidate.is_symlink() for candidate in (path, *path.parents)):
        raise DirectGenericLogError("SOURCE_CHANGED", "日志附件路径校验失败。")
    before = path.stat(follow_symlinks=False)
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_size != attachment.size or attachment.sha256 is None):
        raise DirectGenericLogError("SOURCE_CHANGED", "日志附件大小或文件身份校验失败。")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        opened = os.fstat(source.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise DirectGenericLogError("SOURCE_CHANGED", "日志附件在读取前发生变化。")
        digest, count = hashlib.sha256(), 0
        while chunk := source.read(_CHUNK):
            budget.check()
            count += len(chunk)
            if count > attachment.size:
                raise DirectGenericLogError("SOURCE_CHANGED", "日志附件在读取时发生变化。")
            digest.update(chunk)
        if count != attachment.size or digest.hexdigest() != attachment.sha256:
            raise DirectGenericLogError("SOURCE_CHANGED", "日志附件 SHA-256 校验失败。")
        source.seek(0)
        yield _CheckedReader(source, budget)
        budget.check()
        after, named = os.fstat(source.fileno()), path.stat(follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
        if (any(getattr(before, field) != getattr(after, field)
                or getattr(after, field) != getattr(named, field) for field in fields)
                or opened.st_ctime_ns != after.st_ctime_ns or before.st_ctime_ns != named.st_ctime_ns):
            raise DirectGenericLogError("SOURCE_CHANGED", "日志附件在分析准备期间发生变化。")


class _Preparation:
    def __init__(self, workspace, budget, capacity):
        self.workspace, self.budget, self.capacity = workspace, budget, capacity
        self.logs, self.skipped, self.sources = [], [], []
        self.manifest_bytes = 0

    def add(self, collection, row):
        self.manifest_bytes += len(canonical_json_bytes(row)) + 1
        if self.manifest_bytes > self.budget.limits.max_manifest_bytes:
            raise DirectGenericLogError("MANIFEST_BYTES_LIMIT", "日志文件清单超出大小限制。")
        collection.append(row)

    def consume(self, stream, *, label, identity, size=None, counted=False):
        budget = self.budget
        if size is not None and (size < 0 or size > budget.limits.max_expanded_bytes):
            raise DirectGenericLogError("EXPANDED_BYTES_LIMIT", "日志文件展开后的大小超出限制。")
        relative = f"inputs/generic-logs/log-{len(self.logs) + 1:06d}.log"
        destination = self.workspace.root / relative
        reserve = size if size is not None else budget.limits.max_expanded_bytes - budget.expanded
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        reason = "NESTED_ARCHIVE" if label.lower().endswith(_ARCHIVE_SUFFIXES) else None
        digest, observed, prefix = hashlib.sha256(), 0, b""
        with capacity_reservation(self.capacity, destination, reserve), destination.open("xb") as target:
            while True:
                budget.check()
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                if not counted:
                    budget.expand(len(chunk))
                observed += len(chunk)
                if size is not None and observed > size:
                    raise DirectGenericLogError("ARCHIVE_INVALID", "日志包成员大小与声明不一致。")
                digest.update(chunk)
                if len(prefix) < 512:
                    prefix += chunk[:512 - len(prefix)]
                if reason is None:
                    if b"\x00" in chunk:
                        reason = "NON_TEXT"
                    else:
                        try:
                            decoder.decode(chunk)
                        except UnicodeDecodeError:
                            reason = "UNSUPPORTED_ENCODING"
                if reason is None:
                    if budget.readable + observed > budget.limits.max_readable_bytes:
                        raise DirectGenericLogError("READABLE_BYTES_LIMIT", "可分析日志的总大小超出限制。")
                    target.write(chunk)
            if size is not None and size != observed:
                raise DirectGenericLogError("ARCHIVE_INVALID", "日志包成员内容不完整。")
            if reason is None:
                try:
                    decoder.decode(b"", final=True)
                except UnicodeDecodeError:
                    reason = "UNSUPPORTED_ENCODING"
            if (prefix.startswith((b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b", b"7z\xbc\xaf\x27\x1c",
                    b"Rar!\x1a\x07", b"\xfd7zXZ\x00", b"BZh")) or prefix[257:262] == b"ustar"):
                reason = "NESTED_ARCHIVE"
            if observed == 0 or prefix == codecs.BOM_UTF8 and observed == 3:
                reason = "EMPTY_FILE"
            if reason is None:
                budget.readable += observed
                if budget.readable > budget.limits.max_readable_bytes:
                    raise DirectGenericLogError("READABLE_BYTES_LIMIT", "可分析日志的总大小超出限制。")
                target.flush()
                os.fsync(target.fileno())
        row = {**identity, "relative_path": label, "size": observed, "sha256": digest.hexdigest()}
        if reason is not None:
            # Only remove our newly created, unpublished candidate. Existing
            # evidence and failed preparation workspaces are never reclaimed.
            destination.unlink()
            self.add(self.skipped, {**row, "reason": reason})
        else:
            self.add(self.logs, {**row, "log_path": relative,
                "encoding": "utf-8-sig" if prefix.startswith(codecs.BOM_UTF8) else "utf-8"})

    def zip_members(self, reader, identity):
        _check_zip_directory(reader, self.budget)
        with zipfile.ZipFile(reader, "r") as archive:
            seen = {}
            for member in archive.infolist():
                self.budget.entry()
                label = _member_path(member.orig_filename, self.budget)
                mode = member.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise DirectGenericLogError("UNSAFE_ARCHIVE_ENTRY", "日志包包含链接或特殊文件。")
                if member.flag_bits & 1:
                    raise DirectGenericLogError("ENCRYPTED_ARCHIVE", "不支持加密日志包。")
                if member.is_dir():
                    if member.file_size != 0:
                        raise DirectGenericLogError("ARCHIVE_INVALID", "日志 ZIP 包的目录项包含文件内容。")
                    if label:
                        _register_path(seen, label, True)
                    continue
                _register_path(seen, label, False)
                if member.file_size > max(_CHUNK, member.compress_size * self.budget.limits.max_compression_ratio):
                    raise DirectGenericLogError("COMPRESSION_RATIO_LIMIT", "日志包的压缩比超出限制。")
                with archive.open(member, "r") as stream:
                    self.consume(stream, label=label, identity=identity, size=member.file_size)

    def tar_members(self, stream, identity):
        budget = self.budget
        consecutive_metadata = 0
        end_marker_seen = False

        class ScopedTarInfo(_BoundedTarInfo):
            @classmethod
            def frombuf(cls, buf, encoding, errors):
                nonlocal consecutive_metadata, end_marker_seen
                try:
                    member = super().frombuf(buf, encoding, errors)
                except tarfile.EOFHeaderError:
                    end_marker_seen = True
                    raise
                except tarfile.HeaderError as exc:
                    # TarFile tolerates a malformed/missing final header after
                    # valid members. Inputs must instead fail as a whole.
                    raise DirectGenericLogError("ARCHIVE_INVALID", "日志 TAR 包包含损坏或不完整的头部。") from exc
                budget.entry()
                if member.type in (tarfile.XHDTYPE, tarfile.XGLTYPE,
                        tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
                    consecutive_metadata += 1
                    if consecutive_metadata > budget.limits.max_depth:
                        raise DirectGenericLogError("ARCHIVE_METADATA_LIMIT", "日志包包含过多连续扩展头。")
                else:
                    consecutive_metadata = 0
                return member

        with tarfile.open(fileobj=stream, mode="r|", tarinfo=ScopedTarInfo) as archive:
            seen = {}
            for member in archive:
                label = _member_path(member.name, self.budget)
                if not label and member.isdir():
                    continue
                _register_path(seen, label, member.isdir())
                if member.isdir():
                    continue
                if not member.isfile() or member.sparse is not None:
                    raise DirectGenericLogError("UNSAFE_ARCHIVE_ENTRY", "日志包包含链接、稀疏文件或特殊文件。")
                with archive.extractfile(member) as source:
                    self.consume(source, label=label, identity=identity, size=member.size, counted=True)
            # Drain via TarFile's reader, including its read-ahead buffer. This
            # validates both TAR end blocks, rejects hidden trailing archives,
            # and forces GZIP footer/CRC validation before accepting any logs.
            trailing = 0
            while chunk := archive.fileobj.read(_CHUNK):
                trailing += len(chunk)
                if chunk.strip(b"\x00"):
                    raise DirectGenericLogError("ARCHIVE_INVALID", "日志 TAR 包结束标记后包含其他内容。")
            if not end_marker_seen or trailing < 512:
                raise DirectGenericLogError("ARCHIVE_INVALID", "日志 TAR 包缺少完整结束标记。")


def prepare_direct_generic_sources(workspace: PreparedWorkspace, *,
        sources: list[tuple[Path, Attachment]], cancellation: CancellationSignal,
        deadline: float, capacity=None, limits: DirectGenericLogLimits = DirectGenericLogLimits()) -> bytes:
    budget = _Budget(limits, cancellation, deadline)
    budget.check()
    if not sources or len(sources) > 20 or len({item.attachment_id for _, item in sources}) != len(sources):
        raise DirectGenericLogError("INVALID_SOURCES", "日志附件选择为空、重复或数量超出限制。")
    inputs = workspace.root / "inputs"
    before = inputs.stat(follow_symlinks=False)
    if inputs.is_symlink() or (before.st_dev, before.st_ino) != (workspace.inputs_device, workspace.inputs_inode):
        raise DirectGenericLogError("SOURCE_CHANGED", "日志输入目录发生变化。")
    inputs.chmod(0o755)
    preparation = _Preparation(workspace, budget, capacity or getattr(workspace, "capacity", None))
    try:
        (inputs / "generic-logs").mkdir(mode=0o700)
        for path, attachment in sources:
            if attachment.content_type not in GENERIC_DIRECT_LOG_CONTENT_TYPES:
                raise DirectGenericLogError("UNSUPPORTED_INPUT_TYPE", "该附件类型不支持直接日志分析。")
            identity = {"source_attachment_id": attachment.attachment_id, "source_attachment_name": attachment.name,
                "source_attachment_sha256": attachment.sha256}
            name = attachment.name.lower()
            source_kind = "archive" if attachment.content_type in GENERIC_LEGACY_LOG_CONTENT_TYPES else "text"
            preparation.add(preparation.sources, {**identity, "source_kind": source_kind,
                "size": attachment.size, "content_type": attachment.content_type})
            with _verified_source(Path(path), attachment, budget) as reader:
                if attachment.content_type == "application/zip":
                    preparation.zip_members(reader, identity)
                elif attachment.content_type == "application/x-tar":
                    preparation.tar_members(_ExpandedReader(reader, budget), identity)
                elif attachment.content_type == "application/gzip":
                    with gzip.GzipFile(fileobj=reader, mode="rb") as decoded:
                        expanded = _ExpandedReader(decoded, budget, attachment.size)
                        if name.endswith((".tar.gz", ".tgz")):
                            preparation.tar_members(expanded, identity)
                        else:
                            budget.entry()
                            preparation.consume(expanded, label=attachment.name[:-3] or "payload.log",
                                identity=identity, counted=True)
                else:
                    budget.entry()
                    preparation.consume(reader, label=attachment.name, identity=identity, size=attachment.size)
        budget.check()
        # This coverage describes preparation only. It never asserts that the
        # Skill has opened or analysed any of the listed files.
        empty_reason = (None if preparation.logs else
            "NO_READABLE_TEXT" if preparation.skipped else "NO_FILES")
        payload = canonical_json_bytes({"schema_version": 3, "preparation": "direct-v1",
            "sources": preparation.sources, "logs": preparation.logs, "skipped": preparation.skipped,
            "coverage": {"scope": "INPUT_PREPARATION", "expanded_bytes": budget.expanded,
                "readable_bytes": budget.readable, "entries": budget.entries,
                "complete": not preparation.skipped, "empty_reason": empty_reason}})
        if len(payload) > limits.max_manifest_bytes:
            raise DirectGenericLogError("MANIFEST_BYTES_LIMIT", "日志文件清单超出大小限制。")
        _atomic_write(inputs / "generic_logs.json", payload)
        return payload
    except StorageCapacityError as exc:
        raise runtime_failure(stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.WORKSPACE_PREPARE_FAILED, message=str(exc), retryable=True) from exc
    except (zipfile.BadZipFile, tarfile.TarError, gzip.BadGzipFile, EOFError, zlib.error,
            struct.error, NotImplementedError) as exc:
        raise DirectGenericLogError("ARCHIVE_INVALID", "日志包损坏或压缩格式不受支持。") from exc
    finally:
        _set_inputs_read_only(inputs)
