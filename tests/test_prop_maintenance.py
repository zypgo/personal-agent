"""Whatever mix of rows the database holds, pruning removes exactly what is over and old, and never
anything alive. The oracle below is the plain-language rule, written independently of the SQL."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from personal_agent import maintenance
from personal_agent.clock import ManualClock
from personal_agent.config import RetentionConfig
from personal_agent.db import Database, open_database
from personal_agent.maintenance import RULE_NAMES, Janitor, problems
from rows import DAY, ENDED, insert, rowids

# Which of the owner's four settings each kind of record answers to (the rule, restated).
WINDOW = {
    "events": "records",
    "outbox": "records",
    "timers": "records",
    "approvals": "records",
    "pairings": "records",
    "web_invites": "records",
    "web_sessions": "records",
    "runs": "runs",
    "spend": "runs",
    "audit": "audit",
    "memory_history": "memory_history",
}

DAYS = st.sampled_from([0, 1, 7, 30, 90, 365])
AGES = st.sampled_from([-3.0, 0.0, 0.5, 29.999, 30.0, 30.001, 89.0, 91.0, 400.0]) | st.floats(
    -10, 500, allow_nan=False
)
KINDS = [(rule, state) for rule in RULE_NAMES for state in sorted(ENDED[rule])]
PATHS = ("/m/a", "/m/b", "/m/c")

windows = st.builds(
    RetentionConfig,
    records_days=DAYS,
    runs_days=DAYS,
    audit_days=DAYS,
    memory_history_days=DAYS,
)
rows = st.lists(st.tuples(st.sampled_from(KINDS), AGES, st.sampled_from(PATHS)), max_size=40)


@contextmanager
def database() -> Iterator[tuple[Database, ManualClock]]:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(maintenance, "PAUSE_S", 0.0)
        db = open_database(":memory:")
        try:
            yield db, ManualClock()
        finally:
            db.close()


def survivors(keep, placed, now):
    """{rule: rowids} that the plain rule says must still be there."""
    newest: dict[str, int] = {}
    for rule, rowid, _state, _t, path in placed:
        if rule == "memory_history":
            newest[path] = max(newest.get(path, 0), rowid)
    alive: dict[str, set[int]] = defaultdict(set)
    for rule, rowid, state, t, path in placed:
        days = keep.days(WINDOW[rule])
        over = ENDED[rule][state]
        old = days != 0 and t < now - days * DAY
        protected = rule == "memory_history" and newest[path] == rowid
        if not (over and old and not protected):
            alive[rule].add(rowid)
    return alive


@settings(max_examples=150)
@given(keep=windows, specs=rows)
def test_prune_removes_exactly_what_is_over_and_old_and_nothing_alive(keep, specs):
    with database() as (db, clock):
        now = clock.now()
        placed = []
        for tag, ((rule, state), age, path) in enumerate(specs):
            t = now - age * DAY
            placed.append((rule, insert(db, rule, state, t, tag, path=path), state, t, path))
        janitor = Janitor(db, keep, clock)
        expected = survivors(keep, placed, now)
        counted = janitor.eligible()

        removed = janitor.prune()

        for rule in RULE_NAMES:
            assert rowids(db, rule) == expected[rule], (rule, specs)
        assert removed == counted  # the dry run told the truth
        assert janitor.prune() == {name: 0 for name in removed}  # and a second round finds nothing
        assert problems(db, full=True) == []


@settings(max_examples=60)
@given(keep=windows, specs=rows, batch=st.integers(1, 6), passes=st.integers(1, 9))
def test_how_the_work_is_split_into_batches_changes_nothing(keep, specs, batch, passes):
    """The same rows, pruned in tiny batches and passes or in one go, end up the same."""
    with database() as (db, clock), pytest.MonkeyPatch.context() as patch:
        now = clock.now()
        for tag, ((rule, state), age, path) in enumerate(specs):
            insert(db, rule, state, now - age * DAY, tag, path=path)
        whole = {rule: rowids(db, rule) for rule in RULE_NAMES}
        eligible = Janitor(db, keep, clock).eligible()
        patch.setattr(maintenance, "BATCH", batch)
        patch.setattr(maintenance, "PASS_ROWS", passes)
        removed = Janitor(db, keep, clock).prune()
        assert removed == eligible
        for rule in RULE_NAMES:
            assert len(whole[rule]) - len(rowids(db, rule)) == eligible.get(rule, 0)
