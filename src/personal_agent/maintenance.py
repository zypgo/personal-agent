"""Keeping state.db to a size that stays reasonable, and the tools to look at the file and care for it.

An agent that is always on writes all the time: every event, message, tool call and run leaves a
row, and most of that is only of use for a while. `Janitor` removes the rows that have *ended* and
are older than the window the owner chose (`[retention]` in config.toml). A message that is still
to be sent, a timer that is still to fire, an approval nobody answered and a run that is going are
never touched, and neither is what was said or what the agent remembers.

The design follows two projects that run a SQLite store for years (see
docs/research/reference-analysis.md): Home Assistant's recorder purges in small batches, once a
night, so that nothing else waits for it, and rewrites the file only on request; hermes-agent
refuses a negative age, because "older than" then matches everything, and rewrites the file only
while nobody else is using it.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from personal_agent.clock import Clock
from personal_agent.config import RetentionConfig, Window
from personal_agent.db import Database, dumps, loads

log = logging.getLogger(__name__)

DAY_S = 86_400.0
EVERY_S = DAY_S
"""The running agent tidies up this often."""
BATCH = 500
"""Rows removed in one transaction: short enough that the agent never waits long for the database."""
PASS_ROWS = 50_000
"""Rows picked out at a time; a table with more is dealt with in several passes."""
PAUSE_S = 0.002
"""Between two batches the database is left to everyone else for this long."""
LAST_KEY = "maintenance:last"


@dataclass(frozen=True)
class Rule:
    """One kind of record that can be removed, and when."""

    name: str
    """What `pa db prune` and its report call it."""
    table: str
    window: Window
    """Which of the owner's four retention settings decides how old the row must be."""
    ended: str
    """SQL: the moment the row stopped mattering. It may go once that is older than the window."""
    only: str
    """SQL: which rows have ended at all. Every other row is alive and is never removed."""
    what: str
    """A few words, for `pa db`."""


def _never_suspended(column: str, table: str) -> str:
    """Unfinished approval execution or dream review keeps its run and decision records."""
    return (
        f"NOT EXISTS (SELECT 1 FROM run_states WHERE run_states.run_id = {table}.{column})"
        f" AND NOT EXISTS (SELECT 1 FROM kv WHERE key = 'dream:pending:' || {table}.{column})"
    )


RULES: tuple[Rule, ...] = (
    Rule(
        "events",
        "events",
        "records",
        "COALESCE(done_at, created_at)",
        "status IN ('done', 'dead') AND NOT EXISTS "
        "(SELECT 1 FROM dead_event_notices WHERE event_id=events.id)",
        "inbox events that were handled",
    ),
    Rule(
        "outbox",
        "outbox",
        "records",
        "COALESCE(sent_at, created_at)",
        "status IN ('sent', 'cancelled')",
        "messages delivered or dismissed (failed ones stay until dismissed)",
    ),
    Rule(
        "timers",
        "timers",
        "records",
        "COALESCE(last_fired_at, created_at)",
        "status IN ('fired', 'cancelled')",
        "timers that fired or were cancelled",
    ),
    Rule(
        "approvals",
        "approvals",
        "records",
        "COALESCE(decided_at, expires_at)",
        f"status != 'pending' AND {_never_suspended('run_id', 'approvals')}",
        "approval requests that were answered",
    ),
    Rule(
        "pairings",
        "pairings",
        "records",
        "expires_at",
        "1",
        "chat pairing codes that expired",
    ),
    Rule(
        "web_invites",
        "web_invites",
        "records",
        "MIN(expires_at, COALESCE(used_at, expires_at))",
        "1",
        "web pairing links that were used or expired",
    ),
    Rule(
        "web_sessions",
        "web_sessions",
        "records",
        "MIN(expires_at, COALESCE(revoked_at, expires_at))",
        "1",
        "browser sessions that expired or were revoked",
    ),
    Rule(
        "runs",
        "runs",
        "runs",
        "COALESCE(finished_at, started_at)",
        f"status IN ('completed', 'silent', 'failed', 'cancelled') AND {_never_suspended('id', 'runs')}",
        "runs that finished",
    ),
    Rule("spend", "spend", "runs", "at", "1", "speech-to-text charges"),
    Rule("usage_events", "usage_events", "runs", "at", "1", "model usage at the time it happened"),
    Rule(
        "ingress_receipts",
        "ingress_receipts",
        "records",
        "updated_at",
        "state IN ('done', 'rejected', 'uncertain')",
        "completed voice-message receipts",
    ),
    Rule("audit", "audit", "audit", "ts", "1", "tool call records"),
    Rule(
        "memory_history",
        "memory_versions",
        "memory_history",
        "created_at",
        "id NOT IN (SELECT MAX(id) FROM memory_versions GROUP BY path) "
        # An unfinished dream needs every version from its first edit onward, including later
        # owner edits used to detect a rollback conflict. Its first old_content is the baseline.
        "AND id < COALESCE((SELECT MIN(v.id) FROM memory_versions v WHERE EXISTS "
        "(SELECT 1 FROM kv WHERE key = 'dream:pending:' || v.run_id)), 9223372036854775807)",
        "older versions of memory files (the newest of each stays)",
    ),
)

RULE_NAMES = tuple(rule.name for rule in RULES)


@dataclass(frozen=True)
class Tidy:
    """What one round of upkeep did."""

    at: float
    removed: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    error: str | None = None

    @property
    def total(self) -> int:
        return sum(self.removed.values())

    def to_json(self) -> str:
        return dumps(
            {"at": self.at, "removed": self.removed, "seconds": self.seconds, "error": self.error}
        )

    @classmethod
    def from_json(cls, text: str | None) -> Tidy | None:
        """What `to_json` wrote; None for anything else (a damaged or missing record)."""
        data = loads(text)
        try:
            error = data.get("error")
            return cls(
                at=float(data["at"]),
                removed={str(k): int(v) for k, v in data["removed"].items()},
                seconds=float(data.get("seconds", 0.0)),
                error=None if error is None else str(error),
            )
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
            return None


class Janitor:
    """Removes finished records that are older than the retention windows."""

    def __init__(self, db: Database, retention: RetentionConfig, clock: Clock) -> None:
        self.db = db
        self.retention = retention
        self.clock = clock

    # ---- what would go ------------------------------------------------------------------
    def _cutoff(self, rule: Rule) -> float | None:
        """Rows that ended before this moment can go; None when the window is 0 (keep for ever)."""
        days = self.retention.days(rule.window)
        return None if days == 0 else self.clock.now() - days * DAY_S

    @staticmethod
    def _where(rule: Rule) -> str:
        return f"({rule.only}) AND ({rule.ended}) < ?"

    def eligible(self) -> dict[str, int]:
        """How many rows each rule would remove now. A rule whose window is 0 is left out."""
        counts: dict[str, int] = {}
        for rule in RULES:
            cutoff = self._cutoff(rule)
            if cutoff is not None:
                counts[rule.name] = int(
                    self.db.scalar(
                        f"SELECT COUNT(*) FROM {rule.table} WHERE {self._where(rule)}",
                        (cutoff,),
                        default=0,
                    )
                )
        return counts

    # ---- removing it --------------------------------------------------------------------
    def prune(
        self, removed: dict[str, int] | None = None, *, stop: Callable[[], bool] = lambda: False
    ) -> dict[str, int]:
        """Remove what is due, in small batches. Returns how many rows each rule removed. Pass a
        dictionary to see the count as it grows, which is how far it got if something stops it.
        `stop` is asked between two batches: when it says yes, the rest is left for next time."""
        removed = {} if removed is None else removed
        for rule in RULES:
            if stop():
                break
            cutoff = self._cutoff(rule)
            if cutoff is not None:
                removed[rule.name] = 0
                self._prune(rule, cutoff, removed, stop)
        return removed

    def _prune(
        self, rule: Rule, cutoff: float, removed: dict[str, int], stop: Callable[[], bool]
    ) -> None:
        where = self._where(rule)
        while True:
            picked = [
                row[0]
                for row in self.db.query(
                    f"SELECT rowid FROM {rule.table} WHERE {where} ORDER BY rowid LIMIT {PASS_ROWS}",
                    (cutoff,),
                )
            ]
            for start in range(0, len(picked), BATCH):
                if stop():
                    return
                batch = picked[start : start + BATCH]
                marks = ",".join("?" * len(batch))
                # The condition is checked again: a row may have changed since it was picked.
                with self.db.tx():
                    cur = self.db.execute(
                        f"DELETE FROM {rule.table} WHERE rowid IN ({marks}) AND {where}",
                        (*batch, cutoff),
                    )
                removed[rule.name] += cur.rowcount
                time.sleep(PAUSE_S)
            if len(picked) < PASS_ROWS:
                return

    # ---- the daily round ----------------------------------------------------------------
    def last(self) -> Tidy | None:
        return Tidy.from_json(self.db.kv_get(LAST_KEY))

    def due(self) -> bool:
        """The running agent is to tidy up: it is switched on and it has been a day (or the clock
        was set back by more than that since the last time)."""
        if not self.retention.auto:
            return False
        last = self.last()
        return last is None or abs(self.clock.now() - last.at) >= EVERY_S

    def maintain(self, *, stop: Callable[[], bool] = lambda: False) -> Tidy:
        """Remove what is due, let SQLite refresh its statistics and fold the write-ahead log into
        the file, and write down what happened. A database that is busy for the moment (another
        program is writing) is reported in the result, not raised: the next round tries again.
        A round that `stop` cut short is not written down as done, so the next start finishes it."""
        started = self.clock.now()
        began = time.monotonic()
        removed: dict[str, int] = {}
        error: str | None = None
        try:
            self.prune(removed, stop=stop)
            if stop():
                return Tidy(started, removed, time.monotonic() - began, "stopped early")
            self.db.optimize()
            self.db.checkpoint("PASSIVE")
        except sqlite3.Error as exc:
            error = str(exc) or type(exc).__name__
            log.warning("database upkeep stopped early: %s", error)
        except Exception as exc:
            # Written down all the same: otherwise the running agent would try again every few seconds.
            self._remember(Tidy(started, removed, time.monotonic() - began, repr(exc)))
            raise
        tidy = Tidy(started, removed, time.monotonic() - began, error)
        self._remember(tidy)
        return tidy

    def _remember(self, tidy: Tidy) -> None:
        self.db.kv_set(LAST_KEY, tidy.to_json(), now=tidy.at)


# ---- looking at the file ------------------------------------------------------------------
class Queryable(Protocol):
    """A `Database` or a plain `sqlite3.Connection`: the look-only functions below work on either,
    so they also work on a file that the program itself cannot open."""

    def execute(self, sql: str, /) -> sqlite3.Cursor: ...


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _count(conn: Queryable, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {_quoted(table)}").fetchone()[0])


def _pragma(conn: Queryable, name: str) -> int:
    return int(conn.execute(f"PRAGMA {name}").fetchone()[0])


def size(count: int) -> str:
    """ "812 B", "4.3 kB", "12.4 MB" or "1.2 GB": bytes, in the largest unit that keeps the number
    under a thousand."""
    value = float(max(0, count))
    units = ["B", "kB", "MB", "GB"]
    unit = units.pop(0)
    while round(value, 1) >= 1000 and units:
        value /= 1000
        unit = units.pop(0)
    return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"


@dataclass(frozen=True)
class Footprint:
    """How big the database is, and what it is made of."""

    db_bytes: int
    wal_bytes: int
    page_size: int
    pages: int
    free_pages: int
    schema: int
    rows: dict[str, int]

    @property
    def reclaimable_bytes(self) -> int:
        """What rewriting the file (`vacuum`) would give back: the pages deleted rows left empty."""
        return self.free_pages * self.page_size

    @property
    def total_bytes(self) -> int:
        return self.db_bytes + self.wal_bytes


def _own_tables(conn: Queryable) -> list[str]:
    """The tables the program made, not the ones a search index keeps its data in
    (`transcript_fts_data`, ...)."""
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]
    virtual = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE sql LIKE 'CREATE VIRTUAL TABLE%'"
        )
    ]
    return [
        t for t in tables if t not in virtual and not any(t.startswith(f"{v}_") for v in virtual)
    ]


def footprint(conn: Queryable, path: Path, *, rows: bool = True) -> Footprint:
    """The size of the database; `rows=False` leaves out counting what is in each table."""
    wal = path.with_name(path.name + "-wal")
    return Footprint(
        db_bytes=path.stat().st_size,
        wal_bytes=wal.stat().st_size if wal.exists() else 0,
        page_size=_pragma(conn, "page_size"),
        pages=_pragma(conn, "page_count"),
        free_pages=_pragma(conn, "freelist_count"),
        schema=_pragma(conn, "user_version"),
        rows={table: _count(conn, table) for table in _own_tables(conn)} if rows else {},
    )


def problems(conn: Queryable, *, full: bool = False) -> list[str]:
    """What is wrong with the file; empty when nothing is. `full` reads everything and checks the
    indexes against the tables, which takes a while on a big file."""
    found = [
        str(row[0])
        for row in conn.execute(f"PRAGMA {'integrity_check' if full else 'quick_check'}")
    ]
    if found == ["ok"]:
        found = []
    rows: list[Any] = conn.execute("PRAGMA foreign_key_check").fetchall()
    found += [f"a row of {row[0]} points at a row that does not exist" for row in rows]
    return found
