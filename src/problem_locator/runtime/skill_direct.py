"""Deliver the Skill's bounded Markdown without evidence-based rewriting."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable

from problem_locator.contracts import (
    ErrorCode, ExecutionStage, GenericDiagnosisOutcomeV2, GenericResultStatus,
)

from .failures import runtime_failure
from .generic_locator import MAX_GENERIC_REPORT_BYTES


_HEADERS = {
    "<<<SKILL_DIAGNOSIS_RESULT_V1:RESOLVED>>>": GenericResultStatus.RESOLVED,
    "<<<SKILL_DIAGNOSIS_RESULT_V1:UNRESOLVED>>>": GenericResultStatus.UNRESOLVED,
}


def parse_skill_direct_response(
    text: str | None, *, skill_name: str, secrets: Iterable[bytes | str] = (),
) -> GenericDiagnosisOutcomeV2:
    """Validate only the delivery envelope; preserve every report body byte."""
    try:
        if not isinstance(text, str):
            raise ValueError("Skill response is missing")
        header, separator, body = text.partition("\n")
        status = _HEADERS.get(header.removesuffix("\r"))
        if not separator or status is None or not body.strip():
            raise ValueError("Skill report envelope is invalid")
        raw = text.encode("utf-8")
        report = body.encode("utf-8")
        if len(report) > MAX_GENERIC_REPORT_BYTES:
            raise ValueError("Skill report exceeds its byte limit")
        for secret in secrets:
            token = secret.encode("utf-8") if isinstance(secret, str) else secret
            if token and token in raw:
                raise ValueError("Skill response contains a private capability")
        return GenericDiagnosisOutcomeV2(
            format_version=2, status=status, report_markdown=body,
            report_utf8_size=len(report), report_sha256=hashlib.sha256(report).hexdigest(),
            skill_name=skill_name,
        )
    except (TypeError, UnicodeError, ValueError):
        raise runtime_failure(
            stage=ExecutionStage.OUTCOME_VALIDATE, code=ErrorCode.OUTCOME_INVALID,
            message="Skill 报告的状态包络、正文或长度不符合输出合同。",
        ) from None
