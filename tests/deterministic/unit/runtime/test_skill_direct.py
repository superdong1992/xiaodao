"""The framework checks the report envelope, not the Skill's diagnosis."""
import hashlib

import pytest

from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.skill_direct import parse_skill_direct_response


@pytest.mark.parametrize("status", ["RESOLVED", "UNRESOLVED"])
def test_direct_report_keeps_skill_status_and_exact_body_without_citations(status):
    body = "# 定位结果\r\n\r\nSkill 的判断：服务端处理耗时过长。  \r\n\n未附行号、marker 或身份词。\n"
    outcome = parse_skill_direct_response(
        f"<<<SKILL_DIAGNOSIS_RESULT_V1:{status}>>>\r\n" + body,
        skill_name="rpc-timeout",
    )
    assert outcome.status.value == status
    assert outcome.report_markdown == body
    assert outcome.report_utf8_size == len(body.encode("utf-8"))
    assert outcome.report_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert "证据不足" not in outcome.report_markdown


@pytest.mark.parametrize("text", [
    None, "", "<<<SKILL_DIAGNOSIS_RESULT_V1:RESOLVED>>>",
    "<<<SKILL_DIAGNOSIS_RESULT_V1:RESOLVED>>>\n \n",
    "<<<SKILL_DIAGNOSIS_RESULT_V1:RESOLVED>>>\n" + "中" * 21846,
], ids=["none", "empty", "missing-body", "blank-body", "oversized-utf8"])
def test_invalid_delivery_protocol_is_an_error_not_an_evidence_conclusion(text):
    with pytest.raises(RuntimeExecutionError) as rejected:
        parse_skill_direct_response(text, skill_name="rpc-timeout")
    assert rejected.value.failure.code.value == "OUTCOME_INVALID"


@pytest.mark.parametrize("prefix,suffix,status", [
    ("", "", "UNRESOLVED"),
    ("\ufeff<<<SKILL_DIAGNOSIS_RESULT_V1:RESOLVED>>>\n", "", "RESOLVED"),
    ("\n  <<<SKILL_DIAGNOSIS_RESULT_V1: resolved >>>\r", "", "RESOLVED"),
    ("```markdown\n<<<SKILL_DIAGNOSIS_RESULT_V1:RESOLVED>>>\n", "```\n", "RESOLVED"),
    ("<<<SKILL_DIAGNOSIS_RESULT_V1:PARTIAL>>>\n", "", "UNRESOLVED"),
])
def test_report_presentation_variants_keep_content_without_exception(caplog, prefix, suffix, status):
    body = "# 定位报告\n\n已有分析。\n```text\n证据原文\n```\n"
    result = parse_skill_direct_response(prefix + body + suffix, skill_name="rpc-timeout")
    assert result.report_markdown == body
    assert result.status.value == status
    assert not caplog.records


def test_plain_report_keeps_an_intentional_full_body_code_block():
    body = "```text\n完整报告只有这一段代码。\n```\n"
    result = parse_skill_direct_response(body, skill_name="rpc-timeout")
    assert result.report_markdown == body
    assert result.status.value == "UNRESOLVED"


def test_private_capability_cannot_be_delivered_as_skill_text():
    with pytest.raises(RuntimeExecutionError):
        parse_skill_direct_response(
            "<<<SKILL_DIAGNOSIS_RESULT_V1:RESOLVED>>>\nprivate-token",
            skill_name="rpc-timeout", secrets=(b"private-token",),
        )
