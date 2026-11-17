"""Permissions are revalidated at meaningful async and durable-write boundaries."""

from __future__ import annotations

import asyncio
import json

import pytest
from agents.testing import ScriptedModel, assistant_message
from agents.tool_context import ToolContext
from starlette.exceptions import HTTPException
from starlette.requests import Request

from personal_agent.channels.ingress import Ingress
from personal_agent.channels.web.api import WebApi
from personal_agent.channels.web.artifacts import artifact_routes
from personal_agent.channels.web.collaboration import V3Api
from personal_agent.collaboration.models import AccessDenied
from personal_agent.config import ContextConfig, PolicyConfig
from personal_agent.core.approvals import PendingCall
from personal_agent.core.dispatcher import Dispatcher
from personal_agent.core.worker import Worker
from personal_agent.harness.compaction import Compactor, Summary
from personal_agent.harness.runctx import Origin, RunContext
from personal_agent.nodes.registry import DEFAULT_CAPABILITIES
from personal_agent.security.guardian import Review
from personal_agent.security.policy import Policy
from personal_agent.tools.builtin import schedule_tool
from personal_agent.tools.guard import ToolGuard
from test_artifacts import publish, source
from test_compaction import seed
from test_schedule_tool import full
from test_v3_collaboration_http import group as group
from tooling import Spy, install


def request(
    path, token, *, data=None, query="", after_read=lambda: None, extra_headers=(), params=None
):
    async def receive():
        after_read()
        return {"type": "http.request", "body": json.dumps(data or {}).encode(), "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"cookie", f"pa_session={token}".encode()), *extra_headers],
            "query_string": query.encode(),
            "path_params": params or {},
        },
        receive=receive,
    )


def context(app, actor):
    run_id = app.runs.start(
        session_key="main", trigger="test", profile="full", model="offline", event_ids=[]
    )
    return RunContext(app, run_id, "main", "full", Origin("web"), principal_id=actor)


async def test_timer_and_authority_commit_together_on_storage_failure(group, monkeypatch):
    _root, shared, _, member, _, _ = group
    rc = context(shared, member.principal.id)
    original = shared.db.kv_set

    def fail_origin(key, value, **kwargs):
        if key.startswith("timer_origin:"):
            raise OSError("disk full")
        return original(key, value, **kwargs)

    monkeypatch.setattr(shared.db, "kv_set", fail_origin)
    await schedule_tool.on_invoke_tool(
        ToolContext(
            context=rc, tool_name="schedule", tool_call_id="schedule-1", tool_arguments="{}"
        ),
        json.dumps(full(name="must-rollback", prompt="Follow up", delay_minutes=5)),
    )
    assert shared.timers.get("must-rollback") is None
    assert shared.db.scalar("SELECT COUNT(*) FROM kv WHERE key LIKE 'timer_origin:%'") == 0


async def test_timer_preserves_actor_and_stops_after_membership_revocation(group):
    root, shared, _, member, _, _ = group
    rc = context(shared, member.principal.id)
    await schedule_tool.on_invoke_tool(
        ToolContext(
            context=rc, tool_name="schedule", tool_call_id="schedule-1", tool_arguments="{}"
        ),
        json.dumps(full(name="follow-up", prompt="Continue", delay_minutes=5)),
    )
    root.identities.revoke_member("owner", shared.space_id, member.principal.id)
    root.clock.advance(301)
    shared.timers.tick()
    payload = json.loads(shared.db.row("SELECT payload FROM events")["payload"])
    assert payload["principal_id"] == member.principal.id
    assert payload["membership_version"] == rc.membership_version
    before = shared.db.scalar("SELECT COUNT(*) FROM runs")
    await Worker(shared).run_once()
    assert shared.db.scalar("SELECT COUNT(*) FROM runs") == before


async def test_revocation_during_delivery_skips_after_sent_writes_but_records_delivery(group):
    root, shared, _, member, _, _ = group
    actor = member.principal.id
    path = "/memories/state/progress.md"
    shared.memory.write_text(path, "before")
    mid = shared.outbox.enqueue(
        session_key="main",
        channel="web",
        text="Already delivered",
        extra={"principal_id": actor, **shared.actor_snapshot(actor)},
        on_sent=[{"command": "str_replace", "path": path, "old_str": "before", "new_str": "after"}],
    )

    class DeliveredThenRevoked:
        async def send(self, message):
            root.identities.revoke_member("owner", shared.space_id, actor)

    result = await Dispatcher(shared, {"web": DeliveredThenRevoked()}).deliver_once()
    assert result.sent == [mid]
    assert shared.outbox.get(mid).status == "sent"
    assert shared.memory.read_text(path) == "before"


@pytest.mark.parametrize("change", ["membership", "device"])
async def test_artifact_download_rechecks_after_snapshot_read(group, monkeypatch, change):
    root, shared, _, member, _, user = group
    source(shared, content=b"must not escape after revocation")
    artifact = publish(shared).artifact
    token = user.token()

    async def read_then_revoke(call, *args):
        result = call(*args)
        if change == "membership":
            root.identities.revoke_member("owner", shared.space_id, member.principal.id)
        else:
            root.web_auth.revoke_token(token)
        return result

    monkeypatch.setattr("personal_agent.channels.web.artifacts.asyncio.to_thread", read_then_revoke)
    response = await artifact_routes(shared)[0].endpoint(
        request(
            f"/api/artifacts/{artifact.id}",
            token,
            params={"artifact_id": artifact.id},
        )
    )
    assert response.status_code == (403 if change == "membership" else 401)
    assert b"must not escape" not in response.body


@pytest.mark.parametrize("change", ["device", "membership_rejoin"])
async def test_chat_upload_cannot_outlive_device_or_membership_generation(
    group, monkeypatch, change
):
    root, shared, _, member, _, user = group
    actor = member.principal.id
    api = WebApi(shared, Ingress(shared), stop=asyncio.Event(), secure=False)
    from personal_agent.channels.web.api import Sent

    async def delayed_read(_request):
        if change == "device":
            root.web_auth.revoke_token(user.token())
        else:
            root.identities.revoke_member("owner", shared.space_id, actor)
            code = root.identities.create_member_invite("owner", shared.space_id).code
            root.identities.redeem_member(code, existing_principal_id=actor)
        return Sent("must not run", "request-001", [], None)

    monkeypatch.setattr(api, "_read_message", delayed_read)
    with pytest.raises(HTTPException) as error:
        await api.messages(
            request(
                "/api/messages",
                user.token(),
                extra_headers=[(b"content-type", b"multipart/form-data")],
            )
        )
    assert error.value.status_code == (401 if change == "device" else 403)
    assert shared.inbox.counts()["pending"] == 0


async def test_owner_demotion_while_reading_approval_body_does_not_approve(group):
    root, shared, _, member, _, user = group
    actor = member.principal.id
    root.identities.set_role("owner", shared.space_id, actor, "owner")
    [approval] = shared.approvals.create("r", "main", [PendingCall("c", "shell", "operation")])
    api = WebApi(shared, Ingress(shared), stop=asyncio.Event(), secure=False)
    with pytest.raises(HTTPException) as error:
        await api.decide(
            request(
                f"/api/approvals/{approval.id}",
                user.token(),
                data={"decision": "approve"},
                params={"approval_id": str(approval.id)},
                after_read=lambda: root.identities.set_role(
                    "owner", shared.space_id, actor, "member"
                ),
            )
        )
    assert error.value.status_code == 403
    assert shared.approvals.get(approval.id).status == "pending"


async def test_v3_request_body_revalidates_browser_session_before_mutation(group):
    root, shared, _, _member, _, user = group
    api = V3Api(root, secure=False)
    with pytest.raises(HTTPException) as error:
        await api.tasks(
            request(
                "/api/v3/tasks",
                user.token(),
                query=f"space={shared.space_id}",
                data={"title": "Old device", "command": "true", "client_request_id": "old-device"},
                after_read=lambda: root.web_auth.revoke_token(user.token()),
            )
        )
    assert error.value.status_code == 401
    assert not shared.jobs.list_tasks()


async def test_node_body_revalidates_token_before_claim(group):
    root, shared, _, _member, _, _ = group
    code = root.nodes.create_invite("owner", [shared.space_id]).code
    registration = root.nodes.redeem(code, DEFAULT_CAPABILITIES)
    api = V3Api(root, secure=False)
    with pytest.raises(HTTPException) as error:
        await api.node_claim(
            request(
                "/api/v3/nodes/claim",
                "",
                extra_headers=[(b"authorization", f"Bearer {registration.token}".encode())],
                after_read=lambda: root.nodes.revoke(registration.node.id, "owner"),
            )
        )
    assert error.value.status_code == 401


async def test_revocation_during_policy_review_prevents_actual_tool_execution(group):
    root, shared, _, member, _, _ = group
    actor = member.principal.id
    rc = context(shared, actor)
    shared.policy = Policy(PolicyConfig(mode="auto"))
    spy = install(shared, Spy())

    class RevokingReview:
        async def review(self, *args, **kwargs):
            root.identities.revoke_member("owner", shared.space_id, actor)
            return Review("approve", "approved before membership changed")

    shared.guardian = RevokingReview()
    tool = shared.tools.get("shell")
    raw = '{"command":"git push"}'
    call_context = ToolContext(context=rc, tool_name="shell", tool_call_id="c1", tool_arguments=raw)
    with pytest.raises(AccessDenied):
        await ToolGuard(shared).invoke(tool, tool.tool, call_context, raw)
    assert spy.calls == []


async def test_revocation_during_compaction_preserves_original_history(group):
    root, shared, _, member, _, _ = group
    actor = member.principal.id
    rc = context(shared, actor)
    shared.config = shared.config.model_copy(
        update={"context": ContextConfig(compact_at_tokens=2000, keep_recent_tokens=600)}
    )
    seed(shared)

    class RevokingSummary:
        async def summarize(self, previous, conversation):
            root.identities.revoke_member("owner", shared.space_id, actor)
            return Summary("The summary was generated while membership changed.", cost_usd=0.01)

    shared.summarizer = RevokingSummary()
    before = shared.sessions.items("main")
    with pytest.raises(AccessDenied):
        await Compactor(shared).compact("main", rc.run_id, authorize=rc.require_authorized)
    assert shared.sessions.items("main") == before
    assert shared.sessions.summary("main") is None


async def test_shared_inbox_batch_keeps_each_persons_authority_and_reply(group):
    _, shared, _, member, _, _ = group
    model = ScriptedModel([[assistant_message("owner reply")], [assistant_message("member reply")]])
    shared.model_factory = lambda name: model
    await Ingress(shared).owner_message(channel="web", text="first", principal_id="owner")
    await Ingress(shared).owner_message(
        channel="web", text="second", principal_id=member.principal.id
    )
    await Worker(shared).run_once()
    actors = {message.text: message.payload["principal_id"] for message in shared.outbox.recent()}
    assert actors == {"owner reply": "owner", "member reply": member.principal.id}
    assert len(model.calls) == 2


async def test_revoked_model_response_never_enters_delivery_queue(group, monkeypatch):
    root, shared, _, member, _, _ = group
    model = ScriptedModel([[assistant_message("must not be delivered")]])
    original = model.get_response

    async def response_then_revoke(*args, **kwargs):
        response = await original(*args, **kwargs)
        root.identities.revoke_member("owner", shared.space_id, member.principal.id)
        return response

    monkeypatch.setattr(model, "get_response", response_then_revoke)
    shared.model_factory = lambda name: model
    await Ingress(shared).owner_message(
        channel="web", text="question", principal_id=member.principal.id
    )
    result = await Worker(shared).run_once()
    assert result.outcome.status == "cancelled"
    assert shared.outbox.recent() == []
