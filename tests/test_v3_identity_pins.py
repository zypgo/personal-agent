"""What a stored authorization is pinned to, and what moves the pin.

Invitations, sessions, channel bindings and tasks all remember the version of the person's
authorization and of their membership at the moment they were made. Anything that changes who a
person is allowed to be in a space moves the version, and whatever was made under the old one stops
working. Each test moves one of those versions and looks at what stops.
"""

from __future__ import annotations

import pytest

from personal_agent.channels.web.auth import WebAuth
from personal_agent.collaboration.models import OWNER_SPACE, AccessDenied, InvalidInvite
from personal_agent.collaboration.store import IdentityStore


@pytest.fixture
def identities(db, clock):
    return IdentityStore(db, clock)


def enroll(identities, *, role="member", name="Member"):
    project = identities.create_project("owner", "Research")
    invite = identities.create_member_invite("owner", project.id, role)
    return identities.redeem_member(invite.code, label=name)


def authorization_changes(db, principal_id="owner"):
    """What restoring a backup does to everybody: their authorization is a new generation."""
    with db.tx():
        db.execute(
            "UPDATE principals SET authz_version = authz_version + 1 WHERE id = ?", (principal_id,)
        )


def membership_version(db, space_id, principal_id):
    return db.scalar(
        "SELECT version FROM memberships WHERE space_id = ? AND principal_id = ?",
        (space_id, principal_id),
    )


def test_a_pinned_principal_version_stops_matching_when_the_authorization_changes(identities, db):
    assert identities.authorized("owner", OWNER_SPACE, principal_version=1)
    authorization_changes(db)
    assert not identities.authorized("owner", OWNER_SPACE, principal_version=1)
    assert identities.authorized("owner", OWNER_SPACE, principal_version=2)
    assert identities.authorized("owner", OWNER_SPACE)


@pytest.mark.parametrize("kind", ["member", "device", "channel"])
def test_an_invitation_ends_with_the_authorization_of_the_one_who_made_it(identities, db, kind):
    project = identities.create_project("owner", "Research")
    code = {
        "member": lambda: identities.create_member_invite("owner", project.id).code,
        "device": lambda: identities.create_device_invite("owner").code,
        "channel": lambda: identities.create_channel_binding("owner", project.id).code,
    }[kind]()
    authorization_changes(db)
    with pytest.raises(InvalidInvite):
        if kind == "member":
            identities.redeem_member(code)
        elif kind == "device":
            identities.redeem_device(code)
        else:
            identities.bind_channel(code, "telegram", "bot", "sender", "chat")


def test_an_invitation_ends_when_its_issuer_is_given_the_same_role_again(identities):
    # The role is the same afterwards, the standing is not: the membership is a new generation,
    # and an invitation made under the old one is not something this membership agreed to.
    second = enroll(identities, role="owner")
    pending = identities.create_member_invite(second.principal.id, second.space.id)
    identities.set_role("owner", second.space.id, second.principal.id, "owner")
    with pytest.raises(InvalidInvite):
        identities.redeem_member(pending.code)


def test_every_change_of_a_standing_in_a_project_moves_its_version(identities, db):
    member = enroll(identities)
    space, person = member.space.id, member.principal.id
    first = membership_version(db, space, person)

    identities.set_role("owner", space, person, "viewer")
    after_role = membership_version(db, space, person)
    assert after_role == first + 1

    assert identities.revoke_member("owner", space, person)
    after_removal = membership_version(db, space, person)
    assert after_removal == after_role + 1

    identities.redeem_member(
        identities.create_member_invite("owner", space).code, existing_principal_id=person
    )
    assert membership_version(db, space, person) == after_removal + 1


def test_disabling_a_person_closes_their_sessions_and_moves_their_authorization(
    identities, db, clock
):
    member = enroll(identities)
    auth = WebAuth(db, clock)
    auth.create_session(member.principal.id)
    other = auth.create_session("owner")
    before = db.scalar("SELECT authz_version FROM principals WHERE id = ?", (member.principal.id,))
    assert identities.disable_principal("owner", member.principal.id)
    assert (
        db.scalar(
            "SELECT COUNT(*) FROM web_sessions WHERE principal_id = ? AND revoked_at IS NULL",
            (member.principal.id,),
        )
        == 0
    )
    assert (
        db.scalar("SELECT authz_version FROM principals WHERE id = ?", (member.principal.id,))
        == before + 1
    )
    assert auth.lookup(other.token) is not None


def test_removing_a_member_ends_the_grants_made_to_them_for_good(identities, db):
    member = enroll(identities)
    space, person = member.space.id, member.principal.id
    grant = identities.grant_resource(
        "owner", space, "node", "node-one", ["execute"], principal_id=person
    )
    assert identities.require_resource(person, space, "node", "node-one", "execute")
    identities.revoke_member("owner", space, person)
    assert db.scalar("SELECT revoked_at IS NOT NULL FROM resource_grants WHERE id = ?", (grant.id,))

    # Joining again later is a new membership: the old grant does not come back with it.
    identities.redeem_member(
        identities.create_member_invite("owner", space).code, existing_principal_id=person
    )
    with pytest.raises(AccessDenied):
        identities.require_resource(person, space, "node", "node-one", "execute")


def test_a_resource_grant_is_for_people_who_may_write_and_ends_when_it_expires(identities, clock):
    viewer = enroll(identities, role="viewer")
    space = viewer.space.id
    identities.grant_resource(
        "owner", space, "node", "node-one", ["execute"], expires_at=clock.now() + 100
    )
    with pytest.raises(AccessDenied):
        identities.require_resource(viewer.principal.id, space, "node", "node-one", "execute")
    assert identities.require_resource("owner", space, "node", "node-one", "execute")
    clock.advance(101)
    with pytest.raises(AccessDenied):
        identities.require_resource("owner", space, "node", "node-one", "execute")
