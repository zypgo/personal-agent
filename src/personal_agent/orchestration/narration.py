"""What the owner is told about a task handed to other agents, and when to nudge or put it away.

Plain functions, no database and no model: the sentences are fixed, so a task update costs nothing
and always reads the same. The assistant speaks only when something is the owner's to do (a
question, a result to look at, a failure); progress in between stays quiet.

Wording rule: say who, what happened, and what is wanted from the owner, in that order and in as
few words as possible. Never an id, a state name or a protocol word.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from personal_agent.config import ThreadsConfig
from personal_agent.orchestration.models import TASK_TERMINAL

NEED_WORDS = {"approve": "批准", "answer": "回答", "accept": "验收", "check": "确认"}
STATE_WORDS = {
    "planned": "等你批准",
    "queued": "等助手接手",
    "running": "进行中",
    "waiting_input": "进行中",
    "cancelling": "正在取消",
    "awaiting_acceptance": "等你验收",
    "uncertain": "等你确认",
    "succeeded": "完成了",
    "failed": "没做成",
    "cancelled": "已取消",
    "expired": "超时了",
    "revoked": "已失效",
}


def need_of(state: str, open_questions: int) -> str | None:
    """What the owner has to do for this task to move on, if anything."""
    if state in TASK_TERMINAL:
        return None
    if state == "planned":
        return "approve"
    if open_questions:
        return "answer"
    if state == "awaiting_acceptance":
        return "accept"
    if state == "uncertain":
        return "check"
    return None


def phase_of(state: str, need: str | None, *, archived: bool = False) -> str:
    if archived:
        return "archived"
    if state == "succeeded":
        return "done"
    if state in TASK_TERMINAL:
        return "closed"
    return "needs_you" if need else "running"


def label_of(state: str, need: str | None) -> str:
    if need:
        return f"等你{NEED_WORDS[need]}"
    return STATE_WORDS.get(state, state)


def span(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    if minutes < 90:
        return f"{minutes} 分钟"
    hours = round(minutes / 60)
    if hours < 36:
        return f"{hours} 小时"
    return f"{round(hours / 24)} 天"


def one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def named(agent: str, *, after_chinese: bool = False) -> str:
    """The agent's name for a Chinese sentence. A Latin name is set off from the Chinese next to
    it by a space: after it always, before it only when the sentence has Chinese just before."""
    name = agent or "助手"
    before = " " if after_chinese and name[0].isascii() and name[0].isalnum() else ""
    after = " " if name[-1].isascii() and name[-1].isalnum() else ""
    return f"{before}{name}{after}"


def _quoted(title: str) -> str:
    return f"「{one_line(title, 40) or '这个任务'}」"


def say(
    kind: str,
    *,
    title: str,
    agent: str = "",
    detail: str = "",
    options: Sequence[str] = (),
    web: bool = True,
) -> str:
    """The message for one thing that happened. `detail` is what the agent wrote (a question, the
    start of a result, a reason), already shortened and cleaned by the caller."""
    who, task = named(agent), _quoted(title)
    where = "在对话里的卡片上" if web else "到网页的「任务」页里"
    if kind == "asked":
        choices = f"\n可以选：{' / '.join(options)}" if options else ""
        return f"{who}有个问题要你拿主意：{detail}{choices}\n直接回我就行，我转给它。"
    if kind == "reported":
        head = f"{who}交回了{task}的结果。"
        peek = f"\n开头是：{detail}" if detail else ""
        return f"{head}{peek}\n看一眼，{where}点「验收通过」就算完成，不满意点「未通过」。"
    if kind == "reported_failed":
        why = f"\n它说：{detail}" if detail else ""
        return f"{who}说{task}没做成。{why}\n要我换个办法再安排一次吗？"
    if kind == "uncertain":
        return (
            f"{who}那边断了联系，不知道{task}有没有做完。我不会自动重发，免得它做两遍。\n"
            "等你确认它已经停了（任务页里有按钮）。"
        )
    if kind == "failed":
        return f"{task}没做成。" + (f"{detail}" if detail else "")
    if kind == "expired":
        return f"{task}超过时限了，我已经把它收起来。要接着做的话告诉我，我重新安排。"
    if kind == "revoked":
        return f"{task}的授权变了，这个任务作废了。"
    raise ValueError(kind)


def remind(need: str, *, title: str, agent: str, again: bool) -> str:
    who, task = named(agent), _quoted(title)
    text = {
        "approve": f"{task}的计划还在等你确认，确认之后才会派出去。",
        "answer": f"{who}还在等你回答{task}里的问题。",
        "accept": f"{task}的结果交回来了，还没验收。",
        "check": f"{task}还需要你确认{named(agent, after_chinese=True)}是不是已经停了。",
    }[need]
    return f"再提醒一下：{text}" if again else text


BUNDLE_AT = 3
"""From this many nudges at once (the first look after an upgrade, a restart or a night's sleep),
they are said in one message instead of one after another."""
_BUNDLE_LINES = 6

_NEED_LINE = {
    "approve": "的计划等你确认",
    "answer": "有问题等你回答",
    "accept": "的结果等你验收",
    "check": "要你确认它停了没有",
}


def bundle(nudges: Sequence[Nudge]) -> str:
    """Several nudges as one message: how many, one line each, and where to go."""
    shown = nudges[:_BUNDLE_LINES]
    lines = "\n".join(f"· {n.line}" for n in shown)
    more = f"\n……还有 {len(nudges) - len(shown)} 件。" if len(nudges) > len(shown) else ""
    return f"有 {len(nudges)} 件事要你看一眼：\n{lines}{more}\n去「任务」页处理就行。"


def stalled(*, state: str, title: str, agent: str, idle: float) -> str:
    who, task = named(agent), _quoted(title)
    if state == "queued":
        return (
            f"{task}派出去 {span(idle)} 了，{who}还没接手，可能没在线。"
            "我不会重发；想放弃就告诉我，我来取消。"
        )
    return (
        f"{task}已经 {span(idle)} 没有新动静了，{who}可能卡住了。"
        "我先不重发，免得它做两遍；想放弃就告诉我，我来取消。"
    )


@dataclass(frozen=True)
class Facts:
    task_id: str
    title: str
    state: str
    need: str | None
    agent: str
    since: float
    """When the owner's turn began (the plan was made, the question asked, the result came in)."""
    activity: float
    """The last sign that something is happening on it."""
    ended_at: float
    archived: bool
    snoozed_until: float


@dataclass(frozen=True)
class Nudge:
    task_id: str
    key: str
    text: str
    line: str = ""
    """The same thing in a few words, for when several are said together."""


def plan_patrol(
    facts: Sequence[Facts], cfg: ThreadsConfig, now: float, *, baseline: float = 0.0
) -> tuple[list[Nudge], list[str]]:
    """What the housekeeping should say and put away now. Calling it again with the same facts
    gives the same keys, and a message with a key that was already queued is never queued twice,
    so being asked every half minute costs nothing.

    `baseline` is when the service started or last woke up. Time before it is not counted: a task
    did not go quiet because the machine was off, so it is mentioned only after a whole stretch
    of the service being up. The keys do not move with it, so nothing is said twice."""
    nudges: list[Nudge] = []
    put_away: list[str] = []
    for f in facts:
        if f.archived:
            continue
        if f.state in TASK_TERMINAL:
            if cfg.archive_after_hours and now - f.ended_at >= cfg.archive_after_hours * 3600:
                put_away.append(f.task_id)
            continue
        if f.snoozed_until > now:
            continue
        if f.need:
            waited = now - max(f.since, baseline)
            base = f"thread:{f.task_id}:remind"
            line = f"{_quoted(f.title)}{_NEED_LINE[f.need]}"
            # Twice at most. After a long silence (the computer was off) only the later one goes.
            if waited >= cfg.remind_again_hours * 3600:
                text = remind(f.need, title=f.title, agent=f.agent, again=True)
                nudges.append(Nudge(f.task_id, f"{base}:2:{int(f.since)}", text, line))
            elif waited >= cfg.remind_minutes * 60:
                text = remind(f.need, title=f.title, agent=f.agent, again=False)
                nudges.append(Nudge(f.task_id, f"{base}:1:{int(f.since)}", text, line))
        else:
            idle = now - max(f.activity, baseline)
            if idle >= cfg.stall_minutes * 60:
                text = stalled(state=f.state, title=f.title, agent=f.agent, idle=idle)
                line = (
                    f"{_quoted(f.title)}还没人接手"
                    if f.state == "queued"
                    else f"{_quoted(f.title)}已经 {span(idle)} 没有新动静"
                )
                key = f"thread:{f.task_id}:stall:{int(f.activity)}"
                nudges.append(Nudge(f.task_id, key, text, line))
    return nudges, put_away
