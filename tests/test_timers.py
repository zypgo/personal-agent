from __future__ import annotations

from datetime import UTC, datetime

import pytest

from personal_agent.clock import get_tz, to_local
from personal_agent.config import SchedulerConfig
from personal_agent.core.events import Trust
from personal_agent.core.inbox import Inbox
from personal_agent.core.timers import TimerError, Timers, next_fire

SHANGHAI = "Asia/Shanghai"


@pytest.fixture
def inbox(db, clock):
    return Inbox(db, clock)


@pytest.fixture
def timers(db, clock, inbox):
    return Timers(db, inbox, SchedulerConfig(), clock=clock, default_tz=SHANGHAI)


def epoch(*args) -> float:
    return datetime(*args, tzinfo=UTC).timestamp()


def claim_all(inbox):
    batches = []
    while (batch := inbox.claim("t")) is not None:
        batches.append(batch)
        inbox.complete(batch)
    return batches


# ---- follow-ups -------------------------------------------------------------------------
def test_a_followup_fires_once_at_its_time(timers, inbox, clock):
    timer = timers.add_followup(
        name="check-flight", prompt="Is the flight on time?", due_at=clock.now() + 600
    )

    assert timers.tick() == [] and inbox.counts()["pending"] == 0  # not yet

    clock.advance(601)
    [fired] = timers.tick()
    assert fired.timer.id == timer.id and not fired.skipped
    assert timers.tick() == []  # never again

    [batch] = claim_all(inbox)
    event = batch.events[0]
    assert (event.kind, event.source, event.session_key) == ("timer", "timer", "timer:check-flight")
    assert event.payload["prompt"] == "Is the flight on time?"
    assert event.trust is Trust.OWNER
    assert timers.get("check-flight").status == "fired"


def test_followups_can_wake_a_chosen_session(timers, inbox, clock):
    timers.add_followup(name="nudge", prompt="x", due_at=clock.now() + 1, session_key="main")
    clock.advance(2)
    timers.tick()
    assert claim_all(inbox)[0].session_key == "main"


def test_followups_in_the_past_or_absurdly_far_away_are_refused(timers, clock):
    with pytest.raises(TimerError, match="past"):
        timers.add_followup(name="a", prompt="x", due_at=clock.now() - 100)
    with pytest.raises(TimerError, match="days"):
        timers.add_followup(name="b", prompt="x", due_at=clock.now() + 400 * 86400)


def test_two_processes_ticking_at_once_fire_once(db, clock, inbox):
    one = Timers(db, inbox, SchedulerConfig(), clock=clock)
    two = Timers(db, inbox, SchedulerConfig(), clock=clock)
    one.add_followup(name="once", prompt="x", due_at=clock.now() + 1)
    clock.advance(5)

    results = [one.tick(), two.tick()]

    assert sum(len(r) for r in results) == 1
    assert inbox.counts()["pending"] == 1


# ---- schedules --------------------------------------------------------------------------
def test_a_schedule_fires_and_moves_to_the_next_local_time(timers, inbox, clock):
    clock.set(epoch(2026, 11, 14, 23, 0))  # 07:00 on the 15th in Shanghai
    timer = timers.add_schedule(
        name="brief", prompt="Morning brief", cron="30 8 * * *", tz=SHANGHAI
    )
    assert to_local(timer.due_at, get_tz(SHANGHAI)).strftime("%d %H:%M") == "15 08:30"

    clock.set(timer.due_at + 5)
    [fired] = timers.tick()
    nxt = timers.get("brief")
    assert nxt.status == "active" and nxt.fire_count == 1
    assert to_local(nxt.due_at, get_tz(SHANGHAI)).strftime("%d %H:%M") == "16 08:30"
    assert fired.late_s == pytest.approx(5)
    assert claim_all(inbox)[0].events[0].payload["scheduled_for"].startswith("2026-11-15 08:30")


def test_schedules_follow_daylight_saving_time(timers, clock):
    # New York switches to summer time on 2024-03-10; "09:00" must stay 09:00 local.
    clock.set(epoch(2024, 3, 9, 12, 0))
    timers.add_schedule(name="nine", prompt="x", cron="0 9 * * *", tz="America/New_York")
    ny = get_tz("America/New_York")
    first = timers.get("nine").due_at
    assert to_local(first, ny).strftime("%Y-%m-%d %H:%M") == "2024-03-09 09:00"

    clock.set(first + 1)
    timers.tick()
    second = timers.get("nine").due_at
    assert to_local(second, ny).strftime("%Y-%m-%d %H:%M") == "2024-03-10 09:00"
    assert second - first == 23 * 3600  # the day was an hour shorter


def test_a_long_sleep_fires_once_not_once_per_missed_day(timers, inbox, clock):
    timers.add_schedule(name="daily", prompt="x", cron="0 8 * * *", tz=SHANGHAI)
    clock.advance(5 * 86400)  # the laptop was closed for five days

    assert len(timers.tick()) == 1
    assert timers.tick() == []
    assert len(claim_all(inbox)[0].events) == 1
    assert timers.get("daily").due_at > clock.now()


def test_skip_policy_drops_a_firing_that_is_too_late_but_keeps_the_schedule(timers, inbox, clock):
    timers.add_schedule(name="lunch", prompt="x", cron="0 12 * * *", tz=SHANGHAI, misfire="skip")
    clock.advance(3 * 86400)

    [result] = timers.tick()

    assert result.skipped and result.event_id is None
    assert inbox.counts()["pending"] == 0
    assert timers.get("lunch").status == "active" and timers.get("lunch").fire_count == 0
    assert timers.get("lunch").due_at > clock.now()


def test_skip_policy_still_fires_when_only_slightly_late(timers, inbox, clock):
    timer = timers.add_schedule(
        name="lunch", prompt="x", cron="0 12 * * *", tz=SHANGHAI, misfire="skip"
    )
    clock.set(timer.due_at + 600)  # ten minutes late is within the grace period

    [result] = timers.tick()

    assert not result.skipped and inbox.counts()["pending"] == 1


def test_paused_timers_do_not_fire_and_resume_without_a_backlog(timers, inbox, clock):
    timers.add_schedule(name="daily", prompt="x", cron="0 8 * * *", tz=SHANGHAI)
    assert timers.pause("daily")
    clock.advance(3 * 86400)
    assert timers.tick() == []

    assert timers.resume("daily")
    assert timers.get("daily").due_at > clock.now()
    assert timers.tick() == []
    assert inbox.counts()["pending"] == 0


def test_cancelled_timers_never_fire(timers, inbox, clock):
    timers.add_followup(name="nope", prompt="x", due_at=clock.now() + 10)
    assert timers.cancel("nope") and not timers.cancel("nope")
    clock.advance(60)
    assert timers.tick() == [] and inbox.counts()["pending"] == 0


def test_untrusted_timers_produce_untrusted_events(timers, inbox, clock):
    timers.add_followup(name="tainted", prompt="x", due_at=clock.now() + 1, trust=Trust.UNTRUSTED)
    clock.advance(2)
    timers.tick()
    assert claim_all(inbox)[0].events[0].trust is Trust.UNTRUSTED


# ---- validation -------------------------------------------------------------------------
@pytest.mark.parametrize("cron", ["not cron", "* * * *", "0 0 8 * * *", "61 * * * *"])
def test_invalid_cron_expressions_are_refused(timers, cron):
    with pytest.raises(TimerError, match="cron"):
        timers.add_schedule(name="bad", prompt="x", cron=cron)


def test_schedules_that_fire_too_often_are_refused(timers):
    with pytest.raises(TimerError, match="minimum gap"):
        timers.add_schedule(name="spam", prompt="x", cron="* * * * *")
    timers.add_schedule(name="ok", prompt="x", cron="*/10 * * * *")


def test_unknown_time_zones_are_refused(timers):
    with pytest.raises(TimerError, match="time zone"):
        timers.add_schedule(name="tz", prompt="x", cron="0 8 * * *", tz="Mars/Olympus")


@pytest.mark.parametrize("name", ["", "Has Space", "UPPER", "-lead", "x" * 65, "12345"])
def test_bad_names_are_refused(timers, clock, name):
    with pytest.raises(TimerError, match="name"):
        timers.add_followup(name=name, prompt="x", due_at=clock.now() + 60)


def test_names_are_unique_among_live_timers_but_reusable_afterwards(timers, clock):
    timers.add_followup(name="dup", prompt="x", due_at=clock.now() + 60)
    with pytest.raises(TimerError, match="already exists"):
        timers.add_followup(name="dup", prompt="y", due_at=clock.now() + 60)
    timers.cancel("dup")
    timers.add_followup(name="dup", prompt="z", due_at=clock.now() + 60)
    assert timers.get("dup").prompt == "z"


def test_the_number_of_live_timers_is_capped(db, clock, inbox):
    timers = Timers(db, inbox, SchedulerConfig(max_timers=2), clock=clock)
    timers.add_followup(name="a", prompt="x", due_at=clock.now() + 60)
    timers.add_followup(name="b", prompt="x", due_at=clock.now() + 60)
    with pytest.raises(TimerError, match="limit 2"):
        timers.add_followup(name="c", prompt="x", due_at=clock.now() + 60)


def test_an_empty_prompt_is_refused(timers, clock):
    with pytest.raises(TimerError, match="prompt"):
        timers.add_followup(name="blank", prompt="   ", due_at=clock.now() + 60)


def test_lookup_by_id_or_name_and_listing(timers, clock):
    a = timers.add_followup(name="first", prompt="x", due_at=clock.now() + 60)
    timers.add_followup(name="second", prompt="x", due_at=clock.now() + 30)
    assert (
        timers.get(a.id).name == "first" == timers.get(str(a.id)).name == timers.get("first").name
    )
    assert [t.name for t in timers.list()] == ["second", "first"]  # soonest first
    timers.cancel("first")
    assert [t.name for t in timers.list()] == ["second"]
    assert {t.name for t in timers.list(include_finished=True)} == {"first", "second"}
    assert timers.next_due() == pytest.approx(clock.now() + 30)


def test_next_fire_is_strictly_after_the_reference_time():
    at_eight = epoch(2026, 1, 1, 0, 0)  # 08:00 in Shanghai
    assert next_fire("0 8 * * *", SHANGHAI, at_eight) == at_eight + 86400
