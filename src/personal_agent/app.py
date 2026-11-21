"""The application container: every long-lived component, wired together once."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from personal_agent.channels.base import PULL_ONLY
from personal_agent.channels.pairing import PairingStore
from personal_agent.channels.transcribe import OpenAITranscriber, Transcriber
from personal_agent.channels.web.auth import WebAuth
from personal_agent.clock import Clock, SystemClock, get_tz
from personal_agent.config import Config, load_config, load_env
from personal_agent.core.approvals import Approvals
from personal_agent.core.artifacts import ArtifactStore
from personal_agent.core.budget import BudgetGuard
from personal_agent.core.inbox import Inbox
from personal_agent.core.live import LiveBus
from personal_agent.core.media import MediaStore
from personal_agent.core.outbox import Outbox
from personal_agent.core.receipts import ReceiptStore
from personal_agent.core.runs import RunStore
from personal_agent.core.sessions import SessionStore
from personal_agent.core.timers import Timers
from personal_agent.core.transcript import Transcript
from personal_agent.db import Database, open_database
from personal_agent.harness.compaction import LLMSummarizer, Summarizer
from personal_agent.harness.models import ModelFactory, OpenAIModels
from personal_agent.maintenance import Janitor
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

if TYPE_CHECKING:
    from personal_agent.collaboration.runtime import SpaceManager
    from personal_agent.collaboration.store import IdentityStore
    from personal_agent.execution.store import ExecutionStore
    from personal_agent.nodes.registry import NodeRegistry
    from personal_agent.orchestration.store import OrchestrationStore
    from personal_agent.orchestration.threads import ThreadKeeper
    from personal_agent.vault.store import Vault

log = logging.getLogger(__name__)


@runtime_checkable
class ClosesConnections(Protocol):
    """Something that keeps connections open for the event loop it was used in."""

    async def aclose(self) -> None: ...


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
    artifacts: ArtifactStore
    transcriber: Transcriber
    web_auth: WebAuth
    janitor: Janitor
    plugins: list[PluginInfo] = field(default_factory=list)
    plugin_channels: dict[str, ChannelSpec] = field(default_factory=dict)

    space_id: str = "personal-owner"
    space_owner_id: str = "owner"
    identities: IdentityStore | None = None
    collaboration: SpaceManager | None = None
    jobs: ExecutionStore | None = None
    nodes: NodeRegistry | None = None
    orchestration: OrchestrationStore | None = None
    threads: ThreadKeeper | None = None
    vault: Vault | None = None

    def require_actor(
        self,
        principal_id: str,
        *,
        write: bool = True,
        version: int | None = None,
        principal_version: int | None = None,
    ) -> None:
        if self.identities is not None:
            from personal_agent.collaboration.models import AccessDenied

            if not self.identities.authorized(
                principal_id,
                self.space_id,
                write=write,
                version=version,
                principal_version=principal_version,
            ):
                raise AccessDenied("Authorization changed; request a new action.")

    def require_approval_actor(self, principal_id: str) -> None:
        if self.identities is not None:
            self.identities.require_access(principal_id, self.space_id, manage=True)

    def actor_snapshot(self, principal_id: str, *, write: bool = True) -> dict[str, int]:
        if self.identities is None:
            return {}
        member = self.identities.require_access(principal_id, self.space_id, write=write)
        principal = self.identities.principal(principal_id)
        return {"membership_version": member.version, "principal_version": principal.authz_version}

    def push_channels(self) -> set[str]:
        """Chat channels that are switched on and can reach the owner without them looking: a
        message there pings the phone."""
        names = {name for name, spec in self.plugin_channels.items() if spec.push}
        if self.config.telegram.enabled:
            names.add("telegram")
        if self.config.dingtalk.enabled:
            names.add("dingtalk")
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
        """Clean up after a previous process that died: orphaned runs, half-sent messages and the
        events its workers had claimed.

        Only call this from the single process that owns the agent (the daemon, or a lone CLI).
        Because nobody else is working, everything it finds in flight belongs to the dead process
        and is dealt with now, not after a timeout: a message sent just before the crash is
        retried or parked, and the owner's message is picked up again within seconds."""
        from personal_agent.harness.dreaming import Dreamer

        # Finish committed file projections before reviewing or undoing a memory pass.
        self.memory.recover()
        reclaimed_artifacts = self.artifacts.sweep()
        orphaned = self.runs.recover_orphans()
        dreams = Dreamer(self).recover()
        receipts = ReceiptStore(self.db, self.clock).recover()
        stuck = self.outbox.recover_stuck(older_than_s=0)
        interrupted = self.audit.mark_interrupted()
        released = self.inbox.release_leases()
        return {
            "orphaned_runs": orphaned,
            "reclaimed_artifacts": reclaimed_artifacts,
            "recovered_dreams": dreams,
            "interrupted_transcriptions": receipts,
            "interrupted_tool_calls": interrupted,
            "released_events": released,
            **{f"outbox_{k}": v for k, v in stuck.items()},
        }

    async def warm_up(self) -> None:
        """Do the slow, once-only setup now so it never blocks a conversation later (finding out
        whether a sandbox works means starting a process or asking Docker)."""
        if self.config.tools.shell:
            await asyncio.to_thread(self.sandbox.resolve)

    async def stop_services(self) -> None:
        """Close what was opened for the running event loop: the MCP connections and the programs
        behind them, and the connections to the model and speech services. Every place that runs
        a loop to its end calls this first; calling it again does no harm."""
        if self.collaboration is not None and self.collaboration.root is self:
            # An HTTP request may have opened a child App before its daemon's first poll.
            # Close those clients too, even when no child daemon was ever started for them.
            await self.collaboration.stop_services()
        try:
            await self.mcp.stop()
        except Exception:
            log.warning("could not stop the MCP servers cleanly", exc_info=True)
        holders: tuple[object, ...] = (self.model_factory, self.transcriber)
        for held in holders:
            if isinstance(held, ClosesConnections):
                try:
                    await held.aclose()
                except Exception:
                    log.warning("could not close %s cleanly", type(held).__name__, exc_info=True)

    def extend_budget(self, amount: float) -> tuple[float, int]:
        """Raise today's spending cap and let work that was paused for the budget continue.

        Returns today's new limit and how many waiting events were released."""
        limit = self.budget.extend_today(amount)
        return limit, self.inbox.expedite("%budget reached%")

    def close(self) -> None:
        if self.collaboration is not None and self.collaboration.root is self:
            self.collaboration.close()
        self.db.close()

    def __enter__(self) -> App:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


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
    collaboration: bool = True,
    paths: Paths | None = None,
) -> App:
    if paths is not None and home is not None:
        raise ValueError("Choose either home or explicit paths, not both.")
    paths = paths or Paths.resolve(home)
    paths.ensure()
    if read_env:
        load_env(paths)
    cfg = config or load_config(paths)
    if os.environ.get("PA_CONTROL_ONLY") == "1":
        cfg = cfg.model_copy(update={"tools": cfg.tools.model_copy(update={"shell": False})})
    register_secret_envs(
        [cfg.model.api_key_env, cfg.media.transcribe_api_key_env or cfg.model.api_key_env]
    )
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
    register_secret_envs((cfg.dingtalk.client_id_env, cfg.dingtalk.client_secret_env))
    tz = get_tz(cfg.agent.timezone)
    inbox = Inbox(database, clock)
    runs = RunStore(database, clock)
    factory = model_factory or OpenAIModels(cfg.model)
    live = LiveBus()
    app = App(
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
        artifacts=ArtifactStore(
            database, paths.artifacts_dir, paths.workspace_dir, cfg.artifacts, clock
        ),
        transcriber=transcriber or OpenAITranscriber(cfg.model, cfg.media),
        web_auth=WebAuth(
            database,
            clock,
            session_days=cfg.web.session_days,
            invite_minutes=cfg.web.invite_minutes,
        ),
        janitor=Janitor(database, cfg.retention, clock),
        plugins=plugins,
        plugin_channels=plugin_channels,
    )

    from personal_agent.collaboration.store import IdentityStore
    from personal_agent.execution.store import ExecutionStore
    from personal_agent.nodes.registry import NodeRegistry
    from personal_agent.orchestration.store import OrchestrationStore

    app.identities = IdentityStore(database, clock)
    app.jobs = ExecutionStore(
        database,
        clock,
        inbox=app.inbox,
        outbox=app.outbox,
        artifacts=app.artifacts,
        workspace=app.paths.workspace_dir,
    )
    app.jobs.authorization_snapshot = app.actor_snapshot
    app.nodes = NodeRegistry(database, clock)
    app.orchestration = OrchestrationStore(
        database,
        clock,
        app.actor_snapshot,
        read_authorization_snapshot=lambda actor: app.actor_snapshot(actor, write=False),
        space_id=app.space_id,
        space_owner_id=app.space_owner_id,
    )
    if cfg.vault.enabled:
        from personal_agent.vault.store import Vault

        app.vault = Vault(
            Path(cfg.vault.path) if cfg.vault.path else paths.vault_dir,
            database,
            cfg.vault,
            clock,
            lock_path=paths.run_dir / "vault.lock",
            tz=tz,
        )
        from personal_agent.vault.legacy import bring_over

        try:
            bring_over(database, app.vault, clock)
        except Exception:
            log.warning("could not bring the old documents into the knowledge base", exc_info=True)
    from personal_agent.orchestration.threads import ThreadKeeper

    app.threads = ThreadKeeper(app)
    if collaboration:
        from personal_agent.collaboration.runtime import SpaceManager

        app.collaboration = SpaceManager(app)
        app.budget.instance_guard = app.collaboration.check_instance_budget
    return app
