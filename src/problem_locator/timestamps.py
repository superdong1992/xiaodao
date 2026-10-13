"""Service timestamps use a fixed UTC+08:00 offset, independent of host settings."""

from datetime import datetime, timedelta, timezone


LOCAL_TIMEZONE = timezone(timedelta(hours=8))


def parse_timestamp(value: str) -> datetime:
    """Parse an explicitly zoned timestamp without assuming the host timezone."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.utcoffset() is None:
        raise ValueError("时间戳必须包含时区偏移。")
    return moment


def format_timestamp(moment: datetime) -> str:
    """Keep the instant, convert its offset, and truncate to milliseconds."""
    if moment.utcoffset() is None:
        raise ValueError("时间戳必须包含时区偏移。")
    return moment.astimezone(LOCAL_TIMEZONE).isoformat(timespec="milliseconds")


def now_timestamp() -> str:
    return format_timestamp(datetime.now(LOCAL_TIMEZONE))
