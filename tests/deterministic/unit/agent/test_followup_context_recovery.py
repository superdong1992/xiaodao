"""Long user supplements remain readable without a summarization model."""
import json

import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.followup.models import MAX_CONTEXT_BYTES
from tests.deterministic.unit.agent.test_followup_handoff import state, item, counts
from tests.deterministic.unit.agent.test_followup_material_selection import claim, fail_attempt_with_messages
from tests.deterministic.unit.agent.test_report_continuation import OWNER, system


def long_attempt(s):
    texts = [f"fact-{index}:" + "a" * 65_500 + f"-tail-{index}" for index in range(4)]
    fail_attempt_with_messages(s, claim(s, key="first"),
        [(f"supplement-{index}", text, None, "APPLIED") for index, text in enumerate(texts)])
    return texts


def test_long_failed_attempt_supplements_use_readonly_file_without_extra_model(state):
    s = state
    texts = long_attempt(s)
    before = counts(s)
    s.followups.submit(s.cid, s.rid, "again", "解释刚才补充的内容。", owner_key=OWNER)
    assert s.followups.worker.run_once()
    assert len(s.backend.calls) == 1 and counts(s) == before and item(s).status == "COMPLETED"
    call = s.backend.calls[0]
    context = json.loads((call["workspace_root"] / "inputs/context.json").read_bytes())
    attempt = context["diagnosis_context"]["latest_attempt"]
    assert attempt["user_messages"] == [] and len(attempt["user_messages_index"]) == 4
    path = call["workspace_root"] / attempt["user_messages_file"]
    messages = json.loads(path.read_bytes())
    assert [entry["text"] for entry in messages] == texts
    assert all(entry["status"] == "APPLIED" for entry in messages)
    assert path.stat().st_mode & 0o222 == 0
    assert call["file_access"] == "read-search" and len(call["prompt"].encode()) <= MAX_CONTEXT_BYTES


def test_launcher_without_read_search_rejects_oversized_supplements_at_admission(state):
    s = state
    long_attempt(s)
    s.followups.read_search_supported = False
    before = counts(s)
    with pytest.raises(AgentStoreError) as error:
        s.followups.submit(s.cid, s.rid, "again", "解释刚才补充的内容。", owner_key=OWNER)
    assert error.value.code == "AGENT_FOLLOWUP_CONTEXT_LIMIT"
    assert s.backend.calls == [] and counts(s) == before
    assert s.followups.store.request(s.cid, s.rid, "again", "解释刚才补充的内容。", OWNER) is None
