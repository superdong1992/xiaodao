"""The same HTTP entry answers first, then drives the existing diagnosis pipeline."""
import json
from types import SimpleNamespace

from problem_locator.followup.service import ReportFollowupService
from problem_locator.runtime.agent_backend import BackendExecution
from tests.deterministic.integration.test_website_agent import OWNER_KEY, _post, _preupload, website
from tests.deterministic.integration.test_website_report_continuation import _get, _upload_second_archive


def test_unified_followup_answers_then_diagnoses_new_logs_and_publishes_another_version(website, monkeypatch):
    stack, store, engine, agent, client = website
    monkeypatch.setattr(stack.catalog, "_route_skill_refs", [])
    monkeypatch.setattr(stack.catalog, "_generic_logparse_product", "compact")
    diagnoses = []
    def diagnose(**kwargs):
        root = kwargs["workspace_root"]
        logs = json.loads((root / "inputs/generic_logs.json").read_bytes())
        diagnoses.append((kwargs["prompt"], logs))
        report = ("# 原报告\n连接池等待仍待核实。" if len(diagnoses) == 1 else
            "## 本轮变化\n新增服务端日志支持连接池等待的判断。\n\n# 完整报告\n建议核对连接池容量。")
        (root / "output/generic_diagnosis_result.md").write_text(
            "<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\n" + report, encoding="utf-8")
        return BackendExecution(returncode=0, stdout_stderr_bytes=0, workspace_bytes=0,
            elapsed_seconds=0.01, final_result="报告已保存。")
    monkeypatch.setattr(stack.runtime._generic_locator_executor, "_backend", SimpleNamespace(execute=diagnose))
    followup_calls = []
    decision = {}
    def respond(**kwargs):
        followup_calls.append(json.loads((kwargs["workspace_root"] / "inputs/context.json").read_bytes()))
        return SimpleNamespace(final_result=json.dumps(decision, ensure_ascii=False) if decision else "原报告暂未确定根因。")
    followups = ReportFollowupService(agent, "claude", enabled=True, backend=SimpleNamespace(execute=respond))
    agent.followups, store.followup_store = followups, followups.store
    monkeypatch.setattr(engine, "prepare_continuation", lambda **kw: (_ for _ in ()).throw(
        AssertionError("unified followup must not run an intent classifier")), raising=False)
    cid = agent.create_conversation("unified-followup", owner_key=OWNER_KEY).conversation_id
    prefix = f"/api/v1/agent/conversations/{cid}"
    attachment = _preupload(client, prefix)
    _post(client, prefix + "/messages", {"request_id": "problem", "text": "设备为何反复重启？", "attachment_ids": [attachment]})
    for _ in range(4):
        agent.run_once(cid)
        assert stack.scheduler.wait_until_idle(20)
        if store.get_status(cid).report_state == "READY":
            break
    first = _get(client, prefix + "?include=report")
    assert first["result"]["report_state"] == "READY"
    parent = first["run_id"]
    original = stack.repository.read_case(first["case_id"]).model_dump_json()
    path = prefix + f"/runs/{parent}/followups"
    _post(client, path, {"request_id": "explain", "text": "这份报告确定根因了吗？"})
    assert followups.worker.run_once()
    assert _get(client, path)["items"][0]["diagnosis_run_id"] is None
    assert store.get_run(cid)["run_id"] == parent and len(diagnoses) == 1
    extra, _ = _upload_second_archive(client, cid)
    decision.update(action="CONTINUE_DIAGNOSIS",
        effective_problem_text=store.get_run(cid)["report_inputs"]["effective_problem_text"],
        diagnosis_goal="核对新增服务端日志能否确认根因。", reason="需解析新增日志并核对连接池等待。", log_mode="APPEND")
    request = {"request_id": "new-evidence", "text": "结合这些日志，现在能确定原因吗？", "attachment_ids": [extra]}
    receipt = _post(client, path, request)
    assert followups.worker.run_once()
    assert _post(client, path, request) == receipt
    handoff = _get(client, path)["items"][-1]
    assert handoff["status"] == "COMPLETED" and handoff["diagnosis_run_id"] != parent
    for _ in range(4):
        agent.run_once(cid)
        assert stack.scheduler.wait_until_idle(20)
        if store.get_status(cid).report_state == "READY":
            break
    current = _get(client, prefix + "?include=report")
    assert current["run_id"] == handoff["diagnosis_run_id"] and current["case_id"] != first["case_id"]
    assert current["result"]["report_state"] == "READY" and "本轮变化" in current["result"]["markdown"]
    assert len(followup_calls) == len(diagnoses) == 2
    assert followup_calls[1]["supplementary_log_attachments"][0]["attachment_id"] == extra
    assert len(diagnoses[1][1]["sources"]) == 2
    assert "连接池等待仍待核实" in diagnoses[1][0]
    assert stack.repository.read_case(first["case_id"]).model_dump_json() == original
    versions = _get(client, prefix + f"/runs/{current['run_id']}/report-versions")
    assert [version["version"] for version in versions["versions"]] == [1, 2]
    with stack.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM agent_continuation_drafts").fetchone()[0] == 0
