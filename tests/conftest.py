from __future__ import annotations

import pytest
from agents.testing import ScriptedModel

from personal_agent.app import App, build_app
from personal_agent.clock import ManualClock
from personal_agent.config import Config
from personal_agent.db import Database, open_database
from personal_agent.paths import Paths


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def db() -> Database:
    database = open_database(":memory:")
    yield database
    database.close()


@pytest.fixture
def paths(tmp_path) -> Paths:
    p = Paths.resolve(tmp_path / "home")
    p.ensure()
    return p


@pytest.fixture
def make_app(tmp_path, clock, monkeypatch):
    """Build a fully wired `App` on a temp home, driven by a scripted model (no network)."""
    monkeypatch.setenv("TZ", "UTC")
    created: list[App] = []

    def make(model: ScriptedModel | None = None, *, config: Config | None = None) -> App:
        scripted = model if model is not None else ScriptedModel([])
        app = build_app(
            home=tmp_path / "pa-home",
            clock=clock,
            config=config,
            model_factory=lambda name: scripted,
            read_env=False,
        )
        created.append(app)
        return app

    yield make
    for app in created:
        app.close()


@pytest.fixture
def wired(make_app, monkeypatch):
    """Run CLI commands against the test app (and keep it open between commands)."""
    from personal_agent import cli

    def wire(model: ScriptedModel | None = None, *, config: Config | None = None) -> App:
        app = make_app(model, config=config)
        monkeypatch.setattr(app, "close", lambda: None)
        monkeypatch.setattr(cli, "_build", lambda: app)
        return app

    return wire


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fresh agent home for tests that exercise `pa init` and the real wiring."""
    monkeypatch.setenv("PA_HOME", str(tmp_path / "fresh-home"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    return tmp_path / "fresh-home"
