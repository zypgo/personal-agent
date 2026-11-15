"""Time sources and timezone helpers.

All timestamps stored by the agent are UTC epoch seconds (float). Local, human-facing
time is derived through the owner's timezone only at the edges (prompts, UI).
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, tzinfo
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class ManualClock:
    """A clock tests can move. Never used in production code paths."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        self._now += seconds
        return self._now

    def set(self, value: float) -> None:
        self._now = value


def detect_timezone() -> str:
    """Best-effort IANA timezone name of this machine ("UTC" if it cannot be determined)."""
    candidate = os.environ.get("TZ", "").lstrip(":")
    if candidate and _valid_zone(candidate):
        return candidate
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        target = ""
    marker = "zoneinfo/"
    if marker in target:
        name = target.split(marker, 1)[1]
        if _valid_zone(name):
            return name
    return "UTC"


def _valid_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def get_tz(name: str | None) -> tzinfo:
    """Resolve a configured zone name; fall back to the detected zone, then UTC."""
    for candidate in (name, detect_timezone()):
        if candidate and _valid_zone(candidate):
            return ZoneInfo(candidate)
    return ZoneInfo("UTC")


def to_local(ts: float, tz: tzinfo) -> datetime:
    return datetime.fromtimestamp(ts, tz)


def format_local(ts: float, tz: tzinfo) -> str:
    """`2026-11-15 18:20 Sunday (Asia/Shanghai, UTC+08:00)`"""
    dt = to_local(ts, tz)
    offset = dt.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}" if offset else "+00:00"
    zone = getattr(tz, "key", None) or dt.tzname() or "local"
    return f"{dt:%Y-%m-%d %H:%M} {dt:%A} ({zone}, UTC{offset})"


def start_of_day(ts: float, tz: tzinfo) -> float:
    """Local midnight at the start of the day `ts` falls in (UTC epoch seconds)."""
    return to_local(ts, tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def start_of_next_day(ts: float, tz: tzinfo) -> float:
    midnight = to_local(ts, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight + timedelta(days=1)).timestamp()


_QUIET = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_quiet_hours(text: str) -> tuple[int, int]:
    """`"23:00-08:00"` -> (start, end) as minutes since local midnight. The window may wrap past
    midnight."""
    match = _QUIET.match(text)
    if match is None:
        raise ValueError(f'quiet_hours must look like "23:00-08:00", got {text!r}')
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    if not (0 <= h1 < 24 and 0 <= h2 < 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
        raise ValueError(f"quiet_hours has an impossible time: {text!r}")
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if start == end:
        raise ValueError("quiet_hours must start and end at different times")
    return start, end


def in_quiet_hours(ts: float, tz: tzinfo, window: tuple[int, int]) -> bool:
    local = to_local(ts, tz)
    minute = local.hour * 60 + local.minute
    start, end = window
    return start <= minute < end if start < end else minute >= start or minute < end


def quiet_hours_end(ts: float, tz: tzinfo, window: tuple[int, int]) -> float:
    """When the quiet window that contains `ts` ends (UTC epoch seconds)."""
    local = to_local(ts, tz)
    end = window[1]
    end_at = local.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if end_at <= local:
        end_at += timedelta(days=1)
    return end_at.timestamp()
