"""The workspace file tools: what they do, and that they cannot be talked out of their box."""

from __future__ import annotations

import os
import shutil
import signal

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from helpers import owner_message
from personal_agent.config import Config, PolicyConfig, ToolsConfig
from personal_agent.tools import files
from personal_agent.tools.files import FileProblem, resolve_path
from tooling import drain, outputs


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def ws(app):
    return app.paths.workspace_dir.resolve()


def exists(path) -> bool:
    return os.path.exists(path)


def put(ws, name, text="", *, binary=None):
    path = ws / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(binary if binary is not None else text.encode())
    return path


# ---- confinement ------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["../x", "a/../../x", "/etc/passwd", "~/x", "~", "", "  ", "a\0b"])
def test_paths_outside_the_workspace_are_refused(app, raw):
    with pytest.raises(FileProblem):
        resolve_path(app, raw, write=False)
    with pytest.raises(FileProblem):
        resolve_path(app, raw, write=True)


def test_a_symlink_that_points_outside_is_followed_before_checking(app, ws, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    (ws / "link").symlink_to(secret)
    (ws / "dirlink").symlink_to(tmp_path)
    for raw in ("link", "dirlink/secret.txt", "dirlink/new.txt"):
        with pytest.raises(FileProblem):
            resolve_path(app, raw, write=True)
        with pytest.raises(FileProblem):
            resolve_path(app, raw, write=False)


def test_a_symlink_inside_the_workspace_is_fine(app, ws):
    put(ws, "real/a.txt", "x")
    (ws / "alias").symlink_to(ws / "real")
    assert resolve_path(app, "alias/a.txt", write=True) == ws / "real" / "a.txt"


def test_the_skills_folder_can_be_read_but_never_written(app):
    skill = app.paths.skills_dir / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\n---\n")
    assert resolve_path(app, str(skill), write=False) == skill.resolve()
    with pytest.raises(FileProblem, match="outside the workspace"):
        resolve_path(app, str(skill), write=True)


def test_the_workspace_folder_itself_cannot_be_overwritten(app, ws):
    with pytest.raises(FileProblem, match="workspace folder itself"):
        resolve_path(app, ".", write=True)
    assert resolve_path(app, ".", write=False) == ws


def test_the_agents_secrets_are_not_reachable_even_by_absolute_path(app):
    app.paths.env_file.write_text("OPENAI_API_KEY=x")
    for target in (app.paths.env_file, app.paths.db_file, app.paths.memory_dir):
        with pytest.raises(FileProblem):
            resolve_path(app, str(target), write=False)


# ---- a folder changed after the check --------------------------------------------------------------
@pytest.fixture
def outside(tmp_path):
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "secret.txt").write_text("OUTSIDE-SECRET")
    return folder


def swap_for_a_link(ws, name, target):
    """What a command running in the sandbox at the same moment could do inside the workspace."""
    path = ws / name
    shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
    path.symlink_to(target)


def test_a_folder_swapped_for_a_link_after_the_check_is_not_followed_when_reading(app, ws, outside):
    put(ws, "d/secret.txt", "inside")
    real = resolve_path(app, "d/secret.txt", write=False)  # checked while d was a folder
    swap_for_a_link(ws, "d", outside)  # ... and swapped before the file is opened
    with pytest.raises(FileProblem, match="changed while it was being opened"):
        files._read(app, real, 1, 10)


@pytest.mark.parametrize("name", ["d", "d/nested"])
def test_listing_does_not_follow_a_folder_swapped_after_validation(app, ws, outside, name):
    put(ws, f"{name}/inside.txt", "inside")
    (outside / "nested").mkdir()
    (outside / "nested" / "private-filename.txt").write_text("outside")
    real = resolve_path(app, name, write=False)
    swap_for_a_link(ws, "d", outside)

    with pytest.raises(FileProblem, match="changed while it was being opened"):
        files._list(app, real, 3)


def test_a_file_swapped_for_a_link_after_the_check_is_not_followed_either(app, ws, outside):
    put(ws, "f.txt", "inside")
    real = resolve_path(app, "f.txt", write=False)
    swap_for_a_link(ws, "f.txt", outside / "secret.txt")
    with pytest.raises(FileProblem, match="changed while it was being opened"):
        files._read(app, real, 1, 10)
    with pytest.raises(FileProblem, match="changed while it was being opened"):
        files._edit(app, real, "OUTSIDE", "x", False)
    assert (outside / "secret.txt").read_text() == "OUTSIDE-SECRET"


def test_a_folder_swapped_for_a_link_after_the_check_is_not_written_through(app, ws, outside):
    put(ws, "d/a.txt", "inside")
    real = resolve_path(app, "d/a.txt", write=True)
    swap_for_a_link(ws, "d", outside)
    with pytest.raises(FileProblem, match="changed while it was being opened"):
        files._write(app, real, "overwritten")
    with pytest.raises(FileProblem, match="changed while it was being opened"):
        files._edit(app, real, "a", "b", False)
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]  # nothing made or changed
    assert (outside / "secret.txt").read_text() == "OUTSIDE-SECRET"


def test_a_pipe_in_the_workspace_is_refused_and_not_waited_for(app, ws):
    os.mkfifo(ws / "pipe")

    def too_slow(*_):
        raise AssertionError("opening a pipe waited for a writer that never comes")

    previous = signal.signal(signal.SIGALRM, too_slow)
    signal.alarm(5)
    try:
        with pytest.raises(FileProblem, match="not an ordinary file"):
            read(app, "pipe")
        with pytest.raises(FileProblem, match="not an ordinary file"):
            files._edit(app, resolve_path(app, "pipe", write=True), "a", "b", False)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


async def test_reading_a_pipe_inside_a_run_gives_the_model_an_error_and_the_run_goes_on(make_app):
    model = ScriptedModel(
        [[function_call("read_file", {"path": "pipe"}, call_id="r1")], [assistant_message("ok")]]
    )
    app = make_app(model)
    os.mkfifo(app.paths.workspace_dir / "pipe")
    [result] = await run(app)
    assert result.outcome.status == "completed"
    assert outputs(app)["r1"].startswith("Error:") and "not an ordinary file" in outputs(app)["r1"]


# ---- listing --------------------------------------------------------------------------------------
def test_listing_shows_folders_first_with_sizes(app, ws):
    put(ws, "b.txt", "hello")
    put(ws, "a.txt", "x" * 2048)
    put(ws, "sub/inner.md", "# hi")
    text = files._list(app, resolve_path(app, ".", write=False), 1)
    assert text.splitlines() == ["./", "sub/", "a.txt  (2.0 KB)", "b.txt  (5 B)"]


def test_listing_goes_deeper_on_request_and_marks_links(app, ws, tmp_path):
    put(ws, "a/b/c.txt", "x")
    (ws / "out").symlink_to(tmp_path)
    text = files._list(app, resolve_path(app, ".", write=False), 3)
    assert "a/\n  b/\n    c.txt  (1 B)" in text
    assert "out  (symbolic link)" in text and str(tmp_path) not in text


def test_listing_problems_are_explained(app, ws):
    put(ws, "f.txt", "x")
    (ws / "empty").mkdir()
    with pytest.raises(FileProblem, match="does not exist"):
        files._list(app, resolve_path(app, "nope", write=False), 1)
    with pytest.raises(FileProblem, match="is a file"):
        files._list(app, resolve_path(app, "f.txt", write=False), 1)
    assert files._list(app, resolve_path(app, "empty", write=False), 1) == "empty/ is empty."


def test_a_huge_listing_is_cut_with_a_count(app, ws, monkeypatch):
    monkeypatch.setattr(files, "LIST_MAX_ENTRIES", 5)
    for i in range(12):
        put(ws, f"f{i:02}.txt", "x")
    text = files._list(app, resolve_path(app, ".", write=False), 1)
    assert text.count("f0") + text.count("f1") == 5 and "[7 more entries not shown]" in text


# ---- reading --------------------------------------------------------------------------------------
def read(app, raw, start=1, count=500):
    return files._read(app, resolve_path(app, raw, write=False), start, count)


def test_a_short_file_is_returned_whole_with_a_footer(app, ws):
    put(ws, "a.txt", "one\ntwo\nthree\n")
    assert read(app, "a.txt") == "one\ntwo\nthree\n[lines 1-3 of 3]"


def test_a_long_file_is_paged_and_the_footer_says_how_to_continue(app, ws):
    put(ws, "long.txt", "".join(f"line {i}\n" for i in range(1, 101)))
    first = read(app, "long.txt", 1, 40)
    assert first.endswith("line 40\n[lines 1-40 of 100; continue with start_line=41]")
    last = read(app, "long.txt", 81, 40)
    assert last.startswith("line 81\n") and last.endswith("line 100\n[lines 81-100 of 100]")


def test_reading_past_the_end_is_an_error_not_silence(app, ws):
    put(ws, "a.txt", "one\n")
    with pytest.raises(FileProblem, match="only 1 lines"):
        read(app, "a.txt", 5)


def test_an_empty_file_is_reported_as_empty(app, ws):
    put(ws, "e.txt", "")
    assert read(app, "e.txt") == "e.txt is empty."


def test_binary_files_and_folders_are_refused(app, ws):
    put(ws, "blob.bin", binary=b"\x89PNG\r\n\x1a\n\0\0\0data")
    (ws / "d").mkdir()
    with pytest.raises(FileProblem, match="binary"):
        read(app, "blob.bin")
    with pytest.raises(FileProblem, match="use list_files"):
        read(app, "d")
    with pytest.raises(FileProblem, match="does not exist"):
        read(app, "missing.txt")


def test_output_is_bounded_and_the_footer_still_lets_the_model_continue(make_app):
    app = make_app(config=Config.model_validate({"runtime": {"tool_output_chars": 1_000}}))
    ws = app.paths.workspace_dir.resolve()
    put(ws, "big.txt", "".join(f"{i:04} " + "x" * 90 + "\n" for i in range(200)))  # 95 chars a line
    text = read(app, "big.txt")
    assert len(text) <= 1_000 and "continue with start_line=" in text
    resume = int(text.rsplit("start_line=", 1)[1].rstrip("]"))
    assert resume > 1
    assert read(app, "big.txt", resume).startswith(
        f"{resume - 1:04} "
    )  # nothing skipped, nothing repeated
    assert f"{resume - 2:04} " in text and f"{resume - 1:04} " not in text


def test_one_enormous_line_is_cut_and_the_reader_still_makes_progress(make_app):
    config = Config.model_validate({"runtime": {"tool_output_chars": 1_000}})
    app = make_app(config=config)
    ws = app.paths.workspace_dir.resolve()
    put(ws, "min.js", "a" * 50_000 + "\nsecond\n")
    text = read(app, "min.js")
    assert "[line 1 is 50001 characters; cut]" in text and "continue with start_line=2" in text
    assert read(app, "min.js", 2).startswith("second")


def test_windows_line_endings_read_as_plain_lines(app, ws):
    put(ws, "w.txt", binary=b"a\r\nb\r\n")
    assert read(app, "w.txt") == "a\nb\n[lines 1-2 of 2]"


# ---- writing --------------------------------------------------------------------------------------
def write(app, raw, content):
    return files._write(app, resolve_path(app, raw, write=True), content)


def test_write_creates_folders_and_then_replaces(app, ws):
    assert (
        write(app, "deep/er/note.md", "# a\nb\n") == "Created deep/er/note.md (6 bytes, 2 lines)."
    )
    assert (ws / "deep/er/note.md").read_text() == "# a\nb\n"
    assert write(app, "deep/er/note.md", "new") == "Replaced deep/er/note.md (3 bytes, 1 lines)."
    assert (ws / "deep/er/note.md").read_text() == "new"


def test_write_leaves_no_temporary_files_behind(app, ws):
    write(app, "x.txt", "1")
    write(app, "x.txt", "2")
    assert sorted(p.name for p in ws.iterdir()) == ["x.txt"]


def test_write_keeps_the_mode_of_an_existing_file(app, ws):
    path = put(ws, "run.sh", "#!/bin/sh\n")
    path.chmod(0o755)
    write(app, "run.sh", "#!/bin/sh\necho hi\n")
    assert path.stat().st_mode & 0o777 == 0o755


def test_write_refuses_oversized_content_and_folders(make_app):
    app = make_app(config=Config(tools=ToolsConfig(file_max_bytes=1_000)))
    ws = app.paths.workspace_dir.resolve()
    (ws / "dir").mkdir()
    with pytest.raises(FileProblem, match="over the 1000 byte limit"):
        write(app, "big.txt", "x" * 1_001)
    with pytest.raises(FileProblem, match="is a folder"):
        write(app, "dir", "x")
    assert not (ws / "big.txt").exists()


def test_writing_through_an_alias_inside_the_workspace_changes_the_real_file(app, ws):
    target = put(ws, "real.txt", "original")
    (ws / "alias").symlink_to(target)
    write(app, "alias", "through the link")
    assert target.read_text() == "through the link" and (ws / "alias").is_symlink()


def test_a_link_swapped_in_at_the_last_moment_is_replaced_not_followed(app, ws, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    (ws / "x.txt").symlink_to(outside)
    files._write_atomic(
        app, ws / "x.txt", b"new data"
    )  # what happens if a link appears after the check
    assert outside.read_text() == "untouched"
    assert not (ws / "x.txt").is_symlink() and (ws / "x.txt").read_text() == "new data"


# ---- editing --------------------------------------------------------------------------------------
def edit(app, raw, old, new, all_=False):
    return files._edit(app, resolve_path(app, raw, write=True), old, new, all_)


def test_edit_replaces_one_exact_piece_and_shows_the_change(app, ws):
    put(ws, "c.py", "def f():\n    return 1\n\ndef g():\n    return 2\n")
    result = edit(app, "c.py", "    return 1\n", "    return 10\n")
    assert (ws / "c.py").read_text() == "def f():\n    return 10\n\ndef g():\n    return 2\n"
    assert result.startswith("Edited c.py: replaced 1 occurrence(s).")
    assert "-    return 1" in result and "+    return 10" in result


def test_edit_demands_a_unique_match_unless_told_otherwise(app, ws):
    put(ws, "t.txt", "a\nb\na\n")
    with pytest.raises(FileProblem, match="appears 2 times"):
        edit(app, "t.txt", "a", "z")
    assert (ws / "t.txt").read_text() == "a\nb\na\n"
    assert "replaced 2" in edit(app, "t.txt", "a", "z", True)
    assert (ws / "t.txt").read_text() == "z\nb\nz\n"


def test_edit_explains_every_way_it_can_fail(app, ws):
    put(ws, "t.txt", "hello\n")
    with pytest.raises(FileProblem, match="was not found"):
        edit(app, "t.txt", "bye", "x")
    with pytest.raises(FileProblem, match="identical"):
        edit(app, "t.txt", "hello", "hello")
    with pytest.raises(FileProblem, match="old_text is empty"):
        edit(app, "t.txt", "", "x")
    with pytest.raises(FileProblem, match="use write_file"):
        edit(app, "nothing.txt", "a", "b")
    put(ws, "bin.dat", binary=b"\xff\xfe\x00\x01")
    with pytest.raises(FileProblem, match="not valid UTF-8"):
        edit(app, "bin.dat", "a", "b")


def test_edit_keeps_windows_line_endings(app, ws):
    put(ws, "w.txt", binary=b"one\r\ntwo\r\nthree\r\n")
    edit(app, "w.txt", "one\ntwo\n", "uno\ndos\n")  # the model saw \n in read_file
    assert (ws / "w.txt").read_bytes() == b"uno\r\ndos\r\nthree\r\n"


# ---- inside real runs --------------------------------------------------------------------------------
def call(name, call_id="c1", **args):
    return function_call(name, args, call_id=call_id)


async def run(app, text="go"):
    app.inbox.add(owner_message(text))
    return await drain(app)


def policy(**kw) -> Config:
    return Config(policy=PolicyConfig(**kw))


async def test_reading_needs_no_approval_and_writing_asks_in_supervised_mode(make_app):
    model = ScriptedModel(
        [
            [call("list_files", "l1"), call("write_file", "w1", path="n.txt", content="hi")],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model)
    [result] = await run(app)
    ws = app.paths.workspace_dir
    assert result.outcome.status == "awaiting_approval" and not (ws / "n.txt").exists()
    [pending] = app.approvals.pending()
    assert pending.tool == "write_file" and pending.summary == "n.txt | 2 characters: hi"


async def test_an_allow_rule_lets_writes_through_and_the_model_gets_the_result(make_app):
    model = ScriptedModel(
        [
            [call("write_file", "w1", path="notes/a.md", content="# A\n")],
            [call("read_file", "r1", path="notes/a.md")],
            [assistant_message("done")],
        ]
    )
    app = make_app(model, config=policy(allow=["write_file"]))
    [result] = await run(app)
    assert result.outcome.status == "completed"
    assert (app.paths.workspace_dir / "notes/a.md").read_text() == "# A\n"
    got = outputs(app)
    assert got["w1"].startswith("Created notes/a.md") and got["r1"] == "# A\n[lines 1-1 of 1]"


async def test_a_path_that_can_never_work_is_not_put_to_the_owner(make_app):
    model = ScriptedModel(
        [[call("write_file", "w1", path="/etc/cron.d/x", content="*")], [assistant_message("ok")]]
    )
    app = make_app(model)
    [result] = await run(app)
    assert result.outcome.status == "completed" and app.approvals.pending() == []
    assert outputs(app)["w1"].startswith("Error: '/etc/cron.d/x' is outside the workspace")
    assert not exists("/etc/cron.d/x")


async def test_the_model_cannot_read_the_agents_secrets_through_the_file_tools(make_app):
    model = ScriptedModel(
        [
            [call("read_file", "r1", path=str(make_app().paths.env_file))],
            [assistant_message("no")],
        ]
    )
    app = make_app(model)
    app.paths.env_file.write_text("OPENAI_API_KEY=" + "sk-" + "s" * 30)
    await run(app)
    assert outputs(app)["r1"].startswith("Error:") and "s" * 30 not in outputs(app)["r1"]


async def test_file_tools_can_be_switched_off(make_app):
    app = make_app(config=Config(tools=ToolsConfig(files=False)))
    assert not {"read_file", "write_file", "edit_file", "list_files"} & set(app.tools.names())
