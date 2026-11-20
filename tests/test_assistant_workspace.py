"""Real workspace state and action fences; the assistant never grants itself authority."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from agents.tool_context import ToolContext

from personal_agent.assistant import (
    BY_ACTION,
    AssistantConflict,
    AssistantError,
    action_cards,
    begin_action,
    capability_catalog,
    dismiss_action,
    finish_action,
    inspect_workspace,
    prepare_action,
)
from personal_agent.collaboration.models import AccessDenied
from personal_agent.core.events import Trust
from personal_agent.harness.runctx import Origin, RunContext
from personal_agent.orchestration.models import OrchestrationNotFound
from personal_agent.tools.workspace import inspect_workspace_tool, prepare_workspace_action_tool


@pytest.fixture
def workspace(make_app):
    root = make_app()
    project = root.identities.create_project("owner", "Team")
    invitation = root.identities.create_member_invite("owner", project.id)
    member = root.identities.redeem_member(invitation.code, label="Member")
    return root, root.collaboration.get(project.id), member.principal.id


def propose(app, actor="owner", action="create_space", target="", draft=None, key="one"):
    return prepare_action(
        app,
        actor,
        work_id="work-1",
        action_key=key,
        action=action,
        target_id=target,
        draft={"name": "New project"} if draft is None else draft,
    )


def test_catalog_only_names_real_tools_and_user_actions(make_app):
    app = make_app()
    catalog = capability_catalog(app, "owner")
    assert {item["action"] for item in catalog["actions"]} == set(BY_ACTION)
    for names in catalog["direct_tools"].values():
        assert all(app.tools.get(name) is not None for name in names)
    for name in ("workspace_capabilities", "inspect_workspace", "prepare_workspace_action"):
        assert app.tools.get(name) is not None
    assert not app.tools.get("approve_agent_plan")
    assert "ask_task_input" in catalog["direct_tools"]["computer_tasks"]


def test_project_invitation_never_enters_proposal_or_begin_payload(make_app):
    app = make_app()
    action = "accept_project_invite"
    code = "user-only-invitation-code"
    with pytest.raises(AssistantError):
        propose(app, action=action, draft={"code": code})
    with pytest.raises(AssistantError):
        propose(app, action=action, target=code, draft={})
    card = propose(app, action=action, draft={})
    assert card["data"] == {"action": action, "target_id": "", "draft": {}}
    with pytest.raises(AssistantError):
        begin_action(app, "owner", card["id"], {"code": code})
    receipt = begin_action(app, "owner", card["id"], {})
    assert receipt["payload"] == {} and receipt["action"] == action
    assert code not in json.dumps(action_cards(app, "owner"))
    assert code not in "".join(row["value"] for row in app.db.query("SELECT value FROM kv"))


@pytest.mark.parametrize("state", ["cancelled", "revoked", "wake"])
def test_resource_owner_stop_card_never_reads_private_task_payload(workspace, state, monkeypatch):
    _root, shared, actor = workspace
    store = shared.orchestration
    agent = store.create_agent(
        "owner", name="Owned worker", kind="muse", allowed_actor_ids=[actor]
    )["agent"]
    task = store.create_task(
        actor,
        request_id="private-stop",
        title="PRIVATE_MEMBER_TITLE",
        steps=[
            {
                "id": "s1",
                "agent_id": agent["id"],
                "prompt": "PRIVATE_MEMBER_PROMPT",
                "acceptance": "PRIVATE_MEMBER_ACCEPTANCE",
            }
        ],
        deadline_at=shared.clock.now() + 3600,
    )
    store.approve_task(task["id"], actor, revision=task["revision"], plan_hash=task["plan_hash"])
    with pytest.raises(OrchestrationNotFound):
        store.get_task(task["id"], "owner")
    if state == "wake":
        wake = store.reserve_wake(agent["id"], task["id"])
        store.finish_wake(wake["id"], outcome="sent", http_status=200)
        store.cancel_task(task["id"], actor)
        action, draft = "confirm_wake_stopped", {"wake_id": wake["id"]}
        payload = {"note": "I checked the native run has ended."}
    else:
        attempt = store.claim(agent["id"], "worker")["attempt"]
        action = "confirm_agent_stopped"
        draft = {"step_id": "s1", "attempt_id": attempt["attempt_id"]}
        # A live run cannot be cleared just because its owner asks the model.
        with pytest.raises(AccessDenied):
            propose(shared, action=action, target=task["id"], draft=draft)
        if state == "revoked":
            store.report_result(
                attempt["attempt_id"],
                agent["id"],
                generation=attempt["generation"],
                lease_token=attempt["lease_token"],
                text="PRIVATE_MEMBER_RESULT",
            )
            store.update_agent(agent["id"], "owner", name="Changed connection")
        else:
            store.cancel_task(task["id"], actor)
        payload = {**draft, "note": "I checked the native run has ended."}

    def no_private_task_read(*_args, **_kwargs):
        pytest.fail("Stop preparation must use only resource-owner occupancy.")

    monkeypatch.setattr(store, "get_task", no_private_task_read)
    with pytest.raises(AccessDenied):
        propose(shared, actor, action=action, target=task["id"], draft=draft)
    with pytest.raises(AccessDenied):
        propose(shared, action=action, target="task_not_this_resource", draft=draft)
    with pytest.raises(AssistantError):
        propose(shared, action=action, target=task["id"], draft={**draft, "note": "Model proof"})
    card = propose(shared, action=action, target=task["id"], draft=draft)
    receipt = begin_action(shared, "owner", card["id"], payload)
    encoded = json.dumps(
        [
            card,
            receipt,
            inspect_workspace(shared, "owner", topic="agent_occupancy", resource_id=agent["id"]),
        ]
    )
    assert "PRIVATE_MEMBER" not in encoded
    if state == "wake":
        stopped = store.confirm_wake_stopped(task["id"], wake["id"], "owner", **receipt["payload"])
    else:
        stopped = store.confirm_stopped(task["id"], actor_id="owner", **receipt["payload"])
    assert stopped["redacted"] and stopped["state"] == "stopped"
    assert "PRIVATE_MEMBER" not in json.dumps(stopped)
    # Once stopped, a fresh proposal cannot authorize a second native send.
    with pytest.raises(AccessDenied):
        propose(shared, action=action, target=task["id"], draft=draft, key="after-stop")


def test_proposal_is_durable_private_and_does_not_execute(workspace):
    root, shared, actor = workspace
    count = root.db.scalar("SELECT COUNT(*) FROM spaces")
    card = propose(shared, actor)
    assert card["status"] == "proposed"
    assert root.db.scalar("SELECT COUNT(*) FROM spaces") == count
    assert propose(shared, actor) == card
    assert any(item["id"] == card["id"] for item in action_cards(shared, actor)["cards"])
    assert not any(item["id"] == card["id"] for item in action_cards(shared, "owner")["cards"])
    with pytest.raises(AccessDenied):
        begin_action(shared, "owner", card["id"], {"name": "Changed"})
    with pytest.raises(AssistantConflict):
        propose(shared, actor, draft={"name": "Different"})


def test_all_read_topics_respect_private_space(workspace):
    root, _shared, actor = workspace
    private = root.collaboration.get(root.identities.personal_space(actor).id)
    private.memory.write_text("/memories/state/knowledge/private.md", "member-only-content")
    with pytest.raises(AccessDenied):
        inspect_workspace(private, "owner", topic="knowledge")
    with pytest.raises(AccessDenied):
        inspect_workspace(root, actor, topic="overview")
    value = inspect_workspace(private, actor, topic="knowledge", resource_id="private.md")
    assert value["data"][0]["text"] == "member-only-content"
    with pytest.raises(AssistantError):
        inspect_workspace(private, actor, topic="knowledge", resource_id="../private.md")


def test_model_resource_reviews_never_copy_other_spaces_into_tool_history(workspace):
    root, shared, actor = workspace
    private = root.collaboration.get(root.identities.personal_space(actor).id)
    private.jobs.create_task(
        title="PRIVATE_MEMBER_TITLE",
        command="printf PRIVATE_MEMBER_COMMAND",
        actor_id=actor,
        request_id="private-command",
        approver_id="owner",
    )
    with pytest.raises(AccessDenied):
        root.identities.require_access("owner", private.space_id)
    for app in (shared, root):
        for topic in ("reviews", "cards", "overview", "tasks"):
            result = json.dumps(inspect_workspace(app, "owner", topic=topic))
            assert "PRIVATE_MEMBER" not in result
    # Cross-space resource review is explicitly browser-only and private to the
    # resource approver. It is not a grant to that person's private conversation.
    assert "PRIVATE_MEMBER_COMMAND" in json.dumps(
        action_cards(root, "owner", include_cross_space_reviews=True)
    )
    with pytest.raises(AccessDenied):
        action_cards(shared, "owner", include_cross_space_reviews=True)


def test_queries_strip_credentials_and_owner_configuration(make_app, monkeypatch):
    app = make_app()
    secret = "never-return-this-deployment-secret"
    monkeypatch.setenv("WORKSPACE_TEST_SECRET", secret)
    agent = app.orchestration.create_agent(
        "owner",
        name="Service " + secret,
        kind="grok",
        secret_env={"webhook_url": "ROUTINE_URL", "webhook_key": "ROUTINE_KEY"},
    )
    result = inspect_workspace(app, "owner", topic="agents")
    encoded = json.dumps(result)
    assert secret not in encoded
    assert agent["worker_token"] not in encoded
    assert "secret_env" not in encoded
    assert "token_hash" not in encoded
    assert "[REDACTED]" in encoded


@pytest.mark.parametrize(
    "draft",
    [
        {"secret_env": {"webhook_key": "KEY"}},
        {"note": "I certify it stopped"},
        {"url": "https://example.com"},
    ],
)
def test_model_cannot_supply_hidden_fields(make_app, draft):
    app = make_app()
    with pytest.raises(AssistantError):
        propose(app, draft=draft)
    assert not app.db.scalar("SELECT COUNT(*) FROM kv WHERE key LIKE 'assistant:proposal:%'")


def test_known_secrets_and_nonfinite_json_are_never_persisted(make_app, monkeypatch):
    app = make_app()
    secret = "sensitive-test-value-123"
    monkeypatch.setenv("ACTION_TEST_SECRET", secret)
    with pytest.raises(AssistantError):
        propose(app, draft={"name": secret})
    with pytest.raises(AssistantError):
        propose(app, draft={"name": float("nan")})
    card = propose(app)
    with pytest.raises(AssistantError):
        begin_action(app, "owner", card["id"], {"name": secret})
    assert secret not in "".join(row["value"] for row in app.db.query("SELECT value FROM kv"))
    assert action_cards(app, "owner")["cards"][0]["status"] == "proposed"


def test_concurrent_begin_admits_one_send_and_reload_cannot_retry(make_app, clock):
    app = make_app()
    card = propose(app)

    def begin():
        try:
            return begin_action(app, "owner", card["id"], {"name": "Exact confirmed name"})
        except AssistantConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: begin(), range(2)))
    receipt = next(item for item in receipts if item is not None)
    assert sum(item is not None for item in receipts) == 1
    assert receipt["payload"] == {"name": "Exact confirmed name"}
    clock.advance(2 * 86400)
    loaded = next(item for item in action_cards(app, "owner")["cards"] if item["id"] == card["id"])
    assert loaded["status"] == "executing" and loaded["actions"] == []
    with pytest.raises(AssistantConflict):
        begin_action(app, "owner", card["id"], receipt["payload"])
    with pytest.raises(AssistantConflict):
        dismiss_action(app, "owner", card["id"])
    finish_action(
        app, "owner", card["id"], execution_id=receipt["execution_id"], outcome="uncertain"
    )
    assert (
        next(item for item in action_cards(app, "owner")["cards"] if item["id"] == card["id"])[
            "actions"
        ]
        == []
    )
    with pytest.raises(AssistantConflict):
        begin_action(app, "owner", card["id"], receipt["payload"])


def test_completion_is_fixed_idempotent_and_cannot_reopen(make_app):
    app = make_app()
    card = propose(app)
    receipt = begin_action(app, "owner", card["id"], {"name": "Confirmed"})
    with pytest.raises(AssistantConflict):
        finish_action(app, "owner", card["id"], execution_id="wrong", outcome="succeeded")
    with pytest.raises(AssistantError):
        finish_action(
            app,
            "owner",
            card["id"],
            execution_id=receipt["execution_id"],
            outcome="response-token-data",
        )
    expected = {"ok": True, "status": "succeeded"}
    assert (
        finish_action(
            app, "owner", card["id"], execution_id=receipt["execution_id"], outcome="succeeded"
        )
        == expected
    )
    assert (
        finish_action(
            app, "owner", card["id"], execution_id=receipt["execution_id"], outcome="succeeded"
        )
        == expected
    )
    assert not any(item["id"] == card["id"] for item in action_cards(app, "owner")["cards"])
    with pytest.raises(AssistantConflict):
        begin_action(app, "owner", card["id"], receipt["payload"])


def test_expiry_and_rejoined_membership_do_not_revive_proposals(workspace, clock):
    root, shared, actor = workspace
    card = propose(shared, actor)
    root.identities.revoke_member("owner", shared.space_id, actor)
    invite = root.identities.create_member_invite("owner", shared.space_id)
    root.identities.redeem_member(invite.code, existing_principal_id=actor)
    assert not any(item["id"] == card["id"] for item in action_cards(shared, actor)["cards"])
    with pytest.raises(AccessDenied):
        begin_action(shared, actor, card["id"], {"name": "Attempted"})
    with pytest.raises(AccessDenied):
        propose(shared, actor)
    other = propose(shared, actor, key="new")
    clock.advance(86401)
    with pytest.raises(AssistantConflict):
        begin_action(shared, actor, other["id"], {"name": "Attempted"})


def test_viewer_and_nonowner_cannot_prepare_privileged_actions(workspace):
    root, shared, actor = workspace
    with pytest.raises(AccessDenied):
        propose(shared, actor, action="invite_member", draft={"role": "member"})
    agent = shared.orchestration.create_agent(
        "owner", name="Owner Agent", kind="muse", allowed_actor_ids=[actor]
    )
    with pytest.raises(AccessDenied):
        propose(shared, actor, action="rotate_agent_token", target=agent["agent"]["id"], draft={})
    root.identities.set_role("owner", shared.space_id, actor, "viewer")
    assert all(not item["available"] for item in capability_catalog(shared, actor)["actions"])
    with pytest.raises(AccessDenied):
        propose(shared, actor)


def test_agent_task_cards_follow_actual_plan_question_and_acceptance(make_app):
    app = make_app()
    store = app.orchestration
    agent = store.create_agent("owner", name="Muse", kind="muse")["agent"]
    task = store.create_task(
        "owner",
        request_id="plan",
        title="Research",
        steps=[
            {
                "id": "s1",
                "agent_id": agent["id"],
                "prompt": "Find evidence",
                "acceptance": "Cite sources",
                "depends_on": [],
            }
        ],
        deadline_at=app.clock.now() + 3600,
    )
    assert any(
        card["kind"] == "agent_plan" and card["actions"][0]["id"] == "approve"
        for card in action_cards(app, "owner")["cards"]
    )
    assert store.claim(agent["id"], "worker")["attempt"] is None
    store.approve_task(task["id"], "owner", revision=task["revision"], plan_hash=task["plan_hash"])
    attempt = store.claim(agent["id"], "worker")["attempt"]
    store.ask_question(
        attempt["attempt_id"],
        agent["id"],
        generation=attempt["generation"],
        lease_token=attempt["lease_token"],
        question_id="q1",
        text="Which region?",
        choices=["East", "West"],
    )
    cards = action_cards(app, "owner")["cards"]
    question = next(card for card in cards if card["kind"] == "agent_question")
    assert question["data"]["question"]["text"] == "Which region?"
    assert "lease_token" not in json.dumps(cards)
    store.answer_question(task["id"], "q1", "owner", answer="East")
    store.report_result(
        attempt["attempt_id"],
        agent["id"],
        generation=attempt["generation"],
        lease_token=attempt["lease_token"],
        outcome="succeeded",
        text="Evidence",
        artifacts=[],
    )
    result = next(
        card for card in action_cards(app, "owner")["cards"] if card["kind"] == "agent_result"
    )
    assert result["actions"][0]["id"] == "accept"
    assert store.get_task(task["id"], "owner")["steps"][0]["state"] == "result_reported"


@pytest.mark.parametrize(
    "profile,trust,interactive",
    [
        ("untrusted", Trust.UNTRUSTED, True),
        ("scheduled", Trust.OWNER, False),
        ("full", Trust.UNTRUSTED, True),
    ],
)
async def test_model_cannot_prepare_from_untrusted_or_background_runs(
    make_app, profile, trust, interactive
):
    app = make_app()
    rc = RunContext(
        app, "test-run", "main", profile, Origin("web", interactive=interactive), trust=trust
    )
    args = {
        "action": "create_space",
        "action_key": "one",
        "target_id": "",
        "draft_json": '{"name":"Team"}',
    }
    context = ToolContext(
        context=rc,
        tool_name="prepare_workspace_action",
        tool_call_id="call-1",
        tool_arguments=json.dumps(args),
    )
    result = await prepare_workspace_action_tool.on_invoke_tool(context, json.dumps(args))
    assert result.error
    assert not action_cards(app, "owner")["cards"]


async def test_tool_rechecks_saved_actor_version(workspace):
    root, shared, actor = workspace
    rc = RunContext(shared, "test-run", "main", "full", Origin("web"), principal_id=actor)
    root.identities.revoke_member("owner", shared.space_id, actor)
    args = {"topic": "members", "resource_id": "", "query": "", "limit": 20}
    context = ToolContext(
        context=rc,
        tool_name="inspect_workspace",
        tool_call_id="call-1",
        tool_arguments=json.dumps(args),
    )
    result = await inspect_workspace_tool.on_invoke_tool(context, json.dumps(args))
    assert result.error
