"""Two budgeted calls through the production follow-up service and Agent backend.

The existing report and verified log archive are deterministic input fixtures;
neither diagnosis nor experience extraction launches another model here.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.followup.models import FollowupSource
from problem_locator.memory.models import FeedbackSource
from problem_locator.memory.store import MemoryStore
from problem_locator.runtime.agent_backend import AgentBackend
from tests.deterministic.unit.storage.test_state_repository import _open

OWNER = "a" * 64
NOW = "2026-09-24T00:00:00.000Z"
CASE_ID = "00000000-0000-0000-0000-000000000001"
JOB_ID = "00000000-0000-0000-0000-000000000011"
REPORT = "# 定位报告\n\n初步判断：请求可能在队列中等待，需要结合日志进一步核对。\n"
LOG = ("2026-09-24T00:00:00Z report_followup_probe incident_code=RF-INC-A17F6 "
       "trace_key=RF-TRACE-83D29 queue_depth=27 wait_ms=9400\n")
FIRST = ("请先用 Grep 在 inputs/logs 目录内搜索 report_followup_probe，再用 Read 阅读命中的日志文件。"
         "这条日志支持报告中的哪个判断？答案中原样引用日志里的 incident_code 和 trace_key，"
         "并在最后单独写 FOLLOWUP_MEMORY_A17F6，供下一次追问引用。")
SECOND = ("补充信息：同一时刻另一实例记录 queue_depth=0。请结合你上一条回答重新分析，"
          "说明这个补充对原判断有什么影响，保留上一条回答最后的标记，"
          "并原样引用上一条回答中的 trace_key 和本次补充的 queue_depth=0。")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class RecordingBackend(AgentBackend):
    """Capture evidence in memory while preserving production execution."""

    def __init__(self, command):
        super().__init__(command)
        self.calls = []

    def execute(self, **kwargs):
        root = kwargs["workspace_root"]
        context = json.loads((root / "inputs/context.json").read_text(encoding="utf-8"))
        self.calls.append({"context": context, "prompt": kwargs["prompt"],
            "file_access": kwargs.get("file_access"), "backend_phase": kwargs.get("backend_phase")})
        # Only this synthetic fixture captures errors; production discards raw model logs.
        class Trace(io.BytesIO):
            def close(self):
                pass
        trace = Trace()
        kwargs["log_sinks"] = kwargs["log_sinks"].model_copy(update={"stdout": trace})
        try:
            return super().execute(**kwargs)
        except Exception:
            for line in trace.getvalue().splitlines():
                event = json.loads(line)
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_result" and block.get("is_error"):
                        print("Synthetic follow-up tool failure:", block.get("content"))
            raise


def _diagnosis_rows(repository):
    with repository.database_read() as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if (row[0].startswith("agent_") and not row[0].startswith("agent_followup_"))
                  or row[0].startswith("memory_")]
        return {name: db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
                for name in sorted(tables)}


def _usage(command):
    from problem_locator.runtime.claude_command import prepare_claude_command
    argv = prepare_claude_command(command).argv
    root = Path(argv[argv.index("--usage-root") + 1])
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*.json"))]


@pytest.fixture(scope="module")
def system(tmp_path_factory):
    if os.environ.get("S08_REAL_REPORT_FOLLOWUP_GATE") != "1":
        pytest.skip("requires the explicitly selected real report-followup gate")
    from problem_locator.followup.service import ReportFollowupService

    command = os.environ["S08_REAL_REPORT_FOLLOWUP_AGENT_COMMAND"]
    root = tmp_path_factory.mktemp("real-report-followup")
    repository = _open(root / "data")
    clock = SimpleNamespace(now=lambda: NOW)
    agent_store = AgentStore(repository, clock=clock, runtime_epoch="real-followup-gate")
    memory = MemoryStore(repository, clock)
    agent_store.memory_store = memory
    created = agent_store.create_conversation("followup-real-create", owner_key=OWNER)
    with repository.database_transaction() as db:
        db.execute("UPDATE agent_conversation_runs SET case_id=? WHERE run_id=?", (CASE_ID, created.run_id))
    source = FollowupSource(case_id=CASE_ID, source_job_id=JOB_ID,
        problem_text="订单支付成功后页面仍显示处理中，请核对队列。",
        report_markdown=REPORT, report_sha256=_sha256(REPORT.encode()),
        source_kind="GENERIC", has_logs=True, occurred_at=NOW)
    memory.put_feedback(created.conversation_id, created.run_id, owner_key=OWNER,
        request_id="followup-real-like", rating="LIKE", source=FeedbackSource(
            case_id=CASE_ID, source_job_id=JOB_ID, skill_name="generic-real-followup-fixture",
            problem_text=source.problem_text, report_markdown=REPORT, report_sha256=source.report_sha256))
    original = repository.layout.workspaces / JOB_ID
    logs = original / "inputs/generic-logs"
    logs.mkdir(parents=True)
    (logs / "log-000001.log").write_bytes(LOG.encode())
    manifest = json.dumps({"schema_version": 1, "logs": [{
        "log_path": "inputs/generic-logs/log-000001.log", "size": len(LOG.encode()),
        "sha256": _sha256(LOG.encode())}]}, sort_keys=True).encode()
    (original / "inputs/generic_logs.json").write_bytes(manifest)
    archive = repository.layout.jobs / JOB_ID
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "generic_logs.json").write_bytes(manifest)
    report_file = archive / "report-fixture.md"
    report_file.write_bytes(REPORT.encode())
    agent = AgentConversationService(agent_store, None, None, repository.layout,
        memory_store=memory, memory_enabled=True)
    backend = RecordingBackend(command)
    service = ReportFollowupService(agent, command, enabled=True, backend=backend)
    # Source publication is fixed test input. The service, durable store,
    # snapshot worker, context builder, prompt and model backend are production.
    service._source = lambda cid, rid: source if (cid, rid) == (created.conversation_id, created.run_id) else None
    before = _diagnosis_rows(repository)
    service.observe_report(created.conversation_id, created.run_id)
    assert service.snapshot_worker.run_once()
    assert service.get(created.conversation_id, created.run_id, owner_key=OWNER).snapshot_status == "READY"
    value = SimpleNamespace(repository=repository, service=service, created=created, backend=backend,
        source=source, command=command, before=before, original=original, report_file=report_file)
    yield value
    repository.close()


def _answer(system, request_id, text):
    cid, rid = system.created.conversation_id, system.created.run_id
    receipt = system.service.submit(cid, rid, request_id, text, owner_key=OWNER)
    assert system.service.submit(cid, rid, request_id, text, owner_key=OWNER) == receipt
    assert system.service.worker.run_once()
    view = system.service.get(cid, rid, owner_key=OWNER)
    item = next(item for item in view.items if item.followup_id == receipt.followup_id)
    assert item.status == "COMPLETED", item.failure
    assert item.context_mode == "REPORT_AND_LOGS"
    assert len(system.backend.calls) == item.ordinal
    assert system.report_file.read_bytes() == REPORT.encode()
    assert (system.original / "inputs/generic-logs/log-000001.log").read_bytes() == LOG.encode()
    assert _diagnosis_rows(system.repository) == system.before
    return item


@pytest.fixture(scope="module")
def first_answer(system):
    return _answer(system, "followup-real-first", FIRST)


def test_real_followup_reads_and_searches_the_frozen_original_log(system, first_answer):
    assert "RF-INC-A17F6" in first_answer.answer_markdown
    assert "RF-TRACE-83D29" in first_answer.answer_markdown
    assert "FOLLOWUP_MEMORY_A17F6" in first_answer.answer_markdown
    call = system.backend.calls[0]
    assert call["context"]["question"] == FIRST
    assert call["context"]["prior_turns"] == []
    assert call["file_access"] == "read-search" and call["backend_phase"] == "REPORT_FOLLOWUP"
    assert "RF-INC-A17F6" not in call["prompt"] and "RF-TRACE-83D29" not in call["prompt"]
    receipts = _usage(system.command)
    assert len(receipts) == 1
    trace = receipts[0]["tool_trace_audit"]
    assert trace["status"] == "PASS" and trace["log_reads"] >= 1 and trace["log_searches"] >= 1


def test_real_followup_uses_the_previous_answer_and_new_text_without_rewriting_diagnosis(system, first_answer):
    second = _answer(system, "followup-real-second", SECOND)
    assert "FOLLOWUP_MEMORY_A17F6" in second.answer_markdown
    assert "RF-TRACE-83D29" in second.answer_markdown
    assert "queue_depth=0" in second.answer_markdown
    context = system.backend.calls[1]["context"]
    assert context["question"] == SECOND
    assert context["prior_turns"] == [{"question": FIRST, "answer": first_answer.answer_markdown, "status": "COMPLETED"}]
    receipts = _usage(system.command)
    assert len(receipts) == 2
    assert all(item["workflow"] == "report-followup" and item["wrapper_outcome"]["status"] == "PASS" for item in receipts)
    cid, rid = system.created.conversation_id, system.created.run_id
    before_calls = len(system.backend.calls)
    refreshed = system.service.get(cid, rid, owner_key=OWNER)
    assert [item.followup_id for item in refreshed.items] == [first_answer.followup_id, second.followup_id]
    assert len(system.backend.calls) == before_calls == 2
    audit = {"schema_version": 1, "status": "PASS", "model_invocations": 2,
        "production_worker": f"{type(system.service.worker).__module__}.{type(system.service.worker).__name__}",
        "source_kind": "GENERIC", "source_report_sha256": system.source.report_sha256,
        "source_log_sha256": _sha256(LOG.encode()), "original_report_unchanged": True,
        "diagnosis_and_feedback_rows_unchanged": True, "prior_answer_preserved": True,
        "new_text_used": True, "log_tool_evidence_verified": True,
        "input_manifest_sha256": [item["tool_trace_audit"]["input_manifest_sha256"] for item in receipts]}
    Path(os.environ["S08_REAL_REPORT_FOLLOWUP_AUDIT_PATH"]).write_text(
        json.dumps(audit, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
