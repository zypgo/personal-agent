"""Failed dream candidates must never become durable memory by escaping final review."""

from __future__ import annotations

import asyncio

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message

from deadlines import Deadlines
from helpers import usage
from personal_agent.config import RuntimeConfig
from personal_agent.harness import runner
from personal_agent.harness.dreaming import DREAM_KEY, FAILURES, PENDING_PREFIX, Dreamer
from personal_agent.harness.errors import RunTimedOut
from personal_agent.harness.runner import Harness
from personal_agent.memory.store import Actor
from test_dreaming import config, dream_batch, memory_call, notes


def deletes(count=6):
    return [
        memory_call(f"d{i}", "delete", path=f"/memories/state/note{i}.md") for i in range(count)
    ]


def pending(app):
    return app.db.query("SELECT key FROM kv WHERE key GLOB ?", (PENDING_PREFIX + "*",))


def unfinished(app, status="running"):
    run_id = app.runs.start(
        session_key=DREAM_KEY, trigger="system", profile="dream", model="test", event_ids=[1]
    )
    Dreamer(app).begin(run_id, dream_batch(to_id=40))
    if status != "running":
        app.runs.finish(run_id, status)
    return run_id


async def test_provider_failure_after_destructive_edits_rolls_back_before_retry(make_app):
    app = make_app(ScriptedModel([deletes(), RuntimeError("provider failed")]), config=config())
    paths = notes(app, 8)
    before = {p: app.memory.read_text(p) for p in paths}
    with pytest.raises(RuntimeError, match="provider failed"):
        await Harness(app).run(dream_batch())
    assert {p: app.memory.read_text(p) for p in paths} == before
    assert not pending(app)
    assert Dreamer(app).cursor() == 0
    assert app.db.kv_get(FAILURES) is None  # retrying an attempt is not another failed night


async def test_budget_stop_reviews_destructive_edits_and_counts_one_failed_night(make_app):
    model = ScriptedModel([ModelStep(output=deletes(), usage=usage(100_000, 100))])
    app = make_app(model, config=config(max_cost_usd=0.05))
    paths = notes(app, 8)
    result = await Harness(app).run(dream_batch())
    assert result.status == "failed"
    assert all(app.memory.read_text(p) is not None for p in paths)
    assert app.runs.get(result.run_id).cost_usd > 0
    assert app.db.kv_get(FAILURES) == "1" and Dreamer(app).cursor() == 0
    assert not pending(app)


async def test_even_a_small_incomplete_dream_does_not_publish_partial_edits(make_app):
    app = make_app(ScriptedModel([deletes(1), RuntimeError("later failure")]), config=config())
    paths = notes(app, 8)
    with pytest.raises(RuntimeError):
        await Harness(app).run(dream_batch())
    assert app.memory.read_text(paths[0]) is not None


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_interruption_after_edits_rolls_back_and_keeps_paid_usage(
    make_app, monkeypatch, stop
):
    entered = asyncio.Event()
    deadlines = Deadlines(monkeypatch, runner) if stop == "timeout" else None

    async def wait_forever(call):
        entered.set()
        await asyncio.Event().wait()

    model = ScriptedModel(
        [
            ModelStep(output=deletes(), usage=usage(1_000, 100)),
            ModelStep(responder=wait_forever),
        ]
    )
    cfg = config().model_copy(
        update={"runtime": RuntimeConfig(run_timeout_s=0.05 if stop == "timeout" else 5)}
    )
    app = make_app(model, config=cfg)
    paths = notes(app, 8)
    task = asyncio.create_task(Harness(app).run(dream_batch()))
    await asyncio.wait_for(entered.wait(), 2)
    if deadlines is not None:
        assert deadlines.seconds == [app.config.runtime.run_timeout_s]
        deadlines.expire()
    if stop == "cancel":
        task.cancel()
    with pytest.raises(asyncio.CancelledError if stop == "cancel" else RunTimedOut):
        await task
    assert all(app.memory.read_text(p) is not None for p in paths)
    [run] = app.runs.recent(1)
    assert run.status == ("cancelled" if stop == "cancel" else "failed")
    assert run.requests == 1 and run.cost_usd > 0
    assert not pending(app)


@pytest.mark.parametrize("status", ["running", "failed", "cancelled", "paused_budget"])
def test_restart_restores_incomplete_candidates_once(make_app, status):
    app = make_app(config=config())
    [path] = notes(app, 1)
    before = app.memory.read_text(path)
    run_id = unfinished(app, status)
    app.memory.write_text(path, "unfinished rewrite", actor=Actor.DREAM, run_id=run_id)
    app.close()

    restarted = make_app(config=config())
    assert restarted.recover()["recovered_dreams"] == 1
    assert restarted.memory.read_text(path) == before
    restarted.memory.write_text(path, "a newer owner correction", actor=Actor.USER)
    assert restarted.recover()["recovered_dreams"] == 0
    assert restarted.memory.read_text(path) == "a newer owner correction"


def test_restart_finishes_the_guard_if_process_died_after_success_before_review(make_app):
    app = make_app(config=config())
    paths = notes(app, 8)
    run_id = unfinished(app)
    for path in paths[:6]:
        app.memory.delete(path, actor=Actor.DREAM, run_id=run_id)
    app.runs.finish(run_id, "silent")
    app.close()

    restarted = make_app(config=config())
    assert restarted.recover()["recovered_dreams"] == 1
    assert all(restarted.memory.read_text(p) is not None for p in paths)
    assert Dreamer(restarted).cursor() == 40
    assert len(restarted.outbox.recent()) == 1
    assert restarted.recover()["recovered_dreams"] == 0


def test_recovery_never_revisits_a_historical_success_without_a_receipt(make_app):
    app = make_app(config=config())
    paths = notes(app, 8)
    run_id = app.runs.start(
        session_key=DREAM_KEY, trigger="system", profile="dream", model="old", event_ids=[1]
    )
    for path in paths[:6]:
        app.memory.delete(path, actor=Actor.DREAM, run_id=run_id)
    app.runs.finish(run_id, "silent")
    assert Dreamer(app).recover() == 0
    assert all(app.memory.read_text(p) is None for p in paths[:6])


def test_a_restore_failure_keeps_its_receipt_for_recovery(make_app, monkeypatch):
    app = make_app(config=config())
    run_id = unfinished(app)

    def fail(run_id):
        raise OSError("disk unavailable")

    monkeypatch.setattr(app.memory, "revert_run", fail)
    with pytest.raises(OSError):
        Dreamer(app).abort(run_id)
    assert pending(app)


async def test_a_successful_small_dream_keeps_its_changes_and_clears_receipt(make_app):
    app = make_app(ScriptedModel([deletes(1), [assistant_message("tidied")]]), config=config())
    paths = notes(app, 8)
    result = await Harness(app).run(dream_batch())
    assert result.status == "silent" and app.memory.read_text(paths[0]) is None
    assert not pending(app)
