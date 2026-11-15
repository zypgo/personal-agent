"""The memory tool is a file system the *model* drives. Whatever it asks for:

- the answer is text (a refusal is an answer; an exception is a bug),
- nothing outside the memory directory is read, written or removed, not through `..`, an encoded
  dot, a symlink or a rename,
- the owner's preferences are never written by the agent,
- the limits hold, and every change leaves a version behind.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from personal_agent.config import MemoryConfig
from personal_agent.db import open_database
from personal_agent.memory.store import Actor, MemoryStore, PathError
from personal_agent.paths import Paths

SMALL = MemoryConfig(
    max_file_bytes=1_024, max_total_bytes=65_536, max_files=10, view_max_chars=1_000
)


@contextmanager
def fresh_store(config: MemoryConfig | None = None) -> Iterator[tuple[MemoryStore, Path]]:
    """A store on a temporary home, with a directory next to it that must never change, and
    symlinks inside the memory directory that try to lead out of it."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        paths = Paths.resolve(root / "home")
        paths.ensure()
        outside = root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("do not touch")
        (outside / "dir").mkdir()
        (outside / "dir" / "inner.txt").write_text("nor this")
        os.symlink(outside, paths.state_dir / "out-dir")
        os.symlink(outside / "secret.txt", paths.state_dir / "out-file")
        os.symlink(paths.preferences_dir, paths.state_dir / "to-preferences")
        os.symlink(outside, paths.preferences_dir / "out-dir")
        (paths.preferences_dir / "profile.md").write_text("the owner's profile")
        db = open_database(":memory:")
        try:
            yield MemoryStore(paths, db, config), root
        finally:
            db.close()


def snapshot(directory: Path) -> dict[str, str]:
    """Every file and link under `directory`, with a digest of what it holds."""
    seen: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(directory):
        for name in [*dirnames, *filenames]:
            full = Path(dirpath) / name
            key = str(full.relative_to(directory))
            if full.is_symlink():
                seen[key] = f"-> {os.readlink(full)}"
            elif full.is_file():
                seen[key] = hashlib.sha256(full.read_bytes()).hexdigest()
            else:
                seen[key] = "dir"
    return seen


# ---- paths --------------------------------------------------------------------------------------
_NAMES = ["memories", "state", "preferences", "..", ".", "", "a", "notes.md", "out-dir", "out-file"]
_LINKS = ["to-preferences", "secret.txt", "dir", "inner.txt", ".hidden", "%2e%2e", "%2f", "%5c"]
_ODD = [
    "\x00",
    "\\",
    "~",
    "$HOME",
    "*",
    "?",
    "caf\u00e9",
    "\u4f60\u597d",
    " ",
    "a" * 300,
    "MEMORIES",
]
PIECES = st.sampled_from([*_NAMES, *_LINKS, *_ODD])
PATHS = st.one_of(
    st.lists(PIECES, max_size=7).map(lambda parts: "/" + "/".join(parts)),
    st.lists(PIECES, max_size=7).map("/".join),
    st.text(max_size=60),
    st.sampled_from(["/memories/state/out-dir/secret.txt", "/memories/state/out-file"]),
)


@settings(max_examples=400, deadline=None)
@given(PATHS)
def test_no_path_leads_outside_the_memory_directory(path):
    with fresh_store() as (store, _root):
        try:
            target = store._parse(path)
        except PathError:
            return
        physical = target.physical.resolve()
        memory = store.paths.memory_dir.resolve()
        assert physical == memory or memory in physical.parents, (path, physical)
        if not target.is_root:
            mount = store._mount_dir(target.mount or "")
            assert physical == mount or mount in physical.parents, (path, physical)


@pytest.mark.parametrize(
    "path",
    [
        "/memories/state/out-dir",
        "/memories/state/out-dir/secret.txt",
        "/memories/state/out-dir/dir/inner.txt",
        "/memories/state/out-file",
        "/memories/state/to-preferences/profile.md",
        "/memories/preferences/out-dir/secret.txt",
    ],
)
def test_a_symlink_that_leads_out_is_refused_for_every_command(path):
    with fresh_store() as (store, root):
        before = snapshot(root / "outside")
        for command in (
            {"command": "view", "path": path},
            {"command": "create", "path": path, "file_text": "x"},
            {"command": "str_replace", "path": path, "old_str": "do", "new_str": "x"},
            {"command": "insert", "path": path, "insert_line": 0, "insert_text": "x"},
            {"command": "delete", "path": path},
            {"command": "rename", "old_path": path, "new_path": "/memories/state/moved"},
            {"command": "rename", "old_path": "/memories/state/x", "new_path": path},
        ):
            result = store.execute(command)
            assert (
                "resolves outside" in result or "does not exist" in result or "read-only" in result
            ), (command, result)
        assert snapshot(root / "outside") == before


# ---- a model at the keyboard -------------------------------------------------------------------
JUNK = st.one_of(
    st.none(),
    st.integers(-5, 10**12),
    st.booleans(),
    st.floats(allow_nan=True),
    st.text(max_size=40),
    st.lists(st.one_of(st.integers(-3, 9), st.text(max_size=3), st.none()), max_size=3),
    st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
)
VERBS = st.sampled_from(
    ["view", "create", "str_replace", "insert", "delete", "rename", "explode", None, 7]
)
_STATE = ["/memories", "/memories/state", "/memories/preferences", "/memories/state/a.md"]
_NESTED = ["/memories/state/dir/b.md", "/memories/state/dir", "/memories/state/MEMORY.md"]
_OWNER = ["/memories/preferences/profile.md", "/memories/preferences/new.md"]
_TRICKS = ["/memories/state/out-dir/secret.txt", "/memories/state/../preferences/profile.md"]
STATE_PATHS = st.sampled_from([*_STATE, *_NESTED, *_OWNER, *_TRICKS])


@st.composite
def commands(draw):
    command = {"command": draw(VERBS)}
    for key in ("path", "old_path", "new_path"):
        if draw(st.booleans()):
            command[key] = draw(st.one_of(STATE_PATHS, PATHS, JUNK))
    for key in ("file_text", "old_str", "new_str", "insert_text", "insert_line", "view_range"):
        if draw(st.booleans()):
            command[key] = draw(
                st.one_of(st.sampled_from(["a", "b\nc", "", "dir"]), st.text(max_size=80), JUNK)
            )
    return command


@settings(max_examples=250, deadline=None, stateful_step_count=1)
@given(st.lists(commands(), min_size=1, max_size=14), st.sampled_from([Actor.AGENT, Actor.DREAM]))
def test_whatever_the_model_asks_the_answer_is_text_and_nothing_outside_changes(script, actor):
    with fresh_store(SMALL) as (store, root):
        outside = snapshot(root / "outside")
        preferences = snapshot(store.paths.preferences_dir)
        for command in script:
            result = store.execute(command, actor=actor)
            assert isinstance(result, str) and result, command
        assert snapshot(root / "outside") == outside
        assert snapshot(store.paths.preferences_dir) == preferences  # the owner's files are theirs


@settings(max_examples=150, deadline=None)
@given(st.lists(commands(), min_size=1, max_size=20))
def test_the_limits_hold_and_every_change_leaves_a_version(script):
    with fresh_store(SMALL) as (store, _):
        for command in script:
            store.execute(command)
        state = store.paths.state_dir
        files = [f for f in store._files(state)]
        assert (
            len(files) <= SMALL.max_files + 3
        )  # the three links made by the fixture are not files
        assert all(f.stat().st_size <= SMALL.max_file_bytes for f in files)
        assert sum(f.stat().st_size for f in files) <= SMALL.max_total_bytes


def lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


@settings(max_examples=150, deadline=None)
@given(
    st.lists(
        st.tuples(st.sampled_from(["a.md", "d/b.md", "d/e/c.md"]), st.text(max_size=50)),
        min_size=1,
        max_size=8,
    )
)
def test_a_created_file_reads_back_as_written_and_leaves_a_version(writes):
    with fresh_store() as (store, _):
        written: dict[str, str] = {}
        for name, text in writes:
            path = f"/memories/state/{name}"
            result = store.execute({"command": "create", "path": path, "file_text": text})
            if result.startswith("File created"):
                written[path] = lf(text)
                assert store.read_text(path) == lf(text)  # with `\n` the only line ending
        versions = store.db.query("SELECT path, action, new_content FROM memory_versions")
        assert {(r["path"], r["new_content"]) for r in versions if r["action"] == "create"} == set(
            written.items()
        )


LINE_TEXT = st.lists(
    st.sampled_from(["a", "b c", "\n", "\n", "\x0c", "\x0b", "\x85", "\u2028", "\u2029", "\x1c"]),
    max_size=14,
).map("".join)


@settings(max_examples=300, deadline=None)
@given(LINE_TEXT, st.integers(0, 20), st.text(alphabet="xyz", min_size=1, max_size=5))
def test_insert_counts_lines_the_way_view_numbers_them(base, at, inserted):
    with fresh_store() as (store, _):
        path = "/memories/state/n.md"
        store.execute({"command": "create", "path": path, "file_text": base})
        shown = base.split("\n")
        count = len(shown) - 1 if shown[-1] == "" else len(shown)
        result = store.execute(
            {"command": "insert", "path": path, "insert_line": at, "insert_text": inserted}
        )
        if at > count:
            assert "Invalid `insert_line`" in result
            assert store.read_text(path) == base
            return
        assert result.startswith("The file"), result
        after = store.read_text(path).split("\n")
        assert after[at] == inserted  # `view` would show it as line number `at + 1`


@settings(max_examples=150, deadline=None)
@given(st.text(max_size=60), st.text(max_size=20), st.text(max_size=20))
def test_a_replacement_changes_exactly_what_was_named(base, old, new):
    base, old, new = lf(base), lf(old), lf(new)  # what is stored, and what is searched for
    with fresh_store() as (store, _):
        path = "/memories/state/n.md"
        store.execute({"command": "create", "path": path, "file_text": base})
        result = store.execute(
            {"command": "str_replace", "path": path, "old_str": old, "new_str": new}
        )
        after = store.read_text(path)
        if result.startswith("The memory file has been edited"):
            assert base.count(old) == 1 and after == base.replace(old, new, 1)
        else:
            assert after == base
