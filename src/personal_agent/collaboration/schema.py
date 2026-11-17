"""Append-only V3 directory migration; each space's existing data stays in its own database."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    """Called by Database.migrate inside its existing transaction. Never commits itself."""
    statements = [
        """CREATE TABLE principals (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0 CHECK(is_admin IN (0, 1)),
            authz_version INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            disabled_at REAL
        )""",
        """CREATE TABLE spaces (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('personal', 'project')),
            name TEXT NOT NULL,
            owner_id TEXT NOT NULL REFERENCES principals(id),
            created_at REAL NOT NULL,
            archived_at REAL
        )""",
        "CREATE UNIQUE INDEX one_personal_space ON spaces(owner_id) WHERE kind = 'personal'",
        """CREATE TABLE memberships (
            space_id TEXT NOT NULL REFERENCES spaces(id),
            principal_id TEXT NOT NULL REFERENCES principals(id),
            role TEXT NOT NULL CHECK(role IN ('owner', 'member', 'viewer')),
            version INTEGER NOT NULL DEFAULT 1,
            joined_at REAL NOT NULL,
            revoked_at REAL,
            PRIMARY KEY(space_id, principal_id)
        )""",
        "CREATE INDEX memberships_principal ON memberships(principal_id, revoked_at)",
        """CREATE TABLE collaboration_invites (
            token_hash TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('member', 'device', 'recovery', 'channel')),
            issued_by TEXT NOT NULL REFERENCES principals(id),
            target_principal_id TEXT REFERENCES principals(id),
            space_id TEXT REFERENCES spaces(id),
            role TEXT CHECK(role IN ('owner', 'member', 'viewer')),
            label TEXT NOT NULL DEFAULT '',
            issuer_version INTEGER NOT NULL,
            membership_version INTEGER,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            used_at REAL,
            revoked_at REAL
        )""",
        "CREATE INDEX collaboration_invites_issuer ON collaboration_invites(issued_by, kind, expires_at)",
        """CREATE TABLE resource_grants (
            id TEXT PRIMARY KEY,
            space_id TEXT NOT NULL REFERENCES spaces(id),
            resource_type TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            principal_id TEXT NOT NULL DEFAULT '*',
            actions TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            issued_by TEXT NOT NULL REFERENCES principals(id),
            created_at REAL NOT NULL,
            expires_at REAL,
            revoked_at REAL,
            UNIQUE(space_id, resource_type, resource_id, principal_id)
        )""",
        """CREATE TABLE channel_identities (
            channel TEXT NOT NULL,
            account_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            principal_id TEXT NOT NULL REFERENCES principals(id),
            space_id TEXT NOT NULL REFERENCES spaces(id),
            recipient_id TEXT NOT NULL,
            membership_version INTEGER NOT NULL,
            principal_version INTEGER NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            bound_at REAL NOT NULL,
            revoked_at REAL,
            PRIMARY KEY(channel, account_id, sender_id)
        )""",
        """CREATE TABLE legacy_channel_owners (
            channel TEXT NOT NULL,
            sender TEXT NOT NULL,
            approved_at REAL NOT NULL,
            PRIMARY KEY(channel, sender)
        )""",
        """CREATE TABLE collaboration_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL,
            space_id TEXT,
            subject_id TEXT,
            created_at REAL NOT NULL
        )""",
        "INSERT INTO principals(id, display_name, is_admin, created_at) VALUES ('owner', 'Owner', 1, 0)",
        "INSERT INTO spaces(id, kind, name, owner_id, created_at) VALUES ('personal-owner', 'personal', 'Personal', 'owner', 0)",
        "INSERT INTO memberships(space_id, principal_id, role, joined_at) VALUES ('personal-owner', 'owner', 'owner', 0)",
        "INSERT INTO legacy_channel_owners(channel, sender, approved_at) SELECT channel, sender, approved_at FROM senders",
        "ALTER TABLE web_sessions ADD COLUMN principal_id TEXT NOT NULL DEFAULT 'owner'",
        "ALTER TABLE web_sessions ADD COLUMN authz_version INTEGER NOT NULL DEFAULT 1",
        "CREATE INDEX web_sessions_principal ON web_sessions(principal_id, revoked_at)",
        "ALTER TABLE web_invites ADD COLUMN principal_id TEXT NOT NULL DEFAULT 'owner'",
        "ALTER TABLE web_invites ADD COLUMN issued_by TEXT NOT NULL DEFAULT 'owner'",
        "ALTER TABLE web_invites ADD COLUMN issuer_version INTEGER NOT NULL DEFAULT 1",
    ]
    for statement in statements:
        conn.execute(statement)
