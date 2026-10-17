"""Minimal website feedback contracts and private report source identity."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from problem_locator.contracts.models import OpaqueId, ServiceTimestamp

Rating = Literal["LIKE", "DISLIKE"]


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(min_length=1, max_length=128, pattern=r"\S")
    rating: Rating


class FeedbackView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    conversation_id: OpaqueId
    run_id: OpaqueId
    can_rate: bool
    rating: Rating | None = None
    updated_at: ServiceTimestamp | None = None

    @model_validator(mode="after")
    def paired_rating_time(self):
        if (self.rating is None) != (self.updated_at is None):
            raise ValueError("反馈及其更新时间必须同时存在或同时为空。")
        return self


@dataclass(frozen=True, slots=True)
class FeedbackSource:
    case_id: str
    source_job_id: str
    skill_name: str
    problem_text: str
    report_markdown: str
    report_sha256: str
    answer_key: str = ""
    source_kind: Literal["REPORT", "FOLLOWUP", "CORRECTION"] = "REPORT"
    context_json: str = ""
    replaces_answer_key: str | None = None

class FollowupFeedbackView(FeedbackView):
    followup_id: OpaqueId


class FeedbackCommentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(min_length=1, max_length=128, pattern=r"\S")
    text: str | None = Field(default=None, max_length=16384, description="用户意见，可选，UTF-8 最多 16 KiB。")
    analysis_text: str | None = Field(default=None, max_length=65536, description="网站后端附带的管理员分析资料，可选，UTF-8 最多 64 KiB。")
    analysis_reference: str | None = Field(default=None, max_length=256, description="管理员资料的来源标识；仅供追溯，不会自动访问。")

    @field_validator("text", "analysis_text", "analysis_reference")
    @classmethod
    def normalize_content(cls, value, info):
        if value is None:
            return None
        limit = {"text": 16384, "analysis_text": 65536}.get(info.field_name)
        if limit is not None and len(value.encode("utf-8")) > limit:
            raise ValueError("反馈文字超过 UTF-8 字节上限。")
        return value if value.strip() else None

    @model_validator(mode="after")
    def require_content(self):
        if self.text is None and self.analysis_text is None:
            raise ValueError("请填写意见或附带管理员分析资料。")
        if self.analysis_reference is not None and self.analysis_text is None:
            raise ValueError("资料来源标识必须与管理员分析资料一起提交。")
        return self


class FeedbackCommentReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    conversation_id: OpaqueId
    run_id: OpaqueId
    followup_id: OpaqueId | None = None
    comment_id: OpaqueId
    request_id: str = Field(min_length=1, max_length=128, pattern=r"\S")
    created_at: ServiceTimestamp
