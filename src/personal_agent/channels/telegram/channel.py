"""Telegram: talk to the agent from a phone, and let it reach you there.

One long-polling loop receives updates (no public address or open port needed; it works behind
any NAT), and `send` delivers outbox messages. Both are the same object because they share the
bot token and the connection.

Who may talk: only Telegram accounts the owner approved on this machine (`pa channels approve`).
A stranger who finds the bot gets one reply with a pairing code and nothing else; their messages
never become events, so they cost nothing and cannot try to talk the model into anything.
Groups and channels are ignored: this is a private assistant.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import TYPE_CHECKING, Any

import httpx

from personal_agent.channels.base import ChannelError
from personal_agent.channels.ingress import Accepted, Ingress, Upload
from personal_agent.channels.pairing import PAIRING_TTL_S
from personal_agent.channels.telegram.api import NotSent, TelegramApi, TelegramApiError
from personal_agent.channels.telegram.render import Piece, render
from personal_agent.core.outbox import OutMessage
from personal_agent.core.sessions import DEFAULT_SESSION

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.daemon import Daemon

log = logging.getLogger(__name__)

OFFSET_KEY = "telegram:offset"
MAX_SEND_ATTEMPTS = 3
HANDLE_ATTEMPTS = 3
MAX_WAIT_S = 30.0
"""A rate-limit wait longer than this is handed back to the dispatcher instead of slept through."""
CONFLICT_WAIT_S = 30.0
TYPING_EVERY_S = 4.5
TYPING_MAX_S = 600.0
PHOTO_MAX_SIDE = 1600
"""Telegram offers each photo in several sizes; the biggest one that is still a sensible thing to
upload to a model."""
UNSUPPORTED = "I can only read text, pictures and voice messages here."
_FORWARD_FIELDS = (
    "forward_origin",
    "forward_from",
    "forward_from_chat",
    "forward_sender_name",
    "forward_date",
    "is_automatic_forward",
)


class PartialDelivery(Exception):
    """Some pieces of a long message arrived and a later one did not. The outcome is unknown, not
    "not delivered": sending the whole message again would repeat the pieces that arrived."""


def approval_markup(ids: list[int], *, all_button: bool = True) -> dict[str, Any]:
    """Buttons under an approval request: one pair per request, and "all" when there are several
    (and `all_button`: "all" is what is waiting in the owner's own conversation, which is not what
    a timer's run or a webhook asked about)."""
    rows = [
        [
            {"text": f"✅ Approve #{i}", "callback_data": f"ap:{i}:y"},
            {"text": f"❌ Deny #{i}", "callback_data": f"ap:{i}:n"},
        ]
        for i in ids[:6]
    ]
    if len(ids) > 1 and all_button:
        rows.append(
            [
                {"text": "✅ Approve all", "callback_data": "ap:all:y"},
                {"text": "❌ Deny all", "callback_data": "ap:all:n"},
            ]
        )
    return {"inline_keyboard": rows}


class TelegramChannel:
    name = "telegram"

    def __init__(
        self,
        app: App,
        *,
        api: TelegramApi | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.app = app
        self.cfg = app.config.telegram
        self._api = api
        self._sleep = sleep
        self._owns_api = api is None
        self._token = os.environ.get(self.cfg.token_env, "").strip()

    # ---- the connection ---------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return self._api is not None or bool(self._token)

    @property
    def api(self) -> TelegramApi:
        if self._api is None:
            if not self._token:
                raise ChannelError(
                    f"{self.cfg.token_env} is not set, so Telegram cannot be reached",
                    permanent=True,
                )
            self._api = TelegramApi(
                self._token, api_base=self.cfg.api_base, proxy=self.cfg.proxy or None
            )
        return self._api

    async def close(self) -> None:
        if self._api is not None and self._owns_api:
            api, self._api = self._api, None
            await api.aclose()

    # ---- sending ----------------------------------------------------------------------
    def _chat_for(self, message: OutMessage) -> str:
        """Where this message goes: the chat it answers, else the most recently active paired
        account. Never anyone who is not approved, whatever the message says."""
        pairing = self.app.pairing
        chat = message.recipient
        if not chat:
            recent = pairing.recent_sender(self.name)
            if recent is None:
                raise ChannelError(
                    "no Telegram account is paired yet (message the bot, then "
                    "`pa channels approve telegram CODE`)",
                    permanent=True,
                )
            chat = recent.sender
        if not pairing.is_approved(self.name, chat):
            raise ChannelError("that Telegram account is not approved (any more)", permanent=True)
        return str(chat)

    async def send(self, message: OutMessage) -> None:
        chat = self._chat_for(message)
        pieces = render(message.text)
        ids = [int(i) for i in message.payload.get("approval_ids") or []]
        markup = None
        if message.kind == "approval_request" and ids:
            markup = approval_markup(ids, all_button=message.session_key == DEFAULT_SESSION)
        silent = message.urgency == "low"
        for index, piece in enumerate(pieces):
            last = index == len(pieces) - 1
            try:
                await self._send_piece(chat, piece, markup if last else None, silent)
            except ChannelError as exc:
                if index:
                    raise PartialDelivery(
                        f"{index} of {len(pieces)} parts were delivered before: {exc}"
                    ) from exc
                raise

    async def _send_piece(
        self, chat: str, piece: Piece, markup: dict[str, Any] | None, silent: bool
    ) -> None:
        api = self.api
        for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
            try:
                try:
                    await api.send_message(
                        chat, piece.html, parse_mode="HTML", reply_markup=markup, silent=silent
                    )
                except TelegramApiError as exc:
                    if not exc.unparsable:
                        raise
                    # Telegram did not like our markup: send the words, plain.
                    await api.send_message(chat, piece.plain, reply_markup=markup, silent=silent)
                return
            except NotSent as exc:
                raise ChannelError(str(exc)) from None
            except TelegramApiError as exc:
                if exc.status == 429:
                    wait = exc.retry_after or 1.0
                    if attempt < MAX_SEND_ATTEMPTS and wait <= MAX_WAIT_S:
                        await self._sleep(wait + 0.5)
                        continue
                    raise ChannelError(str(exc), retry_after=wait) from None
                if exc.status >= 500:
                    if attempt < MAX_SEND_ATTEMPTS:
                        await self._sleep(2.0 * attempt)
                        continue
                    # Telegram failed, but we cannot know it did nothing: leave it to the caller.
                    raise
                raise ChannelError(str(exc), permanent=exc.permanent) from None

    # ---- receiving --------------------------------------------------------------------
    async def run(self, daemon: Daemon) -> None:
        """Poll Telegram until the daemon stops. Called under supervision (restarted on a crash)."""
        if not self.configured:
            log.error(
                "Telegram is enabled but %s is not set in .env; the channel is off",
                self.cfg.token_env,
            )
            return
        ingress = Ingress(self.app, wake=daemon.wake)
        typing = asyncio.create_task(self._typing_loop(daemon), name="telegram-typing")
        try:
            try:
                me = await self.api.get_me()
            except TelegramApiError as exc:
                if exc.status in (401, 404):
                    log.error("Telegram rejected the bot token; the channel is off (%s)", exc)
                    return
                raise
            log.info("Telegram bot @%s is listening", me.get("username", "?"))
            await self._poll(daemon, ingress)
        except asyncio.CancelledError:
            await self.close()
            raise
        finally:
            typing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing

    async def _poll(self, daemon: Daemon, ingress: Ingress) -> None:
        app = self.app
        offset = int(app.db.kv_get(OFFSET_KEY) or 0)
        backoff = 1.0
        while not daemon.stopping:
            try:
                updates = await self.api.get_updates(offset, self.cfg.poll_timeout_s)
                backoff = 1.0
            except TelegramApiError as exc:
                if exc.status in (401, 404):
                    log.error("Telegram rejected the bot token; the channel is off (%s)", exc)
                    return
                if exc.status == 409:
                    await self._conflict(exc)
                    continue
                log.warning("Telegram error while polling: %s", exc)
                await self._sleep(exc.retry_after or backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            except (NotSent, httpx.HTTPError) as exc:
                log.warning(
                    "Telegram is unreachable (%s); trying again in %.0f s",
                    type(exc).__name__,
                    backoff,
                )
                await self._sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            for update in updates:
                await self._handle_reliably(update, ingress)
                # Only now, with the update safely in the inbox (or given up on), move past it.
                offset = int(update["update_id"]) + 1
                app.db.kv_set(OFFSET_KEY, str(offset), now=app.clock.now())

    async def _handle_reliably(self, update: dict[str, Any], ingress: Ingress) -> None:
        """Handle one update; a failure that is not Telegram's (a locked database) is retried
        a few times, since handling is idempotent. One that never works is given up on, with the
        owner told, so a single poisonous update cannot stall the whole conversation."""
        for attempt in range(1, HANDLE_ATTEMPTS + 1):
            try:
                await self._handle(update, ingress)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "could not handle Telegram update %s (attempt %d)",
                    update.get("update_id"),
                    attempt,
                )
                if attempt < HANDLE_ATTEMPTS:
                    await self._sleep(float(attempt))
        message = update.get("message") or {}
        sender, chat = message.get("from") or {}, message.get("chat") or {}
        if chat.get("type") == "private" and self.app.pairing.is_approved(
            self.name, str(sender.get("id", ""))
        ):
            await self._say(
                str(chat["id"]), "Something went wrong with that message. Please send it again."
            )

    async def _conflict(self, exc: TelegramApiError) -> None:
        if "webhook" in exc.description.lower():
            log.warning("a webhook is set for this bot; removing it so polling can work")
            try:
                await self.api.delete_webhook()
                return
            except (TelegramApiError, NotSent, httpx.HTTPError):
                log.warning("could not remove the webhook", exc_info=True)
        else:
            log.warning(
                "another program is polling this bot (%s). Only one can; waiting %.0f s",
                exc.description,
                CONFLICT_WAIT_S,
            )
        await self._sleep(CONFLICT_WAIT_S)

    # ---- one update -------------------------------------------------------------------
    async def _handle(self, update: dict[str, Any], ingress: Ingress) -> None:
        if callback := update.get("callback_query"):
            await self._callback(callback, ingress)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return  # edited messages, channel posts and the rest are not conversation
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or sender.get("is_bot") or "id" not in sender:
            return
        sender_id, chat_id = str(sender["id"]), str(chat["id"])
        pairing = self.app.pairing
        if not pairing.is_approved(self.name, sender_id):
            await self._stranger(sender, chat_id)
            return
        pairing.touch(self.name, sender_id)

        text = str(message.get("text") or message.get("caption") or "")
        images: list[Upload] = []
        voice: Upload | None = None
        media = self.app.config.media
        try:
            if photos := message.get("photo"):
                images.append(await self._photo(photos))
            elif (doc := message.get("document")) and str(doc.get("mime_type", "")).startswith(
                "image/"
            ):
                images.append(
                    Upload(await self._download(doc, media.max_image_bytes), str(doc["mime_type"]))
                )
            elif (clip := message.get("voice") or message.get("audio")) is not None:
                voice = Upload(
                    await self._download(clip, media.max_voice_bytes),
                    str(clip.get("mime_type") or "audio/ogg"),
                    str(clip.get("file_name") or ""),
                    float(clip["duration"]) if clip.get("duration") else None,
                )
            elif "text" not in message:
                # A video, a PDF, a sticker, a location... even with a caption. Answering only the
                # caption would pretend the rest had been looked at.
                await self._say(chat_id, UNSUPPORTED)
                return
        except TelegramApiError as exc:
            log.warning("could not download a Telegram file: %s", exc)
            too_big = exc.status == 413
            await self._say(
                chat_id,
                "That file is too large for me."
                if too_big
                else "I could not download that file from Telegram. Send it again?",
            )
            return
        except (NotSent, httpx.HTTPError) as exc:
            log.warning("could not download a Telegram file: %s", type(exc).__name__)
            await self._say(chat_id, "I could not download that file from Telegram. Send it again?")
            return

        replied = message.get("reply_to_message") or {}
        quoted = (
            (message.get("quote") or {}).get("text")
            or replied.get("text")
            or replied.get("caption")
        )
        result = await ingress.owner_message(
            channel=self.name,
            text=text,
            images=images,
            voice=voice,
            external_id=f"tg:{update['update_id']}",
            recipient=chat_id,
            reply_to=str(quoted) if quoted else None,
            forwarded=any(message.get(k) for k in _FORWARD_FIELDS),
        )
        await self._after(result, chat_id)

    async def _after(self, result: Accepted, chat_id: str) -> None:
        if result.status == "rejected" and result.reply:
            await self._say(chat_id, result.reply)

    async def _photo(self, sizes: list[dict[str, Any]]) -> Upload:
        fitting = [s for s in sizes if max(s.get("width", 0), s.get("height", 0)) <= PHOTO_MAX_SIDE]
        chosen = fitting[-1] if fitting else sizes[0]
        return Upload(
            await self._download(chosen, self.app.config.media.max_image_bytes), "image/jpeg"
        )

    async def _download(self, item: dict[str, Any], max_bytes: int) -> bytes:
        return await self.api.download(str(item["file_id"]), max_bytes=max_bytes)

    async def _stranger(self, sender: dict[str, Any], chat_id: str) -> None:
        """Someone who is not approved: give them a code (once) and nothing else."""
        name = " ".join(
            str(sender.get(k, "")) for k in ("first_name", "last_name") if sender.get(k)
        ) or str(sender.get("username") or "")
        request = self.app.pairing.request(self.name, str(sender["id"]), name)
        if request is None or request.notified_at is not None:
            return  # too many waiting, or they already have their code
        text = (
            "This is a private assistant. To use it, run this on the computer where it lives:\n\n"
            f"    pa channels approve telegram {request.code}\n\n"
            f"The code works for {int(PAIRING_TTL_S // 60)} minutes."
        )
        try:
            await self.api.send_message(chat_id, text)
        except (TelegramApiError, NotSent, httpx.HTTPError):
            log.warning("could not send a pairing code", exc_info=True)
            return  # not marked as told: they get it with their next message
        self.app.pairing.mark_notified(request.code)
        log.info("Telegram pairing request %s from %r", request.code, request.label)

    async def _say(self, chat_id: str, text: str) -> None:
        """A direct reply to something that happened right now (an error, a refusal)."""
        try:
            await self.api.send_message(chat_id, text)
        except (TelegramApiError, NotSent, httpx.HTTPError):
            log.warning("could not send a short reply", exc_info=True)

    async def _callback(self, callback: dict[str, Any], ingress: Ingress) -> None:
        """The owner pressed Approve or Deny under a request."""
        sender_id = str((callback.get("from") or {}).get("id", ""))
        callback_id = str(callback.get("id", ""))
        if not self.app.pairing.is_approved(self.name, sender_id):
            with contextlib.suppress(TelegramApiError, NotSent, httpx.HTTPError):
                await self.api.answer_callback(callback_id)
            return
        parts = str(callback.get("data", "")).split(":")
        if (
            len(parts) != 3
            or parts[0] != "ap"
            or parts[2] not in ("y", "n")
            or not (parts[1] == "all" or parts[1].isdigit())
        ):
            with contextlib.suppress(TelegramApiError, NotSent, httpx.HTTPError):
                await self.api.answer_callback(callback_id)
            return
        answer = ingress.commands.decide(
            parts[2] == "y", [parts[1]], self.name, ingress.session_key
        )
        with contextlib.suppress(TelegramApiError, NotSent, httpx.HTTPError):
            await self.api.answer_callback(callback_id, answer.splitlines()[0] if answer else "")
        message = callback.get("message") or {}
        if message.get("message_id") and (message.get("chat") or {}).get("id"):
            with contextlib.suppress(TelegramApiError, NotSent, httpx.HTTPError):
                await self.api.clear_buttons(message["chat"]["id"], message["message_id"])

    # ---- the typing indicator ---------------------------------------------------------
    async def _typing_loop(self, daemon: Daemon) -> None:
        """ "typing..." in the chat while the agent works on something the owner asked there."""
        if not self.cfg.typing:
            return
        app = self.app
        with app.live.subscribe() as events:
            typing_since: float | None = None
            last_sent = 0.0
            while True:
                event = await events.next(TYPING_EVERY_S)
                now = time.monotonic()
                if event is not None and event.get("type") == "activity":
                    if event.get("working") and event.get("channel") == self.name:
                        typing_since = typing_since or now
                    elif not event.get("working"):
                        typing_since = None
                if typing_since is not None and now - typing_since > TYPING_MAX_S:
                    typing_since = None  # never keep it up forever if "finished" was missed
                if typing_since is None:
                    continue
                if now - last_sent >= TYPING_EVERY_S:
                    last_sent = now
                    recent = app.pairing.recent_sender(self.name)
                    if recent is not None:
                        with contextlib.suppress(TelegramApiError, NotSent, httpx.HTTPError):
                            await self.api.send_chat_action(recent.sender)
