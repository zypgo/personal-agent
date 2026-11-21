"""What the product remembers about changes it made to notes: enough to undo them."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE vault_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, action TEXT NOT NULL,
            actor TEXT NOT NULL, run_id TEXT, old_sha TEXT, new_sha TEXT,
            old_text TEXT, new_size INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX vault_versions_path ON vault_versions(path, id)")
    conn.execute("CREATE INDEX vault_versions_run ON vault_versions(run_id)")
    conn.execute("CREATE INDEX vault_versions_time ON vault_versions(created_at)")
