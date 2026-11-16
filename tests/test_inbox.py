import pytest

from personal_agent.core.events import NewEvent, Trust
from personal_agent.core.inbox import Inbox


def msg(text="hi", session="main", **kw):
    return NewEvent(source="cli", kind="message", session_key=session, payload={"text": text}, **kw)


@pytest.fixture
def inbox(db, clock):
    return Inbox(db, clock, lease_s=60, max_attempts=3, max_age_s=3600)


def test_add_dedupes_by_source_and_external_id(inbox):
    first = inbox.add(msg(external_id="x1"))
    again = inbox.add(msg("changed text", external_id="x1"))
    other_source = inbox.add(NewEvent("web", "message", "main", {"text": "hi"}, external_id="x1"))
    assert not first.duplicate and again.duplicate and again.id == first.id
    assert not other_source.duplicate and other_source.id != first.id


def test_claim_batches_one_session_in_order(inbox):
    a1 = inbox.add(msg("a1", session="A")).id
    b1 = inbox.add(msg("b1", session="B")).id
    a2 = inbox.add(msg("a2", session="A")).id

    batch = inbox.claim("w1")
    assert batch.session_key == "A"
    assert batch.ids == [a1, a2]
    assert [e.text for e in batch.events] == ["a1", "a2"]

    # session A is leased: a second worker gets session B, then nothing
    other = inbox.claim("w2")
    assert other.session_key == "B" and other.ids == [b1]
    assert inbox.claim("w3") is None


def test_trust_is_carried_and_batch_reports_it(inbox):
    inbox.add(msg("from outside", trust=Trust.UNTRUSTED))
    assert not inbox.claim("w").trusted


def test_a_batch_has_one_trust_level_and_keeps_the_order(inbox):
    """Text from outside must not ride along in the run of the owner's message, and the owner's
    message must not be held to the untrusted profile because something else was waiting."""
    inbox.add(msg("first"))
    inbox.add(msg("from outside", trust=Trust.UNTRUSTED))
    inbox.add(msg("third"))
    inbox.add(msg("fourth"))

    first = inbox.claim("w")
    assert first.trusted and [e.text for e in first.events] == ["first"]
    assert inbox.claim("w2") is None  # one run per session at a time, whatever the trust
    inbox.complete(first)

    second = inbox.claim("w")
    assert not second.trusted and [e.text for e in second.events] == ["from outside"]
    inbox.complete(second)

    third = inbox.claim("w")
    assert third.trusted and [e.text for e in third.events] == ["third", "fourth"]


def test_expired_lease_makes_events_claimable_again_and_counts_attempt(inbox, clock):
    event_id = inbox.add(msg()).id
    assert inbox.claim("w1").ids == [event_id]
    assert inbox.claim("w2") is None  # still leased
    clock.advance(61)
    second = inbox.claim("w2")
    assert second.ids == [event_id]
    assert second.events[0].attempts == 2  # the crashed claim counted


def test_a_lease_held_by_a_live_run_is_not_taken_over_when_the_clock_jumps(inbox, clock):
    """A laptop that slept past the lease did not stop the run that was waiting for a tool."""
    first = inbox.add(msg("charge the card once")).id
    batch = inbox.claim("w1")
    with inbox.holding(batch):
        clock.advance(1_000)  # far past the 60 s lease
        assert not inbox.has_ready()
        assert inbox.claim("w2") is None
        inbox.add(msg("and this"))  # later events of the session wait for the run too
        assert inbox.claim("w2") is None
        assert inbox.counts()["dead"] == 0
    # once the run is gone (the worker died), the lease runs out as it always did
    taken = inbox.claim("w2")
    assert taken is not None and taken.ids[0] == first


def test_a_live_run_is_not_declared_dead_for_having_used_its_attempts(db, clock):
    inbox = Inbox(db, clock, lease_s=60, max_attempts=1, max_age_s=3600)
    inbox.add(msg())
    batch = inbox.claim("w1")  # the one allowed attempt
    with inbox.holding(batch):
        clock.advance(120)
        assert inbox.claim("w2") is None
        assert inbox.counts() == {"pending": 0, "leased": 1, "done": 0, "dead": 0}
    assert inbox.claim("w2") is None  # not running any more: now it is poison
    assert inbox.counts()["dead"] == 1


def test_poison_event_dies_after_max_attempts(inbox, clock):
    event_id = inbox.add(msg("poison")).id
    for _ in range(3):  # three claims that never finish
        assert inbox.claim("w") is not None
        clock.advance(61)
    assert inbox.claim("w") is None
    dead = inbox.take_dead()
    assert [e.id for e in dead] == [event_id]
    assert inbox.counts()["dead"] == 1
    assert inbox.take_dead() == []  # reported once


def test_fail_backs_off_and_blocks_later_events_of_the_same_session(inbox, clock):
    first = inbox.add(msg("first")).id
    second = inbox.add(msg("second")).id
    batch = inbox.claim("w")
    assert batch.ids == [first, second]
    inbox.fail(batch, "oops")  # both go back; backoff 2s
    assert inbox.claim("w") is None
    clock.advance(2.1)
    again = inbox.claim("w")
    assert again.ids == [first, second]


def test_later_event_waits_behind_an_earlier_one_in_backoff(inbox, clock):
    first = inbox.add(msg("first")).id
    batch = inbox.claim("w")
    inbox.fail(batch, "oops")
    second = inbox.add(msg("second")).id  # arrives while `first` waits out its backoff
    assert inbox.claim("w") is None
    clock.advance(2.1)
    assert inbox.claim("w").ids == [first, second]


def test_environmental_failure_does_not_use_attempts(inbox, clock):
    event_id = inbox.add(msg()).id
    for _ in range(10):  # more than max_attempts
        batch = inbox.claim("w")
        assert batch is not None, "environmental failures must never kill an event"
        inbox.fail(batch, "network down", environmental=True)
        clock.advance(400)
    assert inbox.get(event_id)["status"] == "pending"
    assert inbox.get(event_id)["attempts"] == 0


def test_real_failures_eventually_kill_the_event(inbox, clock):
    event_id = inbox.add(msg()).id
    for _ in range(3):
        batch = inbox.claim("w")
        inbox.fail(batch, "bug")
        clock.advance(400)
    assert inbox.get(event_id)["status"] == "dead"
    assert [e.id for e in inbox.take_dead()] == [event_id]


def test_events_expire(inbox, clock):
    event_id = inbox.add(msg(delay_s=10)).id
    clock.advance(4000)  # older than max_age_s
    assert inbox.claim("w") is None
    assert inbox.get(event_id)["status"] == "dead"


def test_complete_and_lost_lease(inbox, clock):
    event_id = inbox.add(msg()).id
    batch = inbox.claim("w1")
    clock.advance(61)
    stolen = inbox.claim("w2")  # lease expired, another worker took over
    inbox.complete(batch)  # the stale owner must not be able to finish it
    assert inbox.get(event_id)["status"] == "leased"
    inbox.complete(stolen)
    assert inbox.get(event_id)["status"] == "done"


def test_delay_and_heartbeat(inbox, clock):
    inbox.add(msg(delay_s=30))
    assert inbox.claim("w") is None
    clock.advance(31)
    batch = inbox.claim("w")
    clock.advance(50)
    assert inbox.heartbeat(batch, lease_s=60)
    clock.advance(50)  # 100s since claim, but the lease was extended at t+50
    assert inbox.claim("other") is None


def test_a_restarted_process_does_not_wait_for_a_dead_workers_lease(inbox):
    """The process that holds the engine lock knows nobody else is working: what is leased is a
    dead worker's. Waiting out the lease would keep the owner's message waiting for minutes."""
    event = inbox.add(msg()).id
    inbox.claim("dead-worker")
    assert inbox.claim("new-process") is None  # leased: not yet

    assert inbox.release_leases() == 1

    [claimed] = inbox.claim("new-process").events
    assert (claimed.id, claimed.attempts) == (event, 2)  # the first, unfinished attempt stays used


def test_a_crash_loop_that_restarts_at_once_still_ends_in_a_dead_event(inbox):
    inbox.add(msg())
    for _ in range(3):  # max_attempts
        assert inbox.claim("worker") is not None
        inbox.release_leases()

    assert inbox.claim("worker") is None
    assert inbox.counts()["dead"] == 1


def test_releasing_leases_leaves_everything_else_alone(inbox, clock):
    inbox.add(msg("done"))
    inbox.complete(inbox.claim("w"))
    inbox.add(msg("later", delay_s=60))
    inbox.add(msg("waiting"))

    assert inbox.release_leases() == 0
    assert inbox.counts() == {"pending": 2, "leased": 0, "done": 1, "dead": 0}


def test_release_returns_without_consuming_an_attempt(inbox):
    event_id = inbox.add(msg()).id
    batch = inbox.claim("w")
    inbox.release(batch)
    row = inbox.get(event_id)
    assert row["status"] == "pending" and row["attempts"] == 0


# ---- cheap readiness checks used by idle loops ------------------------------------------------
def test_has_ready_agrees_with_what_claim_would_return(inbox, clock):
    assert not inbox.has_ready()
    first = inbox.add(msg("a", session="A")).id
    assert inbox.has_ready() and inbox.has_ready("A") and not inbox.has_ready("B")

    batch = inbox.claim("w1")
    assert not inbox.has_ready()  # leased and the lease is live

    clock.advance(61)  # lease expired: another worker may take it over
    assert inbox.has_ready()
    inbox.fail(inbox.claim("w2"), "boom", environmental=True, retry_in=30)
    assert not inbox.has_ready()  # waiting to retry
    clock.advance(31)
    assert inbox.has_ready()
    assert batch.ids == [first]


def test_active_for_counts_running_and_ready_events_but_not_ones_waiting_to_retry(inbox, clock):
    assert inbox.active_for("main") == 0
    inbox.add(msg("a"))
    assert inbox.active_for("main") == 1 and inbox.active_for("other") == 0
    batch = inbox.claim("w1")
    assert inbox.active_for("main") == 1  # running
    inbox.fail(batch, "no network", environmental=True, retry_in=600)
    assert inbox.active_for("main") == 0
    clock.advance(601)
    assert inbox.active_for("main") == 1
    inbox.complete(inbox.claim("w1"))
    assert inbox.active_for("main") == 0
