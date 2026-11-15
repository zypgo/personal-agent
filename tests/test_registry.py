from __future__ import annotations

import pytest

from personal_agent.channels.registry import ChannelConflict, ChannelSet, build_channels
from personal_agent.channels.telegram import TelegramChannel
from personal_agent.config import Config, TelegramConfig


class Passive:
    """A channel that can send but does not listen."""

    def __init__(self, name):
        self.name = name

    async def send(self, message) -> None: ...


class Listening(Passive):
    async def run(self, daemon) -> None: ...


def test_a_listening_channel_is_also_run_as_a_service():
    chosen = ChannelSet()
    passive, listening = Passive("cli"), Listening("web")
    chosen.add(passive)
    chosen.add(listening)
    assert chosen.channels == {"cli": passive, "web": listening}
    assert chosen.services == [listening]


def test_two_channels_cannot_share_a_name():
    chosen = ChannelSet()
    chosen.add(Passive("web"))
    with pytest.raises(ChannelConflict, match="web"):
        chosen.add(Listening("web"))
    assert len(chosen.channels) == 1


def test_by_default_only_the_terminal_is_built(make_app):
    app = make_app()
    assert build_channels(app).channels == {}
    terminal = Passive("cli")
    chosen = build_channels(app, terminal=terminal)
    assert chosen.channels == {"cli": terminal} and chosen.services == []


def test_telegram_is_built_only_when_it_is_switched_on(make_app):
    on = build_channels(make_app(config=Config(telegram=TelegramConfig(enabled=True))))
    assert isinstance(on.channels["telegram"], TelegramChannel)
    assert on.services == [on.channels["telegram"]]
