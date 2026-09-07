"""Bounded, private conversation uploads and exact-byte adoption by a Case."""
from __future__ import annotations

import hashlib
import os
import threading
import uuid
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from problem_locator.contracts import PrepareAttachment, UploadAttachmentContent
from problem_locator.contracts.models import derive_attachment_filename_suffix
from problem_locator.storage.atomic import (
    finalize_read_only_file, require_ordinary_file, require_real_directory,
)
from problem_locator.storage.platform import PlatformFileSync

from .models import AgentStoreError

_CHUNK = 1024 * 1024


class _BorrowedStream:
    """The upload port closes its input; the verification context owns this fd."""
    def __init__(self, source):
        self.source = source

    def read(self, size=-1):
        return self.source.read(size)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class ConversationUploads:
    def __init__(self, store, application, layout, *, file_sync=None):
        self.store, self.application, self.layout = store, application, layout
        self._sync = file_sync or PlatformFileSync()
        self._guard = threading.Lock()
        self._locks = weakref.WeakValueDictionary()

    def _lock(self, attachment_id):
        with self._guard:
            return self._locks.setdefault(attachment_id, threading.Lock())

    def _directory(self, attachment_id: str) -> Path:
        if str(uuid.UUID(attachment_id)) != attachment_id:
            raise AgentStoreError("VALIDATION_ERROR", "附件标识格式不正确。")
        require_real_directory(self.layout.data_root)
        require_real_directory(self.layout.resources)
        root = self.layout.conversation_uploads
        require_real_directory(root)
        directory = root / attachment_id
        if not directory.exists():
            directory.mkdir(mode=0o700)
            self._sync.sync_directory(root)
        require_real_directory(directory)
        return directory

    def prepare(self, conversation_id, request_id, name, content_type, declared_size, declared_sha256):
        derive_attachment_filename_suffix(name, content_type)
        return self.store.reserve_attachment(
            conversation_id, request_id, name, content_type, declared_size, declared_sha256,
        )

    @staticmethod
    def _consume(content: BinaryIO, size: int, sha256: str, destination=None) -> None:
        digest, observed = hashlib.sha256(), 0
        while True:
            chunk = content.read(min(_CHUNK, size - observed + 1))
            if not isinstance(chunk, bytes):
                raise AgentStoreError("VALIDATION_ERROR", "附件上传内容必须是原始字节。")
            if not chunk:
                break
            observed += len(chunk)
            if observed > size:
                raise AgentStoreError("RESOURCE_SIZE_MISMATCH", "附件大小与声明不一致。", 422)
            digest.update(chunk)
            if destination is not None:
                destination.write(chunk)
        if observed != size:
            raise AgentStoreError("RESOURCE_SIZE_MISMATCH", "附件大小与声明不一致。", 422)
        if digest.hexdigest() != sha256:
            raise AgentStoreError("RESOURCE_HASH_MISMATCH", "附件 SHA-256 校验失败。", 422)

    @contextmanager
    def _verified_source(self, record):
        path = self._directory(record.attachment_id) / "payload"
        before = require_ordinary_file(path)
        if before.st_nlink != 1:
            raise AgentStoreError("RESOURCE_INVALID", "附件文件身份校验失败。", 422)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            opened = os.fstat(source.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise AgentStoreError("RESOURCE_INVALID", "附件文件身份发生变化。", 422)
            self._consume(source, record.size, record.sha256)
            source.seek(0)
            yield source
            after = os.fstat(source.fileno())
            final = require_ordinary_file(path)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
            # Match FileBinaryStream: Windows path and handle ctime differ;
            # retain ctime checks within each view, never across the two APIs.
            path_fields = tuple(f for f in fields if not (os.name == "nt" and f == "st_ctime_ns"))
            if (any(getattr(opened, f) != getattr(after, f) or getattr(before, f) != getattr(final, f) for f in fields)
                    or any(getattr(after, f) != getattr(final, f) for f in path_fields)):
                raise AgentStoreError("RESOURCE_INVALID", "附件在读取期间发生变化。", 422)

    def upload(self, attachment_id, request_id, content_type, content_length, content_sha256, content):
        record = self.store.get_attachment(attachment_id)
        if (request_id, content_type, content_length, content_sha256) != (
            attachment_id, record.content_type, record.size, record.sha256,
        ):
            raise AgentStoreError("VALIDATION_ERROR", "上传请求头与附件预约不一致。")
        lock = self._lock(attachment_id)
        if not lock.acquire(blocking=False):
            raise AgentStoreError("UPLOAD_IN_PROGRESS", "该附件正在上传，请稍后重试同一请求。", 409)
        temporary = None
        started = False
        try:
            record = self.store.get_attachment(attachment_id)
            if record.status in {"READY", "IMPORTED"}:
                self._consume(content, record.size, record.sha256)
                return record
            view = self.store.get_conversation(record.conversation_id)
            if view.status in {"COMPLETED", "FAILED", "INTERRUPTED"} or view.case_status in {
                "RESOLVED", "PARTIALLY_RESOLVED", "UNRESOLVED", "FAILED", "CANCELLED",
            }:
                raise AgentStoreError("CONVERSATION_CLOSED", "本次定位已结束，请新建任务。", 409)
            self.store.set_attachment_status(attachment_id, "UPLOADING")
            started = True
            directory = self._directory(attachment_id)
            final = directory / "payload"
            # A crash after file publication may leave a valid file before READY.
            if final.exists() or final.is_symlink():
                self._consume(content, record.size, record.sha256)
                with self._verified_source(record):
                    pass
            else:
                temporary = directory / ("upload-" + uuid.uuid4().hex)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                with os.fdopen(os.open(temporary, flags, 0o600), "wb") as sink:
                    self._consume(content, record.size, record.sha256, sink)
                    sink.flush()
                    self._sync.sync_file(sink)
                    metadata = require_ordinary_file(temporary)
                    opened = os.fstat(sink.fileno())
                    if (metadata.st_dev, metadata.st_ino, metadata.st_nlink) != (opened.st_dev, opened.st_ino, 1):
                        raise AgentStoreError("RESOURCE_INVALID", "附件暂存文件身份发生变化。", 422)
                os.rename(temporary, final)
                temporary = None
                self._sync.sync_directory(directory)
            finalize_read_only_file(final, self._sync)
            return self.store.complete_attachment(attachment_id, storage_path=str(final))
        except Exception:
            if started:
                try:
                    self.store.set_attachment_status(attachment_id, "FAILED")
                except AgentStoreError:
                    # A concurrent terminal result owns the final visibility.
                    pass
            raise
        finally:
            if temporary is not None:
                # This exact, uncommitted upload is owned by this invocation.
                try:
                    require_ordinary_file(temporary)
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            lock.release()

    def import_into_case(self, conversation_id, case_id, attachment_id, execute_command):
        record = self.store.get_attachment(attachment_id)
        if record.conversation_id != conversation_id:
            raise AgentStoreError("ATTACHMENT_CONVERSATION_MISMATCH", "附件不属于当前会话。", 409)
        if record.status == "IMPORTED":
            return record.case_attachment_id
        if record.status != "READY":
            raise AgentStoreError("ATTACHMENT_NOT_READY", "附件尚未上传完成。", 409)
        view = self.application.get_case(case_id).case_view
        prepared = execute_command(
            conversation_id, "prepare-" + attachment_id,
            PrepareAttachment(idempotency_key="agent-prepare-" + attachment_id,
                case_id=case_id, expected_case_revision=view.case_revision,
                name=record.name, content_type=record.content_type,
                declared_size=record.size, declared_sha256=record.sha256),
        )
        target_id = prepared.business_receipt.primary_resource_id
        with self._verified_source(record) as source:
            response = self.application.execute(UploadAttachmentContent(
                idempotency_key=target_id, attachment_id=target_id,
                expected_content_type=record.content_type, expected_size=record.size,
                expected_sha256=record.sha256, byte_stream=_BorrowedStream(source),
            ))
        self.store.bind_attachment(attachment_id, target_id)
        return response.business_receipt.primary_resource_id
