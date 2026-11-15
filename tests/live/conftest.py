"""Tests against the real OpenAI API. They are skipped unless you ask for them:

    PA_LIVE=1 uv run pytest tests/live -q

What they need
- The key: `OPENAI_API_KEY` in `$PA_LIVE_HOME/.env` (default `~/.personal-agent-livetest`). It is
  read into this process's environment and nowhere else; the tests copy it to no file.
- Money: every model call uses `gpt-6.1-sol` with medium reasoning (`PA_LIVE_MODEL` changes the
  name). A test caps its own spending, and the whole folder costs well under a dollar.

Each test builds its own agent in a temporary home, so nothing here touches your real one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values
from livehelp import LIVE_HOME, Chat, live_config

from personal_agent.app import App, build_app
from personal_agent.config import Config

HERE = Path(__file__).parent


def pytest_collection_modifyitems(config, items) -> None:
    if os.environ.get("PA_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="spends real money: run with PA_LIVE=1")
    for item in items:
        if HERE in Path(str(item.fspath)).parents:
            item.add_marker(skip)


@pytest.fixture
async def live(tmp_path, monkeypatch):
    """`live(config=None, **build_options)` starts a real agent and returns a `Chat` with it."""
    key = (dotenv_values(LIVE_HOME / ".env").get("OPENAI_API_KEY") or "").strip()
    if not key:
        pytest.skip(f"no OPENAI_API_KEY in {LIVE_HOME}/.env")
    monkeypatch.setenv("OPENAI_API_KEY", key)
    started: list[App] = []

    def start(config: Config | None = None, **options: Any) -> Chat:
        app = build_app(
            home=tmp_path / "home", config=config or live_config(), read_env=False, **options
        )
        started.append(app)
        return Chat(app)

    yield start
    for app in started:
        await app.stop_services()
        app.close()
