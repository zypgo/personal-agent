from __future__ import annotations

from typing import Protocol

from personal_agent.core.outbox import OutMessage


class ChannelError(Exception):
    """Raised by `Channel.send` when the message was definitely NOT delivered.

    Anything else a channel raises means the outcome is unknown (the message may have gone
    out), and the dispatcher will not blindly resend it.
    """

    def __init__(
        self, message: str, *, permanent: bool = False, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.permanent = permanent
        """Retrying cannot help (for example the owner blocked the bot)."""
        self.retry_after = retry_after


PULL_ONLY = frozenset({"cli", "web"})
"""Channels that show a message only when the owner looks: the terminal and the web page cannot
ping a phone. Unprompted news goes to one that can, when there is one."""


class Channel(Protocol):
    name: str

    async def send(self, message: OutMessage) -> None:
        """Deliver one message. Returning normally means the channel confirmed delivery."""
        ...
