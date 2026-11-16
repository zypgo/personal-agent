"""Who may open the web page: pairing links, and the browser sessions they turn into.

The page can run the agent, so getting in is the one thing that must be hard. The flow, in the
order it happens:

1. At the computer the owner runs `pa web pair`. That makes a random code (192 bits) and prints it
   as a link, `https://.../#pair=CODE`. The code is in the link's *fragment*: browsers never send a
   fragment to a server, so it cannot appear in an access log, a proxy log or a Referer header.
2. The page reads the fragment and trades it for a session once. The code then stops working.
3. The session is a second random token in an HttpOnly cookie. This store keeps only the SHA-256 of
   both tokens: a copy of the database (a backup, a stolen laptop image) cannot open the page.
4. A session lasts a while after it was last used, and the owner can end any of them, from the
   computer, at any time.

Both tokens are long random strings, so a fast hash is the right one (nothing here is a password
that a person picked) and looking them up by their hash leaks nothing useful.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
from dataclasses import dataclass

from personal_agent.channels.pairing import clean_label
from personal_agent.clock import Clock, SystemClock
from personal_agent.db import Database
from personal_agent.security.sanitize import strip_invisible

log = logging.getLogger(__name__)

TOUCH_EVERY_S = 60.0
"""Using a session pushes its expiry forward, at most once a minute (not a write per request)."""
MAX_CODE_CHARS = 200
"""Longer than any code we make, shorter than something worth hashing."""
KEEP_DEAD_S = 7 * 86400.0
"""How long ended sessions stay visible in `pa web sessions` before they are forgotten."""
MAX_LIVE_INVITES = 5
"""Links that wait to be used. More than that is somebody mashing the command."""


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clean_agent(text: str) -> str:
    """A browser's own description of itself, reduced to something safe to print and easy to
    recognise ("Safari on iPhone"). It is only ever shown to the owner."""
    flat = " ".join(strip_invisible(text, formatting=True).split())
    for needle, name in (
        ("Edg/", "Edge"),
        ("Firefox/", "Firefox"),
        ("CriOS/", "Chrome"),
        ("Chrome/", "Chrome"),
        ("Safari/", "Safari"),
    ):
        if needle in flat:
            browser = name
            break
    else:
        browser = flat[:30] or "unknown browser"
    for needle, name in (
        ("iPhone", "iPhone"),
        ("iPad", "iPad"),
        ("Android", "Android"),
        ("Macintosh", "Mac"),
        ("Windows", "Windows"),
        ("Linux", "Linux"),
    ):
        if needle in flat:
            return f"{browser} on {name}"
    return browser


@dataclass(frozen=True)
class Invite:
    code: str
    """Shown once, when it is made; only its hash is stored."""
    label: str
    expires_at: float


@dataclass(frozen=True)
class PendingInvite:
    label: str
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class WebSession:
    id: int
    label: str
    agent: str
    created_at: float
    last_seen_at: float
    expires_at: float
    revoked_at: float | None = None

    def state(self, now: float) -> str:
        if self.revoked_at is not None:
            return "revoked"
        return "expired" if self.expires_at <= now else "active"


@dataclass(frozen=True)
class NewSession:
    token: str
    """What goes in the cookie. Not stored anywhere on this side."""
    session: WebSession


def _session(row: sqlite3.Row) -> WebSession:
    return WebSession(
        id=row["id"],
        label=row["label"],
        agent=row["agent"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )


class WebAuth:
    def __init__(
        self,
        db: Database,
        clock: Clock | None = None,
        *,
        session_days: float = 30.0,
        invite_minutes: float = 10.0,
    ) -> None:
        self.db = db
        self.clock = clock or SystemClock()
        self.session_s = session_days * 86400.0
        self.invite_s = invite_minutes * 60.0

    # ---- links ------------------------------------------------------------------------
    def invite(self, label: str = "") -> Invite:
        """A one-time code for a browser to trade for a session."""
        now = self.clock.now()
        code = secrets.token_urlsafe(24)
        with self.db.tx():
            self._purge(now)
            waiting = self.db.scalar(
                "SELECT COUNT(*) FROM web_invites WHERE used_at IS NULL AND expires_at > ?",
                (now,),
                default=0,
            )
            if waiting >= MAX_LIVE_INVITES:
                raise TooManyInvites(
                    f"{waiting} pairing links are still waiting to be used; use one, or wait for "
                    "them to expire"
                )
            self.db.execute(
                "INSERT INTO web_invites(token_hash, label, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (digest(code), clean_label(label), now, now + self.invite_s),
            )
        return Invite(code, clean_label(label), now + self.invite_s)

    def pending_invites(self) -> list[PendingInvite]:
        rows = self.db.query(
            "SELECT * FROM web_invites WHERE used_at IS NULL AND expires_at > ? ORDER BY created_at",
            (self.clock.now(),),
        )
        return [PendingInvite(r["label"], r["created_at"], r["expires_at"]) for r in rows]

    def cancel_invites(self) -> int:
        """Make every unused pairing link stop working."""
        with self.db.tx():
            cur = self.db.execute(
                "UPDATE web_invites SET used_at = ? WHERE used_at IS NULL", (self.clock.now(),)
            )
        return cur.rowcount

    def redeem(self, code: str, *, agent: str = "") -> NewSession | None:
        """Trade a pairing code for a session. The code is used up whether or not anything else
        goes wrong afterwards; a wrong, used or expired code returns None."""
        code = code.strip()
        if not code or len(code) > MAX_CODE_CHARS:
            return None
        now = self.clock.now()
        token = secrets.token_urlsafe(32)
        with self.db.tx():
            claimed = self.db.execute(
                "UPDATE web_invites SET used_at = ? "
                "WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
                (now, digest(code), now),
            )
            if claimed.rowcount != 1:
                return None
            label = self.db.scalar(
                "SELECT label FROM web_invites WHERE token_hash = ?", (digest(code),), default=""
            )
            new_id = self.db.insert(
                """INSERT INTO web_sessions(token_hash, label, agent, created_at, last_seen_at,
                                            expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (digest(token), label, clean_agent(agent), now, now, now + self.session_s),
            )
            row = self.db.row("SELECT * FROM web_sessions WHERE id = ?", (new_id,))
        return NewSession(token, _session(row))

    # ---- sessions ---------------------------------------------------------------------
    def lookup(self, token: str) -> WebSession | None:
        """The session a cookie belongs to, or None. Never raises: a database that cannot be read
        lets nobody in."""
        if not token or len(token) > MAX_CODE_CHARS:
            return None
        now = self.clock.now()
        try:
            row = self.db.one(
                "SELECT * FROM web_sessions "
                "WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?",
                (digest(token), now),
            )
            if row is None:
                return None
            session = _session(row)
            if now - session.last_seen_at >= TOUCH_EVERY_S:
                self.db.execute(
                    "UPDATE web_sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
                    (now, now + self.session_s, session.id),
                )
                session = WebSession(
                    session.id,
                    session.label,
                    session.agent,
                    session.created_at,
                    now,
                    now + self.session_s,
                )
        except sqlite3.Error:
            log.warning("could not read the web sessions; letting nobody in", exc_info=True)
            return None
        return session

    def sessions(self, *, include_ended: bool = False) -> list[WebSession]:
        if include_ended:
            rows = self.db.query("SELECT * FROM web_sessions ORDER BY created_at")
        else:
            rows = self.db.query(
                "SELECT * FROM web_sessions WHERE revoked_at IS NULL AND expires_at > ? "
                "ORDER BY created_at",
                (self.clock.now(),),
            )
        return [_session(r) for r in rows]

    def revoke(self, session_id: int) -> bool:
        with self.db.tx():
            cur = self.db.execute(
                "UPDATE web_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (self.clock.now(), session_id),
            )
        return cur.rowcount > 0

    def revoke_token(self, token: str) -> bool:
        """Sign out the browser that holds this cookie."""
        with self.db.tx():
            cur = self.db.execute(
                "UPDATE web_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (self.clock.now(), digest(token)),
            )
        return cur.rowcount > 0

    def revoke_all(self) -> int:
        with self.db.tx():
            cur = self.db.execute(
                "UPDATE web_sessions SET revoked_at = ? WHERE revoked_at IS NULL",
                (self.clock.now(),),
            )
        return cur.rowcount

    # ---- housekeeping -----------------------------------------------------------------
    def _purge(self, now: float) -> None:
        """Forget what has been dead for a while. Done whenever a link is made (the daily upkeep,
        maintenance.py, does the same by the owner's retention settings)."""
        old = now - KEEP_DEAD_S
        self.db.execute(
            "DELETE FROM web_invites WHERE expires_at < ? OR (used_at IS NOT NULL AND used_at < ?)",
            (old, old),
        )
        self.db.execute(
            "DELETE FROM web_sessions WHERE expires_at < ? OR (revoked_at IS NOT NULL AND revoked_at < ?)",
            (old, old),
        )


class TooManyInvites(Exception):
    """Too many unused pairing links at once."""
