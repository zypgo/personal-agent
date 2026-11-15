"""SQLite access: WAL, short busy timeout, explicit transactions, forward-only migrations.

One `Database` object owns one connection. The agent's I/O volume is tiny and every
statement is a local file operation, so synchronous calls are fine even from async code;
an RLock keeps the connection safe if a tool runs in a worker thread.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------------------
# Migrations. Append only; never edit a released migration.
# --------------------------------------------------------------------------------------
MIGRATIONS: list[str] = [
    # ---- 1: iteration 1 kernel ----------------------------------------------------------
    """
    -- Event inbox: everything that can wake the agent lands here first.
    CREATE TABLE events (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        source           TEXT NOT NULL,     -- cli | web | telegram | webhook:<name> | timer | system
        kind             TEXT NOT NULL,     -- message | timer | approval_response | webhook | system
        session_key      TEXT NOT NULL,
        payload          TEXT NOT NULL,     -- JSON
        external_id      TEXT,              -- idempotency key within `source`
        trust            TEXT NOT NULL DEFAULT 'owner',   -- owner | untrusted
        status           TEXT NOT NULL DEFAULT 'pending', -- pending | leased | done | dead
        attempts         INTEGER NOT NULL DEFAULT 0,      -- claims so far (poison-pill guard)
        lease_owner      TEXT,
        lease_expires_at REAL,
        last_error       TEXT,
        created_at       REAL NOT NULL,
        available_at     REAL NOT NULL,     -- not claimable before this (retry backoff / delay)
        done_at          REAL
    );
    CREATE UNIQUE INDEX events_dedupe ON events(source, external_id) WHERE external_id IS NOT NULL;
    CREATE INDEX events_ready ON events(status, available_at);
    CREATE INDEX events_session ON events(session_key, status);

    -- Append-only transcript. The model-visible history is a projection of this table.
    CREATE TABLE session_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_key TEXT NOT NULL,
        kind        TEXT NOT NULL,          -- item | tombstone | clear | compaction
        item        TEXT NOT NULL,          -- JSON
        event_id    INTEGER,                -- inbox event this item came from (idempotent append)
        run_id      TEXT,
        created_at  REAL NOT NULL
    );
    CREATE INDEX session_events_key ON session_events(session_key, id);
    CREATE UNIQUE INDEX session_events_event ON session_events(session_key, event_id)
        WHERE event_id IS NOT NULL AND kind = 'item';

    -- Outbox: everything the agent says goes through here. "Sent" means confirmed delivered.
    CREATE TABLE outbox (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_key TEXT NOT NULL,
        channel     TEXT NOT NULL,          -- cli | web | telegram | ...
        recipient   TEXT,                   -- channel-specific address (chat id, ...)
        kind        TEXT NOT NULL DEFAULT 'message',  -- message | approval_request | notice
        payload     TEXT NOT NULL,          -- JSON {text, ...}
        urgency     TEXT NOT NULL DEFAULT 'normal',   -- urgent | normal | low
        status      TEXT NOT NULL DEFAULT 'pending',  -- pending | sending | sent | maybe_sent | failed | cancelled
        on_sent     TEXT,                   -- JSON list of memory commands applied after confirmation
        run_id      TEXT,
        attempts    INTEGER NOT NULL DEFAULT 0,
        last_error  TEXT,
        dedupe_key  TEXT,
        created_at  REAL NOT NULL,
        not_before  REAL NOT NULL,
        claimed_at  REAL,                   -- when the dispatcher started sending (crash detection)
        sent_at     REAL
    );
    CREATE UNIQUE INDEX outbox_dedupe ON outbox(dedupe_key) WHERE dedupe_key IS NOT NULL;
    CREATE INDEX outbox_due ON outbox(status, not_before);

    -- One row per agent run (a wake-up): what triggered it, how it ended, what it cost.
    CREATE TABLE runs (
        id               TEXT PRIMARY KEY,
        session_key      TEXT NOT NULL,
        trigger          TEXT NOT NULL,     -- comma separated event kinds
        profile          TEXT NOT NULL,     -- full | scheduled | untrusted | dream
        status           TEXT NOT NULL,     -- running | completed | silent | failed | paused_budget | awaiting_approval | cancelled
        model            TEXT,
        event_ids        TEXT,              -- JSON list
        started_at       REAL NOT NULL,
        finished_at      REAL,
        requests         INTEGER NOT NULL DEFAULT 0,
        input_tokens     INTEGER NOT NULL DEFAULT 0,
        cached_tokens    INTEGER NOT NULL DEFAULT 0,
        output_tokens    INTEGER NOT NULL DEFAULT 0,
        reasoning_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd         REAL NOT NULL DEFAULT 0,
        tool_calls       INTEGER NOT NULL DEFAULT 0,
        tainted          INTEGER NOT NULL DEFAULT 0,
        final_text       TEXT,
        error            TEXT
    );
    CREATE INDEX runs_session ON runs(session_key, started_at);
    CREATE INDEX runs_started ON runs(started_at);

    -- Every write to agent memory is versioned so it can be inspected and rolled back.
    CREATE TABLE memory_versions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        path        TEXT NOT NULL,          -- virtual path, e.g. /memories/state/notes.md
        action      TEXT NOT NULL,          -- create | update | delete | rename
        old_content TEXT,
        new_content TEXT,
        actor       TEXT NOT NULL,          -- agent | user | dream | system
        run_id      TEXT,
        created_at  REAL NOT NULL
    );
    CREATE INDEX memory_versions_path ON memory_versions(path, id);

    -- Small key/value state (last dream time, counters, ...).
    CREATE TABLE kv (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    """,
    # ---- 2: iteration 2 always-on loop ----------------------------------------------------
    """
    -- Timers: follow-ups the agent sets for itself, and recurring schedules.
    CREATE TABLE timers (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        kind          TEXT NOT NULL CHECK (kind IN ('followup', 'schedule')),
        cron          TEXT,                  -- 5-field cron expression (schedule only)
        tz            TEXT,                  -- IANA zone the cron expression is evaluated in
        due_at        REAL NOT NULL,         -- next firing, UTC epoch seconds
        prompt        TEXT NOT NULL,         -- what the agent is told when it fires
        session_key   TEXT NOT NULL,
        trust         TEXT NOT NULL DEFAULT 'owner' CHECK (trust IN ('owner', 'untrusted')),
        status        TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'paused', 'fired', 'cancelled')),
        misfire       TEXT NOT NULL DEFAULT 'fire_once' CHECK (misfire IN ('fire_once', 'skip')),
        created_by    TEXT NOT NULL DEFAULT 'owner',   -- owner | agent | system
        created_at    REAL NOT NULL,
        last_fired_at REAL,
        fire_count    INTEGER NOT NULL DEFAULT 0
    );
    CREATE UNIQUE INDEX timers_live_name ON timers(name) WHERE status IN ('active', 'paused');
    CREATE INDEX timers_due ON timers(due_at) WHERE status = 'active';

    -- Approvals: a tool call that must wait for the owner. The run is suspended (run_states)
    -- until every approval of that run is decided.
    CREATE TABLE approvals (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id      TEXT NOT NULL,
        session_key TEXT NOT NULL,
        call_id     TEXT NOT NULL,           -- the model's tool call id
        tool        TEXT NOT NULL,
        summary     TEXT NOT NULL,           -- what the owner is asked to approve (redacted)
        status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'denied', 'expired', 'superseded')),
        created_at  REAL NOT NULL,
        expires_at  REAL NOT NULL,
        decided_at  REAL,
        decided_by  TEXT,
        note        TEXT
    );
    CREATE UNIQUE INDEX approvals_call ON approvals(run_id, call_id);
    CREATE INDEX approvals_status ON approvals(status, expires_at);
    CREATE INDEX approvals_session ON approvals(session_key, status);

    -- A suspended run: the SDK's serialized RunState plus what we need to rebuild its context.
    -- Stays on this machine; it contains the conversation so far.
    CREATE TABLE run_states (
        run_id      TEXT PRIMARY KEY,
        session_key TEXT NOT NULL,
        state       TEXT NOT NULL,
        meta        TEXT NOT NULL,           -- JSON: profile, origin, trust, taint reasons
        created_at  REAL NOT NULL
    );

    -- Every tool call and what the policy decided about it.
    CREATE TABLE audit (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          REAL NOT NULL,
        run_id      TEXT,
        session_key TEXT,
        call_id     TEXT,                    -- the model's tool call id
        tool        TEXT NOT NULL,
        category    TEXT,
        verdict     TEXT NOT NULL,           -- allow | ask | deny
        layer       TEXT,                    -- which policy layer decided
        reason      TEXT,
        args        TEXT,                    -- redacted and truncated
        detail      TEXT,
        outcome     TEXT,                    -- ok | error | denied | rejected | timeout | pending
        duration_ms INTEGER,
        tainted     INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX audit_ts ON audit(ts);
    CREATE INDEX audit_run ON audit(run_id);
    CREATE UNIQUE INDEX audit_call ON audit(run_id, call_id) WHERE call_id IS NOT NULL;
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


class Database:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(
            self.path,
            isolation_level=None,  # autocommit; transactions are explicit
            check_same_thread=False,
            timeout=busy_timeout_ms / 1000,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")

    # ---- statements -------------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, rows)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def scalar(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = (), default: Any = None
    ) -> Any:
        row = self.one(sql, params)
        return default if row is None or row[0] is None else row[0]

    # ---- small key/value state ---------------------------------------------------------
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return default if row is None else row["value"]

    def kv_set(self, key: str, value: str, *, now: float = 0.0) -> None:
        with self.tx():
            self.execute(
                """INSERT INTO kv(key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (key, value, now),
            )

    # ---- transactions -----------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[Database]:
        """`BEGIN IMMEDIATE` transaction; nested use becomes a savepoint."""
        with self._lock:
            if self._depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
                self._depth = 1
                try:
                    yield self
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                else:
                    self._conn.execute("COMMIT")
                finally:
                    self._depth = 0
            else:
                name = f"sp{self._depth}"
                self._conn.execute(f"SAVEPOINT {name}")
                self._depth += 1
                try:
                    yield self
                except BaseException:
                    self._conn.execute(f"ROLLBACK TO {name}")
                    self._conn.execute(f"RELEASE {name}")
                    raise
                else:
                    self._conn.execute(f"RELEASE {name}")
                finally:
                    self._depth -= 1

    # ---- schema -----------------------------------------------------------------------
    @property
    def version(self) -> int:
        return int(self.scalar("PRAGMA user_version", default=0))

    def migrate(self) -> int:
        """Apply pending migrations; returns the resulting schema version."""
        current = self.version
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema v{current} is newer than this program (v{SCHEMA_VERSION}); "
                "upgrade personal-agent"
            )
        for number in range(current + 1, SCHEMA_VERSION + 1):
            script = MIGRATIONS[number - 1]
            with self._lock:
                # executescript() runs in autocommit mode here; wrap it in one transaction so a
                # failed migration leaves the database untouched.
                try:
                    self._conn.executescript(
                        f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;"
                    )
                except BaseException:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise
        return self.version

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def open_database(path: str | Path) -> Database:
    db = Database(path)
    db.migrate()
    return db
