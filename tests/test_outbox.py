import pytest

from personal_agent.core.outbox import MUST_ARRIVE_SQL, URGENCIES, Outbox


@pytest.fixture
def outbox(db, clock):
    return Outbox(db, clock)


def enq(outbox, text="hello", **kw):
    kw.setdefault("session_key", "main")
    kw.setdefault("channel", "cli")
    return outbox.enqueue(text=text, **kw)


def test_enqueue_claim_send(outbox):
    mid = enq(
        outbox,
        "hi",
        on_sent=[{"command": "create", "path": "/memories/state/x.md", "file_text": "x"}],
    )
    [msg] = outbox.claim_due()
    assert msg.id == mid and msg.text == "hi" and msg.status == "sending" and msg.attempts == 1
    assert msg.on_sent[0]["command"] == "create"
    assert outbox.claim_due() == []  # already claimed
    outbox.mark_sent(mid)
    assert outbox.get(mid).status == "sent"
    assert outbox.counts()["sent"] == 1


def test_dedupe_key(outbox):
    assert enq(outbox, dedupe_key="brief:2026-11-15") is not None
    assert enq(outbox, "again", dedupe_key="brief:2026-11-15") is None
    assert len(outbox.recent()) == 1


def test_not_before_and_channel_filter(outbox, clock):
    enq(outbox, "later", not_before=clock.now() + 60)
    enq(outbox, "tg", channel="telegram")
    assert [m.text for m in outbox.claim_due(channels={"cli"})] == []
    assert [m.text for m in outbox.claim_due(channels={"telegram"})] == ["tg"]
    clock.advance(61)
    assert [m.text for m in outbox.claim_due(channels={"cli"})] == ["later"]
    assert outbox.claim_due(channels=set()) == []


def test_priority_decides_who_fits_in_the_limit(outbox):
    enq(outbox, "low", urgency="low")
    enq(outbox, "urgent", urgency="urgent")
    enq(outbox, "normal")
    # urgent first, then normal; the low-priority message waits for the next round
    assert [m.text for m in outbox.claim_due(limit=2)] == ["urgent", "normal"]
    assert [m.text for m in outbox.claim_due()] == ["low"]


def test_claimed_messages_come_back_in_priority_order_then_age(outbox):
    enq(outbox, "n1")
    enq(outbox, "low", urgency="low")
    enq(outbox, "u1", urgency="urgent")
    enq(outbox, "n2")
    enq(outbox, "u2", urgency="urgent")
    assert [m.text for m in outbox.claim_due()] == ["u1", "u2", "n1", "n2", "low"]


def test_failure_retries_then_gives_up(outbox, clock):
    mid = enq(outbox)
    for attempt in range(1, 6):
        [msg] = outbox.claim_due()
        assert msg.attempts == attempt
        status = outbox.mark_failed(mid, "channel down", retry_in=30, max_attempts=5)
        clock.advance(31)
    assert status == "failed" and outbox.get(mid).status == "failed"
    assert outbox.get(mid).last_error == "channel down"


def test_permanent_failure_does_not_retry(outbox):
    mid = enq(outbox)
    outbox.claim_due()
    assert outbox.mark_failed(mid, "blocked by user", retry_in=None) == "failed"


def test_hold_puts_message_back_without_using_an_attempt(outbox, clock):
    mid = enq(outbox)
    outbox.claim_due()
    outbox.hold(mid, until=clock.now() + 3600)  # quiet hours
    assert outbox.claim_due() == []
    clock.advance(3601)
    [msg] = outbox.claim_due()
    assert msg.attempts == 1


def test_crash_recovery_distinguishes_urgent_from_the_rest(outbox, clock):
    urgent = enq(outbox, "alert", urgency="urgent")
    normal = enq(outbox, "fyi")
    outbox.claim_due()  # both now `sending`; then the process dies
    clock.advance(300)
    assert outbox.recover_stuck() == {"retried": 1, "maybe_sent": 1}
    assert outbox.get(urgent).status == "pending"
    assert outbox.get(normal).status == "maybe_sent"
    assert [m.id for m in outbox.unresolved()] == [normal]
    assert outbox.cancel(normal)
    assert outbox.unresolved() == []


@pytest.mark.parametrize("urgency", URGENCIES)
@pytest.mark.parametrize("kind", ["message", "notice", "approval_request"])
@pytest.mark.parametrize("interactive", [False, True])
def test_a_message_that_must_arrive_is_retried_after_a_crash_and_the_rest_are_not(
    outbox, clock, urgency, kind, interactive
):
    """An alert, an answer the owner is waiting for, a request the agent is blocked on: a duplicate
    beats a lost one. Background messages are parked instead, and the owner decides."""
    mid = enq(outbox, urgency=urgency, kind=kind, extra={"interactive": interactive})
    outbox.claim_due()
    clock.advance(300)
    must = urgency == "urgent" or kind == "approval_request" or interactive

    assert outbox.get(mid).must_arrive is must
    assert outbox.recover_stuck() == {"retried": int(must), "maybe_sent": int(not must)}
    assert outbox.get(mid).status == ("pending" if must else "maybe_sent")


@pytest.mark.parametrize("urgency", URGENCIES)
@pytest.mark.parametrize("kind", ["message", "notice", "approval_request"])
@pytest.mark.parametrize("extra", [{}, {"interactive": False}, {"interactive": True}])
def test_the_database_and_the_code_agree_on_what_must_arrive(outbox, urgency, kind, extra):
    mid = enq(outbox, urgency=urgency, kind=kind, extra=extra)
    in_sql = outbox.db.scalar(f"SELECT {MUST_ARRIVE_SQL} FROM outbox WHERE id = ?", (mid,))
    assert bool(in_sql) is outbox.get(mid).must_arrive


def test_recent_claims_are_not_considered_stuck(outbox, clock):
    enq(outbox)
    outbox.claim_due()
    clock.advance(10)
    assert outbox.recover_stuck(older_than_s=120) == {"retried": 0, "maybe_sent": 0}


def test_rejects_unknown_urgency(outbox):
    with pytest.raises(ValueError):
        enq(outbox, urgency="whenever")


def test_has_due_agrees_with_what_claim_due_would_return(outbox, clock):
    assert not outbox.has_due()
    enq(outbox, "later", not_before=clock.now() + 60)
    assert not outbox.has_due()  # held back
    enq(outbox, "now", channel="web")
    assert outbox.has_due() and outbox.has_due({"web"}) and not outbox.has_due({"cli"})
    assert not outbox.has_due(set())
    clock.advance(61)
    assert outbox.has_due({"cli"})
    outbox.claim_due()
    assert not outbox.has_due()  # everything is claimed
