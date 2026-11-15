"""Outbox: every message the agent wants to deliver is queued here first.

"Sent" only ever means *delivery was confirmed by the channel*. A message that was handed to
a channel but whose outcome is unknown (crash, timeout) is `maybe_sent` and is surfaced to the
owner and to the next run instead of being silently resent or silently forgotten, the same
"posted / maybe posted / held" distinction Anthropic's daily-brief run record makes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from personal_agent.clock import Clock, SystemClock
from personal_agent.db import Database, dumps, loads

URGENCIES = ("urgent", "normal", "low")


@dataclass(frozen=True)
class OutMessage:
    id: int
    session_key: str
    channel: str
    recipient: str | None
    kind: str
    payload: dict[str, Any]
    urgency: str
    status: str
    on_sent: list[dict[str, Any]]
    run_id: str | None
    attempts: int
    created_at: float
    not_before: float
    last_error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        value = self.payload.get("text")
        return value if isinstance(value, str) else ""


def _message(row: sqlite3.Row) -> OutMessage:
    return OutMessage(
        id=row["id"],
        session_key=row["session_key"],
        channel=row["channel"],
        recipient=row["recipient"],
        kind=row["kind"],
        payload=loads(row["payload"], {}),
        urgency=row["urgency"],
        status=row["status"],
        on_sent=loads(row["on_sent"], []),
        run_id=row["run_id"],
        attempts=row["attempts"],
        created_at=row["created_at"],
        not_before=row["not_before"],
        last_error=row["last_error"],
    )


class Outbox:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock = clock or SystemClock()

    def enqueue(
        self,
        *,
        session_key: str,
        channel: str,
        text: str,
        kind: str = "message",
        urgency: str = "normal",
        recipient: str | None = None,
        run_id: str | None = None,
        not_before: float | None = None,
        dedupe_key: str | None = None,
        on_sent: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> int | None:
        """Queue a message. Returns its id, or None if `dedupe_key` was already queued."""
        if urgency not in URGENCIES:
            raise ValueError(f"urgency must be one of {URGENCIES}")
        now = self.clock.now()
        payload = {"text": text, **(extra or {})}
        with self.db.tx():
            cur = self.db.execute(
                """INSERT OR IGNORE INTO outbox
                   (session_key, channel, recipient, kind, payload, urgency, on_sent, run_id,
                    dedupe_key, created_at, not_before)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_key,
                    channel,
                    recipient,
                    kind,
                    dumps(payload),
                    urgency,
                    dumps(on_sent) if on_sent else None,
                    run_id,
                    dedupe_key,
                    now,
                    now if not_before is None else not_before,
                ),
            )
            return int(cur.lastrowid) if cur.rowcount == 1 else None

    def has_due(self, channels: Collection[str] | None = None) -> bool:
        """Cheap read-only check that `claim_due` would return something."""
        params: list[Any] = [self.clock.now()]
        sql = "SELECT 1 FROM outbox WHERE status = 'pending' AND not_before <= ?"
        if channels is not None:
            if not channels:
                return False
            sql += f" AND channel IN ({','.join('?' * len(channels))})"
            params.extend(channels)
        return self.db.one(sql + " LIMIT 1", params) is not None

    # ---- delivery state machine -----------------------------------------------------------
    def claim_due(
        self, *, channels: Collection[str] | None = None, limit: int = 20
    ) -> list[OutMessage]:
        """Move due messages from `pending` to `sending` and return them (oldest first)."""
        now = self.clock.now()
        with self.db.tx():
            params: list[Any] = [now]
            channel_sql = ""
            if channels is not None:
                if not channels:
                    return []
                channel_sql = f" AND channel IN ({','.join('?' * len(channels))})"
                params.extend(channels)
            params.append(limit)
            rows = self.db.query(
                f"""SELECT * FROM outbox WHERE status = 'pending' AND not_before <= ?{channel_sql}
                    ORDER BY CASE urgency WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, id
                    LIMIT ?""",
                params,
            )
            for row in rows:
                self.db.execute(
                    "UPDATE outbox SET status = 'sending', claimed_at = ?, attempts = attempts + 1 WHERE id = ?",
                    (now, row["id"]),
                )
            fresh = (
                {
                    r["id"]: r
                    for r in self.db.query(
                        f"SELECT * FROM outbox WHERE id IN ({','.join('?' * len(rows))})",
                        [r["id"] for r in rows],
                    )
                }
                if rows
                else {}
            )
        # Keep the order they were selected in: urgent first, then oldest first.
        return [_message(fresh[r["id"]]) for r in rows]

    def mark_sent(self, message_id: int) -> None:
        with self.db.tx():
            self.db.execute(
                "UPDATE outbox SET status = 'sent', sent_at = ?, last_error = NULL WHERE id = ? AND status = 'sending'",
                (self.clock.now(), message_id),
            )

    def mark_failed(
        self, message_id: int, error: str, *, retry_in: float | None, max_attempts: int = 5
    ) -> str:
        """Record a delivery failure. Returns the new status (`pending` again, or `failed`)."""
        with self.db.tx():
            row = self.db.one("SELECT attempts FROM outbox WHERE id = ?", (message_id,))
            if row is None:
                return "missing"
            if retry_in is None or row["attempts"] >= max_attempts:
                status, not_before = "failed", None
            else:
                status, not_before = "pending", self.clock.now() + retry_in
            self.db.execute(
                """UPDATE outbox SET status = ?, last_error = ?, claimed_at = NULL,
                   not_before = COALESCE(?, not_before) WHERE id = ? AND status = 'sending'""",
                (status, error[:1000], not_before, message_id),
            )
            return status

    def mark_maybe_sent(self, message_id: int, note: str = "outcome unknown") -> None:
        with self.db.tx():
            self.db.execute(
                "UPDATE outbox SET status = 'maybe_sent', last_error = ? WHERE id = ? AND status = 'sending'",
                (note[:1000], message_id),
            )

    def hold(self, message_id: int, until: float) -> None:
        """Put a claimed message back until `until` (quiet hours, rate limit) at no cost."""
        with self.db.tx():
            self.db.execute(
                """UPDATE outbox SET status = 'pending', not_before = ?, claimed_at = NULL,
                   attempts = MAX(attempts - 1, 0) WHERE id = ? AND status = 'sending'""",
                (until, message_id),
            )

    def cancel(self, message_id: int) -> bool:
        with self.db.tx():
            cur = self.db.execute(
                "UPDATE outbox SET status = 'cancelled' WHERE id = ? AND status IN ('pending','maybe_sent','failed')",
                (message_id,),
            )
        return cur.rowcount == 1

    def recover_stuck(self, *, older_than_s: float = 120.0) -> dict[str, int]:
        """After a crash: messages stuck in `sending` have an unknown outcome.

        Urgent ones are retried (a duplicate beats a lost alert); the rest become
        `maybe_sent` so the owner can decide.
        """
        cutoff = self.clock.now() - older_than_s
        with self.db.tx():
            urgent = self.db.execute(
                """UPDATE outbox SET status = 'pending', claimed_at = NULL
                   WHERE status = 'sending' AND claimed_at <= ? AND urgency = 'urgent'""",
                (cutoff,),
            ).rowcount
            other = self.db.execute(
                """UPDATE outbox SET status = 'maybe_sent', last_error = 'process stopped while sending'
                   WHERE status = 'sending' AND claimed_at <= ?""",
                (cutoff,),
            ).rowcount
        return {"retried": urgent, "maybe_sent": other}

    def background_sends_since(self, channel: str, since: float) -> list[float]:
        """When non-urgent, non-interactive messages went out on `channel` since `since` (oldest
        first). These are the ones quiet hours and the hourly limit apply to."""
        rows = self.db.query(
            """SELECT sent_at FROM outbox
               WHERE channel = ? AND status = 'sent' AND sent_at >= ? AND urgency != 'urgent'
                 AND COALESCE(json_extract(payload, '$.interactive'), 0) = 0
               ORDER BY sent_at""",
            (channel, since),
        )
        return [r["sent_at"] for r in rows]

    # ---- introspection -----------------------------------------------------------------
    def get(self, message_id: int) -> OutMessage | None:
        row = self.db.one("SELECT * FROM outbox WHERE id = ?", (message_id,))
        return _message(row) if row else None

    def list(self, *, status: str | None = None, limit: int = 50) -> list[OutMessage]:
        if status:
            rows = self.db.query(
                "SELECT * FROM outbox WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
            )
        else:
            rows = self.db.query("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit,))
        return [_message(r) for r in rows]

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in ("pending", "sending", "sent", "maybe_sent", "failed", "cancelled")}
        for r in self.db.query("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status"):
            out[r["status"]] = r["n"]
        return out

    def unresolved(self, session_key: str | None = None) -> list[OutMessage]:
        """Messages whose delivery is unknown or failed (shown to the next run)."""
        sql = "SELECT * FROM outbox WHERE status IN ('maybe_sent','failed')"
        params: list[Any] = []
        if session_key:
            sql += " AND session_key = ?"
            params.append(session_key)
        return [_message(r) for r in self.db.query(sql + " ORDER BY id", params)]
