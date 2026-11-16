"""Retention may not shorten an unfinished dream's rollback chain or erase conflict evidence."""

from __future__ import annotations

import pytest

from personal_agent.config import RetentionConfig
from personal_agent.core.approvals import PendingCall
from personal_agent.maintenance import Janitor
from personal_agent.memory.store import Actor, MemoryRestoreError

DAY = 86400


def start_pending(app):
    run = app.runs.start(
        session_key="system:dream", trigger="system", profile="dream", model="test", event_ids=[]
    )
    app.db.kv_set("dream:pending:" + run, '{"to_id":40}', now=app.clock.now())
    return run


def test_suspended_dream_keeps_its_complete_rollback_chain_beyond_retention(make_app, clock):
    app = make_app()
    path = "/memories/state/note.md"
    app.memory.write_text(path, "ORIGINAL", actor=Actor.USER)
    run = start_pending(app)
    app.memory.write_text(path, "draft 1", actor=Actor.DREAM, run_id=run)
    app.memory.write_text(path, "draft 2", actor=Actor.DREAM, run_id=run)
    [approval] = app.approvals.create(
        run, "system:dream", [PendingCall("c1", "memory", "Continue editing")]
    )
    app.approvals.save_run(run, "system:dream", "{}", {})
    app.runs.set_status(run, "awaiting_approval")
    app.approvals.decide(approval.id, True, by="owner")
    janitor = Janitor(
        app.db, RetentionConfig(records_days=1, runs_days=1, memory_history_days=1), clock
    )
    clock.advance(2 * DAY)
    janitor.prune()
    assert len(app.memory.changes_by_run(run)) == 2

    # A crash after committing failure may leave only the review receipt, without a run_state.
    app.approvals.drop_run(run)
    app.runs.finish(run, "failed")
    clock.advance(2 * DAY)
    janitor.prune()
    assert app.runs.get(run) is not None
    assert len(app.approvals.for_run(run)) == 1
    assert len(app.memory.changes_by_run(run)) == 2
    with app.db.tx():
        app.memory.revert_run(run)
        app.db.execute("DELETE FROM kv WHERE key = ?", ("dream:pending:" + run,))
    assert app.memory.read_text(path) == "ORIGINAL"

    janitor.prune()  # completed review releases its finished records to ordinary retention
    assert app.runs.get(run) is None and app.approvals.for_run(run) == []


def test_pending_review_keeps_later_owner_versions_that_prove_a_conflict(make_app, clock):
    app = make_app()
    path = "/memories/state/note.md"
    app.memory.write_text(path, "original", actor=Actor.USER)
    run = start_pending(app)
    app.memory.write_text(path, "first draft", actor=Actor.DREAM, run_id=run)
    app.memory.write_text(path, "owner corrected", actor=Actor.USER)
    owner_version = app.memory.history(path)[0]["id"]
    app.memory.write_text(path, "resumed draft", actor=Actor.DREAM, run_id=run)
    clock.advance(2 * DAY)
    Janitor(app.db, RetentionConfig(memory_history_days=1), clock).prune()
    assert app.db.one("SELECT 1 FROM memory_versions WHERE id = ?", (owner_version,))
    with pytest.raises(MemoryRestoreError, match="later edit"):
        app.memory.revert_run(run)
    assert app.db.kv_get("dream:pending:" + run) is not None


def test_history_before_the_first_pending_edit_can_still_be_pruned(make_app, clock):
    app = make_app()
    old = "/memories/state/old.md"
    app.memory.write_text(old, "older", actor=Actor.USER)
    app.memory.write_text(old, "newer", actor=Actor.USER)
    run = start_pending(app)
    app.memory.write_text("/memories/state/candidate.md", "draft", actor=Actor.DREAM, run_id=run)
    clock.advance(2 * DAY)
    Janitor(app.db, RetentionConfig(memory_history_days=1), clock).prune()
    assert len(app.memory.history(old)) == 1
    assert len(app.memory.changes_by_run(run)) == 1
