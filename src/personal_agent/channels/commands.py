"""`/commands` typed into a chat app or the web page.

They are handled here, in the channel layer, and never reach the model: an owner who types
`/approve 3` must get exactly that, not the model's reading of it. The same commands exist in the
terminal chat (`pa chat`), which prints them with colours; these return plain text that any
channel can show.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from personal_agent.clock import format_local
from personal_agent.core.approvals import (
    NOTHING_WAITING,
    choose_approvals,
    elsewhere,
    listing,
    split_targets,
)

if TYPE_CHECKING:
    from personal_agent.app import App

HELP = """\
/new              start a fresh conversation (the old one stays in the log)
/status           what is queued, what is running, what it cost today
/approvals        actions waiting for your yes or no
/approve [N|all]  allow a waiting action
/deny [N|all]     refuse it; add a few words after the number to tell me why
/help             this list"""

_COMMAND = re.compile(r"^/([A-Za-z]+)(?:@\w+)?(?:\s+(.*))?$", re.S)


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """("approve", ["3", "not", "now"]) for "/approve 3 not now"; None if it is not a command.

    Telegram appends the bot's name in groups ("/status@my_bot"); that is ignored."""
    match = _COMMAND.match(text.strip())
    if match is None:
        return None
    return match.group(1).lower(), (match.group(2) or "").split()


def _span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


class ChatCommands:
    def __init__(self, app: App, *, wake: Callable[[], None] = lambda: None) -> None:
        self.app = app
        self.wake = wake

    def known(self, name: str) -> bool:
        return name in {"help", "start", "new", "status", "approvals", "approve", "deny"}

    def run(self, name: str, args: list[str], *, channel: str, session_key: str) -> str:
        if name in ("help", "start"):
            return HELP
        if name == "new":
            self.app.sessions.clear(session_key)
            return "Started a fresh conversation. The old one stays in the log."
        if name == "status":
            return self.status()
        if name == "approvals":
            return self.approvals()
        if name in ("approve", "deny"):
            return self.decide(name == "approve", args, channel, session_key)
        return f"Unknown command /{name}. Try /help."

    # ---- the commands ---------------------------------------------------------------
    def approvals(self) -> str:
        waiting = self.app.approvals.pending()
        if not waiting:
            return NOTHING_WAITING
        now = self.app.clock.now()
        expiry = ", ".join(f"#{a.id} in {_span(a.expires_at - now)}" for a in waiting)
        return "\n".join(
            [
                "Waiting for your answer:",
                "",
                listing(waiting),
                "",
                f"They expire: {expiry}.",
                "/approve N or /deny N (or `all`).",
            ]
        )

    def decide(self, approve: bool, args: list[str], channel: str, session_key: str) -> str:
        app = self.app
        targets, note = split_targets(args)
        ids, problem = choose_approvals(app.approvals.pending(), targets, session_key=session_key)
        if ids is None:
            return problem or NOTHING_WAITING
        lines: list[str] = []
        resumed = False
        for approval_id in ids:
            result = app.approvals.decide(approval_id, approve, by=f"owner:{channel}", note=note)
            lines.append(result.message)
            resumed = resumed or result.resume_queued
        if resumed:
            self.wake()  # the paused work continues now
        if "all" in targets and (left := app.approvals.pending()):
            lines.append(f"Still waiting elsewhere: {elsewhere(left)}. Name one to answer it.")
        return "\n".join(lines)

    def status(self) -> str:
        app = self.app
        cfg = app.config
        now = app.clock.now()
        lines = [f"{cfg.agent.name} · {cfg.model.main} · {format_local(now, app.tz)}"]
        lines.append("Working on something now." if app.live.busy else "Idle.")
        counts = {k: v for k, v in app.inbox.counts().items() if v and k != "done"}
        if counts:
            lines.append("Inbox: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        waiting = app.approvals.pending()
        if waiting:
            lines.append(f"{len(waiting)} action(s) waiting for your answer (/approvals).")
        timers = app.timers.scheduled()
        if timers:
            upcoming = app.timers.next_due()
            soon = f", next in {_span(upcoming - now)}" if upcoming is not None else ""
            lines.append(f"{len(timers)} timer(s){soon}.")
        budget = app.budget.status()
        lines.append(f"Today: ${budget.spent_today:.4f} of ${budget.daily_limit:.2f}.")
        if budget.remaining_today <= 0:
            lines.append(f"Paused for the budget until midnight (in {_span(budget.resets_in_s)}).")
        unsent = app.outbox.counts()
        if unsent.get("maybe_sent") or unsent.get("failed"):
            lines.append("Some messages may not have reached you: run `pa outbox` on the computer.")
        return "\n".join(lines)
