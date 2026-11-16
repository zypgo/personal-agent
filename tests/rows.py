"""Rows for the tests of what the database forgets (`maintenance.py`).

One row of any kind that `Janitor` looks at is made by `insert(db, rule, state, t)`, where `rule` is
the rule's name and `state` is one of the states that kind of row can be in. `ENDED` says which of
them are over and done with; `t` is the moment that decides how old the row is. A row that is not
over (a pending message, a timer that will fire, a run in progress) must survive whatever `t` is.

A row that ended at `t` was usually made long before (`LIFETIME`): a timer set two months ago that
fired yesterday. So the rows here are made that way, and a rule that went by the day the row was
made instead of the day it ended gives the wrong answer.
"""

from __future__ import annotations

from personal_agent.db import Database

# rule name -> state -> is the row over and done with?
ENDED: dict[str, dict[str, bool]] = {
    "events": {"done": True, "dead": True, "pending": False, "leased": False},
    "outbox": {
        "sent": True,
        "cancelled": True,
        "pending": False,
        "sending": False,
        "maybe_sent": False,
        "failed": False,
    },
    "timers": {"fired": True, "cancelled": True, "active": False, "paused": False},
    "approvals": {
        "approved": True,
        "denied": True,
        "expired": True,
        "superseded": True,
        "pending": False,
        "approved-but-run-suspended": False,
    },
    "pairings": {"code": True},
    "web_invites": {"expired": True, "used": True},
    "web_sessions": {"expired": True, "revoked": True},
    "runs": {
        "completed": True,
        "silent": True,
        "failed": True,
        "cancelled": True,
        "running": False,
        "awaiting_approval": False,
        "paused_budget": False,
        "completed-but-suspended": False,
    },
    "spend": {"charge": True},
    "usage_events": {"charge": True},
    "ingress_receipts": {
        "done": True,
        "rejected": True,
        "uncertain": True,
        "processing": False,
        "ready": False,
    },
    "audit": {"call": True},
    "memory_history": {"version": True},
}

TABLE = {
    "events": "events",
    "outbox": "outbox",
    "timers": "timers",
    "approvals": "approvals",
    "pairings": "pairings",
    "web_invites": "web_invites",
    "web_sessions": "web_sessions",
    "runs": "runs",
    "spend": "spend",
    "usage_events": "usage_events",
    "ingress_receipts": "ingress_receipts",
    "audit": "audit",
    "memory_history": "memory_versions",
}

DAY = 86_400.0
LIFETIME = 60 * DAY


def _suspended(db: Database, run_id: str) -> None:
    db.execute(
        "INSERT INTO run_states(run_id, session_key, state, meta, created_at) VALUES (?, 's', '{}', '{}', 0)",
        (run_id,),
    )


def insert(
    db: Database, rule: str, state: str, t: float, tag: int = 0, *, path: str = "/m/a"
) -> int:
    """A row of this kind in this state whose moment (when it ended, or would have) is `t`.
    `tag` keeps what must be unique unique. Returns the row's rowid."""
    assert state in ENDED[rule], f"{rule} has no state {state!r}"
    ident = f"{rule}-{tag}"
    if rule == "events":
        done = ENDED[rule][state]
        cur = db.execute(
            """INSERT INTO events(source, kind, session_key, payload, status, created_at,
                                  available_at, done_at) VALUES ('test', 'message', 's', '{}', ?, ?, ?, ?)""",
            (state, t - LIFETIME, t, t if done else None),
        )
    elif rule == "outbox":
        cur = db.execute(
            """INSERT INTO outbox(session_key, channel, payload, status, created_at, not_before,
                                  sent_at) VALUES ('s', 'cli', '{}', ?, ?, ?, ?)""",
            (state, t - LIFETIME if state == "sent" else t, t, t if state == "sent" else None),
        )
    elif rule == "timers":
        cur = db.execute(
            """INSERT INTO timers(name, kind, due_at, prompt, session_key, status, created_at,
                                  last_fired_at) VALUES (?, 'followup', ?, 'p', 's', ?, ?, ?)""",
            (
                ident,
                t,
                state,
                t - LIFETIME if state == "fired" else t,
                t if state == "fired" else None,
            ),
        )
    elif rule == "approvals":
        run_id = ident
        if state == "approved-but-run-suspended":
            _suspended(db, run_id)
        status = "approved" if state.startswith("approved") else state
        decided = status in ("approved", "denied", "superseded")
        cur = db.execute(
            """INSERT INTO approvals(run_id, session_key, call_id, tool, summary, status,
                                     created_at, expires_at, decided_at)
               VALUES (?, 's', 'c', 'shell', 'x', ?, ?, ?, ?)""",
            (
                run_id,
                status,
                t - LIFETIME,
                t - LIFETIME + 43_200
                if decided
                else t,  # decided: it had 12 hours; expired: ended then
                t if decided else None,
            ),
        )
    elif rule == "pairings":
        cur = db.execute(
            """INSERT INTO pairings(code, channel, sender, label, created_at, expires_at)
               VALUES (?, 'telegram', ?, '', ?, ?)""",
            (ident, ident, t - LIFETIME, t),
        )
    elif rule == "web_invites":
        used, expires = (t, t + 3600) if state == "used" else (None, t)
        cur = db.execute(
            "INSERT INTO web_invites(token_hash, created_at, expires_at, used_at) VALUES (?, ?, ?, ?)",
            (ident, t - LIFETIME, expires, used),
        )
    elif rule == "web_sessions":
        revoked, expires = (t, t + 3600) if state == "revoked" else (None, t)
        cur = db.execute(
            """INSERT INTO web_sessions(token_hash, created_at, last_seen_at, expires_at, revoked_at)
               VALUES (?, ?, ?, ?, ?)""",
            (ident, t - LIFETIME, t - DAY, expires, revoked),
        )
    elif rule == "runs":
        run_id = ident
        if state == "completed-but-suspended":
            _suspended(db, run_id)
        status = "completed" if state.startswith("completed") else state
        cur = db.execute(
            """INSERT INTO runs(id, session_key, trigger, profile, status, started_at, finished_at)
               VALUES (?, 's', 'message', 'full', ?, ?, ?)""",
            (run_id, status, t - LIFETIME, t if ENDED[rule][state] else None),
        )
    elif rule == "spend":
        cur = db.execute("INSERT INTO spend(at, kind, usd) VALUES (?, 'transcription', 0.01)", (t,))
    elif rule == "usage_events":
        cur = db.execute(
            "INSERT INTO usage_events(run_id,at,cost_usd) VALUES (?,?,0.01)", (ident, t)
        )
    elif rule == "ingress_receipts":
        cur = db.execute(
            "INSERT INTO ingress_receipts(source,external_id,fingerprint,state,created_at,updated_at,lease_until) VALUES ('web',?,?,?, ?,?,?)",
            (ident, ident, state, t - LIFETIME, t, t),
        )
    elif rule == "audit":
        cur = db.execute(
            "INSERT INTO audit(ts, tool, verdict) VALUES (?, 'shell', 'allow')",
            (t,),
        )
    else:
        assert rule == "memory_history"
        cur = db.execute(
            "INSERT INTO memory_versions(path, action, actor, created_at) VALUES (?, 'update', 'agent', ?)",
            (path, t),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid


def rowids(db: Database, rule: str) -> set[int]:
    return {row[0] for row in db.query(f"SELECT rowid FROM {TABLE[rule]}")}
