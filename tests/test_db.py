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
