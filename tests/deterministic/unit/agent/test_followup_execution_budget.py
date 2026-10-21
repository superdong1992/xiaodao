"""Follow-up budgets reach execution without disabling cancellation or handoff."""
import threading

import pytest

from problem_locator.contracts.enums import ErrorCode, ExecutionStage
from problem_locator.followup.context import build_prompt
from problem_locator.followup.service import ReportFollowupService
from problem_locator.runtime.failures import runtime_failure
from tests.deterministic.unit.agent.test_report_followup import (
    OWNER, original_rows, submit, system, view,
)


def configure(system, **options):
    system.service.shutdown(0)
    system.service = ReportFollowupService(
        system.agent, "claude", enabled=True, backend=system.backend, **options)
    system.service._source = lambda *_: system.source


def test_default_budget_allows_a_six_minute_model_invocation(system):
    def model(invocation):
        # Deterministic model duration; this test does not wait six minutes.
        if invocation["resource_limits"].wall_time_seconds <= 360:
            raise runtime_failure(stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.BACKEND_TIMEOUT, message="simulated elapsed time: 360s", retryable=True)
    system.backend.action = model
    submit(system)
    assert system.service.worker.run_once()
    assert view(system).items[0].status == "COMPLETED"
    assert len(system.backend.calls) == 1
    assert system.backend.calls[0]["resource_limits"].wall_time_seconds == 600


@pytest.mark.parametrize("seconds", [1, 300, 900, 1800])
def test_configured_budget_reaches_the_followup_invocation(system, seconds):
    configure(system, timeout_seconds=seconds)
    submit(system)
    assert system.service.worker.run_once()
    assert system.backend.calls[0]["resource_limits"].wall_time_seconds == seconds
    assert system.backend.calls[0]["backend_phase"] == "REPORT_FOLLOWUP"


@pytest.mark.parametrize("seconds", [0, -1, 1801, True, False, 1.5, None, "600"])
def test_direct_service_cannot_bypass_the_timeout_range(system, seconds):
    with pytest.raises(ValueError, match="追问执行时间"):
        ReportFollowupService(system.agent, "claude", timeout_seconds=seconds)


def test_custom_budget_timeout_still_fails_without_retry_or_handoff(system):
    configure(system, timeout_seconds=900)
    before = original_rows(system)
    def timeout(invocation):
        assert invocation["resource_limits"].wall_time_seconds == 900
        raise runtime_failure(stage=ExecutionStage.BACKEND_EXECUTE,
            code=ErrorCode.BACKEND_TIMEOUT, message="simulated elapsed time: 900s", retryable=True)
    system.backend.action = timeout
    system.service.continue_diagnosis = lambda *_: pytest.fail("timeout is not a diagnosis decision")
    submit(system)
    assert not system.service.worker.run_once()
    answer = view(system).items[0]
    assert answer.status == "FAILED" and answer.answer_markdown is None
    assert not system.service.worker.run_once()
    assert len(system.backend.calls) == 1 and original_rows(system) == before


@pytest.mark.parametrize("stop_kind", ["user", "shutdown"])
def test_maximum_budget_keeps_prompt_cancellation_and_discards_late_answers(system, stop_kind):
    configure(system, timeout_seconds=1800)
    entered, cancelled = threading.Event(), threading.Event()
    def model(invocation):
        entered.set()
        signal = invocation["cancellation"]
        for _ in range(500):
            if signal.wait(0.01):
                cancelled.set()
                return
        pytest.fail("extended execution budget prevented cancellation")
    system.backend.action = model
    receipt = submit(system)
    thread = threading.Thread(target=system.service.worker.run_once)
    thread.start()
    try:
        assert entered.wait(5)
        if stop_kind == "user":
            system.service.stop(system.cid, system.rid, receipt.followup_id, "stop", owner_key=OWNER)
        else:
            system.service.shutdown(0)
        assert cancelled.wait(2)
    finally:
        system.service.worker.cancel(shutdown=True)
        thread.join(5)
    assert not thread.is_alive()
    answer = view(system).items[0]
    assert answer.status == ("CANCELLED" if stop_kind == "user" else "INTERRUPTED")
    assert answer.answer_markdown is None
    assert len(system.backend.calls) == 1


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
