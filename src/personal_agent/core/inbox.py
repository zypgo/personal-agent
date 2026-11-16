"""Durable event inbox.

Guarantees:
- **At-least-once, deduplicated**: `(source, external_id)` is unique, so a webhook retry or a
  timer firing twice is stored once.
- **One run per session at a time**: a session with an unexpired lease is never claimed again,
  and a session's events are claimed in order (an earlier event waiting out a retry backoff
  holds back the later ones).
- **One trust level per batch**: events of one session are batched only while they come from the
  same kind of source, so text from outside never rides along in the run of the owner's message
  (and the owner's message is not held to the untrusted profile because of it).
- **Crash recovery**: a worker that dies leaves an expiring lease; the events become claimable
  again (at once, if the process that restarts holds the engine lock: see `release_leases`).
  Every claim counts as an attempt, so an event that crashes workers repeatedly becomes `dead`
  instead of looping forever (poison pill). Environmental failures (network, rate limit) do not
  count against the event.
- **A live run keeps its lease**: leases are timed by the wall clock, which jumps when a laptop
  sleeps. A lease whose holder is a worker of this process that is still running (`holding`) is
  never taken over, so the same message cannot run twice at once.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from typing import Any

from personal_agent.clock import Clock, SystemClock
from personal_agent.core.events import AddResult, Batch, Event, NewEvent, Trust
from personal_agent.db import Database, dumps, loads

# seconds to wait before the Nth retry (attempt number starts at 1)
BACKOFF_S = (2.0, 10.0, 60.0, 300.0)


def backoff_for(attempts: int) -> float:
    return BACKOFF_S[min(max(attempts, 1) - 1, len(BACKOFF_S) - 1)]


def _event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        source=row["source"],
        kind=row["kind"],
        session_key=row["session_key"],
        payload=loads(row["payload"], {}),
        external_id=row["external_id"],
        trust=Trust(row["trust"]),
        attempts=row["attempts"],
        created_at=row["created_at"],
    )


class Inbox:
    def __init__(
        self,
        db: Database,
        clock: Clock | None = None,
        *,
        lease_s: float = 300.0,
        max_attempts: int = 4,
        max_age_s: float = 24 * 3600.0,
    ) -> None:
        self.db = db
        self.clock = clock or SystemClock()
        self.lease_s = lease_s
        self.max_attempts = max_attempts
        self.max_age_s = max_age_s
        self._dead: list[Event] = []
        self._held: set[str] = set()

    @contextlib.contextmanager
    def holding(self, batch: Batch) -> Iterator[None]:
        """Keep every other worker off `batch` for as long as its run is alive in this process.

        A lease that ran out normally means the worker died. But the clock it runs on also jumps
        when a laptop sleeps, and a run that is only waiting for a slow tool would look dead and
        be started a second time (a charge made twice). Leases of a process that is gone are held
        by nobody here, so they still expire.
        """
        self._held.add(batch.owner)
        try:
            yield
        finally:
            self._held.discard(batch.owner)

    def _held_json(self) -> str:
        return dumps(sorted(self._held))

    # ---- producers --------------------------------------------------------------------
    def add(self, event: NewEvent) -> AddResult:
        now = self.clock.now()
        with self.db.tx():
            new_id = self.db.insert(
                """INSERT OR IGNORE INTO events
                   (source, kind, session_key, payload, external_id, trust, created_at, available_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.source,
                    event.kind,
                    event.session_key,
                    dumps(event.payload),
                    event.external_id,
                    event.trust.value,
                    now,
                    now + max(0.0, event.delay_s),
                ),
            )
            if new_id is not None:
                return AddResult(id=new_id, duplicate=False)
            row = self.db.row(
                "SELECT id FROM events WHERE source = ? AND external_id = ?",
                (event.source, event.external_id),
            )
            return AddResult(id=int(row["id"]), duplicate=True)

    # ---- consumers --------------------------------------------------------------------
    def _claimable(self, row: sqlite3.Row, now: float) -> bool:
        if row["status"] == "pending":
            return bool(row["available_at"] <= now)
        if row["status"] == "leased":
            return (
                bool((row["lease_expires_at"] or 0) <= now) and row["lease_owner"] not in self._held
            )
        return False

    def has_ready(self, session_key: str | None = None) -> bool:
        """Cheap read-only check that something could be claimed (idle loops use it so they
        do not take the database write lock every second)."""
        now = self.clock.now()
        row = self.db.one(
            """SELECT 1 FROM events e
               WHERE ((e.status = 'pending' AND e.available_at <= :now)
                      OR (e.status = 'leased' AND e.lease_expires_at <= :now
                          AND COALESCE(e.lease_owner, '') NOT IN (SELECT value FROM json_each(:held))))
                 AND (:session IS NULL OR e.session_key = :session) LIMIT 1""",
            {"now": now, "session": session_key, "held": self._held_json()},
        )
        return row is not None

    def active_for(self, session_key: str) -> int:
        """Events of a session that are running or ready to run (not ones waiting to retry)."""
        return int(
            self.db.scalar(
                """SELECT COUNT(*) FROM events
                   WHERE session_key = :session
                     AND (status = 'leased' OR (status = 'pending' AND available_at <= :now))""",
                {"session": session_key, "now": self.clock.now()},
                default=0,
            )
        )

    def claim(
        self,
        owner: str,
        *,
        session_key: str | None = None,
        max_batch: int = 20,
        lease_s: float | None = None,
    ) -> Batch | None:
        """Claim the next ready batch, or None: the ready events of one session, in order, up to the
        first one that comes from a different kind of source (see "One trust level per batch")."""
        now = self.clock.now()
        lease = self.lease_s if lease_s is None else lease_s
        with self.db.tx():
            self._reap(now)
            first = self.db.one(
                """
                SELECT e.session_key FROM events e
                WHERE ((e.status = 'pending' AND e.available_at <= :now)
                       OR (e.status = 'leased' AND e.lease_expires_at <= :now
                           AND COALESCE(e.lease_owner, '') NOT IN (SELECT value FROM json_each(:held))))
                  AND (:session IS NULL OR e.session_key = :session)
                  AND NOT EXISTS (
                      SELECT 1 FROM events o
                      WHERE o.session_key = e.session_key
                        AND ((o.status = 'leased'
                              AND (o.lease_expires_at > :now
                                   OR COALESCE(o.lease_owner, '') IN (SELECT value FROM json_each(:held))))
                             OR (o.status = 'pending' AND o.available_at > :now AND o.id < e.id)))
                ORDER BY e.id LIMIT 1
                """,
                {"now": now, "session": session_key, "held": self._held_json()},
            )
            if first is None:
                return None
            key = first["session_key"]
            rows = self.db.query(
                "SELECT * FROM events WHERE session_key = ? AND status IN ('pending','leased') ORDER BY id",
                (key,),
            )
            taken: list[sqlite3.Row] = []
            for row in rows:
                if not self._claimable(row, now):
                    break  # keep per-session order: do not skip over a waiting event
                if taken and row["trust"] != taken[0]["trust"]:
                    break  # the next batch: outside text does not ride along with the owner's
                taken.append(row)
                if len(taken) >= max_batch:
                    break
            expires = now + lease
            for row in taken:
                self.db.execute(
                    """UPDATE events SET status = 'leased', lease_owner = ?, lease_expires_at = ?,
                       attempts = attempts + 1 WHERE id = ?""",
                    (owner, expires, row["id"]),
                )
            fresh = self.db.query(
                f"SELECT * FROM events WHERE id IN ({','.join('?' * len(taken))}) ORDER BY id",
                [r["id"] for r in taken],
            )
            return Batch(
                session_key=key,
                owner=owner,
                events=[_event(r) for r in fresh],
                lease_expires_at=expires,
            )

    def _reap(self, now: float) -> None:
        """Mark exhausted or expired claimable events dead (called inside `claim`)."""
        stale = self.db.query(
            """
            SELECT * FROM events
            WHERE ((status = 'pending' AND available_at <= :now)
                   OR (status = 'leased' AND lease_expires_at <= :now))
              AND (attempts >= :max_attempts OR created_at < :oldest)
            """,
            {"now": now, "max_attempts": self.max_attempts, "oldest": now - self.max_age_s},
        )
        for row in stale:
            if row["status"] == "leased" and row["lease_owner"] in self._held:
                continue  # its run is alive here: a long sleep did not kill it
            reason = (
                "exceeded max attempts"
                if row["attempts"] >= self.max_attempts
                else "expired before it could be processed"
            )
            self._mark_dead(row, reason, now)

    def _mark_dead(self, row: sqlite3.Row, reason: str, now: float) -> None:
        detail = f"{row['last_error']}; {reason}" if row["last_error"] else reason
        self.db.execute(
            """UPDATE events SET status = 'dead', last_error = ?, lease_owner = NULL,
               lease_expires_at = NULL, done_at = ? WHERE id = ?""",
            (detail, now, row["id"]),
        )
        self._dead.append(_event(row))

    def take_dead(self) -> list[Event]:
        """Events that became dead since the last call (the worker tells the owner)."""
        dead, self._dead = self._dead, []
        return dead

    def heartbeat(self, batch: Batch, lease_s: float | None = None) -> bool:
        """Extend the lease of a running batch. False means the lease was lost."""
        expires = self.clock.now() + (self.lease_s if lease_s is None else lease_s)
        with self.db.tx():
            cur = self.db.execute(
                f"""UPDATE events SET lease_expires_at = ?
                    WHERE id IN ({",".join("?" * len(batch.ids))})
                      AND status = 'leased' AND lease_owner = ?""",
                [expires, *batch.ids, batch.owner],
            )
        batch.lease_expires_at = expires
        return cur.rowcount == len(batch.ids)

    def complete(self, batch: Batch) -> None:
        now = self.clock.now()
        with self.db.tx():
            self.db.execute(
                f"""UPDATE events SET status = 'done', done_at = ?, lease_owner = NULL,
                    lease_expires_at = NULL
                    WHERE id IN ({",".join("?" * len(batch.ids))})
                      AND status = 'leased' AND lease_owner = ?""",
                [now, *batch.ids, batch.owner],
            )

    def fail(
        self,
        batch: Batch,
        error: str,
        *,
        environmental: bool = False,
        retry_in: float | None = None,
    ) -> None:
        """Return a batch to the queue after a failure (or kill it if it keeps failing).

        `environmental` failures (network down, rate limited, missing key) are not the
        event's fault: they do not use up an attempt.
        """
        now = self.clock.now()
        with self.db.tx():
            for event_id in batch.ids:
                row = self.db.one(
                    "SELECT * FROM events WHERE id = ? AND status = 'leased' AND lease_owner = ?",
                    (event_id, batch.owner),
                )
                if row is None:
                    continue  # lease was lost; someone else owns it now
                attempts = row["attempts"] - (1 if environmental else 0)
                if attempts >= self.max_attempts:
                    self._mark_dead(row, f"{error}; exceeded max attempts", now)
                    continue
                delay = backoff_for(attempts) if retry_in is None else retry_in
                self.db.execute(
                    """UPDATE events SET status = 'pending', lease_owner = NULL,
                       lease_expires_at = NULL, available_at = ?, attempts = ?, last_error = ?
                       WHERE id = ?""",
                    (now + delay, attempts, error[:2000], event_id),
                )

    def discard(self, batch: Batch, error: str) -> None:
        """Drop a batch for good without notifying anyone (an interactive caller shows the error)."""
        now = self.clock.now()
        with self.db.tx():
            self.db.execute(
                f"""UPDATE events SET status = 'dead', last_error = ?, lease_owner = NULL,
                    lease_expires_at = NULL, done_at = ?
                    WHERE id IN ({",".join("?" * len(batch.ids))})
                      AND status = 'leased' AND lease_owner = ?""",
                [error[:2000], now, *batch.ids, batch.owner],
            )

    def drop_pending(self, session_key: str, reason: str) -> int:
        """Kill every not-yet-finished event of a session (the owner pressed Ctrl-C)."""
        with self.db.tx():
            cur = self.db.execute(
                """UPDATE events SET status = 'dead', last_error = ?, lease_owner = NULL,
                   lease_expires_at = NULL, done_at = ?
                   WHERE session_key = ? AND status IN ('pending', 'leased')""",
                (reason[:2000], self.clock.now(), session_key),
            )
        return cur.rowcount

    def expedite(self, error_like: str) -> int:
        """Make events that were put off because of a problem that is now fixed ready at once.

        `error_like` is an SQL LIKE pattern over the stored failure text (for example
        `%budget reached%` after the owner raised today's limit)."""
        now = self.clock.now()
        with self.db.tx():
            cur = self.db.execute(
                """UPDATE events SET available_at = ?
                   WHERE status = 'pending' AND available_at > ? AND last_error LIKE ?""",
                (now, now, error_like),
            )
        return cur.rowcount

    def release_leases(self) -> int:
        """Make every leased event claimable at once. Returns how many there were.

        For the process that has just taken the engine lock: no other worker exists, so a lease it
        finds belongs to one that died, and waiting for it to run out (minutes) would only keep
        the owner's message waiting. The attempt that was started stays used: a claim that never
        finished still counts toward the poison-pill limit.
        """
        with self.db.tx():
            cur = self.db.execute(
                """UPDATE events SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL
                   WHERE status = 'leased'"""
            )
        return cur.rowcount

    def release(self, batch: Batch) -> None:
        """Give a batch back untouched (graceful shutdown): no attempt is consumed."""
        with self.db.tx():
            self.db.execute(
                f"""UPDATE events SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL,
                    attempts = MAX(attempts - 1, 0)
                    WHERE id IN ({",".join("?" * len(batch.ids))})
                      AND status = 'leased' AND lease_owner = ?""",
                [*batch.ids, batch.owner],
            )

    # ---- introspection -----------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM events GROUP BY status")
        out = {"pending": 0, "leased": 0, "done": 0, "dead": 0}
        out.update({r["status"]: r["n"] for r in rows})
        return out

    def get(self, event_id: int) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM events WHERE id = ?", (event_id,))
        return dict(row) if row else None
