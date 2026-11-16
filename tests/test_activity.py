from __future__ import annotations

import pytest

from personal_agent.core.activity import activity_snapshot
from personal_agent.core.events import Kind, NewEvent
from web_fake import Web


def add(app, text="Call mom"):
    return app.inbox.add(
        NewEvent(source="web", kind=Kind.MESSAGE, session_key="main", payload={"text": text})
    )


def run(app, event_id):
    return app.runs.start(
        session_key="main", trigger="message", profile="full", model="test", event_ids=[event_id]
    )


def test_a_message_is_visible_before_its_first_run(make_app):
    app = make_app()
    event = add(app)
    snapshot = activity_snapshot(app)
    assert snapshot["queue"]["pending"] == 1
    assert snapshot["items"][0]["id"] == f"event:{event.id}"
    assert snapshot["items"][0]["title"] == "Call mom"
    assert snapshot["items"][0]["state"] == "queued"


def test_failed_run_with_pending_event_is_a_retry_not_a_terminal_failure(make_app):
    app = make_app()
    event = add(app)
    key = run(app, event.id)
    app.runs.finish(key, "failed", error="secret remote error")
    at = app.clock.now() + 60
    app.db.execute("UPDATE events SET available_at=?, attempts=1 WHERE id=?", (at, event.id))
    item = activity_snapshot(app)["items"][0]
    assert item["state"] == "retrying" and item["next_retry_at"] == at
    assert "secret" not in str(item)


def test_dead_work_is_stopped_and_old_retry_runs_do_not_duplicate_it(make_app):
    app = make_app()
    event = add(app)
    old = run(app, event.id)
    app.runs.finish(old, "failed")
    app.clock.advance(1)
    latest = run(app, event.id)
    app.runs.finish(latest, "failed")
    app.db.execute("UPDATE events SET status='dead', attempts=4 WHERE id=?", (event.id,))
    snapshot = activity_snapshot(app)
    assert snapshot["queue"]["dead"] == 1
    assert len(snapshot["items"]) == 1
    assert snapshot["items"][0]["id"] == latest
    assert snapshot["items"][0]["state"] == "stopped"


def test_approval_wait_is_visible_after_its_original_event_is_done(make_app):
    app = make_app()
    event = add(app)
    key = run(app, event.id)
    app.runs.set_status(key, "awaiting_approval")
    app.db.execute("UPDATE events SET status='done' WHERE id=?", (event.id,))
    assert activity_snapshot(app)["items"][0]["state"] == "awaiting_approval"


@pytest.mark.parametrize("path", ["/api/activity", "/api/status"])
def test_activity_is_owner_only_and_not_cached(make_app, path):
    web = Web(make_app())
    assert web.client.get(path).status_code == 401
    web.pair()
    add(web.app, "private task")
    response = web.client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert "private task" in str(data)
