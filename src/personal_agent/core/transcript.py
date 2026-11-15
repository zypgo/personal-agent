"""The conversation as the owner lived it: what they said and what actually reached them.

Different from the session log on purpose. The session log is what the *model* is shown, and
`/new`, compaction and tombstones rewrite the view of it. This table is the owner's history across
every channel (terminal, phone, chat app); it only ever grows, so the app can always show what
happened, and it records an agent message only once a channel has confirmed delivering it.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

from personal_agent.clock import Clock, SystemClock
from personal_agent.core.live import LiveBus
from personal_agent.core.outbox import OutMessage
from personal_agent.db import Database, dumps, loads

IN, OUT = "in", "out"

MAX_TERMS = 8
MAX_TERM_CHARS = 80
INDEXED_MIN_CHARS = 3
"""The trigram index finds words of at least this many characters; shorter ones are scanned for."""
_QUERY_TOKEN = re.compile(r'"([^"]+)"|(\S+)')
_EDGE_PUNCTUATION = "\"'.,;:!?()[]{}<>\u3002\uff0c\u3001\uff1b\uff1a\uff01\uff1f\uff08\uff09"


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


@dataclass(frozen=True)
class Hit:
    turn: Turn
    snippet: str
    """The part of the message around the words that matched."""


def search_terms(query: str) -> list[str]:
    """The words of a search: separated by spaces, or "quoted together" as one phrase."""
    terms: list[str] = []
    for phrase, word in _QUERY_TOKEN.findall(query.replace("\x00", " ")):
        text = " ".join(phrase.split()) if phrase else word.strip(_EDGE_PUNCTUATION)
        text = text[:MAX_TERM_CHARS]
        if text and text.lower() not in (t.lower() for t in terms):
            terms.append(text)
    return terms[:MAX_TERMS]


def snippet_of(text: str, terms: Sequence[str], width: int = 240) -> str:
    """`text` on one line, cut to about `width` characters around the first word that matched."""
    flat = " ".join(text.split())
    if len(flat) <= width:
        return flat
    lower = flat.lower()
    at = min((i for t in terms if (i := lower.find(t.lower())) >= 0), default=0)
    start = max(0, at - width // 3)
    end = min(len(flat), start + width)
    start = max(0, end - width)
    return ("\u2026" if start else "") + flat[start:end] + ("\u2026" if end < len(flat) else "")


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


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
            new_id = self.db.insert(
                """INSERT OR IGNORE INTO transcript(at, direction, channel, kind, text, outbox_id, meta)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (self.clock.now(), direction, channel, kind, text, outbox_id, dumps(meta)),
            )
            if new_id is None:
                return None
            row = self.db.row("SELECT * FROM transcript WHERE id = ?", (new_id,))
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

    def recent_messages(self, limit: int) -> list[Turn]:
        """The newest `limit` messages the agent got through to the owner (oldest first): what a
        fresh conversation needs to know so that it does not say the same thing twice. Only what a
        channel confirmed delivering is here, and not the questions about approvals."""
        if limit <= 0:
            return []
        rows = self.db.query(
            """SELECT * FROM transcript WHERE direction = 'out' AND kind = 'message'
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
        return [_turn(r) for r in reversed(rows)]

    def last_id(self) -> int:
        return int(self.db.scalar("SELECT MAX(id) FROM transcript", default=0))

    def last_in_channel(
        self, *, among: Collection[str] | None = None, exclude: Collection[str] = ()
    ) -> str | None:
        """The channel the owner last spoke on (where a "tell me when..." should be answered),
        counting only the channels in `among` when given and never those in `exclude`."""
        skip = tuple(exclude)
        only = None if among is None else tuple(among)
        if only is not None and not only:
            return None
        sql = "SELECT channel FROM transcript WHERE direction = 'in'"
        params: list[str] = []
        if only is not None:
            sql += f" AND channel IN ({','.join('?' * len(only))})"
            params.extend(only)
        if skip:
            sql += f" AND channel NOT IN ({','.join('?' * len(skip))})"
            params.extend(skip)
        row = self.db.one(sql + " ORDER BY id DESC LIMIT 1", params)
        return row["channel"] if row else None

    # ---- searching --------------------------------------------------------------------
    def _indexed(self) -> bool:
        return self.db.one("SELECT 1 FROM sqlite_master WHERE name = 'transcript_fts'") is not None

    def search(
        self,
        query: str,
        *,
        direction: str | None = None,
        since: float | None = None,
        limit: int = 8,
    ) -> list[Hit]:
        """Messages (not notices or approval questions) that contain the words of `query`.

        Every word has to be in a message; if no message has them all, messages with any of them
        are returned. Words are never read as search syntax: an operator or a quote typed into a
        query is just text to look for. Most relevant first when the index can rank; otherwise
        (a word under three characters, or an SQLite without FTS5) newest first."""
        terms = search_terms(query)
        if not terms or limit <= 0:
            return []
        where = "t.kind = 'message'"
        extra: list[object] = []
        if direction:
            where += " AND t.direction = ?"
            extra.append(direction)
        if since is not None:
            where += " AND t.at >= ?"
            extra.append(since)
        if self._indexed() and all(len(t) >= INDEXED_MIN_CHARS for t in terms):
            rows = self._match(terms, " ", where, extra, limit)
            if not rows and len(terms) > 1:
                rows = self._match(terms, " OR ", where, extra, limit)
        else:
            rows = self._scan(terms, where, extra, limit)
        return [Hit(_turn(r), snippet_of(r["text"], terms)) for r in rows]

    def _match(
        self, terms: Sequence[str], joiner: str, where: str, extra: list[object], limit: int
    ) -> list[sqlite3.Row]:
        return self.db.query(
            f"""SELECT t.* FROM transcript_fts JOIN transcript t ON t.id = transcript_fts.rowid
                WHERE transcript_fts MATCH ? AND {where}
                ORDER BY bm25(transcript_fts), t.id DESC LIMIT ?""",
            [joiner.join(_quote(t) for t in terms), *extra, limit],
        )

    def _scan(
        self, terms: Sequence[str], where: str, extra: list[object], limit: int
    ) -> list[sqlite3.Row]:
        """Every word must be in the message (`LIKE` ignores case for ASCII only)."""
        every = " AND ".join("t.text LIKE ? ESCAPE '\\'" for _ in terms)
        return self.db.query(
            f"SELECT t.* FROM transcript t WHERE {where} AND {every} ORDER BY t.id DESC LIMIT ?",
            [*extra, *(_like(t) for t in terms), limit],
        )

    def around(self, turn_id: int, *, before: int = 3, after: int = 3) -> list[Turn]:
        """The message `turn_id` and the ones just before and after it, oldest first: the exchange
        a search result was part of. Empty if there is no such message."""
        middle = self.db.one(
            "SELECT * FROM transcript WHERE id = ? AND kind = 'message'", (turn_id,)
        )
        if middle is None:
            return []
        earlier = self.db.query(
            "SELECT * FROM transcript WHERE id < ? AND kind = 'message' ORDER BY id DESC LIMIT ?",
            (turn_id, max(0, before)),
        )
        later = self.db.query(
            "SELECT * FROM transcript WHERE id > ? AND kind = 'message' ORDER BY id LIMIT ?",
            (turn_id, max(0, after)),
        )
        return [_turn(r) for r in (*reversed(earlier), middle, *later)]
