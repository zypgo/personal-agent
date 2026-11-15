"""The one way a chat channel hands the owner's words to the agent.

Telegram, the web page and any channel a plugin adds all come through here, so the rules are
written once: what counts as an owner message, what a voice note or a picture becomes, which
things are commands, and that nothing is queued twice.

A channel decides *who* is the owner (an approved Telegram id, a web session). By the time a
message reaches `Ingress` that has been settled; this class never looks at the content to decide
trust.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from personal_agent.channels.commands import ChatCommands, parse_command
from personal_agent.channels.transcribe import TranscriptionError, estimate_seconds
from personal_agent.core.events import Kind, NewEvent, Trust
from personal_agent.core.media import MediaError
from personal_agent.core.sessions import DEFAULT_SESSION
from personal_agent.core.transcript import Turn
from personal_agent.harness.pricing import transcription_cost_usd
from personal_agent.security.sanitize import strip_invisible

if TYPE_CHECKING:
    from personal_agent.app import App

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 20_000
MAX_IMAGES = 4
MAX_QUOTE_CHARS = 1_000

_C0 = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class Upload:
    """A file that came with a message."""

    data: bytes
    mime: str = ""
    name: str = ""
    duration_s: float | None = None
    """Length of an audio clip, if the sender's app said (Telegram does)."""


@dataclass(frozen=True)
class Accepted:
    status: str
    """`queued` (the agent will answer), `duplicate` (already had it), `command` (handled here;
    its answer is queued like any other message) or `rejected` (see `reply`)."""
    reply: str | None = None
    """For `rejected`: what to tell the sender."""
    turn: Turn | None = None
    event_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status != "rejected"


def clean_text(text: str) -> str:
    """The owner's own words, minus characters that rewrite what is displayed. Tabs, newlines and
    ordinary punctuation stay; the owner is trusted, but a pasted terminal escape is not text."""
    return _C0.sub("", strip_invisible(text)).strip()


class Ingress:
    def __init__(
        self,
        app: App,
        *,
        wake: Callable[[], None] = lambda: None,
        session_key: str = DEFAULT_SESSION,
    ) -> None:
        self.app = app
        self.wake = wake
        self.session_key = session_key
        self.commands = ChatCommands(app, wake=wake)

    async def owner_message(
        self,
        *,
        channel: str,
        text: str = "",
        images: Sequence[Upload] = (),
        voice: Upload | None = None,
        external_id: str | None = None,
        recipient: str | None = None,
        reply_to: str | None = None,
        forwarded: bool = False,
    ) -> Accepted:
        """A message from the owner on `channel`. `recipient` is how that channel addresses the
        owner (a Telegram chat id); the answer is sent back to it.

        `forwarded`: the owner passed on something somebody else wrote. It reaches the agent as
        quoted outside text, never as the owner's own words, and is never run as a command."""
        app = self.app
        body = clean_text(text)
        if len(body) > MAX_TEXT_CHARS:
            return Accepted(
                "rejected",
                reply=f"That message is {len(body):,} characters; the limit is {MAX_TEXT_CHARS:,}.",
            )

        parsed = (
            parse_command(body) if body and not images and voice is None and not forwarded else None
        )
        if parsed is not None and self.commands.known(parsed[0]):
            return self._command(parsed, body, channel, recipient, external_id)

        spoken = False
        if voice is not None:
            heard = await self._transcribe(voice)
            if isinstance(heard, Accepted):
                return heard
            body = f"{body}\n\n{heard}".strip() if body else heard
            spoken = True

        try:
            attachments = self._store_images(images)
        except MediaError as exc:
            return Accepted("rejected", reply=str(exc))
        if not body and not attachments:
            return Accepted("rejected", reply="That message is empty.")

        payload: dict[str, Any] = {"text": "" if forwarded else body, "recipient": recipient}
        if forwarded and body:
            payload["forwarded"] = body
        if attachments:
            payload["attachments"] = attachments
        if spoken:
            payload["voice"] = True
        if reply_to:
            payload["reply_to"] = clean_text(reply_to)[:MAX_QUOTE_CHARS]
        with app.db.tx():
            added = app.inbox.add(
                NewEvent(
                    source=channel,
                    kind=Kind.MESSAGE,
                    session_key=self.session_key,
                    payload=payload,
                    external_id=external_id,
                    trust=Trust.OWNER,
                )
            )
            if added.duplicate:
                return Accepted("duplicate", event_id=added.id)
            marks = {k: True for k, on in (("voice", spoken), ("forwarded", forwarded)) if on}
            turn = app.transcript.add_in(channel, body, attachments=attachments, meta=marks or None)
        self.wake()
        return Accepted("queued", turn=turn, event_id=added.id)

    # ---- commands ---------------------------------------------------------------------
    def _command(
        self,
        parsed: tuple[str, list[str]],
        body: str,
        channel: str,
        recipient: str | None,
        external_id: str | None,
    ) -> Accepted:
        app = self.app
        key = f"command:{channel}:{external_id}" if external_id else None
        if key is not None and app.db.kv_get(key) is not None:
            return Accepted("duplicate")  # a re-delivered update: do not run it twice
        turn = app.transcript.add_in(channel, body)
        name, args = parsed
        try:
            answer = self.commands.run(name, args, channel=channel, session_key=self.session_key)
        except Exception:
            log.exception("command /%s failed", name)
            answer = f"/{name} failed. The details are in the log."
        if key is not None:
            app.db.kv_set(key, "1", now=app.clock.now())
        app.outbox.enqueue(
            session_key=self.session_key,
            channel=channel,
            recipient=recipient,
            text=answer,
            kind="notice",
            dedupe_key=key or f"command:{channel}:{uuid.uuid4().hex}",
            extra={"interactive": True},
        )
        self.wake()
        return Accepted("command", turn=turn)

    # ---- voice and pictures -----------------------------------------------------------
    async def _transcribe(self, voice: Upload) -> str | Accepted:
        """The words of a voice message, or the reason it was not turned into any."""
        app, cfg = self.app, self.app.config.media
        if not cfg.voice:
            return Accepted("rejected", reply="Voice messages are switched off ([media] voice).")
        if len(voice.data) > cfg.max_voice_bytes:
            return Accepted(
                "rejected",
                reply=f"That recording is too large ({len(voice.data) // 1_000_000} MB).",
            )
        seconds = estimate_seconds(len(voice.data), voice.duration_s)
        if seconds > cfg.max_voice_seconds:
            return Accepted(
                "rejected",
                reply=f"That recording is too long ({int(seconds)} s; the limit is {cfg.max_voice_seconds} s).",
            )
        if app.budget.spent_today() >= app.budget.daily_limit():
            return Accepted(
                "rejected", reply="Today's spending limit is reached, so I cannot listen right now."
            )
        try:
            words = await app.transcriber.transcribe(voice.data, mime=voice.mime, name=voice.name)
        except TranscriptionError as exc:
            return Accepted("rejected", reply=str(exc))
        # Whatever happened inside, the audio was sent and billed: count it.
        app.budget.record_spend(
            "transcription",
            transcription_cost_usd(cfg.transcribe_model, seconds),
            f"{cfg.transcribe_model}, {seconds:.0f}s",
        )
        if not words:
            return Accepted("rejected", reply="I could not make out any words in that recording.")
        return words

    def _store_images(self, images: Sequence[Upload]) -> list[dict[str, Any]]:
        if not images:
            return []
        cfg = self.app.config.media
        if not cfg.images:
            raise MediaError("Pictures are switched off ([media] images).")
        if len(images) > MAX_IMAGES:
            raise MediaError(f"At most {MAX_IMAGES} pictures per message.")
        stored: list[dict[str, Any]] = []
        for image in images:
            if len(image.data) > cfg.max_image_bytes:
                raise MediaError(
                    f"A picture is too large ({len(image.data) // 1_000_000} MB; "
                    f"the limit is {cfg.max_image_bytes // 1_000_000} MB)."
                )
            ref = self.app.media.put(image.data)
            stored.append(ref.to_json())
        return stored
