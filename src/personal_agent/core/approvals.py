"""Approvals: tool calls that wait for the owner, and the suspended runs that wait with them.

A run that reaches a call needing approval does not block a process. Its state is written to the
database, the owner is asked, and the process moves on (or exits). Whenever the owner answers,
possibly days later and after a restart, an `approval_response` event wakes the agent, which
restores the run and continues with the owner's decision in hand.

Nothing here executes a tool: it only records decisions. A decision that arrives after the owner
moved on (a newer message superseded the request) or after it expired is reported as such and
changes nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from personal_agent.clock import Clock, SystemClock
from personal_agent.core.events import Kind, NewEvent, Trust
from personal_agent.core.inbox import Inbox
from personal_agent.db import Database, dumps, loads

PENDING, APPROVED, DENIED, EXPIRED, SUPERSEDED = (
    "pending",
    "approved",
    "denied",
    "expired",
    "superseded",
)

DENIED_MESSAGE = (
    "The owner declined this action. Do not retry it; ask them how they would like to proceed."
)
EXPIRED_MESSAGE = (
    "The owner did not answer the approval request in time, so this action was not taken. "
    "Do not retry it unless the owner asks again."
)
SUPERSEDED_MESSAGE = (
    "Not executed: the owner sent a new message before answering the approval request."
)


@dataclass(frozen=True)
class Approval:
    id: int
    run_id: str
    session_key: str
    call_id: str
    tool: str
    summary: str
    status: str
    created_at: float
    expires_at: float
    decided_at: float | None = None
    decided_by: str | None = None
    note: str | None = None

    def rejection_message(self) -> str:
        base = EXPIRED_MESSAGE if self.status == EXPIRED else DENIED_MESSAGE
        if self.status == DENIED and self.note:
            base += f" The owner said: {self.note}"
        return base


@dataclass(frozen=True)
class PendingCall:
    call_id: str
    tool: str
    summary: str


@dataclass(frozen=True)
class DecideResult:
    approval: Approval | None
    changed: bool
    resume_queued: bool
    message: str


@dataclass(frozen=True)
class SavedRun:
    run_id: str
    session_key: str
    state: str
    meta: dict[str, Any]


def _approval(row: sqlite3.Row) -> Approval:
    return Approval(
        id=row["id"],
        run_id=row["run_id"],
        session_key=row["session_key"],
        call_id=row["call_id"],
        tool=row["tool"],
        summary=row["summary"],
        status=row["status"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        note=row["note"],
    )


class Approvals:
    def __init__(
        self, db: Database, inbox: Inbox, *, clock: Clock | None = None, timeout_hours: float = 12.0
    ) -> None:
        self.db = db
        self.inbox = inbox
        self.clock = clock or SystemClock()
        self.timeout_s = timeout_hours * 3600

    # ---- creating ---------------------------------------------------------------------
    def create(self, run_id: str, session_key: str, calls: Sequence[PendingCall]) -> list[Approval]:
        """Open one approval per call. Safe to repeat for the same run and call."""
        now = self.clock.now()
        created: list[Approval] = []
        with self.db.tx():
            for call in calls:
                self.db.execute(
                    """INSERT OR IGNORE INTO approvals
                       (run_id, session_key, call_id, tool, summary, created_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        session_key,
                        call.call_id,
                        call.tool,
                        call.summary,
                        now,
                        now + self.timeout_s,
                    ),
                )
                row = self.db.one(
                    "SELECT * FROM approvals WHERE run_id = ? AND call_id = ?",
                    (run_id, call.call_id),
                )
                created.append(_approval(row))
        return created

    # ---- reading ----------------------------------------------------------------------
    def get(self, approval_id: int) -> Approval | None:
        row = self.db.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return _approval(row) if row else None

    def for_run(self, run_id: str) -> list[Approval]:
        rows = self.db.query("SELECT * FROM approvals WHERE run_id = ? ORDER BY id", (run_id,))
        return [_approval(r) for r in rows]

    def pending(self, session_key: str | None = None) -> list[Approval]:
        if session_key:
            rows = self.db.query(
                "SELECT * FROM approvals WHERE status = 'pending' AND session_key = ? ORDER BY id",
                (session_key,),
            )
        else:
            rows = self.db.query("SELECT * FROM approvals WHERE status = 'pending' ORDER BY id")
        return [_approval(r) for r in rows]

    def recent(self, limit: int = 20) -> list[Approval]:
        rows = self.db.query("SELECT * FROM approvals ORDER BY id DESC LIMIT ?", (limit,))
        return [_approval(r) for r in rows]

    def is_approved(self, run_id: str, call_id: str) -> bool:
        return (
            self.db.one(
                "SELECT 1 FROM approvals WHERE run_id = ? AND call_id = ? AND status = 'approved'",
                (run_id, call_id),
            )
            is not None
        )

    def suspended_call_ids(self, session_key: str) -> set[str]:
        """Tool calls of this session that belong to a suspended run and will be answered when
        that run resumes. Crash repair must leave them alone."""
        rows = self.db.query(
            """SELECT a.call_id FROM approvals a JOIN run_states s ON s.run_id = a.run_id
               WHERE a.session_key = ?""",
            (session_key,),
        )
        return {r["call_id"] for r in rows}

    # ---- deciding ---------------------------------------------------------------------
    def decide(
        self, approval_id: int, approve: bool, *, by: str, note: str | None = None
    ) -> DecideResult:
        with self.db.tx():
            row = self.db.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            if row is None:
                return DecideResult(None, False, False, f"There is no approval #{approval_id}.")
            current = _approval(row)
            if current.status != PENDING:
                return DecideResult(
                    current,
                    False,
                    False,
                    f"Approval #{approval_id} is already {current.status}"
                    + (
                        "; the agent moved on after your newer message."
                        if current.status == SUPERSEDED
                        else "."
                    ),
                )
            now = self.clock.now()
            if now >= current.expires_at:
                self._close(current.id, EXPIRED, now, "timeout", None)
                queued = self._queue_resume_if_done(current.run_id, current.session_key)
                return DecideResult(
                    self.get(current.id),
                    False,
                    queued,
                    f"Approval #{approval_id} expired before it was answered; the action was not taken.",
                )
            self._close(
                current.id, APPROVED if approve else DENIED, now, by, (note or "").strip() or None
            )
            queued = self._queue_resume_if_done(current.run_id, current.session_key)
            verb = "Approved" if approve else "Denied"
            return DecideResult(self.get(current.id), True, queued, f"{verb} #{approval_id}.")

    def expire_due(self) -> list[Approval]:
        """Close approvals nobody answered in time and wake the runs waiting on them."""
        now = self.clock.now()
        expired: list[Approval] = []
        with self.db.tx():
            rows = self.db.query(
                "SELECT * FROM approvals WHERE status = 'pending' AND expires_at <= ? ORDER BY id",
                (now,),
            )
            for row in rows:
                approval = _approval(row)
                self._close(approval.id, EXPIRED, now, "timeout", None)
                expired.append(approval)
            for run_id, session_key in {(a.run_id, a.session_key) for a in expired}:
                self._queue_resume_if_done(run_id, session_key)
        return [self.get(a.id) or a for a in expired]

    def supersede_session(self, session_key: str) -> list[Approval]:
        """The owner sent a new message: requests still waiting are closed, never executed."""
        now = self.clock.now()
        with self.db.tx():
            rows = self.db.query(
                "SELECT * FROM approvals WHERE session_key = ? AND status = 'pending' ORDER BY id",
                (session_key,),
            )
            for row in rows:
                self._close(row["id"], SUPERSEDED, now, "system", "superseded by a new message")
        return [self.get(r["id"]) or _approval(r) for r in rows]

    def _close(self, approval_id: int, status: str, at: float, by: str, note: str | None) -> None:
        self.db.execute(
            "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, note = ? WHERE id = ?",
            (status, at, by, note, approval_id),
        )

    def _queue_resume_if_done(self, run_id: str, session_key: str) -> bool:
        """Once every approval of a run is answered, queue the event that resumes it."""
        waiting = self.db.scalar(
            "SELECT COUNT(*) FROM approvals WHERE run_id = ? AND status = 'pending'",
            (run_id,),
            default=0,
        )
        if waiting or self.load_run(run_id) is None:
            return False
        # A run can be suspended again after it resumes (the next call may also need approval),
        # so the idempotency key must differ per round: each round adds approvals.
        round_no = self.db.scalar(
            "SELECT COUNT(*) FROM approvals WHERE run_id = ?", (run_id,), default=0
        )
        self.inbox.add(
            NewEvent(
                source="approval",
                kind=Kind.APPROVAL_RESPONSE,
                session_key=session_key,
                payload={"run_id": run_id},
                external_id=f"resume:{run_id}:{round_no}",
                trust=Trust.OWNER,
            )
        )
        return True

    # ---- suspended runs ---------------------------------------------------------------
    def save_run(self, run_id: str, session_key: str, state: str, meta: dict[str, Any]) -> None:
        with self.db.tx():
            self.db.execute(
                """INSERT OR REPLACE INTO run_states(run_id, session_key, state, meta, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, session_key, state, dumps(meta), self.clock.now()),
            )

    def load_run(self, run_id: str) -> SavedRun | None:
        row = self.db.one("SELECT * FROM run_states WHERE run_id = ?", (run_id,))
        if row is None:
            return None
        return SavedRun(row["run_id"], row["session_key"], row["state"], loads(row["meta"], {}))

    def drop_run(self, run_id: str) -> None:
        with self.db.tx():
            self.db.execute("DELETE FROM run_states WHERE run_id = ?", (run_id,))

    def suspended_runs(self, session_key: str | None = None) -> list[str]:
        if session_key:
            rows = self.db.query(
                "SELECT run_id FROM run_states WHERE session_key = ? ORDER BY created_at",
                (session_key,),
            )
        else:
            rows = self.db.query("SELECT run_id FROM run_states ORDER BY created_at")
        return [r["run_id"] for r in rows]


def request_text(approvals: Sequence[Approval]) -> str:
    """The message that asks the owner. Plain text so every channel can show it."""
    lines = ["I need your approval before I continue:"]
    for a in approvals:
        lines.append(f"  #{a.id}  {a.tool}: {a.summary}")
    ids = " ".join(f"#{a.id}" for a in approvals)
    first = approvals[0].id
    lines.append("")
    lines.append(
        f"Reply `/approve {first}` or `/deny {first}`"
        + (" (and likewise for the others)." if len(approvals) > 1 else ".")
    )
    if len(approvals) > 1:
        lines.append(f"Or answer all at once: `/approve all` / `/deny all` ({ids}).")
    return "\n".join(lines)
