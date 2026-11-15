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

A plugin that fails to load costs only its own tools: the others, and the agent, carry on.
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
_NOT_NAME = re.compile(r"[^A-Za-z0-9_-]")


class PluginError(Exception):
    """A plugin asked for something the API does not allow."""


class PluginApi:
    """What a plugin's `register` function is given. Version 1."""

    version = API_VERSION

    def __init__(
        self,
        name: str,
        registry: ToolRegistry,
        paths: Paths,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self._registry = registry
        self._paths = paths
        self._settings = settings or {}
        self.added: list[str] = []
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

    def rollback(self) -> None:
        for name in self.added:
            self._registry.unregister(name)
        self.added.clear()


@dataclass(frozen=True)
class PluginInfo:
    name: str
    status: str
    """`loaded`, `failed`, `missing` (enabled but nothing installed provides it) or `off`."""
    detail: str = ""
    tools: tuple[str, ...] = field(default_factory=tuple)


def installed() -> dict[str, metadata.EntryPoint]:
    """Plugins that are installed, by name, without importing any of them."""
    return {ep.name: ep for ep in metadata.entry_points(group=GROUP)}


def load_plugins(
    config: PluginsConfig,
    *,
    registry: ToolRegistry,
    paths: Paths,
    entry_points: Iterable[Any] | None = None,
) -> list[PluginInfo]:
    """Load the enabled plugins. Never raises: what went wrong is in the returned report."""
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
        api = PluginApi(name, registry, paths, config.settings.get(name))
        try:
            entry.load()(api)
        except (Exception, SystemExit) as exc:
            api.rollback()
            text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
            log.warning("plugin %s could not be loaded: %r", name, exc)
            report.append(PluginInfo(name, "failed", f"{type(exc).__name__}: {text}"[:300]))
            continue
        report.append(PluginInfo(name, "loaded", "", tuple(api.added)))
    enabled = set(config.enabled)
    report.extend(
        PluginInfo(name, "off", "installed, not listed in [plugins] enabled")
        for name in sorted(found)
        if name not in enabled
    )
    return report
