"""Legacy followup settings never add a model gate or weaken handoff cancellation."""
import threading

import pytest

from problem_locator.contracts.enums import ErrorCode, ExecutionStage
from problem_locator.followup.context import build_prompt
from problem_locator.followup.service import ReportFollowupService
from problem_locator.runtime.failures import runtime_failure
from tests.deterministic.unit.agent.test_report_followup import (
    OWNER, diagnostic_system, original_rows, submit, system, view,
)


def configure(system, **options):
    system.service.shutdown(0)
    system.service = ReportFollowupService(
        system.agent, "claude", enabled=True, backend=system.backend, **options)
    system.service._source = lambda *_: system.source


def test_default_budget_does_not_create_an_extra_followup_model_invocation(diagnostic_system):
    s = diagnostic_system
    assert s.service.timeout_seconds == 600
    s.backend.execute = lambda **kwargs: pytest.fail("diagnosis owns model work")
    submit(s)
    assert s.service.worker.run_once()
    assert view(s).items[0].diagnosis_run_id is not None
    assert not s.backend.calls


@pytest.mark.parametrize("seconds", [1, 300, 900, 1800])
def test_legacy_timeout_setting_does_not_add_a_followup_model_gate(diagnostic_system, seconds):
    s = diagnostic_system
    s.service.timeout_seconds = seconds
    s.backend.execute = lambda **kwargs: pytest.fail("no budget spent on classification")
    submit(s)
    assert s.service.worker.run_once()
    assert view(s).items[0].diagnosis_run_id is not None
    assert not s.backend.calls


@pytest.mark.parametrize("seconds", [0, -1, 1801, True, False, 1.5, None, "600"])
def test_direct_service_cannot_bypass_the_timeout_range(system, seconds):
    with pytest.raises(ValueError, match="追问执行时间"):
        ReportFollowupService(system.agent, "claude", timeout_seconds=seconds)


def test_dispatch_timeout_fails_safely_without_retry_or_model(system, monkeypatch):
    before = original_rows(system)
    attempts = []
    def timeout(task):
        attempts.append(task["followup_id"])
        raise TimeoutError("private dispatch timeout")
    monkeypatch.setattr(system.service, "dispatch_generic", timeout)
    submit(system)
    assert not system.service.worker.run_once()
    item = view(system).items[0]
    assert item.status == "FAILED" and item.answer_markdown is None
    assert not system.service.worker.run_once()
    assert len(attempts) == 1 and not system.backend.calls
    assert original_rows(system) == before


@pytest.mark.parametrize("stop_kind", ["user", "shutdown"])
def test_stop_during_dispatch_preparation_cannot_commit_a_late_handoff(diagnostic_system, stop_kind, monkeypatch):
    s = diagnostic_system
    s.service.timeout_seconds = 1800
    entered, release = threading.Event(), threading.Event()
    baseline = s.agent.continuations._baseline
    def pause(*args, **kwargs):
        result = baseline(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(s.agent.continuations, "_baseline", pause)
    receipt = submit(s)
    thread = threading.Thread(target=s.service.worker.run_once)
    thread.start()
    try:
        assert entered.wait(5)
        if stop_kind == "user":
            s.service.stop(s.cid, s.rid, receipt.followup_id, "stop", owner_key=OWNER)
        else:
            s.service.shutdown(0)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    item = view(s).items[0]
    assert item.status == ("CANCELLED" if stop_kind == "user" else "INTERRUPTED")
    assert item.diagnosis_run_id is None and item.answer_markdown is None
    assert s.store.get_conversation(s.cid).run_id == s.rid
    assert not s.backend.calls


def test_log_prompt_requires_targeted_reading_and_early_diagnosis_handoff(system):
    prompt, _ = build_prompt(system.source, [], "请核对故障时间线。", "REPORT_AND_LOGS", read_search=True)
    for rule in (
        "现有报告和问答已经足以回答时，不再读取日志",
        "先用 Grep 定位，再用 Read 读取命中附近的片段",
        "显式限制搜索结果条数和读取行数",
        "不要重复读取已核对的相同片段",
        "重建跨文件时间线",
        "不要先在追问中完成这项深入分析",
        "截断或未命中不能证明日志中不存在相关事件",
        "不能仅因耗时或检索次数增加就启动诊断",
    ):
        assert rule in prompt


def test_report_only_prompt_does_not_offer_raw_log_access(system):
    prompt, _ = build_prompt(system.source, [], "解释报告即可。", "REPORT_ONLY", read_search=True)
    assert "本次没有可用的原始日志副本" in prompt
    assert "先用 Grep 定位，再用 Read" not in prompt
    assert "缺少只能由用户提供的信息或日志" in prompt
