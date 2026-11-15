"""Schedules, for every zone and every moment: cron answers must be in order, one a day means one a
day (also on the two days a year when the clocks change), and nothing a person or a model can type
as a schedule may end in anything but a message that says what is wrong with it.
"""

from __future__ import annotations

import contextlib
import itertools
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from croniter import CroniterError, croniter
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from personal_agent.clock import ManualClock
from personal_agent.config import SchedulerConfig
from personal_agent.core.inbox import Inbox
from personal_agent.core.timers import TimerError, Timers, next_fire
from personal_agent.db import open_database


def _transitions(tz: ZoneInfo, year: int) -> list[float]:
    """The moments in `year` when `tz` changes its offset, found by looking hour by hour."""
    moment = datetime(year, 1, 1, tzinfo=UTC)
    found: list[float] = []
    previous = moment.astimezone(tz).utcoffset()
    while moment.year == year:
        moment += timedelta(hours=1)
        offset = moment.astimezone(tz).utcoffset()
        if offset != previous:
            found.append(moment.timestamp())
            previous = offset
    return found


ZONES = [
    "UTC",
    "Asia/Shanghai",
    "America/New_York",
    "America/Los_Angeles",
    "Europe/London",
    "Europe/Berlin",
    "Australia/Sydney",
    "Australia/Lord_Howe",  # its clocks move by half an hour
    "Pacific/Chatham",  # and by 45 minutes of offset
    "America/Sao_Paulo",
    "America/Havana",  # the change happens at midnight
    "Asia/Beirut",  # and here the repeated hour is the one before midnight
    "Asia/Kolkata",
    "Africa/Casablanca",
]
ZONE = st.sampled_from(ZONES)
DST_ZONES = [
    zone for zone in ZONES if _transitions(ZoneInfo(zone), 2024)
]  # not Sao Paulo: it stopped
FROM = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
TO = datetime(2027, 12, 31, tzinfo=UTC).timestamp()
MOMENTS = st.integers(int(FROM), int(TO)).map(float)

# ---- cron expressions ---------------------------------------------------------------------------
NUMBER = st.integers(0, 59)


def field(low: int, high: int) -> st.SearchStrategy[str]:
    value = st.integers(low, high)
    return st.one_of(
        st.just("*"),
        value.map(str),
        st.builds(lambda a, b: f"{min(a, b)}-{max(a, b)}", value, value),
        st.lists(value, min_size=1, max_size=3, unique=True).map(
            lambda xs: ",".join(map(str, sorted(xs)))
        ),
        st.integers(1, max(2, high // 2)).map(lambda n: f"*/{n}"),
    )


CRONS = st.builds(
    " ".join,
    st.tuples(field(0, 59), field(0, 23), field(1, 28), field(1, 12), field(0, 6)).map(list),
).filter(croniter.is_valid)


def fires(cron: str, zone: str, start: float, count: int) -> list[float]:
    found, at = [], start
    for _ in range(count):
        at = next_fire(cron, zone, at)
        found.append(at)
    return found


# ---- order ----------------------------------------------------------------------------------------
@settings(max_examples=300, deadline=None)
@given(CRONS, ZONE, MOMENTS)
def test_the_next_firing_is_after_now_and_the_ones_after_it_keep_coming_in_order(cron, zone, now):
    try:
        found = fires(cron, zone, now, 8)
    except CroniterError:
        assume(False)  # a date that does not exist, such as February 31st
    assert found[0] > now
    assert all(later > earlier for earlier, later in itertools.pairwise(found))


@settings(max_examples=200, deadline=None)
@given(CRONS, ZONE, MOMENTS, st.integers(0, 10**6))
def test_a_later_moment_never_gets_an_earlier_answer(cron, zone, now, extra):
    try:
        sooner, later = next_fire(cron, zone, now), next_fire(cron, zone, now + extra)
    except CroniterError:
        assume(False)
    assert sooner <= later


@settings(max_examples=200, deadline=None)
@given(CRONS, ZONE, MOMENTS)
def test_no_firing_is_skipped_between_now_and_the_next(cron, zone, now):
    try:
        first = next_fire(cron, zone, now)
    except CroniterError:
        assume(False)
    # asking again from just before the answer gives the same answer: nothing lies in between
    assert next_fire(cron, zone, first - 1) == first


# ---- a daily time is once a day -------------------------------------------------------------------
@settings(max_examples=60, deadline=None)
@given(st.sampled_from(DST_ZONES), st.integers(0, 23), st.integers(0, 59), st.integers(2022, 2026))
def test_a_time_of_day_fires_once_on_every_date_of_the_year_including_the_days_the_clocks_change(
    zone, hour, minute, year
):
    tz = ZoneInfo(zone)
    start = datetime(year, 1, 1, tzinfo=tz).timestamp() - 1
    end = datetime(year + 1, 1, 1, tzinfo=tz).timestamp() - 1
    dates: list[date] = []
    at = start
    while (at := next_fire(f"{minute} {hour} * * *", zone, at)) <= end:
        dates.append(datetime.fromtimestamp(at, tz).date())
    days = (date(year + 1, 1, 1) - date(year, 1, 1)).days
    assert len(dates) == days, f"{len(dates)} firings in {days} days"
    assert len(set(dates)) == days


@pytest.mark.parametrize("zone", ["America/New_York", "Europe/London", "Australia/Sydney"])
def test_the_hour_that_happens_twice_has_one_daily_firing_and_the_hour_that_is_missing_still_has_one(
    zone,
):
    tz = ZoneInfo(zone)
    for hour in (0, 1, 2, 3):
        for minute in (0, 30):
            stamps = fires(
                f"{minute} {hour} * * *", zone, datetime(2024, 1, 1, tzinfo=tz).timestamp(), 366
            )
            local = [datetime.fromtimestamp(s, tz) for s in stamps]
            assert len({d.date() for d in local}) == 366


# ---- a pace stays a pace ----------------------------------------------------------------------------
# Lord Howe moves its clocks by half an hour, and croniter then skips one firing of a pace that
# is not a divisor of that half hour (a gap of 50 minutes instead of 20, once a year, on an island
# of 400 people). Every other zone moves by a whole hour.
WHOLE_HOUR_ZONES = [zone for zone in DST_ZONES if zone != "Australia/Lord_Howe"]


@pytest.mark.parametrize("zone", WHOLE_HOUR_ZONES)
@pytest.mark.parametrize("cron", ["* * * * *", "*/15 * * * *", "*/20 * * * *", "0 * * * *"])
def test_a_schedule_that_runs_all_day_keeps_its_pace_through_a_change_of_clocks(zone, cron):
    tz = ZoneInfo(zone)
    pace = {"* * * * *": 60, "*/15 * * * *": 900, "*/20 * * * *": 1200, "0 * * * *": 3600}[cron]
    # Four days either side of each of the year's clock changes
    transitions = [t for t in _transitions(tz, 2024)]
    assert transitions, f"{zone} has no change of clocks in 2024"
    for moment in transitions:
        found = fires(cron, zone, moment - 4 * 3600, 12 * 3600 // pace + 2)
        gaps = {round(b - a) for a, b in itertools.pairwise(found)}
        assert gaps == {pace}, (zone, cron, gaps)


# ---- anything typed as a schedule ----------------------------------------------------------------------
@contextlib.contextmanager
def timers() -> Iterator[Timers]:
    """A scheduler on a database of its own, gone again when the block ends."""
    with contextlib.closing(open_database(":memory:")) as db:
        yield Timers(db, Inbox(db), SchedulerConfig(), clock=ManualClock())


JUNK_CRON = st.one_of(
    st.text(max_size=40),
    st.text(min_size=130, max_size=400),
    st.lists(
        st.sampled_from(
            [
                "*",
                "*/5",
                "1-5",
                "0",
                "31",
                "2",
                "L",
                "#",
                "mon",
                "jan",
                "7",
                "60",
                "-1",
                "/",
                ",",
                "?",
            ]
        ),
        min_size=1,
        max_size=7,
    ).map(" ".join),
    st.sampled_from(
        [
            "0 0 31 2 *",
            "0 0 30 2 *",
            "0 0 31 4,6,9,11 *",
            "* * * * * *",
            "@reboot",
            "@daily",
            "\x00 * * * *",
            "1," * 50_000 + "1 * * * *",
            "x" * 1_000_000,
        ]
    ),
)
JUNK_ZONE = st.one_of(
    st.none(),
    st.sampled_from(ZONES),
    st.text(max_size=20),
    st.sampled_from(["A" * 5000, "../../etc/passwd", "America", "\udc80", "zone.tab", "/", "."]),
)


@settings(max_examples=400, deadline=None)
@given(JUNK_CRON, JUNK_ZONE)
def test_nothing_typed_as_a_schedule_fails_with_anything_but_a_message(cron, zone):
    with timers() as t:
        why = t.check_schedule("junk", "p", cron, zone)  # asking never raises either
        try:
            timer = t.add_schedule(name="junk", prompt="p", cron=cron, tz=zone)
        except TimerError as problem:
            assert str(problem) == why
            assert len(str(problem)) < 400, "the message repeats what was typed in full"
        else:
            assert why is None
            assert timer.due_at > t.clock.now()


@pytest.mark.parametrize("cron", ["0 0 31 2 *", "0 0 30 2 *", "0 0 31 4,6,9,11 *"])
def test_a_schedule_no_date_satisfies_is_refused_in_words(cron):
    with timers() as t, pytest.raises(TimerError, match="never fire"):
        t.add_schedule(name="never", prompt="p", cron=cron, tz="UTC")


@pytest.mark.parametrize("cron", ["0 0 1,31 2 *", "0 0 29 2 *", "0 0 L * *", "0 0 31 * *"])
def test_a_schedule_some_dates_satisfy_is_accepted_even_if_it_rarely_comes(cron):
    with timers() as t:
        assert t.add_schedule(name="rare", prompt="p", cron=cron, tz="UTC").due_at > 0
