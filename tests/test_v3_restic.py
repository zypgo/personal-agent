"""Restic supplies encrypted storage; the application's snapshot boundary stays authoritative."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_agent.channels.web.auth import WebAuth
from personal_agent.cli import app as cli
from personal_agent.collaboration import restic
from personal_agent.collaboration.backup import MANIFEST, SnapshotError, create_snapshot
from personal_agent.collaboration.restic import ResticArchive, ResticError
from personal_agent.core.events import NewEvent
from personal_agent.db import open_database
from personal_agent.security.sandbox import ExecResult

IDENTITY = "a" * 64


@pytest.fixture
def archive(monkeypatch, tmp_path):
    monkeypatch.delenv("RESTIC_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("RESTIC_PASSWORD", "test-password-never-in-argv")
    return ResticArchive(
        str(tmp_path / "repository"), namespace="test-agent", executable="/test/restic"
    )


def metadata(archive, identity=IDENTITY, **changes):
    return [
        {
            "id": identity,
            "time": "2026-01-01T01:02:03.000001Z",
            "tags": [archive.tag],
            **changes,
        }
    ]


def process(monkeypatch, result):
    calls = []

    async def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        return result

    monkeypatch.setattr(restic, "run_process", execute)
    return calls


def test_backup_uploads_only_verified_files_without_shell_or_service_secrets(
    archive, monkeypatch, tmp_path, make_app
):
    root = make_app()
    (root.paths.workspace_dir / "line\nbreak.txt").write_text("valid name")
    snapshot = tmp_path / "snapshot"
    manifest = create_snapshot(root, snapshot)
    (snapshot / "unlisted-sensitive-file").write_text("never upload")
    monkeypatch.setenv("OPENAI_API_KEY", "model-key-must-stay-local")
    monkeypatch.setenv("RESTIC_PASSWORD_COMMAND", "do-not-execute")
    # Construct after setting ambient values so minimal-env behavior is tested.
    archive = ResticArchive(
        str(tmp_path / "repository"), namespace="test-agent", executable="/test/restic"
    )
    calls = []

    async def upload(argv, **kwargs):
        calls.append((argv, kwargs))
        listing = Path(argv[argv.index("--files-from-raw") + 1])
        assert listing.stat().st_mode & 0o077 == 0  # noqa: ASYNC240 - inspect this fake's input
        names = listing.read_bytes().split(b"\0")  # noqa: ASYNC240
        assert names == [
            name.encode() for name in [MANIFEST, *(entry["path"] for entry in manifest["files"])]
        ] + [b""]
        assert b"unlisted-sensitive-file" not in names
        assert b"workspace/line\nbreak.txt" in names
        return ExecResult(
            0, stdout=json.dumps({"message_type": "summary", "snapshot_id": IDENTITY})
        )

    monkeypatch.setattr(restic, "run_process", upload)
    assert archive.backup(snapshot) == IDENTITY
    assert len(calls) == 1
    argv, options = calls[0]
    assert argv[:5] == ["/test/restic", "--quiet", "--json", "--no-cache", "backup"]
    assert argv[argv.index("--tag") + 1] == archive.tag
    assert argv[argv.index("--host") + 1] == "personal-agent"
    assert "test-password" not in " ".join(argv)
    assert options["env"]["RESTIC_PASSWORD"] == "test-password-never-in-argv"
    assert "OPENAI_API_KEY" not in options["env"]
    assert "RESTIC_PASSWORD_COMMAND" not in options["env"]
    assert options["cwd"] == snapshot


def test_live_home_and_corrupt_snapshots_never_reach_restic(
    archive, monkeypatch, tmp_path, make_app
):
    root = make_app()
    calls = process(monkeypatch, ExecResult(0))
    with pytest.raises(SnapshotError):
        archive.backup(root.paths.home)
    snapshot = tmp_path / "snapshot"
    create_snapshot(root, snapshot)
    (snapshot / "state.db").write_text("corrupt")
    with pytest.raises(SnapshotError, match="checksum"):
        archive.backup(snapshot)
    assert calls == []


def test_changed_snapshot_after_upload_is_not_reported_as_success(
    archive, monkeypatch, tmp_path, make_app
):
    snapshot = tmp_path / "snapshot"
    create_snapshot(make_app(), snapshot)

    async def upload(*args, **kwargs):
        (snapshot / "state.db").write_text("changed while uploading")
        return ExecResult(
            0, stdout=json.dumps({"message_type": "summary", "snapshot_id": IDENTITY})
        )

    monkeypatch.setattr(restic, "run_process", upload)
    with pytest.raises(SnapshotError, match="checksum"):
        archive.backup(snapshot)


@pytest.mark.parametrize(
    "repository",
    [
        "s3:bucket",
        "sftp:user@host:path",
        "--option",
        "sftp://user:password@host/path",
        "sftp://host/path?option=1",
        "sftp://host/path\nanything",
        "sftp://-flag/path",
        "sftp://host/",
        "sftp://host:70000/path",
    ],
)
def test_repository_policy_rejects_passwords_options_and_unsupported_clouds(
    repository, monkeypatch
):
    monkeypatch.setenv("RESTIC_PASSWORD", "test")
    with pytest.raises(ResticError):
        ResticArchive(repository, executable="/test/restic")


def test_sftp_and_private_password_file_are_supported_without_leaking_credentials(
    monkeypatch, tmp_path
):
    password = tmp_path / "password"
    password.write_text("private")
    password.chmod(0o600)
    archive = ResticArchive(
        "sftp://backup@host.test:22/backups", password_file=password, executable="/test/restic"
    )
    calls = process(monkeypatch, ExecResult(0))
    archive.initialize()
    assert calls[0][1]["env"]["RESTIC_PASSWORD_FILE"] == str(password)
    assert "RESTIC_PASSWORD" not in calls[0][1]["env"]
    assert "sftp://" not in " ".join(calls[0][0])
    password.chmod(0o644)
    with pytest.raises(ResticError, match="private"):
        ResticArchive("/tmp/test-repo", password_file=password, executable="/test/restic")
    password.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(password)
    with pytest.raises(ResticError, match="private"):
        ResticArchive("/tmp/test-repo", password_file=alias, executable="/test/restic")


@pytest.mark.parametrize(
    "result",
    [
        ExecResult(1, stderr="password-secret host@example.test"),
        ExecResult(None, error="password-secret host@example.test"),
        ExecResult(None, timed_out=True, stdout="password-secret host@example.test"),
        ExecResult(0, truncated=True, stdout="password-secret host@example.test"),
    ],
)
def test_failed_commands_are_bounded_and_diagnostics_do_not_echo_secrets(
    archive, monkeypatch, result
):
    calls = process(monkeypatch, result)
    with pytest.raises(ResticError) as caught:
        archive.check()
    assert "secret" not in str(caught.value) and "example.test" not in str(caught.value)
    assert calls[0][1]["timeout_s"] == 300
    assert calls[0][0][-2:] == ["check", "--read-data"]


@pytest.mark.parametrize(
    "changes",
    [
        {"tags": ["another-instance"]},
        {"tags": "personal-agent-v1:test-agent"},
        {"id": "--latest"},
        {"time": "2026-01-01\nprivate-content"},
        {"time": "2026-01-01\u202e01:02:03Z"},
        {"time": "2026-01-01T01:02:03"},
    ],
)
def test_snapshot_metadata_is_checked_before_restore(archive, monkeypatch, tmp_path, changes):
    calls = process(monkeypatch, ExecResult(0, stdout=json.dumps(metadata(archive, **changes))))
    target = tmp_path / "restored"
    with pytest.raises(ResticError, match="metadata"):
        archive.restore(IDENTITY, target)
    assert len(calls) == 1 and calls[0][0][4] == "snapshots"
    assert not target.exists()


def test_restore_downloads_then_uses_original_fencing_and_holds_work(
    archive, monkeypatch, tmp_path, make_app
):
    root = make_app()
    session = root.web_auth.create_session("owner")
    invite = root.web_auth.invite("old phone")
    root.nodes.create_invite("owner", ["personal-owner"])
    root.outbox.enqueue(session_key="main", channel="web", text="pending send")
    root.timers.add_followup(name="old", prompt="later", due_at=root.clock.now() + 10)
    root.inbox.add(
        NewEvent(source="cli", kind="message", session_key="main", payload={"text": "old"})
    )
    snapshot = tmp_path / "snapshot"
    create_snapshot(root, snapshot)
    commands = []

    async def download(argv, **kwargs):
        commands.append(argv)
        if argv[4] == "snapshots":
            return ExecResult(0, stdout=json.dumps(metadata(archive)))
        assert argv[4:6] == ["restore", IDENTITY] and "--verify" in argv
        staging = Path(argv[argv.index("--target") + 1])
        assert staging.stat().st_mode & 0o077 == 0  # noqa: ASYNC240 - inspect this fake's target
        shutil.copytree(snapshot, staging, dirs_exist_ok=True)
        return ExecResult(0)

    monkeypatch.setattr(restic, "run_process", download)
    target = tmp_path / "restored"
    archive.restore(IDENTITY, target)
    assert len(commands) == 2
    db = open_database(target / "state.db")
    try:
        auth = WebAuth(db, root.clock)
        assert auth.lookup(session.token) is None
        assert auth.redeem(invite.code) is None
        assert db.scalar("SELECT authz_version FROM principals WHERE id='owner'") == 2
        assert db.scalar("SELECT COUNT(*) FROM node_pairings WHERE used_at IS NULL") == 0
        assert db.scalar("SELECT status FROM timers WHERE name='old'") == "paused"
        assert db.scalar("SELECT status FROM events") == "dead"
        assert db.scalar("SELECT status FROM outbox") == "cancelled"
    finally:
        db.close()
    assert not list(tmp_path.glob("pa-restic-restore-*"))


def test_corrupt_download_never_creates_a_live_home(archive, monkeypatch, tmp_path):
    async def download(argv, **kwargs):
        return ExecResult(0, stdout=json.dumps(metadata(archive)) if argv[4] == "snapshots" else "")

    monkeypatch.setattr(restic, "run_process", download)
    with pytest.raises(SnapshotError):
        archive.restore(IDENTITY, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    assert not list(tmp_path.glob("pa-restic-restore-*"))


def test_retention_is_explicit_scoped_and_never_prunes(archive, monkeypatch):
    calls = process(monkeypatch, ExecResult(0, stdout='[{"remove": [{"id": "one"}]}]'))
    assert archive.forget(keep_last=7) == 1
    assert calls[-1][0][-1] == "--dry-run"
    assert archive.forget(keep_last=7, apply=True) == 1
    assert "--dry-run" not in calls[-1][0]
    for argv, _ in calls:
        assert argv[argv.index("--tag") + 1] == archive.tag
        assert argv[argv.index("--keep-last") + 1] == "7"
        assert "--prune" not in argv
        assert argv[argv.index("--group-by") + 1] == "tags"
    with pytest.raises(ResticError):
        archive.forget(keep_last=0)
    assert len(calls) == 2
    process(monkeypatch, ExecResult(0, stdout='[{"remove": null}]'))
    assert archive.forget(keep_last=7) == 0
    process(monkeypatch, ExecResult(0))
    assert archive.forget(keep_last=7) == 0


def test_cli_invalid_snapshot_fails_without_traceback(archive, monkeypatch, tmp_path):
    monkeypatch.setattr(restic.shutil, "which", lambda _: "/test/restic")
    calls = process(monkeypatch, ExecResult(0))
    result = CliRunner().invoke(
        cli,
        [
            "db",
            "restic",
            "--repository",
            str(tmp_path / "repo"),
            "backup",
            str(tmp_path / "absent"),
        ],
    )
    assert result.exit_code == 1
    assert "snapshot" in result.output
    assert "Traceback" not in result.output
    assert calls == []


def test_real_restic_local_encrypted_roundtrip_and_retention(monkeypatch, tmp_path, make_app):
    executable = os.environ.get("PA_TEST_RESTIC_BINARY") or shutil.which("restic")
    if executable is None:
        pytest.skip("install restic or set PA_TEST_RESTIC_BINARY for the local integration test")
    monkeypatch.delenv("RESTIC_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("RESTIC_PASSWORD", "local-integration-test-password")
    archive = ResticArchive(str(tmp_path / "repo"), executable=executable, timeout_s=60)
    root = make_app()
    browser = root.web_auth.create_session("owner")
    root.transcript.add_in("web", "restic-roundtrip")
    snapshot = tmp_path / "snapshot"
    create_snapshot(root, snapshot)
    archive.initialize()
    identity = archive.backup(snapshot)
    assert [item.id for item in archive.snapshots()] == [identity]
    archive.check()
    restored = tmp_path / "restored"
    archive.restore(identity, restored)
    db = open_database(restored / "state.db")
    try:
        assert db.scalar("SELECT text FROM transcript") == "restic-roundtrip"
        assert WebAuth(db, root.clock).lookup(browser.token) is None
    finally:
        db.close()
    assert archive.forget(keep_last=1) == 0
    assert archive.forget(keep_last=1, apply=True) == 0
