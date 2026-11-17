"""A tool call stops at the first moment its person no longer has the right to make it.

A call goes through four stages: the policy decides, the guard starts it, a review may take its
time, and the tool begins to run. The grant can end between any two of them, so each stage looks
again. These tests end the grant at one stage each and check how far the call got.
"""

from __future__ import annotations

import pytest
from agents.tool_context import ToolContext

from personal_agent.collaboration.models import AccessDenied
from personal_agent.config import PolicyConfig
from personal_agent.core.approvals import PendingCall
from personal_agent.security.guardian import Review
from personal_agent.security.policy import Policy
from personal_agent.tools.guard import ToolGuard
from test_v3_authorization_boundaries import context
from test_v3_collaboration_http import group as group
from tooling import Spy, install

RAW = '{"command":"git push"}'


def call(app, actor):
    rc = context(app, actor)
    tool = app.tools.get("shell")
    return (
        rc,
        tool,
        ToolContext(context=rc, tool_name="shell", tool_call_id="c1", tool_arguments=RAW),
    )


async def test_a_person_who_lost_access_gets_no_decision_and_no_question(group):
    root, shared, _, member, _, _ = group
    actor = member.principal.id
    spy = install(shared, Spy())
    rc, tool, _ = call(shared, actor)
    root.identities.revoke_member("owner", shared.space_id, actor)
    with pytest.raises(AccessDenied):
        await ToolGuard(shared).decide(tool, {"command": "git push"}, rc, "c1")
    assert not shared.audit.recent() and not shared.approvals.pending() and not spy.calls


async def test_a_person_who_lost_access_is_not_told_how_an_interrupted_call_ended(group):
    root, shared, _, member, _, _ = group
    actor = member.principal.id
    spy = install(shared, Spy())
    rc, tool, context_for_call = call(shared, actor)
    guard = ToolGuard(shared)
    await guard.decide(tool, {"command": "git push"}, rc, "c1")
    [approval] = shared.approvals.create(
        rc.run_id, "main", [PendingCall("c1", "shell", "git push")]
    )
    assert shared.approvals.decide(approval.id, True, by="owner").approval.status == "approved"
    shared.audit.set_outcome(rc.run_id, "c1", "running")  # an earlier attempt may have started
    root.identities.revoke_member("owner", shared.space_id, actor)
    with pytest.raises(AccessDenied):
        await guard.invoke(tool, tool.tool, context_for_call, RAW)
    assert not spy.calls


async def test_a_call_the_reviewer_approved_after_the_grant_ended_is_not_even_started(group):
    root, shared, _, member, _, _ = group
    actor = member.principal.id
    shared.policy = Policy(PolicyConfig(mode="auto"))
    spy = install(shared, Spy())
    _, tool, context_for_call = call(shared, actor)

    class RevokingReview:
        async def review(self, *args, **kwargs):
            root.identities.revoke_member("owner", shared.space_id, actor)
            return Review("approve", "approved before membership changed")

    shared.guardian = RevokingReview()
    with pytest.raises(AccessDenied):
        await ToolGuard(shared).invoke(tool, tool.tool, context_for_call, RAW)
    [row] = shared.audit.recent()
    assert row.verdict == "allow" and row.outcome is None  # decided, and never marked as started
    assert not spy.calls


async def test_a_call_whose_grant_ended_just_before_it_began_to_run_never_runs(group, monkeypatch):
    root, shared, _, member, _, _ = group
    actor = member.principal.id
    shared.policy = Policy(PolicyConfig(allow=["shell"]))
    spy = install(shared, Spy())
    _, tool, context_for_call = call(shared, actor)
    marked = shared.audit.set_outcome

    def revoke_once_marked_as_started(run_id, call_id, outcome, *rest):
        marked(run_id, call_id, outcome, *rest)
        if outcome == "running":
            root.identities.revoke_member("owner", shared.space_id, actor)

    monkeypatch.setattr(shared.audit, "set_outcome", revoke_once_marked_as_started)
    with pytest.raises(AccessDenied):
        await ToolGuard(shared).invoke(tool, tool.tool, context_for_call, RAW)
    assert not spy.calls
