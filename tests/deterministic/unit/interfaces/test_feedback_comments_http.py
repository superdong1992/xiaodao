"""Standalone negative feedback has no conversational side effects."""
from types import SimpleNamespace

import pytest

from problem_locator.agent.models import AgentStoreError
from tests.deterministic.unit.interfaces.test_agent_http import CONVERSATION, RUN, OWNER, WHEN, run_request, app_for

FID = "60000000-0000-0000-0000-000000000001"
COMMENT = "70000000-0000-0000-0000-000000000001"
ROOT = f"/api/v1/agent/conversations/{CONVERSATION}/runs/{RUN}"


class Comments:
    def __init__(self):
        self.calls = []
        self.override = {}

    def post_feedback_comment(self, conversation_id, run_id, **kwargs):
        self.calls.append((conversation_id, run_id, kwargs))
        return dict(schema_version=1, conversation_id=conversation_id, run_id=run_id,
            followup_id=kwargs.get("followup_id"), comment_id=COMMENT,
            request_id=kwargs["request_id"], created_at=WHEN, **self.override)


@pytest.fixture
def agent():
    comments = Comments()
    return SimpleNamespace(feedback=comments, post_feedback_comment=comments.post_feedback_comment,
        followups=SimpleNamespace(submit=lambda *a, **kw: pytest.fail("文字反馈不能创建追问")))


@pytest.mark.parametrize("fid", [None, FID])
@pytest.mark.parametrize("content", [
    {"text": "原报告遗漏了连接池耗尽。"},
    {"analysis_text": "管理员已核对日志。", "analysis_reference": "analysis-123"},
    {"text": "   ", "analysis_text": "管理员已核对日志。", "analysis_reference": " "},
    {"text": "意见", "analysis_text": None, "analysis_reference": None},
])
def test_comment_receipt_and_normalized_material_bind_target_without_followup(agent, fid, content):
    path = ROOT + (f"/followups/{fid}" if fid else "") + "/feedback/comments"
    result = run_request(agent, "POST", path, json={"request_id": "comment-1", **content})
    assert result.status_code == 200
    data = result.json()["data"]
    assert data == dict(schema_version=1, conversation_id=CONVERSATION, run_id=RUN,
        followup_id=fid, comment_id=COMMENT, request_id="comment-1", created_at=WHEN)
    call = agent.feedback.calls[-1]
    assert call[:2] == (CONVERSATION, RUN)
    assert call[2]["owner_key"] == OWNER
    assert call[2].get("followup_id") == fid
    for key in ("text", "analysis_text", "analysis_reference"):
        value = content.get(key)
        assert call[2][key] == (value if value and value.strip() else None)


@pytest.mark.parametrize("fid", [None, FID])
@pytest.mark.parametrize("input", [
    {}, {"request_id": "x"}, {"request_id": "x", "text": " "},
    {"request_id": "x", "text": "意见", "rating": "DISLIKE"},
    {"request_id": "x", "text": "意见", "analysis_reference": "ref"},
    {"request_id": "x", "text": 1}, {"request_id": "x", "analysis_text": {}},
    {"request_id": "x", "analysis_text": "资料", "analysis_reference": "a" * 257},
    {"request_id": " ", "text": "意见"}, {"request_id": "x" * 129, "text": "意见"},
    {"request_id": "x", "text": "中" * 5462},
    {"request_id": "x", "analysis_text": "中" * 21846},
    {"request_id": "x", "text": "意见", "owner_key": OWNER},
])
def test_comment_rejects_invalid_material_before_service(agent, fid, input):
    path = ROOT + (f"/followups/{fid}" if fid else "") + "/feedback/comments"
    result = run_request(agent, "POST", path, json=input)
    assert result.status_code == 400
    assert not agent.feedback.calls


@pytest.mark.parametrize("fid", [None, FID])
def test_comment_checks_owner_query_receipt_and_business_conflict(agent, fid):
    path = ROOT + (f"/followups/{fid}" if fid else "") + "/feedback/comments"
    payload = {"request_id": "comment-1", "text": "意见"}
    for suffix, headers in [("?share=true", {}), ("", {"X-Agent-Owner-Key": "forged"})]:
        assert run_request(agent, "POST", path + suffix, json=payload, headers=headers).status_code == 400
    assert not agent.feedback.calls
    original = agent.feedback.post_feedback_comment
    def wrong(*args, **kwargs):
        return {**original(*args, **kwargs), "request_id": "another-request"}
    agent.feedback.post_feedback_comment = wrong
    agent.post_feedback_comment = wrong
    assert run_request(agent, "POST", path, json=payload).status_code == 500
    def conflict(*args, **kwargs):
        raise AgentStoreError("AGENT_FEEDBACK_REQUIRES_DISLIKE", "请先点踩。", 409)
    agent.feedback.post_feedback_comment = conflict
    agent.post_feedback_comment = conflict
    result = run_request(agent, "POST", path, json=payload)
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "AGENT_FEEDBACK_REQUIRES_DISLIKE"


def test_comment_contract_does_not_expand_existing_rating_contract():
    schema = app_for(None).openapi()["components"]["schemas"]
    assert set(schema["FeedbackRequest"]["properties"]) == {"request_id", "rating"}
    assert set(schema["FeedbackView"]["properties"]) == {"schema_version", "conversation_id", "run_id", "can_rate", "rating", "updated_at"}
    assert set(schema["FollowupFeedbackView"]["properties"]) == set(schema["FeedbackView"]["properties"]) | {"followup_id"}
    assert set(schema["FeedbackCommentRequest"]["properties"]) == {"request_id", "text", "analysis_text", "analysis_reference"}
