"""Test tools that record what actually ran, so tests can prove an action did or did not happen."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from agents import RunContextWrapper, function_tool

from personal_agent.app import App
from personal_agent.core.worker import Worker
from personal_agent.harness.runctx import RunContext
from personal_agent.tools.base import ALL_PROFILES, Category, ToolDef


@dataclass
class Spy:
    calls: list[tuple[str, str]] = field(default_factory=list)

    def ran(self, tool: str | None = None) -> list[str]:
        return [arg for name, arg in self.calls if tool in (None, name)]


def install(app: App, spy: Spy, *, shell_profiles=ALL_PROFILES, delay: float = 0.0) -> Spy:
    """Register `shell` (exec), `fetch` (read, returns outside content), `notes` (write)."""

    @function_tool(name_override="shell", description_override="Run a shell command.")
    async def shell(ctx: RunContextWrapper[RunContext], command: str) -> str:
        spy.calls.append(("shell", command))
        if delay:
            await asyncio.sleep(delay)
        return f"ran: {command}"

    @function_tool(name_override="fetch", description_override="Fetch a web page.")
    async def fetch(ctx: RunContextWrapper[RunContext], url: str) -> str:
        spy.calls.append(("fetch", url))
        return "PAGE CONTENT: " + ("x" * 100)

    @function_tool(name_override="notes", description_override="Write a note.")
    async def notes(ctx: RunContextWrapper[RunContext], text: str) -> str:
        spy.calls.append(("notes", text))
        return "saved"

    @function_tool(name_override="leaky", description_override="Returns text with a secret.")
    async def leaky(ctx: RunContextWrapper[RunContext]) -> str:
        spy.calls.append(("leaky", ""))
        return "key is sk-" + "a" * 30 + " " + "y" * 30_000

    app.tools.register(
        ToolDef(
            name="shell",
            category=Category.EXEC,
            tool=shell,
            profiles=frozenset(shell_profiles),
            detail=lambda a: a.get("command", ""),
        )
    )
    app.tools.register(ToolDef(name="fetch", category=Category.READ, tool=fetch, taints=True))
    app.tools.register(ToolDef(name="notes", category=Category.WRITE, tool=notes))
    app.tools.register(ToolDef(name="leaky", category=Category.READ, tool=leaky))
    return spy


async def drain(app: App, *, limit: int = 20, session_key: str | None = None) -> list:
    """Run the worker until the inbox has nothing ready. Returns the results."""
    worker = Worker(app)
    results = []
    for _ in range(limit):
        result = await worker.run_once(session_key=session_key)
        if result is None:
            return results
        results.append(result)
    raise AssertionError("the inbox never went idle")
