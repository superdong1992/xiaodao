"""Length-preserving streaming redaction for job-scoped broker secrets."""

from __future__ import annotations

from collections.abc import Iterable

from problem_locator.contracts import AppendOnlyByteSink


_REDACTION_BYTE = ord("*")


class StreamingSecretRedactor:
    """Redact exact binary secrets before bytes reach an execution log sink.

    The redactor retains at most ``max_secret_length - 1`` uncommitted source
    bytes, allowing exact matches that cross arbitrary input chunk boundaries.
    Every matching byte is replaced by one ASCII ``*`` so downstream byte
    accounting remains unchanged.
    """

    def __init__(
        self,
        secrets: Iterable[bytes | str],
        sink: AppendOnlyByteSink,
        *,
        close_sink: bool = True,
    ) -> None:
        if not isinstance(sink, AppendOnlyByteSink):
            raise TypeError("sink must implement AppendOnlyByteSink")
        patterns: list[bytes] = []
        seen: set[bytes] = set()
        for secret in secrets:
            if isinstance(secret, str):
                encoded = secret.encode("utf-8")
            elif isinstance(secret, bytes):
                encoded = secret
            else:
                raise TypeError("secret patterns must be bytes or strings")
            if not encoded:
                raise ValueError("secret patterns must be non-empty")
            if encoded not in seen:
                seen.add(encoded)
                patterns.append(encoded)

        self._sink = sink
        self._patterns = tuple(patterns)
        self._maximum_pattern_bytes = max((len(item) for item in patterns), default=0)
        self._pending = bytearray()
        self._pending_mask = bytearray()
        self._close_sink = close_sink
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def _mark_matches(self) -> None:
        if not self._patterns or not self._pending:
            return
        source = bytes(self._pending)
        for pattern in self._patterns:
            start = 0
            while True:
                found = source.find(pattern, start)
                if found < 0:
                    break
                self._pending_mask[found : found + len(pattern)] = b"\x01" * len(
                    pattern
                )
                start = found + 1

    def _emit(self, count: int) -> None:
        if count <= 0:
            return
        payload = bytes(
            _REDACTION_BYTE if masked else value
            for value, masked in zip(
                self._pending[:count],
                self._pending_mask[:count],
                strict=True,
            )
        )
        self._sink.write(payload)
        del self._pending[:count]
        del self._pending_mask[:count]

    def write(self, chunk: bytes) -> None:
        if self._closed:
            raise ValueError("redactor is closed")
        if not isinstance(chunk, bytes) or not chunk:
            raise ValueError("write requires non-empty bytes")
        if not self._patterns:
            self._sink.write(chunk)
            return

        self._pending.extend(chunk)
        self._pending_mask.extend(b"\x00" * len(chunk))
        self._mark_matches()
        retained = self._maximum_pattern_bytes - 1
        self._emit(max(0, len(self._pending) - retained))

    def flush(self) -> None:
        if self._closed:
            return
        # The possible cross-chunk tail must remain pending until close.
        self._sink.flush()

    def _forget_sensitive_state(self) -> None:
        for index in range(len(self._pending)):
            self._pending[index] = 0
        self._pending.clear()
        self._pending_mask.clear()
        self._patterns = ()
        self._maximum_pattern_bytes = 0

    def close(self) -> None:
        if self._closed:
            return

        primary_error: BaseException | None = None
        try:
            self._emit(len(self._pending))
            self._sink.flush()
        except BaseException as exc:  # preserve the all-or-error sink failure
            primary_error = exc

        try:
            if self._close_sink:
                self._sink.close()
        except BaseException as exc:
            if primary_error is None:
                primary_error = exc
        finally:
            self._closed = True
            self._forget_sensitive_state()

        if primary_error is not None:
            raise primary_error


__all__ = ["StreamingSecretRedactor"]
