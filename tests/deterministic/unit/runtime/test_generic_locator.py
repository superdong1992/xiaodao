from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.generic_locator import (
    GENERIC_RESULT_V2_FILENAME,
    MAX_GENERIC_REPORT_BYTES,
    parse_generic_result,
)


def _workspace(tmp_path, raw):
    output = tmp_path / "output"
    output.mkdir()
    (output / GENERIC_RESULT_V2_FILENAME).write_bytes(raw)
    metadata = output.stat()
    return SimpleNamespace(root=tmp_path, output_device=metadata.st_dev, output_inode=metadata.st_ino)


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
@pytest.mark.parametrize("status", ["RESOLVED", "UNRESOLVED"])
def test_generic_protocol_header_accepts_bom_and_crlf_without_touching_full_size_body(tmp_path, bom, ending, status):
    prefix = '# 诊断报告\r\n\n```json\n{"evidence":"原文"}\n```\r\n'.encode('utf-8')
    body = prefix + b"x" * (MAX_GENERIC_REPORT_BYTES - len(prefix))
    raw = bom + f"<<<GENERIC_DIAGNOSIS_RESULT_V2:{status}>>>".encode() + ending + body
    workspace = _workspace(tmp_path, raw)
    result = parse_generic_result(workspace, skill_name="generic-diagnosis")
    assert result.status.value == status
    assert result.report_markdown.encode('utf-8') == body
    assert result.report_utf8_size == MAX_GENERIC_REPORT_BYTES
    assert result.report_sha256 == hashlib.sha256(body).hexdigest()
    assert (workspace.root / "output" / GENERIC_RESULT_V2_FILENAME).read_bytes() == raw


@pytest.mark.parametrize("raw", [
    b"\xef\xbb\xbf\xef\xbb\xbf<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\nreport",
    b"<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\n\xef\xbb\xbfreport",
    b"<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\rreport",
    b"```markdown\n<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\nreport\n```",
    b"\xef\xbb\xbf<<<GENERIC_DIAGNOSIS_RESULT_V2:UNRESOLVED>>>\r\n" + b"x" * (MAX_GENERIC_REPORT_BYTES + 1),
], ids=["duplicate-bom", "body-bom", "bare-cr", "whole-report-fence", "oversize-body"])
def test_generic_header_compatibility_does_not_relax_body_or_protocol_boundaries(tmp_path, raw):
    with pytest.raises(RuntimeExecutionError):
        parse_generic_result(_workspace(tmp_path, raw), skill_name="generic-diagnosis")
