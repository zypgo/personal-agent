import os
import sqlite3

import pytest

from helpers import watching_for_leaks
from personal_agent.db import MIGRATIONS, SCHEMA_VERSION, Database, open_database


def test_migrate_fresh_and_idempotent(tmp_path):
    path = tmp_path / "state.db"
    db = open_database(path)
    assert db.version == SCHEMA_VERSION
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"events", "session_events", "outbox", "runs", "memory_versions", "kv"} <= tables
    db.close()
    # re-open: nothing to do, nothing breaks
    db2 = open_database(path)
    assert db2.version == SCHEMA_VERSION
    db2.close()


def test_wal_enabled_for_file_databases(tmp_path):
    db = open_database(tmp_path / "state.db")
    assert db.scalar("PRAGMA journal_mode") == "wal"
    db.close()


def test_a_new_database_and_its_log_are_private_whatever_the_umask(tmp_path):
    """Everything the agent was told and did is in this file; SQLite would make it with the
    process's umask (readable by the group) and give the write-ahead log the same permissions."""
    before = os.umask(0o022)
    try:
        db = open_database(tmp_path / "state.db")
        names = ["state.db", "state.db-wal", "state.db-shm"]
        found = [name for name in names if (tmp_path / name).exists()]
        assert {"state.db", "state.db-wal"} <= set(found)
        for name in found:
            assert (tmp_path / name).stat().st_mode & 0o077 == 0, name
        db.close()
    finally:
        os.umask(before)


def test_an_existing_database_keeps_the_permissions_it_has(tmp_path):
    path = tmp_path / "state.db"
    open_database(path).close()
    path.chmod(0o640)
    open_database(path).close()
    assert path.stat().st_mode & 0o777 == 0o640  # `pa init` tightens it, opening does not


def test_refuses_newer_schema(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    conn.close()
    db = Database(path)
    with pytest.raises(RuntimeError, match="newer"):
        db.migrate()
    db.close()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    broken = [*MIGRATIONS, "CREATE TABLE ok_table(x); CREATE TABLE ok_table(x);"]
    monkeypatch.setattr("personal_agent.db.MIGRATIONS", broken)
    monkeypatch.setattr("personal_agent.db.SCHEMA_VERSION", len(broken))
    db = Database(tmp_path / "state.db")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate()
    # the failing migration left no trace and the version did not advance past the good one
    assert db.one("SELECT name FROM sqlite_master WHERE name='ok_table'") is None
    assert db.version == len(MIGRATIONS)
    db.close()


def test_tx_commit_and_rollback(db):
    with db.tx():
        db.execute("INSERT INTO kv(key,value,updated_at) VALUES('a','1',0)")
    assert db.scalar("SELECT value FROM kv WHERE key='a'") == "1"

    with pytest.raises(ValueError), db.tx():
        db.execute("INSERT INTO kv(key,value,updated_at) VALUES('b','2',0)")
        raise ValueError("boom")
    assert db.one("SELECT 1 FROM kv WHERE key='b'") is None


def test_nested_tx_uses_savepoints(db):
    with db.tx():
        db.execute("INSERT INTO kv(key,value,updated_at) VALUES('outer','1',0)")
        with pytest.raises(ValueError), db.tx():
            db.execute("INSERT INTO kv(key,value,updated_at) VALUES('inner','2',0)")
            raise ValueError("inner failure")
        db.execute("INSERT INTO kv(key,value,updated_at) VALUES('after','3',0)")
    keys = {r["key"] for r in db.query("SELECT key FROM kv")}
    assert keys == {"outer", "after"}


def test_the_chat_channel_tables_exist(db):
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"senders", "pairings", "transcript", "spend"} <= tables


def test_a_sender_is_unique_per_channel_and_a_sender_has_one_pending_code(db):
    import sqlite3

    db.execute("INSERT INTO senders(channel, sender, approved_at) VALUES('telegram','7',0)")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO senders(channel, sender, approved_at) VALUES('telegram','7',0)")
    db.execute("INSERT INTO senders(channel, sender, approved_at) VALUES('web','7',0)")

    def code(c):
        db.execute(
            "INSERT INTO pairings(code, channel, sender, created_at, expires_at) VALUES(?, 'telegram', '9', 0, 1)",
            (c,),
        )

    code("AAAA-BBBB")
    with pytest.raises(sqlite3.IntegrityError):
        code("CCCC-DDDD")


def test_a_message_can_enter_the_history_only_once(db):
    import sqlite3

    def add(outbox_id):
        db.execute(
            "INSERT INTO transcript(at, direction, channel, kind, text, outbox_id) VALUES(0,'out','cli','message','x',?)",
            (outbox_id,),
        )

    add(5)
    with pytest.raises(sqlite3.IntegrityError):
        add(5)
    add(None)
    add(None)  # inbound lines have no outbox id and may repeat


def test_insert_gives_the_new_id_or_none_when_or_ignore_skipped_the_row(db):
    db.execute("CREATE TABLE scratch(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)")
    first = db.insert("INSERT INTO scratch(name) VALUES(?)", ("a",))
    second = db.insert("INSERT INTO scratch(name) VALUES(?)", ("b",))
    assert isinstance(first, int) and second == first + 1

    assert db.insert("INSERT OR IGNORE INTO scratch(name) VALUES(?)", ("a",)) is None
    assert db.scalar("SELECT COUNT(*) FROM scratch") == 2  # the duplicate wrote nothing


def test_row_is_the_one_row_a_query_must_find(db):
    db.execute("INSERT INTO kv(key,value,updated_at) VALUES('a','1',0)")
    assert db.row("SELECT value FROM kv WHERE key = ?", ("a",))["value"] == "1"
    with pytest.raises(LookupError, match="expected a row: SELECT value FROM kv"):
        db.row("SELECT value FROM kv WHERE key = ?", ("missing",))


def test_a_file_that_is_not_a_database_is_not_left_open(tmp_path):
    path = tmp_path / "state.db"
    path.write_bytes(b"this is not a database " * 500)

    def attempt():
        with pytest.raises(sqlite3.DatabaseError):
            Database(path)

    with watching_for_leaks() as leaks:
        attempt()
    assert leaks == []


def test_a_database_that_cannot_be_migrated_is_not_left_open(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    conn.commit()
    conn.close()

    def attempt():
        with pytest.raises(RuntimeError, match="newer"):
            open_database(path)

    with watching_for_leaks() as leaks:
        attempt()
    assert leaks == []


# ---- an upgrade keeps what the older version stored ---------------------------------------------
def _database_at(path, version: int) -> sqlite3.Connection:
    """A database as the program left it at `version`: only the migrations up to it have run."""
    conn = sqlite3.connect(path)
    for number in range(1, version + 1):
        script = MIGRATIONS[number - 1]
        if callable(script):
            conn.execute("BEGIN IMMEDIATE")
            script(conn)
            conn.execute(f"PRAGMA user_version = {number}")
            conn.execute("COMMIT")
        else:
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;"
            )
    return conn


def _one_row_in_every_table(conn: sqlite3.Connection) -> dict[str, int]:
    """A row with a plausible value in each column of each table that takes one this way."""
    conn.execute("PRAGMA foreign_keys = OFF")
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            " AND name NOT LIKE '%_fts%'"
        )
    ]
    filled: dict[str, int] = {}
    for table in tables:
        columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
        values: list[object] = []
        for _, name, kind, *_rest in columns:
            kind = (kind or "").upper()
            if "INT" in kind:
                values.append(1)
            elif "REAL" in kind or "FLOA" in kind:
                values.append(1.5)
            elif "BLOB" in kind:
                values.append(b"x")
            else:
                values.append("{}" if name in {"data", "payload", "result", "extra"} else "x")
        names = ",".join(c[1] for c in columns)
        try:
            conn.execute(
                f"INSERT INTO {table} ({names}) VALUES ({','.join('?' * len(columns))})", values
            )
        except sqlite3.Error:
            continue  # a table with a rule a made-up row does not meet
        filled[table] = 1
    conn.commit()
    return filled


@pytest.mark.parametrize("version", [1, 5, 8, 9, 14])
def test_a_database_from_an_older_version_keeps_every_row_when_it_is_upgraded(tmp_path, version):
    path = tmp_path / "old.db"
    old = _database_at(path, version)
    stored = _one_row_in_every_table(old)
    assert len(stored) >= 6  # the check is only worth something if it has rows to lose
    old.close()
    db = open_database(path)
    try:
        assert db.version == SCHEMA_VERSION
        assert db.scalar("PRAGMA integrity_check") == "ok"
        for table in stored:
            assert db.scalar(f"SELECT COUNT(*) FROM {table}") >= 1, table  # some are seeded
    finally:
        db.close()
