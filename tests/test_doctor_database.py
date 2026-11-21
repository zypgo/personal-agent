"""What `pa doctor` says about state.db: that it opens, which version it is, how big, and when a good
part of it is empty space that `pa db vacuum` would give back."""

from __future__ import annotations

import sqlite3

import pytest

from personal_agent import doctor
from personal_agent.db import SCHEMA_VERSION, open_database
from personal_agent.doctor import static_checks
from personal_agent.maintenance import size
from personal_agent.paths import Paths


def database_check(paths: Paths):
    return next(c for c in static_checks(paths) if c.name == "database")


def fill_and_empty(paths: Paths, rows: int, *, keep: int = 0) -> None:
    """Rows go in and (all but `keep` of them) come out again: the file stays as big as it was."""
    db = open_database(paths.db_file)
    try:
        with db.tx():
            for i in range(rows):
                db.execute(
                    "INSERT INTO kv(key, value, updated_at) VALUES (?, ?, 0)",
                    (f"k{i}", "x" * 3_000),
                )
        db.checkpoint("TRUNCATE")
        with db.tx():
            db.execute("DELETE FROM kv WHERE CAST(SUBSTR(key, 2) AS INTEGER) >= ?", (keep,))
        db.checkpoint("TRUNCATE")
    finally:
        db.close()


def test_no_database_yet_is_a_warning_that_says_what_to_run(paths):
    found = database_check(paths)
    assert (found.status, found.detail, found.fix) == ("warn", "not created yet", "Run: pa init")


def test_a_sound_database_is_fine_and_its_version_and_size_are_named(paths):
    open_database(paths.db_file).close()
    found = database_check(paths)
    assert found.status == "ok"
    assert found.detail.startswith(f"schema v{SCHEMA_VERSION}, ")
    total = paths.db_file.stat().st_size
    assert found.detail == f"schema v{SCHEMA_VERSION}, {size(total)}"


def test_a_file_that_is_not_a_database_fails_and_says_which(paths):
    paths.db_file.write_bytes(b"not a database " * 600)
    found = database_check(paths)
    assert found.status == "fail" and found.detail.startswith("cannot open state.db")


def test_a_database_from_a_newer_version_fails_with_the_fix(paths):
    open_database(paths.db_file).close()
    conn = sqlite3.connect(paths.db_file)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    found = database_check(paths)
    assert found.status == "fail"
    assert f"v{SCHEMA_VERSION + 1} is newer than this program" in found.detail
    assert found.fix == "Upgrade Qixu."


def test_mostly_empty_space_is_a_warning_that_names_the_command(paths, monkeypatch):
    monkeypatch.setattr(doctor, "EMPTY_SPACE_WARN_BYTES", 100_000)
    fill_and_empty(paths, 600)
    found = database_check(paths)
    assert found.status == "warn"
    assert "over half of it is empty space" in found.detail
    assert found.fix == "Stop the agent, then run: pa db vacuum"


def test_a_database_that_is_less_than_half_empty_is_fine(paths, monkeypatch):
    monkeypatch.setattr(doctor, "EMPTY_SPACE_WARN_BYTES", 100_000)
    fill_and_empty(paths, 600, keep=400)  # a third of the pages are free
    assert database_check(paths).status == "ok"


def test_a_small_database_is_fine_however_empty_it_is(paths):
    fill_and_empty(paths, 600)  # far below the default threshold of tens of megabytes
    assert doctor.EMPTY_SPACE_WARN_BYTES >= 10_000_000
    assert database_check(paths).status == "ok"


@pytest.mark.parametrize("name", ["database"])
def test_the_check_is_part_of_the_static_checks_in_order(paths, name):
    names = [c.name for c in static_checks(paths)]
    assert names.index(name) > names.index("config")
