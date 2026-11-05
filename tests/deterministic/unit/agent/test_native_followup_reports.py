"""Previously discussed reports remain readable when native sessions start or resume."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import uuid

import pytest

from problem_locator.followup.snapshots import InputChanged
from tests.deterministic.unit.agent.test_report_followup import (
    OWNER, Backend, seed_historical_turn, submit, system, view,
)


def set_report(system, marker):
    report = "# 定位报告\n" + marker + "\n"
    system.source = replace(system.source, problem_text="原问题：" + marker,
        report_markdown=report, report_sha256=hashlib.sha256(report.encode()).hexdigest())


def new_report(system, marker):
    with system.repository.database_transaction() as db:
        run = system.agent.store._new_run(db, system.agent.store._load(db, system.cid))
        run.update(status="COMPLETED", report_available=True, case_id=str(uuid.uuid4()))
        system.agent.store._save(db, run)
    system.rid = run["run_id"]
    system.source = replace(system.source, case_id=run["case_id"])
    set_report(system, marker)



def legacy_report(system):
    set_report(system, "LEGACY_REPORT_ONLY_MARKER_A")
    seed_historical_turn(system, "legacy", "请解释第一份报告。", "第一份报告的既有解释。")
    return system.rid, system.source


def claimed(system, request="compare"):
    submit(system, request, "比较先前讨论的报告。")
    return system.service.store.claim_task()


def test_historical_report_remains_complete_across_later_questions(system):
    old_rid, old_source = legacy_report(system)
    new_report(system, "CURRENT_REPORT_MARKER_B")
    task = claimed(system)
    expected = [{"run_id": old_rid, "report_sha256": old_source.report_sha256,
        "original_problem": old_source.problem_text, "original_report": old_source.report_markdown}]
    assert system.service.store.sessions.historical_reports(task) == expected
    assert system.service.store.finish_task(task["followup_id"], answer="两版报告的历史解释。")
    assert system.service.store.sessions.historical_reports(claimed(system, "next")) == expected
    assert system.backend.calls == []


def test_discussed_intermediate_report_survives_another_publication(system):
    old_rid, old_source = legacy_report(system)
    new_report(system, "INTERMEDIATE_REPORT_B")
    intermediate, intermediate_source = system.rid, system.source
    seed_historical_turn(system, "intermediate", "解释中间报告。", "中间报告的历史解释。")
    new_report(system, "LATEST_REPORT_C")
    reports = system.service.store.sessions.historical_reports(claimed(system))
    by_id = {entry["run_id"]: entry for entry in reports}
    assert set(by_id) == {old_rid, intermediate}
    assert by_id[intermediate]["original_report"] == intermediate_source.report_markdown
    assert by_id[old_rid]["original_report"] == old_source.report_markdown


def test_report_index_excludes_undiscussed_current_and_other_conversation(system):
    from problem_locator.followup.models import FollowupRequest
    old_rid, _ = legacy_report(system)
    new_report(system, "UNDISCUSSED_REPORT")
    with system.repository.database_transaction() as db:
        system.service.store._register_source(db, system.cid, system.rid, system.source)
    other = system.agent.store.create_conversation("other", owner_key=OWNER)
    system.service.store.submit(other.conversation_id, other.run_id,
        FollowupRequest(request_id="other-turn", text="其他会话的问题。"), system.source, OWNER)
    other_task = system.service.store.claim_task()
    assert system.service.store.finish_task(other_task["followup_id"], answer="其他会话的历史回答。")
    new_report(system, "CURRENT_REPORT")
    reports = system.service.store.sessions.historical_reports(claimed(system))
    assert [entry["run_id"] for entry in reports] == [old_rid]


@pytest.mark.parametrize("damage", ["body", "hash", "missing"])
def test_missing_or_corrupt_historical_report_cannot_be_used_as_context(system, damage):
    old_rid, _ = legacy_report(system)
    with system.repository.database_transaction() as db:
        if damage == "missing":
            db.execute("DELETE FROM agent_followup_snapshots WHERE run_id=?", (old_rid,))
        elif damage == "hash":
            db.execute("UPDATE agent_followup_snapshots SET report_sha256=? WHERE run_id=?", ("0" * 64, old_rid))
        else:
            raw = json.loads(db.execute("SELECT source_json FROM agent_followup_snapshots WHERE run_id=?", (old_rid,)).fetchone()[0])
            raw["report_markdown"] += "changed"
            db.execute("UPDATE agent_followup_snapshots SET source_json=? WHERE run_id=?", (json.dumps(raw), old_rid))
    new_report(system, "CURRENT_REPORT")
    with pytest.raises(InputChanged, match="历史报告"):
        system.service.store.sessions.historical_reports(claimed(system))
    assert system.backend.calls == []


@pytest.mark.parametrize("tools", [False, True])
def test_full_historical_reports_are_retained_independently_of_retired_launcher_tools(system, tools):
    system.service.read_search_supported = tools
    expected = {}
    for index in range(5):
        if index:
            new_report(system, "")
        marker = f"report-{index}:" + "r" * 60_000 + f":tail-{index}"
        set_report(system, marker)
        expected[system.rid] = system.source.report_markdown
        seed_historical_turn(system, str(index), "解释这一版。", "既有解释。")
    new_report(system, "CURRENT_REPORT")
    reports = system.service.store.sessions.historical_reports(claimed(system))
    assert {entry["run_id"]: entry["original_report"] for entry in reports} == expected
    assert system.backend.calls == []
