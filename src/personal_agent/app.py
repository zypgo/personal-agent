"""The application container: every long-lived component, wired together once."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path

from personal_agent.clock import Clock, SystemClock, get_tz
from personal_agent.config import Config, load_config, load_env
from personal_agent.core.inbox import Inbox
from personal_agent.core.outbox import Outbox
from personal_agent.core.runs import RunStore
from personal_agent.core.sessions import SessionStore
from personal_agent.db import Database, open_database
from personal_agent.harness.models import ModelFactory, OpenAIModels
from personal_agent.memory.store import MemoryStore
from personal_agent.memory.templates import bootstrap
from personal_agent.paths import Paths
from personal_agent.tools.base import ToolRegistry
from personal_agent.tools.builtin import register_builtin


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

    def recover(self) -> dict[str, int]:
        """Clean up after a previous process that died: orphaned runs and half-sent messages.

        Only call this from the single process that owns the agent (the daemon, or a lone CLI)."""
        orphaned = self.runs.recover_orphans()
        stuck = self.outbox.recover_stuck()
        return {"orphaned_runs": orphaned, **{f"outbox_{k}": v for k, v in stuck.items()}}

    def close(self) -> None:
        self.db.close()


def build_app(
    *,
    home: str | Path | None = None,
    config: Config | None = None,
    clock: Clock | None = None,
    model_factory: ModelFactory | None = None,
    db: Database | None = None,
    read_env: bool = True,
    create_templates: bool = True,
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
    register_builtin(tools)
    return App(
        paths=paths,
        config=cfg,
        db=database,
        clock=clock,
        tz=get_tz(cfg.agent.timezone),
        inbox=Inbox(database, clock),
        sessions=SessionStore(database, clock),
        outbox=Outbox(database, clock),
        runs=RunStore(database, clock),
        memory=memory,
        tools=tools,
        model_factory=model_factory or OpenAIModels(cfg.model),
    )
