from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from problem_locator.contracts import (
    SCHEMA_MODELS, LogparseEvidenceLocator, ServiceTimestamp,
    UserResultTimeObservationV2, UtcTimestamp,
)
from problem_locator.contracts.serialization import canonical_json_bytes, parse_canonical_json_bytes
from problem_locator.agent.models import PublicArtifactData
from problem_locator.followup.models import FollowupEvent, FollowupItem
from problem_locator.memory.models import FeedbackView

from tests.deterministic.contracts._support import FIXTURE_ROOT, load_json, schema_validator


LOCAL_TIME = "2026-10-10T12:34:56.789+08:00"
LEGACY_TIME = "2026-10-10T04:34:56.789Z"
ID = "00000000-0000-4000-8000-000000000001"
SERVICE_TIME_FIELDS = {
    "created_at", "updated_at", "started_at", "finished_at", "occurred_at",
    "collected_at", "produced_at", "processed_at", "recorded_at",
    "recovery_completed_at", "completed_at",
}


@pytest.mark.parametrize("value", [LOCAL_TIME, LEGACY_TIME])
def test_service_timestamp_preserves_new_local_and_legacy_utc_bytes(value: str) -> None:
    adapter = TypeAdapter(ServiceTimestamp)
    assert adapter.validate_python(value) == value
    assert adapter.dump_json(adapter.validate_json(adapter.dump_json(value))) == adapter.dump_json(value)


@pytest.mark.parametrize("value", [
    "2026-02-30T12:34:56.789+08:00",
    "2026-10-10T24:34:56.789+08:00",
    "2026-10-10T12:34:56.78+08:00",
    "2026-10-10T12:34:56.7890+08:00",
    "2026-10-10T12:34:56.789",
    "2026-10-10T12:34:56.789+8:00",
    "2026-10-10T12:34:56.789+09:00",
    "2026-10-10T12:34:56.789+00:00",
])
def test_service_timestamp_rejects_invalid_dates_precision_and_offsets(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ServiceTimestamp).validate_python(value)


def _use_local_service_times(value: object) -> object:
    if isinstance(value, list):
        return [_use_local_service_times(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in SERVICE_TIME_FIELDS and isinstance(item, str):
            item = datetime.fromisoformat(item.replace("Z", "+00:00")).astimezone(
                timezone(timedelta(hours=8))
            ).isoformat(timespec="milliseconds")
        result[key] = _use_local_service_times(item)
    return result


@pytest.mark.parametrize(("fixture", "schema"), [
    ("state.json", "state.schema.json"),
    ("job-route.json", "job.schema.json"),
    ("agent-job-outcome-diagnosis.json", "agent-job-outcome.schema.json"),
    ("agent-job-outcome-failure.json", "agent-job-outcome.schema.json"),
    ("job-outcome-diagnosis.json", "job-outcome.schema.json"),
    ("job-outcome-failure.json", "job-outcome.schema.json"),
])
def test_local_service_times_survive_public_schema_and_persistence_roundtrip(
    fixture: str, schema: str,
) -> None:
    payload = _use_local_service_times(load_json(FIXTURE_ROOT / "positive" / fixture))
    schema_validator(schema).validate(payload)
    model_type = SCHEMA_MODELS[schema]
    model = TypeAdapter(model_type).validate_python(payload)
    raw = canonical_json_bytes(model)
    assert b"+08:00" in raw
    assert parse_canonical_json_bytes(raw, model_type=model_type) == model
    assert canonical_json_bytes(payload) == raw


def test_state_preserves_legacy_creation_time_when_update_uses_local_offset() -> None:
    payload = load_json(FIXTURE_ROOT / "positive" / "state.json")
    original_created_at = payload["created_at"]
    payload["updated_at"] = LOCAL_TIME
    state = TypeAdapter(SCHEMA_MODELS["state.schema.json"]).validate_python(payload)
    assert state.created_at == original_created_at
    assert state.updated_at == LOCAL_TIME
    assert canonical_json_bytes(state) == canonical_json_bytes(payload)


def test_problem_time_and_log_evidence_remain_strict_utc() -> None:
    assert TypeAdapter(UtcTimestamp).validate_python(LEGACY_TIME) == LEGACY_TIME
    with pytest.raises(ValidationError):
        TypeAdapter(UtcTimestamp).validate_python(LOCAL_TIME)
    payload = load_json(FIXTURE_ROOT / "positive" / "logparse-parse-claim.json")
    payload["problem_time"] = LOCAL_TIME
    with pytest.raises(ValidationError):
        TypeAdapter(SCHEMA_MODELS["logparse-parse-claim.schema.json"]).validate_python(payload)
    with pytest.raises(ValidationError):
        LogparseEvidenceLocator(kind="LOGPARSE", relative_path="parsed/events.log",
                                start_line=None, end_line=None,
                                start_time=LOCAL_TIME, end_time=LOCAL_TIME)
    with pytest.raises(ValidationError):
        UserResultTimeObservationV2(rule_id="rule-1", event_time=LOCAL_TIME, offset_ms=0)


@pytest.mark.parametrize("value", [LOCAL_TIME, LEGACY_TIME])
def test_feedback_and_followup_accept_service_timestamps(value: str) -> None:
    feedback = FeedbackView(conversation_id=ID, run_id=ID, can_rate=True,
                            rating="LIKE", updated_at=value)
    assert feedback.model_dump(mode="json")["updated_at"] == value
    item = FollowupItem(followup_id=ID, run_id=ID, request_id="request-1", ordinal=1,
                        status="QUEUED", context_mode="REPORT_ONLY", text="原因是什么？",
                        created_at=value, updated_at=value)
    event = FollowupEvent(sequence=1, conversation_id=ID, run_id=ID, followup_id=ID,
                          type="followup.accepted", created_at=value, data=item)
    assert event.model_dump(mode="json")["data"]["updated_at"] == value
    assert event.model_dump(mode="json")["created_at"] == value
    artifact = PublicArtifactData(artifact_id=ID, kind="GENERIC_REPORT", name="report.md",
                                  content_type="text/markdown", resource_kind="FILE", size=1,
                                  sha256="a" * 64, created_by_job_id=ID, created_at=value,
                                  downloadable=True)
    assert artifact.model_dump(mode="json")["created_at"] == value
