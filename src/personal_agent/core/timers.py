"""Timers: the agent's way of waking itself (follow-ups) and the owner's recurring schedules.

Two kinds, one table:
- `followup`: fires once at a given moment, then is done ("check on this tomorrow at 9").
- `schedule`: a cron expression evaluated in a time zone ("every weekday at 08:00").

Firing writes an event into the inbox and advances the timer in the same transaction, so a crash
cannot fire twice or lose a firing, and two processes ticking at once produce one event
(the event's `external_id` is `timer:<id>:<due>`).

If the computer was asleep, a schedule never "catches up" with a pile of missed runs: it fires at
most once for the whole gap, or skips it, depending on its misfire policy.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from personal_agent.clock import Clock, SystemClock, format_local, get_tz
from personal_agent.config import SchedulerConfig
from personal_agent.core.events import Kind, NewEvent, Trust
from personal_agent.core.inbox import Inbox
from personal_agent.db import Database

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
MAX_PROMPT_CHARS = 4000
MAX_FOLLOWUP_DAYS = 366


class TimerError(ValueError):
    """The request is invalid; the message is written for the person (or model) who made it."""


@dataclass(frozen=True)
class Timer:
    id: int
    name: str
    kind: str
    cron: str | None
    tz: str | None
    due_at: float
    prompt: str
    session_key: str
    trust: Trust
    status: str
    misfire: str
    created_by: str
    created_at: float
    last_fired_at: float | None
    fire_count: int


@dataclass(frozen=True)
class Fired:
    timer: Timer
    event_id: int | None
    """None when the firing was skipped by the misfire policy."""
    late_s: float
    skipped: bool = False


def _timer(row: sqlite3.Row) -> Timer:
    return Timer(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        cron=row["cron"],
        tz=row["tz"],
        due_at=row["due_at"],
        prompt=row["prompt"],
        session_key=row["session_key"],
        trust=Trust(row["trust"]),
        status=row["status"],
        misfire=row["misfire"],
        created_at=row["created_at"],
        created_by=row["created_by"],
        last_fired_at=row["last_fired_at"],
        fire_count=row["fire_count"],
    )


def next_fire(cron: str, tz_name: str, after: float) -> float:
    """The first firing of `cron` (evaluated in `tz_name`) strictly after `after`."""
    base = datetime.fromtimestamp(after, ZoneInfo(tz_name))
    return croniter(cron, base).get_next(datetime).timestamp()


class Timers:
    def __init__(
        self,
        db: Database,
        inbox: Inbox,
        cfg: SchedulerConfig,
        *,
        clock: Clock | None = None,
        default_tz: str = "UTC",
    ) -> None:
        self.db = db
        self.inbox = inbox
        self.cfg = cfg
        self.clock = clock or SystemClock()
        self.default_tz = default_tz

    # ---- creating ---------------------------------------------------------------------
    def add_followup(
        self,
        *,
        name: str,
        prompt: str,
        due_at: float,
        session_key: str | None = None,
        trust: Trust = Trust.OWNER,
        created_by: str = "agent",
    ) -> Timer:
        now = self.clock.now()
        if due_at < now - 1:
            raise TimerError("that time is in the past")
        if due_at > now + MAX_FOLLOWUP_DAYS * 86400:
            raise TimerError(f"a follow-up cannot be more than {MAX_FOLLOWUP_DAYS} days away")
        return self._insert(
            name=name,
            kind="followup",
            cron=None,
            tz=None,
            due_at=due_at,
            prompt=prompt,
            session_key=session_key,
            trust=trust,
            misfire="fire_once",
            created_by=created_by,
        )

    def add_schedule(
        self,
        *,
        name: str,
        prompt: str,
        cron: str,
        tz: str | None = None,
        session_key: str | None = None,
        trust: Trust = Trust.OWNER,
        created_by: str = "agent",
        misfire: str = "fire_once",
    ) -> Timer:
        tz_name = tz or self.default_tz
        try:
            ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise TimerError(f"unknown time zone {tz_name!r}") from exc
        cron = " ".join(cron.split())
        if len(cron.split()) != 5 or not croniter.is_valid(cron):
            raise TimerError(
                f"{cron!r} is not a valid 5-field cron expression (minute hour day month weekday)"
            )
        if misfire not in ("fire_once", "skip"):
            raise TimerError("misfire must be 'fire_once' or 'skip'")
        now = self.clock.now()
        self._check_interval(cron, tz_name, now)
        return self._insert(
            name=name,
            kind="schedule",
            cron=cron,
            tz=tz_name,
            due_at=next_fire(cron, tz_name, now),
            prompt=prompt,
            session_key=session_key,
            trust=trust,
            misfire=misfire,
            created_by=created_by,
        )

    def _check_interval(self, cron: str, tz_name: str, now: float) -> None:
        """Reject schedules that would fire more often than allowed (each firing costs money)."""
        t, smallest = now, float("inf")
        previous = None
        for _ in range(6):
            t = next_fire(cron, tz_name, t)
            if previous is not None:
                smallest = min(smallest, t - previous)
            previous = t
        if smallest < self.cfg.min_interval_s:
            raise TimerError(
                f"that schedule can fire every {int(smallest)}s; the minimum gap is "
                f"{self.cfg.min_interval_s}s"
            )

    def _insert(
        self,
        *,
        name: str,
        kind: str,
        cron: str | None,
        tz: str | None,
        due_at: float,
        prompt: str,
        session_key: str | None,
        trust: Trust,
        misfire: str,
        created_by: str,
    ) -> Timer:
        name = name.strip()
        if not NAME_RE.match(name) or name.isdigit():
            raise TimerError(
                "name must be 1-64 characters: lowercase letters, digits, '-', '_' or '.', "
                "starting with a letter or digit, and not only digits"
            )
        prompt = prompt.strip()
        if not prompt:
            raise TimerError("the prompt (what to do when it fires) cannot be empty")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise TimerError(
                f"the prompt is {len(prompt)} characters; the limit is {MAX_PROMPT_CHARS}"
            )
        with self.db.tx():
            live = self.db.scalar(
                "SELECT COUNT(*) FROM timers WHERE status IN ('active', 'paused')", default=0
            )
            if live >= self.cfg.max_timers:
                raise TimerError(
                    f"there are already {live} timers (limit {self.cfg.max_timers}); cancel some first"
                )
            try:
                cur = self.db.execute(
                    """INSERT INTO timers (name, kind, cron, tz, due_at, prompt, session_key, trust,
                                           misfire, created_by, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        name,
                        kind,
                        cron,
                        tz,
                        due_at,
                        prompt,
                        session_key or f"timer:{name}",
                        trust.value,
                        misfire,
                        created_by,
                        self.clock.now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TimerError(f"a timer named {name!r} already exists") from exc
            row = self.db.one("SELECT * FROM timers WHERE id = ?", (cur.lastrowid,))
        return _timer(row)

    # ---- managing ---------------------------------------------------------------------
    def get(self, ref: str | int) -> Timer | None:
        if isinstance(ref, int) or str(ref).isdigit():
            row = self.db.one("SELECT * FROM timers WHERE id = ?", (int(ref),))
        else:
            row = self.db.one(
                "SELECT * FROM timers WHERE name = ? ORDER BY id DESC LIMIT 1", (str(ref),)
            )
        return _timer(row) if row else None

    def list(self, *, include_finished: bool = False) -> list[Timer]:
        sql = "SELECT * FROM timers"
        if not include_finished:
            sql += " WHERE status IN ('active', 'paused')"
        rows = self.db.query(sql + " ORDER BY status, due_at, id")
        return [_timer(r) for r in rows]

    def cancel(self, ref: str | int) -> bool:
        timer = self.get(ref)
        if timer is None or timer.status not in ("active", "paused"):
            return False
        with self.db.tx():
            cur = self.db.execute(
                "UPDATE timers SET status = 'cancelled' WHERE id = ? AND status IN ('active','paused')",
                (timer.id,),
            )
        return cur.rowcount == 1

    def pause(self, ref: str | int) -> bool:
        timer = self.get(ref)
        if timer is None:
            return False
        with self.db.tx():
            cur = self.db.execute(
                "UPDATE timers SET status = 'paused' WHERE id = ? AND status = 'active'",
                (timer.id,),
            )
        return cur.rowcount == 1

    def resume(self, ref: str | int) -> bool:
        """Un-pause. A schedule continues from the next regular time (no backlog)."""
        timer = self.get(ref)
        if timer is None or timer.status != "paused":
            return False
        now = self.clock.now()
        due = (
            next_fire(timer.cron, timer.tz or self.default_tz, now) if timer.cron else timer.due_at
        )
        with self.db.tx():
            self.db.execute(
                "UPDATE timers SET status = 'active', due_at = ? WHERE id = ? AND status = 'paused'",
                (due, timer.id),
            )
        return True

    # ---- firing -----------------------------------------------------------------------
    def next_due(self) -> float | None:
        value = self.db.scalar("SELECT MIN(due_at) FROM timers WHERE status = 'active'")
        return None if value is None else float(value)

    def tick(self) -> list[Fired]:
        """Fire everything that is due. Safe to call from several processes at once."""
        now = self.clock.now()
        fired: list[Fired] = []
        for candidate in self.db.query(
            "SELECT id FROM timers WHERE status = 'active' AND due_at <= ? ORDER BY due_at, id",
            (now,),
        ):
            result = self._fire(candidate["id"], now)
            if result is not None:
                fired.append(result)
        return fired

    def _fire(self, timer_id: int, now: float) -> Fired | None:
        with self.db.tx():
            # Re-read inside the transaction: another process may have fired it a moment ago.
            row = self.db.one(
                "SELECT * FROM timers WHERE id = ? AND status = 'active' AND due_at <= ?",
                (timer_id, now),
            )
            if row is None:
                return None
            timer = _timer(row)
            late = max(0.0, now - timer.due_at)
            skipped = (
                timer.kind == "schedule"
                and timer.misfire == "skip"
                and late > self.cfg.misfire_grace_s
            )
            event_id = None
            if not skipped:
                event_id = self.inbox.add(self._event(timer, late)).id
            if timer.kind == "followup":
                self.db.execute(
                    """UPDATE timers SET status = 'fired', last_fired_at = ?, fire_count = fire_count + 1
                       WHERE id = ?""",
                    (now, timer.id),
                )
            else:
                next_due = next_fire(timer.cron, timer.tz or self.default_tz, now)
                self.db.execute(
                    """UPDATE timers SET due_at = ?, last_fired_at = COALESCE(?, last_fired_at),
                       fire_count = fire_count + ? WHERE id = ?""",
                    (next_due, None if skipped else now, 0 if skipped else 1, timer.id),
                )
        return Fired(timer=timer, event_id=event_id, late_s=late, skipped=skipped)

    def _event(self, timer: Timer, late: float) -> NewEvent:
        tz = get_tz(timer.tz or self.default_tz)
        return NewEvent(
            source="timer",
            kind=Kind.TIMER,
            session_key=timer.session_key,
            payload={
                "name": timer.name,
                "prompt": timer.prompt,
                "timer_id": timer.id,
                "timer_kind": timer.kind,
                "scheduled_for": format_local(timer.due_at, tz),
                "late_s": int(late),
            },
            external_id=f"timer:{timer.id}:{int(timer.due_at)}",
            trust=timer.trust,
        )
