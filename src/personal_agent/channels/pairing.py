"""Who may talk to the agent: the owner's approved senders, and the pairing codes of the rest.

A chat bot can be found by anyone who knows its name. Strangers must never reach the model (that
costs money and gives them a way to try prompt injection), so a message from an unknown sender is
answered with a short code and nothing else. The owner approves the code *on this machine*
(`pa channels approve`); there is deliberately no way to approve from inside a chat.

Rules, borrowed from the pairing flows of nanobot and openclaw and made stricter:
- a code expires after an hour and the same sender always gets the same one while it lives;
- the sender is told the code once (only a reply that really went out counts);
- at most a few requests wait per channel; further strangers are ignored until one is settled;
- the store fails closed: if it cannot be read, nobody is approved.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from dataclasses import dataclass

from personal_agent.clock import Clock, SystemClock
from personal_agent.db import Database
from personal_agent.security.sanitize import strip_invisible

log = logging.getLogger(__name__)

PAIRING_TTL_S = 3600.0
MAX_PENDING = 3
CODE_LENGTH = 8
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
"""No 0/O or 1/I/L: the owner reads the code off a phone screen and types it on a computer."""
MAX_LABEL = 60


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_code(text: str) -> str:
    """`abcd efgh`, `ABCD-EFGH` and `abcdefgh` are the same code."""
    cleaned = "".join(ch for ch in text.upper() if ch.isalnum())
    return f"{cleaned[:4]}-{cleaned[4:]}" if len(cleaned) == CODE_LENGTH else cleaned


def _label(text: str) -> str:
    """A display name chosen by a stranger, shown in the owner's terminal: no control or
    invisible characters, bounded length."""
    flat = " ".join(strip_invisible(text, formatting=True).split())
    return flat[:MAX_LABEL]


@dataclass(frozen=True)
class PairingRequest:
    code: str
    channel: str
    sender: str
    label: str
    created_at: float
    expires_at: float
    notified_at: float | None = None


@dataclass(frozen=True)
class Sender:
    channel: str
    sender: str
    label: str
    approved_at: float
    last_seen_at: float | None = None


def _request(row: sqlite3.Row) -> PairingRequest:
    return PairingRequest(
        code=row["code"],
        channel=row["channel"],
        sender=row["sender"],
        label=row["label"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        notified_at=row["notified_at"],
    )


def _sender(row: sqlite3.Row) -> Sender:
    return Sender(
        channel=row["channel"],
        sender=row["sender"],
        label=row["label"],
        approved_at=row["approved_at"],
        last_seen_at=row["last_seen_at"],
    )


class PairingStore:
    def __init__(
        self,
        db: Database,
        clock: Clock | None = None,
        *,
        ttl_s: float = PAIRING_TTL_S,
        max_pending: int = MAX_PENDING,
    ) -> None:
        self.db = db
        self.clock = clock or SystemClock()
        self.ttl_s = ttl_s
        self.max_pending = max_pending

    # ---- is this sender allowed? ------------------------------------------------------
    def is_approved(self, channel: str, sender: str) -> bool:
        try:
            row = self.db.one(
                "SELECT 1 FROM senders WHERE channel = ? AND sender = ?", (channel, str(sender))
            )
        except sqlite3.Error:
            log.warning("could not read the approved senders; treating %s as unknown", channel)
            return False  # fail closed
        return row is not None

    def touch(self, channel: str, sender: str) -> None:
        """Remember that this approved sender was just heard from."""
        self.db.execute(
            "UPDATE senders SET last_seen_at = ? WHERE channel = ? AND sender = ?",
            (self.clock.now(), channel, str(sender)),
        )

    def senders(self, channel: str | None = None) -> list[Sender]:
        if channel is None:
            rows = self.db.query("SELECT * FROM senders ORDER BY channel, approved_at")
        else:
            rows = self.db.query(
                "SELECT * FROM senders WHERE channel = ? ORDER BY approved_at", (channel,)
            )
        return [_sender(r) for r in rows]

    def recent_sender(self, channel: str) -> Sender | None:
        """The approved sender heard from most recently (where unprompted messages go)."""
        row = self.db.one(
            """SELECT * FROM senders WHERE channel = ?
               ORDER BY COALESCE(last_seen_at, approved_at) DESC, approved_at DESC LIMIT 1""",
            (channel,),
        )
        return _sender(row) if row else None

    # ---- strangers --------------------------------------------------------------------
    def request(self, channel: str, sender: str, label: str = "") -> PairingRequest | None:
        """The pending request of this sender (made now if there is none).

        Returns None when the channel already has as many waiting requests as it allows: the
        stranger is ignored rather than given a code.
        """
        now = self.clock.now()
        with self.db.tx():
            self.db.execute("DELETE FROM pairings WHERE expires_at <= ?", (now,))
            existing = self.db.one(
                "SELECT * FROM pairings WHERE channel = ? AND sender = ?", (channel, str(sender))
            )
            if existing is not None:
                return _request(existing)
            waiting = self.db.scalar(
                "SELECT COUNT(*) FROM pairings WHERE channel = ?", (channel,), default=0
            )
            if waiting >= self.max_pending:
                return None
            code = new_code()
            while self.db.one("SELECT 1 FROM pairings WHERE code = ?", (code,)) is not None:
                code = new_code()  # pragma: no cover - 31**8 possibilities
            self.db.execute(
                """INSERT INTO pairings(code, channel, sender, label, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (code, channel, str(sender), _label(label), now, now + self.ttl_s),
            )
            created = self.db.one("SELECT * FROM pairings WHERE code = ?", (code,))
        assert created is not None
        return _request(created)

    def mark_notified(self, code: str) -> None:
        """The code was really shown to the requester; do not tell them again."""
        self.db.execute(
            "UPDATE pairings SET notified_at = ? WHERE code = ? AND notified_at IS NULL",
            (self.clock.now(), code),
        )

    def pending(self, channel: str | None = None) -> list[PairingRequest]:
        now = self.clock.now()
        if channel is None:
            rows = self.db.query(
                "SELECT * FROM pairings WHERE expires_at > ? ORDER BY created_at", (now,)
            )
        else:
            rows = self.db.query(
                "SELECT * FROM pairings WHERE expires_at > ? AND channel = ? ORDER BY created_at",
                (now, channel),
            )
        return [_request(r) for r in rows]

    def peek(self, code: str) -> PairingRequest | None:
        """The live request behind a code, without settling it."""
        row = self.db.one(
            "SELECT * FROM pairings WHERE code = ? AND expires_at > ?",
            (normalize_code(code), self.clock.now()),
        )
        return _request(row) if row else None

    # ---- the owner decides -------------------------------------------------------------
    def approve(self, code: str) -> Sender | None:
        """Let the sender behind `code` talk to the agent. None if the code is unknown or expired."""
        key = normalize_code(code)
        now = self.clock.now()
        with self.db.tx():
            row = self.db.one(
                "SELECT * FROM pairings WHERE code = ? AND expires_at > ?", (key, now)
            )
            if row is None:
                return None
            self.db.execute(
                """INSERT INTO senders(channel, sender, label, approved_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(channel, sender) DO UPDATE SET label = excluded.label""",
                (row["channel"], row["sender"], row["label"], now),
            )
            self.db.execute("DELETE FROM pairings WHERE code = ?", (key,))
        return Sender(row["channel"], row["sender"], row["label"], now)

    def deny(self, code: str) -> PairingRequest | None:
        key = normalize_code(code)
        with self.db.tx():
            row = self.db.one("SELECT * FROM pairings WHERE code = ?", (key,))
            if row is None:
                return None
            self.db.execute("DELETE FROM pairings WHERE code = ?", (key,))
        return _request(row)

    def allow(self, channel: str, sender: str, label: str = "") -> Sender:
        """Approve a sender whose id the owner already knows (no code involved)."""
        now = self.clock.now()
        with self.db.tx():
            self.db.execute(
                """INSERT INTO senders(channel, sender, label, approved_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(channel, sender) DO UPDATE SET label = excluded.label""",
                (channel, str(sender), _label(label), now),
            )
            self.db.execute(
                "DELETE FROM pairings WHERE channel = ? AND sender = ?", (channel, str(sender))
            )
        return Sender(channel, str(sender), _label(label), now)

    def revoke(self, channel: str, sender: str) -> bool:
        with self.db.tx():
            cur = self.db.execute(
                "DELETE FROM senders WHERE channel = ? AND sender = ?", (channel, str(sender))
            )
            self.db.execute(
                "DELETE FROM pairings WHERE channel = ? AND sender = ?", (channel, str(sender))
            )
        return cur.rowcount > 0
