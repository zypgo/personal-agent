"""`pa init` and `pa doctor`: make a home for the agent, and check that what it needs works."""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING

import typer
from rich.markup import escape

from personal_agent.commands.common import console, opened, say
from personal_agent.config import DEFAULT_CONFIG_TOML, DEFAULT_ENV, ConfigError, load_env
from personal_agent.doctor import Check, capability_checks, live_checks, static_checks
from personal_agent.paths import Paths
from personal_agent.skills.install import install_bundled

if TYPE_CHECKING:
    from personal_agent.app import App

app = typer.Typer()

MARKS = {"ok": "[green]ok[/green]  ", "warn": "[yellow]warn[/yellow]", "fail": "[red]FAIL[/red]"}


@app.command()
def init() -> None:
    """Create the agent's home (~/.personal-agent), config and database. Safe to run again."""
    paths = Paths.resolve()
    paths.ensure()
    created: list[str] = []
    if not paths.config_file.exists():
        paths.config_file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        created.append(str(paths.config_file))
    if not paths.env_file.exists():
        fd = os.open(paths.env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:  # private before the first byte
            handle.write(DEFAULT_ENV)
        created.append(str(paths.env_file))
    with opened():  # creates the database and the memory templates, and loads .env
        pass
    tightened = paths.tighten()  # files from an earlier version, or made under another umask
    starter = install_bundled(paths)
    say(f"[green]Ready.[/green] Home: {escape(str(paths.home))}")
    for item in created:
        say(f"  created {escape(item)}")
    for path in tightened:
        say(f"  made {escape(path.name)} private: other users could read it")
    if starter:
        say(f"  added starter skills: {', '.join(starter)} (see `pa skills`)")
    if not os.environ.get("OPENAI_API_KEY"):
        say(
            f"\nNext: put your key in [bold]{escape(str(paths.env_file))}[/bold] "
            "(OPENAI_API_KEY=...), then run [bold]pa doctor[/bold]."
        )
    else:
        say("\nNext: run [bold]pa doctor[/bold], then [bold]pa chat[/bold].")


async def _live(agent: App) -> list[Check]:
    """The live check, and then everything it opened closed, before the event loop ends."""
    try:
        return await live_checks(agent)
    finally:
        await agent.stop_services()


@app.command()
def doctor(
    live: bool = typer.Option(
        False, "--live", help="Also make two real model calls (a few cents)."
    ),
) -> None:
    """Check the setup and explain how to fix what is wrong."""
    paths = Paths.resolve()
    with contextlib.suppress(ConfigError):  # a .env that cannot be read is reported below
        load_env(paths)
    checks = static_checks(paths)
    checks += capability_checks(paths)  # says nothing if the config is broken (already reported)
    skipped_live = live and any(c.status == "fail" for c in checks)
    if live and not skipped_live:
        with opened() as agent, console.status("[dim]calling the model...[/dim]"):
            checks += asyncio.run(_live(agent))
    for check in checks:
        say(f"{MARKS[check.status]} {check.name}: {escape(check.detail)}")
        if check.fix and check.status != "ok":
            say(f"       [dim]-> {escape(check.fix)}[/dim]")
    if skipped_live:
        say("[dim]Skipped the live check: fix the failures above first.[/dim]")
    if any(c.status == "fail" for c in checks):
        raise typer.Exit(1)
