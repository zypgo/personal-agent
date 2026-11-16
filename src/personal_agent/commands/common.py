"""What every `pa` command shares: the terminal, the agent's home opened around one command, and the
few ways a command says something or ends.

Command modules import only light modules at the top. What needs the model SDK, which takes most
of a second to load, is imported inside the command that uses it, so `pa --help` and shell
completion answer at once. `tests/test_cli_startup.py` keeps it that way.

Exit codes: 0 it worked, 1 it did not, 2 it was asked wrongly or the agent is not set up (a bad
argument, a broken config, no home yet).
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from personal_agent.config import ConfigError, load_env
from personal_agent.db import DatabaseTooNew
from personal_agent.logs import setup_logging
from personal_agent.paths import Paths

if TYPE_CHECKING:
    from personal_agent.app import App

FAILED = 1
USAGE = 2

console = Console()


def say(text: str = "") -> None:
    """Print a line of Rich markup. It is never wrapped, so a path or an id is never broken in two
    (the terminal wraps it), and never coloured on its own: Rich would colour any number or quoted
    word it happened to see."""
    console.print(text, highlight=False, soft_wrap=True)


def echo(text: str) -> None:
    """Print `text` exactly as it is: whatever in it looks like markup is only text."""
    console.print(text, markup=False, highlight=False, soft_wrap=True)


def warn(text: str) -> None:
    """A line in yellow. `text` is plain text, not markup."""
    say(f"[yellow]{escape(text)}[/yellow]")


def fail(problem: str, then: str = "", *, code: int = FAILED) -> NoReturn:
    """End the command: `problem` in red, then, in the normal colour, what to do about it. Both
    are plain text, not markup."""
    say(f"[red]{escape(problem)}[/red]" + (f" {escape(then)}" if then else ""))
    raise typer.Exit(code)


def table(*columns: str) -> Table:
    """A plain table, with no frame, that has these columns."""
    plain = Table(box=None, pad_edge=False)
    for column in columns:
        plain.add_column(column)
    return plain


def span(seconds: float) -> str:
    """ "3d", "5h", "12m" or "40s": how long, in the largest unit that fits at least once."""
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def ago(then: float | None, now: float) -> str:
    """ "5m ago", or "-" for a time that never happened."""
    return "-" if then is None else f"{span(now - then)} ago"


def _build_for_real() -> App:
    from personal_agent.app import build_app  # the model SDK loads here, not when `pa` starts

    return build_app()


build: Callable[[], App] = _build_for_real
"""What opens the agent. Tests put a function here that returns an agent wired to a scripted model."""


@contextlib.contextmanager
def opened() -> Iterator[App]:
    """The agent's home, opened for one command and closed when the command ends, however it ends."""
    try:
        app = build()
    except ConfigError as exc:
        fail(str(exc), code=USAGE)
    except DatabaseTooNew as exc:
        fail(str(exc), code=USAGE)
    except sqlite3.DatabaseError as exc:
        # Not a database, or a damaged one. `pa db check` can look at it without opening it for use.
        fail(f"cannot open the database: {exc}.", "Run `pa db check`.")
    setup_logging(app.paths)
    with app:
        yield app


def home() -> Paths:
    """The agent's home, which must exist, with its `.env` loaded. For a command that has no use
    for the database but needs to know where things are."""
    paths = Paths.resolve()
    if not paths.home.is_dir():
        fail(f"{paths.home} does not exist.", "Run `pa init` first.", code=USAGE)
    try:
        load_env(paths)
    except ConfigError as exc:
        fail(str(exc), code=USAGE)
    return paths
