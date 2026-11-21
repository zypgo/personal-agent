"""Identity, invitation, revocation and physical data-root boundaries for collaboration."""

from __future__ import annotations

import json

import pytest

from personal_agent.channels.web.auth import TOUCH_EVERY_S, WebAuth
from personal_agent.collaboration.models import (
    OWNER_SPACE,
    AccessDenied,
    IdentityError,
    InvalidInvite,
)
from personal_agent.collaboration.scopes import derive_space_config, space_paths
from personal_agent.collaboration.store import INVITE_SECONDS, IdentityStore
from personal_agent.config import Config


@pytest.fixture
def identities(db, clock):
    return IdentityStore(db, clock)


def enroll(identities, *, role="member", name="Member"):
    project = identities.create_project("owner", "Research")
    invite = identities.create_member_invite("owner", project.id, role)
    return identities.redeem_member(invite.code, label=name)


def test_legacy_owner_is_private_and_not_an_implicit_global_reader(identities):
    member = enroll(identities)
    own = identities.personal_space(member.principal.id)
    assert identities.personal_space("owner").id == OWNER_SPACE
    with pytest.raises(AccessDenied):
        identities.require_access("owner", own.id)
    with pytest.raises(AccessDenied):
        identities.require_access(member.principal.id, OWNER_SPACE)
    assert {s.id for s in identities.spaces_for(member.principal.id)} == {own.id, member.space.id}


def test_a_personal_space_cannot_invite_someone(identities):
    with pytest.raises(IdentityError, match="Personal"):
        identities.create_member_invite("owner", OWNER_SPACE)


def test_member_invite_is_single_use_hashed_and_has_expiry(identities, db, clock):
    project = identities.create_project("owner", "Research")
    invite = identities.create_member_invite("owner", project.id)
    stored = json.dumps([tuple(row) for row in db.query("SELECT * FROM collaboration_invites")])
    assert invite.code not in stored
    assert len(invite.code) >= 40
    identities.redeem_member(invite.code)
    with pytest.raises(InvalidInvite):
        identities.redeem_member(invite.code)
    second = identities.create_member_invite("owner", project.id)
    clock.advance(INVITE_SECONDS)
    with pytest.raises(InvalidInvite):
        identities.redeem_member(second.code)


def test_existing_user_join_does_not_duplicate_identity_or_private_space(identities, db):
    member = enroll(identities)
    second = identities.create_project("owner", "Another project")
    count = db.scalar("SELECT COUNT(*) FROM principals")
    joined = identities.redeem_member(
        identities.create_member_invite("owner", second.id).code,
        existing_principal_id=member.principal.id,
    )
    assert not joined.created and joined.recovery_code is None
    assert joined.principal.id == member.principal.id
    assert db.scalar("SELECT COUNT(*) FROM principals") == count
    assert (
        db.scalar(
            "SELECT COUNT(*) FROM spaces WHERE owner_id = ? AND kind = 'personal'",
            (member.principal.id,),
        )
        == 1
    )


def test_accepting_an_invite_does_not_silently_change_existing_role(identities):
    member = enroll(identities, role="viewer")
    invitation = identities.create_member_invite("owner", member.space.id, "owner")
    identities.redeem_member(invitation.code, existing_principal_id=member.principal.id)
    assert identities.require_access(member.principal.id, member.space.id).role == "viewer"


def test_viewer_cannot_write_and_member_cannot_invite(identities):
    member = enroll(identities, role="viewer")
    assert identities.require_access(member.principal.id, member.space.id).role == "viewer"
    with pytest.raises(AccessDenied):
        identities.require_access(member.principal.id, member.space.id, write=True)
    with pytest.raises(AccessDenied):
        identities.create_member_invite(member.principal.id, member.space.id)


def test_invite_rechecks_issuer_membership_on_redemption(identities):
    member = enroll(identities, role="owner")
    pending = identities.create_member_invite(member.principal.id, member.space.id)
    identities.set_role("owner", member.space.id, member.principal.id, "member")
    with pytest.raises(InvalidInvite):
        identities.redeem_member(pending.code)


def test_last_project_owner_cannot_be_removed_or_demoted(identities):
    project = identities.create_project("owner", "Research")
    with pytest.raises(IdentityError, match="at least one"):
        identities.revoke_member("owner", project.id, "owner")
    with pytest.raises(IdentityError, match="at least one"):
        identities.set_role("owner", project.id, "owner", "member")


def test_membership_revocation_preserves_personal_session_and_other_projects(identities, db, clock):
    member = enroll(identities)
    own = identities.personal_space(member.principal.id)
    auth = WebAuth(db, clock)
    session = auth.create_session(member.principal.id)
    before = identities.require_access(member.principal.id, member.space.id)
    assert identities.revoke_member("owner", member.space.id, member.principal.id)
    assert auth.lookup(session.token) is not None
    assert identities.authorized(member.principal.id, own.id, write=True)
    assert not identities.authorized(member.principal.id, member.space.id, version=before.version)
    rejoin = identities.create_member_invite("owner", member.space.id)
    identities.redeem_member(rejoin.code, existing_principal_id=member.principal.id)
    assert not identities.authorized(member.principal.id, member.space.id, version=before.version)


def test_recovery_code_is_single_use_and_rotation_invalidates_the_previous_code(identities):
    member = enroll(identities)
    assert member.recovery_code
    replacement = identities.issue_recovery(member.principal.id)
    with pytest.raises(InvalidInvite):
        identities.recover(member.recovery_code)
    assert identities.recover(replacement) == member.principal.id
    with pytest.raises(InvalidInvite):
        identities.recover(replacement)


def test_disabled_identity_cannot_use_invites_recovery_or_sessions(identities, db, clock):
    member = enroll(identities)
    auth = WebAuth(db, clock)
    session = auth.create_session(member.principal.id)
    device = identities.create_device_invite(member.principal.id)
    identities.disable_principal("owner", member.principal.id)
    assert auth.lookup(session.token) is None
    assert auth.redeem(device.code) is None
    with pytest.raises(InvalidInvite):
        identities.recover(member.recovery_code)
    with pytest.raises(AccessDenied):
        identities.spaces_for(member.principal.id)


def test_unprivileged_user_cannot_disable_other_accounts(identities):
    member = enroll(identities)
    with pytest.raises(AccessDenied):
        identities.disable_principal(member.principal.id, "owner")
    with pytest.raises(IdentityError):
        identities.disable_principal("owner", "owner")


def test_device_pairing_and_sliding_session_preserve_principal(identities, db, clock):
    member = enroll(identities)
    auth = WebAuth(db, clock)
    device = identities.create_device_invite(member.principal.id)
    paired = auth.redeem(device.code)
    assert paired is not None and paired.session.principal_id == member.principal.id
    assert auth.redeem(device.code) is None
    clock.advance(TOUCH_EVERY_S + 1)
    renewed = auth.lookup(paired.token)
    assert renewed.principal_id == member.principal.id
    assert renewed.expires_at > paired.session.expires_at


def test_device_management_cannot_revoke_someone_elses_session(identities, db, clock):
    member = enroll(identities)
    auth = WebAuth(db, clock)
    owner = auth.create_session("owner")
    own = auth.create_session(member.principal.id)
    assert not auth.revoke(owner.session.id, principal_id=member.principal.id)
    assert len(auth.sessions(principal_id=member.principal.id)) == 1
    assert auth.revoke_all(principal_id=member.principal.id) == 1
    assert auth.lookup(own.token) is None
    assert auth.lookup(owner.token) is not None


def test_forged_device_target_is_not_an_admin_grant(identities, db, clock):
    member = enroll(identities)
    auth = WebAuth(db, clock)
    with pytest.raises(AccessDenied):
        auth.invite(principal_id="owner", issued_by=member.principal.id)
    invite = auth.invite(principal_id=member.principal.id)
    db.execute("UPDATE web_invites SET principal_id = 'owner'")
    assert auth.redeem(invite.code) is None


def test_enrollment_and_session_creation_can_roll_back_together(identities, db):
    project = identities.create_project("owner", "Research")
    invite = identities.create_member_invite("owner", project.id)
    before = db.scalar("SELECT COUNT(*) FROM principals")
    with pytest.raises(RuntimeError), db.tx():
        identities.redeem_member(invite.code)
        raise RuntimeError("session creation failed")
    assert db.scalar("SELECT COUNT(*) FROM principals") == before
    assert identities.redeem_member(invite.code).created


def test_resource_grants_require_admin_and_current_membership(identities, clock):
    member = enroll(identities)
    with pytest.raises(AccessDenied):
        identities.grant_resource(
            member.principal.id, member.space.id, "node", "node-one", ["execute"]
        )
    grant = identities.grant_resource(
        "owner", member.space.id, "node", "node-one", ["execute"], expires_at=clock.now() + 30
    )
    assert (
        identities.require_resource(
            member.principal.id, member.space.id, "node", "node-one", "execute"
        ).id
        == grant.id
    )
    with pytest.raises(AccessDenied):
        identities.require_resource(
            member.principal.id, member.space.id, "node", "node-one", "admin"
        )
    assert identities.revoke_member("owner", member.space.id, member.principal.id)
    with pytest.raises(AccessDenied):
        identities.require_resource(
            member.principal.id, member.space.id, "node", "node-one", "execute"
        )


def test_resource_grant_rotation_fences_older_plan(identities):
    member = enroll(identities)
    first = identities.grant_resource("owner", member.space.id, "node", "one", ["execute"])
    next_grant = identities.grant_resource("owner", member.space.id, "node", "one", ["execute"])
    assert next_grant.id == first.id and next_grant.version > first.version
    with pytest.raises(AccessDenied):
        identities.require_resource(
            member.principal.id, member.space.id, "node", "one", "execute", version=first.version
        )


def test_channel_binding_is_single_use_and_specific_to_account_and_space(identities):
    member = enroll(identities)
    invite = identities.create_channel_binding(member.principal.id, member.space.id)
    bound = identities.bind_channel(invite.code, "telegram", "bot-one", "user-one", "chat-one")
    assert bound.principal_id == member.principal.id and bound.space_id == member.space.id
    assert identities.lookup_channel("telegram", "bot-one", "user-one") == bound
    assert identities.lookup_channel("telegram", "bot-two", "user-one") is None
    with pytest.raises(InvalidInvite):
        identities.bind_channel(invite.code, "telegram", "bot-one", "user-two", "chat-two")


def test_channel_binding_cannot_survive_revocation_or_rejoin(identities):
    member = enroll(identities)
    invite = identities.create_channel_binding(member.principal.id, member.space.id)
    identities.bind_channel(invite.code, "telegram", "bot", "sender", "recipient")
    pending = identities.create_channel_binding(member.principal.id, member.space.id)
    identities.revoke_member("owner", member.space.id, member.principal.id)
    assert identities.lookup_channel("telegram", "bot", "sender") is None
    with pytest.raises(InvalidInvite):
        identities.bind_channel(pending.code, "telegram", "bot", "another", "recipient")
    identities.redeem_member(
        identities.create_member_invite("owner", member.space.id).code,
        existing_principal_id=member.principal.id,
    )
    assert identities.lookup_channel("telegram", "bot", "sender") is None


def test_a_post_migration_pairing_is_not_implicitly_the_owner(identities, db):
    db.execute(
        "INSERT INTO senders(channel, sender, approved_at) VALUES ('telegram', 'new-user', 0)"
    )
    assert not identities.legacy_owner_sender("telegram", "new-user")


@pytest.mark.parametrize("action", ["remove", "demote"])
def test_project_default_actor_moves_to_remaining_owner(identities, action):
    member = enroll(identities)
    identities.set_role("owner", member.space.id, member.principal.id, "owner")
    if action == "remove":
        identities.revoke_member(member.principal.id, member.space.id, "owner")
    else:
        identities.set_role(member.principal.id, member.space.id, "owner", "member")
    assert identities.space(member.space.id).owner_id == member.principal.id
    with pytest.raises(IdentityError, match="at least one"):
        identities.revoke_member(member.principal.id, member.space.id, member.principal.id)


def test_disabling_identity_requires_project_handoff_and_refreshes_default_owner(identities):
    member = enroll(identities)
    project = identities.create_project(member.principal.id, "Member project")
    with pytest.raises(IdentityError, match="at least one"):
        identities.disable_principal("owner", member.principal.id)
    invitation = identities.create_member_invite(member.principal.id, project.id, "owner")
    identities.redeem_member(invitation.code, existing_principal_id="owner")
    assert identities.disable_principal("owner", member.principal.id)
    assert identities.space(project.id).owner_id == "owner"


def test_new_space_paths_do_not_alias_owner_data(paths, identities):
    member = enroll(identities)
    own = identities.personal_space(member.principal.id)
    project_paths = space_paths(paths, member.space.id)
    private_paths = space_paths(paths, own.id)
    assert project_paths.home != private_paths.home != paths.home
    assert project_paths.home.parent == paths.home / "spaces"
    assert space_paths(paths, OWNER_SPACE) == paths
    assert project_paths.home.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "bad", ["../owner", "/tmp/other", "s_../owner", "personal-other", "s_" + "f" * 33]
)
def test_space_paths_reject_user_paths(paths, bad):
    with pytest.raises(IdentityError):
        space_paths(paths, bad)


def test_space_paths_refuse_symbolic_link_roots(paths, tmp_path):
    other = tmp_path / "outside"
    other.mkdir()
    (paths.home / "spaces").symlink_to(other, target_is_directory=True)
    with pytest.raises(IdentityError):
        space_paths(paths, "s_" + "a" * 32)


def test_space_configuration_does_not_inherit_owner_privileges():
    root = Config.model_validate(
        {
            "agent": {"owner_name": "Private identity", "name": "Private assistant"},
            "secrets": {"CUSTOM": {"env": "TEST_PRIVATE_KEY", "hosts": ["service.example"]}},
            "net": {"allow_private": ["192.168.1.0/24"]},
            "sandbox": {"extra_read": ["/private"], "extra_write": ["/private"]},
            "plugins": {
                "enabled": ["private_plugin"],
                "settings": {"private_plugin": {"personal": "value"}},
            },
            "policy": {"mode": "autonomous", "allow": ["*"]},
        }
    )
    child = derive_space_config(root)
    assert child.agent.owner_name is None and child.agent.name == "Assistant"
    assert not child.tools.shell and not child.skills.enabled
    assert not child.secrets and not child.mcp.enabled and not child.mcp.servers
    assert not child.plugins.enabled and not child.plugins.settings
    assert (
        not child.net.allow_private
        and not child.sandbox.extra_read
        and not child.sandbox.extra_write
    )
    assert child.policy.mode == "supervised" and not child.policy.allow
    assert child.notify.channel == "web" and not child.web.enabled
    assert root.agent.owner_name == "Private identity" and root.secrets
