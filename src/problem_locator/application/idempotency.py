"""Pure idempotency helpers for the application-service command pipeline.

The helpers are pure and therefore return an explicit internal decision.
Orchestrators map ``CONFLICT`` through the frozen ``ApplicationPortError`` at
the public Port boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel

from problem_locator.contracts.models import (
    BusinessReceipt,
    IdempotencyRecord,
    StateFile,
)
from problem_locator.contracts.serialization import business_request_sha256


class IdempotencyDisposition(Enum):
    """Internal lookup result; this is not a wire or persistence DTO."""

    NEW = "NEW"
    REPLAY = "REPLAY"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class IdempotencyDecision:
    disposition: IdempotencyDisposition
    request_hash: str
    record: IdempotencyRecord | None


def operation_name(command: BaseModel) -> str:
    """Return the frozen operation spelling used by business receipts."""

    return type(command).__name__


def record_key(operation: str, idempotency_key: str) -> str:
    return f"{operation}:{idempotency_key}"


def decide_idempotency(
    snapshot: StateFile,
    command: BaseModel,
) -> IdempotencyDecision:
    """Inspect one immutable snapshot without executing business work."""

    operation = operation_name(command)
    key = getattr(command, "idempotency_key")
    request_hash = business_request_sha256(command)
    existing = snapshot.idempotency_records.get(record_key(operation, key))
    if existing is None:
        return IdempotencyDecision(
            disposition=IdempotencyDisposition.NEW,
            request_hash=request_hash,
            record=None,
        )
    disposition = (
        IdempotencyDisposition.REPLAY
        if existing.request_hash == request_hash
        else IdempotencyDisposition.CONFLICT
    )
    return IdempotencyDecision(
        disposition=disposition,
        request_hash=request_hash,
        record=existing,
    )


def make_idempotency_record(
    command: BaseModel,
    request_hash: str,
    receipt: BusinessReceipt,
    *,
    case_id: str | None,
    created_at: str,
) -> IdempotencyRecord:
    """Build the S00 record stored atomically with the business mutation."""

    operation = operation_name(command)
    if receipt.operation != operation:
        raise ValueError("business receipt operation does not match the command")
    if receipt.case_id != case_id:
        raise ValueError("business receipt case_id does not match the record")
    command_case_id = getattr(command, "case_id", None)
    if command_case_id is not None and command_case_id != case_id:
        raise ValueError("record case_id does not match the command")
    return IdempotencyRecord(
        operation=operation,
        idempotency_key=getattr(command, "idempotency_key"),
        request_hash=request_hash,
        business_receipt=receipt,
        case_id=case_id,
        created_at=created_at,
    )


__all__ = [
    "IdempotencyDecision",
    "IdempotencyDisposition",
    "decide_idempotency",
    "make_idempotency_record",
    "operation_name",
    "record_key",
]
