"""Run records: one row per wake-up, with its trigger, outcome and cost.

Usage is added after *each model call* (not at the end of the run), so cost is accounted for even
when a run crashes half-way, and budget guards see spend as it happens.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from personal_agent.clock import Clock, SystemClock
from personal_agent.db import Database, dumps, loads

TERMINAL = ("completed", "silent", "failed", "paused_budget", "cancelled")
STATUSES = ("running", "awaiting_approval", *TERMINAL)
SETTLED = ("completed", "silent", "awaiting_approval")
"""A run in one of these states has nothing left to do for its events: the answer is queued, or the
owner is being asked. (A `failed`, `cancelled` or budget-paused run is retried on purpose.)"""


@dataclass(frozen=True)
class RunRecord:
    id: str
    session_key: str
    trigger: str
    profile: str
    status: str
    model: str | None
    event_ids: list[int]
    started_at: float
    finished_at: float | None
    requests: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: float
    tool_calls: int
    tainted: bool
    final_text: str | None
    error: str | None


def _run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        session_key=row["session_key"],
        trigger=row["trigger"],
        profile=row["profile"],
        status=row["status"],
        model=row["model"],
        event_ids=loads(row["event_ids"], []),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        requests=row["requests"],
        input_tokens=row["input_tokens"],
        cached_tokens=row["cached_tokens"],
        output_tokens=row["output_tokens"],
        reasoning_tokens=row["reasoning_tokens"],
        cost_usd=row["cost_usd"],
        tool_calls=row["tool_calls"],
        tainted=bool(row["tainted"]),
        final_text=row["final_text"],
        error=row["error"],
    )


class RunStore:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock = clock or SystemClock()

    def start(
        self,
        *,
        session_key: str,
        trigger: str,
        profile: str,
        model: str | None,
        event_ids: list[int],
        tainted: bool = False,
    ) -> str:
        run_id = secrets.token_hex(6)
        with self.db.tx():
            self.db.execute(
                """INSERT INTO runs(id, session_key, trigger, profile, status, model, event_ids,
                                    started_at, tainted)
                   VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)""",
                (
                    run_id,
                    session_key,
                    trigger,
                    profile,
                    model,
                    dumps(event_ids),
                    self.clock.now(),
                    int(tainted),
                ),
            )
        return run_id

    def add_usage(
        self,
        run_id: str,
        *,
        requests: int = 1,
        input_tokens: int = 0,
        cached_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        with self.db.tx():
            self.db.execute(
                """UPDATE runs SET requests = requests + ?, input_tokens = input_tokens + ?,
                   cached_tokens = cached_tokens + ?, output_tokens = output_tokens + ?,
                   reasoning_tokens = reasoning_tokens + ?, cost_usd = cost_usd + ? WHERE id = ?""",
                (
                    requests,
                    input_tokens,
                    cached_tokens,
                    output_tokens,
                    reasoning_tokens,
                    cost_usd,
                    run_id,
                ),
            )

    def add_tool_call(self, run_id: str) -> None:
        with self.db.tx():
            self.db.execute("UPDATE runs SET tool_calls = tool_calls + 1 WHERE id = ?", (run_id,))

    def mark_tainted(self, run_id: str) -> None:
        with self.db.tx():
            self.db.execute("UPDATE runs SET tainted = 1 WHERE id = ?", (run_id,))

    def set_status(self, run_id: str, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown run status {status!r}")
        with self.db.tx():
            self.db.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))

    def finish(
        self,
        run_id: str,
        status: str,
        *,
        final_text: str | None = None,
        error: str | None = None,
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown run status {status!r}")
        with self.db.tx():
            self.db.execute(
                """UPDATE runs SET status = ?, finished_at = ?, final_text = COALESCE(?, final_text),
                   error = COALESCE(?, error) WHERE id = ?""",
                (
                    status,
                    self.clock.now() if status in TERMINAL else None,
                    final_text,
                    error,
                    run_id,
                ),
            )

    def get(self, run_id: str) -> RunRecord | None:
        row = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return _run(row) if row else None

    def recent(self, limit: int = 20, *, session_key: str | None = None) -> list[RunRecord]:
        if session_key:
            rows = self.db.query(
                "SELECT * FROM runs WHERE session_key = ? ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (session_key, limit),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
            )
        return [_run(r) for r in rows]

    def cost_since(self, since: float, *, session_key: str | None = None) -> float:
        if session_key:
            return float(
                self.db.scalar(
                    "SELECT SUM(cost_usd) FROM runs WHERE started_at >= ? AND session_key = ?",
                    (since, session_key),
                    default=0.0,
                )
            )
        return float(
            self.db.scalar(
                "SELECT SUM(cost_usd) FROM runs WHERE started_at >= ?", (since,), default=0.0
            )
        )

    def handled(self, session_key: str, event_ids: Collection[int], *, since: float) -> set[int]:
        """Which of these events an earlier run has already dealt with.

        A batch is claimed again when its worker died before saying it was done. If the run had
        reached its end by then, running it again would answer the owner twice, or close the
        request that is still waiting for their approval. `since` (when the oldest event arrived)
        bounds the search to runs that can have seen them.
        """
        wanted = set(event_ids)
        marks = ",".join("?" * len(SETTLED))
        rows = self.db.query(
            f"""SELECT event_ids FROM runs
                WHERE session_key = ? AND started_at >= ? AND status IN ({marks})""",
            (session_key, since, *SETTLED),
        )
        done: set[int] = set()
        for row in rows:
            done |= wanted.intersection(loads(row["event_ids"], []))
        return done

    def recover_orphans(self) -> int:
        """Runs left `running` by a process that died are marked failed (called at startup)."""
        with self.db.tx():
            cur = self.db.execute(
                """UPDATE runs SET status = 'failed', finished_at = ?, error = 'process stopped during the run'
                   WHERE status = 'running'""",
                (self.clock.now(),),
            )
        return cur.rowcount

    def summary(self) -> dict[str, Any]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM runs GROUP BY status")
        return {r["status"]: r["n"] for r in rows}
