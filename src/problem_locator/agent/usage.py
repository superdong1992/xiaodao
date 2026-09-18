"""Short registration leases; deletion never races a reader or an intake call."""
from __future__ import annotations

import threading
from contextlib import contextmanager

from .models import AgentStoreError


class ConversationUsageGuard:
    def __init__(self):
        self._lock = threading.Lock()
        self._users: dict[str, int] = {}
        self._cleaning: set[str] = set()

    @contextmanager
    def acquire(self, conversation_id):
        with self._lock:
            if conversation_id in self._cleaning:
                raise AgentStoreError("CONVERSATION_NOT_FOUND", "会话不存在或已删除。", 404)
            self._users[conversation_id] = self._users.get(conversation_id, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                remaining = self._users[conversation_id] - 1
                if remaining:
                    self._users[conversation_id] = remaining
                else:
                    self._users.pop(conversation_id)

    def active(self, conversation_id):
        with self._lock:
            return bool(self._users.get(conversation_id))

    def acquire_cleanup_if_idle(self, conversation_id):
        with self._lock:
            if self._users.get(conversation_id) or conversation_id in self._cleaning:
                return None
            self._cleaning.add(conversation_id)
        return self._cleanup_lease(conversation_id)

    @contextmanager
    def _cleanup_lease(self, conversation_id):
        try:
            yield
        finally:
            with self._lock:
                self._cleaning.remove(conversation_id)
