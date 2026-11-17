"""Real daemon/worker lifecycles across isolated spaces, using only scripted providers."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from agents.testing import ScriptedModel, assistant_message

from helpers import owner_message
from personal_agent.channels.ingress import Ingress, Upload
from personal_agent.collaboration import runtime as runtime_module
from personal_agent.collaboration.models import AccessDenied
from personal_agent.config import BudgetConfig, Config, RuntimeConfig, WebConfig
from personal_agent.core.budget import BudgetExceeded
from personal_agent.daemon import Daemon
from personal_agent.presence import LOCK_NAME, EngineLock
from test_daemon import eventually
from test_ingress import FakeTranscriber


class ClosableModels:
    def __init__(self, model):
        self.model = model
        self.closed = 0

    def __call__(self, name):
        return self.model

    async def aclose(self):
        self.closed += 1


class BlockingModel(ScriptedModel):
    def __init__(self):
        super().__init__([[assistant_message("first")], [assistant_message("second")]])
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = self.peak = self.started_calls = 0

    async def get_response(self, *args, **kwargs):
        self.started_calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.entered.set()
        try:
            await self.release.wait()
            return await super().get_response(*args, **kwargs)
        finally:
            self.active -= 1


def config():
    return Config(
        runtime=RuntimeConfig(concurrency=1, poll_s=0.05),
        web=WebConfig(stream_responses=False),
        budget=BudgetConfig(daily_usd=1),
    )


def project(root, name="Project"):
    space = root.identities.create_project("owner", name)
    return root.collaboration.get(space.id)


def submit(app, text="hello"):
    return app.inbox.add(
        owner_message(text, source="web", principal_id="owner", **app.actor_snapshot("owner"))
    )


async def start(root):
    daemon = Daemon(root, {}, grace_s=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.wait_ready(), 2)
    return daemon, task


async def finish(daemon, task):
    daemon.stop()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


async def test_child_daemon_recovers_a_dead_lease_once_and_closes_with_root(make_app, monkeypatch):
    monkeypatch.setattr(runtime_module, "SPACE_POLL_S", 0.01)
    root = make_app(config=config())
    child = project(root)
    models = ClosableModels(ScriptedModel([[assistant_message("Recovered once.")]]))
    child.model_factory = models
    submit(child)
    assert child.inbox.claim("dead-process") is not None
    assert child.inbox.counts()["leased"] == 1
    daemon, task = await start(root)
    try:
        await eventually(lambda: child.outbox.counts()["sent"] == 1)
        assert child.inbox.counts()["done"] == 1
        assert len(child.runs.recent()) == 1
        assert root.inbox.counts()["done"] == 0
    finally:
        await finish(daemon, task)
    assert models.closed >= 1
    assert not root.collaboration.running
    assert not EngineLock.is_taken(child.paths.run_dir / LOCK_NAME)


async def test_cancelled_child_releases_slot_and_supervisor_restarts_without_duplicate_workers(
    make_app, monkeypatch
):
    monkeypatch.setattr(runtime_module, "SPACE_POLL_S", 0.01)
    root = make_app(config=config())
    first = project(root, "First")
    blocking = BlockingModel()
    first.model_factory = ClosableModels(blocking)
    original_event = submit(first)
    daemon, task = await start(root)
    try:
        await asyncio.wait_for(blocking.entered.wait(), 2)
        second = project(root, "Second")
        second.model_factory = ClosableModels(ScriptedModel([[assistant_message("Other space.")]]))
        submit(second)
        await eventually(lambda: second.inbox.counts()["leased"] == 1)
        assert second.inbox.counts()["done"] == 0  # the shared run slot is occupied

        old = root.collaboration.running[first.space_id]
        old.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(old, 2)
        assert blocking.active == 0
        assert first.inbox.get(original_event.id)["status"] == "pending"
        assert first.inbox.get(original_event.id)["attempts"] == 0
        first.model_factory = ClosableModels(
            ScriptedModel([[assistant_message("Recovered after cancellation.")]])
        )
        await eventually(lambda: second.outbox.counts()["sent"] == 1)
        await eventually(lambda: first.outbox.counts()["sent"] == 1, within=4)
        assert root.collaboration.running[first.space_id] is not old
        assert first.inbox.counts()["done"] == second.inbox.counts()["done"] == 1
        assert len([run for run in first.runs.recent() if run.status == "completed"]) == 1
    finally:
        await finish(daemon, task)
    assert not root.collaboration.running
    async with asyncio.timeout(0.2):
        async with root.collaboration.run_slots:
            pass


async def test_cancelling_root_task_drains_children_and_releases_every_lock(make_app, monkeypatch):
    monkeypatch.setattr(runtime_module, "SPACE_POLL_S", 0.01)
    root = make_app(config=config())
    child = project(root)
    model = BlockingModel()
    child.model_factory = ClosableModels(model)
    added = submit(child)
    before = set(asyncio.all_tasks())
    daemon, task = await start(root)
    try:
        await asyncio.wait_for(model.entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert model.active == 0
        assert child.inbox.get(added.id)["status"] == "pending"
        assert child.inbox.get(added.id)["attempts"] == 0
        assert not root.collaboration.running
        assert not EngineLock.is_taken(root.paths.run_dir / LOCK_NAME)
        assert not EngineLock.is_taken(child.paths.run_dir / LOCK_NAME)
        assert not (set(asyncio.all_tasks()) - before)
    finally:
        await finish(daemon, task)


async def test_two_spaces_share_one_model_slot(make_app, monkeypatch):
    monkeypatch.setattr(runtime_module, "SPACE_POLL_S", 0.01)
    root = make_app(config=config())
    first, second = project(root, "First"), project(root, "Second")
    model = BlockingModel()
    first.model_factory = second.model_factory = ClosableModels(model)
    submit(first)
    submit(second)
    daemon, task = await start(root)
    try:
        await asyncio.wait_for(model.entered.wait(), 2)
        await eventually(
            lambda: first.inbox.counts()["leased"] + second.inbox.counts()["leased"] == 2
        )
        assert model.started_calls == 1
        model.release.set()
        await eventually(
            lambda: first.outbox.counts()["sent"] + second.outbox.counts()["sent"] == 2
        )
        assert model.peak == 1 and model.started_calls == 2
    finally:
        await finish(daemon, task)


async def test_root_closes_child_clients_even_before_the_child_daemon_exists(make_app):
    root = make_app(config=config())
    child = project(root)
    models = ClosableModels(ScriptedModel([]))
    child.model_factory = models
    assert not root.collaboration.running
    await root.stop_services()
    assert models.closed == 1


async def test_instance_budget_blocks_another_spaces_voice_request_before_billing(make_app):
    root = make_app(config=config())
    child = project(root)
    root.budget.record_spend("test", 0.7)
    child.budget.record_spend("test", 0.3)
    with pytest.raises(BudgetExceeded):
        child.budget.check_day()
    transcriber = FakeTranscriber("hello")
    child.transcriber = transcriber
    result = await Ingress(child).owner_message(
        channel="web",
        voice=Upload(b"audio", "audio/wav", duration_s=1),
        external_id="voice-budget",
    )
    assert result.status == "rejected" and "spending limit" in result.reply
    assert not transcriber.calls
    async with asyncio.timeout(0.2):
        async with root.collaboration.run_slots:
            pass


async def test_voice_waiting_for_capacity_rechecks_membership_before_paid_call(make_app):
    root = make_app(config=config())
    child = project(root)
    invite = root.identities.create_member_invite("owner", child.space_id, "member")
    enrollment = root.identities.redeem_member(invite.code, label="Member")
    transcriber = FakeTranscriber("hello")
    child.transcriber = transcriber
    slots = root.collaboration.run_slots
    await slots.acquire()
    task = asyncio.create_task(
        Ingress(child).owner_message(
            channel="web",
            principal_id=enrollment.principal.id,
            voice=Upload(b"audio", "audio/wav", duration_s=1),
            external_id="voice-auth",
        )
    )
    try:
        await eventually(lambda: child.db.scalar("SELECT COUNT(*) FROM ingress_receipts") == 1)
        root.identities.revoke_member("owner", child.space_id, enrollment.principal.id)
    finally:
        slots.release()
    with pytest.raises(AccessDenied):
        await asyncio.wait_for(task, 2)
    assert not transcriber.calls
    assert child.db.scalar("SELECT COUNT(*) FROM events") == 0
