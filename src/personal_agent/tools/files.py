"""File tools: `list_files`, `read_file`, `write_file`, `edit_file`, confined to the workspace.

The workspace (`PA_HOME/workspace`) is the agent's scratch area: it is also the only place a
sandboxed shell may write, so what the shell produced can be read back here. Paths are resolved
(symlinks included) before they are checked, so a link that points outside is refused. Reading is
also allowed inside the skills directory, which holds instructions and scripts the owner installed.

These tools run in our own process, not in the sandbox. That is why they are strict: no `~`, no
paths outside the roots, no binary files, size limits, writes by replacing a file atomically (a
symlink swapped in at the last moment is replaced, not followed).
"""

from __future__ import annotations

import difflib
import itertools
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents import RunContextWrapper, function_tool

from personal_agent.harness.runctx import RunContext
from personal_agent.tools.base import FULL, SCHEDULED, Category, ToolDef, ToolRegistry

if TYPE_CHECKING:
    from personal_agent.app import App

LIST_MAX_ENTRIES = 300
READ_DEFAULT_LINES = 500
READ_SCAN_MAX_BYTES = 50_000_000
"""Files bigger than this are not read at all: paging through them would stall the agent."""
DIFF_MAX_LINES = 30

LIST_DESCRIPTION = """\
List files and directories in your workspace (or in an installed skill's folder).
path: relative to the workspace root; "." is the root. depth: 1 lists direct children, up to 4 \
goes deeper. Directories end with "/"."""

READ_DESCRIPTION = """\
Read a text file from your workspace (or an installed skill's folder, by absolute path).
Long files are paged: start_line (1-based) and num_lines choose the window, and the answer says \
how to continue. Binary files cannot be read."""

WRITE_DESCRIPTION = """\
Create or completely replace a file in your workspace. Parent folders are created. For a small \
change to an existing file, prefer edit_file. Only the workspace is writable."""

EDIT_DESCRIPTION = """\
Change an existing workspace file by replacing exact text: old_text must appear exactly once \
(copy it from read_file, including indentation) unless replace_all is true."""


class FileProblem(Exception):
    """A mistake the model can fix; the message says how."""


def _roots(app: App) -> tuple[Path, Path]:
    return app.paths.workspace_dir.resolve(), app.paths.skills_dir.resolve()


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def resolve_path(app: App, raw: str, *, write: bool) -> Path:
    """The real path a request points at, or FileProblem. Nothing outside the roots gets through."""
    text = (raw or "").strip()
    if not text:
        raise FileProblem("give a path inside the workspace.")
    if "\0" in text:
        raise FileProblem("the path contains a NUL character.")
    if text.startswith("~"):
        raise FileProblem("`~` is not available: paths are relative to the workspace root.")
    workspace, skills = _roots(app)
    path = Path(text)
    if not path.is_absolute():
        path = workspace / path
    try:
        real = path.resolve()
    except (OSError, RuntimeError) as exc:  # loops of symlinks, names too long
        raise FileProblem(f"cannot resolve {text!r}: {exc}") from exc
    allowed = [workspace] if write else [workspace, skills]
    if not any(_inside(real, root) for root in allowed):
        where = "the workspace" if write else "the workspace or the skills folder"
        raise FileProblem(f"{text!r} is outside {where} ({workspace}).")
    if write and real == workspace:
        raise FileProblem("that is the workspace folder itself; name a file inside it.")
    return real


def _show(app: App, path: Path) -> str:
    workspace, _ = _roots(app)
    try:
        return str(path.relative_to(workspace)) or "."
    except ValueError:
        return str(path)


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


# ---- list ------------------------------------------------------------------------------------------
def _list(app: App, path: Path, depth: int) -> str:
    if not path.exists():
        raise FileProblem(f"{_show(app, path)} does not exist.")
    if not path.is_dir():
        raise FileProblem(f"{_show(app, path)} is a file, not a folder.")
    lines: list[str] = []
    total = 0

    def walk(folder: Path, level: int) -> None:
        nonlocal total
        try:
            entries = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            lines.append(f"{'  ' * (level - 1)}[cannot read {folder.name}: {exc.strerror}]")
            return
        for entry in entries:
            total += 1
            indent = "  " * (level - 1)
            link = entry.is_symlink()
            is_dir = entry.is_dir() and not link
            if total <= LIST_MAX_ENTRIES:
                if is_dir:
                    lines.append(f"{indent}{entry.name}/")
                elif link:
                    lines.append(f"{indent}{entry.name}  (symbolic link)")
                else:
                    try:
                        size = _human(entry.stat().st_size)
                    except OSError:
                        size = "?"
                    lines.append(f"{indent}{entry.name}  ({size})")
            if is_dir and level < depth:
                walk(entry, level + 1)

    walk(path, 1)
    if not lines:
        return f"{_show(app, path)}/ is empty."
    note = (
        f"\n[{total - LIST_MAX_ENTRIES} more entries not shown]" if total > LIST_MAX_ENTRIES else ""
    )
    return f"{_show(app, path)}/\n" + "\n".join(lines) + note


# ---- read ------------------------------------------------------------------------------------------
def _read(app: App, path: Path, start_line: int, num_lines: int) -> str:
    if not path.exists():
        raise FileProblem(f"{_show(app, path)} does not exist.")
    if path.is_dir():
        raise FileProblem(f"{_show(app, path)} is a folder; use list_files.")
    size = path.stat().st_size
    if size > READ_SCAN_MAX_BYTES:
        raise FileProblem(f"{_show(app, path)} is {_human(size)}, too large to read here.")
    with path.open("rb") as handle:
        if b"\0" in handle.read(8192):
            raise FileProblem(f"{_show(app, path)} looks like a binary file ({_human(size)}).")
    start = max(1, start_line)
    count = max(1, min(num_lines, 5000))
    budget = max(200, app.config.runtime.tool_output_chars - 600)  # room for the footer
    out: list[str] = []
    used = 0
    total = 0
    last = start - 1
    cut_by_size = False
    with path.open("r", encoding="utf-8", errors="replace", newline=None) as handle:
        for number, line in enumerate(handle, start=1):
            total = number
            if number < start or number >= start + count or cut_by_size:
                continue
            if used + len(line) > budget:
                cut_by_size = True
                if not out:  # one enormous line (minified code): show its start, then move on
                    out.append(f"{line[:budget]}\n[line {number} is {len(line)} characters; cut]\n")
                    last = number
                continue
            out.append(line)
            used += len(line)
            last = number
    if total == 0:
        return f"{_show(app, path)} is empty."
    if start > total:
        raise FileProblem(f"{_show(app, path)} has only {total} lines.")
    text = "".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    footer = f"[lines {start}-{last} of {total}"
    footer += f"; continue with start_line={last + 1}]" if last < total else "]"
    return text + footer


# ---- write and edit --------------------------------------------------------------------------------
def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write(app: App, path: Path, content: str) -> str:
    data = content.encode("utf-8")
    limit = app.config.tools.file_max_bytes
    if len(data) > limit:
        raise FileProblem(f"{len(data)} bytes is over the {limit} byte limit; write it in parts.")
    if path.is_dir():
        raise FileProblem(f"{_show(app, path)} is a folder.")
    existed = path.exists()
    _write_atomic(path, data)
    verb = "Replaced" if existed else "Created"
    lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    return f"{verb} {_show(app, path)} ({len(data)} bytes, {lines} lines)."


def _edit(app: App, path: Path, old: str, new: str, replace_all: bool) -> str:
    if not path.is_file():
        raise FileProblem(f"{_show(app, path)} does not exist; use write_file to create it.")
    if not old:
        raise FileProblem("old_text is empty; to create or replace a whole file use write_file.")
    if old == new:
        raise FileProblem("old_text and new_text are identical; nothing to change.")
    try:
        with path.open(
            "r", encoding="utf-8", newline=""
        ) as handle:  # keep line endings as they are
            before = handle.read()
    except UnicodeDecodeError as exc:
        raise FileProblem(f"{_show(app, path)} is not valid UTF-8 text.") from exc
    if "\r\n" in before and "\r\n" not in old:  # read_file shows Windows line endings as \n
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    count = before.count(old)
    if count == 0:
        raise FileProblem(
            "old_text was not found. Read the file again and copy the text exactly, including "
            "indentation and line breaks."
        )
    if count > 1 and not replace_all:
        raise FileProblem(
            f"old_text appears {count} times. Add surrounding lines to make it unique, or set "
            "replace_all to true."
        )
    after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
    limit = app.config.tools.file_max_bytes
    if len(after.encode("utf-8")) > limit:
        raise FileProblem(f"the result would be over the {limit} byte limit.")
    _write_atomic(path, after.encode("utf-8"))
    diff = list(
        itertools.islice(
            difflib.unified_diff(
                before.splitlines(), after.splitlines(), "before", "after", n=1, lineterm=""
            ),
            DIFF_MAX_LINES + 1,
        )
    )
    shown = "\n".join(diff[:DIFF_MAX_LINES]) + (
        "\n[diff cut]" if len(diff) > DIFF_MAX_LINES else ""
    )
    changed = count if replace_all else 1
    return f"Edited {_show(app, path)}: replaced {changed} occurrence(s).\n{shown}"


# ---- tools -----------------------------------------------------------------------------------------
@function_tool(name_override="list_files", description_override=LIST_DESCRIPTION)
async def list_files_tool(
    ctx: RunContextWrapper[RunContext], path: str = ".", depth: int = 1
) -> str:
    app = ctx.context.app
    try:
        return _list(app, resolve_path(app, path, write=False), max(1, min(depth, 4)))
    except FileProblem as exc:
        return f"Error: {exc}"


@function_tool(name_override="read_file", description_override=READ_DESCRIPTION)
async def read_file_tool(
    ctx: RunContextWrapper[RunContext],
    path: str,
    start_line: int = 1,
    num_lines: int = READ_DEFAULT_LINES,
) -> str:
    app = ctx.context.app
    try:
        return _read(app, resolve_path(app, path, write=False), start_line, num_lines)
    except FileProblem as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: cannot read {path}: {exc.strerror or exc}"


@function_tool(name_override="write_file", description_override=WRITE_DESCRIPTION)
async def write_file_tool(ctx: RunContextWrapper[RunContext], path: str, content: str) -> str:
    app = ctx.context.app
    try:
        return _write(app, resolve_path(app, path, write=True), content)
    except FileProblem as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: cannot write {path}: {exc.strerror or exc}"


@function_tool(name_override="edit_file", description_override=EDIT_DESCRIPTION)
async def edit_file_tool(
    ctx: RunContextWrapper[RunContext],
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> str:
    app = ctx.context.app
    try:
        return _edit(app, resolve_path(app, path, write=True), old_text, new_text, replace_all)
    except FileProblem as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: cannot edit {path}: {exc.strerror or exc}"


def _path_check(write: bool):
    def check(rc: RunContext, args: dict[str, Any]) -> str | None:
        try:
            resolve_path(rc.app, str(args.get("path", ".")), write=write)
        except FileProblem as exc:
            return str(exc)
        return None

    return check


def _path_detail(args: dict[str, Any]) -> str:
    return str(args.get("path", ""))


def register_files(registry: ToolRegistry) -> None:
    # Not offered to untrusted runs (woken by content nobody vetted) or to memory maintenance.
    profiles = frozenset({FULL, SCHEDULED})
    for name, category, tool, write in (
        ("list_files", Category.READ, list_files_tool, False),
        ("read_file", Category.READ, read_file_tool, False),
        ("write_file", Category.WRITE, write_file_tool, True),
        ("edit_file", Category.WRITE, edit_file_tool, True),
    ):
        registry.register(
            ToolDef(
                name=name,
                category=category,
                tool=tool,
                profiles=profiles,
                detail=_path_detail,
                validate=_path_check(write),
            )
        )
