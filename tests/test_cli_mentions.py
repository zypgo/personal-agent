"""Every `pa ...` command that the program, the README or the docs tell the owner to run exists.

A message that sends the owner to a command that is not there is worse than no message. It happened
once (the install summary named `pa service status` before that command existed) and the test that
pinned the summary pinned the mistake, so this one looks at the commands instead of at the words.
"""

from __future__ import annotations

import re
from pathlib import Path

import typer

from personal_agent.cli import app

ROOT = Path(__file__).resolve().parent.parent
MENTION = re.compile(r"\bpa ((?:[a-z][a-z-]*)(?: [a-z][a-z-]*)?)")


def mentions() -> dict[str, set[str]]:
    files = [*(ROOT / "src").rglob("*.py"), ROOT / "README.md", *(ROOT / "docs").glob("*.md")]
    found: dict[str, set[str]] = {}
    for file in files:
        for match in MENTION.finditer(file.read_text(encoding="utf-8", errors="replace")):
            found.setdefault(match.group(1), set()).add(str(file.relative_to(ROOT)))
    return found


def resolves(words: str, root) -> bool:
    """Whether the words name a command: a group needs the next word to be one of its commands,
    and once a command that is not a group is reached, what follows is only its arguments."""
    command = root
    for word in words.split():
        if not hasattr(command, "commands"):  # not a group (Typer brings its own click)
            return True
        if word not in command.commands:
            return False
        command = command.commands[word]
    return True


def test_every_command_that_is_suggested_is_a_command():
    root = typer.main.get_command(app)
    found = mentions()
    assert len(found) > 30, "the search found almost nothing: the pattern or the folders changed"
    missing = {
        words: sorted(where)[:3] for words, where in found.items() if not resolves(words, root)
    }
    assert not missing, f"suggested but not commands: {missing}"


def test_the_walker_does_see_a_command_that_is_not_there():
    root = typer.main.get_command(app)
    assert resolves("service install", root) and resolves("memory restore", root)
    assert resolves("ask hello", root)  # a leaf: the rest is arguments
    assert not resolves("service nonsense", root)
    assert not resolves("nonsense", root)
