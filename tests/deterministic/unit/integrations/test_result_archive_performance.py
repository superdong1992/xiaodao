"""Bound duplicate byte work in real archive preparation, build and validation."""
from __future__ import annotations

import hashlib
import io
import json
import sys
import weakref
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import pytest

from problem_locator.contracts import UserResultPayloadV3
from problem_locator.integrations import result_archive
from tests.deterministic.unit.integrations.test_result_archive import PROBLEM_TIME, _logs, _report, _target


class _TrackedLines(list):
    """Allow observing copied-line lifetime without keeping a strong reference."""


class _SplitLifetime:
    def __init__(self):
        self.live_sources = 0
        self.peak_sources = 0
        self.live_bytes = 0
        self.peak_bytes = 0
        self.largest_source_bytes = 0

    def track(self, values):
        lines = _TrackedLines(values)
        retained = sys.getsizeof(lines) + sum(sys.getsizeof(line) for line in lines)
        self.live_sources += 1
        self.live_bytes += retained
        self.peak_sources = max(self.peak_sources, self.live_sources)
        self.peak_bytes = max(self.peak_bytes, self.live_bytes)
        self.largest_source_bytes = max(self.largest_source_bytes, retained)
        weakref.finalize(lines, self._released, retained)
        return lines

    def _released(self, retained):
        self.live_sources -= 1
        self.live_bytes -= retained


class _CountingBytes(bytes):
    def __new__(cls, content):
        value = super().__new__(cls, content)
        value.split_calls = 0
        return value

    def splitlines(self, keepends=False):
        self.split_calls += 1
        lines = super().splitlines(keepends)
        tracker = getattr(self, "split_tracker", None)
        return lines if tracker is None else tracker.track(lines)


def _inputs(finding_count=30):
    logs = tuple(replace(item, content=_CountingBytes(item.content + b"uncited noise\n" * 1000)) for item in _logs())
    value = _report(logs[0]).model_dump(mode="json")
    finding = value["findings"][0]
    value["findings"] = [{**finding, "statement": f"模型发现 {index}。"} for index in range(finding_count)]
    report = UserResultPayloadV3.model_validate(value)
    for item in logs:
        item.content.split_calls = 0
    return report, logs


@pytest.mark.parametrize("operation", ["prepare", "build", "validate"])
@pytest.mark.parametrize("finding_count", [1, 30], ids=["one-finding", "thirty-findings"])
def test_archive_reuses_source_hashes_and_repeated_citation_ranges(monkeypatch, operation, finding_count):
    report, logs = _inputs(finding_count)
    plain = tuple(replace(item, content=bytes(item.content)) for item in logs)
    expected_text = result_archive.render_result_text(report, target_logs=plain)
    existing = result_archive.build_result_archive(report, problem_time=PROBLEM_TIME, target_logs=plain)
    original_hash = hashlib.sha256
    full_hashes = [0] * len(logs)
    range_hashes = []
    cited = bytes(logs[0].content).splitlines(keepends=True)[0]

    def counted(raw=b"", *args, **kwargs):
        for index, item in enumerate(logs):
            if raw is item.content:
                full_hashes[index] += 1
        if raw == cited:
            range_hashes.append(raw)
        return original_hash(raw, *args, **kwargs)

    # Patch only this module's hashlib binding, not the shared standard library.
    monkeypatch.setattr(result_archive, "hashlib", SimpleNamespace(sha256=counted))
    if operation == "prepare":
        result = result_archive.prepare_result_archive(report, problem_time=PROBLEM_TIME, target_logs=logs)
        assert result.result_text == expected_text
        manifest = json.loads(result.manifest_json)
        assert [row["sha256"] for row in manifest["target_logs"]] == [
            original_hash(item.content).hexdigest() for item in logs
        ]
    elif operation == "build":
        assert result_archive.build_result_archive(report, problem_time=PROBLEM_TIME, target_logs=logs) == existing
    else:
        assert result_archive.validate_result_archive_bytes(
            existing, report=report, problem_time=PROBLEM_TIME, target_logs=logs,
        ) == expected_text
    assert [item.content.split_calls for item in logs] == [1, 0]
    assert full_hashes == [1, 1]
    assert len(range_hashes) == 1


def test_archive_range_cache_does_not_skip_validation_of_later_citations():
    report, logs = _inputs(2)
    value = report.model_dump(mode="json")
    value["findings"][1]["citations"][0]["raw_bytes_sha256"] = "0" * 64
    bad = UserResultPayloadV3.model_validate(value)
    with pytest.raises(ValueError, match="raw hash differs"):
        result_archive.prepare_result_archive(bad, problem_time=PROBLEM_TIME, target_logs=logs)
    assert logs[0].content.split_calls == 1


def test_archive_validation_still_rejects_a_different_encoding_of_identical_entries():
    report, logs = _inputs(2)
    canonical = result_archive.build_result_archive(report, problem_time=PROBLEM_TIME, target_logs=logs)
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(canonical), "r") as source:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for entry in source.infolist():
                info = result_archive._zip_info(entry.filename)
                info._compresslevel = 9
                archive.writestr(info, source.read(entry))
    alternate = stream.getvalue()
    assert alternate != canonical
    with pytest.raises(ValueError, match="canonical v2 encoding"):
        result_archive.validate_result_archive_bytes(
            alternate, report=report, problem_time=PROBLEM_TIME, target_logs=logs,
        )


def test_archive_hash_cache_is_discarded_between_preparations():
    report, logs = _inputs(2)
    first = result_archive.prepare_result_archive(report, problem_time=PROBLEM_TIME, target_logs=logs)
    changed = (replace(logs[0], content=bytes(logs[0].content) + b"new uncited bytes\n"), logs[1])
    second = result_archive.prepare_result_archive(report, problem_time=PROBLEM_TIME, target_logs=changed)
    assert first.logs[0].relative_path == second.logs[0].relative_path
    assert first.logs[0].sha256 != second.logs[0].sha256
    assert second.logs[0].sha256 == hashlib.sha256(changed[0].content).hexdigest()


@pytest.mark.parametrize("source_count", [2, 32], ids=["two-sources", "max-sources"])
def test_archive_releases_uncited_lines_before_splitting_the_next_source(
    monkeypatch, record_property, source_count,
):
    tracker = _SplitLifetime()
    template = _logs()[0]
    raw = b"x" * 31 + b"\n"
    logs = []
    for index in range(source_count):
        content = _CountingBytes(raw * 8192)
        content.split_tracker = tracker
        logs.append(replace(template, content=content, target=_target(
            index + 1, label=f"target-{index}", module="payment",
            process="payment-service", path=f"task/logs/target-{index}.log",
        )))
    logs = tuple(logs)
    plain = tuple(replace(item, content=bytes(item.content)) for item in logs)
    value = _report(plain[0]).model_dump(mode="json")
    finding = value["findings"][0]
    citation = finding["citations"][0]
    value["findings"] = [
        {**finding, "statement": f"目标日志 {index}。", "citations": [
            {**citation, "archive_name": item.target.archive_name},
        ]}
        for _repeat in range(3) for index, item in enumerate(logs)
    ]
    report = UserResultPayloadV3.model_validate(value)
    expected = result_archive.prepare_result_archive(report, problem_time=PROBLEM_TIME, target_logs=plain)
    original_hash = hashlib.sha256
    full_hashes = 0
    range_hashes = 0
    source_ids = {id(item.content) for item in logs}

    def counted(data=b"", *args, **kwargs):
        nonlocal full_hashes, range_hashes
        if id(data) in source_ids:
            full_hashes += 1
        elif data == raw:
            range_hashes += 1
        return original_hash(data, *args, **kwargs)

    monkeypatch.setattr(result_archive, "hashlib", SimpleNamespace(sha256=counted))
    actual = result_archive.prepare_result_archive(report, problem_time=PROBLEM_TIME, target_logs=logs)
    assert actual == expected
    assert [item.content.split_calls for item in logs] == [1] * source_count
    assert full_hashes == range_hashes == source_count
    assert tracker.peak_sources == 1
    assert tracker.peak_bytes == tracker.largest_source_bytes
    assert tracker.live_sources == tracker.live_bytes == 0
    record_property("source_count", source_count)
    record_property("raw_log_bytes", sum(len(item.content) for item in logs))
    record_property("peak_live_split_sources", tracker.peak_sources)
    record_property("peak_copied_line_bytes", tracker.peak_bytes)
    record_property("source_sha256_calls", full_hashes)
    record_property("range_sha256_calls", range_hashes)


def test_archive_semantic_factor_is_labeled_model_judgment_instead_of_confirmation():
    logs = _logs()
    strict = _report(logs[0])
    strict_text = result_archive.render_result_text(strict, target_logs=logs)
    strict_line = next(line for line in strict_text.splitlines() if "已确认[confirmed_factor]" in line)
    value = strict.model_dump(mode="json")
    value.update(status="PARTIAL", root_cause=None)
    value["completion_criteria_mapping"][0].update(status="UNKNOWN", explanation="尚未核对证据。")
    value["verification_rules"][0].update(rule_kind="SEMANTIC_CAUSALITY", status="SEMANTIC_ONLY")
    advisory = UserResultPayloadV3.model_validate(value)
    archive = result_archive.build_result_archive(advisory, problem_time=PROBLEM_TIME, target_logs=logs)
    text = result_archive.validate_result_archive_bytes(
        archive, report=advisory, problem_time=PROBLEM_TIME, target_logs=logs,
    )
    assert "已确认[confirmed_factor]" not in text
    assert strict_line.replace("已确认[", "模型判断[", 1) in text
    assert result_archive.render_result_text(strict, target_logs=logs) == strict_text


def test_unrelated_semantic_rule_does_not_relabel_a_verified_factor():
    logs = _logs()
    report = _report(logs[0])
    value = report.model_dump(mode="json")
    value["verification_rules"].append({**value["verification_rules"][0],
        "rule_id": "unrelated-semantic-judgment", "rule_kind": "SEMANTIC_CAUSALITY", "status": "SEMANTIC_ONLY"})
    with_unrelated = UserResultPayloadV3.model_validate(value)
    text = result_archive.render_result_text(with_unrelated, target_logs=logs)
    assert "已确认[confirmed_factor][CAUSE]" in text
    assert "模型判断[confirmed_factor]" not in text
