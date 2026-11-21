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
    """早安，今天是这样的。

## 今天
- **09:30** 和平台组站会
- **13:00** 和 Mira 吃午饭（她想改到 *13:30*）
- **16:00** 看牙，`Rua Augusta 112`

> 收件箱里没有急事，只有一件要你拿主意：场地周五前要付订金。

要我把付订金设成日历提醒吗？""",
    """脚本在这里，运行后会打印出最新的那份笔记。

```python
from pathlib import Path

def newest(folder: str, pattern: str = "*.md") -> Path | None:
    files = sorted(Path(folder).expanduser().glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None

print(newest("~/notes"))
```

它会找最近改动过的 `.md` 文件，没有的话返回 `None`。""",
    """三种去的方式比较：

| 方式 | 用时 | 花费 | 备注 |
|:--|--:|--:|:--|
| 火车 | 3 小时 10 分 | €48 | 直达，安静车厢 |
| 飞机 | 1 小时 05 分 | €96 | 另加机场 2 小时 |
| 自驾 | 3 小时 40 分 | €31 | 油费和过路费，自己开 |

想在路上工作的话，火车最合适。时刻表在[这里](https://www.example.com/timetable)。""",
    """好了，摘要写到了 `/notes/2026-11-15.md`：

1. 看了三个未处理的 pull request
2. 回复了 Mira 午饭的事
3. 订好了 16:00 的牙医

- [x] 摘要写好了
- [ ] 发给团队（等你确认）

需要缩短的话告诉我。""",
    "好的，明天 9:00 我提醒你。\n\n还有别的吗？",
]

EARLIER = [
    (-26 * 3600, "in", "telegram", "提醒我续办护照"),
    (-26 * 3600 + 20, "out", "telegram", "记下了，我会在**周一早上**提醒你。"),
    (-5 * 3600, "in", "web", "场地的事，我当时是怎么定的？"),
    (
        -5 * 3600 + 15,
        "out",
        "web",
        "你说选**里斯本**，前提是订金不超过 €500。场地要的是 €450。",
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
        return "这是录音里说的话"


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
        agent=AgentConfig(owner_name="Frank"),
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
        sandbox=SandboxProvider.fixed(None, "演示里不会真的运行命令"),
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
        (-30 * 3600 - (bulk - i) * 1800, "in" if i % 2 == 0 else "out", "web", f"较早的消息 {i}")
        for i in range(bulk)
    ]
    for offset, direction, channel, text in [*older, *EARLIER]:
        app.db.execute(
            "INSERT INTO transcript(at, direction, channel, kind, text, outbox_id, meta)"
            " VALUES (?, ?, ?, 'message', ?, NULL, '{}')",
            (now + offset, direction, channel, text),
        )
    ask(app, "git -C ~/notes push origin main", "我想把你的笔记推送上去，可以吗？")


def ask(app: App, command: str, text: str = "要运行这个吗？") -> None:
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
