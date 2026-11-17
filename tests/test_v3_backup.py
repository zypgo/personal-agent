from __future__ import annotations

import json

import pytest

from personal_agent.collaboration.backup import (
    SnapshotError,
    create_snapshot,
    restore_snapshot,
    verify_snapshot,
)
from personal_agent.core.events import NewEvent
from personal_agent.db import open_database
from personal_agent.presence import LOCK_NAME, EngineLock


def test_whole_instance_restore_preserves_isolated_data(make_app, tmp_path):
    root = make_app()
    project = root.identities.create_project("owner", "Project")
    child = root.collaboration.get(project.id)
    root.transcript.add_in("web", "private")
    child.transcript.add_in("web", "shared")
    child.memory.write_text("/memories/state/knowledge/a.md", "source")
    (child.paths.workspace_dir / "answer.txt").write_text("result")
    backup = tmp_path / "snapshot"
    manifest = create_snapshot(root, backup)
    assert len(manifest["databases"]) == 2
    assert backup.stat().st_mode & 0o077 == 0
    restored = tmp_path / "restored"
    restore_snapshot(backup, restored)
    for path, expected in (("state.db", "private"), (f"spaces/{project.id}/state.db", "shared")):
        db = open_database(restored / path)
        try:
            assert db.scalar("SELECT text FROM transcript") == expected
        finally:
            db.close()
    assert (restored / f"spaces/{project.id}/memory/state/knowledge/a.md").read_text() == "source"
    assert (restored / f"spaces/{project.id}/workspace/answer.txt").read_text() == "result"


def test_backup_refuses_live_engine_and_inside_home(make_app, tmp_path):
    root = make_app()
    lock = EngineLock(root.paths.run_dir / LOCK_NAME)
    assert lock.acquire()
    try:
        with pytest.raises(SnapshotError, match="Stop"):
            create_snapshot(root, tmp_path / "snapshot")
    finally:
        lock.release()
    with pytest.raises(SnapshotError, match="outside"):
        create_snapshot(root, root.paths.home / "snapshot")


def test_restore_rejects_corruption_traversal_and_never_overwrites(make_app, tmp_path):
    root = make_app()
    backup = tmp_path / "snapshot"
    create_snapshot(root, backup)
    with pytest.raises(SnapshotError, match="new directory"):
        restore_snapshot(backup, root.paths.home)
    original = (backup / "snapshot.json").read_text()
    manifest = json.loads(original)
    manifest["files"][0]["path"] = "../escape"
    (backup / "snapshot.json").write_text(json.dumps(manifest))
    with pytest.raises(SnapshotError, match="entry"):
        restore_snapshot(backup, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()
    (backup / "snapshot.json").write_text(original)
    (backup / "state.db").write_bytes(b"corrupt")
    with pytest.raises(SnapshotError, match="checksum"):
        verify_snapshot(backup)


def test_snapshot_refuses_symlink_and_cleans_incomplete_destination(make_app, tmp_path):
    root = make_app()
    (root.paths.workspace_dir / "link").symlink_to(tmp_path)
    with pytest.raises(SnapshotError, match="symbolic"):
        create_snapshot(root, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_restore_invalidates_old_auth_and_holds_pending_side_effects(make_app, tmp_path):
    from personal_agent.channels.web.auth import WebAuth

    root = make_app()
    browser = root.web_auth.create_session("owner")
    invite = root.web_auth.invite("old unused phone")
    root.nodes.create_invite("owner", ["personal-owner"])
    root.outbox.enqueue(session_key="main", channel="web", text="pending send")
    root.timers.add_followup(name="old", prompt="do later", due_at=root.clock.now() + 10)
    root.inbox.add(
        NewEvent(source="cli", kind="message", session_key="main", payload={"text": "old work"})
    )
    snapshot = tmp_path / "snapshot"
    create_snapshot(root, snapshot)
    restored = tmp_path / "restored"
    restore_snapshot(snapshot, restored)
    db = open_database(restored / "state.db")
    try:
        auth = WebAuth(db, root.clock)
        assert auth.lookup(browser.token) is None
        assert auth.redeem(invite.code) is None
        assert db.scalar("SELECT authz_version FROM principals WHERE id='owner'") == 2
        assert db.scalar("SELECT COUNT(*) FROM node_pairings WHERE used_at IS NULL") == 0
        assert db.scalar("SELECT status FROM timers WHERE name='old'") == "paused"
        assert db.scalar("SELECT status FROM events") == "dead"
        assert db.scalar("SELECT status FROM outbox") == "cancelled"
        assert auth.redeem(auth.invite("new phone").code) is not None
    finally:
        db.close()


def test_backup_rejects_parent_symlink_pointing_into_home(make_app, tmp_path):
    root = make_app()
    alias = tmp_path / "alias"
    alias.symlink_to(root.paths.home, target_is_directory=True)
    with pytest.raises(SnapshotError, match="outside"):
        create_snapshot(root, alias / "recursive-backup")
    assert not (root.paths.home / "recursive-backup").exists()


def test_restore_rejects_target_inside_snapshot_via_parent_alias(make_app, tmp_path):
    root = make_app()
    real = tmp_path / "real"
    real.mkdir()
    snapshot = real / "snapshot"
    create_snapshot(root, snapshot)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(SnapshotError, match="outside"):
        restore_snapshot(alias / "snapshot", snapshot / "recursive-restore")
    assert not (snapshot / "recursive-restore").exists()
    verify_snapshot(snapshot)
