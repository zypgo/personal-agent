"""Fake provider used by tests: stdio only, never contacts an account or model."""

import json
import sys
from pathlib import Path

mode = sys.argv[1]
log = sys.argv[2]
prompt_id = None
approval_stage = 0
is_cursor = mode.startswith("cursor")


def send(value):
    if is_cursor:
        value["jsonrpc"] = "2.0"
    print(json.dumps(value), flush=True)


def finished(cancelled=False):
    if is_cursor:
        if not cancelled:
            send(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": "session_a",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "native result"},
                        },
                    },
                }
            )
        send({"id": prompt_id, "result": {"stopReason": "cancelled" if cancelled else "end_turn"}})
    else:
        if not cancelled:
            send(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_a",
                        "turnId": "turn_a",
                        "item": {
                            "type": "agentMessage",
                            "id": "message_a",
                            "text": "native result",
                        },
                    },
                }
            )
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread_a",
                    "turn": {
                        "id": "turn_a",
                        "status": "interrupted" if cancelled else "completed",
                        "items": [],
                    },
                },
            }
        )


for raw in sys.stdin:
    message = json.loads(raw)
    with open(log, "a", encoding="utf-8") as output:
        output.write(json.dumps(message) + "\n")
    method, identity = message.get("method"), message.get("id")
    if method == "initialize":
        if is_cursor:
            send(
                {
                    "id": identity,
                    "result": {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []},
                }
            )
        else:
            send({"id": identity, "result": {"userAgent": "fake-test"}})
    elif method == "initialized":
        pass
    elif method == "thread/start":
        send({"id": identity, "result": {"thread": {"id": "thread_a"}}})
    elif method == "session/new":
        send({"id": identity, "result": {"sessionId": "session_a"}})
    elif method in {"turn/start", "session/prompt"}:
        prompt_id = identity
        if mode == "codex_edit":
            Path("base.txt").write_text("changed in worktree\n")
            Path("new.txt").write_text("new bounded output\n")
        if not is_cursor:
            send(
                {
                    "id": identity,
                    "result": {"turn": {"id": "turn_a", "status": "inProgress", "items": []}},
                }
            )
        if mode.endswith("eof"):
            sys.exit(0)
        if mode.endswith("cancel"):
            continue
        if mode.endswith("permission"):
            if is_cursor:
                send(
                    {
                        "id": "approve_a",
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": "session_a",
                            "toolCall": {
                                "toolCallId": "tool_a",
                                "title": "write controlled file",
                                "kind": "edit",
                                "status": "pending",
                            },
                            "options": [
                                {
                                    "optionId": "allow-once",
                                    "kind": "allow_once",
                                    "name": "Allow once",
                                },
                                {
                                    "optionId": "allow-always",
                                    "kind": "allow_always",
                                    "name": "Always",
                                },
                                {
                                    "optionId": "reject-once",
                                    "kind": "reject_once",
                                    "name": "Reject",
                                },
                            ],
                        },
                    }
                )
            else:
                send(
                    {
                        "id": "approve_a",
                        "method": "item/commandExecution/requestApproval",
                        "params": {
                            "threadId": "thread_a",
                            "turnId": "turn_a",
                            "itemId": "tool_a",
                            "startedAtMs": 0,
                            "command": "echo safe-fixture",
                        },
                    }
                )
        elif mode.endswith("extensions"):
            send(
                {
                    "id": "ext_a",
                    "method": "cursor/ask_question",
                    "params": {
                        "toolCallId": "tool_a",
                        "questions": [
                            {
                                "id": "q",
                                "prompt": "Choose language",
                                "options": [{"id": "en", "label": "English"}],
                            }
                        ],
                    },
                }
            )
        else:
            finished()
    elif method in {"turn/interrupt", "session/cancel"}:
        if not is_cursor:
            send({"id": identity, "result": {}})
        finished(cancelled=True)
    elif identity == "approve_a":
        result = message.get("result", {})
        granted = (
            result.get("decision") == "accept"
            or result.get("outcome", {}).get("optionId") == "allow-once"
        )
        finished(cancelled=not granted)
    elif identity == "ext_a":
        send(
            {
                "id": "ext_b",
                "method": "cursor/create_plan",
                "params": {
                    "toolCallId": "tool_b",
                    "name": "Plan",
                    "plan": "Read the requested fixture",
                    "todos": [],
                },
            }
        )
    elif identity == "ext_b":
        finished(
            cancelled=message.get("result", {}).get("outcome", {}).get("outcome") != "accepted"
        )
