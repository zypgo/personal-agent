"""The list of unfinished tasks that every run starts from.

Work that takes several wake-ups keeps a progress file in state/tasks/ (what is done, what is next,
where things are). Only this list is put in front of the model; it opens a file when it takes the
task up again. The list is built here, not by the model, so it is always as current as the files
and costs the same few lines however many tasks have ever been finished.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from personal_agent.security.sanitize import strip_invisible

FINISHED = (
    "done",
    "finished",
    "complete",
    "completed",
    "closed",
    "cancelled",
    "canceled",
    "abandoned",
)
"""A task whose `status:` line starts with one of these words is over; it is not listed any more."""
_FINISHED_RE = re.compile(r"(?i)(?:" + "|".join(FINISHED) + r")\b")
_STATUS_LINE = re.compile(r"(?im)^\s*(?:[-*>]\s*)?\**status\**\s*:\s*\**\s*(.+?)\s*$")
_TITLE_LINE = re.compile(r"(?m)^\s*#+\s*(.+?)\s*$")


def task_index(directory: Path, limit: int, virtual_dir: str) -> str:
    """One line for each unfinished task file in `directory`, newest first, at most `limit` of them.
    `virtual_dir` is where the model finds that folder, for the lines that say how many are left out."""
    if limit <= 0 or directory.is_symlink() or not directory.is_dir():
        return ""
    found: list[tuple[float, str]] = []
    finished = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return ""
    for entry in entries:
        if (
            entry.name.startswith(".")
            or not entry.name.endswith(".md")
            or entry.is_symlink()
            or not entry.is_file()
        ):
            continue
        try:
            with open(entry.path, encoding="utf-8", errors="replace") as handle:
                head = handle.read(4096)
            stamp = entry.stat().st_mtime
        except OSError:
            continue
        status = _label(_STATUS_LINE.search(head), 100)
        if _FINISHED_RE.match(status):
            finished += 1
            continue
        title = _label(_TITLE_LINE.search(head), 80) or entry.name.removesuffix(".md")
        line = f"- tasks/{entry.name}: {title}" + (f" [status: {status}]" if status else "")
        found.append((stamp, line))
    if not found:
        return ""
    found.sort(key=lambda item: (-item[0], item[1]))
    lines = [line for _, line in found[:limit]]
    if len(found) > limit:
        lines.append(f"[{len(found) - limit} more unfinished tasks are in {virtual_dir}/]")
    if finished:
        lines.append(f"[{finished} finished tasks are in {virtual_dir}/ and not listed]")
    return "\n".join(lines)


def _label(match: re.Match[str] | None, limit: int) -> str:
    """One short line of plain text out of a file the model wrote (and so is only data)."""
    if match is None:
        return ""
    text = " ".join(strip_invisible(match.group(1)).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
