"""What the live tests share: the settings they start from, a way to talk to a real agent, and
small generators for the pictures and recordings they send."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from personal_agent.app import App
from personal_agent.channels.ingress import Ingress, Upload
from personal_agent.config import Config
from personal_agent.core.dispatcher import Dispatcher
from personal_agent.core.outbox import OutMessage
from personal_agent.core.runs import RunRecord
from tooling import drain

LIVE_HOME = Path(os.environ.get("PA_LIVE_HOME", "~/.personal-agent-livetest")).expanduser()
MODEL = os.environ.get("PA_LIVE_MODEL", "gpt-6.1-sol")
EFFORT = "medium"

SPENT: list[float] = []


def record_spend(usd: float) -> None:
    """Keep what one agent (or one trial) cost, so the run can say what it cost altogether."""
    SPENT.append(float(usd))


def live_config(**sections: dict[str, Any]) -> Config:
    """The settings every live test starts from; `sections` adds to or overrides them."""
    base: dict[str, dict[str, Any]] = {
        "agent": {"name": "Sam", "owner_name": "Frank", "timezone": "Asia/Shanghai"},
        "model": {"main": MODEL, "fast": MODEL, "reasoning_effort": EFFORT},
        "budget": {"per_run_usd": 0.5, "daily_usd": 2.0},
    }
    for name, values in sections.items():
        base[name] = {**base.get(name, {}), **values}
    return Config.model_validate(base)


class Collector:
    """A channel that remembers what it was asked to deliver."""

    name = "cli"

    def __init__(self) -> None:
        self.sent: list[OutMessage] = []

    async def send(self, message: OutMessage) -> None:
        self.sent.append(message)


@dataclass
class Reply:
    """What came back for one message of the owner's."""

    messages: list[OutMessage] = field(default_factory=list)
    runs: list[RunRecord] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(m.text for m in self.messages)

    @property
    def run(self) -> RunRecord:
        return self.runs[-1]

    @property
    def cost(self) -> float:
        return sum(r.cost_usd for r in self.runs)


class Chat:
    """The owner talking to a real agent, the way a channel would hand their words over."""

    def __init__(self, app: App) -> None:
        self.app = app

    async def say(
        self,
        text: str = "",
        *,
        session: str = "main",
        images: tuple[Upload, ...] = (),
        voice: Upload | None = None,
    ) -> Reply:
        ingress = Ingress(self.app, session_key=session)
        accepted = await ingress.owner_message(channel="cli", text=text, images=images, voice=voice)
        assert accepted.status == "queued", f"the message was not queued: {accepted}"
        return await self.settle(session)

    async def settle(self, session: str = "main") -> Reply:
        """Let the agent work until it has nothing left to do in `session`; collect what it said."""
        results = await drain(self.app, session_key=session)
        sink = Collector()
        await Dispatcher(self.app, {"cli": sink}).deliver_once()
        runs = [
            run
            for r in results
            if r.outcome is not None and (run := self.app.runs.get(r.outcome.run_id)) is not None
        ]
        return Reply(sink.sent, runs)

    def tools_used(self, run: RunRecord | None = None) -> list[str]:
        """Names of the tools the agent called (in `run`, or ever), oldest first."""
        rows = self.app.audit.recent(200, run_id=run.id if run else None)
        return [row.tool for row in reversed(rows)]


def plain_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A real PNG of one colour, made without any imaging library."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes(rgb) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def spoken(sentence: str) -> Upload:
    """A recording of `sentence` read aloud by this Mac, as a phone app would send it."""
    if not (shutil.which("say") and shutil.which("afconvert")):
        pytest.skip("needs macOS `say` and `afconvert` to record speech")
    with tempfile.TemporaryDirectory() as folder:
        aiff, m4a = Path(folder) / "voice.aiff", Path(folder) / "voice.m4a"
        subprocess.run(["say", "-o", str(aiff), sentence], check=True)
        subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(aiff), str(m4a)], check=True)
        return Upload(m4a.read_bytes(), "audio/m4a", "voice.m4a", duration_s=5.0)
