"""Tool metadata. Every tool declares a *category* and which run *profiles* may use it.

Capability narrowing beats approval and approval beats prompting: a run that has no business
touching the network is simply never given a network tool.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agents import FunctionTool

from personal_agent.security.sanitize import clipped, strip_invisible

DETAIL_CHARS = 400
"""The most of a call's `detail` that rules and the audit log see. A tool whose detail can be
longer says so itself (`...[+N more characters]`), and `detail_of` does it for the rest."""


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
    review: Callable[[dict[str, Any]], str] | None = None
    """What the owner reads when asked to approve a call: everything the call would send, write or
    run, not only its `detail`. A tool whose approval hangs on something its short detail leaves
    out (the body of a request, the content of a file, the prompt of a recurring job) says it here;
    without one the owner is shown the detail."""
    views: Callable[[dict[str, Any]], Sequence[str]] | None = None
    """Further texts that `deny` and `always_ask` rules are tried on, besides the detail: for a
    shell line, each command inside it. A rule written for `rm -rf *` then also catches
    `cd build && rm -rf .`, which the detail alone (one text for the whole line) would not."""
    category_for: Callable[[Any, dict[str, Any]], Category] | None = None
    """Called with (run context, arguments): lets one tool change category by what it is asked to
    do (a reminder is lighter than a recurring job) or by what the run has already seen."""
    validate: Callable[[Any, dict[str, Any]], str | None] | None = None
    """Called with (run context, arguments) before policy: returns why the call cannot work, so
    the model hears about a mistake at once instead of the owner being asked to approve it."""
    floor: Callable[[Any, dict[str, Any]], str | None] | None = None
    """Called with (run context, arguments) before everything else: returns why this call must
    never run. Unlike `validate` this is a safety rule: no mode, rule or approval overrides it,
    and a check that crashes counts as a refusal."""
    available: Callable[[Any], str | None] | None = None
    """Called with the app: returns why the tool cannot be offered right now (no sandbox, say)."""
    timeout: Callable[[Any, dict[str, Any]], float] | None = None
    """Called with (app, arguments): how long one call may run, for tools that know better than
    the global tool timeout (a shell command that was given ten minutes)."""

    def category_of(self, ctx: Any, args: dict[str, Any]) -> Category:
        """The category of this call. A category function that crashes gives the tool's own
        (strictest) category, never a lighter one."""
        if self.category_for is not None:
            try:
                return self.category_for(ctx, args)
            except Exception:
                return self.category
        return self.category

    def problem_with(self, ctx: Any, args: dict[str, Any]) -> str | None:
        """Why this call cannot work, if the tool can tell in advance. A validator that crashes
        says nothing: this is a convenience for the owner, never a security check."""
        if self.validate is None:
            return None
        try:
            return self.validate(ctx, args)
        except Exception:
            return None

    def floor_of(self, ctx: Any, args: dict[str, Any]) -> str | None:
        """Why this call is forbidden outright. Fails closed: a broken check blocks the call."""
        if self.floor is None:
            return None
        try:
            return self.floor(ctx, args)
        except Exception:
            return "this call could not be checked against the safety rules"

    def unavailable(self, app: Any) -> str | None:
        """Why the tool cannot be offered right now. Fails closed: a broken check hides it."""
        if self.available is None:
            return None
        try:
            return self.available(app)
        except Exception:
            return "its availability could not be determined"

    def timeout_of(self, app: Any, args: dict[str, Any]) -> float | None:
        if self.timeout is None:
            return None
        try:
            seconds = float(self.timeout(app, args))
        except Exception:
            return None
        return seconds if seconds > 0 else None

    def detail_of(self, args: dict[str, Any]) -> str:
        """The short summary for rules and the audit log: invisible characters removed, and cut
        with a note when it is long."""
        if self.detail is None:
            return ""
        try:
            return clipped(strip_invisible(str(self.detail(args)), formatting=True), DETAIL_CHARS)
        except Exception:
            return ""

    def review_of(self, args: dict[str, Any]) -> str:
        """What the owner is shown to approve this call (see `review`), or "" for the detail."""
        if self.review is None:
            return ""
        try:
            return strip_invisible(str(self.review(args)), formatting=True)
        except Exception:
            return ""

    def views_of(self, args: dict[str, Any]) -> tuple[str, ...]:
        """The further texts a `deny` or `always_ask` rule is tried on (see `views`)."""
        if self.views is None:
            return ()
        try:
            return tuple(strip_invisible(str(v), formatting=True) for v in self.views(args))
        except Exception:
            return ()


@dataclass
class ToolRegistry:
    _tools: dict[str, ToolDef] = field(default_factory=dict)

    def register(self, tool: ToolDef) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def for_profile(self, profile: str) -> list[ToolDef]:
        return [t for t in self._tools.values() if profile in t.profiles]

    def names(self) -> list[str]:
        return list(self._tools)

    def extend(self, tools: Iterable[ToolDef]) -> None:
        for tool in tools:
            self.register(tool)
