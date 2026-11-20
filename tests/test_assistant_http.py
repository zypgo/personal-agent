"""Assistant cards through the actual browser, CSRF and selected-space API stack."""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.exceptions import HTTPException

from personal_agent.assistant import prepare_action
from personal_agent.channels.ingress import Ingress
from personal_agent.channels.web.api import WebApi
from personal_agent.channels.web.assistant import AssistantApi
from personal_agent.channels.web.collaboration import V3Api
from test_v3_authorization_boundaries import request
from test_v3_collaboration_http import group as group
from web_fake import Web


@pytest.fixture
def site(make_app):
    web = Web(make_app())
    web.pair()
    yield web
    web.client.close()


def proposal(app, actor="owner", action="create_recovery_code", draft=None, key="one"):
    return prepare_action(
        app, actor, work_id="message-1", action_key=key, action=action, draft=draft or {}
    )


def test_read_cards_are_authenticated_and_never_create_schema_or_codes(site):
    app = site.app
    before = app.db.scalar("SELECT COUNT(*) FROM collaboration_invites")
    response = site.client.get("/api/assistant/cards")
    assert response.status_code == 200, response.text
    assert response.json()["space_id"] == "personal-owner"
    assert response.json()["cards"] == []
    assert site.client.get("/api/assistant/capabilities").status_code == 200
    assert app.db.scalar("SELECT COUNT(*) FROM collaboration_invites") == before
    site.client.cookies.clear()
    assert site.client.get("/api/assistant/cards").status_code == 401
    assert site.client.get("/api/assistant/capabilities").status_code == 401


def test_user_secret_lives_only_in_existing_api_response(site):
    card = proposal(site.app)
    base = f"/api/assistant/cards/{card['id']}"
    assert site.client.post(base + "/begin", json={"payload": {}}).status_code == 403
    begun = site.client.post(base + "/begin", headers=site.write, json={"payload": {}})
    assert begun.status_code == 200, begun.text
    receipt = begun.json()
    assert receipt["action"] == "create_recovery_code"
    # The sensitive action has not happened merely by beginning the card.
    secret_response = site.client.post("/api/v3/recovery-code", headers=site.write, json={})
    assert secret_response.status_code == 200
    secret = secret_response.json()["code"]
    assert secret not in site.client.get("/api/assistant/cards").text
    rejected = site.client.post(
        base + "/complete",
        headers=site.write,
        json={
            "execution_id": receipt["execution_id"],
            "outcome": "succeeded",
            "response": {"code": secret},
        },
    )
    assert rejected.status_code == 400
    completed = site.client.post(
        base + "/complete",
        headers=site.write,
        json={"execution_id": receipt["execution_id"], "outcome": "succeeded"},
    )
    assert completed.status_code == 200, completed.text
    assert not any(
        item["id"] == card["id"] for item in site.client.get("/api/assistant/cards").json()["cards"]
    )
    saved = "".join(
        row["value"]
        for row in site.app.db.query("SELECT value FROM kv WHERE key LIKE 'assistant:proposal:%'")
    )
    assert secret not in saved
    assert (
        site.client.post(base + "/begin", headers=site.write, json={"payload": {}}).status_code
        == 409
    )


def test_begin_freezes_user_form_and_never_executes_it(site):
    card = proposal(site.app, action="create_space", draft={"name": "Proposed name"})
    before = site.app.db.scalar("SELECT COUNT(*) FROM spaces")
    response = site.client.post(
        f"/api/assistant/cards/{card['id']}/begin",
        headers=site.write,
        json={"payload": {"name": "User corrected name"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["payload"] == {"name": "User corrected name"}
    assert site.app.db.scalar("SELECT COUNT(*) FROM spaces") == before
    cards = site.client.get("/api/assistant/cards").json()["cards"]
    assert cards[0]["status"] == "executing" and cards[0]["actions"] == []


def test_space_actor_and_viewer_boundaries(group):
    root, shared, private, member, owner, user = group
    card = proposal(shared, member.principal.id)
    route = f"/api/assistant/cards/{card['id']}/begin?space={shared.space_id}"
    assert owner.client.post(route, headers=owner.write, json={"payload": {}}).status_code == 403
    assert user.client.get("/api/assistant/cards?space=personal-owner").status_code == 403
    assert owner.client.get(f"/api/assistant/cards?space={private.space_id}").status_code == 403
    visible = user.client.get(f"/api/assistant/cards?space={shared.space_id}").json()["cards"]
    assert any(item["id"] == card["id"] for item in visible)
    root.identities.set_role("owner", shared.space_id, member.principal.id, "viewer")
    assert user.client.post(route, headers=user.write, json={"payload": {}}).status_code == 403
    assert user.client.get(f"/api/assistant/cards?space={shared.space_id}").status_code == 200


@pytest.mark.parametrize("change", ["session", "membership"])
async def test_begin_revalidates_after_body_await(group, change):
    root, shared, _private, member, _owner, user = group
    actor = member.principal.id
    card = proposal(shared, actor)
    api = AssistantApi(WebApi(shared, Ingress(shared), stop=asyncio.Event(), secure=False))
    token = user.token()

    def revoke():
        if change == "session":
            root.web_auth.revoke(root.web_auth.lookup(token).id, principal_id=actor)
        else:
            root.identities.revoke_member("owner", shared.space_id, actor)

    req = request(
        f"/api/assistant/cards/{card['id']}/begin",
        token,
        data={"payload": {}},
        after_read=revoke,
        params={"card_id": card["id"], "operation": "begin"},
    )
    if change == "session":
        with pytest.raises(HTTPException) as caught:
            await api.mutate(req)
        assert caught.value.status_code == 401
    else:
        with pytest.raises(HTTPException) as caught:
            await api.mutate(req)
        assert caught.value.status_code == 403
    saved = json.loads(
        shared.db.row("SELECT value FROM kv WHERE key LIKE 'assistant:proposal:%'")["value"]
    )
    assert saved["status"] == "proposed"


def test_worker_bearer_is_not_a_browser_user(site):
    registered = site.app.orchestration.create_agent("owner", name="Worker", kind="muse")
    card = proposal(site.app)
    site.client.cookies.clear()
    response = site.client.post(
        f"/api/assistant/cards/{card['id']}/begin",
        headers={**site.write, "Authorization": "Bearer " + registered["worker_token"]},
        json={"payload": {}},
    )
    assert response.status_code == 401


def test_join_card_accepts_invite_as_current_account_without_persisting_code(group):
    root, _shared, private, member, _owner, user = group
    project = root.identities.create_project("owner", "Invited project")
    invite = root.identities.create_member_invite("owner", project.id)
    before = root.db.scalar("SELECT COUNT(*) FROM principals")
    card = proposal(private, member.principal.id, action="accept_project_invite")
    base = f"/api/assistant/cards/{card['id']}"
    rejected = user.client.post(
        base + "/begin", headers=user.write, json={"payload": {"code": invite.code}}
    )
    assert rejected.status_code == 400
    begun = user.client.post(base + "/begin", headers=user.write, json={"payload": {}})
    assert begun.status_code == 200, begun.text
    assert begun.json()["payload"] == {}
    joined = user.client.post(
        "/api/v3/enroll",
        headers=user.write,
        json={"code": invite.code, "require_existing_session": True},
    )
    assert joined.status_code == 200, joined.text
    assert joined.json() == {"ok": True, "recovery_code": None}
    # TestClient can retain a hostless fixture cookie alongside the response cookie.
    token = joined.cookies["pa_session"]
    user.client.cookies.clear()
    user.client.cookies.set("pa_session", token)
    assert user.client.get("/api/v3/me").json()["principal"]["id"] == member.principal.id
    assert root.db.scalar("SELECT COUNT(*) FROM principals") == before
    assert root.identities.require_access(member.principal.id, project.id).role == "member"
    completed = user.client.post(
        base + "/complete",
        headers=user.write,
        json={"execution_id": begun.json()["execution_id"], "outcome": "succeeded"},
    )
    assert completed.status_code == 200, completed.text
    assert invite.code not in user.client.get("/api/assistant/cards").text
    assert invite.code not in "".join(
        row["value"] for row in private.db.query("SELECT value FROM kv")
    )


@pytest.mark.parametrize("session_state", ["missing", "revoked", "expired"])
def test_join_requires_existing_session_and_does_not_consume_invitation(site, session_state):
    root = site.app
    project = root.identities.create_project("owner", "Join target")
    invite = root.identities.create_member_invite("owner", project.id)
    before = root.db.scalar("SELECT COUNT(*) FROM principals")
    if session_state == "missing":
        site.client.cookies.clear()
    else:
        session = root.web_auth.lookup(site.token())
        if session_state == "revoked":
            root.web_auth.revoke(session.id, principal_id="owner")
        else:
            root.db.execute(
                "UPDATE web_sessions SET expires_at=? WHERE id=?", (root.clock.now(), session.id)
            )
    response = site.client.post(
        "/api/v3/enroll",
        headers=site.write,
        json={"code": invite.code, "require_existing_session": True},
    )
    assert response.status_code == 401, response.text
    assert root.db.scalar("SELECT COUNT(*) FROM principals") == before
    assert (
        root.db.scalar(
            "SELECT used_at FROM collaboration_invites WHERE space_id=? AND kind='member'",
            (project.id,),
        )
        is None
    )
    # The original public enrollment route remains compatible when the flag is absent.
    compatible = site.client.post("/api/v3/enroll", headers=site.write, json={"code": invite.code})
    assert compatible.status_code == 200, compatible.text
    assert compatible.json()["recovery_code"]
    assert root.db.scalar("SELECT COUNT(*) FROM principals") == before + 1


@pytest.mark.parametrize("change", ["session", "principal", "generation", "session_identity"])
async def test_join_fences_identity_captured_before_body_await(group, change):
    root, _shared, _private, member, _owner, user = group
    target = root.identities.create_project("owner", "Join target")
    invite = root.identities.create_member_invite("owner", target.id)
    token = user.token()
    session = root.web_auth.lookup(token)
    before = root.db.scalar("SELECT COUNT(*) FROM principals")

    def change_identity():
        if change == "session":
            root.web_auth.revoke(session.id, principal_id=member.principal.id)
        elif change == "principal":
            root.identities.disable_principal("owner", member.principal.id)
        elif change == "generation":
            root.db.execute(
                "UPDATE principals SET authz_version=authz_version+1 WHERE id=?",
                (member.principal.id,),
            )
            root.db.execute(
                "UPDATE web_sessions SET authz_version=authz_version+1 WHERE id=?", (session.id,)
            )
        else:
            root.db.execute(
                "UPDATE web_sessions SET principal_id='owner' WHERE id=?", (session.id,)
            )

    req = request(
        "/api/v3/enroll",
        token,
        data={"code": invite.code, "require_existing_session": True},
        after_read=change_identity,
    )
    with pytest.raises(HTTPException) as caught:
        await V3Api(root, secure=False).enroll(req)
    assert caught.value.status_code == 401
    assert root.db.scalar("SELECT COUNT(*) FROM principals") == before
    assert (
        root.db.scalar(
            "SELECT used_at FROM collaboration_invites WHERE space_id=? AND kind='member'",
            (target.id,),
        )
        is None
    )


async def test_join_revalidates_session_inside_redemption_transaction(group, monkeypatch):
    root, _shared, _private, member, _owner, user = group
    project = root.identities.create_project("owner", "Join target")
    invite = root.identities.create_member_invite("owner", project.id)
    token = user.token()
    lookup = root.web_auth.lookup
    calls = 0

    def revoke_after_post_body_check(value):
        nonlocal calls
        current = lookup(value)
        calls += 1
        if calls == 2:
            root.web_auth.revoke(current.id, principal_id=member.principal.id)
        return current

    monkeypatch.setattr(root.web_auth, "lookup", revoke_after_post_body_check)
    with pytest.raises(HTTPException) as caught:
        await V3Api(root, secure=False).enroll(
            request(
                "/api/v3/enroll",
                token,
                data={"code": invite.code, "require_existing_session": True},
            )
        )
    assert caught.value.status_code == 401
    assert calls == 3
    assert (
        root.db.scalar(
            "SELECT used_at FROM collaboration_invites WHERE space_id=? AND kind='member'",
            (project.id,),
        )
        is None
    )
