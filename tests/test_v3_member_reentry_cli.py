"""Trusted local member re-entry preserves the identity and its existing permissions."""

from __future__ import annotations

import pytest

from helpers import invoke
from personal_agent.collaboration.models import AccessDenied
from personal_agent.config import Config, WebConfig
from test_cli_web import PUBLIC, link_in
from web_fake import Web


@pytest.fixture
def app(wired):
    return wired(config=Config(web=WebConfig(enabled=True, public_url=PUBLIC)))


def member(app, role="member", label="Existing member"):
    identities = app.identities
    space = identities.create_project("owner", "Project")
    invitation = identities.create_member_invite("owner", space.id, role)
    return identities.redeem_member(invitation.code, label=label)


def authority_rows(app):
    return {
        table: [tuple(row) for row in app.db.query(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("principals", "spaces", "memberships")
    }


@pytest.mark.parametrize("role", ["member", "viewer"])
def test_existing_principal_pair_link_redeems_through_normal_web_flow_without_escalation(app, role):
    enrolled = member(app, role)
    principal_id = enrolled.principal.id
    before = authority_rows(app)
    result = invoke("web", "pair", "--principal", principal_id, "--no-qr")
    assert result.exit_code == 0, result.output
    address, code = link_in(result.output)
    assert address == PUBLIC + "/"
    invitation = app.db.one("SELECT * FROM collaboration_invites WHERE kind='device'")
    assert invitation["target_principal_id"] == principal_id
    assert invitation["issuer_version"] == enrolled.principal.authz_version
    assert invitation["space_id"] is None and invitation["role"] is None
    assert code not in str(dict(invitation))
    assert app.db.scalar("SELECT COUNT(*) FROM web_invites") == 0
    audit = app.db.one(
        "SELECT * FROM collaboration_audit WHERE action='admin_device_invite_created'"
    )
    assert audit["actor_id"] == "owner" and audit["subject_id"] == principal_id

    web = Web(app, public_url=PUBLIC)
    try:
        paired = web.client.post(
            "/api/pair", headers=web.write, json={"code": code, "principal_id": "owner"}
        )
        assert paired.status_code == 200, paired.text
        session = app.web_auth.lookup(web.token())
        assert session is not None and session.principal_id == principal_id
        me = web.client.get("/api/v3/me").json()
        assert me["principal"]["id"] == principal_id
        assert not me["principal"]["is_admin"]
        assert web.client.get("/api/v3/me", params={"space": "personal-owner"}).status_code == 403
        assert app.web_auth.redeem(code) is None
    finally:
        web.client.close()
    assert authority_rows(app) == before
    assert app.identities.require_access(principal_id, enrolled.space.id).role == role


@pytest.mark.parametrize("target", ["missing", "disabled"])
def test_pair_rejects_missing_or_disabled_principal_without_creating_an_invite(app, target):
    principal_id = "u_missing"
    if target == "disabled":
        principal_id = member(app).principal.id
        app.identities.disable_principal("owner", principal_id)
    before = authority_rows(app)
    invites = app.db.scalar("SELECT COUNT(*) FROM collaboration_invites")
    result = invoke("web", "pair", "--principal", principal_id, "--no-qr")
    assert result.exit_code == 1 and "Identity is unavailable" in result.output
    assert "#pair=" not in result.output
    assert authority_rows(app) == before
    assert app.db.scalar("SELECT COUNT(*) FROM collaboration_invites") == invites


def test_admin_device_invite_is_unusable_if_the_target_is_disabled_after_issue(app):
    principal_id = member(app).principal.id
    result = invoke("web", "pair", "--principal", principal_id, "--no-qr")
    assert result.exit_code == 0
    _, code = link_in(result.output)
    app.identities.disable_principal("owner", principal_id)
    assert app.web_auth.redeem(code) is None
    assert app.db.scalar("SELECT COUNT(*) FROM web_sessions") == 0


def test_member_cannot_use_the_local_admin_invitation_capability(app):
    principal_id = member(app).principal.id
    with pytest.raises(AccessDenied, match="administrator"):
        app.identities.create_admin_device_invite(principal_id, "owner")
    assert app.db.scalar("SELECT COUNT(*) FROM collaboration_invites WHERE kind='device'") == 0


def test_principals_lists_only_identity_labels_and_states_without_secrets(app):
    active = member(app, label="Active member")
    disabled = member(app, label="Disabled member")
    app.identities.disable_principal("owner", disabled.principal.id)
    secret = app.web_auth.invite("private browser").code
    before = authority_rows(app)
    result = invoke("web", "principals")
    assert result.exit_code == 0, result.output
    assert active.principal.id in result.output and "Active member" in result.output
    assert disabled.principal.id in result.output and "Disabled member" in result.output
    assert "active" in result.output and "disabled" in result.output
    assert secret not in result.output and active.recovery_code not in result.output
    assert "token" not in result.output.lower() and "email" not in result.output.lower()
    assert authority_rows(app) == before


def test_principals_requires_active_local_administrator(app):
    app.db.execute("UPDATE principals SET is_admin=0 WHERE id='owner'")
    result = invoke("web", "principals")
    assert result.exit_code == 1 and "administrator" in result.output
