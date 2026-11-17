"""Browser controls expose and revoke only the intended channel identity and space."""

from __future__ import annotations

import pytest
from starlette.exceptions import HTTPException

from personal_agent.channels.web.collaboration import V3Api
from personal_agent.collaboration.models import OWNER_SPACE
from personal_agent.core.approvals import PendingCall
from test_v3_authorization_boundaries import request
from test_v3_collaboration_http import group as group
from test_v3_collaboration_http import post


def bind(root, principal_id, space_id, sender):
    invite = root.identities.create_channel_binding(principal_id, space_id)
    root.identities.bind_channel(invite.code, "telegram", "bot", sender, sender)
    return {"channel": "telegram", "account_id": "bot", "sender_id": sender}


def test_bindings_list_is_current_person_and_current_space_only(group):
    root, shared, private, member, owner, user = group
    bind(root, "owner", OWNER_SPACE, "owner-private")
    bind(root, "owner", shared.space_id, "owner-project")
    bind(root, member.principal.id, private.space_id, "member-private")
    bind(root, member.principal.id, shared.space_id, "member-project")
    mine = user.client.get(f"/api/v3/channel-bindings?space={shared.space_id}").json()["bindings"]
    assert len(mine) == 1 and mine[0]["sender_id"] == "member-project"
    assert set(mine[0]) == {
        "channel",
        "account_id",
        "sender_id",
        "recipient_id",
        "bound_at",
        "version",
        "active",
    }
    assert mine[0]["active"] is True
    theirs = owner.client.get(f"/api/v3/channel-bindings?space={shared.space_id}").json()[
        "bindings"
    ]
    assert [row["sender_id"] for row in theirs] == ["owner-project"]
    assert owner.client.get(f"/api/v3/channel-bindings?space={private.space_id}").status_code == 403
    assert user.client.get(f"/api/v3/channel-bindings?space={OWNER_SPACE}").status_code == 403


def test_viewer_can_remove_their_stale_binding_but_cannot_create_one(group):
    root, shared, _, member, _, user = group
    identity = bind(root, member.principal.id, shared.space_id, "mine")
    root.identities.set_role("owner", shared.space_id, member.principal.id, "viewer")
    bindings = user.client.get(f"/api/v3/channel-bindings?space={shared.space_id}").json()[
        "bindings"
    ]
    assert len(bindings) == 1 and bindings[0]["active"] is False
    assert post(user, "/api/v3/channel-binding", {}, shared.space_id).status_code == 403
    assert (
        post(user, "/api/v3/channel-bindings/revoke", identity, shared.space_id).status_code == 200
    )
    assert root.identities.lookup_channel("telegram", "bot", "mine") is None
    assert root.identities.has_channel_binding("telegram", "bot", "mine")
    assert user.client.get(f"/api/v3/channel-bindings?space={shared.space_id}").json() == {
        "bindings": []
    }


def test_revoke_cannot_cross_selected_space_or_other_users_binding(group):
    root, shared, private, member, owner, user = group
    owners = bind(root, "owner", shared.space_id, "owners")
    mine = bind(root, member.principal.id, shared.space_id, "mine")
    private_binding = bind(root, member.principal.id, private.space_id, "my-private")
    assert post(user, "/api/v3/channel-bindings/revoke", owners, shared.space_id).status_code == 403
    assert post(user, "/api/v3/channel-bindings/revoke", mine, private.space_id).status_code == 403
    assert (
        post(owner, "/api/v3/channel-bindings/revoke", private_binding, shared.space_id).status_code
        == 403
    )
    assert (
        post(
            owner, "/api/v3/channel-bindings/revoke", private_binding, private.space_id
        ).status_code
        == 403
    )
    assert root.identities.lookup_channel("telegram", "bot", "mine") is not None
    # Instance admin may revoke a resource binding in a shared space they can access.
    assert post(owner, "/api/v3/channel-bindings/revoke", mine, shared.space_id).status_code == 200
    assert root.identities.lookup_channel("telegram", "bot", "my-private") is not None


async def test_channel_revoke_revalidates_session_after_body_read(group):
    root, shared, _, member, _, user = group
    identity = bind(root, member.principal.id, shared.space_id, "mine")
    session = root.web_auth.lookup(user.token())
    api = V3Api(root, secure=False)
    with pytest.raises(HTTPException) as error:
        await api.channel_revoke(
            request(
                "/api/v3/channel-bindings/revoke",
                user.token(),
                data=identity,
                query=f"space={shared.space_id}",
                after_read=lambda: root.web_auth.revoke(session.id),
            )
        )
    assert error.value.status_code == 401
    assert root.identities.lookup_channel("telegram", "bot", "mine") is not None


def test_project_coowner_can_approve_through_space_router(group):
    root, shared, _, member, _, user = group
    root.identities.set_role("owner", shared.space_id, member.principal.id, "owner")
    assert shared.space_owner_id != member.principal.id
    [approval] = shared.approvals.create("r", "main", [PendingCall("c", "shell", "operation")])
    response = post(user, f"/api/approvals/{approval.id}", {"decision": "approve"}, shared.space_id)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    saved = shared.approvals.get(approval.id)
    assert saved.status == "approved" and saved.decided_by == f"{member.principal.id}:web"
