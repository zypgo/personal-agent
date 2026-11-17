"""Which person and space a chat message belongs to, and where a queued reply may still go.

`test_v3_channel_identity.py` runs this through the Telegram and DingTalk channels. These tests
call the routing itself, so each rule is decided on its own: an account is bound to one person,
one space and one chat, and nothing queued before a change may follow the change.
"""

from __future__ import annotations

import pytest

from personal_agent.channels.base import ChannelError
from personal_agent.channels.ingress import Ingress
from personal_agent.collaboration.channel_routing import incoming_route, validate_delivery
from personal_agent.collaboration.models import OWNER_ID, OWNER_SPACE


def member(root, project=None):
    """A collaborator in a project (a new one, unless it is given)."""
    identities = root.identities
    project = project or identities.create_project(OWNER_ID, "Shared project")
    invite = identities.create_member_invite(OWNER_ID, project.id)
    enrollment = identities.redeem_member(invite.code, label="Collaborator")
    return enrollment.principal.id, project.id


def bound(root, principal, space, *, sender="7", recipient="7"):
    code = root.identities.create_channel_binding(principal, space).code
    return root.identities.bind_channel(code, "telegram", "bot", sender, recipient)


def old_owner_chat(root, sender="7"):
    """A chat that was the owner's before accounts could be bound to people."""
    root.db.execute(
        "INSERT INTO legacy_channel_owners(channel, sender, approved_at) VALUES ('telegram', ?, 0)",
        (sender,),
    )
    root.pairing.allow("telegram", sender)


def route(app, *, sender="7", recipient="7"):
    return incoming_route(
        app,
        Ingress(app),
        channel="telegram",
        account="bot",
        sender=sender,
        recipient=recipient,
        legacy_sender=sender,
    )


def reply(app, principal, *, recipient="7", versions=True):
    extra = {"principal_id": principal}
    if versions:
        extra.update(app.actor_snapshot(principal))
    message_id = app.outbox.enqueue(
        session_key="main", channel="telegram", recipient=recipient, text="answer", extra=extra
    )
    return app.outbox.get(message_id)


def delivery(app, message, *, sender="7", recipient="7"):
    return validate_delivery(
        app,
        message,
        channel="telegram",
        account="bot",
        sender=sender,
        recipient=recipient,
        legacy_sender=sender,
    )


# ---- incoming messages ----


def test_a_bound_account_is_routed_only_from_the_chat_it_was_bound_in(make_app):
    root = make_app()
    person, project = member(root)
    old_owner_chat(root)  # the same sender was also the owner's chat in the old days
    bound(root, person, project)
    routed = route(root)
    assert routed and routed.principal_id == person and not routed.legacy
    assert route(root, recipient="99") is None


def test_an_old_owner_chat_is_not_the_owner_once_the_account_is_bound_to_someone(make_app):
    root = make_app()
    person, project = member(root)
    old_owner_chat(root)
    assert route(root).legacy
    bound(root, person, project)
    assert not route(root).legacy
    assert root.identities.revoke_channel(OWNER_ID, "telegram", "bot", "7")
    assert route(root) is None


def test_an_old_owner_chat_stops_working_when_the_owners_access_is_withdrawn(make_app):
    root = make_app()
    old_owner_chat(root)
    assert route(root).legacy
    root.db.execute("UPDATE principals SET disabled_at = 1 WHERE id = ?", (OWNER_ID,))
    assert route(root) is None


def test_an_app_without_a_space_manager_serves_only_its_own_space(make_app):
    root = make_app()
    person, project = member(root)
    bound(root, person, project, sender="8")
    bound(root, OWNER_ID, OWNER_SPACE, sender="9")
    manager, root.collaboration = root.collaboration, None
    try:
        assert route(root, sender="8") is None
        mine = route(root, sender="9")
        assert mine and mine.app is root and mine.principal_id == OWNER_ID
    finally:
        root.collaboration = manager


# ---- replies that were queued before something changed ----


def test_a_reply_is_not_sent_for_a_person_who_may_no_longer_write(make_app):
    root = make_app()
    person, project = member(root)
    bound(root, person, project)
    shared = root.collaboration.get(project)
    message = reply(shared, person, versions=False)
    assert delivery(shared, message).principal_id == person
    root.identities.set_role(OWNER_ID, project, person, "viewer")
    with pytest.raises(ChannelError, match="authorization changed"):
        delivery(shared, message)


def test_a_reply_does_not_follow_an_account_to_another_person(make_app):
    root = make_app()
    first, project = member(root)
    second, _ = member(root, root.identities.space(project))
    bound(root, first, project)
    shared = root.collaboration.get(project)
    message = reply(shared, first)
    assert delivery(shared, message).principal_id == first
    bound(root, second, project)  # the same chat account now belongs to somebody else
    with pytest.raises(ChannelError, match="changed"):
        delivery(shared, message)


def test_a_reply_does_not_follow_a_person_to_another_chat(make_app):
    root = make_app()
    person, project = member(root)
    bound(root, person, project)
    shared = root.collaboration.get(project)
    message = reply(shared, person)
    assert delivery(shared, message).recipient_id == "7"
    bound(root, person, project, recipient="42")  # the same account now talks from another chat
    with pytest.raises(ChannelError, match="changed"):
        delivery(shared, message)
