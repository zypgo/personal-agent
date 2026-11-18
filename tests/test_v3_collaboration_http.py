"""Real HTTP boundaries and revocation through the existing harness/delivery pipeline."""

from __future__ import annotations

import asyncio

import pytest
from agents.testing import ScriptedModel
from starlette.requests import Request

from personal_agent.channels.ingress import Ingress
from personal_agent.channels.web.api import WebApi
from personal_agent.collaboration.models import OWNER_SPACE
from personal_agent.core.dispatcher import Dispatcher
from personal_agent.core.worker import Worker
from personal_agent.memory.store import Actor
from web_fake import Web


@pytest.fixture
def group(make_app):
    root = make_app()
    directory = root.identities
    project = directory.create_project("owner", "Shared")
    invitation = directory.create_member_invite("owner", project.id)
    member = directory.redeem_member(invitation.code, label="Collaborator")
    own = directory.personal_space(member.principal.id)
    shared = root.collaboration.get(project.id)
    private = root.collaboration.get(own.id)
    owner_web, member_web = Web(root), Web(root)
    owner_web.pair()
    member_web.client.cookies.set(
        "pa_session", root.web_auth.create_session(member.principal.id).token
    )
    yield root, shared, private, member, owner_web, member_web
    owner_web.client.close()
    member_web.client.close()


def post(web, path, data, space=None):
    suffix = f"?space={space}" if space else ""
    return web.client.post(path + suffix, json=data, headers=web.write)


def test_history_memory_artifacts_and_paths_cannot_cross_private_roots(group):
    root, shared, private, _member, owner, user = group
    root.transcript.add_in("web", "owner-only-data")
    private.transcript.add_in("web", "member-only-data")
    shared.transcript.add_in("web", "project-data")
    root.memory.write_text("/memories/state/secret.md", "owner memory", actor=Actor.USER)
    for web, forbidden in ((user, OWNER_SPACE), (owner, private.space_id)):
        for path in (
            "history",
            "activity",
            "approvals",
            "status",
            "media/missing",
            "artifacts/missing",
        ):
            assert web.client.get(f"/api/{path}?space={forbidden}").status_code == 403
    assert "member-only-data" in user.client.get("/api/history").text
    assert "owner-only-data" not in user.client.get("/api/history").text
    assert "project-data" in user.client.get(f"/api/history?space={shared.space_id}").text
    assert private.memory.read_text("/memories/state/secret.md") is None
    assert private.paths.home != root.paths.home
    assert not private.config.secrets and not private.config.tools.shell
    assert user.client.get("/api/history?space=../../personal-owner").status_code == 403


def test_viewer_is_read_only_and_members_cannot_change_resources(group):
    root, shared, _, member, _, user = group
    root.identities.set_role("owner", shared.space_id, member.principal.id, "viewer")
    assert user.client.get(f"/api/history?space={shared.space_id}").status_code == 200
    for path, data in (
        ("knowledge", {"path": "note.md", "text": "x"}),
        ("tasks", {"title": "x", "command": "true", "client_request_id": "a"}),
        ("invites", {}),
    ):
        assert post(user, f"/api/v3/{path}", data, shared.space_id).status_code == 403
    assert post(user, "/api/v3/node-invites", {"space_ids": [shared.space_id]}).status_code == 403
    assert (
        post(user, "/api/approvals/1", {"decision": "approve"}, shared.space_id).status_code == 403
    )


def test_devices_pair_into_same_identity_and_revocation_is_scoped(group):
    root, _, _private, member, owner, user = group
    invited = post(user, "/api/v3/devices/pair", {"label": "Second phone"}).json()
    second = Web(root)
    try:
        assert post(second, "/api/pair", {"code": invited["code"]}).status_code == 200
        assert second.client.get("/api/v3/me").json()["principal"]["id"] == member.principal.id
        owner_device = root.web_auth.lookup(owner.token())
        assert post(user, f"/api/v3/devices/{owner_device.id}/revoke", {}).status_code in (403, 404)
        devices = user.client.get("/api/v3/devices").json()["devices"]
        assert all(device["principal_id"] == member.principal.id for device in devices)
        mine = root.web_auth.lookup(second.token())
        assert post(user, f"/api/v3/devices/{mine.id}/revoke", {}).status_code == 200
        assert second.client.get("/api/v3/me").status_code == 401
        assert owner.client.get("/api/v3/me").status_code == 200
    finally:
        second.client.close()


def test_shared_knowledge_uses_compare_and_swap_and_survives_empty_edits(group):
    _, shared, _, _, owner, user = group
    first = post(
        owner,
        "/api/v3/knowledge",
        {"path": "sources.md", "text": "Source A", "revision": 0},
        shared.space_id,
    )
    assert first.status_code == 200, first.text
    revision = first.json()["documents"][0]["revision"]
    second = post(
        user,
        "/api/v3/knowledge",
        {"path": "sources.md", "text": "", "revision": revision},
        shared.space_id,
    )
    assert second.status_code == 200, second.text
    assert (
        post(
            owner,
            "/api/v3/knowledge",
            {"path": "sources.md", "text": "stale", "revision": revision},
            shared.space_id,
        ).status_code
        == 409
    )
    revision = second.json()["documents"][0]["revision"]
    assert (
        post(
            user,
            "/api/v3/knowledge",
            {"path": "sources.md", "text": "Source B", "revision": revision},
            shared.space_id,
        ).status_code
        == 200
    )
    assert (
        post(
            user, "/api/v3/knowledge", {"path": "../other.md", "text": "bad"}, shared.space_id
        ).status_code
        == 400
    )


async def test_revoked_inbox_and_queued_delivery_do_not_run_after_rejoin(group):
    root, shared, _, member, _, _ = group
    actor = member.principal.id
    model = ScriptedModel([])
    shared.model_factory = lambda name: model
    await Ingress(shared).owner_message(channel="web", text="do something", principal_id=actor)
    message_id = shared.outbox.enqueue(
        session_key="main",
        channel="web",
        text="private reply",
        extra={"principal_id": actor, **shared.actor_snapshot(actor)},
    )
    root.identities.revoke_member("owner", shared.space_id, actor)
    invitation = root.identities.create_member_invite("owner", shared.space_id)
    root.identities.redeem_member(invitation.code, existing_principal_id=actor)
    await Worker(shared).run_once()
    assert shared.db.scalar("SELECT COUNT(*) FROM runs") == 0

    class NoSend:
        name = "web"

        async def send(self, message):
            raise AssertionError("revoked generation was sent")

    result = await Dispatcher(shared, {"web": NoSend()}).deliver_once()
    assert message_id in result.failed
    assert shared.db.scalar("SELECT status FROM outbox WHERE id=?", (message_id,)) == "cancelled"


async def test_stream_checks_revocation_before_new_content(group):
    root, shared, _, member, _, user = group
    token = user.token()
    session = root.web_auth.lookup(token)
    api = WebApi(shared, Ingress(shared), stop=asyncio.Event(), secure=False)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/stream",
            "headers": [(b"cookie", f"pa_session={token}".encode())],
            "query_string": b"",
        }
    )
    stream = api._events(request, session, 0)
    assert b"retry" in await anext(stream)
    assert b"hello" in await anext(stream)
    root.identities.revoke_member("owner", shared.space_id, member.principal.id)
    shared.transcript.add_in("web", "must-never-be-streamed")
    frame = await anext(stream)
    assert b"signout" in frame and b"must-never-be-streamed" not in frame
    await stream.aclose()


async def test_member_cannot_approve_another_persons_tools(group):
    _, shared, _, member, _, _ = group
    result = await Ingress(shared).owner_message(
        channel="web", text="/approve all", principal_id=member.principal.id
    )
    assert result.status == "rejected"


@pytest.mark.parametrize("selection", ["revoked", "unavailable", "another_person"])
def test_logout_does_not_require_access_to_the_selected_space(group, selection):
    root, shared, _, member, owner, user = group
    token = user.token()
    root.identities.revoke_member("owner", shared.space_id, member.principal.id)
    space = {
        "revoked": shared.space_id,
        "unavailable": "no-such-space",
        "another_person": OWNER_SPACE,
    }[selection]

    response = post(user, "/api/logout", {}, space)

    assert response.status_code == 200
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert root.web_auth.lookup(token) is None
    assert user.client.get("/api/v3/me").status_code == 401
    assert owner.client.get("/api/v3/me").status_code == 200


def test_an_owner_can_leave_a_project_without_a_false_permission_error(group):
    root, shared, _, member, owner, user = group
    root.identities.set_role("owner", shared.space_id, member.principal.id, "owner")

    response = post(
        user,
        "/api/v3/members",
        {"principal_id": member.principal.id, "revoke": True},
        shared.space_id,
    )

    assert response.status_code == 200
    assert response.json() == {"members": []}
    assert user.client.get(f"/api/v3/members?space={shared.space_id}").status_code == 403
    assert owner.client.get(f"/api/v3/members?space={shared.space_id}").status_code == 200
    own = user.client.get("/api/v3/me")
    assert own.status_code == 200
    assert shared.space_id not in {space["id"] for space in own.json()["spaces"]}


def test_membership_revoke_preserves_private_space_and_drops_project(group):
    _, shared, private, member, owner, user = group
    result = post(
        owner,
        "/api/v3/members",
        {"principal_id": member.principal.id, "revoke": True},
        shared.space_id,
    )
    assert result.status_code == 200, result.text
    assert user.client.get(f"/api/history?space={shared.space_id}").status_code == 403
    assert user.client.get("/api/v3/me").json()["space_id"] == private.space_id


def test_saving_shared_job_never_needs_private_owner_files(group):
    root, shared, _, _member, _, user = group
    (root.paths.workspace_dir / "private.txt").write_text("not shared")
    response = post(
        user,
        "/api/v3/tasks",
        {
            "title": "Copy",
            "command": "cat private.txt",
            "input_paths": ["private.txt"],
            "client_request_id": "copy-test",
        },
        shared.space_id,
    )
    assert response.status_code in (400, 404)
    assert shared.jobs.list_tasks() == []


def test_control_plane_disables_local_shell(make_app, monkeypatch):
    monkeypatch.setenv("PA_CONTROL_ONLY", "1")
    app = make_app()
    assert app.config.tools.shell is False
