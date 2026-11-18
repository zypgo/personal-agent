"""Tests against the real OpenAI API. They are skipped unless you ask for them:

    PA_LIVE=1 uv run pytest tests/live -q

What they need
- The key: inject `OPENAI_API_KEY` into the environment, or use `$PA_LIVE_HOME/.env`
  (default `~/.personal-agent-livetest`). An injected key wins; tests copy it to no file.
  An explicitly requested live run without a key fails rather than silently skipping.
- Money: every model call uses `gpt-6.1-sol` with medium reasoning (`PA_LIVE_MODEL` changes the
  name). A test caps its own spending, and the whole folder costs about 35 cents (the last
  line of the run says what it really cost).

Each test builds its own agent in a temporary home, so nothing here touches your real one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from livehelp import LIVE_HOME, SPENT, Chat, live_config, record_spend

from live_credentials import live_key
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
    try:
        key = live_key(LIVE_HOME)
    except RuntimeError as exc:
        pytest.fail(str(exc), pytrace=False)
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
        record_spend(app.db.scalar("SELECT SUM(cost_usd) FROM runs", default=0.0))
        app.close()


def pytest_terminal_summary(terminalreporter) -> None:
    if SPENT:
        terminalreporter.write_line(
            f"live spend: ${sum(SPENT):.4f} across {len(SPENT)} agents / trial sets "
            "(model calls only; the OpenAI dashboard is the authority)"
        )
