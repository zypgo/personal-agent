"""Built-in tools: `memory` and `send_message` (more arrive with later iterations)."""

from __future__ import annotations

import hashlib
from typing import Literal

from agents import RunContextWrapper, function_tool

from personal_agent.harness.runctx import RunContext
from personal_agent.memory.store import Actor
from personal_agent.tools.base import (
    ALL_PROFILES,
    DREAM,
    FULL,
    SCHEDULED,
    Category,
    ToolDef,
    ToolRegistry,
)

MEMORY_DESCRIPTION = """\
Your persistent memory: a small file system under /memories that survives between conversations \
and restarts.
- /memories/state/ is yours. Read and write notes, task lists, ledgers and the index file MEMORY.md.
- /memories/preferences/ belongs to the owner and is read-only. To suggest a change to it, append \
the suggestion to /memories/state/proposals.md.
Commands:
- view: list a directory (path), or show a file with line numbers (path, optional view_range \
[start, end]; end = -1 means to the end of the file).
- create: make a new file (path, file_text). Fails if the file already exists.
- str_replace: replace one exact, unique piece of text in a file (path, old_str, new_str).
- insert: insert text after a line (path, insert_line, insert_text); insert_line 0 means the top.
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
urgency: "urgent" interrupts them now (rare); "normal"; "low" may wait and be batched."""

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


@function_tool(name_override="send_message", description_override=SEND_MESSAGE_DESCRIPTION)
async def send_message_tool(
    ctx: RunContextWrapper[RunContext],
    text: str,
    urgency: Literal["urgent", "normal", "low"] = "normal",
) -> str:
    rc = ctx.context
    body = text.strip()
    if not body:
        return "Error: the message is empty."
    if len(body) > MAX_MESSAGE_CHARS:
        return f"Error: the message is {len(body)} characters; the limit is {MAX_MESSAGE_CHARS}. Shorten it."
    digest = hashlib.sha256(body.encode()).hexdigest()[:16]
    message_id = rc.app.outbox.enqueue(
        session_key=rc.session_key,
        channel=rc.origin.channel,
        recipient=rc.origin.recipient,
        text=body,
        urgency=urgency,
        run_id=rc.run_id,
        dedupe_key=f"run:{rc.run_id}:{digest}",
    )
    if message_id is None:
        return "That exact message was already queued in this run; not sending it twice."
    rc.queued_messages.append(message_id)
    return (
        f"Queued as message {message_id}. It is delivered after this run ends; the system confirms "
        "delivery, so do not claim it was delivered."
    )


def register_builtin(registry: ToolRegistry) -> None:
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
