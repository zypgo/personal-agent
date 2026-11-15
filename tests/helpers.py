"""Small builders shared by the test modules."""

from __future__ import annotations

from typing import Any

from agents import Usage
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails
from typer.testing import CliRunner

from personal_agent.core.events import Kind, NewEvent, Trust


def usage(
    inp: int = 1000, out: int = 100, *, cached: int = 0, cache_write: int = 0, reasoning: int = 0
) -> Usage:
    return Usage(
        requests=1,
        input_tokens=inp,
        output_tokens=out,
        total_tokens=inp + out,
        input_tokens_details=InputTokensDetails(
            cached_tokens=cached, cache_write_tokens=cache_write
        ),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=reasoning),
    )


def owner_message(
    text: str, *, session: str = "main", source: str = "cli", **payload: Any
) -> NewEvent:
    return NewEvent(
        source=source, kind=Kind.MESSAGE, session_key=session, payload={"text": text, **payload}
    )


def timer(
    name: str, prompt: str, *, session: str | None = None, trust: Trust = Trust.OWNER
) -> NewEvent:
    return NewEvent(
        source="timer",
        kind=Kind.TIMER,
        session_key=session or f"timer:{name}",
        payload={"name": name, "prompt": prompt},
        trust=trust,
    )


def webhook(name: str, body: Any, *, session: str | None = None) -> NewEvent:
    return NewEvent(
        source=f"webhook:{name}",
        kind=Kind.WEBHOOK,
        session_key=session or f"webhook:{name}",
        payload={"name": name, "body": body},
        trust=Trust.UNTRUSTED,
    )


def roles(items: list[dict[str, Any]]) -> list[str]:
    """Compact description of a list of session items, for readable assertions."""
    return [str(i.get("role") or i.get("type")) for i in items]


def text_of(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def invoke(*args: str, input: str | None = None):
    """Run a `pa` command in-process."""
    from personal_agent import cli

    return CliRunner().invoke(cli.app, list(args), input=input)
