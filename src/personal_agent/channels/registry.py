"""Which channels this process runs.

A channel is a `Channel` (delivers outbox messages) and, usually, also a `Service` (listens for
the owner: polls a chat app, serves a web page). Built-in channels and channels that plugins
provide are put together the same way, so adding one never means editing the daemon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from personal_agent.channels.base import Channel

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.daemon import Service


class ChannelConflict(Exception):
    """Two channels want the same name."""


@dataclass
class ChannelSet:
    channels: dict[str, Channel] = field(default_factory=dict)
    services: list[Service] = field(default_factory=list)

    def add(self, channel: Any) -> None:
        name = channel.name
        if name in self.channels:
            raise ChannelConflict(f"two channels are called {name!r}")
        self.channels[name] = channel
        if callable(getattr(channel, "run", None)):
            self.services.append(channel)


def build_channels(app: App, *, terminal: Channel | None = None) -> ChannelSet:
    """The channels the configuration switches on. `terminal` is the console channel when the
    caller wants the agent's messages printed (`pa serve --console`)."""
    chosen = ChannelSet()
    if terminal is not None:
        chosen.add(terminal)
    if app.config.telegram.enabled:
        from personal_agent.channels.telegram import TelegramChannel

        chosen.add(TelegramChannel(app))
    return chosen
