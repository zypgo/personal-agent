"""Tool metadata. Every tool declares a *category* and which run *profiles* may use it.

Capability narrowing beats approval and approval beats prompting: a run that has no business
touching the network is simply never given a network tool.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agents import FunctionTool


class Category(StrEnum):
    READ = "read"  # reads local or remote data without changing anything
    MEMORY = "memory"  # reads/writes the agent's own memory
    NOTIFY = "notify"  # talks to the owner
    REMIND = "remind"  # sets a one-off reminder for itself
    SCHEDULE = "schedule"  # creates recurring wake-ups
    WRITE = "write"  # changes files or external state
    EXEC = "exec"  # runs commands
    NET_WRITE = "net_write"  # sends data to the outside world
    CONFIG = "config"  # changes the agent's own configuration or capabilities


# Run profiles, from most to least capable.
FULL = "full"  # the owner is talking to the agent
SCHEDULED = "scheduled"  # a timer woke the agent; no human is waiting
UNTRUSTED = "untrusted"  # woken by content the owner did not write (webhook, third-party mail)
DREAM = "dream"  # nightly memory maintenance

ALL_PROFILES = frozenset({FULL, SCHEDULED, UNTRUSTED, DREAM})


@dataclass(frozen=True)
class ToolDef:
    name: str
    category: Category
    tool: FunctionTool
    profiles: frozenset[str] = ALL_PROFILES
    taints: bool = False
    """The tool returns content the owner did not write (web pages, mail, files from outside)."""
    source: str = "builtin"
    detail: Callable[[dict[str, Any]], str] | None = None
    """Short summary of one call for `tool:detail` rules and the audit log (a command, a host)."""
    category_for: Callable[[dict[str, Any]], Category] | None = None
    """Lets one tool change category by arguments (a reminder is lighter than a recurring job)."""

    def category_of(self, args: dict[str, Any]) -> Category:
        if self.category_for is not None:
            try:
                return self.category_for(args)
            except Exception:
                return self.category
        return self.category

    def detail_of(self, args: dict[str, Any]) -> str:
        if self.detail is None:
            return ""
        try:
            return str(self.detail(args))[:300]
        except Exception:
            return ""


@dataclass
class ToolRegistry:
    _tools: dict[str, ToolDef] = field(default_factory=dict)

    def register(self, tool: ToolDef) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def for_profile(self, profile: str) -> list[ToolDef]:
        return [t for t in self._tools.values() if profile in t.profiles]

    def names(self) -> list[str]:
        return list(self._tools)

    def extend(self, tools: Iterable[ToolDef]) -> None:
        for tool in tools:
            self.register(tool)
