"""Report-followup wire contracts and independent resumable SSE."""
from types import SimpleNamespace

import pytest

from problem_locator.agent.models import AgentStoreError
from tests.deterministic.unit.interfaces.test_agent_http import (
    CONVERSATION, RUN, OWNER, WHEN, run_request, app_for, FakeAgent,
)

FID = "60000000-0000-0000-0000-000000000001"
ROOT = f"/api/v1/agent/conversations/{CONVERSATION}/runs/{RUN}/followups"
ITEM = dict(followup_id=FID, run_id=RUN, request_id="ask-1", ordinal=1, status="COMPLETED",
    context_mode="REPORT_ONLY", text="为何判断超时？", answer_markdown="仅依据报告：请求等待超时。",
    failure=None, created_at=WHEN, updated_at=WHEN)


class Followups:
    def __init__(self):
        self.calls = []
        self.events = [dict(schema_version=1, sequence=i, conversation_id=CONVERSATION, run_id=RUN,
            followup_id=FID, type="followup.updated", created_at=WHEN, data=ITEM) for i in (1, 2)]

    def get(self, cid, rid, **kwargs):
        self.calls.append(("get", cid, rid, kwargs))
        return dict(schema_version=1, conversation_id=cid, run_id=rid, can_ask=False,
            reason="DISABLED", snapshot_status="UNAVAILABLE", active_followup=None,
            items=[ITEM], next_cursor=None, last_event_id=2)

    def submit(self, cid, rid, **kwargs):
        self.calls.append(("submit", cid, rid, kwargs))
        return dict(conversation_id=cid, run_id=rid, followup_id=FID,
                    request_id=kwargs["request_id"], event_id=1, status="ACCEPTED")

    def stop(self, cid, rid, **kwargs):
        self.calls.append(("stop", cid, rid, kwargs))
        return dict(conversation_id=cid, run_id=rid, followup_id=kwargs["followup_id"],
                    request_id=kwargs["request_id"], event_id=2, status="ALREADY_FINISHED")

    def list_events(self, cid, rid, **kwargs):
        self.calls.append(("events", cid, rid, kwargs))
        assert kwargs["owner_key"] == OWNER
        return dict(events=[e for e in self.events if e["sequence"] > kwargs["after_sequence"]], stream_closed=True)


@pytest.fixture
def agent():
    return SimpleNamespace(followups=Followups())


def test_submit_list_stop_keep_identity_and_owner(agent):
    response = run_request(agent, "POST", ROOT, json={"request_id": "ask-1", "text": "进一步解释"})
    assert response.status_code == 200
    assert response.json()["data"]["followup_id"] == FID
    assert agent.followups.calls[-1] == ("submit", CONVERSATION, RUN,
        {"owner_key": OWNER, "request_id": "ask-1", "text": "进一步解释"})
    response = run_request(agent, "GET", ROOT + "?cursor=opaque&limit=2")
    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["answer_markdown"] == ITEM["answer_markdown"]
    assert agent.followups.calls[-1][3]["cursor"] == "opaque"
    response = run_request(agent, "POST", ROOT + f"/{FID}/stop", json={"request_id": "stop-1"})
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ALREADY_FINISHED"


@pytest.mark.parametrize("method,path,kwargs", [
    ("POST", "", {"json": {"request_id": "id", "text": " ",}}),
    ("POST", "", {"json": {"request_id": "id", "text": "说明", "attachment_ids": []}}),
    ("POST", "?unexpected=1", {"json": {"request_id": "id", "text": "说明"}}),
    ("GET", "?limit=0", {}), ("GET", "?limit=1&limit=2", {}),
    ("GET", "?unknown=x", {}), ("GET", "", {"content": b"{}"}),
    ("GET", "/events?cursor=1", {}), ("GET", "/events", {"headers": {"Last-Event-ID": "01"}}),
    ("GET", "/events", {"headers": {"Last-Event-ID": "9223372036854775808"}}),
    ("GET", "/events", {"content": b"{}"}),
    ("POST", f"/{FID}/stop", {"json": {"request_id": "stop", "text": "no"}}),
])
def test_followup_rejects_invalid_input_before_service(agent, method, path, kwargs):
    response = run_request(agent, method, ROOT + path, **kwargs)
    assert response.status_code == 400
    assert not agent.followups.calls


def test_query_and_replay_do_not_submit_and_have_separate_cursor(agent):
    response = run_request(agent, "GET", ROOT + "/events", headers={"Last-Event-ID": "1"})
    assert response.status_code == 200
    assert response.headers["x-accel-buffering"] == "no"
    assert response.text.startswith(": connected\n\n")
    assert response.text.count("data: ") == 1
    assert "\n\n\n" not in response.text
    assert '"sequence":2' in response.text
    assert not any(operation[0] == "submit" for operation in agent.followups.calls)
    assert agent.followups.calls[0][3]["after_sequence"] == 1


def test_event_identity_or_gap_is_rejected_before_stream_headers(agent):
    agent.followups.events[0]["sequence"] = 3
    response = run_request(agent, "GET", ROOT + "/events")
    assert response.status_code == 500
    assert response.headers["content-type"] == "application/json"


def test_owner_failure_is_private_and_does_not_fall_back_to_messages(agent):
    def fail(*args, **kwargs):
        raise AgentStoreError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在。", 404)
    agent.followups.get = fail
    response = run_request(agent, "GET", ROOT)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AGENT_CONVERSATION_NOT_FOUND"


def test_new_routes_do_not_change_existing_sse_or_detail_versions():
    schemas = app_for(FakeAgent()).openapi()["components"]["schemas"]
    assert schemas["AgentEvent"]["properties"]["schema_version"]["const"] == 2
    assert schemas["ConversationDetailResponse"]["properties"]["schema_version"]["const"] == 3
    assert schemas["FollowupEvent"]["properties"]["schema_version"]["const"] == 1
    assert schemas["FollowupRequest"]["additionalProperties"] is False
    assert set(schemas["FollowupRequest"]["properties"]) == {"request_id", "text"}
