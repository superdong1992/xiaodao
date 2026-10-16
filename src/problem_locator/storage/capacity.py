"""Disk watermarks and short-lived reservations for the single server owner.

This is admission protection for known large writes, not a filesystem quota.
External tools and unrelated processes still require their own limits.
"""
from __future__ import annotations

import errno
import math
import shutil
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

GIB = 1024 ** 3


class StorageCapacityError(OSError):
    """A temporary storage-pressure failure; callers may retry after cleanup."""

    code = "AGENT_STORAGE_PRESSURE"
    retryable = True
    status_code = 503

    def __init__(self, message="服务器存储空间不足，正在清理历史日志，请稍后重试。"):
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DiskCapacitySnapshot:
    total: int
    free: int
    reserved: int
    effective_free: int
    low: int
    target: int
    under_pressure: bool


class DiskCapacityGuard:
    def __init__(self, *, low_free_bytes=10 * GIB, low_free_ratio=0.10,
                 target_free_bytes=20 * GIB, target_free_ratio=0.15,
                 disk_usage=None, wake_reclaim=None):
        if (type(low_free_bytes) is not int or type(target_free_bytes) is not int
                or not 0 <= low_free_bytes < target_free_bytes
                or isinstance(low_free_ratio, bool) or isinstance(target_free_ratio, bool)
                or not 0 <= low_free_ratio < target_free_ratio < 1):
            raise ValueError("磁盘容量水位无效，目标余量必须高于最低余量。")
        self.low_free_bytes, self.low_free_ratio = low_free_bytes, low_free_ratio
        self.target_free_bytes, self.target_free_ratio = target_free_bytes, target_free_ratio
        self._disk_usage = disk_usage or shutil.disk_usage
        self._wake_reclaim = wake_reclaim
        self._lock = threading.Lock()
        self._reserved: dict[int, int] = {}

    def set_wake_reclaim(self, callback):
        """Install a nonblocking wakeup callback after service composition."""
        with self._lock:
            self._wake_reclaim = callback

    def _wake(self, destination, required_free_bytes=0):
        # Never invoke the cleanup worker while holding a reservation mutex.
        callback = self._wake_reclaim
        if callback is not None:
            try:
                callback(Path(destination), required_free_bytes)
            except Exception:
                # A notification failure must not turn rejection into admission.
                pass

    @staticmethod
    def _existing_parent(destination):
        path = Path(destination).absolute()
        while not path.exists():
            parent = path.parent
            if parent == path:
                raise StorageCapacityError("无法确认存储空间，请稍后重试。")
            path = parent
        return path

    def _snapshot(self, path):
        usage = self._disk_usage(path)
        device = path.stat().st_dev
        reserved = self._reserved.get(device, 0)
        low = max(self.low_free_bytes, math.ceil(usage.total * self.low_free_ratio))
        target = max(self.target_free_bytes, math.ceil(usage.total * self.target_free_ratio))
        effective = max(0, usage.free - reserved)
        return device, DiskCapacitySnapshot(usage.total, usage.free, reserved,
            effective, low, target, effective < low)

    def snapshot(self, destination):
        try:
            path = self._existing_parent(destination)
            with self._lock:
                return self._snapshot(path)[1]
        except OSError as exc:
            self._wake(destination)
            raise StorageCapacityError("无法确认存储空间，请稍后重试。") from exc

    @contextmanager
    def try_reserve(self, destination, upper_bound_bytes):
        if type(upper_bound_bytes) is not int or upper_bound_bytes < 0:
            raise ValueError("写入预约大小必须是非负整数。")
        try:
            path = self._existing_parent(destination)
            with self._lock:
                device, observed = self._snapshot(path)
                admitted = observed.effective_free >= observed.low + upper_bound_bytes
                if admitted:
                    self._reserved[device] = observed.reserved + upper_bound_bytes
        except OSError as exc:
            self._wake(destination)
            raise StorageCapacityError("无法确认存储空间，请稍后重试。") from exc
        if not admitted:
            self._wake(path, max(observed.target, observed.low + observed.reserved + upper_bound_bytes))
            raise StorageCapacityError()
        try:
            yield
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                self._wake(path, max(observed.target, observed.low + observed.reserved + upper_bound_bytes))
                raise StorageCapacityError() from exc
            raise
        finally:
            with self._lock:
                remaining = self._reserved.get(device, 0) - upper_bound_bytes
                if remaining:
                    self._reserved[device] = remaining
                else:
                    self._reserved.pop(device, None)


def capacity_reservation(capacity, destination, upper_bound_bytes):
    """Keep isolated library callers compatible; production injects one guard."""
    return (nullcontext() if capacity is None
        else capacity.try_reserve(destination, upper_bound_bytes))
