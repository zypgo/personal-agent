"""`pa db`: size, pruning, checks, backups, and what other commands say about a damaged database."""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import types
from pathlib import Path

from helpers import invoke
from personal_agent.config import Config, RetentionConfig
from personal_agent.db import SCHEMA_VERSION, open_database
from personal_agent.paths import Paths
from personal_agent.presence import LOCK_NAME, EngineLock
from rows import DAY, insert, rowids

BACKUP_NAME = "state-20231114-221320.db"  # the test clock starts at 2023-11-14 22:13:20 UTC


def old_events(app, count: int = 3, state: str = "done") -> None:
    for tag in range(count):
        insert(app.db, "events", state, app.clock.now() - 400 * DAY, tag)


# ---- pa db --------------------------------------------------------------------------------
def test_pa_db_shows_the_size_the_tables_and_what_could_be_removed(wired):
    app = wired()
    old_events(app, 3)
    result = invoke("db")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "state.db" in out and f"schema v{SCHEMA_VERSION}" in out
    row = next(line for line in out.splitlines() if line.startswith("events"))
    assert row.split() == ["events", "3", "3"]  # rows, and how many could go
    assert next(line for line in out.splitlines() if line.startswith("outbox")).split() == [
        "outbox",
        "0",
    ]


def test_pa_db_says_what_is_kept_and_how_long_with_the_section_name_visible(wired):
    wired(config=Config(retention=RetentionConfig(records_days=7, audit_days=0)))
    out = invoke("db").output
    assert "records 7 days" in out and "runs 180 days" in out
    assert "audit for ever" in out and "memory history 365 days" in out
    assert "config.toml [retention]" in out  # the brackets are text, not markup
    assert "has not run yet" in out


def test_pa_db_reports_the_last_upkeep(wired):
    app = wired()
    old_events(app, 2)
    app.janitor.maintain()
    app.clock.advance(3 * 3600)
    assert "daily upkeep: on, last 3h ago, removed 2" in invoke("db").output


def test_pa_db_says_when_the_daily_upkeep_is_switched_off(wired):
    wired(config=Config(retention=RetentionConfig(auto=False)))
    assert "daily upkeep: off" in invoke("db").output


def test_pa_db_reports_an_upkeep_that_stopped_early(wired, monkeypatch):
    app = wired()

    def locked() -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app.db, "optimize", locked)
    app.janitor.maintain()
    assert "stopped: database is locked" in invoke("db").output


def test_pa_db_suggests_a_vacuum_only_when_a_good_part_of_the_file_is_empty(wired):
    app = wired()
    assert "vacuum" not in invoke("db").output
    big = "x" * 3_000
    with app.db.tx():
        for _ in range(2_000):
            app.db.execute(
                "INSERT INTO events(source, kind, session_key, payload, status, created_at, available_at, done_at)"
                " VALUES ('t', 'm', 's', ?, 'done', 0, 0, 0)",
                (big,),
            )
    app.db.checkpoint("TRUNCATE")
    assert "vacuum" not in invoke("db").output  # full, not empty
    invoke("db", "prune")
    app.db.checkpoint("TRUNCATE")
    assert "could be given back by `pa db vacuum`" in invoke("db").output


# ---- pa db prune ----------------------------------------------------------------------------
def test_a_dry_run_says_what_would_go_and_removes_nothing(wired):
    app = wired()
    old_events(app, 4)
    result = invoke("db", "prune", "--dry-run")
    assert result.exit_code == 0
    assert "Would remove 4 record(s)" in result.output
    assert "events: inbox events that were handled" in result.output
    assert len(rowids(app.db, "events")) == 4
    assert app.janitor.last() is None  # a look is not an upkeep


def test_prune_removes_and_reports_by_kind_and_writes_it_down(wired):
    app = wired()
    old_events(app, 4)
    insert(app.db, "audit", "call", app.clock.now() - 1_000 * DAY, 1)
    result = invoke("db", "prune")
    assert result.exit_code == 0, result.output
    assert "Removed 5 record(s)" in result.output
    assert "events: inbox events that were handled" in result.output
    assert "tool call records" in result.output
    assert "outbox:" not in result.output and "runs:" not in result.output  # none of those went
    assert rowids(app.db, "events") == set() and rowids(app.db, "audit") == set()
    assert app.janitor.last().total == 5
    assert "Nothing is old enough to remove." in invoke("db", "prune").output


def test_prune_never_removes_what_is_alive(wired):
    app = wired()
    old_events(app, 2, state="pending")
    result = invoke("db", "prune")
    assert "Nothing is old enough to remove." in result.output
    assert len(rowids(app.db, "events")) == 2


def test_when_every_window_is_zero_prune_says_nothing_is_removed(wired):
    zero = RetentionConfig(records_days=0, runs_days=0, audit_days=0, memory_history_days=0)
    app = wired(config=Config(retention=zero))
    old_events(app, 2)
    result = invoke("db", "prune")
    assert result.exit_code == 0
    assert "Every retention window is 0" in result.output
    assert len(rowids(app.db, "events")) == 2


def test_prune_that_is_cut_short_by_a_busy_database_fails_and_says_how_far_it_got(
    wired, monkeypatch
):
    app = wired()
    old_events(app, 3)

    def locked() -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app.db, "checkpoint", lambda mode="PASSIVE": locked())
    result = invoke("db", "prune")
    assert result.exit_code == 1
    assert "Removed 3 record(s)" in result.output
    assert "Stopped before it was done: database is locked." in result.output


# ---- pa db check ----------------------------------------------------------------------------
def make_home(home) -> Paths:
    paths = Paths.resolve(home)
    paths.ensure()
    open_database(paths.db_file).close()
    return paths


def test_check_finds_a_sound_database_sound(home):
    make_home(home)
    quick = invoke("db", "check")
    assert quick.exit_code == 0, quick.output
    assert "ok: the structure of state.db is sound" in quick.output
    full = invoke("db", "check", "--full")
    assert full.exit_code == 0 and "every page and index" in full.output


def test_check_without_a_database_says_to_run_init(home):
    home.mkdir(parents=True)
    result = invoke("db", "check")
    assert result.exit_code == 2
    assert "does not exist" in result.output and "pa init" in result.output


def test_check_without_a_home_says_to_run_init(home):
    result = invoke("db", "check")
    assert result.exit_code == 2 and "pa init" in result.output


def test_check_reports_a_file_that_is_not_a_database(home):
    paths = make_home(home)
    paths.db_file.write_bytes(b"this is not a database " * 500)
    result = invoke("db", "check")
    assert result.exit_code == 1
    assert "cannot be read" in result.output and "pa db backup" in result.output


def test_check_reports_a_database_with_damaged_pages(home):
    paths = make_home(home)
    db = open_database(paths.db_file)
    for i in range(500):
        db.execute("INSERT INTO kv(key, value, updated_at) VALUES (?, ?, 0)", (f"k{i}", "v" * 200))
    db.checkpoint("TRUNCATE")
    db.close()
    raw = bytearray(paths.db_file.read_bytes())
    for offset in range(8_192, 20_000, 7):
        raw[offset] = 0xFF
    paths.db_file.write_bytes(bytes(raw))
    result = invoke("db", "check", "--full")
    assert result.exit_code == 1, result.output
    assert "cannot be read" in result.output or "problem(s) in state.db" in result.output


def test_check_names_the_problems_it_found_and_fails(home, monkeypatch):
    from personal_agent.commands import db as db_command

    make_home(home)
    monkeypatch.setattr(db_command, "problems", lambda conn, full=False: ["page 7 is damaged"])
    result = invoke("db", "check")
    assert result.exit_code == 1
    assert "page 7 is damaged" in result.output and "1 problem(s) in state.db" in result.output


def test_check_reads_the_file_without_being_able_to_write_to_it(home, monkeypatch):
    """A check of a damaged file must not change it (a connection that may write also folds the
    log into the file and deletes it when it closes)."""
    from personal_agent.commands import db as db_command

    make_home(home)
    refused = []

    def probe(conn, full=False):
        try:
            conn.execute("CREATE TABLE intruder(x)")
        except sqlite3.OperationalError as exc:
            refused.append(str(exc))
        return []

    monkeypatch.setattr(db_command, "problems", probe)
    assert invoke("db", "check").exit_code == 0
    assert refused == ["attempt to write a readonly database"]


def test_check_reads_everything_only_when_asked_to(home, monkeypatch):
    from personal_agent.commands import db as db_command

    make_home(home)
    asked: list[bool] = []
    monkeypatch.setattr(db_command, "problems", lambda conn, full=False: asked.append(full) or [])
    plain = invoke("db", "check")
    full = invoke("db", "check", "--full")
    assert asked == [False, True]
    assert "the structure" in plain.output and "every page and index" in full.output


# ---- pa db backup ---------------------------------------------------------------------------
def test_backup_copies_the_database_into_the_backups_folder_privately(wired):
    app = wired()
    app.db.kv_set("note", "kept", now=1.0)
    result = invoke("db", "backup")
    assert result.exit_code == 0, result.output
    target = app.paths.backups_dir / BACKUP_NAME
    assert f"Copied the database to {target}" in result.output.replace("\n", "")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(app.paths.backups_dir.stat().st_mode) == 0o700
    assert "That is the database only" in result.output
    copy = open_database(target)
    try:
        assert copy.kv_get("note") == "kept"
    finally:
        copy.close()


def test_backup_never_overwrites_an_earlier_one(wired):
    app = wired()
    assert invoke("db", "backup").exit_code == 0
    target = app.paths.backups_dir / BACKUP_NAME
    first = target.read_bytes()
    app.db.kv_set("later", "x", now=2.0)
    again = invoke("db", "backup")  # the same second, so the same name
    assert again.exit_code == 1 and "already exists" in again.output
    assert target.read_bytes() == first


def test_backup_to_a_chosen_file_or_folder(wired, tmp_path):
    app = wired()
    chosen = tmp_path / "mine.db"
    assert invoke("db", "backup", str(chosen)).exit_code == 0 and chosen.is_file()
    folder = tmp_path / "usb"
    folder.mkdir()
    assert invoke("db", "backup", str(folder)).exit_code == 0
    assert (folder / BACKUP_NAME).is_file()
    assert not app.paths.backups_dir.exists()  # the default place was not needed


def test_backup_into_a_folder_that_does_not_exist_is_a_usage_problem(wired, tmp_path):
    wired()
    result = invoke("db", "backup", str(tmp_path / "nowhere" / "copy.db"))
    assert result.exit_code == 2 and "is not a folder" in result.output


def test_a_backup_that_fails_leaves_nothing_behind(wired, tmp_path):
    app = wired()

    class Failing:
        def __init__(self, real):
            self.real = real

        def backup(self, target):
            raise sqlite3.OperationalError("disk is full")

        def __getattr__(self, name):
            return getattr(self.real, name)

    app.db._conn = Failing(app.db._conn)
    target = tmp_path / "copy.db"
    result = invoke("db", "backup", str(target))
    assert result.exit_code == 1 and "The backup failed: disk is full" in result.output
    assert not target.exists()


def test_a_backup_never_removes_a_file_that_was_there_before(wired, tmp_path, monkeypatch):
    """Even when something goes wrong in the very place it checked first (someone else made the
    file a moment after the check): what is not ours is not removed."""
    app = wired()
    target = tmp_path / "precious.db"
    target.write_bytes(b"mine")
    monkeypatch.setattr(
        Path, "exists", lambda self: False if self == target else Path.is_file(self)
    )
    result = invoke("db", "backup", str(target))
    assert result.exit_code == 1 and "The backup failed" in result.output
    assert target.read_bytes() == b"mine"
    assert (
        app.db.scalar("SELECT COUNT(*) FROM kv") is not None
    )  # and the agent's own database is fine


def test_a_backup_that_does_not_pass_its_own_check_is_removed(wired, tmp_path, monkeypatch):
    from personal_agent.commands import db as db_command

    app = wired()
    monkeypatch.setattr(db_command, "problems", lambda conn, full=False: ["page 7 is damaged"])
    target = tmp_path / "copy.db"
    result = invoke("db", "backup", str(target))
    assert result.exit_code == 1
    assert "did not pass its check" in result.output and "page 7 is damaged" in result.output
    assert not target.exists()
    assert app.db.version == SCHEMA_VERSION


# ---- pa db vacuum ---------------------------------------------------------------------------
def test_vacuum_refuses_while_the_agent_is_running_and_names_it(wired):
    app = wired()
    running = EngineLock(app.paths.run_dir / LOCK_NAME)
    assert running.acquire()
    try:
        result = invoke("db", "vacuum")
    finally:
        running.release()
    assert result.exit_code == 1
    assert f"The agent is running (process {os.getpid()})" in result.output
    assert "Stop it first" in result.output and "pa service uninstall" in result.output


def test_vacuum_rewrites_the_file_and_gives_the_lock_back(wired):
    app = wired()
    big = "x" * 3_000
    with app.db.tx():
        for _ in range(1_500):
            app.db.execute(
                "INSERT INTO events(source, kind, session_key, payload, status, created_at, available_at, done_at)"
                " VALUES ('t', 'm', 's', ?, 'done', 0, 0, 0)",
                (big,),
            )
    app.db.kv_set("note", "kept", now=1.0)
    invoke("db", "prune")
    app.db.checkpoint("TRUNCATE")
    before = app.paths.db_file.stat().st_size
    result = invoke("db", "vacuum")
    assert result.exit_code == 0, result.output
    assert "->" in result.output
    assert app.paths.db_file.stat().st_size < before * 0.3
    assert not EngineLock.is_taken(app.paths.run_dir / LOCK_NAME)
    assert app.db.kv_get("note") == "kept"  # what was not removed is still there


def test_vacuum_refuses_when_there_is_no_room_for_the_second_copy(wired, monkeypatch):
    app = wired()
    monkeypatch.setattr(
        shutil, "disk_usage", lambda path: types.SimpleNamespace(total=100, used=99, free=1)
    )
    result = invoke("db", "vacuum")
    assert result.exit_code == 1 and "room for a second copy" in result.output
    assert not EngineLock.is_taken(app.paths.run_dir / LOCK_NAME)


def test_vacuum_blocked_by_another_program_says_so_and_still_gives_the_lock_back(
    wired, monkeypatch
):
    app = wired()

    def busy() -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app.db, "vacuum", busy)
    result = invoke("db", "vacuum")
    assert result.exit_code == 1 and "Another program is using it" in result.output
    assert not EngineLock.is_taken(app.paths.run_dir / LOCK_NAME)


# ---- what every other command says about a database it cannot use ------------------------------
def test_a_damaged_database_is_one_line_and_a_pointer_not_a_traceback(home):
    paths = make_home(home)
    paths.db_file.write_bytes(b"this is not a database " * 500)
    result = invoke("status")
    assert result.exit_code == 1
    assert "cannot open the database" in result.output and "pa db check" in result.output
    assert "Traceback" not in result.output


def test_a_database_from_a_newer_version_is_refused_with_its_own_message(home):
    paths = make_home(home)
    conn = sqlite3.connect(paths.db_file)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    conn.close()
    result = invoke("status")
    assert result.exit_code == 2
    assert "newer than this program" in result.output and "Traceback" not in result.output


def test_the_group_is_listed_and_documented(wired):
    assert "db" in invoke("--help").output
    listing = invoke("db", "--help").output
    for command in ("prune", "check", "backup", "vacuum"):
        assert command in listing
