"""One row per task that the owner can follow like a conversation: where it was handed out from,
whether it is put away, and where its note is. Everything else about a task is derived."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE threads (
            task_id TEXT PRIMARY KEY, session_key TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL DEFAULT '', recipient TEXT,
            archived_at REAL, snoozed_until REAL, note_path TEXT, created_at REAL NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX threads_archived ON threads(archived_at)")
