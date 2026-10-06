"""时间工具：统一使用带时区的 UTC 时间，测试时可注入时钟。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_ts(dt: datetime) -> str:
    return parse_ts(dt).isoformat().replace("+00:00", "Z")


def windows_overlap(
    start_a: datetime,
    end_a: datetime,
    start_b: datetime,
    end_b: datetime,
) -> bool:
    """半开区间 [start, end) 是否重叠。"""
    return start_a < end_b and start_b < end_a


class MutableClock:
    """测试/演示用固定时钟，可显式推进。"""

    def __init__(self, start: str | datetime = "2026-09-01T00:00:00Z") -> None:
        self._now = parse_ts(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)

    def set(self, value: str | datetime) -> None:
        self._now = parse_ts(value)
