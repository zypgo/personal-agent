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


class Channel(Protocol):
    name: str

    async def send(self, message: OutMessage) -> None:
        """Deliver one message. Returning normally means the channel confirmed delivery."""
        ...
