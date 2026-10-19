"""Only memory passed to a successful generic invocation can become a usage receipt."""
from types import SimpleNamespace

import pytest

from tests.deterministic.contracts.fakes import InMemoryCancellationSignal
from problem_locator.memory.retrieval import ExperienceRetriever
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.generic_locator import GenericLocatorExecutor
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    _GenericRuntimeBackend, _failure, _generic_runtime_fixture, _generic_v2_result_bytes,
)
from tests.deterministic.unit.runtime.test_experience_memory import Store, row


def run_executor(tmp_path, backend, selection, *, headroom=None):
    runtime, job, state, records = _generic_runtime_fixture(tmp_path, backend,
        experience_retriever=SimpleNamespace(select=lambda *args: selection))
    assets = runtime._resolve_assets(job)
    if headroom is not None:
        mandatory = len(GenericLocatorExecutor.build_prompt(job, assets).encode("utf-8"))
        build_prompt = runtime._generic_locator_executor.build_prompt
        padding = " " * (job.resource_limits.context_bytes - mandatory - headroom)
        runtime._generic_locator_executor.build_prompt = lambda *args, **kwargs: (
            padding + build_prompt(*args, **kwargs))
    result = runtime._generic_locator_executor.execute(job=job, aggregate=state.aggregate,
        assets=assets, resource_store=runtime._resource_store, cancellation=InMemoryCancellationSignal())
    return result, job, records


def selection():
    return ExperienceRetriever(Store(cards=[row()])).select("generic", "Redis连接超时")


def backend():
    return _GenericRuntimeBackend(None, v2_result_bytes=_generic_v2_result_bytes("# 本次诊断\n保持正式报告原文。"))


def test_success_returns_the_exact_selection_sent_to_the_model(tmp_path):
    memory, agent = selection(), backend()
    result, job, records = run_executor(tmp_path, agent, memory)
    assert result.memory_selection is memory
    assert memory.reference_text in agent.calls[0]["prompt"]
    assert memory.reference_text in result.prompt
    assert records.read_audit_bytes(job.job_id, "context.txt") == result.prompt.encode()
    assert len(agent.calls) == 1


@pytest.mark.parametrize("headroom", [0, 1, 2, 100])
def test_skipped_memory_is_not_returned_as_used(tmp_path, headroom):
    memory, agent = selection(), backend()
    result, _, _ = run_executor(tmp_path, agent, memory, headroom=headroom)
    assert result.memory_selection is None
    assert memory.reference_text not in result.prompt
    assert len(agent.calls) == 1


@pytest.mark.parametrize("selected", [None, SimpleNamespace(reference_text=""),
    SimpleNamespace(reference_text="x" * 4097)])
def test_missing_empty_and_oversized_memory_do_not_create_usage(tmp_path, selected):
    result, _, _ = run_executor(tmp_path, backend(), selected)
    assert result.memory_selection is None


@pytest.mark.parametrize("failure", ["backend", "invalid-report"])
def test_failed_execution_cannot_return_a_successful_usage_receipt(tmp_path, failure):
    agent = (_GenericRuntimeBackend(None, failure=_failure()) if failure == "backend"
        else _GenericRuntimeBackend(None, v2_result_bytes=b"invalid"))
    with pytest.raises(RuntimeExecutionError):
        run_executor(tmp_path, agent, selection())
    assert len(agent.calls) == 1
