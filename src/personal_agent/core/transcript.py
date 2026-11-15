"""The conversation as the owner lived it: what they said and what actually reached them.

Different from the session log on purpose. The session log is what the *model* is shown, and
`/new`, compaction and tombstones rewrite the view of it. This table is the owner's history across
every channel (terminal, phone, chat app); it only ever grows, so the app can always show what
happened, and it records an agent message only once a channel has confirmed delivering it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

from personal_agent.clock import Clock, SystemClock
from personal_agent.core.live import LiveBus
from personal_agent.core.outbox import OutMessage
from personal_agent.db import Database, dumps, loads

IN, OUT = "in", "out"


@dataclass(frozen=True)
class Turn:
    id: int
    at: float
    direction: str
    channel: str
    kind: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "direction": self.direction,
            "channel": self.channel,
            "kind": self.kind,
            "text": self.text,
            **({"meta": self.meta} if self.meta else {}),
        }


def _turn(row: sqlite3.Row) -> Turn:
    return Turn(
        id=row["id"],
        at=row["at"],
        direction=row["direction"],
        channel=row["channel"],
        kind=row["kind"],
        text=row["text"],
        meta=loads(row["meta"], {}),
    )


class Transcript:
    def __init__(
        self, db: Database, clock: Clock | None = None, live: LiveBus | None = None
    ) -> None:
        self.db = db
        self.clock = clock or SystemClock()
        self.live = live

    # ---- writing ----------------------------------------------------------------------
    def add_in(
        self,
        channel: str,
        text: str,
        *,
        attachments: Sequence[dict[str, Any]] = (),
        meta: dict[str, Any] | None = None,
    ) -> Turn:
        """Something the owner said, on any channel."""
        details = dict(meta or {})
        if attachments:
            details["attachments"] = list(attachments)
        turn = self._insert(IN, channel, "message", text, None, details)
        assert turn is not None
        return turn

    def add_out(self, message: OutMessage) -> Turn | None:
        """A message a channel confirmed delivering. Recording the same message twice is a no-op."""
        details: dict[str, Any] = {"urgency": message.urgency}
        approvals = message.payload.get("approval_ids")
        if approvals:
            details["approval_ids"] = list(approvals)
        return self._insert(OUT, message.channel, message.kind, message.text, message.id, details)

    def _insert(
        self,
        direction: str,
        channel: str,
        kind: str,
        text: str,
        outbox_id: int | None,
        meta: dict[str, Any],
    ) -> Turn | None:
        with self.db.tx():
            cur = self.db.execute(
                """INSERT OR IGNORE INTO transcript(at, direction, channel, kind, text, outbox_id, meta)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (self.clock.now(), direction, channel, kind, text, outbox_id, dumps(meta)),
            )
            if cur.rowcount != 1:
                return None
            row = self.db.one("SELECT * FROM transcript WHERE id = ?", (cur.lastrowid,))
        assert row is not None
        turn = _turn(row)
        if self.live is not None:
            self.live.publish({"type": "turn", "id": turn.id})
        return turn

    # ---- reading ----------------------------------------------------------------------
    def after(self, after_id: int, limit: int = 200) -> list[Turn]:
        rows = self.db.query(
            "SELECT * FROM transcript WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)
        )
        return [_turn(r) for r in rows]

    def latest(self, limit: int = 50, *, before: int | None = None) -> list[Turn]:
        """The newest `limit` turns (oldest first), optionally only those before turn `before`."""
        if before is None:
            rows = self.db.query("SELECT * FROM transcript ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self.db.query(
                "SELECT * FROM transcript WHERE id < ? ORDER BY id DESC LIMIT ?", (before, limit)
            )
        return [_turn(r) for r in reversed(rows)]

    def last_id(self) -> int:
        return int(self.db.scalar("SELECT MAX(id) FROM transcript", default=0))

    def last_in_channel(self, *, exclude: Collection[str] = ()) -> str | None:
        """The channel the owner last spoke on, ignoring `exclude` (where a "tell me when..."
        should be answered)."""
        skip = tuple(exclude)
        marks = ",".join("?" * len(skip))
        row = self.db.one(
            "SELECT channel FROM transcript WHERE direction = 'in'"
            + (f" AND channel NOT IN ({marks})" if skip else "")
            + " ORDER BY id DESC LIMIT 1",
            skip,
        )
        return row["channel"] if row else None
