"""A slow browser cannot receive another SSE frame after its authorization changes."""

from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request

from personal_agent.channels.ingress import Ingress
from personal_agent.channels.web.api import WebApi
from personal_agent.core.approvals import PendingCall
from test_v3_collaboration_http import group as group


@pytest.mark.parametrize("next_event", ["hello", "turn", "activity", "drafts", "approvals"])
@pytest.mark.parametrize("change", ["device", "membership", "membership_rejoin"])
async def test_revocation_between_frames_stops_every_kind_of_private_event(
    group, next_event, change
):
    root, shared, _, member, _, user = group
    token = user.token()
    api = WebApi(shared, Ingress(shared), stop=asyncio.Event(), secure=False)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/stream",
            "headers": [(b"cookie", f"pa_session={token}".encode())],
            "query_string": b"after=0",
        }
    )
    response = await api.stream(request)
    stream = response.body_iterator
    try:
        assert b"retry" in await anext(stream)
        if next_event != "hello":
            assert b"hello" in await anext(stream)
            shared.transcript.add_in("web", "before revocation")
            if next_event == "turn":
                shared.transcript.add_in("web", "private second turn")
            elif next_event == "activity":
                shared.live.publish({"type": "activity", "working": True, "tool": "private tool"})
            elif next_event == "drafts":
                shared.live.response_started("run")
                shared.live.response_text("run", "private draft", complete=True)
            else:
                shared.approvals.create(
                    "run", "main", [PendingCall("call", "shell", "private plan")]
                )
            shared.live.publish({"type": "wake"})
            assert b"event: turn" in await anext(stream)

        # Each yield can block on a slow browser while another request removes its access.
        if change == "device":
            root.web_auth.revoke_token(token)
        else:
            root.identities.revoke_member("owner", shared.space_id, member.principal.id)
            if change == "membership_rejoin":
                invitation = root.identities.create_member_invite("owner", shared.space_id)
                root.identities.redeem_member(
                    invitation.code, existing_principal_id=member.principal.id
                )

        frame = await anext(stream)
        assert frame == b"event: signout\ndata: {}\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        await stream.aclose()
    assert shared.live.listeners == 0
