"""The application container: every long-lived component, wired together once."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import tzinfo
from pathlib import Path
from typing import Any

from personal_agent.channels.base import PULL_ONLY
from personal_agent.channels.pairing import PairingStore
from personal_agent.channels.transcribe import OpenAITranscriber, Transcriber
from personal_agent.channels.web.auth import WebAuth
from personal_agent.clock import Clock, SystemClock, get_tz
from personal_agent.config import Config, load_config, load_env
from personal_agent.core.approvals import Approvals
from personal_agent.core.budget import BudgetGuard
from personal_agent.core.inbox import Inbox
from personal_agent.core.live import LiveBus
from personal_agent.core.media import MediaStore
from personal_agent.core.outbox import Outbox
from personal_agent.core.runs import RunStore
from personal_agent.core.sessions import SessionStore
from personal_agent.core.timers import Timers
from personal_agent.core.transcript import Transcript
from personal_agent.db import Database, open_database
from personal_agent.harness.compaction import LLMSummarizer, Summarizer
from personal_agent.harness.models import ModelFactory, OpenAIModels
from personal_agent.memory.store import MemoryStore
from personal_agent.memory.templates import bootstrap
from personal_agent.paths import Paths
from personal_agent.plugins.mcp import McpHub, ServerFactory
from personal_agent.plugins.registry import ChannelSpec, PluginInfo, load_plugins
from personal_agent.security.audit import Audit
from personal_agent.security.broker import SecretBroker
from personal_agent.security.guardian import Guardian, LLMGuardian
from personal_agent.security.policy import Policy
from personal_agent.security.redact import register_secret_envs
from personal_agent.security.sandbox import SandboxProvider
from personal_agent.security.ssrf import NetPolicy
from personal_agent.skills.store import SkillStore
from personal_agent.tools.base import ToolRegistry
from personal_agent.tools.builtin import register_builtin

log = logging.getLogger(__name__)


@dataclass
class App:
    paths: Paths
    config: Config
    db: Database
    clock: Clock
    tz: tzinfo
    inbox: Inbox
    sessions: SessionStore
    outbox: Outbox
    runs: RunStore
    memory: MemoryStore
    tools: ToolRegistry
    model_factory: ModelFactory
    timers: Timers
    approvals: Approvals
    audit: Audit
    policy: Policy
    budget: BudgetGuard
    guardian: Guardian
    summarizer: Summarizer
    sandbox: SandboxProvider
    net: NetPolicy
    secrets: SecretBroker
    skills: SkillStore
    mcp: McpHub
    pairing: PairingStore
    transcript: Transcript
    live: LiveBus
    media: MediaStore
    transcriber: Transcriber
    web_auth: WebAuth
    plugins: list[PluginInfo] = field(default_factory=list)
    plugin_channels: dict[str, ChannelSpec] = field(default_factory=dict)

    def push_channels(self) -> set[str]:
        """Chat channels that are switched on and can reach the owner without them looking: a
        message there pings the phone."""
        names = {name for name, spec in self.plugin_channels.items() if spec.push}
        if self.config.telegram.enabled:
            names.add("telegram")
        return names - PULL_ONLY

    def notify_channel(self) -> str:
        """Where unprompted messages go.

        `auto`: the chat app that can ping the owner and that they last wrote from. If they never
        used one, the web page when it is on (they see the message when they open it), else the
        terminal. A channel that is not switched on is never picked: nothing would deliver there."""
        configured = self.config.notify.channel
        if configured != "auto":
            return configured
        found = self.transcript.last_in_channel(among=self.push_channels())
        if found:
            return found
        return "web" if self.config.web.enabled else "cli"

    def recover(self) -> dict[str, int]:
        """Clean up after a previous process that died: orphaned runs and half-sent messages.

        Only call this from the single process that owns the agent (the daemon, or a lone CLI)."""
        orphaned = self.runs.recover_orphans()
        stuck = self.outbox.recover_stuck()
        interrupted = self.audit.mark_interrupted()
        return {
            "orphaned_runs": orphaned,
            "interrupted_tool_calls": interrupted,
            **{f"outbox_{k}": v for k, v in stuck.items()},
        }

    async def warm_up(self) -> None:
        """Do the slow, once-only setup now so it never blocks a conversation later (finding out
        whether a sandbox works means starting a process or asking Docker)."""
        if self.config.tools.shell:
            await asyncio.to_thread(self.sandbox.resolve)

    async def stop_services(self) -> None:
        """Close what was opened for the running event loop (MCP connections and the programs
        behind them). Every place that runs a loop to its end calls this first."""
        try:
            await self.mcp.stop()
        except Exception:
            log.warning("could not stop the MCP servers cleanly", exc_info=True)

    def extend_budget(self, amount: float) -> tuple[float, int]:
        """Raise today's spending cap and let work that was paused for the budget continue.

        Returns today's new limit and how many waiting events were released."""
        limit = self.budget.extend_today(amount)
        return limit, self.inbox.expedite("%budget reached%")

    def close(self) -> None:
        self.db.close()


def build_app(
    *,
    home: str | Path | None = None,
    config: Config | None = None,
    clock: Clock | None = None,
    model_factory: ModelFactory | None = None,
    db: Database | None = None,
    sandbox: SandboxProvider | None = None,
    read_env: bool = True,
    create_templates: bool = True,
    mcp_factory: ServerFactory | None = None,
    plugin_entry_points: Iterable[Any] | None = None,
    transcriber: Transcriber | None = None,
) -> App:
    paths = Paths.resolve(home)
    paths.ensure()
    if read_env:
        load_env(paths)
    cfg = config or load_config(paths)
    clock = clock or SystemClock()
    database = db or open_database(paths.db_file)
    memory = MemoryStore(paths, database, cfg.memory, clock)
    if create_templates:
        bootstrap(memory)
    tools = ToolRegistry()
    register_builtin(tools, cfg)
    plugin_channels: dict[str, ChannelSpec] = {}
    plugins = load_plugins(
        cfg.plugins,
        registry=tools,
        paths=paths,
        entry_points=plugin_entry_points,
        channels=plugin_channels,
    )
    register_secret_envs(secret.env for secret in cfg.secrets.values())
    register_secret_envs(route.secret_env for route in cfg.webhooks.routes.values())
    tz = get_tz(cfg.agent.timezone)
    inbox = Inbox(database, clock)
    runs = RunStore(database, clock)
    factory = model_factory or OpenAIModels(cfg.model)
    live = LiveBus()
    return App(
        paths=paths,
        config=cfg,
        db=database,
        clock=clock,
        tz=tz,
        inbox=inbox,
        sessions=SessionStore(database, clock),
        outbox=Outbox(database, clock),
        runs=runs,
        memory=memory,
        tools=tools,
        model_factory=factory,
        timers=Timers(
            database,
            inbox,
            cfg.scheduler,
            clock=clock,
            default_tz=getattr(tz, "key", None) or "UTC",
        ),
        approvals=Approvals(
            database, inbox, clock=clock, timeout_hours=cfg.policy.approval_timeout_hours
        ),
        audit=Audit(database, clock),
        policy=Policy(cfg.policy),
        budget=BudgetGuard(runs, database, cfg.budget, clock=clock, tz=tz),
        guardian=LLMGuardian(factory, cfg.model),
        summarizer=LLMSummarizer(factory, cfg.model, cfg.context),
        sandbox=sandbox or SandboxProvider(paths, cfg.sandbox),
        net=NetPolicy.from_config(cfg.net),
        secrets=SecretBroker(cfg.secrets, os.environ),
        skills=SkillStore(paths, cfg.skills),
        mcp=McpHub(
            cfg.mcp,
            paths=paths,
            db=database,
            clock=clock,
            registry=tools,
            runtime=cfg.runtime,
            factory=mcp_factory,
        ),
        pairing=PairingStore(database, clock),
        transcript=Transcript(database, clock, live),
        live=live,
        media=MediaStore(paths.media_dir),
        transcriber=transcriber or OpenAITranscriber(cfg.model, cfg.media),
        web_auth=WebAuth(
            database,
            clock,
            session_days=cfg.web.session_days,
            invite_minutes=cfg.web.invite_minutes,
        ),
        plugins=plugins,
        plugin_channels=plugin_channels,
    )
