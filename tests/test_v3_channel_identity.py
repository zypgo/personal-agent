from __future__ import annotations

import json

import pytest

import personal_agent.db as database_module
import telegram_fake as tg
from personal_agent.channels.base import ChannelError
from personal_agent.channels.dingtalk.api import DingTalkApi
from personal_agent.channels.dingtalk.channel import DingTalkChannel, account_key, recipient_key
from personal_agent.channels.dingtalk.config import DingTalkConfig
from personal_agent.channels.ingress import Ingress
from personal_agent.channels.telegram.api import TelegramApi
from personal_agent.channels.telegram.channel import TelegramChannel, approval_markup
from personal_agent.collaboration.models import OWNER_ID, OWNER_SPACE
from personal_agent.collaboration.schema import migrate as identity_migration
from personal_agent.config import Config, TelegramConfig
from personal_agent.core.approvals import PendingCall
from personal_agent.paths import Paths
from telegram_fake import TOKEN, Fail, FakeTelegram
from test_dingtalk_api import FakeDingTalk
from test_dingtalk_channel import incoming
from test_ingress import events


def member(app):
    project = app.identities.create_project(OWNER_ID, "Shared project")
    invite = app.identities.create_member_invite(OWNER_ID, project.id)
    return app.identities.redeem_member(invite.code, label="Collaborator")


def private_app(app, principal):
    return app.collaboration.get(app.identities.personal_space(principal).id)


def queue(app, channel, recipient, principal):
    message_id = app.outbox.enqueue(
        session_key="main",
        channel=channel,
        recipient=recipient,
        text="Space-local answer",
        extra={"principal_id": principal, **app.actor_snapshot(principal)},
    )
    return app.outbox.get(message_id)


@pytest.fixture
async def telegram(make_app):
    fake = FakeTelegram()
    app = make_app(config=Config(telegram=TelegramConfig(enabled=True)))
    async with fake.client() as client:
        yield app, TelegramChannel(app, api=TelegramApi(TOKEN, client=client)), fake


@pytest.fixture
async def dingtalk(make_app):
    fake = FakeDingTalk()
    app = make_app(config=Config(dingtalk=DingTalkConfig(enabled=True)))
    async with fake.client() as client:
        yield (
            app,
            DingTalkChannel(app, api=DingTalkApi("offline-app", "offline-secret", client=client)),
            fake,
        )


async def test_telegram_binding_routes_to_private_database_and_stays_out_of_history(telegram):
    app, channel, fake = telegram
    enrollment = member(app)
    private = private_app(app, enrollment.principal.id)
    code = app.identities.create_channel_binding(enrollment.principal.id, private.space_id).code
    await channel._handle(tg.text(1, f"/bind {code}", sender=17), Ingress(app))
    assert not events(app) and not events(private)
    assert not app.transcript.latest() and not private.transcript.latest()
    assert code not in repr(fake.texts())
    await channel._handle(tg.text(2, "private note", sender=17), Ingress(app))
    [event] = events(private)
    assert event["payload"]["principal_id"] == enrollment.principal.id
    assert event["payload"]["membership_version"] == 1
    assert event["payload"]["text"] == "private note"
    assert not events(app)
    await channel.for_space(private).send(queue(private, "telegram", "17", enrollment.principal.id))
    assert fake.sent()[-1]["chat_id"] == "17"


async def test_postmigration_pairing_allow_does_not_grant_owner_telegram_access(telegram):
    app, channel, fake = telegram
    app.pairing.allow("telegram", "7")
    await channel._handle(tg.text(1, "owner secrets please"), Ingress(app))
    assert not events(app)
    assert "/bind" in fake.texts()[0]
    with pytest.raises(ChannelError):
        await channel.send(queue(app, "telegram", "7", OWNER_ID))


async def test_forwarded_bind_code_is_not_consumed(telegram):
    app, channel, _ = telegram
    code = app.identities.create_channel_binding(OWNER_ID, OWNER_SPACE).code
    await channel._handle(
        tg.text(1, f"/bind {code}", forward_origin={"type": "user"}), Ingress(app)
    )
    assert app.identities.lookup_channel("telegram", "1", "7") is None
    await channel._handle(tg.text(2, f"/bind {code}"), Ingress(app))
    assert app.identities.lookup_channel("telegram", "1", "7") is not None
    assert not events(app)


async def test_rebinding_blocks_old_space_delivery_and_approval_buttons(telegram):
    app, channel, fake = telegram
    enrollment = member(app)
    private = private_app(app, enrollment.principal.id)
    project = app.collaboration.get(enrollment.space.id)
    code = app.identities.create_channel_binding(enrollment.principal.id, private.space_id).code
    await channel._handle(tg.text(1, f"/bind {code}", sender=17), Ingress(app))
    old_message = queue(private, "telegram", "17", enrollment.principal.id)
    code = app.identities.create_channel_binding(enrollment.principal.id, project.space_id).code
    await channel._handle(tg.text(2, f"/bind {code}", sender=17), Ingress(app))
    with pytest.raises(ChannelError, match="space changed"):
        await channel.for_space(private).send(old_message)
    before = len(fake.sent())
    buttons = approval_markup([1], space_id=private.space_id, membership_version=1)
    await channel._handle(
        tg.callback(3, buttons["inline_keyboard"][0][0]["callback_data"], sender=17), Ingress(app)
    )
    assert len(fake.sent()) == before
    assert not events(app)


async def test_project_revocation_invalidates_ingress_and_queued_telegram_reply(telegram):
    app, channel, _ = telegram
    enrollment = member(app)
    project = app.collaboration.get(enrollment.space.id)
    code = app.identities.create_channel_binding(enrollment.principal.id, project.space_id).code
    await channel._handle(tg.text(1, f"/bind {code}", sender=17), Ingress(app))
    message = queue(project, "telegram", "17", enrollment.principal.id)
    app.identities.revoke_member(OWNER_ID, project.space_id, enrollment.principal.id)
    await channel._handle(tg.text(2, "after removal", sender=17), Ingress(app))
    assert not events(project)
    with pytest.raises(ChannelError, match="authorization changed"):
        await channel.for_space(project).send(message)


async def test_owner_cannot_approve_an_old_scoped_button_after_rebinding(telegram):
    app, channel, fake = telegram
    project = app.collaboration.get(app.identities.create_project(OWNER_ID, "Project").id)
    [old] = app.approvals.create("old", "main", [PendingCall("c", "write", "Old owner approval")])
    [current] = project.approvals.create(
        "current", "main", [PendingCall("c", "write", "Project approval")]
    )
    assert old.id == current.id
    code = app.identities.create_channel_binding(OWNER_ID, project.space_id).code
    await channel._handle(tg.text(1, f"/bind {code}"), Ingress(app))
    callback = approval_markup([old.id], space_id=OWNER_SPACE, membership_version=1)
    await channel._handle(
        tg.callback(2, callback["inline_keyboard"][0][0]["callback_data"]), Ingress(app)
    )
    assert project.approvals.get(current.id).status == "pending"
    assert "another space" in fake.params("answerCallbackQuery")[-1]["text"]


async def test_legacy_owner_migration_preserves_only_existing_sender_and_never_falls_back(
    make_app,
    tmp_path,
    monkeypatch,
):
    paths = Paths.resolve(tmp_path / "pa-home")
    paths.ensure()
    old_version = database_module.MIGRATIONS.index(identity_migration)
    with monkeypatch.context() as temporary:
        temporary.setattr(database_module, "SCHEMA_VERSION", old_version)
        old = database_module.open_database(paths.db_file)
        old.execute("INSERT INTO senders(channel, sender, approved_at) VALUES ('telegram','7',0)")
        old.close()
    app = make_app(config=Config(telegram=TelegramConfig(enabled=True)))
    fake = FakeTelegram()
    async with fake.client() as client:
        channel = TelegramChannel(app, api=TelegramApi(TOKEN, client=client))
        await channel._handle(tg.text(1, "legacy message"), Ingress(app))
        assert len(events(app)) == 1
        await channel.send(queue(app, "telegram", "7", OWNER_ID))
        code = app.identities.create_channel_binding(OWNER_ID, OWNER_SPACE).code
        await channel._handle(tg.text(2, f"/bind {code}"), Ingress(app))
        app.identities.revoke_channel(OWNER_ID, "telegram", "1", "7")
        await channel._handle(tg.text(3, "cannot become legacy owner again"), Ingress(app))
        assert len(events(app)) == 1
        with pytest.raises(ChannelError):
            await channel.send(queue(app, "telegram", "7", OWNER_ID))


async def test_dingtalk_binding_routes_and_replies_using_organization_scoped_identity(dingtalk):
    app, channel, fake = dingtalk
    enrollment = member(app)
    private = private_app(app, enrollment.principal.id)
    code = app.identities.create_channel_binding(enrollment.principal.id, private.space_id).code
    await channel._message(incoming(f"/bind {code}", senderStaffId="collaborator"), Ingress(app))
    await channel._message(
        incoming("hello", senderStaffId="collaborator", message_id="m2"), Ingress(app)
    )
    assert not events(app)
    [event] = events(private)
    assert event["payload"]["principal_id"] == enrollment.principal.id
    recipient = recipient_key("offline-app", "corp", "collaborator")
    await channel.for_space(private).send(
        queue(private, "dingtalk", recipient, enrollment.principal.id)
    )
    assert fake.sent()[-1]["userIds"] == ["collaborator"]
    assert code not in repr([dict(row) for row in private.db.query("SELECT * FROM transcript")])
    await channel._message(
        incoming(
            "foreign",
            senderStaffId="collaborator",
            senderCorpId="foreign",
            chatbotCorpId="foreign",
            message_id="m3",
        ),
        Ingress(app),
    )
    assert len(events(private)) == 1


async def test_dingtalk_raw_pairing_and_viewer_cannot_enter_owner_scope(dingtalk):
    app, channel, fake = dingtalk
    recipient = recipient_key("offline-app", "corp", "owner")
    app.pairing.allow("dingtalk", recipient)
    await channel._message(incoming("private owner request"), Ingress(app))
    assert not events(app)
    assert "/bind" in json.loads(fake.sent()[0]["msgParam"])["content"]
    enrollment = member(app)
    code = app.identities.create_channel_binding(enrollment.principal.id, enrollment.space.id).code
    app.identities.set_role(OWNER_ID, enrollment.space.id, enrollment.principal.id, "viewer")
    await channel._message(incoming(f"/bind {code}", message_id="m2"), Ingress(app))
    assert (
        app.identities.lookup_channel("dingtalk", account_key("offline-app", "corp"), "owner")
        is None
    )


async def test_dingtalk_member_cannot_approve_space_owner_tools(dingtalk):
    app, channel, fake = dingtalk
    enrollment = member(app)
    project = app.collaboration.get(enrollment.space.id)
    code = app.identities.create_channel_binding(enrollment.principal.id, project.space_id).code
    await channel._message(incoming(f"/bind {code}"), Ingress(app))
    [approval] = project.approvals.create("r", "main", [PendingCall("c", "write", "Needs owner")])
    await channel._message(incoming(f"/approve {approval.id}", message_id="m2"), Ingress(app))
    assert project.approvals.get(approval.id).status == "pending"
    [notice] = project.outbox.recent()
    assert "Only this space's owner" in notice.text
    assert notice.payload["principal_id"] == enrollment.principal.id
    assert not app.outbox.recent()
    await channel.for_space(project).send(notice)
    assert "Only this space's owner" in json.loads(fake.sent()[-1]["msgParam"])["text"]


async def test_telegram_retry_rechecks_binding_after_backoff(telegram):
    app, channel, fake = telegram
    code = app.identities.create_channel_binding(OWNER_ID, OWNER_SPACE).code
    await channel._handle(tg.text(1, f"/bind {code}"), Ingress(app))
    fake.script("sendMessage", Fail(429, "Slow down", retry_after=1))
    before = fake.count("sendMessage")

    async def revoke_while_waiting(_seconds):
        app.identities.revoke_channel(OWNER_ID, "telegram", "1", "7")

    channel._sleep = revoke_while_waiting
    with pytest.raises(ChannelError) as error:
        await channel.send(queue(app, "telegram", "7", OWNER_ID))
    assert error.value.permanent
    assert fake.count("sendMessage") == before + 1


async def test_project_coowner_approval_records_actual_identity(telegram):
    app, channel, _ = telegram
    enrollment = member(app)
    actor = enrollment.principal.id
    project = app.collaboration.get(enrollment.space.id)
    membership = app.identities.set_role(OWNER_ID, project.space_id, actor, "owner")
    code = app.identities.create_channel_binding(actor, project.space_id).code
    await channel._handle(tg.text(1, f"/bind {code}", sender=17), Ingress(app))
    [approval] = project.approvals.create("r", "main", [PendingCall("c", "write", "Review")])
    data = approval_markup(
        [approval.id], space_id=project.space_id, membership_version=membership.version
    )
    await channel._handle(
        tg.callback(2, data["inline_keyboard"][0][0]["callback_data"], sender=17), Ingress(app)
    )
    decided = project.approvals.get(approval.id)
    assert decided.status == "approved"
    assert decided.decided_by == f"{actor}:telegram"


def test_cli_owner_allow_requires_bot_scope_and_revoke_removes_explicit_binding(wired, monkeypatch):
    from helpers import invoke

    app = wired()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    refused = invoke("channels", "allow", "telegram", "7")
    assert refused.exit_code == 1
    assert "/bind CODE" in refused.output
    assert not app.pairing.is_approved("telegram", "7")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    accepted = invoke("channels", "allow", "telegram", "7")
    assert accepted.exit_code == 0 and TOKEN not in accepted.output
    account = TOKEN.partition(":")[0]
    assert app.identities.lookup_channel("telegram", account, "7").principal_id == OWNER_ID
    # Revocation works even if the token has since been removed from local configuration.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert invoke("channels", "revoke", "telegram", "7").exit_code == 0
    assert app.identities.lookup_channel("telegram", account, "7") is None
