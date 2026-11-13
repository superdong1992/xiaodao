"""Deliver the Skill's bounded Markdown without evidence-based rewriting."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable

from problem_locator.contracts import (
    ErrorCode, ExecutionStage, GenericDiagnosisOutcomeV2,
)

from .failures import runtime_failure
from .generic_locator import MAX_GENERIC_REPORT_BYTES
from .report_text import read_report_text


def parse_skill_direct_response(
    text: str | None, *, skill_name: str, secrets: Iterable[bytes | str] = (),
) -> GenericDiagnosisOutcomeV2:
    """Accept presentation differences without checking the Skill's reasoning."""
    try:
        if not isinstance(text, str):
            raise ValueError("Skill response is missing")
        raw = text.encode("utf-8")
        status, body, report = read_report_text(
            text, marker="SKILL_DIAGNOSIS_RESULT_V1", max_bytes=MAX_GENERIC_REPORT_BYTES,
        )
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
            message="Skill 报告为空、超过长度限制或包含不可交付的内容。",
        ) from None
