from __future__ import annotations

import pytest

from personal_agent.core.approvals import (
    DENIED_MESSAGE,
    EXPIRED_MESSAGE,
    Approvals,
    PendingCall,
    request_text,
)
from personal_agent.core.events import Kind
from personal_agent.core.inbox import Inbox
from personal_agent.security.audit import Audit

HOUR = 3600


@pytest.fixture
def inbox(db, clock):
    return Inbox(db, clock)


@pytest.fixture
def approvals(db, inbox, clock):
    return Approvals(db, inbox, clock=clock, timeout_hours=12)


def suspend(approvals, run_id="r1", session="main", calls=("c1",)):
    created = approvals.create(
        run_id, session, [PendingCall(c, "shell", f"command {c}") for c in calls]
    )
    approvals.save_run(run_id, session, "STATE", {"profile": "full"})
    return created


def pending_events(inbox):
    out = []
    while (batch := inbox.claim("t")) is not None:
        out.extend(batch.events)
        inbox.complete(batch)
    return out


def test_creating_an_approval_twice_for_the_same_call_is_harmless(approvals):
    [a] = suspend(approvals)
    [b] = approvals.create("r1", "main", [PendingCall("c1", "shell", "command c1")])
    assert a.id == b.id and len(approvals.pending()) == 1


def test_approving_the_only_call_queues_the_resume_event_once(approvals, inbox):
    [a] = suspend(approvals)
    result = approvals.decide(a.id, True, by="cli")

    assert result.changed and result.resume_queued
    assert approvals.is_approved("r1", "c1")
    [event] = pending_events(inbox)
    assert event.kind == Kind.APPROVAL_RESPONSE
    assert event.payload == {"run_id": "r1", "generation": 1}

    again = approvals.decide(a.id, True, by="cli")
    assert not again.changed and "already approved" in again.message
    assert pending_events(inbox) == []


def test_a_run_resumes_only_when_every_call_is_answered(approvals, inbox):
    a, b = suspend(approvals, calls=("c1", "c2"))
    first = approvals.decide(a.id, True, by="cli")
    assert first.changed and not first.resume_queued
    assert pending_events(inbox) == []

    second = approvals.decide(b.id, False, by="cli", note="not that one")
    assert second.resume_queued
    assert [e.kind for e in pending_events(inbox)] == [Kind.APPROVAL_RESPONSE]
    assert approvals.get(b.id).rejection_message().startswith(DENIED_MESSAGE)
    assert "not that one" in approvals.get(b.id).rejection_message()


def test_a_run_that_is_suspended_again_can_be_resumed_again(approvals, inbox):
    [first] = suspend(approvals, calls=("c1",))
    approvals.decide(first.id, True, by="cli")
    assert len(pending_events(inbox)) == 1

    [second] = suspend(approvals, calls=("c2",))  # same run, next round
    approvals.decide(second.id, True, by="cli")
    assert len(pending_events(inbox)) == 1  # not swallowed as a duplicate of round one


def test_an_unknown_approval_id_is_reported_not_raised(approvals):
    result = approvals.decide(999, True, by="cli")
    assert not result.changed and result.approval is None and "999" in result.message


def test_answering_an_expired_request_does_not_approve_it(approvals, inbox, clock):
    [a] = suspend(approvals)
    clock.advance(13 * HOUR)
    result = approvals.decide(a.id, True, by="cli")

    assert not result.changed and "expired" in result.message
    assert not approvals.is_approved("r1", "c1")
    assert approvals.get(a.id).rejection_message() == EXPIRED_MESSAGE
    assert [e.kind for e in pending_events(inbox)] == [Kind.APPROVAL_RESPONSE]  # run is woken


def test_expire_due_closes_old_requests_and_wakes_the_run(approvals, inbox, clock):
    [a] = suspend(approvals)
    assert approvals.expire_due() == []
    clock.advance(12 * HOUR + 1)
    [expired] = approvals.expire_due()

    assert expired.id == a.id and expired.status == "expired"
    assert approvals.expire_due() == []
    assert len(pending_events(inbox)) == 1


def test_a_new_owner_message_supersedes_waiting_requests(approvals, inbox):
    a, b = suspend(approvals, calls=("c1", "c2"))
    approvals.decide(a.id, True, by="cli")
    closed = approvals.supersede_session("main")

    assert [x.id for x in closed] == [b.id]  # the unanswered one only
    late = approvals.decide(b.id, True, by="cli")
    assert not late.changed and "superseded" in late.message
    assert not approvals.is_approved("r1", "c2")


def test_calls_of_a_suspended_run_are_protected_from_crash_repair(approvals):
    suspend(approvals, run_id="r1", session="main", calls=("c1",))
    suspend(approvals, run_id="r2", session="other", calls=("c9",))
    assert approvals.suspended_call_ids("main") == {"c1"}

    approvals.drop_run("r1")
    assert approvals.suspended_call_ids("main") == set()


def test_saved_run_state_round_trips(approvals):
    suspend(approvals)
    saved = approvals.load_run("r1")
    assert saved.state == "STATE" and saved.meta == {"profile": "full"}
    approvals.drop_run("r1")
    assert approvals.load_run("r1") is None


def test_the_request_text_names_every_pending_call(approvals):
    created = suspend(approvals, calls=("c1", "c2"))
    text = request_text(created)
    assert all(f"#{a.id}" in text for a in created)
    assert f"/approve {created[0].id}" in text and "/approve all" in text
    single = request_text(created[:1])
    assert "all" not in single


def test_the_request_text_offers_all_only_where_all_would_cover_it(approvals):
    """`all` is what waits in the conversation it is typed in. A request from a timer's run is not
    in it, so that text names the numbers instead of promising something `all` would not do."""
    elsewhere = suspend(approvals, run_id="r2", session="timer:brief", calls=("c1", "c2"))
    text = request_text(elsewhere)
    assert "/approve all" not in text
    assert f"`/approve {elsewhere[0].id} {elsewhere[1].id}`" in text
    assert f"`/deny {elsewhere[0].id} {elsewhere[1].id}`" in text


# ---- audit ------------------------------------------------------------------------------
def test_audit_redacts_arguments_and_fills_in_the_outcome(db, clock):
    audit = Audit(db, clock)
    audit.record(
        run_id="r1",
        session_key="main",
        call_id="c1",
        tool="http_request",
        category="net_write",
        verdict="ask",
        layer="mode",
        reason="needs approval",
        args={"url": "https://x.test", "token": "sk-" + "a" * 30},
        detail="x.test",
    )
    audit.set_outcome("r1", "c1", "ok", 12)

    [row] = audit.recent()
    assert "sk-aaa" not in row.args and "REDACTED" in row.args
    assert (row.outcome, row.duration_ms, row.verdict) == ("ok", 12, "ask")


def test_audit_ignores_a_second_record_for_the_same_call(db, clock):
    audit = Audit(db, clock)
    kwargs = dict(
        run_id="r1", session_key="main", call_id="c1", tool="shell", category="exec",
        verdict="ask", layer="mode", reason="x", args={},
    )  # fmt: skip
    assert audit.record(**kwargs) is not None
    assert audit.record(**kwargs) is None
    assert len(audit.recent()) == 1


def test_audit_truncates_huge_arguments(db, clock):
    audit = Audit(db, clock)
    audit.record(
        run_id="r", session_key="s", call_id="c", tool="t", category="read", verdict="allow",
        layer="mode", reason="", args={"blob": "x" * 50_000},
    )  # fmt: skip
    [row] = audit.recent()
    assert len(row.args) < 2000 and "more characters" in row.args


def test_audit_filters_and_counts(db, clock):
    audit = Audit(db, clock)
    for i, verdict in enumerate(["allow", "allow", "deny", "ask"]):
        audit.record(
            run_id="r", session_key="s", call_id=f"c{i}", tool="shell" if i else "memory",
            category="exec", verdict=verdict, layer="mode", reason="", args={},
        )  # fmt: skip
    assert [r.verdict for r in audit.recent(verdict="deny")] == ["deny"]
    assert {r.tool for r in audit.recent(tool="shell")} == {"shell"}
    assert audit.counts(0) == {"allow": 2, "deny": 1, "ask": 1}
