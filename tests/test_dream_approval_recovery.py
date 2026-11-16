"""Dream reviews span approval rounds; rollback always invalidates the old execution snapshot."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message

from deadlines import Deadlines
from helpers import usage
from personal_agent.config import PolicyConfig, RuntimeConfig
from personal_agent.core.events import Kind, NewEvent
from personal_agent.core.worker import Worker
from personal_agent.harness import runner
from personal_agent.harness.dreaming import DREAM_KEY, Dreamer
from personal_agent.harness.runner import Harness
from personal_agent.memory.store import Actor
from test_dreaming import config, dream_batch, notes
from test_memory_recovery import deletes, pending


def cfg(*, timeout=5):
    return config().model_copy(
        update={
            "policy": PolicyConfig(always_ask=["memory"]),
            "runtime": RuntimeConfig(run_timeout_s=timeout),
        }
    )


async def suspend(app):
    app.inbox.add(
        NewEvent(
            source="system",
            kind=Kind.SYSTEM,
            session_key=DREAM_KEY,
            payload={"name": "dream", "text": "Tidy the notes.", "to_id": 40},
        )
    )
    result = await Worker(app).run_once()
    assert result.outcome.status == "awaiting_approval"
    return result.outcome.run_id


def approve_all(app):
    for approval in app.approvals.pending():
        app.approvals.decide(approval.id, True, by="owner")


async def test_waiting_dream_keeps_its_candidate_and_receipt_across_restart(make_app):
    model = ScriptedModel([deletes(1), [assistant_message("tidied")]])
    app = make_app(model, config=cfg())
    paths = notes(app, 8)
    run_id = await suspend(app)
    app.memory.write_text(paths[-1], "candidate before the pause", actor=Actor.DREAM, run_id=run_id)
    assert pending(app) and Dreamer(app).cursor() == 0
    app.close()

    restarted = make_app(model, config=cfg())
    assert Dreamer(restarted).recover() == 0
    assert restarted.memory.read_text(paths[-1]) == "candidate before the pause"
    approve_all(restarted)
    result = await Worker(restarted).run_once()
    assert result.outcome.status == "silent"
    assert not pending(restarted) and Dreamer(restarted).cursor() == 40
    assert restarted.memory.read_text(paths[0]) is None


async def test_mass_delete_guard_also_reviews_success_after_approval(make_app):
    app = make_app(ScriptedModel([deletes(), [assistant_message("tidied")]]), config=cfg())
    paths = notes(app, 8)
    await suspend(app)
    approve_all(app)
    await Worker(app).run_once()
    assert all(app.memory.read_text(p) is not None for p in paths)
    assert not pending(app) and Dreamer(app).cursor() == 40
    assert any("undid everything" in m.text for m in app.outbox.recent())


async def test_budget_stop_after_approved_edits_also_rolls_back_and_closes_snapshot(make_app):
    model = ScriptedModel([ModelStep(output=deletes(), usage=usage(100_000, 100))])
    dream_cfg = cfg().model_copy(update={"dreaming": config(max_cost_usd=0.05).dreaming})
    app = make_app(model, config=dream_cfg)
    paths = notes(app, 8)
    run_id = await suspend(app)
    approve_all(app)
    result = await Worker(app).run_once()
    assert result.outcome.status == "failed"
    assert all(app.memory.read_text(p) is not None for p in paths)
    assert app.approvals.load_run(run_id) is None and not pending(app)
    assert len(model.calls) == 1
    assert any("saved execution was closed" in m.text for m in app.outbox.recent())


@pytest.mark.parametrize("stop", ["error", "cancel", "timeout"])
async def test_failed_resume_rolls_back_drops_snapshot_and_never_replays(
    make_app, monkeypatch, stop
):
    entered = asyncio.Event()
    deadlines = Deadlines(monkeypatch, runner) if stop == "timeout" else None

    async def wait_forever(call):
        entered.set()
        await asyncio.Event().wait()

    model = ScriptedModel(
        [
            deletes(),
            RuntimeError("provider failed")
            if stop == "error"
            else ModelStep(responder=wait_forever),
            [assistant_message("Old snapshot must not resume again.")],
        ]
    )
    app = make_app(model, config=cfg(timeout=0.05 if stop == "timeout" else 5))
    paths = notes(app, 8)
    run_id = await suspend(app)
    approve_all(app)
    task = asyncio.create_task(Worker(app).run_once())
    if stop != "error":
        await asyncio.wait_for(entered.wait(), 2)
    if deadlines is not None:
        assert deadlines.seconds[-1] == app.config.runtime.run_timeout_s
        deadlines.expire()
    if stop == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert all(app.memory.read_text(p) is not None for p in paths)
    assert app.approvals.load_run(run_id) is None and not pending(app)
    assert app.runs.get(run_id).status == ("cancelled" if stop == "cancel" else "failed")
    assert any("saved execution was closed" in m.text for m in app.outbox.recent())
    count = len(model.calls)
    old_event = replace(
        dream_batch().events[0], kind=Kind.APPROVAL_RESPONSE, payload={"run_id": run_id}
    )
    await Harness(app)._resume(old_event)
    assert len(model.calls) == count
    assert all(app.memory.read_text(p) is not None for p in paths)


async def test_second_approval_round_keeps_review_open_until_final_completion(make_app):
    second_delete = deletes(2)[1:]
    app = make_app(
        ScriptedModel([deletes(1), second_delete, [assistant_message("done")]]), config=cfg()
    )
    paths = notes(app, 8)
    run_id = await suspend(app)
    approve_all(app)
    result = await Worker(app).run_once()
    assert result.outcome.status == "awaiting_approval"
    assert pending(app) and Dreamer(app).cursor() == 0
    assert app.approvals.load_run(run_id) is not None
    assert app.memory.read_text(paths[0]) is None
    approve_all(app)
    await Worker(app).run_once()
    assert not pending(app) and Dreamer(app).cursor() == 40
    assert all(app.memory.read_text(p) is None for p in paths[:2])


async def test_restart_recovers_between_resume_terminal_commit_and_rollback(make_app):
    app = make_app(ScriptedModel([deletes(1)]), config=cfg())
    [path] = notes(app, 1)
    run_id = await suspend(app)
    app.memory.delete(path, actor=Actor.DREAM, run_id=run_id)
    with app.db.tx():
        app.runs.finish(run_id, "failed", error="resume failed")
        app.approvals.drop_run(run_id)
    app.close()

    restarted = make_app(config=cfg())
    assert Dreamer(restarted).recover() == 1
    assert restarted.memory.read_text(path) is not None
    notices = [m for m in restarted.outbox.recent() if "saved execution was closed" in m.text]
    assert len(notices) == 1 and not pending(restarted)
    assert Dreamer(restarted).recover() == 0
