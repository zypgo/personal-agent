"""The questions the agent asks every second, and at the start of every run, are answered from an
index however much history the tables hold.

The loops that wait for work ask "is anything ready?" once a second each, and hold the database
while they do. Most of what the inbox holds is old: handled events are kept for months. A question
that reads all of it grows with that history, so it is checked here against a table in which
almost every row is old, the way a busy agent's is after a few weeks."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

import personal_agent.db as dbmod
from helpers import owner_message
from personal_agent.clock import ManualClock
from personal_agent.config import SchedulerConfig
from personal_agent.core.events import Kind, NewEvent
from personal_agent.core.inbox import Inbox
from personal_agent.core.outbox import Outbox
from personal_agent.core.timers import Timers
from personal_agent.db import Database, open_database

HISTORY = 20_000
WHOLE_TABLE = re.compile(r"^SCAN (events|outbox|timers|approvals|e|o)\b")


@pytest.fixture
def busy(clock: ManualClock) -> Database:
    """A database whose events and messages are almost all old and finished."""
    database = open_database(":memory:")
    now = clock.now()
    with database.tx():
        database.executemany(
            """INSERT INTO events(source, kind, session_key, payload, external_id, trust, status,
                                  created_at, available_at, done_at, attempts)
               VALUES ('web', 'message', ?, '{}', ?, 'owner', 'done', ?, ?, ?, 1)""",
            [(f"s{i % 50}", f"old-{i}", now - i, now - i, now - i + 1) for i in range(HISTORY)],
        )
        database.executemany(
            """INSERT INTO outbox(session_key, channel, recipient, kind, payload, urgency, status,
                                  created_at, not_before, sent_at)
               VALUES (?, 'web', 'me', 'message', '{}', 'normal', 'sent', ?, ?, ?)""",
            [(f"s{i % 50}", now - i, now - i, now - i + 1) for i in range(HISTORY)],
        )
    database.execute("ANALYZE")  # what `PRAGMA optimize` does on a running agent
    yield database
    database.close()


def plans_of(db: Database, work: Callable[[], object]) -> list[str]:
    """What the database reads, as it says itself, for every statement `work` runs."""
    statements: list[str] = []
    db._conn.set_trace_callback(statements.append)
    try:
        work()
    finally:
        db._conn.set_trace_callback(None)
    lines: list[str] = []
    for sql in statements:
        if sql.lstrip().startswith(("--", "BEGIN", "COMMIT", "SAVEPOINT", "RELEASE", "ROLLBACK")):
            continue
        lines += [row["detail"] for row in db.query(f"EXPLAIN QUERY PLAN {sql}")]
    return lines


def reads_a_whole_table(lines: list[str]) -> list[str]:
    return [line for line in lines if WHOLE_TABLE.match(line)]


def test_asking_whether_anything_is_ready_does_not_read_the_history(busy, clock):
    inbox = Inbox(busy, clock)
    inbox.add(owner_message("hello", session="s1"))
    inbox.add(
        NewEvent(
            source="web",
            kind=Kind.MESSAGE,
            session_key="s2",
            payload={"text": "later"},
            delay_s=3600,
        )
    )
    claimed = inbox.claim("worker-a")  # a live lease, which is what the other workers pass by
    assert claimed is not None

    lines = plans_of(busy, lambda: inbox.has_ready())

    assert lines and reads_a_whole_table(lines) == []


def test_claiming_does_not_read_the_history_when_there_is_nothing_to_claim(busy, clock):
    inbox = Inbox(busy, clock)
    inbox.add(owner_message("hello", session="s1"))
    assert inbox.claim("worker-a") is not None  # only a live lease is left

    lines = plans_of(busy, lambda: inbox.claim("worker-b"))

    assert lines and reads_a_whole_table(lines) == []


def test_claiming_does_not_read_the_history_when_there_is_work(busy, clock):
    inbox = Inbox(busy, clock)
    inbox.add(owner_message("hello", session="s1"))
    inbox.add(owner_message("and again", session="s2"))

    lines = plans_of(busy, lambda: inbox.claim("worker-a"))

    assert lines and reads_a_whole_table(lines) == []


def test_looking_for_a_lease_that_ran_out_does_not_read_the_history(busy, clock):
    inbox = Inbox(busy, clock)
    inbox.add(owner_message("hello", session="s1"))
    assert inbox.claim("worker-a") is not None
    clock.advance(inbox.lease_s + 1)  # the worker is gone and its lease has run out

    lines = plans_of(busy, lambda: (inbox.has_ready(), inbox.claim("worker-b")))

    assert lines and reads_a_whole_table(lines) == []


def test_asking_whether_a_message_is_due_does_not_read_the_history(busy, clock):
    outbox = Outbox(busy, clock)
    outbox.enqueue(session_key="s1", channel="web", recipient="me", text="hi")

    lines = plans_of(busy, lambda: outbox.has_due({"web"}))

    assert lines and reads_a_whole_table(lines) == []


def test_asking_which_timers_are_due_does_not_read_the_history(busy, clock):
    timers = Timers(busy, Inbox(busy, clock), SchedulerConfig(), clock=clock)

    lines = plans_of(busy, timers.tick)

    assert lines and reads_a_whole_table(lines) == []


def test_a_database_made_before_the_index_gets_it_when_the_program_opens_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "state.db"
    older = dbmod.MIGRATIONS[:6]
    monkeypatch.setattr(dbmod, "MIGRATIONS", older)
    monkeypatch.setattr(dbmod, "SCHEMA_VERSION", 6)
    before = open_database(path)
    before.execute(
        """INSERT INTO events(source, kind, session_key, payload, status, lease_owner,
                              lease_expires_at, created_at, available_at)
           VALUES ('web', 'message', 's1', '{}', 'leased', 'worker-a', 100, 1, 1)"""
    )
    assert before.one("SELECT 1 FROM sqlite_master WHERE name = 'events_leased'") is None
    before.close()
    monkeypatch.undo()

    after = open_database(path)
    try:
        assert after.version == dbmod.SCHEMA_VERSION
        assert after.one("SELECT 1 FROM sqlite_master WHERE name = 'events_leased'") is not None
        assert after.scalar("SELECT COUNT(*) FROM events WHERE status = 'leased'") == 1
    finally:
        after.close()
