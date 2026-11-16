"""What the database forgets, when, and the tools for looking after the file (`maintenance.py`)."""

from __future__ import annotations

import itertools
import os
import re
import sqlite3
import stat
import threading
import time as real_time
import types
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from personal_agent import maintenance
from personal_agent.clock import ManualClock
from personal_agent.config import RetentionConfig
from personal_agent.db import WAL_KEEP_BYTES, Database, open_database
from personal_agent.maintenance import (
    EVERY_S,
    LAST_KEY,
    RULE_NAMES,
    RULES,
    Janitor,
    Tidy,
    footprint,
    problems,
    size,
)
from rows import DAY, ENDED, TABLE, insert, rowids

KEEP_30 = RetentionConfig(records_days=30, runs_days=30, audit_days=30, memory_history_days=30)
OLD, RECENT = 400 * DAY, 5 * DAY
WINDOW_RULES = {
    "records": {
        "events",
        "outbox",
        "timers",
        "approvals",
        "pairings",
        "web_invites",
        "web_sessions",
    },
    "runs": {"runs", "spend"},
    "audit": {"audit"},
    "memory_history": {"memory_history"},
}


def tidy_up(db: Database, clock: ManualClock, keep: RetentionConfig = KEEP_30) -> Janitor:
    return Janitor(db, keep, clock)


@pytest.fixture
def fast(monkeypatch):
    """No pause between batches: the tests do not need to leave the database to anyone."""
    monkeypatch.setattr(maintenance, "PAUSE_S", 0.0)


@pytest.fixture
def file_db(tmp_path):
    database = open_database(tmp_path / "state.db")
    yield database
    database.close()


# ---- what each kind of record is, and when it goes -------------------------------------------
@pytest.mark.parametrize("rule", RULE_NAMES)
def test_a_rule_removes_what_is_over_and_old_and_nothing_else(rule, db, clock, fast):
    now = clock.now()
    gone: set[int] = set()
    kept: set[int] = set()
    for tag, (state, over) in enumerate(ENDED[rule].items()):
        (gone if over else kept).add(insert(db, rule, state, now - OLD, tag))
        kept.add(insert(db, rule, state, now - RECENT, 100 + tag))  # young: stays, over or not
        kept.add(insert(db, rule, state, now + RECENT, 200 + tag))  # ends in the future: stays
    removed = tidy_up(db, clock).prune()
    assert removed[rule] == len(gone)
    assert rowids(db, rule) == kept


def test_every_rule_names_a_real_table_and_the_four_windows_cover_them_all():
    assert {rule.name for rule in RULES} == set(ENDED) == set(TABLE)
    assert len(RULE_NAMES) == len(set(RULE_NAMES))
    for rule in RULES:
        assert TABLE[rule.name] == rule.table
        assert rule.name in WINDOW_RULES[rule.window]
    assert set().union(*WINDOW_RULES.values()) == set(RULE_NAMES)


@pytest.mark.parametrize("window", ["records", "runs", "audit", "memory_history"])
def test_each_window_governs_only_its_own_rules(window, db, clock, fast):
    now = clock.now()
    for tag, rule in enumerate(RULE_NAMES):
        state = next(s for s, over in ENDED[rule].items() if over)
        insert(db, rule, state, now - OLD, tag)
        insert(db, rule, state, now - OLD - DAY, 1000 + tag)  # two, so memory keeps one
    days = {"records_days": 0, "runs_days": 0, "audit_days": 0, "memory_history_days": 0}
    days[f"{window}_days"] = 30
    removed = tidy_up(db, clock, RetentionConfig(**days)).prune()
    assert set(removed) == WINDOW_RULES[window]
    assert all(count >= 1 for count in removed.values())
    for rule in RULE_NAMES:
        if rule not in WINDOW_RULES[window]:
            assert len(rowids(db, rule)) == 2  # not touched


def test_a_window_of_zero_keeps_that_kind_of_record_for_ever(db, clock):
    now = clock.now()
    for tag, rule in enumerate(RULE_NAMES):
        state = next(s for s, over in ENDED[rule].items() if over)
        insert(db, rule, state, now - 10_000 * DAY, tag)
    nothing = RetentionConfig(records_days=0, runs_days=0, audit_days=0, memory_history_days=0)
    janitor = tidy_up(db, clock, nothing)
    assert janitor.eligible() == {}
    assert janitor.prune() == {}
    assert all(len(rowids(db, rule)) == 1 for rule in RULE_NAMES)


def test_a_row_exactly_as_old_as_the_window_stays_and_one_second_older_goes(db, clock):
    edge = clock.now() - 30 * DAY
    on_edge = insert(db, "events", "done", edge, 1)
    older = insert(db, "events", "done", edge - 1, 2)
    tidy_up(db, clock).prune()
    assert rowids(db, "events") == {on_edge}
    assert older not in rowids(db, "events")


def test_the_newest_version_of_every_memory_file_stays_however_old(db, clock):
    now = clock.now()
    ids = {
        path: [
            insert(db, "memory_history", "version", now - OLD - i * DAY, i, path=path)
            for i in range(3)
        ]
        for path in ("/m/a", "/m/b")
    }
    # i = 0 is the newest by age, but the newest by *id* is what is kept: inserted last.
    tidy_up(db, clock).prune()
    assert rowids(db, "memory_history") == {ids["/m/a"][-1], ids["/m/b"][-1]}


def test_an_approval_whose_run_is_still_waiting_is_never_removed(db, clock):
    insert(db, "approvals", "approved-but-run-suspended", clock.now() - OLD, 1)
    insert(db, "runs", "completed-but-suspended", clock.now() - OLD, 2)
    removed = tidy_up(db, clock).prune()
    assert removed["approvals"] == removed["runs"] == 0
    assert len(rowids(db, "approvals")) == len(rowids(db, "runs")) == 1


def test_messages_that_failed_or_whose_delivery_is_unclear_stay_until_dismissed(db, clock):
    insert(db, "outbox", "failed", clock.now() - OLD, 1)
    insert(db, "outbox", "maybe_sent", clock.now() - OLD, 2)
    tidy_up(db, clock).prune()
    assert len(rowids(db, "outbox")) == 2


def test_a_browser_session_ages_from_when_it_died_whichever_came_first(db, clock):
    cutoff = clock.now() - 30 * DAY

    def session(name, expires, revoked):
        db.execute(
            "INSERT INTO web_sessions(token_hash, created_at, last_seen_at, expires_at, revoked_at)"
            " VALUES (?, 0, 0, ?, ?)",
            (name, expires, revoked),
        )

    session("revoked-before-the-edge", cutoff + 5 * DAY, cutoff - 10)
    session("revoked-after-the-edge", cutoff + 5 * DAY, cutoff + 10)
    session("expired-before-the-edge-revoked-later", cutoff - 10, cutoff + 5 * DAY)
    session("expired-after-the-edge", cutoff + 10, None)
    session("still-valid", clock.now() + DAY, None)
    tidy_up(db, clock).prune()
    left = {row[0] for row in db.query("SELECT token_hash FROM web_sessions")}
    assert left == {"revoked-after-the-edge", "expired-after-the-edge", "still-valid"}


def test_a_pairing_link_ages_from_when_it_was_used_or_expired_whichever_came_first(db, clock):
    cutoff = clock.now() - 30 * DAY

    def link(name, expires, used):
        db.execute(
            "INSERT INTO web_invites(token_hash, created_at, expires_at, used_at) VALUES (?, 0, ?, ?)",
            (name, expires, used),
        )

    link("used-before-the-edge", cutoff + 5 * DAY, cutoff - 10)
    link("used-after-the-edge", cutoff + 5 * DAY, cutoff + 10)
    link("expired-before-the-edge-used-later", cutoff - 10, cutoff + 5 * DAY)
    link("expired-after-the-edge", cutoff + 10, None)
    link("still-valid", clock.now() + 600, None)
    tidy_up(db, clock).prune()
    left = {row[0] for row in db.query("SELECT token_hash FROM web_invites")}
    assert left == {"used-after-the-edge", "expired-after-the-edge", "still-valid"}


# ---- the dry run is the same question the real run asks ---------------------------------------
def test_eligible_counts_what_prune_then_removes_and_a_second_prune_finds_nothing(db, clock, fast):
    tags = itertools.count()
    for rule in RULE_NAMES:
        for state in ENDED[rule]:
            insert(db, rule, state, clock.now() - OLD, next(tags))
            insert(db, rule, state, clock.now() - OLD - DAY, next(tags))
    janitor = tidy_up(db, clock)
    before = {name: len(rowids(db, name)) for name in RULE_NAMES}
    counts = janitor.eligible()
    assert any(counts.values())
    assert {
        name: len(rowids(db, name)) for name in RULE_NAMES
    } == before  # counting removed nothing
    removed = janitor.prune()
    assert removed == counts
    assert janitor.prune() == {name: 0 for name in RULE_NAMES}
    assert janitor.eligible() == {name: 0 for name in RULE_NAMES}


# ---- in small batches ------------------------------------------------------------------------
def test_a_big_table_goes_in_several_small_transactions(db, clock, monkeypatch):
    monkeypatch.setattr(maintenance, "BATCH", 3)
    monkeypatch.setattr(maintenance, "PASS_ROWS", 7)  # also several passes over the table
    monkeypatch.setattr(maintenance, "PAUSE_S", 0.0)
    for tag in range(40):
        insert(db, "events", "done", clock.now() - OLD, tag)
    alive = {insert(db, "events", "pending", clock.now() - OLD, 100 + i) for i in range(5)}
    opened = []
    real_tx = db.tx
    monkeypatch.setattr(db, "tx", lambda: (opened.append(1), real_tx())[1])
    sizes: list[int] = []
    real_execute = db.execute

    def watching(sql, params=()):
        if sql.startswith("DELETE"):
            sizes.append(len(params))
        return real_execute(sql, params)

    monkeypatch.setattr(db, "execute", watching)
    assert tidy_up(db, clock).prune()["events"] == 40
    assert rowids(db, "events") == alive
    assert max(sizes) == 3 + 1  # three row ids and the cutoff: never more at once
    # Six passes (five of 7 rows, one of 5), each taken 3 rows at a time: 3, 3, 1 and 3, 2.
    assert len(opened) == 5 * 3 + 2


def test_the_batches_leave_the_database_to_others_in_between(db, clock, monkeypatch):
    naps: list[float] = []
    monkeypatch.setattr(maintenance, "BATCH", 5)
    monkeypatch.setattr(
        maintenance,
        "time",
        types.SimpleNamespace(sleep=naps.append, monotonic=real_time.monotonic),
    )
    for tag in range(12):
        insert(db, "events", "done", clock.now() - OLD, tag)
    tidy_up(db, clock).prune()
    assert naps == [maintenance.PAUSE_S] * 3
    assert maintenance.PAUSE_S > 0


def test_a_batch_only_removes_rows_that_still_qualify(db, clock, monkeypatch):
    """A row can change between being picked and being removed (a message nobody sent yet that
    is sent now is still not to be removed because it was `cancelled` a moment ago)."""
    monkeypatch.setattr(maintenance, "BATCH", 2)
    monkeypatch.setattr(maintenance, "PAUSE_S", 0.0)
    ids = [insert(db, "events", "done", clock.now() - OLD, tag) for tag in range(4)]
    revived = ids[3]
    real_tx = db.tx
    state = {"calls": 0}

    def tx_that_revives_once():
        state["calls"] += 1
        if state["calls"] == 2:  # after the first batch, before the second
            db.execute(
                "UPDATE events SET status = 'pending', done_at = NULL WHERE rowid = ?", (revived,)
            )
        return real_tx()

    monkeypatch.setattr(db, "tx", tx_that_revives_once)
    tidy_up(db, clock).prune()
    assert rowids(db, "events") == {revived}


def test_stop_is_asked_between_batches_and_the_rest_is_left_for_next_time(db, clock, monkeypatch):
    monkeypatch.setattr(maintenance, "BATCH", 4)
    monkeypatch.setattr(maintenance, "PAUSE_S", 0.0)
    for tag in range(20):
        insert(db, "events", "done", clock.now() - OLD, tag)
    asked = []

    def stop() -> bool:
        asked.append(1)
        return len(asked) > 3

    janitor = tidy_up(db, clock)
    partial = janitor.prune(stop=stop)
    assert 0 < partial["events"] < 20
    assert len(rowids(db, "events")) == 20 - partial["events"]
    assert janitor.prune()["events"] == 20 - partial["events"]


def test_a_stop_that_is_already_asked_for_starts_nothing(db, clock):
    for tag in range(5):
        insert(db, "events", "done", clock.now() - OLD, tag)
    assert tidy_up(db, clock).prune(stop=lambda: True) == {}
    assert len(rowids(db, "events")) == 5


# ---- the daily round --------------------------------------------------------------------------
def test_maintain_removes_what_is_due_and_writes_it_down(db, clock, fast):
    for tag in range(3):
        insert(db, "events", "done", clock.now() - OLD, tag)
    janitor = tidy_up(db, clock)
    assert janitor.last() is None
    tidy = janitor.maintain()
    assert tidy.removed["events"] == 3 and tidy.total == 3 and tidy.error is None
    assert tidy.at == clock.now()
    assert janitor.last() == tidy


def test_the_agent_tidies_when_it_never_has_and_then_once_a_day(db, clock, fast):
    janitor = tidy_up(db, clock)
    assert janitor.due()  # never done
    janitor.maintain()
    assert not janitor.due()
    clock.advance(EVERY_S - 1)
    assert not janitor.due()
    clock.advance(1)
    assert janitor.due()
    janitor.maintain()
    assert not janitor.due()


def test_a_clock_set_back_by_more_than_a_day_does_not_stop_the_upkeep_for_ever(db, clock, fast):
    janitor = tidy_up(db, clock)
    janitor.maintain()
    clock.advance(-EVERY_S - 1)  # the recorded time is now in the future
    assert janitor.due()
    clock.set(janitor.last().at - 60)  # a minute of drift is nothing
    assert not janitor.due()


def test_switched_off_the_agent_never_tidies_by_itself_but_prune_still_works(db, clock, fast):
    insert(db, "events", "done", clock.now() - OLD, 1)
    off = tidy_up(db, clock, RetentionConfig(auto=False))
    assert not off.due()
    assert off.prune()["events"] == 1


def test_a_database_that_is_busy_is_reported_not_raised_and_not_retried_at_once(
    db, clock, monkeypatch, fast
):
    insert(db, "events", "done", clock.now() - OLD, 1)

    def locked() -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "optimize", locked)
    janitor = tidy_up(db, clock)
    tidy = janitor.maintain()
    assert tidy.error == "database is locked"
    assert tidy.removed == {name: 0 for name in RULE_NAMES} | {"events": 1}  # what it did get done
    assert janitor.last() == tidy
    assert not janitor.due()  # tomorrow, not in a few seconds


def test_a_bug_is_raised_but_still_written_down_so_it_is_not_retried_in_a_loop(
    db, clock, monkeypatch, fast
):
    def broken() -> None:
        raise KeyError("oops")

    monkeypatch.setattr(db, "optimize", broken)
    janitor = tidy_up(db, clock)
    with pytest.raises(KeyError):
        janitor.maintain()
    assert janitor.last().error == "KeyError('oops')"
    assert not janitor.due()


def test_a_round_that_was_cut_short_is_not_written_down_as_done(db, clock, monkeypatch, fast):
    insert(db, "events", "done", clock.now() - OLD, 1)
    optimised = []
    monkeypatch.setattr(db, "optimize", lambda: optimised.append(1))
    janitor = tidy_up(db, clock)
    tidy = janitor.maintain(stop=lambda: True)
    assert tidy.error == "stopped early"
    assert janitor.last() is None and janitor.due()
    assert optimised == []


def test_maintain_folds_the_log_into_the_file_and_asks_sqlite_to_refresh_its_statistics(
    db, clock, monkeypatch, fast
):
    calls = []
    monkeypatch.setattr(db, "optimize", lambda: calls.append("optimize"))
    monkeypatch.setattr(db, "checkpoint", lambda mode="PASSIVE": calls.append(f"checkpoint {mode}"))
    tidy_up(db, clock).maintain()
    assert calls == ["optimize", "checkpoint PASSIVE"]


# ---- the written record -------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "not json",
        "[]",
        "null",
        "3",
        '{"at": "soon", "removed": {}}',
        '{"at": 1, "removed": []}',
        '{"at": 1, "removed": {"events": "many"}}',
        '{"at": 1}',
        '{"removed": {}}',
        '{"at": Infinity, "removed": {"events": Infinity}}',
        "[" * 100_000,
    ],
)
def test_a_damaged_record_is_no_record_rather_than_a_crash(text):
    assert Tidy.from_json(text) is None


@given(st.text())
def test_no_text_makes_reading_the_record_raise(text):
    Tidy.from_json(text)


@given(
    at=st.floats(0, 4e9),
    removed=st.dictionaries(st.sampled_from(RULE_NAMES), st.integers(0, 10**9)),
    seconds=st.floats(0, 1e5),
    error=st.none() | st.text(max_size=50),
)
def test_the_record_reads_back_as_it_was_written(at, removed, seconds, error):
    tidy = Tidy(at, removed, seconds, error)
    assert Tidy.from_json(tidy.to_json()) == tidy


def test_the_record_lives_in_the_kv_table_under_a_fixed_key(db, clock, fast):
    tidy_up(db, clock).maintain()
    assert Tidy.from_json(db.kv_get(LAST_KEY)) is not None


# ---- while the agent keeps working --------------------------------------------------------------
def test_pruning_does_not_get_in_the_way_of_writes_from_another_thread(file_db, clock):
    with file_db.tx():
        for tag in range(3_000):
            insert(file_db, "events", "done", clock.now() - OLD, tag)
    errors: list[BaseException] = []

    def prune() -> None:
        try:
            tidy_up(file_db, clock).prune()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=prune)
    worker.start()
    fresh = [insert(file_db, "events", "pending", clock.now(), 10_000 + i) for i in range(300)]
    worker.join(30)
    assert not worker.is_alive() and errors == []
    assert rowids(file_db, "events") == set(fresh)


def test_a_second_connection_can_read_and_write_while_a_prune_runs(tmp_path, clock):
    """What the daemon and a `pa` command in another terminal do at the same time."""
    path = tmp_path / "state.db"
    first, second = open_database(path), open_database(path)
    try:
        with first.tx():
            for tag in range(1_500):
                insert(first, "events", "done", clock.now() - OLD, tag)
        worker = threading.Thread(target=lambda: tidy_up(first, clock).prune())
        insert(second, "events", "pending", clock.now(), 4_999)
        worker.start()
        while worker.is_alive():
            insert(
                second,
                "events",
                "pending",
                clock.now(),
                5_000 + second.scalar("SELECT COUNT(*) FROM events"),
            )
        worker.join()
        assert second.scalar("SELECT COUNT(*) FROM events WHERE status = 'done'") == 0
        assert second.scalar("SELECT COUNT(*) FROM events WHERE status = 'pending'") >= 1
    finally:
        first.close()
        second.close()


# ---- looking at the file ------------------------------------------------------------------------
def test_size_names_bytes_in_the_largest_unit_that_keeps_the_number_small():
    assert [size(n) for n in (0, 1, 999, 1_000, 4_300, 999_949, 999_950, 12_400_000)] == [
        "0 B",
        "1 B",
        "999 B",
        "1.0 kB",
        "4.3 kB",
        "999.9 kB",
        "1.0 MB",
        "12.4 MB",
    ]
    assert size(1_200_000_000) == "1.2 GB"
    assert size(5 * 10**12) == "5000.0 GB"
    assert size(-5) == "0 B"


@given(st.integers(0, 10**15))
def test_a_size_is_always_a_number_and_one_unit_and_never_four_digits_before_the_giga(count):
    shown = size(count)
    match = re.fullmatch(r"(\d+)(?:\.(\d))? (B|kB|MB|GB)", shown)
    assert match, shown
    whole, unit = int(match.group(1)), match.group(3)
    assert (unit == "B") == (match.group(2) is None)
    assert whole < 1000 or unit == "GB"


def test_the_footprint_counts_rows_of_the_programs_tables_only(file_db, clock):
    for tag in range(3):
        insert(file_db, "events", "pending", clock.now(), tag)
    file_db.checkpoint("TRUNCATE")
    path = Path(file_db.path)
    found = footprint(file_db, path)
    assert found.rows["events"] == 3
    assert found.schema == file_db.version
    assert not [name for name in found.rows if name.startswith("transcript_fts")]
    assert "sqlite_sequence" not in found.rows
    assert found.db_bytes == found.pages * found.page_size and found.wal_bytes == 0
    assert footprint(file_db, path, rows=False).rows == {}


def test_a_big_clean_up_leaves_empty_pages_that_a_vacuum_gives_back(file_db, clock, fast):
    path = Path(file_db.path)
    blob = "x" * 4_000
    with file_db.tx():
        for _ in range(1_500):
            file_db.execute(
                """INSERT INTO events(source, kind, session_key, payload, status, created_at, available_at, done_at)
               VALUES ('t', 'm', 's', ?, 'done', ?, ?, ?)""",
                (blob, clock.now() - OLD, clock.now() - OLD, clock.now() - OLD),
            )
    file_db.checkpoint("TRUNCATE")
    full = footprint(file_db, path)
    assert tidy_up(file_db, clock).prune()["events"] == 1_500
    file_db.checkpoint("TRUNCATE")
    emptied = footprint(file_db, path)
    assert emptied.db_bytes == full.db_bytes  # SQLite keeps the pages for reuse
    assert emptied.reclaimable_bytes > full.db_bytes * 0.8
    file_db.vacuum()
    file_db.checkpoint("TRUNCATE")
    shrunk = footprint(file_db, path)
    assert shrunk.db_bytes < full.db_bytes * 0.2 and shrunk.reclaimable_bytes == 0
    assert shrunk.total_bytes == shrunk.db_bytes + shrunk.wal_bytes


def test_the_log_file_is_counted_while_it_exists(file_db, clock):
    path = Path(file_db.path)
    file_db.checkpoint("TRUNCATE")
    for tag in range(30):
        insert(file_db, "events", "pending", clock.now(), tag)
    found = footprint(file_db, path, rows=False)
    assert found.wal_bytes == os.path.getsize(str(path) + "-wal") > 0
    assert found.total_bytes == found.db_bytes + found.wal_bytes


def test_a_sound_database_has_no_problems(file_db):
    assert problems(file_db) == [] and problems(file_db, full=True) == []


def test_only_the_full_check_compares_the_indexes_with_the_tables(tmp_path):
    path = tmp_path / "x.db"
    conn = sqlite3.connect(path)
    conn.create_function("f", 1, lambda x: x, deterministic=True)
    conn.executescript(
        "CREATE TABLE t(a INTEGER); CREATE INDEX i ON t(f(a)); INSERT INTO t VALUES (1), (2), (3);"
    )
    conn.close()
    conn = sqlite3.connect(path)
    conn.create_function("f", 1, lambda x: x + 100, deterministic=True)  # the index is now wrong
    try:
        assert problems(conn) == []
        full = problems(conn, full=True)
        assert len(full) == 3 and all("index i" in line for line in full)
    finally:
        conn.close()


def test_a_row_that_points_at_nothing_is_a_problem():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE parent(id INTEGER PRIMARY KEY);"
        "CREATE TABLE child(parent INTEGER REFERENCES parent(id));"
        "INSERT INTO child VALUES (99);"
    )
    try:
        assert problems(conn) == ["a row of child points at a row that does not exist"]
    finally:
        conn.close()


def test_a_damaged_file_is_reported_by_the_quick_check_or_refused_outright(tmp_path):
    path = tmp_path / "state.db"
    db = open_database(path)
    for i in range(500):
        db.execute("INSERT INTO kv(key, value, updated_at) VALUES (?, ?, 0)", (f"k{i}", "v" * 200))
    db.checkpoint("TRUNCATE")
    db.close()
    raw = bytearray(path.read_bytes())
    for offset in range(8_192, 20_000, 7):  # scribble over table pages, leaving the header alone
        raw[offset] = 0xFF
    path.write_bytes(bytes(raw))
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        try:
            found = problems(conn, full=True)
        except sqlite3.DatabaseError:
            return  # SQLite refused to read it at all: also not a pass
        assert found
    finally:
        conn.close()


# ---- the database object's own upkeep -------------------------------------------------------------
def test_a_backup_is_a_whole_private_single_file_copy(file_db, tmp_path, clock):
    insert(file_db, "events", "pending", clock.now(), 1)
    file_db.kv_set("note", "hello", now=1.0)
    target = tmp_path / "copy.db"
    file_db.backup(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("copy")) == ["copy.db"]
    copy = sqlite3.connect(target)
    try:
        assert copy.execute("SELECT value FROM kv WHERE key = 'note'").fetchone()[0] == "hello"
        assert copy.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert copy.execute("PRAGMA user_version").fetchone()[0] == file_db.version
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        copy.close()
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("copy")) == ["copy.db"]
    reopened = open_database(target)  # and the program can use it
    assert reopened.kv_get("note") == "hello"
    reopened.close()


def test_a_backup_never_overwrites_a_file(file_db, tmp_path):
    target = tmp_path / "precious.db"
    target.write_bytes(b"mine")
    with pytest.raises(FileExistsError):
        file_db.backup(target)
    assert target.read_bytes() == b"mine"


def test_a_backup_that_fails_leaves_no_half_written_file(file_db, tmp_path):
    class Failing:
        def __init__(self, real: sqlite3.Connection) -> None:
            self.real = real

        def backup(self, target: sqlite3.Connection) -> None:
            raise sqlite3.OperationalError("disk full")

        def __getattr__(self, name: str):
            return getattr(self.real, name)

    file_db._conn = Failing(file_db._conn)  # type: ignore[assignment]
    target = tmp_path / "copy.db"
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        file_db.backup(target)
    assert not target.exists()


def test_a_backup_is_consistent_while_another_connection_writes(tmp_path):
    path = tmp_path / "state.db"
    source, writing = open_database(path), open_database(path)
    stop = threading.Event()

    def writer() -> None:
        n = 0
        while not stop.is_set():
            with writing.tx():  # a pair, written together
                writing.execute(
                    "INSERT INTO kv(key, value, updated_at) VALUES (?, 'a', 0)", (f"a{n}",)
                )
                writing.execute(
                    "INSERT INTO kv(key, value, updated_at) VALUES (?, 'b', 0)", (f"b{n}",)
                )
            n += 1

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for i in range(5):
            source.backup(tmp_path / f"copy{i}.db")
    finally:
        stop.set()
        thread.join(10)
        source.close()
        writing.close()
    for i in range(5):
        copy = sqlite3.connect(tmp_path / f"copy{i}.db")
        try:
            a = copy.execute("SELECT COUNT(*) FROM kv WHERE value = 'a'").fetchone()[0]
            b = copy.execute("SELECT COUNT(*) FROM kv WHERE value = 'b'").fetchone()[0]
            assert a == b  # a copy never holds half of what was written in one transaction
            assert problems(copy) == []
        finally:
            copy.close()


def test_checkpoint_accepts_the_four_modes_and_nothing_else(file_db):
    for mode in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
        file_db.checkpoint(mode)
    with pytest.raises(ValueError, match="BOGUS"):
        file_db.checkpoint("BOGUS); DROP TABLE kv; --")
    assert file_db.scalar("SELECT COUNT(*) FROM kv", default=0) == 0  # still there


def test_truncating_checkpoint_empties_the_log_file(file_db):
    for i in range(300):
        file_db.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES (?, ?, 0)", (f"k{i}", "v" * 500)
        )
    wal = file_db.path + "-wal"
    assert os.path.getsize(wal) > 0
    file_db.checkpoint("TRUNCATE")
    assert os.path.getsize(wal) == 0


def test_a_vacuum_cannot_start_inside_a_transaction(file_db):
    with file_db.tx(), pytest.raises(RuntimeError, match="transaction"):
        file_db.vacuum()


def test_the_log_is_cut_back_after_a_checkpoint_to_a_limit(file_db):
    assert file_db.scalar("PRAGMA journal_size_limit") == WAL_KEEP_BYTES


def test_optimize_runs_on_a_database_with_data(file_db, clock):
    for tag in range(50):
        insert(file_db, "events", "pending", clock.now(), tag)
    file_db.optimize()
