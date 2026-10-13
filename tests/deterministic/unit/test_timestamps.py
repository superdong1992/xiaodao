from datetime import datetime, timedelta, timezone

import pytest

from problem_locator import timestamps
from problem_locator.bootstrap import ProductionClock
from problem_locator.contracts import ErrorCode
from problem_locator.operational import OperationalState
from problem_locator.timestamps import format_timestamp, parse_timestamp


@pytest.mark.parametrize(("source", "expected"), [
    ("2026-10-10T08:25:55.603999Z", "2026-10-10T16:25:55.603+08:00"),
    ("2026-12-31T16:00:00.000Z", "2027-01-01T00:00:00.000+08:00"),
    ("2024-02-29T20:00:00.123Z", "2024-03-01T04:00:00.123+08:00"),
    ("2026-10-10T16:25:55.603+08:00", "2026-10-10T16:25:55.603+08:00"),
    ("2026-10-10T03:25:55.603-05:00", "2026-10-10T16:25:55.603+08:00"),
])
def test_service_timestamp_converts_instant_and_calendar_boundaries(source, expected):
    assert format_timestamp(parse_timestamp(source)) == expected
    assert abs(parse_timestamp(expected).timestamp() - parse_timestamp(source).timestamp()) < .001


def test_production_clock_uses_explicit_offset_independent_of_host_timezone(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is not None
            assert tz.utcoffset(None) == timedelta(hours=8)
            return datetime(2026, 12, 31, 16, 0, 0, 123999, timezone.utc).astimezone(tz)

    monkeypatch.setattr(timestamps, "datetime", FixedDatetime)
    assert ProductionClock().now() == "2027-01-01T00:00:00.123+08:00"
    fault = OperationalState().record(case_id=None, job_id=None, phase="TEST",
                                     error_code=ErrorCode.DISPATCH_REJECTED)
    assert fault.occurred_at == "2027-01-01T00:00:00.123+08:00"


def test_service_timestamps_reject_implicit_host_timezone():
    with pytest.raises(ValueError, match="时区"):
        parse_timestamp("2026-10-10T08:25:55.603")
    with pytest.raises(ValueError, match="时区"):
        format_timestamp(datetime(2026, 10, 10, 8, 25, 55))
