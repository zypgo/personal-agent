"""The run records: which events a run has already dealt with."""

from __future__ import annotations

import pytest

from personal_agent.core.runs import SETTLED, STATUSES


def start(app, *, events: list[int], session: str = "main") -> str:
    return app.runs.start(
        session_key=session, trigger="message", profile="full", model="m", event_ids=events
    )


@pytest.mark.parametrize("status", STATUSES)
def test_only_a_run_that_is_done_or_waiting_for_the_owner_has_handled_its_events(make_app, status):
    """A failed, cancelled or budget-paused run is retried on purpose; a running one is not done."""
    app = make_app()
    run = start(app, events=[1, 2])
    if status != "running":
        app.runs.finish(run, status)

    handled = app.runs.handled("main", [1, 2, 3], since=0)

    assert handled == ({1, 2} if status in SETTLED else set())


def test_a_batch_is_judged_event_by_event(make_app):
    app = make_app()
    app.runs.finish(start(app, events=[1, 2]), "completed")
    app.runs.finish(start(app, events=[3]), "failed")

    assert app.runs.handled("main", [2, 3, 4], since=0) == {2}


def test_a_run_of_another_conversation_has_handled_nothing_here(make_app):
    app = make_app()
    app.runs.finish(start(app, events=[1], session="timer:brief"), "completed")

    assert app.runs.handled("main", [1], since=0) == set()


def test_runs_that_started_before_the_events_arrived_cannot_have_seen_them(make_app, clock):
    app = make_app()
    app.runs.finish(start(app, events=[1]), "completed")
    clock.advance(10)

    assert app.runs.handled("main", [1], since=clock.now()) == set()
    assert app.runs.handled("main", [1], since=clock.now() - 10) == {1}


def test_nothing_is_handled_by_a_run_that_is_still_running(make_app):
    app = make_app()
    start(app, events=[1])

    assert app.runs.handled("main", [1], since=0) == set()
