"""Python plugins: extra tools that ship as installable packages.

A plugin declares an entry point in the group `personal_agent.plugins`:

    [project.entry-points."personal_agent.plugins"]
    weather = "my_weather:register"

and its `register(api)` function adds what it provides through `PluginApi`. Two rules keep this
from becoming a way to run unreviewed code:

* Installing a package does not switch it on. Only the names in `[plugins] enabled` are loaded,
  because a plugin is ordinary code running inside the agent with the owner's rights (unlike an
  MCP server or a skill, it is not in a separate process). Prefer MCP when a tool can live there.
* A plugin's tools are registered like every other tool: they get the `plugin__<name>__` prefix,
  they go through the same guard (policy, approval, audit, output limits, timeouts), and they
  are offered to runs the owner is part of (`full`, `scheduled`), never to runs woken by outside
  content. Rules in `[policy]` apply to them by name.

A plugin may also add a *channel* (a chat app the owner can talk to the agent through). A channel
is switched on by the same list: it exists only if its plugin is enabled. The plugin decides who
the owner is on its side, and hands what they say to `Ingress`, the single door every channel uses
(see `PluginApi.add_channel`).

A plugin that fails to load costs only its own tools and channels: the others, and the agent,
carry on.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

from agents import FunctionTool

from personal_agent.config import PluginsConfig
from personal_agent.paths import Paths
from personal_agent.tools.base import FULL, SCHEDULED, Category, ToolDef, ToolRegistry

log = logging.getLogger(__name__)

GROUP = "personal_agent.plugins"
API_VERSION = 1
NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,23}")
CHANNEL_NAME = re.compile(r"[a-z][a-z0-9-]{1,23}")
RESERVED_CHANNELS = frozenset({"cli", "web", "telegram", "webhooks", "auto"})
_NOT_NAME = re.compile(r"[^A-Za-z0-9_-]")


class PluginError(Exception):
    """A plugin asked for something the API does not allow."""


@dataclass(frozen=True)
class ChannelSpec:
    """A channel a plugin offers: built (with the app) only when the daemon starts."""

    name: str
    factory: Callable[[Any], Any]
    push: bool
    plugin: str


class PluginApi:
    """What a plugin's `register` function is given. Version 1."""

    version = API_VERSION

    def __init__(
        self,
        name: str,
        registry: ToolRegistry,
        paths: Paths,
        settings: dict[str, Any] | None = None,
        channels: dict[str, ChannelSpec] | None = None,
    ) -> None:
        self.name = name
        self._registry = registry
        self._paths = paths
        self._settings = settings or {}
        self._channels = channels if channels is not None else {}
        self.added: list[str] = []
        self.added_channels: list[str] = []
        self.log = logging.getLogger(f"personal_agent.plugin.{name}")

    @property
    def settings(self) -> dict[str, Any]:
        """The plugin's own table from config.toml (`[plugins.settings.<name>]`), as a copy.
        Secrets do not belong there: read them from the environment."""
        return copy.deepcopy(self._settings)

    @property
    def data_dir(self) -> Path:
        """A private folder for the plugin's own files (inside the agent's home)."""
        folder = self._paths.home / "plugins" / self.name
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        return folder

    def add_tool(
        self,
        tool: FunctionTool,
        *,
        category: Category | str,
        taints: bool = False,
        detail: Callable[[dict[str, Any]], str] | None = None,
        validate: Callable[[Any, dict[str, Any]], str | None] | None = None,
        floor: Callable[[Any, dict[str, Any]], str | None] | None = None,
        available: Callable[[Any], str | None] | None = None,
        timeout: Callable[[Any, dict[str, Any]], float] | None = None,
    ) -> str:
        """Offer a tool (an Agents SDK `FunctionTool`, e.g. from `@function_tool`). Returns the
        name the model sees.

        `category` says what the tool can do: one of `read`, `write`, `exec`, `net_write`, ...
        It decides whether a call needs the owner's approval, so declare the strictest truth.
        `taints=True` marks a tool that returns content the owner did not write (web pages,
        mail): a run that read it asks before acting."""
        try:
            kind = Category(category)
        except ValueError:
            allowed = ", ".join(c.value for c in Category)
            raise PluginError(f"unknown category {category!r} (use one of: {allowed})") from None
        if not isinstance(tool, FunctionTool):
            raise PluginError("add_tool takes a FunctionTool (for example from @function_tool)")
        short = _NOT_NAME.sub("_", tool.name) or "_"
        public = f"plugin__{self.name}__{short}"
        if len(public) > 64:
            raise PluginError(f"the tool name {public!r} is longer than the 64 characters allowed")
        renamed = dataclasses.replace(tool, name=public)
        self._registry.register(
            ToolDef(
                name=public,
                category=kind,
                tool=renamed,
                profiles=frozenset({FULL, SCHEDULED}),
                taints=taints,
                source=f"plugin:{self.name}",
                detail=detail,
                validate=validate,
                floor=floor,
                available=available,
                timeout=timeout,
            )
        )
        self.added.append(public)
        return public

    def add_channel(self, name: str, factory: Callable[[Any], Any], *, push: bool = True) -> str:
        """Offer a channel: a way for the owner to talk to the agent from some app.

        `factory(app)` is called once when the daemon starts and returns an object with

        * `name` (the same as here) and `async send(message)`: deliver an outbox message. Return
          normally only when the app really took it; raise `personal_agent.channels.base.
          ChannelError` when it definitely did not, anything else when the outcome is unknown.
        * optionally `async run(daemon)`: listen for the owner (it is supervised and restarted).

        Who may talk is the channel's decision: only an account the owner approved may reach
        `Ingress(app, wake=daemon.wake).owner_message(channel=name, text=..., external_id=...,
        recipient=...)`, and `app.pairing` is there to do the approving (`pa channels approve`).
        A stranger's message must never be passed on: it would reach the model.

        `push=False` says the app shows a message only when the owner opens it, so unprompted news
        prefers one that can ping them."""
        if not CHANNEL_NAME.fullmatch(name):
            raise PluginError("a channel name is 2-24 lowercase letters, digits and hyphens")
        if name in RESERVED_CHANNELS:
            raise PluginError(f"{name!r} is a built-in channel name")
        if name in self._channels:
            raise PluginError(f"another plugin already provides a channel called {name!r}")
        if not callable(factory):
            raise PluginError("add_channel takes a function that builds the channel")
        self._channels[name] = ChannelSpec(name, factory, bool(push), self.name)
        self.added_channels.append(name)
        return name

    def rollback(self) -> None:
        for name in self.added:
            self._registry.unregister(name)
        self.added.clear()
        for channel in self.added_channels:
            self._channels.pop(channel, None)
        self.added_channels.clear()


@dataclass(frozen=True)
class PluginInfo:
    name: str
    status: str
    """`loaded`, `failed`, `missing` (enabled but nothing installed provides it) or `off`."""
    detail: str = ""
    tools: tuple[str, ...] = field(default_factory=tuple)
    channels: tuple[str, ...] = field(default_factory=tuple)


def installed() -> dict[str, metadata.EntryPoint]:
    """Plugins that are installed, by name, without importing any of them."""
    return {ep.name: ep for ep in metadata.entry_points(group=GROUP)}


def load_plugins(
    config: PluginsConfig,
    *,
    registry: ToolRegistry,
    paths: Paths,
    entry_points: Iterable[Any] | None = None,
    channels: dict[str, ChannelSpec] | None = None,
) -> list[PluginInfo]:
    """Load the enabled plugins. Never raises: what went wrong is in the returned report.
    Channels the plugins offer are added to `channels`."""
    found = {
        ep.name: ep for ep in (entry_points if entry_points is not None else installed().values())
    }
    report: list[PluginInfo] = []
    for name in dict.fromkeys(config.enabled):
        if not NAME.fullmatch(name):
            report.append(
                PluginInfo(name, "failed", "plugin names are letters, digits and hyphens")
            )
            continue
        entry = found.get(name)
        if entry is None:
            report.append(
                PluginInfo(
                    name, "missing", f"no installed package provides a plugin called {name!r}"
                )
            )
            continue
        api = PluginApi(name, registry, paths, config.settings.get(name), channels)
        try:
            entry.load()(api)
        except (Exception, SystemExit) as exc:
            api.rollback()
            text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
            log.warning("plugin %s could not be loaded: %r", name, exc)
            report.append(PluginInfo(name, "failed", f"{type(exc).__name__}: {text}"[:300]))
            continue
        report.append(PluginInfo(name, "loaded", "", tuple(api.added), tuple(api.added_channels)))
    enabled = set(config.enabled)
    report.extend(
        PluginInfo(name, "off", "installed, not listed in [plugins] enabled")
        for name in sorted(found)
        if name not in enabled
    )
    return report
