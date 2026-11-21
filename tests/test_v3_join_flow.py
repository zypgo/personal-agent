"""Accepting a project invitation, from the page's side: what it can learn first, where the
person lands, and that the link is not wasted."""

from __future__ import annotations

import pytest

from personal_agent.channels.web.openapi import document
from personal_agent.collaboration.store import MAX_PENDING_INVITES, MEMBER_INVITE_SECONDS
from personal_agent.config import AgentConfig
from web_fake import Web


@pytest.fixture
def share(make_app):
    root = make_app()
    owner = Web(root)
    owner.pair()
    made = owner.client.post("/api/v3/spaces", json={"name": "Research"}, headers=owner.write)
    project = made.json()["space"]
    webs = [owner]

    def invite(role="member"):
        sent = owner.client.post(
            f"/api/v3/invites?space={project['id']}", json={"role": role}, headers=owner.write
        )
        assert sent.status_code == 201, sent.text
        return sent.json()

    def guest() -> Web:
        webs.append(Web(root))
        return webs[-1]

    yield root, owner, project, invite, guest
    for web in webs:
        web.client.close()


def enroll(web, code, name="Mira", **extra):
    return web.client.post(
        "/api/v3/enroll", json={"code": code, "display_name": name, **extra}, headers=web.write
    )


def preview(web, code):
    return web.client.post("/api/v3/invites/preview", json={"code": code}, headers=web.write)


def test_a_new_person_lands_in_the_project_they_were_invited_to(share):
    _root, _owner, project, invite, guest = share
    web = guest()
    response = enroll(web, invite("viewer")["code"])
    assert response.status_code == 200, response.text
    landed = response.json()
    assert (landed["created"], landed["joined"], landed["role"]) == (True, True, "viewer")
    assert landed["space"]["id"] == project["id"] and landed["space"]["name"] == "Research"
    assert landed["space"]["kind"] == "project"
    assert landed["recovery_code"]
    # The session that came back already works in that project.
    assert web.client.get(f"/api/history?space={project['id']}").status_code == 200
    me = web.client.get("/api/v3/me", params={"space": project["id"]}).json()
    assert me["role"] == "viewer" and me["principal"]["display_name"] == "Mira"


def test_someone_already_signed_in_joins_as_themselves_and_keeps_their_session(share):
    root, _owner, project, invite, guest = share
    web = guest()
    assert enroll(web, invite()["code"]).status_code == 200
    second = root.identities.create_project("owner", "Reading")
    code = root.identities.create_member_invite("owner", second.id, "viewer").code
    sessions, token = root.db.scalar("SELECT COUNT(*) FROM web_sessions"), web.token()
    again = enroll(web, code, require_existing_session=True)
    assert again.status_code == 200, again.text
    landed = again.json()
    assert (landed["created"], landed["joined"], landed["role"]) == (False, True, "viewer")
    assert landed["recovery_code"] is None and landed["space"]["id"] == second.id
    # Same device, same session: no second one for the device list to show.
    assert root.db.scalar("SELECT COUNT(*) FROM web_sessions") == sessions
    assert "set-cookie" not in again.headers and web.token() == token
    assert web.client.get(f"/api/history?space={project['id']}").status_code == 200


def test_a_link_for_a_project_you_are_already_in_is_not_used_up(share):
    root, owner, project, invite, guest = share
    code = invite("viewer")["code"]
    own = enroll(owner, code, require_existing_session=True)
    landed = own.json()
    assert (landed["joined"], landed["created"], landed["role"]) == (False, False, "owner")
    assert landed["space"]["id"] == project["id"]
    assert enroll(guest(), code).json()["joined"] is True
    assert root.db.scalar("SELECT COUNT(*) FROM principals") == 2


def test_an_invitation_can_be_read_before_it_is_accepted(share):
    _root, owner, _project, invite, guest = share
    sent = invite("viewer")
    web = guest()
    seen = preview(web, sent["code"])
    assert seen.status_code == 200, seen.text
    assert seen.json() == {
        "space_name": "Research",
        "role": "viewer",
        "invited_by": "Owner",
        "expires_at": sent["expires_at"],
        "already_member": False,
    }
    assert preview(owner, sent["code"]).json()["already_member"] is True
    assert enroll(web, sent["code"]).json()["joined"] is True


def test_a_project_invitation_is_good_for_a_day(share):
    root, _owner, _project, invite, _guest = share
    sent = invite()
    assert sent["expires_at"] == pytest.approx(root.clock.now() + MEMBER_INVITE_SECONDS)


def test_a_dead_invitation_is_one_plain_answer_whatever_the_reason(share):
    _root, _owner, _project, invite, guest = share
    used = invite()["code"]
    assert enroll(guest(), used).status_code == 200
    web = guest()
    answers = {
        (response.status_code, response.json()["error"])
        for response in (preview(web, used), preview(web, "nonsense"), preview(web, ""))
    }
    assert answers == {(400, "Invitation is unavailable.")}


def test_only_failed_previews_count_toward_the_limit(share):
    _root, _owner, _project, invite, guest = share
    code = invite()["code"]
    web = guest()
    assert [preview(web, code).status_code for _ in range(15)] == [200] * 15
    assert [preview(web, "nope").status_code for _ in range(10)] == [400] * 10
    assert preview(web, "nope").status_code == 429
    assert preview(web, code).status_code == 429


def test_the_owner_can_take_back_unused_invitations_and_nobody_else_can(share):
    _root, owner, project, invite, guest = share
    unused, used = invite()["code"], invite("viewer")["code"]
    member = guest()
    assert enroll(member, used).status_code == 200
    # A code that links a chat account to the project is not an invitation for a person.
    chat = owner.client.post(f"/api/v3/channel-binding?space={project['id']}", headers=owner.write)
    assert chat.status_code == 200, chat.text
    revoke = f"/api/v3/invites/revoke?space={project['id']}"
    assert member.client.post(revoke, headers=member.write).status_code == 403
    taken = owner.client.post(revoke, headers=owner.write)
    assert taken.status_code == 200 and taken.json() == {"revoked": 1}
    assert enroll(guest(), unused).status_code == 400
    assert owner.client.post(revoke, headers=owner.write).json() == {"revoked": 0}


def test_a_day_long_invitation_can_pile_up_and_taking_them_back_makes_room(share):
    _root, owner, project, invite, guest = share
    used = invite()["code"]
    assert enroll(guest(), used).status_code == 200  # a used one takes no room
    for _ in range(MAX_PENDING_INVITES):
        invite()
    asked = owner.client.post(
        f"/api/v3/invites?space={project['id']}", json={"role": "member"}, headers=owner.write
    )
    assert asked.status_code == 400
    assert (
        asked.json()["error"]
        == "Too many unused invitations; use or revoke an existing invitation."
    )
    taken = owner.client.post(f"/api/v3/invites/revoke?space={project['id']}", headers=owner.write)
    assert taken.json() == {"revoked": MAX_PENDING_INVITES}
    invite()


def test_every_chat_reports_the_page_version_the_page_was_served_with(share):
    _root, owner, project, invite, guest = share
    web = guest()
    assert enroll(web, invite()["code"]).status_code == 200
    served = owner.client.get("/api/session").json()["version"]
    in_project = web.client.get("/api/session", params={"space": project["id"]}).json()
    in_personal = web.client.get("/api/session").json()
    # A different value would put "a new version is available" on screen, for good.
    assert in_project["version"] == in_personal["version"] == served


def test_a_personal_space_keeps_the_product_name_and_a_project_carries_its_own(share):
    _root, _owner, project, invite, guest = share
    web = guest()
    assert enroll(web, invite()["code"]).status_code == 200
    in_project = web.client.get("/api/session", params={"space": project["id"]}).json()
    in_personal = web.client.get("/api/session").json()
    assert in_project["agent"] == "Research"
    assert in_personal["agent"] == AgentConfig().name


def test_the_contract_says_who_may_look_at_an_invitation_and_who_may_take_them_back():
    paths = document()["paths"]
    # The person looking has no account yet; taking invitations back is for someone signed in.
    assert paths["/api/v3/invites/preview"]["post"]["security"] == []
    assert paths["/api/v3/invites/revoke"]["post"]["security"] == [{"browserSession": []}]
