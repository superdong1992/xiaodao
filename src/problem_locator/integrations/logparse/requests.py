"""S07-private, deliberately narrow broker request protocol.

These request models are transport-local and are never persisted as S00
business DTOs.  Their field sets are the exact ones frozen by S07; notably,
neither request accepts ``logparse_product`` or arbitrary CLI arguments.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from problem_locator.contracts import OpaqueId, UtcTimestamp


_SAFE_PROPOSAL_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_UTC_TIMESTAMP = TypeAdapter(UtcTimestamp)
_MAX_REQUEST_BASE64_CHARS = ((2_000_000 + 2) // 3) * 4


class _PrivateWireModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_assignment=True,
        str_strip_whitespace=False,
    )


class Anchor(_PrivateWireModel):
    label: Annotated[str, Field(min_length=1, max_length=64)]
    module: Annotated[str, Field(min_length=1, max_length=128)]
    slot: Annotated[str, Field(min_length=1, max_length=128)]
    process_name: Annotated[str, Field(min_length=1, max_length=256)]
    pid: Annotated[str, Field(min_length=1, max_length=128)] | None

    @model_validator(mode="after")
    def reject_unsafe_text(self) -> Anchor:
        for value in (self.label, self.module, self.slot, self.process_name, self.pid):
            if value is not None and (
                value != value.strip()
                or any(character in value for character in "\r\n\x00")
                or not value.isascii()
            ):
                raise ValueError("anchor values must be single-line canonical ASCII")
        return self


class _BaseRequest(_PrivateWireModel):
    schema_version: Literal[1]
    problem_time: str
    anchors: Annotated[list[Anchor], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def validate_common_fields(self) -> _BaseRequest:
        _UTC_TIMESTAMP.validate_python(self.problem_time)
        keys = [
            (anchor.label, anchor.module, anchor.slot, anchor.process_name, anchor.pid)
            for anchor in self.anchors
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("anchors must be unique and retain declaration order")
        return self


class ParseTargetsRequest(_BaseRequest):
    attachment_id: OpaqueId
    artifact_proposal_key: Annotated[str, Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def validate_proposal_key(self) -> ParseTargetsRequest:
        if _SAFE_PROPOSAL_KEY.fullmatch(self.artifact_proposal_key) is None:
            raise ValueError("artifact_proposal_key is not a safe path segment")
        return self


class TargetLogsRequest(_BaseRequest):
    artifact_id: OpaqueId


class BrokerEnvelope(_PrivateWireModel):
    schema_version: Literal[1]
    operation: Literal["parse-targets", "target-logs"]
    request_path: Annotated[str, Field(min_length=1, max_length=512)]
    result_path: Annotated[str, Field(min_length=1, max_length=512)]
    request_base64: Annotated[
        str,
        Field(min_length=1, max_length=_MAX_REQUEST_BASE64_CHARS),
    ]


Request: type = ParseTargetsRequest | TargetLogsRequest


__all__ = [
    "Anchor",
    "BrokerEnvelope",
    "ParseTargetsRequest",
    "TargetLogsRequest",
]
