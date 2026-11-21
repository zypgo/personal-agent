"""The notes the product writes for you: what became of each task, a day's lines, the contents.

Pure text in, text out. They are rebuilt from the task records each time, so writing one twice
gives the same note, and a note lost or edited by mistake comes back right at the next change.
Only the part under 「笔记」 is left for you and the assistant to write in.

Nothing a remote agent wrote is copied in here. Its results are kept apart under `raw/` and these
notes only point to them, so what is read back from a task note is never someone else's words.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import TYPE_CHECKING

from personal_agent.vault import note as md
from personal_agent.vault.index import Entry

if TYPE_CHECKING:
    from personal_agent.vault.store import Vault

NOTES = "笔记"
TIMELINE = "经过"
TODO = "要做的"
RESULTS = "结果"
TODAY = "发生了什么"

PHASE_TITLES = {
    "needs_you": "等你",
    "running": "进行中",
    "done": "完成了",
    "closed": "没做成",
    "archived": "已收起",
}
_CLIP_PROMPT = 300
_CLIP_ACCEPT = 160
_LABEL_UNSAFE = re.compile(r"[\[\]|#^\n\r]+")


@dataclass(frozen=True)
class Step:
    agent: str
    prompt: str
    acceptance: str
    result: str = ""
    """Where the agent's result is kept (a path under `raw/`), once it has handed one in."""


@dataclass(frozen=True)
class Line:
    at: float
    text: str
    link: str = ""


@dataclass(frozen=True)
class Thread:
    task_id: str
    title: str
    phase: str
    label: str
    """What it is doing now, in two or three words: 等你验收, 进行中, 没做成."""
    created_at: float
    updated_at: float
    steps: tuple[Step, ...]
    timeline: tuple[Line, ...]

    @property
    def agents(self) -> list[str]:
        seen: list[str] = []
        for step in self.steps:
            if step.agent and step.agent not in seen:
                seen.append(step.agent)
        return seen


def label(text: str) -> str:
    """A title that is safe inside `[[path|title]]`."""
    return " ".join(_LABEL_UNSAFE.sub(" ", text).split())[:80] or "无题"


def link(path: str, title: str = "") -> str:
    target = path.removesuffix(".md")
    return f"[[{target}|{label(title)}]]" if title else f"[[{target}]]"


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def iso(at: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(at, tz).isoformat(timespec="seconds")


def _clock(at: float, tz: tzinfo, *, day: date | None = None) -> str:
    moment = datetime.fromtimestamp(at, tz)
    return (
        moment.strftime("%H:%M")
        if day is None or moment.date() == day
        else moment.strftime("%m-%d %H:%M")
    )


# ---- one note for each task --------------------------------------------------------------
def thread_path(thread: Thread, tz: tzinfo) -> str:
    day = datetime.fromtimestamp(thread.created_at, tz).strftime("%Y-%m-%d")
    short = re.sub(r"[^A-Za-z0-9]", "", thread.task_id)[-8:] or "task"
    return f"threads/{day}-{md.slugify(thread.title, limit=36)}-{short}.md"


def thread_front(thread: Thread, tz: tzinfo) -> dict[str, object]:
    return {
        "title": _clip(thread.title, 200) or "无题",
        "type": "thread",
        "origin": "system",
        "status": thread.phase,
        "summary": " · ".join([*thread.agents[:3], thread.label]),
        "task": thread.task_id,
        "agent": thread.agents,
        "tags": ["qixu/thread"],
        "created": iso(thread.created_at, tz),
        "updated": iso(thread.updated_at, tz),
    }


def thread_sections(thread: Thread, tz: tzinfo) -> dict[str, str]:
    """The parts of the note that follow the task. Each is written whole every time."""
    created = datetime.fromtimestamp(thread.created_at, tz).date()
    todo: list[str] = []
    for step in thread.steps:
        todo.append(
            f"- **{label(step.agent) if step.agent else '助手'}**：{_clip(step.prompt, _CLIP_PROMPT)}"
        )
        if step.acceptance.strip():
            todo.append(f"  - 验收标准：{_clip(step.acceptance, _CLIP_ACCEPT)}")
    lines = []
    for item in thread.timeline:
        text = f"- {_clock(item.at, tz, day=created)} {item.text}"
        lines.append(f"{text} {link(item.link)}" if item.link else text)
    results = [
        f"- {label(step.agent) if step.agent else '助手'}：{link(step.result)}"
        for step in thread.steps
        if step.result
    ]
    return {
        TODO: "\n".join(todo) or "（没有内容）",
        TIMELINE: "\n".join(lines) or "（还没有动静）",
        RESULTS: "\n".join(results) or "（还没有交回结果）",
    }


def thread_text(thread: Thread, tz: tzinfo) -> str:
    """A task's note when it is first made, with an empty 「笔记」 part at the end."""
    sections = thread_sections(thread, tz)
    body = (
        f"# {_clip(thread.title, 200) or '无题'}\n\n"
        f"> 这篇由栖序自动记录，会跟着任务更新。你的想法写在最下面的「{NOTES}」里，不会被覆盖。\n\n"
        + "\n\n".join(f"## {name}\n\n{text}" for name, text in sections.items())
        + f"\n\n## {NOTES}\n"
    )
    return md.render(thread_front(thread, tz), body)


# ---- a day ------------------------------------------------------------------------------
_WEEKDAYS = "一二三四五六日"


def daily_path(day: date) -> str:
    return f"daily/{day.isoformat()}.md"


def daily_text(day: date, tz: tzinfo, now: float) -> str:
    front = {
        "title": day.isoformat(),
        "type": "daily",
        "origin": "system",
        "tags": ["qixu/daily"],
        "created": iso(now, tz),
    }
    body = f"# {day.isoformat()} 星期{_WEEKDAYS[day.weekday()]}\n\n## {TODAY}\n\n## {NOTES}\n"
    return md.render(front, body)


def daily_line(
    at: float, tz: tzinfo, *, path: str, title: str, text: str, event: int
) -> tuple[str, str]:
    """One line for a day, and the hidden mark that says it was already written."""
    mark = f"ev:{event}"
    return f"- {_clock(at, tz)} {link(path, title)}：{text} <!-- {mark} -->", mark


# ---- the contents -----------------------------------------------------------------------
def _wiki_line(entry: Entry) -> str:
    text = f"- {link(entry.path, entry.title)}"
    return f"{text} — {_clip(entry.summary, 60)}" if entry.summary else text


def index_text(
    entries: Iterable[Entry],
    waiting: Sequence[Entry],
    now: float,
    tz: tzinfo,
    *,
    wiki_limit: int = 80,
) -> str:
    items = [e for e in entries if not e.generated and not e.private]
    threads = [e for e in items if e.path.startswith("threads/") and e.type == "thread"]
    wiki = sorted(
        (e for e in items if e.path.startswith("wiki/")), key=lambda e: (e.title.casefold(), e.path)
    )
    agents: dict[str, list[Entry]] = {}
    for e in items:
        parts = e.path.split("/")
        if len(parts) >= 4 and parts[0] == "raw" and parts[1] == "agents":
            agents.setdefault(parts[2], []).append(e)
    days = sorted((e for e in items if e.path.startswith("daily/")), key=lambda e: e.path)[-5:]

    out = ["# 目录", "", "> 栖序自动生成，会隔一阵更新一次。想补充内容，请写在别的笔记里。", ""]

    out.append("## 任务")
    shown = False
    for phase in ("needs_you", "running", "done", "closed"):
        group = sorted(
            (e for e in threads if e.status == phase), key=lambda e: e.mtime_ns, reverse=True
        )[:8]
        if not group:
            continue
        shown = True
        out += ["", f"### {PHASE_TITLES[phase]}"]
        out += [
            f"- {link(e.path, e.title)}" + (f" · {e.summary}" if e.summary else "") for e in group
        ]
    if not shown:
        out += ["", "（还没有交给助手的任务）"]

    out += ["", "## 助手交回的资料"]
    if agents:
        for name in sorted(agents):
            group = sorted(agents[name], key=lambda e: e.mtime_ns, reverse=True)
            out += ["", f"### {label(name)}（{len(group)}）"]
            out += [f"- {link(e.path, e.title)}" for e in group[:5]]
    else:
        out += ["", "（还没有）"]

    out += ["", f"## 还没整理的资料（{len(waiting)}）"]
    out += [f"- {link(e.path, e.title)}" for e in waiting[:10]] or ["（都整理好了）"]
    if len(waiting) > 10:
        out.append(f"- …还有 {len(waiting) - 10} 篇")

    out += ["", f"## 知识（{len(wiki)}）"]
    out += [_wiki_line(e) for e in wiki[:wiki_limit]] or ["（还没有）"]
    if len(wiki) > wiki_limit:
        out.append(f"- …还有 {len(wiki) - wiki_limit} 篇，用搜索找")

    if days:
        out += ["", "## 最近的日记"]
        out += [f"- {link(e.path, e.title)}" for e in reversed(days)]

    front = {
        "title": "目录",
        "type": "index",
        "origin": "system",
        "generated": True,
        "updated": iso(now, tz),
    }
    return md.render(front, "\n".join(out) + "\n")


def refresh_index(vault: Vault, now: float, tz: tzinfo) -> None:
    """Write the contents note again from what is in the folder now."""
    from personal_agent.vault.store import INDEX_NOTE

    vault.put(INDEX_NOTE, index_text(vault.entries().values(), vault.backlog(limit=50), now, tz))
