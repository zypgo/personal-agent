"""Small immutable agent result files, committed with their result receipt."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE orchestration_artifacts (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
            name TEXT NOT NULL, mime TEXT NOT NULL, sha256 TEXT NOT NULL,
            size INTEGER NOT NULL, content BLOB NOT NULL, created_at REAL NOT NULL,
            UNIQUE(attempt_id,name)
        )"""
    )
    conn.execute("CREATE INDEX orchestration_artifacts_task ON orchestration_artifacts(task_id)")
