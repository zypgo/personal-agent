from __future__ import annotations

import pytest

from personal_agent.channels.base import ChannelError
from personal_agent.core.dispatcher import MAX_ATTEMPTS, RETRY_BACKOFF_S, Dispatcher


class FakeChannel:
    def __init__(self, name: str = "cli", *, raises: Exception | None = None) -> None:
        self.name = name
        self.raises = raises
        self.sent: list[str] = []

    async def send(self, message) -> None:
        if self.raises is not None:
            raise self.raises
        self.sent.append(message.text)


def queue(app, text: str = "hello", **kwargs):
    kwargs.setdefault("channel", "cli")
    return app.outbox.enqueue(session_key="main", text=text, **kwargs)


async def test_delivery_marks_sent_and_then_applies_the_on_sent_commits(make_app):
    app = make_app()
    app.memory.write_text("/memories/state/bookmark.md", "last-seen: 100")
    channel = FakeChannel()
    message_id = queue(
        app,
        "Brief: 3 items",
        on_sent=[
            {
                "command": "str_replace",
                "path": "/memories/state/bookmark.md",
                "old_str": "100",
                "new_str": "250",
            }
        ],
    )

    report = await Dispatcher(app, {"cli": channel}).deliver_once()

    assert report.sent == [message_id]
    assert channel.sent == ["Brief: 3 items"]
    assert app.outbox.get(message_id).status == "sent"
    assert app.memory.read_text("/memories/state/bookmark.md") == "last-seen: 250"


async def test_commits_are_not_applied_when_delivery_fails(make_app):
    app = make_app()
    app.memory.write_text("/memories/state/bookmark.md", "last-seen: 100")
    command = {
        "command": "str_replace",
        "path": "/memories/state/bookmark.md",
        "old_str": "100",
        "new_str": "250",
    }
    queue(app, on_sent=[command])

    await Dispatcher(app, {"cli": FakeChannel(raises=ChannelError("offline"))}).deliver_once()

    assert app.memory.read_text("/memories/state/bookmark.md") == "last-seen: 100"


async def test_commits_are_not_applied_when_the_outcome_is_unknown(make_app):
    app = make_app()
    app.memory.write_text("/memories/state/bookmark.md", "last-seen: 100")
    command = {
        "command": "str_replace",
        "path": "/memories/state/bookmark.md",
        "old_str": "100",
        "new_str": "250",
    }
    queue(app, on_sent=[command])

    await Dispatcher(
        app, {"cli": FakeChannel(raises=TimeoutError("read timed out"))}
    ).deliver_once()

    assert app.memory.read_text("/memories/state/bookmark.md") == "last-seen: 100"


async def test_a_definite_failure_is_retried_with_backoff_and_then_given_up(make_app, clock):
    app = make_app()
    message_id = queue(app)
    dispatcher = Dispatcher(app, {"cli": FakeChannel(raises=ChannelError("503"))})

    report = await dispatcher.deliver_once()
    assert report.retrying == [message_id]
    assert (await dispatcher.deliver_once()).retrying == []  # not due yet: nothing is claimed

    for _ in range(MAX_ATTEMPTS - 1):  # the first attempt happened above
        clock.advance(max(RETRY_BACKOFF_S) + 1)
        report = await dispatcher.deliver_once()
    assert report.failed == [message_id]
    assert app.outbox.get(message_id).status == "failed"
    assert app.outbox.get(message_id).last_error == "503"


async def test_a_permanent_failure_is_not_retried(make_app):
    app = make_app()
    message_id = queue(app)

    report = await Dispatcher(
        app, {"cli": FakeChannel(raises=ChannelError("bot was blocked", permanent=True))}
    ).deliver_once()

    assert report.failed == [message_id]


async def test_an_unknown_outcome_is_never_resent_blindly(make_app, clock):
    app = make_app()
    message_id = queue(app)
    channel = FakeChannel(raises=ConnectionResetError("reset by peer"))
    dispatcher = Dispatcher(app, {"cli": channel})

    report = await dispatcher.deliver_once()
    clock.advance(10_000)
    again = await dispatcher.deliver_once()

    assert report.unknown == [message_id]
    assert app.outbox.get(message_id).status == "maybe_sent"
    assert again.sent == again.retrying == []
    assert [m.id for m in app.outbox.unresolved()] == [message_id]


async def test_an_urgent_message_with_unknown_outcome_is_retried(make_app, clock):
    app = make_app()
    message_id = queue(app, urgency="urgent")
    dispatcher = Dispatcher(app, {"cli": FakeChannel(raises=ConnectionResetError("reset"))})

    report = await dispatcher.deliver_once()

    assert report.retrying == [message_id]
    assert app.outbox.get(message_id).status == "pending"


async def test_only_messages_for_registered_channels_are_claimed(make_app):
    app = make_app()
    queue(app, "for the terminal", channel="cli")
    other = queue(app, "for telegram", channel="telegram")

    await Dispatcher(app, {"cli": FakeChannel()}).deliver_once()

    assert app.outbox.get(other).status == "pending"


async def test_urgent_messages_go_first(make_app):
    app = make_app()
    queue(app, "routine")
    queue(app, "fire!", urgency="urgent")
    channel = FakeChannel()

    await Dispatcher(app, {"cli": channel}).deliver_once()

    assert channel.sent == ["fire!", "routine"]


async def test_a_broken_commit_is_reported_but_does_not_undo_the_delivery(make_app, caplog):
    app = make_app()
    message_id = queue(
        app,
        on_sent=[
            {
                "command": "str_replace",
                "path": "/memories/state/missing.md",
                "old_str": "a",
                "new_str": "b",
            }
        ],
    )

    report = await Dispatcher(app, {"cli": FakeChannel()}).deliver_once()

    assert report.sent == [message_id]
    assert "on_sent commit" in caplog.text


@pytest.mark.parametrize("attempts", [1, 2, 3, 4, 5, 9])
def test_backoff_grows_and_is_capped(attempts):
    delay = Dispatcher._backoff(attempts)
    assert delay == RETRY_BACKOFF_S[min(attempts, len(RETRY_BACKOFF_S)) - 1]
