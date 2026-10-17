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
    return SimpleNamespace(followups=Followups(), feedback=Feedback())


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
    ("POST", "", {"json": {"request_id": "id", "text": "说明", "attachment_ids": ["invalid"]}}),
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
    assert set(schemas["FollowupRequest"]["properties"]) == {"request_id", "text", "attachment_ids", "intent", "target_followup_id"}


def test_same_followup_endpoint_accepts_uploaded_logs_and_returns_durable_handoff(agent):
    attachment = "60000000-0000-0000-0000-000000000002"
    response = run_request(agent, "POST", ROOT, json={
        "request_id": "with-logs", "text": "结合服务端日志再看看原因", "attachment_ids": [attachment]})
    assert response.status_code == 200
    assert agent.followups.calls[-1][3]["attachment_ids"] == [attachment]
    next_run = "60000000-0000-0000-0000-000000000003"
    original = agent.followups.get
    def handed_off(*args, **kwargs):
        value = original(*args, **kwargs)
        value["items"] = [{**ITEM, "attachment_ids": [attachment], "diagnosis_run_id": next_run}]
        return value
    agent.followups.get = handed_off
    response = run_request(agent, "GET", ROOT)
    item = response.json()["data"]["items"][0]
    assert item["run_id"] == RUN and item["diagnosis_run_id"] == next_run


def test_expired_supplement_log_is_an_actionable_410_not_an_uncertain_retry(agent):
    def expired(*args, **kwargs):
        raise AgentStoreError("AGENT_RESOURCE_EXPIRED", "日志已清理，请重新上传。", 410)
    agent.followups.submit = expired
    response = run_request(agent, "POST", ROOT, json={
        "request_id": "expired", "text": "请核对日志", "attachment_ids": [FID]})
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "AGENT_RESOURCE_EXPIRED"
    assert "410" in app_for(FakeAgent()).openapi()["paths"][
        "/api/v1/agent/conversations/{conversation_id}/runs/{run_id}/followups"]["post"]["responses"]


class Feedback:
    def __init__(self):
        self.calls = []

    def get_followup_feedback(self, cid, rid, fid, **kwargs):
        self.calls.append(("get", cid, rid, fid, kwargs))
        return dict(schema_version=1, conversation_id=cid, run_id=rid, followup_id=fid,
                    can_rate=True, rating=None, updated_at=None)

    def put_followup_feedback(self, cid, rid, fid, **kwargs):
        self.calls.append(("put", cid, rid, fid, kwargs))
        return dict(schema_version=1, conversation_id=cid, run_id=rid, followup_id=fid,
                    can_rate=True, rating=kwargs["rating"], updated_at=WHEN)


def test_answer_feedback_routes_bind_exact_answer_and_trusted_owner(agent):
    path = ROOT + f"/{FID}/feedback"
    result = run_request(agent, "GET", path)
    assert result.status_code == 200
    assert set(result.json()["data"]) == {"schema_version", "conversation_id", "run_id", "followup_id", "can_rate", "rating", "updated_at"}
    assert agent.feedback.calls[-1] == ("get", CONVERSATION, RUN, FID, {"owner_key": OWNER})
    result = run_request(agent, "PUT", path, json={"request_id": "vote-1", "rating": "LIKE"})
    assert result.status_code == 200
    assert agent.feedback.calls[-1] == ("put", CONVERSATION, RUN, FID,
        {"owner_key": OWNER, "request_id": "vote-1", "rating": "LIKE"})
    assert not agent.followups.calls


@pytest.mark.parametrize("method,suffix,kwargs", [
    ("GET", "?limit=1", {}), ("GET", "?share=true", {}), ("GET", "", {"content": b"{}"}),
    ("PUT", "?share=true", {"json": {"request_id": "vote-1", "rating": "LIKE"}}),
    ("PUT", "", {"json": {"request_id": "vote-1", "rating": "LIKE", "text": "不对"}}),
    ("PUT", "", {"json": {"request_id": "vote-1", "rating": None}}),
    ("PUT", "", {"json": {"request_id": " ", "rating": "LIKE"}}),
])
def test_answer_feedback_rejects_invalid_input_before_service(agent, method, suffix, kwargs):
    result = run_request(agent, method, ROOT + f"/{FID}/feedback" + suffix, **kwargs)
    assert result.status_code == 400
    assert not agent.feedback.calls


def test_answer_feedback_rejects_response_for_another_answer(agent):
    original = agent.feedback.get_followup_feedback
    agent.feedback.get_followup_feedback = lambda *args, **kwargs: {
        **original(*args, **kwargs), "followup_id": "60000000-0000-0000-0000-000000000002"}
    result = run_request(agent, "GET", ROOT + f"/{FID}/feedback")
    assert result.status_code == 500


def test_correction_post_forwards_optional_target_without_changing_receipt(agent):
    body = {"request_id": "correct-1", "text": "不是连接超时，是认证失败。", "intent": "CORRECTION", "target_followup_id": FID}
    result = run_request(agent, "POST", ROOT, json=body)
    assert result.status_code == 200
    assert agent.followups.calls[-1] == ("submit", CONVERSATION, RUN, {"owner_key": OWNER, **body})
    assert set(result.json()["data"]) == {"conversation_id", "run_id", "followup_id", "request_id", "event_id", "status"}
    invalid = run_request(agent, "POST", ROOT, json={"request_id": "ordinary", "text": "解释", "target_followup_id": FID})
    assert invalid.status_code == 400
    schemas = app_for(FakeAgent()).openapi()["components"]["schemas"]
    assert set(schemas["FeedbackView"]["properties"]) == {"schema_version", "conversation_id", "run_id", "can_rate", "rating", "updated_at"}
    assert set(schemas["FollowupFeedbackView"]["properties"]) == set(schemas["FeedbackView"]["properties"]) | {"followup_id"}


@pytest.mark.parametrize("options", [
    {"intent": None}, {"target_followup_id": None},
    {"intent": None, "target_followup_id": None},
    {"intent": "CORRECTION", "target_followup_id": None},
])
def test_correction_optional_null_fields_match_ordinary_or_report_target(agent, options):
    body = {"request_id": "nullable", "text": "进一步说明", **options}
    result = run_request(agent, "POST", ROOT, json=body)
    assert result.status_code == 200
    assert agent.followups.calls[-1][3] == {"owner_key": OWNER, **body}


@pytest.mark.parametrize("options", [
    {"intent": ""}, {"intent": False}, {"intent": "CORRECTION", "target_followup_id": ""},
    {"intent": None, "target_followup_id": FID},
])
def test_correction_invalid_optional_fields_fail_before_submit(agent, options):
    result = run_request(agent, "POST", ROOT, json={"request_id": "bad-options", "text": "说明", **options})
    assert result.status_code == 400
    assert not agent.followups.calls
