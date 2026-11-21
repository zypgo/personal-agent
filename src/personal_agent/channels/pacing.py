"""A short chat reply that arrives the way a person types it: a few messages, a beat apart.

In a chat app one block of three paragraphs reads like a letter. Sent as three messages with the
"typing…" indicator in between (Telegram) or a short pause (DingTalk), the same words read like a
person talking. Only plain, short replies to someone who is waiting get this. Anything formatted
(code, lists, tables, headings, quotes), long, urgent, a file or an approval request goes out
whole, so no structure is ever cut in two.

The pause follows how long the next message would take to type, between half a second and two and
a half (openclaw uses a similar 0.8 to 2.5 second "human delay" between blocks).
"""

from __future__ import annotations

import re

from personal_agent.config import ChatConfig
from personal_agent.core.outbox import OutMessage

MAX_CHARS = 700
"""Longer than this is not a chat message but a document: it stays in one piece."""
MIN_PAUSE_S = 0.5
PER_CHAR_S = 0.035
MAX_PAUSE_S = 2.5

_STRUCTURE = re.compile(
    r"^[ \t]*(?:[-*+•][ \t]|\d+[.)][ \t]|\d+、|[一二三四五六七八九十]+、|[（(]\d+[）)]|#{1,6}[ \t]|>|\|)",
    re.MULTILINE,
)
_FENCE = "```"
_PARAGRAPHS = re.compile(r"\n[ \t]*\n")


def chatty(message: OutMessage, cfg: ChatConfig) -> bool:
    """Whether this message is a plain reply to someone who is waiting for it."""
    return (
        cfg.bubbles
        and message.kind == "message"
        and bool(message.payload.get("interactive"))
        and message.urgency != "urgent"
        and not message.payload.get("artifacts")
        and len(message.text) <= MAX_CHARS
    )


def bubbles(text: str, limit: int) -> list[str]:
    """The reply as chat messages: one per paragraph, at most `limit` (the rest joins the last).
    Text with any structure, or with no paragraph break, is one message."""
    body = text.strip()
    if not body or _FENCE in body or _STRUCTURE.search(body):
        return [text]
    parts = [p.strip() for p in _PARAGRAPHS.split(body) if p.strip()]
    if len(parts) < 2:
        return [text]
    limit = max(1, limit)
    if len(parts) > limit:
        parts = [*parts[: limit - 1], "\n\n".join(parts[limit - 1 :])]
    return parts


def pause(text: str) -> float:
    """How long to show "typing…" before this message goes out."""
    return min(MAX_PAUSE_S, MIN_PAUSE_S + PER_CHAR_S * len(text))
