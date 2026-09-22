"""Minimal website feedback contracts and private report source identity."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from problem_locator.contracts.models import OpaqueId, UtcTimestamp

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
    updated_at: UtcTimestamp | None = None

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
