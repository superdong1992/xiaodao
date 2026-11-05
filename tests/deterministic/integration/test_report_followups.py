"""Published reports enter the real follow-up source, HTTP and worker paths."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from problem_locator.contracts import ReviewPolicy
from problem_locator.followup.service import ReportFollowupService
from problem_locator.memory.models import FeedbackSource
from problem_locator.memory.store import MemoryStore
from problem_locator.runtime.agent_backend import BackendExecution
from tests.deterministic.integration.test_agent_reports import _finish_diagnosis, _get
from tests.deterministic.integration.test_direct_skill_delivery import direct_website, _finish
from tests.deterministic.integration.test_website_agent import OWNER_KEY, _post, _preupload, website
from tests.deterministic.integration.test_followup_generic_round import _GenericBackend, _NoFollowupModel, _publish


class GenericV2Backend:
    def execute(self, **kwargs):
        assert kwargs["backend_phase"] == "GENERIC"
        root = kwargs["workspace_root"]
        manifest = json.loads((root / "inputs/generic_logs.json").read_bytes())
        assert manifest["logs"]
        (root / "output/generic_diagnosis_result.md").write_text(
            "<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\n# 定位报告\n\n服务端连接池存在排队，需要继续核对容量。\n",
            encoding="utf-8", newline="\n")
        sinks = kwargs["log_sinks"]
        for sink in {id(sinks.stdout): sinks.stdout, id(sinks.stderr): sinks.stderr}.values():
            sink.flush()
            sink.close()
        return BackendExecution(returncode=0, stdout_stderr_bytes=0, workspace_bytes=0,
            elapsed_seconds=0.01, final_result="报告已保存。")


def _install(website):
    stack, store, _, agent, _ = website
    backend = _NoFollowupModel()
    followups = ReportFollowupService(agent, "claude", enabled=True, backend=backend)
    agent.followups, store.followup_store = followups, followups.store
    return followups, backend


def _old_rows(repository):
    with repository.database_read() as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not row[0].startswith("agent_followup_")]
        return {name: db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in tables}


def _exercise(website, followups, backend, cid, prefix, kind):
    stack, store, _, agent, client = website
    run_id = store.get_run(cid)["run_id"]
    detail = _get(client, prefix + "?include=report")
    result = detail["result"]
    assert result["format"] == "markdown" and result["report_state"] == "READY"
    source = followups._source(cid, run_id)
    assert source is not None and source.source_kind == kind and source.has_logs
    aggregate = stack.repository.read_case(source.case_id)
    assert source.problem_text == store.get_run(cid, run_id)["report_inputs"]["effective_problem_text"]
    assert source.report_markdown == result["markdown"]
    memory = MemoryStore(stack.repository, stack.clock)
    memory.put_feedback(cid, run_id, owner_key=OWNER_KEY, request_id="like-source", rating="LIKE",
        source=FeedbackSource(case_id=source.case_id, source_job_id=source.source_job_id,
            skill_name=aggregate.case.generic_result_v2.skill_name, problem_text=source.problem_text,
            report_markdown=source.report_markdown, report_sha256=source.report_sha256))
    report_url = f"/api/v1/artifacts/{result['artifact']['artifact_id']}/content?case_id={source.case_id}"
    original_report = client.get(report_url).content
    original_case = aggregate.model_dump_json()
    original_events = client.get(prefix + f"/events?run_id={run_id}").content
    original_workspace = stack.layout.workspaces / source.source_job_id
    name = "generic_logs.json" if kind == "GENERIC" else "target_logs.json"
    original_manifest = (original_workspace / "inputs" / name).read_bytes()
    assert followups.observe_report(cid, run_id)
    while followups.snapshot_worker.run_once():
        pass
    assert followups.store.snapshot(run_id)["status"] == "UNAVAILABLE"
    assert followups.store.enqueue_snapshot(cid, run_id, source)
    assert followups.snapshot_worker.run_once()
    path = prefix + f"/runs/{run_id}/followups"
    available = _get(client, path)
    assert available["can_ask"] and available["snapshot_status"] == "READY"
    questions = ["请解释报告的依据，以及当前还不能确定的地方。", "补充：另一实例 queue_depth=0，这如何影响此前判断？"]
    generic = _GenericBackend()
    stack.runtime._generic_locator_executor._backend = generic
    current_rid = run_id
    s = SimpleNamespace(stack=stack, store=store, agent=agent, client=client, cid=cid, prefix=prefix)
    for ordinal, question in enumerate(questions, 1):
        path = prefix + f"/runs/{current_rid}/followups"
        payload = {"request_id": f"followup-{ordinal}", "text": question}
        receipt = _post(client, path, payload)
        assert _post(client, path, payload) == receipt
        assert followups.worker.run_once()
        current = _get(client, path)
        assert len(current["items"]) == 1 and current["active_followup"] is None
        item = current["items"][0]
        assert item["followup_id"] == receipt["followup_id"] and item["status"] == "COMPLETED"
        assert item["run_id"] == current_rid and item["diagnosis_run_id"] != current_rid
        assert _post(client, path, payload) == receipt
        generic.report = f"## 本轮变化\n第 {ordinal} 次追问已重新核查。\n# 定位报告\n" + question
        detail = _publish(s)
        assert detail["run_id"] == item["diagnosis_run_id"]
        case = stack.repository.read_case(detail["case_id"])
        assert case.case.raw_problem_text == question
        assert all(job.diagnosis_mode.value == "GENERIC" and job.job_type.value == "DIAGNOSE"
            for job in case.jobs.values())
        assert store.get_run(cid)["force_generic"] is True
        reference = json.loads(generic.calls[-1]["followup_context_bytes"])
        if ordinal == 1:
            assert reference["original_problem"] == source.problem_text
        else:
            assert reference["prior_turns"][0]["text"] == questions[0]
            assert reference["prior_turns"][0]["diagnosis_run_id"] == current_rid
            assert reference["prior_reports"][0]["original_report"] == source.report_markdown
        events = client.get(path + "/events", headers={"Last-Event-ID": "1"})
        frames = [json.loads(frame[6:]) for frame in events.content.split(b"\n\n") if frame.startswith(b"data: ")]
        assert [frame["sequence"] for frame in frames] == [2, 3]
        current_rid = detail["run_id"]
    assert backend.calls == 0 and len(generic.calls) == 2
    assert followups.store.sessions.get(cid) is None
    assert client.get(report_url).content == original_report
    assert hashlib.sha256(original_report).hexdigest() == source.report_sha256
    assert stack.repository.read_case(source.case_id).model_dump_json() == original_case
    assert client.get(prefix + f"/events?run_id={run_id}").content == original_events
    assert (original_workspace / "inputs" / name).read_bytes() == original_manifest
    assert store.get_run(cid)["run_id"] != run_id
    versions = _get(client, prefix + f"/runs/{current_rid}/report-versions")
    assert [entry["version"] for entry in versions["versions"]] == [1, 2, 3]


def test_generic_source_two_generic_rounds_preserve_original_report_and_legacy_snapshot(website, monkeypatch):
    stack, store, _, agent, client = website
    followups, backend = _install(website)
    monkeypatch.setattr(stack.catalog, "_route_skill_refs", [])
    monkeypatch.setattr(stack.catalog, "_generic_logparse_product", "compact")
    monkeypatch.setattr(stack.runtime._generic_locator_executor, "_backend", GenericV2Backend())
    cid = agent.create_conversation("generic-followup", owner_key=OWNER_KEY).conversation_id
    prefix = f"/api/v1/agent/conversations/{cid}"
    attachment = _preupload(client, prefix)
    _post(client, prefix + "/messages", {"request_id": "problem", "text": "设备为何反复重启？", "attachment_ids": [attachment]})
    assert agent.run_once(cid)
    assert stack.scheduler.wait_until_idle(20)
    assert store.get_status(cid).case_status == "WAITING_ATTACHMENT"
    assert agent.run_once(cid)
    assert stack.scheduler.wait_until_idle(20)
    status = store.get_status(cid)
    assert status.report_state == "READY", stack.application.get_case(status.case_id).case_view.failure
    _exercise(website, followups, backend, cid, prefix, "GENERIC")


def test_skill_direct_source_two_generic_rounds_preserve_original_report_and_legacy_snapshot(direct_website):
    followups, backend = _install(direct_website.website)
    _, prefix = _finish(direct_website)
    cid = prefix.rsplit("/", 1)[-1]
    _exercise(direct_website.website, followups, backend, cid, prefix, "SKILL_DIRECT")


@pytest.mark.parametrize("policy", ["strict", "advisory"])
def test_published_structured_methods_reports_remain_unsupported_without_model_work(website, policy):
    stack, store, _, _, client = website
    followups, backend = _install(website)
    stack.runtime._methods_evidence_validation = policy
    stack.catalog._specialized_review_policy = ReviewPolicy.INDEPENDENT
    cid, prefix = _finish_diagnosis(website)
    assert stack.archive.run_once()
    run_id = store.get_run(cid)["run_id"]
    assert _get(client, prefix + "?include=report")["result"]["format"] == "problem-locator-diagnosis-v3"
    before = _old_rows(stack.repository)
    path = prefix + f"/runs/{run_id}/followups"
    value = _get(client, path)
    assert not value["can_ask"] and value["reason"] == "UNSUPPORTED"
    assert followups._source(cid, run_id) is None
    rejected = client.post(path, json={"request_id": "unsupported", "text": "解释这个报告"})
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "AGENT_FOLLOWUP_UNSUPPORTED"
    assert not followups.worker.run_once() and backend.calls == 0
    assert _old_rows(stack.repository) == before
