from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import httpx
import pytest
from pydantic import ValidationError

from channel_identity_helpers import bind_owner, revoke_owner
from personal_agent.channels.base import ChannelError
from personal_agent.channels.dingtalk.api import (
    MESSAGE_TOPIC,
    SEND_PATH,
    DeliveryUncertain,
    DingTalkApi,
)
from personal_agent.channels.dingtalk.channel import (
    MESSAGE_CHUNK_BYTES,
    DingTalkChannel,
    message_key,
    recipient_key,
)
from personal_agent.channels.dingtalk.config import DingTalkConfig
from personal_agent.channels.ingress import Ingress
from personal_agent.config import Config
from personal_agent.core.approvals import PendingCall
from personal_agent.core.dispatcher import Dispatcher
from test_dingtalk_api import FakeDingTalk
from test_ingress import events

OWNER = recipient_key("offline-app", "corp", "owner")


def incoming(text="hello", *, message_id="message-1", **kw):
    return {
        "conversationType": "1",
        "senderCorpId": "corp",
        "chatbotCorpId": "corp",
        "senderStaffId": "owner",
        "robotCode": "offline-app",
        "msgId": message_id,
        "senderNick": "Owner",
        "msgtype": "text",
        "text": {"content": text},
        # Deliberately hostile: this URL must never be visited, logged, or persisted.
        "sessionWebhook": "https://evil.invalid/bearer-secret",
        **kw,
    }


def frame(data=None, *, delivery_id="delivery-1", kind="CALLBACK", topic=MESSAGE_TOPIC):
    return json.dumps(
        {
            "specVersion": "1.0",
            "type": kind,
            "headers": {
                "messageId": delivery_id,
                "topic": topic,
                "contentType": "application/json",
            },
            "data": json.dumps(incoming() if data is None else data),
        }
    )


class AckSocket:
    def __init__(self, on_send=lambda: None):
        self.acks = []
        self.on_send = on_send

    async def send(self, message):
        self.on_send()
        self.acks.append(json.loads(message))


@pytest.fixture
async def rig(make_app):
    fake = FakeDingTalk()
    app = make_app(config=Config(dingtalk=DingTalkConfig(enabled=True)))
    async with fake.client() as client:
        channel = DingTalkChannel(
            app, api=DingTalkApi("offline-app", "offline-secret", client=client)
        )
        yield app, channel, fake


@pytest.fixture
def paired(rig):
    app, _, _ = rig
    app.pairing.allow("dingtalk", OWNER, "Owner")
    bind_owner(app, "dingtalk", OWNER)
    return rig


def queue(app, text="hello", **kw):
    kw.setdefault("channel", "dingtalk")
    kw.setdefault("recipient", OWNER)
    message_id = app.outbox.enqueue(session_key="main", text=text, **kw)
    return app.outbox.get(message_id)


async def test_unpaired_private_chat_only_gets_one_local_pairing_code_and_no_model_event(rig):
    app, channel, fake = rig
    ingress = Ingress(app)
    await channel._message(incoming("/approve all"), ingress)
    await channel._message(incoming("again", message_id="message-2"), ingress)
    [request] = app.pairing.pending("dingtalk")
    [sent] = fake.sent()
    assert request.sender == OWNER and request.notified_at is not None
    assert "/bind" in json.loads(sent["msgParam"])["content"]
    assert not events(app) and not app.transcript.latest()
    assert app.pairing.approve(request.code).sender == OWNER
    bind_owner(app, "dingtalk", OWNER)
    await channel._message(incoming("now authorized", message_id="message-3"), ingress)
    assert events(app)[0]["payload"] == {
        "text": "now authorized",
        "recipient": OWNER,
        "principal_id": "owner",
        "membership_version": 1,
        "principal_version": 1,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"conversationType": "2"},
        {"conversationType": 1},
        {"senderStaffId": ""},
        {"senderCorpId": "other-corp"},
        {"chatbotCorpId": "other-corp"},
        {"robotCode": "other-app"},
        {"msgId": ""},
        {"senderStaffId": "owner\n"},
    ],
)
async def test_groups_cross_corp_wrong_robot_and_malformed_identity_are_ignored(paired, overrides):
    app, channel, fake = paired
    await channel._message(incoming(**overrides), Ingress(app))
    assert not events(app) and not app.pairing.pending("dingtalk") and not fake.requests


async def test_configured_corp_is_enforced_even_when_staff_id_matches_an_approval(rig):
    app, channel, fake = rig
    channel.cfg = DingTalkConfig(enabled=True, corp_id="corp")
    foreign = recipient_key("offline-app", "foreign", "owner")
    app.pairing.allow("dingtalk", foreign)
    bind_owner(app, "dingtalk", foreign)
    await channel._message(incoming(senderCorpId="foreign", chatbotCorpId="foreign"), Ingress(app))
    assert not events(app) and not fake.requests


async def test_same_staff_in_another_corp_requires_a_separate_pairing(paired):
    app, channel, fake = paired
    await channel._message(incoming(senderCorpId="foreign", chatbotCorpId="foreign"), Ingress(app))
    assert not events(app)
    assert app.pairing.pending("dingtalk")[0].sender == recipient_key(
        "offline-app", "foreign", "owner"
    )
    assert len(fake.sent()) == 1


async def test_callback_ack_follows_commit_and_duplicates_survive_reopening_the_app(
    paired, make_app
):
    app, channel, _ = paired

    def committed():
        with closing(sqlite3.connect(app.db.path)) as observer:
            assert observer.execute("SELECT count(*) FROM events").fetchone()[0] == 1

    socket = AckSocket(committed)
    await channel._frame(frame(), socket, Ingress(app))
    reopened = make_app(config=app.config)
    replacement = DingTalkChannel(reopened, api=channel.api)
    await replacement._frame(frame(delivery_id="new-transport-id"), socket, Ingress(reopened))
    assert [ack["headers"]["messageId"] for ack in socket.acks] == [
        "delivery-1",
        "new-transport-id",
    ]
    assert all(ack["code"] == 200 for ack in socket.acks)
    [event] = events(reopened)
    assert event["external_id"] == message_key("offline-app", "corp", "message-1")
    assert len(reopened.transcript.latest()) == 1
    assert "bearer-secret" not in repr(events(reopened))
    assert "bearer-secret" not in repr([dict(r) for r in reopened.db.query("SELECT * FROM kv")])


async def test_failed_persistence_sends_no_success_ack_and_can_be_retried(paired, monkeypatch):
    app, channel, _ = paired
    socket = AckSocket()
    original = app.transcript.add_in

    def disk_full(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(app.transcript, "add_in", disk_full)
    with pytest.raises(sqlite3.OperationalError):
        await channel._frame(frame(), socket, Ingress(app))
    assert [ack["code"] for ack in socket.acks] == [500] and not events(app)
    monkeypatch.setattr(app.transcript, "add_in", original)
    await channel._frame(frame(), socket, Ingress(app))
    assert len(events(app)) == 1 and socket.acks[-1]["code"] == 200


async def test_slash_approval_runs_locally_once_and_never_queues_a_model_message(paired):
    app, channel, _ = paired
    [approval] = app.approvals.create("run", "main", [PendingCall("call", "shell", "git push")])
    app.approvals.save_run("run", "main", "STATE", {"profile": "full"})
    content = incoming(f"/approve {approval.id}")
    await channel._message(content, Ingress(app))
    await channel._message(content, Ingress(app))
    assert app.approvals.is_approved("run", "call")
    assert app.approvals.get(approval.id).decided_by == "owner:dingtalk"
    assert [e["kind"] for e in events(app)] == ["approval_response"]
    assert app.db.scalar("SELECT count(*) FROM outbox") == 1
    assert len(app.transcript.latest()) == 1


async def test_command_effect_dedupe_and_reply_roll_back_together(paired, monkeypatch):
    app, channel, _ = paired
    [approval] = app.approvals.create("run", "main", [PendingCall("call", "shell", "git push")])
    app.approvals.save_run("run", "main", "STATE", {"profile": "full"})
    original = app.outbox.enqueue

    def disk_full(**kw):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(app.outbox, "enqueue", disk_full)
    content = incoming(f"/approve {approval.id}")
    with pytest.raises(sqlite3.OperationalError):
        await channel._message(content, Ingress(app))
    assert not app.approvals.is_approved("run", "call")
    assert not events(app) and not app.transcript.latest()
    assert (
        app.db.kv_get(f"command:dingtalk:{message_key('offline-app', 'corp', 'message-1')}") is None
    )
    monkeypatch.setattr(app.outbox, "enqueue", original)
    await channel._message(content, Ingress(app))
    assert app.approvals.is_approved("run", "call") and len(events(app)) == 1


@pytest.mark.parametrize(
    "content", [incoming(msgtype="picture"), incoming(" "), incoming("x" * 20_001)]
)
async def test_unsupported_or_rejected_input_queues_one_durable_notice(paired, content):
    app, channel, fake = paired
    await channel._message(content, Ingress(app))
    await channel._message(content, Ingress(app))
    [row] = app.db.query("SELECT * FROM outbox")
    assert (
        row["session_key"] == "main" and row["channel"] == "dingtalk" and row["recipient"] == OWNER
    )
    assert not events(app) and not fake.requests


async def test_replies_and_proactive_notifications_use_only_current_approved_staff(paired):
    app, channel, fake = paired
    await channel.send(queue(app, "interactive"))
    await channel.send(queue(app, "proactive", recipient=None))
    assert [sent["userIds"] for sent in fake.sent()] == [["owner"], ["owner"]]
    app.pairing.revoke("dingtalk", OWNER)
    revoke_owner(app, "dingtalk", OWNER)
    with pytest.raises(ChannelError) as caught:
        await channel.send(queue(app, "after revoke"))
    assert caught.value.permanent and len(fake.sent()) == 2


@pytest.mark.parametrize(
    "recipient",
    [
        recipient_key("old-app", "corp", "owner"),
        "owner",
        '["offline-app", "corp", "owner"]',
    ],
)
async def test_stale_or_noncanonical_pairings_cannot_address_an_account(paired, recipient):
    app, channel, fake = paired
    app.pairing.allow("dingtalk", recipient)
    with pytest.raises(ChannelError) as caught:
        await channel.send(queue(app, recipient=recipient))
    assert caught.value.permanent and not fake.requests


async def test_unpaired_proactive_send_explains_how_to_pair(rig):
    app, channel, fake = rig
    with pytest.raises(ChannelError, match="pa channels approve dingtalk"):
        await channel.send(queue(app, recipient=None))
    assert not fake.requests


async def test_long_unicode_text_preserves_every_character_and_bounds_request_parts(paired):
    app, channel, fake = paired
    text = "你好🌐hello\n" * 1400
    await channel.send(queue(app, text))
    parts = [json.loads(request["msgParam"])["text"] for request in fake.sent()]
    assert len(parts) > 2 and "".join(parts) == text
    assert all(len(part.encode()) <= MESSAGE_CHUNK_BYTES for part in parts)


async def test_approval_notice_includes_real_slash_commands_without_claiming_buttons(paired):
    app, channel, fake = paired
    await channel.send(queue(app, kind="approval_request", extra={"approval_ids": [3, 4]}))
    text = json.loads(fake.sent()[0]["msgParam"])["text"]
    assert "/approve 3" in text and "/deny 3" in text and "/approvals" in text
    assert channel.capabilities.proactive and channel.capabilities.markdown
    assert not channel.capabilities.approval_buttons and not channel.capabilities.images_in


async def test_failure_after_one_chunk_is_unknown_and_not_a_definite_total_failure(paired):
    app, channel, fake = paired
    fake.script(SEND_PATH, {"processQueryKey": "accepted"}, httpx.Response(403, json={}))
    with pytest.raises(DeliveryUncertain, match="1 DingTalk message part"):
        await channel.send(queue(app, "x" * (MESSAGE_CHUNK_BYTES + 1)))
    assert len(fake.sent()) == 2


async def test_dispatcher_parks_uncertain_background_message_and_keeps_rate_limit_pending(paired):
    app, channel, fake = paired
    first = queue(app, "ambiguous")
    fake.script(SEND_PATH, httpx.ReadTimeout("offline"))
    report = await Dispatcher(app, {"dingtalk": channel}).deliver_once()
    assert report.unknown == [first.id] and app.outbox.get(first.id).status == "maybe_sent"
    second = queue(app, "limited")
    fake.script(SEND_PATH, {"processQueryKey": "partial", "flowControlledStaffIdList": ["owner"]})
    report = await Dispatcher(app, {"dingtalk": channel}).deliver_once()
    assert (
        report.retrying == [second.id]
        and app.outbox.get(second.id).not_before == app.clock.now() + 60
    )


@pytest.mark.parametrize(
    "name", ["lowercase", "A" * 65, "key with spaces", "literal-secret", "", "1KEY"]
)
def test_credentials_must_be_environment_variable_names(name):
    with pytest.raises(ValidationError):
        DingTalkConfig(client_secret_env=name)
