"""Time sources and timezone helpers.

All timestamps stored by the agent are UTC epoch seconds (float). Local, human-facing
time is derived through the owner's timezone only at the edges (prompts, UI).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, tzinfo
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
