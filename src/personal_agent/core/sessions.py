"""Append-only session log and the adapter that plugs it into the Agents SDK.

The log is the source of truth and is never rewritten: removing, clearing or compacting
history only appends a marker. What the model sees is a *projection* of the log, so a better
compaction strategy can always be applied to the original transcript later, and dreaming and
audits can read what really happened.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from personal_agent.clock import Clock, SystemClock
from personal_agent.db import Database, dumps, loads

INTERRUPTED_RESULT = (
    "Error: this tool call was interrupted before its result was recorded. It may or may not "
    "have taken effect. Check the current state before repeating it."
)


def _plain(item: Any) -> dict[str, Any]:
    """Make an SDK input item JSON-safe (they are dicts; pydantic objects are dumped)."""
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_none=True)
    raise TypeError(f"unsupported session item type: {type(item).__name__}")


@dataclass(frozen=True)
class SessionInfo:
    session_key: str
    items: int
    last_activity: float


class SessionStore:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock = clock or SystemClock()

    # ---- writing ----------------------------------------------------------------------
    def append(
        self,
        key: str,
        items: list[Any],
        *,
        run_id: str | None = None,
        event_id: int | None = None,
    ) -> list[int]:
        """Append items; returns their row ids. With `event_id`, a repeat is ignored."""
        now = self.clock.now()
        ids: list[int] = []
        with self.db.tx():
            for item in items:
                cur = self.db.execute(
                    """INSERT OR IGNORE INTO session_events
                       (session_key, kind, item, event_id, run_id, created_at)
                       VALUES (?, 'item', ?, ?, ?, ?)""",
                    (key, dumps(_plain(item)), event_id, run_id, now),
                )
                if cur.rowcount == 1:
                    ids.append(int(cur.lastrowid))
        return ids

    def has_event(self, key: str, event_id: int) -> bool:
        return (
            self.db.one(
                "SELECT 1 FROM session_events WHERE session_key = ? AND event_id = ? AND kind = 'item'",
                (key, event_id),
            )
            is not None
        )

    def pop(self, key: str) -> dict[str, Any] | None:
        """Hide the newest visible item (a tombstone is appended; nothing is deleted)."""
        rows = self._visible_rows(key)
        if not rows:
            return None
        last = rows[-1]
        with self.db.tx():
            self.db.execute(
                "INSERT INTO session_events(session_key, kind, item, created_at) VALUES (?, 'tombstone', ?, ?)",
                (key, dumps({"target_id": last["id"]}), self.clock.now()),
            )
        return loads(last["item"])

    def clear(self, key: str) -> None:
        with self.db.tx():
            self.db.execute(
                "INSERT INTO session_events(session_key, kind, item, created_at) VALUES (?, 'clear', '{}', ?)",
                (key, self.clock.now()),
            )

    def add_compaction(self, key: str, summary: str, covers_up_to_id: int) -> None:
        """Record that everything up to `covers_up_to_id` is represented by `summary`."""
        with self.db.tx():
            self.db.execute(
                "INSERT INTO session_events(session_key, kind, item, created_at) VALUES (?, 'compaction', ?, ?)",
                (
                    key,
                    dumps({"summary": summary, "covers_up_to_id": covers_up_to_id}),
                    self.clock.now(),
                ),
            )

    # ---- reading ----------------------------------------------------------------------
    def _bounds(self, key: str) -> tuple[int, str | None]:
        """(ids at or below this are hidden, summary of the hidden part if compacted)."""
        clear_id = int(
            self.db.scalar(
                "SELECT MAX(id) FROM session_events WHERE session_key = ? AND kind = 'clear'",
                (key,),
                default=0,
            )
        )
        comp = self.db.one(
            """SELECT item FROM session_events
               WHERE session_key = ? AND kind = 'compaction' AND id > ? ORDER BY id DESC LIMIT 1""",
            (key, clear_id),
        )
        if comp is None:
            return clear_id, None
        data = loads(comp["item"], {})
        return max(clear_id, int(data.get("covers_up_to_id", 0))), data.get("summary")

    def _visible_rows(self, key: str) -> list[sqlite3.Row]:
        start, _ = self._bounds(key)
        rows = self.db.query(
            """SELECT id, kind, item FROM session_events
               WHERE session_key = ? AND id > ? AND kind IN ('item','tombstone') ORDER BY id""",
            (key, start),
        )
        hidden = {loads(r["item"], {}).get("target_id") for r in rows if r["kind"] == "tombstone"}
        return [r for r in rows if r["kind"] == "item" and r["id"] not in hidden]

    def items(self, key: str, limit: int | None = None) -> list[dict[str, Any]]:
        """The model-visible history: [compaction summary] + visible items.

        With `limit`, only the newest items are returned, but never starting in the middle
        of a tool exchange: the slice is moved forward to the next user message.
        """
        _, summary = self._bounds(key)
        items = [loads(r["item"], {}) for r in self._visible_rows(key)]
        if limit is not None and limit > 0 and len(items) > limit:
            tail = items[-limit:]
            start = next((i for i, it in enumerate(tail) if it.get("role") == "user"), None)
            items = tail[start:] if start is not None else tail
        if summary:
            items.insert(
                0,
                {
                    "role": "user",
                    "content": "[Summary of the earlier conversation, written by you]\n" + summary,
                },
            )
        return items

    def last_id(self, key: str) -> int:
        return int(
            self.db.scalar(
                "SELECT MAX(id) FROM session_events WHERE session_key = ?", (key,), default=0
            )
        )

    def transcript(self, key: str, *, after_id: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        """Raw log rows (including hidden ones) for dreaming, search and audits."""
        rows = self.db.query(
            """SELECT id, kind, item, event_id, run_id, created_at FROM session_events
               WHERE session_key = ? AND id > ? ORDER BY id LIMIT ?""",
            (key, after_id, limit),
        )
        return [
            {
                "id": r["id"],
                "kind": r["kind"],
                "item": loads(r["item"], {}),
                "event_id": r["event_id"],
                "run_id": r["run_id"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def list_sessions(self) -> list[SessionInfo]:
        rows = self.db.query(
            """SELECT session_key, COUNT(*) AS n, MAX(created_at) AS last
               FROM session_events WHERE kind = 'item' GROUP BY session_key ORDER BY last DESC"""
        )
        return [SessionInfo(r["session_key"], r["n"], r["last"]) for r in rows]

    # ---- crash recovery ---------------------------------------------------------------
    def repair_dangling_calls(self, key: str, *, run_id: str | None = None) -> int:
        """Give every tool call without a recorded result an explicit "interrupted" result.

        The SDK silently drops such calls from the model input, so the model would never learn
        that an action may already have happened. Returns the number of calls repaired.
        """
        items = self.items(key)
        answered = {i.get("call_id") for i in items if i.get("type") == "function_call_output"}
        missing = [
            i["call_id"]
            for i in items
            if i.get("type") == "function_call" and i.get("call_id") not in answered
        ]
        if missing:
            self.append(
                key,
                [
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": INTERRUPTED_RESULT,
                    }
                    for call_id in missing
                ],
                run_id=run_id,
            )
        return len(missing)


class SdkSession:
    """Implements the Agents SDK `Session` protocol on top of `SessionStore`."""

    session_settings = None

    def __init__(
        self, store: SessionStore, key: str, run_id: str | None = None, *, window: int | None = None
    ) -> None:
        self.store = store
        self.session_id = key
        self.run_id = run_id
        self.window = window
        """Newest items to show the model when the caller does not ask for a specific limit."""

    async def get_items(self, limit: int | None = None) -> list[Any]:
        return self.store.items(self.session_id, limit if limit is not None else self.window)

    async def add_items(self, items: list[Any]) -> None:
        self.store.append(self.session_id, items, run_id=self.run_id)

    async def pop_item(self) -> Any | None:
        return self.store.pop(self.session_id)

    async def clear_session(self) -> None:
        self.store.clear(self.session_id)
