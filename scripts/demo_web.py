"""See the web page without an API key: the real server and the real page, a scripted model.

    uv run python scripts/demo_web.py --home /tmp/pa-demo     # then open the link it prints
    uv run python scripts/demo_web.py --home /tmp/pa-demo --ask "git push origin main"

The second form (run while the first is serving) puts a new action in front of the owner, which is
how to try the Allow / Decline card. The demo's home is a temporary folder unless `--home` is given,
and it never touches your agent's data: its "model" only replays canned answers, whatever you ask.
To try the page on a phone, use `--host 0.0.0.0` and a name from `--allow-host`.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import signal
import tempfile
import time
from pathlib import Path
from typing import Any

from agents.testing import ScriptedModel, assistant_message, function_call

from personal_agent.app import App, build_app
from personal_agent.channels.web.channel import WebChannel
from personal_agent.config import AgentConfig, Config, RuntimeConfig, WebConfig
from personal_agent.core.approvals import PendingCall
from personal_agent.daemon import Daemon
from personal_agent.security.sandbox import SandboxProvider

ANSWERS = [
    """Good morning. Here is how the day looks.

## Today
- **09:30** stand-up with the platform team
- **13:00** lunch with Mira (she asked to move it to *13:30*)
- **16:00** dentist, `Rua Augusta 112`

> Nothing urgent in the inbox, but one thing needs a decision: the venue wants a deposit by Friday.

Want me to put the deposit on the calendar as a reminder?""",
    """Here is the script, and what it prints.

```python
from pathlib import Path

def newest(folder: str, pattern: str = "*.md") -> Path | None:
    files = sorted(Path(folder).expanduser().glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None

print(newest("~/notes"))
```

It looks for the most recently changed `.md` file and returns `None` when there is none.""",
    """Three options for the trip, compared:

| Option | Time | Cost | Notes |
|:--|--:|--:|:--|
| Train | 3h 10m | €48 | direct, quiet carriage |
| Flight | 1h 05m | €96 | plus 2h at airports |
| Car | 3h 40m | €31 | fuel and tolls, you drive |

The train is the best fit if you want to work on the way. Details are in [the timetable](https://www.example.com/timetable).""",
    """Done. I wrote the summary to `/notes/2026-11-15.md`:

1. Reviewed the three open pull requests
2. Replied to Mira about lunch
3. Booked the 16:00 dentist slot

- [x] summary written
- [ ] send it to the team (waiting for you)

Let me know if you want it shortened.""",
    "Understood. I will remind you tomorrow at 9:00.\n\nAnything else?",
]

EARLIER = [
    (-26 * 3600, "in", "telegram", "remind me to renew the passport"),
    (-26 * 3600 + 20, "out", "telegram", "Noted. I will remind you on **Monday morning**."),
    (-5 * 3600, "in", "web", "what did I decide about the venue?"),
    (
        -5 * 3600 + 15,
        "out",
        "web",
        "You said the **Lisbon** option, if the deposit stays under €500. The venue asked for €450.",
    ),
]


class Thoughtful(ScriptedModel):
    """The scripted model, but it takes a moment, so the page has a "working" state to show."""

    think_s = 2.0

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(self.think_s)
        return await super().get_response(*args, **kwargs)


class Voice:
    """Stands in for transcription: whatever was said, it heard this."""

    async def transcribe(self, data: bytes, *, mime: str, name: str = "") -> str:
        return "this is what the recording said"


def scripted(think_s: float, first: int = 0) -> Thoughtful:
    """Each answer is preceded by a look at the agent's memory (so a tool shows in the status)."""
    steps: list[Any] = []
    answers = ANSWERS[first % len(ANSWERS) :] + ANSWERS[: first % len(ANSWERS)]
    for index, text in enumerate(itertools.islice(itertools.cycle(answers), 200)):
        looking = function_call(
            "memory", {"command": "view", "path": "/memories"}, call_id=f"look{index}"
        )
        steps += [[looking], [assistant_message(text)]]
    model = Thoughtful(steps)
    model.think_s = think_s
    return model


def open_app(home: Path, args: argparse.Namespace) -> App:
    config = Config(
        agent=AgentConfig(name="Ada", owner_name="Frank"),
        web=WebConfig(
            enabled=True, host=args.host, port=args.port, allowed_hosts=list(args.allow_host)
        ),
        runtime=RuntimeConfig(poll_s=0.1, concurrency=2),
    )
    model = scripted(args.think, args.first)  # one script: each question gets the next answer
    return build_app(
        home=home,
        config=config,
        model_factory=lambda name: model,
        sandbox=SandboxProvider.fixed(None, "the demo runs no commands"),
        read_env=False,
        transcriber=Voice(),
    )


def seed(app: App, bulk: int = 0) -> None:
    """A little history, including an older day, and one action waiting for a yes (once per home).
    `bulk` adds that many plain older messages, to try scrolling back through a long history."""
    if app.db.scalar("SELECT COUNT(*) FROM transcript"):
        return
    now = app.clock.now()
    older = [
        (-30 * 3600 - (bulk - i) * 1800, "in" if i % 2 == 0 else "out", "web", f"Older note {i}")
        for i in range(bulk)
    ]
    for offset, direction, channel, text in [*older, *EARLIER]:
        app.db.execute(
            "INSERT INTO transcript(at, direction, channel, kind, text, outbox_id, meta)"
            " VALUES (?, ?, ?, 'message', ?, NULL, '{}')",
            (now + offset, direction, channel, text),
        )
    ask(app, "git -C ~/notes push origin main", "I would like to push your notes. May I?")


def ask(app: App, command: str, text: str = "May I run this?") -> None:
    """Put an action in front of the owner, the way a paused run does."""
    [approval] = app.approvals.create(
        f"demo-{time.time_ns()}", "main", [PendingCall("c1", "shell", command)]
    )
    app.outbox.enqueue(
        session_key="main",
        channel="web",
        text=text,
        kind="approval_request",
        extra={"approval_ids": [approval.id]},
    )


async def serve(app: App, bulk: int = 0) -> None:
    seed(app, bulk)
    web = WebChannel(app)
    daemon = Daemon(app, {"web": web}, services=[web])
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, daemon.stop)
    running = asyncio.create_task(daemon.run())
    await daemon.wait_ready()
    while web.listener is None or not web.listener.started:  # noqa: ASYNC110 - nothing to await
        await asyncio.sleep(0.05)
    invite = app.web_auth.invite("demo browser")
    print(f"Open this in a browser (it works once, for {app.config.web.invite_minutes} minutes):")
    print(f"\n    {app.config.web.address.replace('127.0.0.1', 'localhost')}/#pair={invite.code}\n")
    print("Ctrl-C to stop.")
    await running


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--home", default=None)
    parser.add_argument("--allow-host", action="append", default=[], metavar="NAME")
    parser.add_argument("--think", type=float, default=2.0, help="seconds the model 'thinks'")
    parser.add_argument("--ask", metavar="COMMAND", help="ask the owner to allow this command")
    parser.add_argument("--bulk", type=int, default=0, help="seed this many older messages")
    parser.add_argument("--first", type=int, default=0, help="start from this canned answer (0-4)")
    args = parser.parse_args()
    home = Path(args.home or tempfile.mkdtemp(prefix="pa-demo-"))
    app = open_app(home, args)
    try:
        if args.ask:
            ask(app, args.ask)
            print("Asked. It shows on the page within a few seconds.")
        else:
            print(f"Demo home: {home}")
            asyncio.run(serve(app, args.bulk))
    finally:
        app.close()


if __name__ == "__main__":
    main()
