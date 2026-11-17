"""One process, physically separate space data, one authenticated directory.

No mutable "current space": every request/run keeps its own App. Legacy owner data remains at
the original path. New spaces never read that path's memory, workspace, skills or database.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from personal_agent.channels.base import Channel
from personal_agent.collaboration.models import OWNER_SPACE
from personal_agent.collaboration.scopes import derive_space_config, space_paths

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.daemon import Daemon

log = logging.getLogger(__name__)
SPACE_POLL_S = 0.5


class SpaceManager:
    name = "collaboration"

    def __init__(self, root: App) -> None:
        self.root = root
        self.apps: dict[str, App] = {OWNER_SPACE: root}
        self.daemons: dict[str, Daemon] = {}
        self.running: dict[str, asyncio.Task[None]] = {}
        self._retry_at: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._started_at: dict[str, float] = {}
        self._closed = False
        self.run_slots = asyncio.Semaphore(root.config.runtime.concurrency)

    def get(self, space_id: str) -> App:
        directory = self.root.identities
        assert directory is not None
        space = directory.space(space_id)
        if space.id not in self.apps:
            from personal_agent.app import build_app

            paths = space_paths(self.root.paths, space.id)
            cfg = derive_space_config(self.root.config)
            cfg = cfg.model_copy(
                update={"agent": cfg.agent.model_copy(update={"name": space.name})}
            )
            child = build_app(
                home=paths.home,
                config=cfg,
                clock=self.root.clock,
                read_env=False,
                collaboration=False,
            )
            child.space_id = space.id
            child.space_owner_id = space.owner_id
            child.identities = directory
            child.web_auth = self.root.web_auth
            child.collaboration = self
            child.budget.instance_guard = self.check_instance_budget
            self.apps[space.id] = child
        self.apps[space.id].space_owner_id = space.owner_id
        return self.apps[space.id]

    def all_apps(self) -> list[App]:
        for row in self.root.db.query("SELECT id FROM spaces WHERE archived_at IS NULL"):
            self.get(str(row["id"]))
        return list(self.apps.values())

    def check_instance_budget(self) -> None:
        from personal_agent.core.budget import BudgetExceeded

        spent = sum(app.budget.spent_today() for app in self.all_apps())
        limit = self.root.budget.daily_limit()
        if spent >= limit:
            raise BudgetExceeded("daily", spent, limit, self.root.budget.resets_in())

    def wake(self, space_id: str) -> None:
        daemon = self.daemons.get(space_id)
        if daemon is not None:
            daemon.wake()

    async def run(self, daemon: Daemon) -> None:
        from personal_agent.channels.web.channel import WebChannel
        from personal_agent.daemon import Daemon

        self.daemons[OWNER_SPACE] = daemon
        loop = asyncio.get_running_loop()
        try:
            while not daemon.stopping:
                for app in self.all_apps():
                    if app is self.root:
                        continue
                    task = self.running.get(app.space_id)
                    if task is not None and not task.done():
                        if loop.time() - self._started_at.get(app.space_id, loop.time()) >= 60:
                            self._failures.pop(app.space_id, None)
                        continue
                    if task is not None:
                        try:
                            task.result()
                        except asyncio.CancelledError:
                            log.warning("space service was cancelled; scheduling restart")
                        except Exception:
                            log.exception("space service stopped; scheduling restart")
                        self.running.pop(app.space_id, None)
                        failures = self._failures.get(app.space_id, 0) + 1
                        self._failures[app.space_id] = failures
                        self._retry_at[app.space_id] = loop.time() + min(
                            60, 2 ** min(failures - 1, 6)
                        )
                    if loop.time() < self._retry_at.get(app.space_id, 0):
                        continue
                    # No additional HTTP port or channel credential. External channels are
                    # wrapped by the root router, with an explicit recipient.
                    channels: dict[str, Channel] = {"web": WebChannel(app)}
                    for name, channel in daemon.channels.items():
                        if name not in ("web", "cli"):
                            factory = getattr(channel, "for_space", None)
                            if factory is not None:
                                channels[name] = factory(app)
                    child = Daemon(app, channels, grace_s=daemon.grace_s)
                    self.daemons[app.space_id] = child
                    self._started_at[app.space_id] = loop.time()
                    self.running[app.space_id] = asyncio.create_task(
                        child.run(), name=f"space-{app.space_id}"
                    )
                await asyncio.sleep(SPACE_POLL_S)
        finally:
            for key, child in self.daemons.items():
                if key != OWNER_SPACE:
                    child.stop()
            try:
                if self.running:
                    await asyncio.gather(*self.running.values(), return_exceptions=True)
            finally:
                self.running.clear()

    async def stop_services(self) -> None:
        for app in list(self.apps.values()):
            if app is not self.root:
                await app.stop_services()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for app in list(self.apps.values()):
            if app is not self.root:
                app.db.close()
