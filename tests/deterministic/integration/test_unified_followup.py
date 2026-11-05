"""One HTTP entry always creates a Generic report, with existing and new logs."""
import json
from tests.deterministic.integration.test_followup_generic_round import (
    generic_round, _seed, _handoff, _assert_generic_case, _assert_published,
)
from tests.deterministic.integration.test_website_agent import _post, website
from tests.deterministic.integration.test_website_report_continuation import _get, _upload_second_archive


def test_unified_followup_reanalyses_every_question_and_keeps_full_history_and_logs(generic_round):
    s = generic_round
    attachment = _seed(s, logs=True)
    question = "这份报告确定根因了吗？"
    first_handoff = _handoff(s, question)
    second = _assert_generic_case(s, question)
    _assert_published(s, second, question)
    second_run, second_case = second["run_id"], second["case_id"]
    second_bytes = s.stack.repository.read_case(second_case).model_dump_json()
    assert first_handoff["diagnosis_run_id"] == second_run
    extra, digest = _upload_second_archive(s.client, s.cid)
    path = s.prefix + f"/runs/{second_run}/followups"
    text = "结合这些日志，现在能确定原因吗？"
    payload = {"request_id": "new-evidence", "text": text, "attachment_ids": [extra]}
    receipt = _post(s.client, path, payload)
    assert s.followups.worker.run_once()
    assert _post(s.client, path, payload) == receipt
    handoff = _get(s.client, path)["items"][0]
    assert handoff["status"] == "COMPLETED" and handoff["diagnosis_run_id"] not in {None, second_run, s.parent}
    s.child = handoff["diagnosis_run_id"]
    s.backend.report = "## 本轮变化\n本轮新增服务端日志。\n# 定位报告\n仍需核对连接池容量。"
    third = _assert_generic_case(s, text)
    assert third["run_id"] == s.child and third["case_id"] not in {s.first["case_id"], second_case}
    assert third["result"]["markdown"] == s.backend.report
    assert s.preliminary.calls == 0 and len(s.backend.calls) == 3
    reference = json.loads(s.backend.calls[-1]["followup_context_bytes"])
    assert reference["prior_turns"][0]["text"] == question
    assert reference["prior_turns"][0]["diagnosis_run_id"] == second_run
    assert reference["prior_reports"][0]["run_id"] == s.parent
    manifest = s.backend.calls[-1]["logs"]
    assert len(manifest["sources"]) == 2
    assert {entry["source_attachment_sha256"] for entry in manifest["sources"]} == {
        s.store.get_attachment(attachment).sha256, digest}
    assert s.store.get_run(s.cid)["report_inputs"]["attachment_ids"] == [attachment, extra]
    assert s.stack.repository.read_case(s.first["case_id"]).model_dump_json() == s.original_case
    assert s.stack.repository.read_case(second_case).model_dump_json() == second_bytes
    versions = _get(s.client, s.prefix + f"/runs/{s.child}/report-versions")
    assert [(v["version"], v["parent_run_id"]) for v in versions["versions"]] == [
        (1, None), (2, s.parent), (3, second_run)]
    with s.stack.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM agent_continuation_drafts").fetchone()[0] == 0
