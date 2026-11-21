"""Publication is a durable snapshot, not a path the browser or chat bot can dereference."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest
from agents.tool_context import ToolContext

from personal_agent.config import Config
from personal_agent.core import artifacts as implementation
from personal_agent.core.artifact_config import ArtifactConfig
from personal_agent.core.artifacts import ArtifactError, ArtifactExpired, ArtifactStore
from personal_agent.daemon import Daemon
from personal_agent.harness.runctx import Origin, RunContext
from personal_agent.tools.artifacts import publish_artifact_tool
from personal_agent.tools.base import DREAM, FULL, SCHEDULED, UNTRUSTED, Category


def publish(app, path="report.txt", **changes):
    args = {"session_key": "main", "work_id": "events:1", "run_id": "run:1", "channel": "web"}
    args.update(changes)
    return app.artifacts.publish(path, **args)


def source(app, name="report.txt", content=b"a finished report"):
    path = app.paths.workspace_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_publication_is_private_immutable_and_one_notification(make_app):
    app = make_app()
    path = source(app)
    result = publish(app)
    artifact = result.artifact
    assert re.fullmatch(r"[a-f0-9]{32}", artifact.id)
    assert artifact.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert artifact.name == "report.txt" and artifact.mime == "text/plain"
    assert artifact.run_id == "run:1" and not result.reused
    assert "source_path" not in artifact.to_json()
    snapshot = app.paths.artifacts_dir / f"{artifact.id}.blob"
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert stat.S_IMODE(app.paths.artifacts_dir.stat().st_mode) == 0o700
    path.write_bytes(b"overwritten after publication")
    assert app.artifacts.read(artifact.id)[1] == b"a finished report"
    queued = app.outbox.get(artifact.outbox_id)
    assert queued.kind == "artifact" and not queued.must_arrive
    assert queued.payload["artifacts"] == [artifact.to_json()]
    assert queued.status == "pending"
    assert app.transcript.latest() == []  # a queue receipt is not a delivery confirmation


def test_retries_and_restarts_reuse_the_original_even_if_source_is_gone(make_app):
    app = make_app()
    path = source(app)
    original = publish(app)
    path.unlink()
    restarted = make_app()
    retried = publish(restarted, run_id="retried-run")
    assert retried.reused and retried.artifact == original.artifact
    assert restarted.artifacts.read(original.artifact.id)[1] == b"a finished report"
    assert restarted.db.scalar("SELECT COUNT(*) FROM outbox") == 1


def test_dedupe_is_per_work_and_path_not_per_run_or_content(make_app):
    app = make_app()
    source(app)
    source(app, "other.txt")
    first = publish(app).artifact
    assert publish(app, run_id="retry").artifact.id == first.id
    second = publish(app, "other.txt").artifact
    third = publish(app, work_id="events:2").artifact
    assert len({first.id, second.id, third.id}) == 3
    assert app.db.scalar("SELECT COUNT(*) FROM outbox") == 3


def test_parallel_retries_queue_once(make_app):
    app = make_app()
    source(app)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: publish(app), range(8)))
    assert len({r.artifact.id for r in results}) == 1
    assert sum(not r.reused for r in results) == 1
    assert app.db.scalar("SELECT COUNT(*) FROM outbox") == 1


@pytest.mark.parametrize(
    "path", ["../private", "sub/../../private", "/etc/passwd", "~/.env", ".", ""]
)
def test_paths_cannot_leave_the_workspace(make_app, path):
    app = make_app()
    with pytest.raises(ArtifactError):
        publish(app, path)
    assert app.db.scalar("SELECT COUNT(*) FROM outbox") == 0


def test_absolute_paths_inside_workspace_are_supported(make_app):
    app = make_app()
    path = source(app)
    first = publish(app, str(path))
    assert publish(app, "./report.txt").artifact == first.artifact


@pytest.mark.parametrize("kind", ["file_link", "directory_link", "hardlink", "fifo"])
def test_links_and_special_files_are_refused(make_app, tmp_path, kind):
    app = make_app()
    secret = tmp_path / "private.txt"
    secret.write_bytes(b"never export this")
    path = app.paths.workspace_dir / "report.txt"
    if kind == "file_link":
        path.symlink_to(secret)
    elif kind == "directory_link":
        (app.paths.workspace_dir / "linked").symlink_to(tmp_path, target_is_directory=True)
        path = app.paths.workspace_dir / "linked/private.txt"
    elif kind == "hardlink":
        os.link(secret, path)
    else:
        os.mkfifo(path)
    with pytest.raises(ArtifactError):
        publish(app, str(path))
    assert app.db.scalar("SELECT COUNT(*) FROM artifacts") == 0


def test_directory_swapped_for_a_link_between_validation_and_open_is_refused(
    make_app, tmp_path, monkeypatch
):
    app = make_app()
    source(app, "sub/report.txt")
    (tmp_path / "report.txt").write_bytes(b"private outside bytes")
    real_snapshot = app.artifacts._snapshot

    def swap_then_open(names):
        directory = app.paths.workspace_dir / "sub"
        directory.rename(app.paths.workspace_dir / "previous-sub")
        directory.symlink_to(tmp_path, target_is_directory=True)
        return real_snapshot(names)

    monkeypatch.setattr(app.artifacts, "_snapshot", swap_then_open)
    with pytest.raises(ArtifactError):
        publish(app, "sub/report.txt")
    assert not list(app.paths.artifacts_dir.iterdir())


def test_a_file_changed_while_being_read_is_not_published(make_app, monkeypatch):
    app = make_app()
    path = source(app)
    real_fstat = implementation.os.fstat
    calls = 0

    def changing(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b"changed halfway through the snapshot")
        return real_fstat(fd)

    monkeypatch.setattr(implementation.os, "fstat", changing)
    with pytest.raises(ArtifactError, match="changed"):
        publish(app)
    assert app.db.scalar("SELECT COUNT(*) FROM outbox") == 0


def test_a_replaced_workspace_root_is_not_followed(make_app, tmp_path):
    app = make_app()
    (tmp_path / "report.txt").write_bytes(b"private")
    app.paths.workspace_dir.rename(app.paths.home / "old-workspace")
    app.paths.workspace_dir.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ArtifactError):
        publish(app)


@pytest.mark.parametrize(
    "name", ["../private", "a/b", "a\\b", ".", "x\r\nInjected: yes", "a\u202eb"]
)
def test_download_names_cannot_be_paths_or_headers(make_app, name):
    app = make_app()
    source(app)
    with pytest.raises(ArtifactError):
        publish(app, name=name)


def test_file_size_storage_and_file_count_limits_are_enforced(make_app):
    app = make_app(
        config=Config(
            artifacts=ArtifactConfig(max_file_bytes=5, max_total_bytes=8, max_active_files=2)
        )
    )
    source(app, content=b"123456")
    with pytest.raises(ArtifactError, match="exceeds"):
        publish(app)
    source(app, content=b"12345")
    publish(app)
    source(app, "two.txt", b"1234")
    with pytest.raises(ArtifactError, match="quota"):
        publish(app, "two.txt")
    source(app, "empty.txt", b"")
    publish(app, "empty.txt")
    source(app, "another-empty.txt", b"")
    with pytest.raises(ArtifactError, match="count"):
        publish(app, "another-empty.txt")
    assert app.db.scalar("SELECT COUNT(*) FROM artifacts") == 2


def test_expiry_reclaims_bytes_without_republishing_previous_work(make_app):
    app = make_app(
        config=Config(
            artifacts=ArtifactConfig(max_file_bytes=5, max_total_bytes=5, retention_days=1)
        )
    )
    source(app, content=b"12345")
    first = publish(app).artifact
    app.clock.advance(86400)
    with pytest.raises(ArtifactExpired):
        app.artifacts.read(first.id)
    assert app.artifacts.sweep() == 1
    assert app.artifacts.get(first.id) is not None
    assert publish(app).artifact == first
    assert publish(app, work_id="next-work").artifact.id != first.id


def test_failed_metadata_transaction_leaves_no_queued_delivery_or_blob(make_app, monkeypatch):
    app = make_app()
    source(app)
    execute = app.db.execute

    def fail_insert(sql, params=()):
        if "INSERT INTO artifacts" in sql:
            raise RuntimeError("simulated database failure")
        return execute(sql, params)

    monkeypatch.setattr(app.db, "execute", fail_insert)
    with pytest.raises(RuntimeError, match="simulated"):
        publish(app)
    assert app.db.scalar("SELECT COUNT(*) FROM outbox") == 0
    assert not list(app.paths.artifacts_dir.iterdir())


def test_recovery_deletes_an_uncommitted_blob_and_preserves_committed_files(make_app):
    app = make_app()
    source(app)
    artifact = publish(app).artifact
    orphan = app.paths.artifacts_dir / ("a" * 32 + ".blob")
    orphan.write_bytes(b"crash before commit")
    restarted_store = ArtifactStore(
        app.db, app.paths.artifacts_dir, app.paths.workspace_dir, app.config.artifacts, app.clock
    )
    assert restarted_store.sweep() == 1
    assert restarted_store.read(artifact.id)[1] == b"a finished report"


def test_corrupted_or_replaced_snapshots_are_never_served(make_app, tmp_path):
    app = make_app()
    source(app)
    artifact = publish(app).artifact
    blob = app.paths.artifacts_dir / f"{artifact.id}.blob"
    blob.write_bytes(b"x" * artifact.size)
    with pytest.raises(ArtifactError, match="integrity"):
        app.artifacts.read(artifact.id)
    blob.unlink()
    secret = tmp_path / "secret"
    secret.write_bytes(b"private")
    blob.symlink_to(secret)
    with pytest.raises(ArtifactError):
        app.artifacts.read(artifact.id)


def test_dingtalk_fallback_does_not_fabricate_a_download_link(make_app):
    app = make_app()
    source(app)
    artifact = publish(app, channel="dingtalk", web_enabled=True).artifact
    message = app.outbox.get(artifact.outbox_id)
    assert "signed-in Qixu web console" in message.text
    assert "http" not in message.text


async def test_tool_uses_durable_work_id_and_tracks_the_queued_message(make_app):
    app = make_app()
    source(app)
    rc = RunContext(app, "run:1", "main", FULL, Origin("web"), work_id="events:42")
    ctx = ToolContext(
        context=rc, tool_name="publish_artifact", tool_call_id="call1", tool_arguments="{}"
    )
    args = json.dumps({"path": "report.txt", "name": None, "description": "Here is your report."})
    result = await publish_artifact_tool.on_invoke_tool(ctx, args)
    again = await publish_artifact_tool.on_invoke_tool(ctx, args)
    assert "Queued" in result and "already published" in again
    assert len(rc.queued_messages) == 1
    tool = app.tools.get("publish_artifact")
    assert tool.category == Category.NET_WRITE
    assert tool.profiles == {FULL, SCHEDULED}
    assert not {DREAM, UNTRUSTED} & tool.profiles


def test_disabled_publication_cannot_bypass_configuration(make_app):
    app = make_app(config=Config(artifacts=ArtifactConfig(enabled=False)))
    source(app)
    with pytest.raises(ArtifactError, match="disabled"):
        publish(app)
    assert app.tools.get("publish_artifact").unavailable(app)


async def test_daily_file_retention_still_runs_when_database_auto_prune_is_off(
    make_app, monkeypatch
):
    app = make_app(config=Config(retention={"auto": False}))
    source(app)
    artifact = publish(app).artifact
    app.clock.advance(31 * 86400)
    daemon = Daemon(app, {})

    async def stop_after_one_pass(_event, _seconds):
        daemon.stop()

    monkeypatch.setattr(daemon, "_nap", stop_after_one_pass)
    await daemon._upkeep_loop()
    assert not (app.paths.artifacts_dir / f"{artifact.id}.blob").exists()
    assert daemon.stats.errors == 0


# ---- Gaps found by breaking the code on purpose (scripts/mutation/lists/v02_channels_files.py) ----


def test_a_rewrite_of_the_same_length_is_still_noticed_by_its_timestamps(make_app, monkeypatch):
    """The size alone cannot show that a file was rewritten while it was being read."""
    app = make_app()
    path = source(app, content=b"a finished report")
    real_fstat = implementation.os.fstat
    calls = 0

    def rewriting(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b"b finished report")  # the same 17 bytes
            # Not left to the file system's timestamp granularity.
            stamp = path.stat().st_mtime_ns + 5_000_000_000
            os.utime(path, ns=(stamp, stamp))
        return real_fstat(fd)

    monkeypatch.setattr(implementation.os, "fstat", rewriting)
    with pytest.raises(ArtifactError, match="changed"):
        publish(app)
    assert app.db.scalar("SELECT COUNT(*) FROM outbox") == 0


def test_a_nul_byte_in_the_path_is_an_artifact_error_not_a_crash(make_app):
    app = make_app()
    source(app)
    # An explicit name keeps the filename check from refusing the request first.
    with pytest.raises(ArtifactError):
        publish(app, "report\0.txt", name="report.txt")
    assert app.db.scalar("SELECT COUNT(*) FROM outbox") == 0


def test_a_caption_may_have_500_characters_and_not_501(make_app):
    app = make_app()
    source(app)
    source(app, "second.txt")
    assert publish(app, description="word " * 100).artifact
    with pytest.raises(ArtifactError, match="500"):
        publish(app, "second.txt", description="word " * 100 + "x")


@pytest.mark.parametrize(
    "name, accepted",
    [
        (("report-" * 40)[:236] + ".txt", True),  # 240 bytes
        (("report-" * 40)[:237] + ".txt", False),  # 241 bytes
        ("é" * 118 + ".txt", True),  # 240 bytes in 122 characters
        ("é" * 119 + ".txt", False),  # 242 bytes in 123 characters
    ],
)
def test_a_download_name_is_limited_to_240_bytes_not_characters(name, accepted):
    if accepted:
        assert implementation.validate_artifact_metadata(name, "") == ""
    else:
        with pytest.raises(ArtifactError, match="short filename"):
            implementation.validate_artifact_metadata(name, "")


@pytest.mark.parametrize("changes", [{"work_id": ""}, {"session_key": ""}])
def test_publication_needs_a_session_and_a_durable_work_identifier(make_app, changes):
    app = make_app()
    source(app)
    with pytest.raises(ArtifactError, match="durable"):
        publish(app, **changes)
    assert app.db.scalar("SELECT COUNT(*) FROM outbox") == 0


def test_the_store_creates_its_own_folder_private_to_the_owner(make_app, tmp_path):
    app = make_app()
    folder = tmp_path / "somewhere-new" / "artifacts"
    old = os.umask(0)  # so the result does not depend on the umask of whoever runs this
    try:
        ArtifactStore(app.db, folder, app.paths.workspace_dir, app.config.artifacts, app.clock)
    finally:
        os.umask(old)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_sweeping_removes_only_the_blobs_it_made_and_nothing_else(make_app):
    app = make_app()
    source(app)
    artifact = publish(app).artifact
    folder = app.paths.artifacts_dir
    (folder / "notes.txt").write_bytes(b"somebody's file")
    (folder / "not-an-identifier.blob").write_bytes(b"not one of ours either")
    orphan = folder / ("b" * 32 + ".blob")
    orphan.write_bytes(b"left behind by a crash")
    assert app.artifacts.sweep() == 1
    assert not orphan.exists()
    assert (folder / "notes.txt").exists() and (folder / "not-an-identifier.blob").exists()
    assert app.artifacts.read(artifact.id)[1] == b"a finished report"


def tool_context(app, *, origin=None, work_id="events:42"):
    rc = RunContext(app, "run:1", "main", FULL, origin or Origin("web"), work_id=work_id)
    ctx = ToolContext(
        context=rc, tool_name="publish_artifact", tool_call_id="call1", tool_arguments="{}"
    )
    return rc, ctx


ARGS = json.dumps({"path": "report.txt", "name": None, "description": ""})


async def test_asking_again_after_expiry_says_the_snapshot_is_gone(make_app):
    app = make_app(config=Config(artifacts=ArtifactConfig(retention_days=1)))
    source(app)
    _, ctx = tool_context(app)
    await publish_artifact_tool.on_invoke_tool(ctx, ARGS)
    app.clock.advance(86400)
    again = await publish_artifact_tool.on_invoke_tool(ctx, ARGS)
    assert "expired" in again and "new source path" in again
    assert "already published for this task" not in again


@pytest.mark.parametrize("interactive", [True, False])
async def test_the_queued_file_knows_whether_a_person_is_waiting(make_app, interactive):
    app = make_app()
    source(app)
    rc, ctx = tool_context(app, origin=Origin("web", interactive=interactive))
    await publish_artifact_tool.on_invoke_tool(ctx, ARGS)
    queued = app.outbox.get(rc.queued_messages[0])
    assert queued.payload["interactive"] is interactive
