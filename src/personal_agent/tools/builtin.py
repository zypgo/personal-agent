"""Built-in tools: `memory`, `send_message`, `schedule`, `search_history`, the file tools, `shell` and the web tools."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from agents import RunContextWrapper, function_tool
from pydantic import BaseModel

from personal_agent.clock import format_local, load_zone
from personal_agent.config import Config
from personal_agent.core.events import Trust
from personal_agent.core.timers import Timer, TimerError
from personal_agent.harness.runctx import RunContext
from personal_agent.memory.store import Actor
from personal_agent.security.sanitize import clipped
from personal_agent.tools.base import (
    ALL_PROFILES,
    DREAM,
    FULL,
    SCHEDULED,
    UNTRUSTED,
    Category,
    ToolDef,
    ToolRegistry,
)

REVIEW_PROMPT_CHARS = 600
"""How much of a new wake-up's prompt the owner is shown to approve it."""

MEMORY_DESCRIPTION = """\
Your persistent memory: a small file system under /memories that survives between conversations \
and restarts.
- /memories/state/ is yours. Read and write notes, ledgers and the index file MEMORY.md. Work that \
takes several wake-ups gets a progress file, /memories/state/tasks/<name>.md, whose first lines are \
"# Title" and "status: in progress" (or waiting / blocked / done).
- /memories/preferences/ belongs to the owner and is read-only. To suggest a change to it, append \
the suggestion to /memories/state/proposals.md.
Commands:
- view: list a directory (path), or show a file with line numbers (path, optional view_range \
[start, end]; end = -1 means to the end of the file).
- create: make a new file (path, file_text). Fails if the file already exists.
- str_replace: replace one exact, unique piece of text in a file (path, old_str, new_str).
- insert: insert text after a line (path, insert_line, insert_text); insert_line 0 puts it above \
the first line, so to append pass the number of the last line.
- delete: delete a file or directory (path).
- rename: move or rename (old_path, new_path).
Only store facts you cannot look up elsewhere; for things that live in another system, store a \
pointer instead of a copy. Never store credentials. Keep MEMORY.md a short index of topics."""

SEND_MESSAGE_DESCRIPTION = """\
Send a message to the owner through their notification channel.
In a conversation your final answer is delivered automatically, so use this only for extra messages \
while you work, or when nobody is waiting for an answer (scheduled and background runs).
Delivery happens after this run ends and is confirmed by the system; never tell the owner that a \
message was delivered.
urgency: "urgent" interrupts them now (rare); "normal"; "low" may wait and be batched.
after_sent: memory edits (create, str_replace, insert, delete, rename; same fields as the memory \
tool) that are applied only once delivery is confirmed. Use them for bookmarks: if you record \
"reported up to item 40" here, a failed delivery cannot make you skip items 37-40 next time."""

SCHEDULE_DESCRIPTION = """\
Wake yourself up later: reminders, follow-ups ("check on this in 2 hours") and recurring jobs.
- action "create" with delay_minutes or at (ISO local time such as 2026-11-16T09:00): one wake-up.
- action "create" with cron (5 fields, e.g. "0 8 * * 1-5" = weekdays at 08:00) and optionally \
timezone: a recurring job. The owner may be asked to approve recurring jobs.
- action "list": your pending wake-ups. action "cancel": remove one by name.
When it fires, `prompt` is the only instruction you get, so write it for a colleague with no \
context: what to check, where, and what to do with the result (usually send_message). For a plain \
reminder, put in it what to tell the owner and any detail they will need at that moment.
session: "own" (default) is a fresh isolated conversation; "here" continues this conversation, \
for reminders that depend on what was just discussed.
Times are in the owner's timezone unless you pass timezone. Every run costs money, so keep \
recurring intervals sensible; wake-ups closer than a few minutes apart are refused."""

MAX_MESSAGE_CHARS = 8000


@function_tool(name_override="memory", description_override=MEMORY_DESCRIPTION)
async def memory_tool(
    ctx: RunContextWrapper[RunContext],
    command: Literal["view", "create", "str_replace", "insert", "delete", "rename"],
    path: str | None = None,
    view_range: list[int] | None = None,
    file_text: str | None = None,
    old_str: str | None = None,
    new_str: str | None = None,
    insert_line: int | None = None,
    insert_text: str | None = None,
    old_path: str | None = None,
    new_path: str | None = None,
) -> str:
    rc = ctx.context
    actor = Actor.DREAM if rc.profile == DREAM else Actor.AGENT
    return rc.app.memory.execute(
        {
            "command": command,
            "path": path,
            "view_range": view_range,
            "file_text": file_text,
            "old_str": old_str,
            "new_str": new_str,
            "insert_line": insert_line,
            "insert_text": insert_text,
            "old_path": old_path,
            "new_path": new_path,
        },
        actor=actor,
        run_id=rc.run_id,
    )


class MemoryEdit(BaseModel):
    """One memory-tool command to apply after a message has been delivered."""

    command: Literal["create", "str_replace", "insert", "delete", "rename"]
    path: str | None = None
    file_text: str | None = None
    old_str: str | None = None
    new_str: str | None = None
    insert_line: int | None = None
    insert_text: str | None = None
    old_path: str | None = None
    new_path: str | None = None


@function_tool(name_override="send_message", description_override=SEND_MESSAGE_DESCRIPTION)
async def send_message_tool(
    ctx: RunContextWrapper[RunContext],
    text: str,
    urgency: Literal["urgent", "normal", "low"] = "normal",
    after_sent: list[MemoryEdit] | None = None,
) -> str:
    rc = ctx.context
    body = text.strip()
    if not body:
        return "Error: the message is empty."
    if len(body) > MAX_MESSAGE_CHARS:
        return f"Error: the message is {len(body)} characters; the limit is {MAX_MESSAGE_CHARS}. Shorten it."
    edits = [e.model_dump(exclude_none=True) for e in after_sent or []]
    if edits and rc.profile == UNTRUSTED:
        # A run woken by outside content has no memory tool, so that nothing it was told can be
        # planted in notes the owner's own runs read back. This is the same door, not another.
        return (
            "Error: this run was started by content the owner did not write, so it cannot change "
            "memory, and after_sent is memory edits. Send the message without after_sent."
        )
    for edit in edits:
        problem = rc.app.memory.check_command(edit, actor=Actor.AGENT)
        if problem:
            return f"{problem} Nothing was queued; fix the edit and send again."
    digest = hashlib.sha256(body.encode()).hexdigest()[:16]
    message_id = rc.app.outbox.enqueue(
        session_key=rc.session_key,
        channel=rc.origin.channel,
        recipient=rc.origin.recipient,
        text=body,
        urgency=urgency,
        run_id=rc.run_id,
        dedupe_key=f"{rc.work_id}:{digest}",
        on_sent=edits or None,
        extra={"interactive": rc.origin.interactive, **rc.authority()},
    )
    if message_id is None:
        return "That exact message was already queued for this task; not sending it twice."
    rc.queued_messages.append(message_id)
    return (
        f"Queued as message {message_id}. It is delivered after this run ends; the system confirms "
        "delivery, so do not claim it was delivered."
    )


def _when(rc: RunContext, at: str, timezone: str | None = None) -> float:
    try:
        moment = datetime.fromisoformat(at.strip())
    except ValueError as exc:
        raise TimerError("`at` must be an ISO local time like 2026-11-16T09:00") from exc
    if moment.tzinfo is None:
        try:
            tz = load_zone(timezone) if timezone else rc.app.tz
        except ValueError as exc:
            raise TimerError(str(exc)) from exc
        moment = moment.replace(tzinfo=tz)
    return moment.timestamp()


def _describe(rc: RunContext, timer: Timer) -> str:
    when = format_local(timer.due_at, rc.app.tz)
    kind = f"recurring {timer.cron} ({timer.tz})" if timer.kind == "schedule" else "one-off"
    preview = timer.prompt if len(timer.prompt) <= 80 else timer.prompt[:77] + "..."
    return f"- {timer.name} [{kind}] next: {when}; session {timer.session_key}; {preview!r}"


@dataclass(frozen=True)
class _CreatePlan:
    name: str
    prompt: str
    cron: str | None
    timezone: str | None
    due_at: float | None
    session_key: str | None
    trust: Trust


def _plan_create(rc: RunContext, args: dict[str, Any]) -> _CreatePlan:
    """Check a `create` request and decide exactly what would be created (changes nothing).
    Raises TimerError with a message the model can act on."""
    name = (args.get("name") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    if not name:
        raise TimerError(
            "a name is required (lowercase letters, digits, dots, dashes, underscores)"
        )
    if not prompt:
        raise TimerError("a prompt is required: what you should do when it fires")
    delay, at, cron = args.get("delay_minutes"), args.get("at"), args.get("cron")
    if sum(x is not None for x in (delay, at, cron)) != 1:
        raise TimerError("give exactly one of delay_minutes, at, or cron")
    due_at = None
    if cron is None:
        due_at = (
            rc.app.clock.now() + delay * 60
            if delay is not None
            else _when(rc, at or "", args.get("timezone"))
        )
    # A run that has read outside content must not schedule work that carries the owner's authority.
    trust = Trust.UNTRUSTED if rc.tainted or rc.trust is Trust.UNTRUSTED else Trust.OWNER
    key = rc.session_key if args.get("session") == "here" else None
    plan = _CreatePlan(name, prompt, cron, args.get("timezone"), due_at, key, trust)
    timers = rc.app.timers
    problem = (
        timers.check_schedule(name, prompt, cron, plan.timezone)
        if cron is not None
        else timers.check_followup(name, prompt, due_at or 0.0)
    )
    if problem:
        raise TimerError(problem)
    return plan


def _validate_schedule(rc: RunContext, args: dict[str, Any]) -> str | None:
    if args.get("action") != "create":
        return None
    try:
        _plan_create(rc, args)
    except TimerError as exc:
        return str(exc)
    return None


@function_tool(name_override="schedule", description_override=SCHEDULE_DESCRIPTION)
async def schedule_tool(
    ctx: RunContextWrapper[RunContext],
    action: Literal["create", "list", "cancel"],
    name: str | None = None,
    prompt: str | None = None,
    delay_minutes: float | None = None,
    at: str | None = None,
    cron: str | None = None,
    timezone: str | None = None,
    session: Literal["own", "here"] = "own",
) -> str:
    rc = ctx.context
    timers = rc.app.timers
    try:
        if action == "list":
            live = timers.scheduled()
            if not live:
                return "No pending wake-ups."
            return f"{len(live)} pending wake-up(s):\n" + "\n".join(_describe(rc, t) for t in live)
        if action == "cancel":
            if not name:
                return "Error: say which wake-up to cancel (its name)."
            return (
                f"Cancelled {name}." if timers.cancel(name) else f"Error: no wake-up named {name}."
            )

        args = {
            "name": name, "prompt": prompt, "delay_minutes": delay_minutes, "at": at,
            "cron": cron, "timezone": timezone, "session": session,
        }  # fmt: skip
        plan = _plan_create(rc, args)
        rc.require_authorized()
        # A timer without its initiating identity would later default to the space owner.
        # Commit the scheduled work and its authority as one indivisible operation.
        with rc.app.db.tx():
            if plan.cron is not None:
                timer = timers.add_schedule(
                    name=plan.name, prompt=plan.prompt, cron=plan.cron, tz=plan.timezone,
                    session_key=plan.session_key, trust=plan.trust, created_by="agent",
                )  # fmt: skip
            else:
                timer = timers.add_followup(
                    name=plan.name, prompt=plan.prompt, due_at=plan.due_at or 0.0,
                    session_key=plan.session_key, trust=plan.trust, created_by="agent",
                )  # fmt: skip
            rc.app.db.kv_set(
                f"timer_origin:{timer.id}",
                json.dumps({**rc.authority(), "origin": rc.origin.to_json()}),
                now=rc.app.clock.now(),
            )
    except TimerError as exc:
        return f"Error: {exc}"
    note = (
        " It will run without owner authority because this conversation read outside content."
        if timer.trust is Trust.UNTRUSTED
        else ""
    )
    return f"Scheduled. {_describe(rc, timer)}{note}"


def _detail_schedule(args: dict[str, Any]) -> str:
    return " ".join(
        str(args[k])
        for k in ("action", "name", "cron", "at", "delay_minutes")
        if args.get(k) is not None
    )


def _review_schedule(args: dict[str, Any]) -> str:
    """What the owner reads before a wake-up is created: when, and above all what it will be told
    to do. The prompt is the whole of a recurring job's authority, so it is shown, not left out."""
    when = _detail_schedule(args)
    prompt = str(args.get("prompt") or "").strip()
    if args.get("action") != "create" or not prompt:
        return when
    where = " | in this conversation" if args.get("session") == "here" else ""
    return (
        f"{when}{where} | prompt ({len(prompt)} characters): {clipped(prompt, REVIEW_PROMPT_CHARS)}"
    )


def _schedule_category(_rc: RunContext, args: dict[str, Any]) -> Category:
    """Listing and cancelling are harmless, a one-off reminder is light, a recurring job is a standing
    authorization and is treated like any other action that changes the world."""
    if args.get("action") in ("list", "cancel"):
        return Category.READ
    return Category.SCHEDULE if args.get("cron") else Category.REMIND


def register_builtin(registry: ToolRegistry, config: Config | None = None) -> None:
    from personal_agent.tools.agents import register_agents
    from personal_agent.tools.jobs import register_jobs
    from personal_agent.tools.vault import register_vault
    from personal_agent.tools.workspace import register_workspace

    register_agents(registry)
    register_vault(registry)
    register_jobs(registry, config)
    register_workspace(registry)
    """The tools every agent has. `config.tools` switches the optional families on and off."""
    from personal_agent.tools.artifacts import register_artifacts
    from personal_agent.tools.files import register_files
    from personal_agent.tools.history import register_history
    from personal_agent.tools.shell import register_shell
    from personal_agent.tools.skills import register_skills
    from personal_agent.tools.web import register_web

    switches = (config or Config()).tools
    registry.register(
        ToolDef(
            name="memory",
            category=Category.MEMORY,
            tool=memory_tool,
            # Untrusted runs get no memory access: they must not be able to read private notes or
            # plant instructions that later, trusted runs would read back.
            profiles=frozenset({FULL, SCHEDULED, DREAM}),
        )
    )
    registry.register(
        ToolDef(
            name="send_message",
            category=Category.NOTIFY,
            tool=send_message_tool,
            # Every profile except dream may alert the owner, including untrusted ones.
            profiles=frozenset(ALL_PROFILES - {DREAM}),
        )
    )
    registry.register(
        ToolDef(
            name="schedule",
            category=Category.SCHEDULE,  # strictest; `category_for` lowers it for harmless calls
            tool=schedule_tool,
            profiles=frozenset({FULL, SCHEDULED}),
            category_for=_schedule_category,
            validate=_validate_schedule,
            detail=_detail_schedule,
            review=_review_schedule,
        )
    )
    register_history(registry)
    register_skills(registry)  # offered only when skills are installed
    if switches.files:
        register_files(registry)
        register_artifacts(registry)
    if switches.shell:
        register_shell(registry)
    if switches.web_fetch or switches.http_request:
        register_web(registry, config or Config())
