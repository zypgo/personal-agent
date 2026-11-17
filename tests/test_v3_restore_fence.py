"""A restored copy of an instance starts with every old door closed.

An older snapshot must never bring back a browser, a phone link, an invitation, a node or a chat
account that was revoked after the snapshot was taken, so each of them is closed on the way in.
`test_v3_backup.py` covers the restore as a whole; this file looks at each door on its own.
"""

from __future__ import annotations

import shutil
from contextlib import closing

import pytest

from personal_agent.collaboration.backup import (
    SnapshotError,
    create_snapshot,
    restore_snapshot,
)
from personal_agent.collaboration.models import OWNER_ID, OWNER_SPACE
from personal_agent.db import open_database
from personal_agent.nodes.registry import DEFAULT_CAPABILITIES


def restored_from(root, tmp_path):
    snapshot = tmp_path / "snapshot"
    create_snapshot(root, snapshot)
    restore_snapshot(snapshot, tmp_path / "restored")
    return open_database(tmp_path / "restored" / "state.db")


def test_a_restore_closes_every_door_the_old_instance_had_open(make_app, tmp_path):
    root = make_app()
    project = root.identities.create_project(OWNER_ID, "Project")
    root.web_auth.create_session(OWNER_ID)
    root.web_auth.invite("a phone that never came")
    root.identities.create_member_invite(OWNER_ID, project.id)
    pairing = root.nodes.create_invite(OWNER_ID, [OWNER_SPACE])
    registration = root.nodes.redeem(pairing.code, DEFAULT_CAPABILITIES)
    root.nodes.create_invite(OWNER_ID, [OWNER_SPACE])
    root.db.execute(
        "INSERT INTO legacy_channel_owners(channel, sender, approved_at) VALUES ('t', '7', 0)"
    )
    code = root.identities.create_channel_binding(OWNER_ID, OWNER_SPACE).code
    root.identities.bind_channel(code, "telegram", "bot", "7", "7")
    node_version = root.db.scalar("SELECT credential_version FROM nodes")
    binding_version = root.db.scalar("SELECT version FROM channel_identities")
    assert registration and not root.db.scalar("SELECT revoked_at FROM nodes")

    with closing(restored_from(root, tmp_path)) as db:
        assert db.scalar("SELECT COUNT(*) FROM web_sessions WHERE revoked_at IS NULL") == 0
        assert db.scalar("SELECT COUNT(*) FROM web_invites WHERE used_at IS NULL") == 0
        assert db.scalar("SELECT COUNT(*) FROM collaboration_invites WHERE used_at IS NULL") == 0
        assert db.scalar("SELECT COUNT(*) FROM node_pairings WHERE used_at IS NULL") == 0
        assert db.scalar("SELECT COUNT(*) FROM legacy_channel_owners") == 0
        node = db.row("SELECT revoked_at, credential_version FROM nodes")
        assert node["revoked_at"] and node["credential_version"] == node_version + 1
        binding = db.row("SELECT revoked_at, version FROM channel_identities")
        assert binding["revoked_at"] and binding["version"] == binding_version + 1


def test_a_file_that_differs_from_the_snapshot_after_it_was_copied_stops_the_restore(
    make_app, tmp_path, monkeypatch
):
    root = make_app()
    snapshot = tmp_path / "snapshot"
    create_snapshot(root, snapshot)

    real_copy = shutil.copyfile

    def damaged_copy(source, destination, **kwargs):
        real_copy(source, destination, **kwargs)
        with open(destination, "ab") as stream:
            stream.write(b"\0")
        return destination

    monkeypatch.setattr(shutil, "copyfile", damaged_copy)
    with pytest.raises(SnapshotError, match="changed while restoring"):
        restore_snapshot(snapshot, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
