"""File projections survive crashes without repeating edits or losing their version history."""

from __future__ import annotations

import pytest

from personal_agent.core.dispatcher import Dispatcher
from personal_agent.memory.store import Actor, MemoryConflictError, MemoryRestoreError
from personal_agent.memory.templates import bootstrap


class ProcessStopped(BaseException):
    pass


def test_outer_rollback_changes_neither_files_nor_versions(make_app):
    app = make_app()
    path = "/memories/state/note.md"
    actual = app.paths.state_dir / "note.md"
    app.memory.write_text(path, "before")
    count = len(app.memory.history(path))
    with pytest.raises(ProcessStopped), app.db.tx():
        app.memory.write_text(path, "after")
        assert app.memory.read_text(path) == "after"  # transactional read-your-writes
        assert actual.read_text() == "before"  # no uncommitted filesystem side effect
        raise ProcessStopped()
    assert app.memory.read_text(path) == actual.read_text() == "before"
    assert len(app.memory.history(path)) == count


def test_committed_edit_is_recovered_if_process_dies_before_projection(make_app, monkeypatch):
    app = make_app()
    path = "/memories/state/note.md"
    actual = app.paths.state_dir / "note.md"
    app.memory.write_text(path, "before")
    project = app.memory._journal.recover
    monkeypatch.setattr(app.memory._journal, "recover", lambda: None)
    app.memory.write_text(path, "after")
    assert actual.read_text() == "before"
    assert app.memory.read_text(path) == "after"
    monkeypatch.setattr(app.memory._journal, "recover", project)
    app.memory.recover()
    app.memory.recover()
    assert actual.read_text() == "after"
    assert [row["new_content"] for row in app.memory.history(path)] == ["after", "before"]


def test_recovery_after_file_replace_before_projection_ack_is_idempotent(make_app, monkeypatch):
    app = make_app()
    path = "/memories/state/note.md"
    actual = app.paths.state_dir / "note.md"
    app.memory.write_text(path, "before")
    replace = app.memory._journal._replace

    def crash_after_replace(*args):
        replace(*args)
        raise ProcessStopped()

    monkeypatch.setattr(app.memory._journal, "_replace", crash_after_replace)
    with pytest.raises(ProcessStopped):
        app.memory.write_text(path, "after")
    assert actual.read_text() == "after"
    assert (
        app.db.scalar("SELECT COUNT(*) FROM memory_documents WHERE revision != projected_revision")
        == 1
    )
    monkeypatch.setattr(app.memory._journal, "_replace", replace)
    app.memory.recover()
    assert actual.read_text() == "after"
    assert len(app.memory.history(path)) == 2
    assert (
        app.db.scalar("SELECT COUNT(*) FROM memory_documents WHERE revision != projected_revision")
        == 0
    )


def test_direct_markdown_edits_are_read_fresh_and_versioned(make_app):
    app = make_app()
    path = "/memories/state/note.md"
    actual = app.paths.state_dir / "note.md"
    app.memory.write_text(path, "agent's note")
    actual.write_text("owner's correction")
    assert app.memory.read_text(path) == "owner's correction"
    latest = app.memory.history(path)[0]
    assert latest["actor"] == "user" and latest["old_content"] == "agent's note"


def test_recovery_preserves_an_owner_edit_made_while_projection_was_pending(make_app, monkeypatch):
    app = make_app()
    path = "/memories/state/note.md"
    actual = app.paths.state_dir / "note.md"
    app.memory.write_text(path, "before")
    project = app.memory._journal.recover
    monkeypatch.setattr(app.memory._journal, "recover", lambda: None)
    app.memory.write_text(path, "committed agent edit")
    actual.write_text("new owner edit")
    monkeypatch.setattr(app.memory._journal, "recover", project)
    app.memory.recover()
    assert app.memory.read_text(path) == actual.read_text() == "new owner edit"
    assert app.memory.history(path)[0]["actor"] == "user"


def test_failed_inner_transaction_never_projects_its_memory(make_app):
    app = make_app()
    with app.db.tx():
        app.memory.write_text("/memories/state/outer.md", "outer")
        with pytest.raises(ProcessStopped), app.db.tx():
            app.memory.write_text("/memories/state/inner.md", "inner")
            raise ProcessStopped()
    assert (app.paths.state_dir / "outer.md").read_text() == "outer"
    assert not (app.paths.state_dir / "inner.md").exists()
    assert app.memory.read_text("/memories/state/inner.md") is None


def test_directory_views_include_staged_creations_and_exclude_staged_deletions(make_app):
    app = make_app()
    app.memory.write_text("/memories/state/old.md", "old")
    with app.db.tx():
        app.memory.write_text("/memories/state/nested/new.md", "new")
        app.memory.delete("/memories/state/old.md")
        listing = app.memory.view("/memories/state")
        assert "/memories/state/nested/new.md" in listing
        assert "/memories/state/old.md" not in listing
        assert "/memories/state/nested" in app.memory.view("/memories")


class RecordingChannel:
    name = "cli"

    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message.text)


async def test_delivery_commits_all_edits_then_recovers_without_appending_twice(
    make_app, monkeypatch
):
    app = make_app()
    path = "/memories/state/ledger.md"
    app.memory.write_text(path, "Header\n")
    message_id = app.outbox.enqueue(
        session_key="main",
        channel="cli",
        text="Delivered",
        extra={"interactive": True},
        on_sent=[{"command": "insert", "path": path, "insert_line": 1, "insert_text": "Entry\n"}],
    )
    channel = RecordingChannel()
    replace = app.memory._journal._replace

    def stop(*args):
        replace(*args)
        raise ProcessStopped()

    monkeypatch.setattr(app.memory._journal, "_replace", stop)
    with pytest.raises(ProcessStopped):
        await Dispatcher(app, {"cli": channel}).deliver_once()
    assert app.outbox.get(message_id).status == "sent"
    monkeypatch.setattr(app.memory._journal, "_replace", replace)
    app.recover()
    app.memory.recover()
    await Dispatcher(app, {"cli": channel}).deliver_once()
    assert channel.sent == ["Delivered"]
    assert app.memory.read_text(path) == "Header\nEntry\n"
    assert len(app.memory.history(path)) == 2


async def test_after_sent_edits_can_read_an_earlier_staged_creation(make_app):
    app = make_app()
    path = "/memories/state/new-ledger.md"
    app.outbox.enqueue(
        session_key="main",
        channel="cli",
        text="Delivered",
        extra={"interactive": True},
        on_sent=[
            {"command": "create", "path": path, "file_text": "Header\n"},
            {"command": "insert", "path": path, "insert_line": 1, "insert_text": "Entry\n"},
        ],
    )
    await Dispatcher(app, {"cli": RecordingChannel()}).deliver_once()
    assert app.memory.read_text(path) == "Header\nEntry\n"
    assert (app.paths.state_dir / "new-ledger.md").read_text() == "Header\nEntry\n"


def test_automatic_undo_refuses_to_overwrite_later_owner_changes(make_app):
    app = make_app()
    path = "/memories/state/note.md"
    app.memory.write_text(path, "before", actor=Actor.USER)
    app.memory.write_text(path, "dream", actor=Actor.DREAM, run_id="dream1")
    (app.paths.state_dir / "note.md").write_text("owner corrected")
    with pytest.raises(MemoryConflictError, match="later edit"):
        app.memory.revert_run("dream1")
    assert app.memory.read_text(path) == "owner corrected"


def test_storage_failure_during_undo_is_not_classified_as_an_owner_conflict(make_app, monkeypatch):
    app = make_app()
    path = "/memories/state/note.md"
    app.memory.write_text(path, "before", actor=Actor.USER)
    app.memory.write_text(path, "dream", actor=Actor.DREAM, run_id="dream1")

    def unavailable(*args, **kwargs):
        raise OSError("temporary storage error")

    monkeypatch.setattr(app.memory, "write_text", unavailable)
    with pytest.raises(MemoryRestoreError) as raised:
        app.memory.revert_run("dream1")
    assert not isinstance(raised.value, MemoryConflictError)
    assert isinstance(raised.value.__cause__, OSError)
    assert app.memory.read_text(path) == "dream"


def test_bootstrap_does_not_overwrite_deleted_templates_before_recovery(make_app):
    app = make_app()
    paths = ["/memories/state/MEMORY.md", "/memories/state/proposals.md"]
    before = {path: app.memory.read_text(path) for path in paths}
    for path in paths:
        app.memory.delete(path, actor=Actor.DREAM, run_id="interrupted-dream")
    assert bootstrap(app.memory) == []  # build_app runs this before the engine acquires its lock
    app.memory.revert_run("interrupted-dream")
    assert {path: app.memory.read_text(path) for path in paths} == before


def test_pending_projection_cannot_write_through_a_replaced_parent_symlink(
    make_app, monkeypatch, tmp_path
):
    app = make_app()
    project = app.memory._journal.recover
    monkeypatch.setattr(app.memory._journal, "recover", lambda: None)
    app.memory.write_text("/memories/state/new/note.md", "agent")
    outside = tmp_path / "outside"
    outside.mkdir()
    (app.paths.state_dir / "new").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(app.memory._journal, "recover", project)
    with pytest.raises(OSError, match="outside"):
        app.memory.recover()
    assert list(outside.iterdir()) == []


def test_task_index_reads_committed_memory_even_when_file_projection_failed(make_app, monkeypatch):
    app = make_app()
    path = "/memories/state/tasks/waiting.md"

    def unavailable(*args):
        raise OSError("filesystem temporarily unavailable")

    monkeypatch.setattr(app.memory._journal, "_replace", unavailable)
    app.memory.write_text(path, "# Waiting task\nstatus: in progress\n")
    assert not (app.paths.state_dir / "tasks" / "waiting.md").exists()
    assert "Waiting task" in app.memory.load_task_index()
    app.memory.write_text(path, "# Waiting task\nstatus: done\n")
    assert app.memory.load_task_index() == ""


def test_task_index_reads_transactional_creation_update_and_deletion(make_app):
    app = make_app()
    folder = "/memories/state/tasks/"
    app.memory.write_text(folder + "existing.md", "# Existing\nstatus: in progress\n")
    app.memory.write_text(folder + "deleted.md", "# Deleted\nstatus: in progress\n")
    with pytest.raises(ProcessStopped), app.db.tx():
        app.memory.write_text(folder + "new.md", "# New\nstatus: waiting\n")
        app.memory.write_text(folder + "existing.md", "# Existing\nstatus: done\n")
        app.memory.delete(folder + "deleted.md")
        text = app.memory.load_task_index()
        assert "tasks/new.md: New [status: waiting]" in text
        assert "tasks/existing.md" not in text and "tasks/deleted.md" not in text
        raise ProcessStopped()
    text = app.memory.load_task_index()
    assert "tasks/new.md" not in text
    assert "tasks/existing.md" in text and "tasks/deleted.md" in text


def test_task_index_imports_the_owners_direct_markdown_edits(make_app):
    app = make_app()
    path = "/memories/state/tasks/note.md"
    app.memory.write_text(path, "# Original\nstatus: in progress\n")
    (app.paths.state_dir / "tasks" / "note.md").write_text("# Corrected\rstatus: waiting\r")
    assert "tasks/note.md: Corrected [status: waiting]" in app.memory.load_task_index()
    assert app.memory.history(path)[0]["actor"] == "user"
