"""Previously discussed reports remain readable when native sessions start or resume."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import uuid

import pytest

from problem_locator.followup.context import build_prompt
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
        system.agent.store._save(db, run)
    system.rid = run["run_id"]
    set_report(system, marker)


def enable_native(system, *, tools=True):
    system.service.read_search_supported = tools
    system.backend = Backend()
    system.service.worker.backend = system.backend


def context(call):
    return json.loads((call["workspace_root"] / "inputs/context.json").read_text("utf-8"))


def legacy_report(system):
    set_report(system, "LEGACY_REPORT_ONLY_MARKER_A")
    seed_historical_turn(system, "legacy", "请解释第一份报告。", "第一份报告的既有解释。")
    return system.rid, system.source


def test_first_native_turn_restores_discussed_report_and_keeps_it_readable_next_turn(system):
    old_rid, old_source = legacy_report(system)
    new_report(system, "CURRENT_REPORT_MARKER_B")
    enable_native(system)
    submit(system, "compare", "比较第一份和第二份报告。")
    assert system.service.worker.run_once()
    first = system.backend.calls[-1]
    index = context(first)["prior_reports"]
    assert index == [{"run_id": old_rid, "report_sha256": old_source.report_sha256,
        "path": f"inputs/prior-reports/{old_rid}.md"}]
    saved = (first["workspace_root"] / index[0]["path"]).read_text("utf-8")
    assert old_source.problem_text in saved and old_source.report_markdown in saved
    assert "LEGACY_REPORT_ONLY_MARKER_A" not in first["prompt"]
    assert context(first)["original_report"] == system.source.report_markdown
    # The first answer need not read every old paragraph. The next turn still can.
    submit(system, "next", "再解释第一份报告中上一轮没有讨论的部分。")
    assert system.service.worker.run_once()
    second = system.backend.calls[-1]
    assert context(second)["prior_turns"] == []
    assert context(second)["prior_reports"] == index
    assert second["workspace_root"] != first["workspace_root"]
    assert (second["workspace_root"] / index[0]["path"]).read_text("utf-8") == saved


def test_discussed_intermediate_report_is_available_after_another_report_is_published(system):
    set_report(system, "FIRST_NATIVE_REPORT_A")
    enable_native(system)
    submit(system, "native-first")
    assert system.service.worker.run_once()
    session_id = system.service.store.sessions.get(system.cid)["session_id"]
    new_report(system, "LEGACY_INTERMEDIATE_REPORT_B")
    intermediate = system.rid
    submit(system, "legacy-second", "请解释中间这份报告。")
    assert system.service.worker.run_once()
    new_report(system, "LATEST_REPORT_C")
    submit(system, "native-resumed", "比较刚才的报告和当前报告。")
    assert system.service.worker.run_once()
    call = system.backend.calls[-1]
    assert call["native_session"].resume_id == session_id
    entry = next(item for item in context(call)["prior_reports"] if item["run_id"] == intermediate)
    assert "LEGACY_INTERMEDIATE_REPORT_B" in (call["workspace_root"] / entry["path"]).read_text("utf-8")
    assert "LEGACY_INTERMEDIATE_REPORT_B" not in call["prompt"]


def test_report_index_excludes_undiscussed_current_and_other_conversation_reports(system):
    old_rid, _ = legacy_report(system)
    new_report(system, "UNDISCUSSED_REPORT")
    undiscussed = system.rid
    with system.repository.database_transaction() as db:
        system.service.store._register_source(db, system.cid, undiscussed, system.source)
    other = system.agent.store.create_conversation("other", owner_key=OWNER)
    system.service.submit(other.conversation_id, other.run_id, "other-turn", "其他会话的问题。", owner_key=OWNER)
    assert system.service.worker.run_once()
    new_report(system, "CURRENT_REPORT")
    enable_native(system)
    submit(system, "compare", "比较先前讨论过的报告。")
    assert system.service.worker.run_once()
    call = system.backend.calls[-1]
    assert [item["run_id"] for item in context(call)["prior_reports"]] == [old_rid]
    assert {path.stem for path in (call["workspace_root"] / "inputs/prior-reports").iterdir()} == {old_rid}


@pytest.mark.parametrize("damage", ["body", "hash", "missing"])
def test_missing_or_corrupt_historical_report_stops_before_model_invocation(system, damage):
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
    enable_native(system)
    submit(system, "compare", "比较旧报告。")
    assert not system.service.worker.run_once()
    assert system.backend.calls == []
    assert view(system).items[-1].failure.code == "AGENT_FOLLOWUP_INPUT_CHANGED"


def test_without_tools_historical_report_is_inlined_once_without_losing_text(system):
    old_rid, old_source = legacy_report(system)
    new_report(system, "CURRENT_REPORT")
    enable_native(system, tools=False)
    submit(system, "compare", "比较旧报告。")
    assert system.service.worker.run_once()
    first = system.backend.calls[-1]
    assert first["file_access"] == "none"
    assert context(first)["prior_reports"] == [{"run_id": old_rid, "report_sha256": old_source.report_sha256,
        "original_problem": old_source.problem_text, "original_report": old_source.report_markdown}]
    assert "LEGACY_REPORT_ONLY_MARKER_A" in first["prompt"]
    submit(system, "next", "继续解释。")
    assert system.service.worker.run_once()
    assert "prior_reports" not in context(system.backend.calls[-1])
    assert "LEGACY_REPORT_ONLY_MARKER_A" not in system.backend.calls[-1]["prompt"]


def test_without_tools_oversized_historical_reports_are_rejected_not_dropped(system):
    reports = [{"run_id": str(uuid.uuid4()), "report_sha256": "a" * 64,
        "original_problem": "此前的问题", "original_report": "r" * 60_000} for _ in range(5)]
    with pytest.raises(ValueError, match="补充内容过多"):
        build_prompt(system.source, [], "比较旧报告。", "REPORT_ONLY", native_session={}, prior_reports=reports)
