"""Which channels this process runs.

A channel is a `Channel` (delivers outbox messages) and, usually, also a `Service` (listens for
the owner: polls a chat app, serves a web page). Built-in channels and channels that plugins
provide are put together the same way, so adding one never means editing the daemon.

A service that is not a channel (the webhook listener only receives) is added with `add_service`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from personal_agent.channels.base import Channel

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.daemon import Service

log = logging.getLogger(__name__)


class ChannelConflict(Exception):
    """Two channels want the same name."""


@dataclass
class ChannelSet:
    channels: dict[str, Channel] = field(default_factory=dict)
    services: list[Service] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    """Channels that could not be built (a plugin's factory raised): the rest still run."""

    def add(self, channel: Any) -> None:
        name = channel.name
        if name in self.channels:
            raise ChannelConflict(f"two channels are called {name!r}")
        self.channels[name] = channel
        if callable(getattr(channel, "run", None)):
            self.services.append(channel)

    def add_service(self, service: Any) -> None:
        self.services.append(service)


def build_channels(app: App, *, terminal: Channel | None = None) -> ChannelSet:
    """The channels the configuration switches on. `terminal` is the console channel when the
    caller wants the agent's messages printed (`pa serve --console`)."""
    chosen = ChannelSet()
    if terminal is not None:
        chosen.add(terminal)
    if app.config.telegram.enabled:
        from personal_agent.channels.telegram import TelegramChannel

        chosen.add(TelegramChannel(app))
    if app.config.dingtalk.enabled:
        from personal_agent.channels.dingtalk.channel import DingTalkChannel

        chosen.add(DingTalkChannel(app))
    if app.config.web.enabled:
        from personal_agent.channels.web.channel import WebChannel

        chosen.add(WebChannel(app))
    if app.config.webhooks.enabled:
        from personal_agent.channels.webhooks import WebhookService

        chosen.add_service(WebhookService(app))
    for name, spec in app.plugin_channels.items():
        try:
            channel = spec.factory(app)
            if getattr(channel, "name", None) != name:
                raise ValueError(
                    f"the channel it built is called {getattr(channel, 'name', None)!r}"
                )
            chosen.add(channel)
        except Exception as exc:  # a plugin's code: its failure costs its channel only
            log.warning("channel %s (plugin %s) could not start", name, spec.plugin, exc_info=True)
            chosen.problems.append(f"{name}: {type(exc).__name__}: {exc}"[:300])
    return chosen
