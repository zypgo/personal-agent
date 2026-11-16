"""Audit log: every tool call, what the policy decided about it, and how it ended.

This answers "what did the agent actually do while I was away?". Arguments are redacted and
truncated before they are stored; the log lives in the same database as everything else and is
only ever appended to (except that a row's outcome is filled in once the call finishes).
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from personal_agent.clock import Clock, SystemClock
from personal_agent.db import Database
from personal_agent.security.redact import known_secret_values, redact
from personal_agent.security.sanitize import clipped

MAX_ARGS_CHARS = 1500
DETAIL_CHARS = 500
"""A call's detail as stored; the tool layer already keeps it shorter and marks what it cut."""


@dataclass(frozen=True)
class AuditRow:
    id: int
    ts: float
    run_id: str | None
    session_key: str | None
    call_id: str | None
    tool: str
    category: str | None
    verdict: str
    layer: str | None
    reason: str | None
    args: str | None
    detail: str | None
    outcome: str | None
    duration_ms: int | None
    tainted: bool


def _row(row: sqlite3.Row) -> AuditRow:
    return AuditRow(
        id=row["id"],
        ts=row["ts"],
        run_id=row["run_id"],
        session_key=row["session_key"],
        call_id=row["call_id"],
        tool=row["tool"],
        category=row["category"],
        verdict=row["verdict"],
        layer=row["layer"],
        reason=row["reason"],
        args=row["args"],
        detail=row["detail"],
        outcome=row["outcome"],
        duration_ms=row["duration_ms"],
        tainted=bool(row["tainted"]),
    )


def safe_args(args: Any) -> str:
    """Arguments as text with credentials removed and a size cap."""
    try:
        text = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(args)
    text = redact(text, known_secret_values(os.environ))
    if len(text) > MAX_ARGS_CHARS:
        text = text[:MAX_ARGS_CHARS] + f"... [{len(text) - MAX_ARGS_CHARS} more characters]"
    return text


class Audit:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock = clock or SystemClock()

    def record(
        self,
        *,
        run_id: str | None,
        session_key: str | None,
        call_id: str | None,
        tool: str,
        category: str | None,
        verdict: str,
        layer: str | None,
        reason: str | None,
        args: Any,
        detail: str = "",
        outcome: str | None = None,
        tainted: bool = False,
    ) -> int | None:
        """Record a decision. A second record for the same (run, call) is ignored, so a decision
        made while asking for approval is not duplicated when the call later executes."""
        with self.db.tx():
            return self.db.insert(
                """INSERT OR IGNORE INTO audit
                   (ts, run_id, session_key, call_id, tool, category, verdict, layer, reason, args,
                    detail, outcome, tainted)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.clock.now(),
                    run_id,
                    session_key,
                    call_id,
                    tool,
                    category,
                    verdict,
                    layer,
                    reason,
                    safe_args(args),
                    clipped(redact(detail, known_secret_values(os.environ)), DETAIL_CHARS),
                    outcome,
                    int(tainted),
                ),
            )

    def set_outcome(
        self,
        run_id: str,
        call_id: str,
        outcome: str,
        duration_ms: int | None = None,
    ) -> None:
        with self.db.tx():
            self.db.execute(
                """UPDATE audit SET outcome = ?, duration_ms = COALESCE(?, duration_ms)
                   WHERE run_id = ? AND call_id = ?""",
                (outcome, duration_ms, run_id, call_id),
            )

    def outcome_of(self, run_id: str, call_id: str) -> str | None:
        row = self.db.one(
            "SELECT outcome FROM audit WHERE run_id = ? AND call_id = ?", (run_id, call_id)
        )
        return row["outcome"] if row else None

    def mark_interrupted(self) -> int:
        """Calls recorded as running by a process that died: their effect is unknown."""
        with self.db.tx():
            cur = self.db.execute(
                "UPDATE audit SET outcome = 'interrupted' WHERE outcome = 'running'"
            )
        return cur.rowcount

    def recent(
        self,
        limit: int = 50,
        *,
        run_id: str | None = None,
        tool: str | None = None,
        verdict: str | None = None,
    ) -> list[AuditRow]:
        clauses, params = [], []
        for column, value in (("run_id", run_id), ("tool", tool), ("verdict", verdict)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.query(
            f"SELECT * FROM audit {where} ORDER BY id DESC LIMIT ?", (*params, limit)
        )
        return [_row(r) for r in rows]

    def counts(self, since: float) -> dict[str, int]:
        rows = self.db.query(
            "SELECT verdict, COUNT(*) AS n FROM audit WHERE ts >= ? GROUP BY verdict", (since,)
        )
        return {r["verdict"]: r["n"] for r in rows}
