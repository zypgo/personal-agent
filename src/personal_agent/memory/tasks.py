"""The list of unfinished tasks that every run starts from.

Work that takes several wake-ups keeps a progress file in state/tasks/ (what is done, what is next,
where things are). Only this list is put in front of the model; it opens a file when it takes the
task up again. The list is built here, not by the model, so it is always as current as the files
and costs the same few lines however many tasks have ever been finished.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

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


@dataclass(frozen=True)
class TaskHead:
    name: str
    text: str
    modified_at: float


def task_index(notes: Iterable[TaskHead], limit: int, virtual_dir: str) -> str:
    """Render unfinished tasks from logical memory snapshots, newest first.

    Only each file's first 4096 characters participate in its title/status. `virtual_dir` is
    where the model finds omitted notes. The caller supplies current contents, including
    committed changes whose files have not yet been projected.
    """
    if limit <= 0:
        return ""
    found: list[tuple[float, str]] = []
    finished = 0
    for note in notes:
        head = note.text[:4096]
        status = _label(_STATUS_LINE.search(head), 100)
        if _FINISHED_RE.match(status):
            finished += 1
            continue
        title = _label(_TITLE_LINE.search(head), 80) or note.name.removesuffix(".md")
        line = f"- tasks/{note.name}: {title}" + (f" [status: {status}]" if status else "")
        found.append((note.modified_at, line))
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
