"""Append-only migration for the control-plane node directory."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            label TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            space_ids TEXT NOT NULL,
            capabilities TEXT NOT NULL,
            declared_capabilities TEXT NOT NULL,
            credential_version INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            last_seen_at REAL,
            revoked_at REAL
        )""",
        """CREATE TABLE node_pairings (
            token_hash TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            label TEXT NOT NULL,
            space_ids TEXT NOT NULL,
            capabilities TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            used_at REAL
        )""",
        "CREATE INDEX nodes_owner ON nodes(owner_id, revoked_at)",
    )
    for sql in statements:
        conn.execute(sql)
