"""An owner-edit conflict needs review, never overwritten files or a blocked daemon."""

from __future__ import annotations

import pytest
from agents.testing import ScriptedModel, assistant_message

from helpers import invoke, owner_message
from personal_agent.core.approvals import PendingCall
from personal_agent.core.worker import Worker
from personal_agent.db import loads
from personal_agent.harness.dreaming import DREAM_KEY, PENDING_PREFIX, Dreamer
from personal_agent.harness.runner import RunOutcome
from personal_agent.memory.store import Actor, MemoryConflictError, MemoryRestoreError
from test_dreaming import dream_batch, notes


def start(app, *, name="note", conflict=True):
    first, second = [f"/memories/state/{name}-{i}.md" for i in range(2)]
    for path in (first, second):
        app.memory.write_text(path, "original", actor=Actor.USER)
    run_id = app.runs.start(
        session_key=DREAM_KEY, trigger="system", profile="dream", model="test", event_ids=[99]
    )
    Dreamer(app).begin(run_id, dream_batch(to_id=40))
    for path in (first, second):
        app.memory.write_text(path, "dream candidate", actor=Actor.DREAM, run_id=run_id)
    if conflict:
        app.memory.write_text(first, "new owner correction", actor=Actor.USER)
    app.runs.finish(run_id, "failed", error="unfinished memory pass")
    return run_id, first, second


def receipt(app, run_id):
    return loads(app.db.kv_get(PENDING_PREFIX + run_id), {})


def review_notices(app):
    return [m for m in app.outbox.recent(limit=100) if "could not automatically undo" in m.text]


def test_recovery_isolates_conflict_and_still_recovers_other_passes(make_app):
    app = make_app()
    run_id, owner_path, other_path = start(app)
    safe_id, safe_path, _ = start(app, name="safe", conflict=False)

    assert app.recover()["recovered_dreams"] == 1
    assert receipt(app, run_id)["needs_review"] is True
    assert receipt(app, run_id)["to_id"] == 40
    assert not receipt(app, safe_id)
    assert app.memory.read_text(owner_path) == "new owner correction"
    assert app.memory.read_text(other_path) == "dream candidate"  # the failed undo was atomic
    assert app.memory.read_text(safe_path) == "original"
    assert Dreamer(app).cursor() == 0
    assert len(review_notices(app)) == 1
    assert "some changes from that pass may remain" in review_notices(app)[0].text


def test_restart_and_repeat_finish_skip_the_flagged_receipt_and_notify_once(make_app, monkeypatch):
    app = make_app()
    run_id, owner_path, _ = start(app)
    app.recover()
    app.close()
    restarted = make_app()

    def should_not_try_again(run_id):
        raise AssertionError("a quarantined conflict must wait for the owner")

    monkeypatch.setattr(restarted.memory, "revert_run", should_not_try_again)
    for _ in range(2):
        assert restarted.recover()["recovered_dreams"] == 0
        Dreamer(restarted).finish_run(run_id, notify_interrupted=True)
        Dreamer(restarted).abort(run_id)
        Dreamer(restarted).finished(dream_batch(to_id=40), RunOutcome(run_id, "silent"))
    assert len(review_notices(restarted)) == 1
    assert restarted.memory.read_text(owner_path) == "new owner correction"
    assert Dreamer(restarted).cursor() == 0
    assert len(Dreamer(restarted).pending_reviews()) == 1


def test_successful_mass_delete_cannot_advance_cursor_or_claim_undo_when_owner_edited(make_app):
    app = make_app()
    paths = notes(app, 8)
    run_id = app.runs.start(
        session_key=DREAM_KEY, trigger="system", profile="dream", model="test", event_ids=[99]
    )
    Dreamer(app).begin(run_id, dream_batch(to_id=40))
    for path in paths[:6]:
        app.memory.delete(path, actor=Actor.DREAM, run_id=run_id)
    app.memory.write_text(paths[0], "new owner correction", actor=Actor.USER)
    app.runs.finish(run_id, "silent")

    Dreamer(app).finished(dream_batch(to_id=40), RunOutcome(run_id, "silent"))
    assert receipt(app, run_id)["needs_review"]
    assert Dreamer(app).cursor() == 0
    assert app.memory.read_text(paths[0]) == "new owner correction"
    assert all(app.memory.read_text(p) is None for p in paths[1:6])
    assert len(review_notices(app)) == 1
    assert not any("undid everything" in m.text for m in app.outbox.recent())


async def test_dead_approval_conflict_does_not_block_other_events_or_retain_sdk_snapshot(make_app):
    app = make_app(ScriptedModel([[assistant_message("The owner's next task still works.")]]))
    run_id, owner_path, _ = start(app)
    [approval] = app.approvals.create(run_id, DREAM_KEY, [PendingCall("c1", "memory", "delete")])
    app.approvals.save_run(run_id, DREAM_KEY, "unreadable old snapshot", {"profile": "dream"})
    app.runs.set_status(run_id, "awaiting_approval")
    app.approvals.decide(approval.id, True, by="owner")
    app.inbox.max_attempts = 1
    batch = app.inbox.claim("dead-process", session_key=DREAM_KEY)
    app.inbox.fail(batch, "cannot resume")
    assert app.inbox.has_unreported_dead()

    app.inbox.add(owner_message("Continue with something else."))
    result = await Worker(app).run_once()
    assert result.outcome.status == "completed"
    assert not app.inbox.has_unreported_dead()
    assert app.approvals.load_run(run_id) is None
    assert receipt(app, run_id)["needs_review"]
    assert app.memory.read_text(owner_path) == "new owner correction"
    assert len(review_notices(app)) == 1
    assert not any("candidate memory changes were undone" in m.text for m in app.outbox.recent())


def test_conflict_marker_and_notice_obey_an_outer_transaction_rollback(make_app):
    app = make_app()
    run_id, owner_path, _ = start(app)
    with pytest.raises(RuntimeError, match="outer transaction"), app.db.tx():
        Dreamer(app).abort(run_id)
        assert receipt(app, run_id)["needs_review"]
        raise RuntimeError("outer transaction failed before acknowledgment")
    assert not receipt(app, run_id).get("needs_review")
    assert not review_notices(app)
    assert app.memory.read_text(owner_path) == "new owner correction"
    Dreamer(app).abort(run_id)
    assert receipt(app, run_id)["needs_review"] and len(review_notices(app)) == 1


def test_io_restore_failure_still_raises_and_is_retried_without_quarantine(make_app, monkeypatch):
    app = make_app()
    run_id, first, second = start(app, conflict=False)

    def failed_write(*args, **kwargs):
        raise OSError("fixture disk temporarily unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(app.memory, "write_text", failed_write)
        with pytest.raises(MemoryRestoreError) as caught:
            Dreamer(app).abort(run_id)
        assert not isinstance(caught.value, MemoryConflictError)
        assert isinstance(caught.value.__cause__, OSError)
    assert receipt(app, run_id) and not receipt(app, run_id).get("needs_review")
    assert not review_notices(app)
    assert app.recover()["recovered_dreams"] == 1
    assert all(app.memory.read_text(p) == "original" for p in (first, second))


def test_review_cli_lists_conflicts_and_requires_explicit_keep_current(make_app, use_app):
    app = use_app(make_app())
    run_id, first, second = start(app)
    Dreamer(app).abort(run_id)
    for command in (("dream", "review"), ("dream", "review", "list")):
        listed = invoke(*command)
        assert listed.exit_code == 0
        assert run_id in listed.output and first in listed.output and "version" in listed.output
        assert "automatic rollback did not complete" in listed.output
    assert "need your decision" in invoke("dream").output
    refused = invoke("dream", "review", "resolve", run_id)
    assert refused.exit_code == 2 and "--keep-current" in refused.output
    assert receipt(app, run_id)["needs_review"]

    contents = {p: app.memory.read_text(p) for p in (first, second)}
    versions = {p: app.memory.history(p) for p in (first, second)}
    resolved = invoke("dream", "review", "resolve", run_id, "--keep-current")
    assert resolved.exit_code == 0 and "Kept current memory" in resolved.output
    assert not receipt(app, run_id)
    assert {p: app.memory.read_text(p) for p in (first, second)} == contents
    assert {p: app.memory.history(p) for p in (first, second)} == versions
    assert Dreamer(app).cursor() == 0
    app.recover()
    assert "No memory passes need review" in invoke("dream", "review").output
    again = invoke("dream", "review", "resolve", run_id, "--keep-current")
    assert again.exit_code == 1 and "No pending review" in again.output


def test_keep_current_cannot_discard_an_active_unreviewed_receipt(make_app):
    app = make_app()
    run_id, _, _ = start(app, conflict=False)
    assert Dreamer(app).resolve_review(run_id, keep_current=True) is False
    assert receipt(app, run_id)
