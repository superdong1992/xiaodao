"""Every report question enters Generic diagnosis without a preliminary model."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from problem_locator.contracts import DiagnosisMode, JobType
from problem_locator.followup.service import ReportFollowupService
from problem_locator.runtime.agent_backend import BackendExecution
from tests.deterministic.integration.test_website_agent import OWNER_KEY, _post, _preupload, website
from tests.deterministic.integration.test_website_report_continuation import _get, _upload_second_archive


OLD_REPORT = "# 原报告\n客户端连接池等待只是待验证的线索。\n"
NEW_REPORT = "## 本轮变化\n本轮已围绕新的追问检查材料。\n\n# 完整报告\n请核对服务端连接池。\n"
MISSING_REPORT = "## 本轮变化\n当前材料不足以确认新问题。\n\n# 诊断结果\n请补充服务端故障时段日志，暂不能确认根因。\n"


class _GenericBackend:
    def __init__(self):
        self.calls = []
        self.status, self.report = "RESOLVED", OLD_REPORT

    def execute(self, **kwargs):
        assert kwargs["backend_phase"] == "GENERIC"
        root = kwargs["workspace_root"]
        manifest = root / "inputs/generic_logs.json"
        reference = root / "inputs/followup-context.json"
        reference_bytes = reference.read_bytes() if reference.exists() else None
        if reference_bytes is not None:
            assert reference.stat().st_mode & 0o222 == 0, "追问背景必须只读。"
        self.calls.append({"prompt": kwargs["prompt"],
            "logs": json.loads(manifest.read_bytes()) if manifest.exists() else None,
            "followup_context_bytes": reference_bytes})
        (root / "output/generic_diagnosis_result.md").write_text(
            f"<<<GENERIC_DIAGNOSIS_RESULT_V2:{self.status}>>>\n{self.report}", encoding="utf-8")
        return BackendExecution(returncode=0, stdout_stderr_bytes=0, workspace_bytes=0,
            elapsed_seconds=0.01, final_result="报告已保存。")


class _NoFollowupModel:
    def __init__(self):
        self.calls = 0

    def execute(self, **_kwargs):
        self.calls += 1
        raise AssertionError("每次追问必须直接诊断，不能先调用 REPORT_FOLLOWUP 模型。")


def _publish(s):
    for _ in range(8):
        s.agent.run_once(s.cid)
        assert s.stack.scheduler.wait_until_idle(20)
        if s.store.get_status(s.cid).report_state == "READY":
            return _get(s.client, s.prefix + "?include=report")
    pytest.fail(str(s.store.get_status(s.cid)))


@pytest.fixture
def generic_round(website, monkeypatch):
    stack, store, engine, agent, client = website
    routes = list(stack.catalog._route_skill_refs)
    assert routes, "需要已配置的专项路由，才能证明追问跳过 ROUTE。"
    monkeypatch.setattr(stack.catalog, "_route_skill_refs", [])
    backend, preliminary = _GenericBackend(), _NoFollowupModel()
    monkeypatch.setattr(stack.runtime._generic_locator_executor, "_backend", backend)
    followups = ReportFollowupService(agent, "claude", enabled=True, backend=preliminary)
    agent.followups, store.followup_store = followups, followups.store
    s = SimpleNamespace(stack=stack, store=store, engine=engine, agent=agent, client=client,
        backend=backend, preliminary=preliminary, followups=followups, routes=routes, monkeypatch=monkeypatch)
    yield s


def _seed(s, *, logs=False):
    s.cid = s.agent.create_conversation("original", owner_key=OWNER_KEY).conversation_id
    s.prefix = f"/api/v1/agent/conversations/{s.cid}"
    attachment = _preupload(s.client, s.prefix) if logs else None
    _post(s.client, s.prefix + "/messages", {"request_id": "problem", "text": "客户端请求为什么超时？",
        "attachment_ids": [attachment] if attachment else []})
    s.first = _publish(s)
    s.parent = s.first["run_id"]
    s.original_case = s.stack.repository.read_case(s.first["case_id"]).model_dump_json()
    s.path = s.prefix + f"/runs/{s.parent}/followups"
    s.backend.report = NEW_REPORT
    # An applicable specialized route exists: the new round must bypass it.
    s.monkeypatch.setattr(s.stack.catalog, "_route_skill_refs", s.routes)
    s.monkeypatch.setattr(s.engine, "intake", lambda *_args, **_kwargs: pytest.fail("Generic 追问不能先走 INTAKE。"))
    s.monkeypatch.setattr(s.engine, "prepare_continuation", lambda **_kwargs: pytest.fail("不能先判断是否继续诊断。"), raising=False)
    return attachment


def _handoff(s, text, attachments=()):
    payload = {"request_id": "question", "text": text, "attachment_ids": list(attachments)}
    receipt = _post(s.client, s.path, payload)
    assert _post(s.client, s.path, payload) == receipt
    handled = s.followups.worker.run_once()
    assert s.preliminary.calls == 0
    assert handled
    item = _get(s.client, s.path)["items"][-1]
    assert item["status"] == "COMPLETED" and item["diagnosis_run_id"] not in {None, s.parent}
    assert item["run_id"] == s.parent and item["request_id"] == payload["request_id"]
    assert _post(s.client, s.path, payload) == receipt
    assert not s.followups.worker.run_once()
    s.child = item["diagnosis_run_id"]
    return item


def _assert_generic_case(s, text):
    assert s.agent.run_once(s.cid)
    child = s.store.get_run(s.cid)
    assert child["run_id"] == s.child and child["force_generic"] is True
    aggregate = s.stack.repository.read_snapshot(child["case_id"]).cases[child["case_id"]]
    assert aggregate.case.raw_problem_text == text
    assert aggregate.jobs and all(job.job_type is JobType.DIAGNOSE and job.diagnosis_mode is DiagnosisMode.GENERIC
        for job in aggregate.jobs.values())
    return _publish(s)


def _assert_published(s, current, text):
    assert current["case_id"] != s.first["case_id"] and current["run_id"] == s.child
    assert current["result"]["markdown"] == s.backend.report
    aggregate = s.stack.repository.read_snapshot(current["case_id"]).cases[current["case_id"]]
    assert all(job.job_type is not JobType.ROUTE for job in aggregate.jobs.values())
    job = aggregate.jobs[aggregate.case.generic_result_v2.source_job_id]
    assert job.diagnosis_mode is DiagnosisMode.GENERIC and job.generic_problem_text == text
    assert job.continuation_context.parent_case_id == s.first["case_id"]
    assert job.continuation_context.parent_run_id == s.parent
    assert job.continuation_context.parent_report_markdown == OLD_REPORT
    assert job.continuation_context.parent_report_sha256 == hashlib.sha256(OLD_REPORT.encode()).hexdigest()
    call = s.backend.calls[-1]
    assert f"Invoke the preinstalled Skill `${job.generic_skill_name}` now." in call["prompt"]
    raw_payload = call["prompt"].split(f"<<<RAW_PROBLEM_TEXT_UTF8_BYTES:{len(text.encode('utf-8'))}>>>\n", 1)[1].split("<<<END_RAW_PROBLEM_TEXT>>>", 1)[0]
    assert raw_payload == text + "\n"
    assert "客户端请求为什么超时？" not in raw_payload
    reference = json.loads(call["prompt"].split("<report_continuation_data>\n", 1)[1].split("</report_continuation_data>", 1)[0])
    assert reference["parent_report_markdown"] == OLD_REPORT
    assert reference["followup_context_path"] == "inputs/followup-context.json"
    assert "followup_context_json" not in reference
    assert call["followup_context_bytes"] == aggregate.case.continuation_context.followup_context_json.encode("utf-8")
    assert call["followup_context_bytes"] == job.continuation_context.followup_context_json.encode("utf-8")
    assert json.loads(call["followup_context_bytes"])["original_problem"] == "客户端请求为什么超时？"
    assert s.stack.repository.read_case(s.first["case_id"]).model_dump_json() == s.original_case
    assert _get(s.client, s.prefix + f"?include=report&run_id={s.parent}")["result"]["markdown"] == OLD_REPORT
    versions = _get(s.client, s.prefix + f"/runs/{s.child}/report-versions")
    assert [(entry["version"], entry["parent_run_id"]) for entry in versions["versions"]] == [(1, None), (2, s.parent)]
    with s.stack.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM agent_conversation_runs WHERE conversation_id=?", (s.cid,)).fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM agent_continuation_drafts WHERE conversation_id=?", (s.cid,)).fetchone()[0] == 0


@pytest.mark.parametrize("text", ["这份报告为什么认为连接池值得检查？", "另一个问题：服务端磁盘写入抖动可能由什么导致？"])
def test_every_followup_starts_one_generic_case_without_route_or_preliminary_model(generic_round, text):
    s = generic_round
    _seed(s)
    _handoff(s, text)
    _assert_published(s, _assert_generic_case(s, text), text)
    assert len(s.backend.calls) == 2
    assert not s.followups.worker.run_once()
    assert len(s.backend.calls) == 2


def test_followup_generic_round_adopts_original_and_new_log_archives(generic_round):
    s = generic_round
    original = _seed(s, logs=True)
    extra, digest = _upload_second_archive(s.client, s.cid)
    text = "新增服务端日志，请核对时间线并更正此前判断。"
    _handoff(s, text, [extra])
    _assert_published(s, _assert_generic_case(s, text), text)
    assert s.store.get_run(s.cid)["report_inputs"]["attachment_ids"] == [original, extra]
    manifest = s.backend.calls[-1]["logs"]
    assert len(manifest["sources"]) == 2
    assert {source["source_attachment_sha256"] for source in manifest["sources"]} == {
        s.store.get_attachment(original).sha256, digest}


def test_missing_evidence_is_a_new_generic_unresolved_report_not_an_old_answer(generic_round):
    s = generic_round
    _seed(s)
    s.backend.status, s.backend.report = "UNRESOLVED", MISSING_REPORT
    text = "没有服务端日志，能否判断服务端崩溃的原因？"
    _handoff(s, text)
    current = _assert_generic_case(s, text)
    _assert_published(s, current, text)
    aggregate = s.stack.repository.read_snapshot(current["case_id"]).cases[current["case_id"]]
    assert aggregate.case.generic_result_v2.status.value == "UNRESOLVED"
    assert current["result"]["report_state"] == "READY" and len(s.backend.calls) == 2


@pytest.mark.parametrize("after_handoff", [False, True])
def test_followup_cancellation_does_not_start_or_replay_generic_work(generic_round, after_handoff):
    s = generic_round
    _seed(s)
    payload = {"request_id": "question", "text": "检查服务端原因。", "attachment_ids": []}
    receipt = _post(s.client, s.path, payload)
    if after_handoff:
        handled = s.followups.worker.run_once()
        assert s.preliminary.calls == 0
        assert handled
        child = _get(s.client, s.path)["items"][-1]["diagnosis_run_id"]
        assert child and child != s.parent
        request = {"request_id": "stop", "run_id": child}
        stop = _post(s.client, s.prefix + "/stop", request)
        assert _post(s.client, s.prefix + "/stop", request) == stop
        s.agent.run_once(s.cid)
        assert s.agent.control_once()
        assert s.store.get_run(s.cid, child)["status"] == "CANCELLED"
    else:
        path = s.path + f"/{receipt['followup_id']}/stop"
        stop = _post(s.client, path, {"request_id": "stop"})
        assert _post(s.client, path, {"request_id": "stop"}) == stop
        assert _get(s.client, s.path)["items"][-1]["status"] == "CANCELLED"
        assert s.store.get_run(s.cid)["run_id"] == s.parent
    assert not s.followups.worker.run_once()
    assert _post(s.client, s.path, payload) == receipt
    assert s.stack.scheduler.wait_until_idle(20)
    assert len(s.backend.calls) == 1
    assert s.preliminary.calls == 0
    assert s.stack.repository.read_case(s.first["case_id"]).model_dump_json() == s.original_case
