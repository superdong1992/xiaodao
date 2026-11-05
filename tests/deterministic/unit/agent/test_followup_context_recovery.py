"""Full failed-attempt supplements reach Generic without a summarization model."""
import json
import pytest
from tests.deterministic.unit.agent.test_followup_handoff import state, item, counts
from tests.deterministic.unit.agent.test_followup_material_selection import claim, fail_attempt_with_messages
from tests.deterministic.unit.agent.test_report_continuation import OWNER, system


def long_attempt(s):
    texts = [f"fact-{index}:" + "a" * 65_500 + f"-tail-{index}" for index in range(4)]
    fail_attempt_with_messages(s, claim(s, key="first"),
        [(f"supplement-{index}", text, None, "APPLIED") for index, text in enumerate(texts)])
    return texts


@pytest.mark.parametrize("read_search", [True, False])
def test_long_failed_attempt_reaches_generic_in_full_without_a_preliminary_model(state, read_search):
    s = state
    texts = long_attempt(s)
    s.followups.read_search_supported = read_search
    before = counts(s)
    question = "解释刚才补充的内容。"
    receipt = s.followups.submit(s.cid, s.rid, "again", question, owner_key=OWNER)
    assert s.followups.worker.run_once()
    run = s.store.get_run(s.cid)
    assert s.backend.calls == [] and item(s).status == "COMPLETED"
    assert counts(s) == (before[0] + 1, before[1], before[2] + 1)
    assert run["force_generic"] is True and run["run_id"] == item(s).diagnosis_run_id
    reference = json.loads(run["continuation_context"]["followup_context_json"])
    messages = reference["latest_attempt"]["user_messages"]
    assert [entry["text"] for entry in messages] == texts
    assert all(entry["status"] == "APPLIED" for entry in messages)
    assert s.store.get_conversation(s.cid).messages[0].text == question
    assert s.followups.submit(s.cid, s.rid, "again", question, owner_key=OWNER) == receipt
    assert not s.followups.worker.run_once()
    assert counts(s) == (before[0] + 1, before[1], before[2] + 1)
