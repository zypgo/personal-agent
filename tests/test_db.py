import sqlite3

import pytest

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
