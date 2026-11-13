"""Read model report framing without rejecting usable Markdown."""
from __future__ import annotations

import re

from problem_locator.contracts import GenericResultStatus


_OUTER_FENCE = re.compile(
    r"\A[ \t\r\n]*(?P<fence>`{3,}|~{3,})(?:markdown|md|text)?[ \t]*\r?\n"
    r"(?P<body>[\s\S]*?)^(?P=fence)[ \t]*(?:\r?\n)?[ \t\r\n]*\Z",
    re.MULTILINE | re.IGNORECASE,
)


def read_report_text(text: str, *, marker: str, max_bytes: int) -> tuple[GenericResultStatus, str, bytes]:
    """Remove presentation framing only; an absent status stays UNRESOLVED."""
    text = text.lstrip("\ufeff")
    header_pattern = re.compile(
        r"\A[ \t\r\n]*<<<" + re.escape(marker) + r":([^>\r\n]*)>>>[ \t]*(?:\r\n|\n|\r|\Z)",
        re.IGNORECASE,
    )
    fence = _OUTER_FENCE.fullmatch(text)
    if fence is not None and header_pattern.match(fence.group("body")):
        text = fence.group("body")
    header = header_pattern.match(text)
    status = GenericResultStatus.UNRESOLVED
    body = text
    if header is not None:
        if header.group(1).strip().upper() == "RESOLVED":
            status = GenericResultStatus.RESOLVED
        body = text[header.end():]
    body = body.lstrip("\ufeff")
    report = body.encode("utf-8")
    if not body.strip() or len(report) > max_bytes:
        raise ValueError("Report is empty or exceeds its byte limit")
    return status, body, report
