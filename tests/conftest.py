from __future__ import annotations

import gc
import os

import pytest
from agents.testing import ScriptedModel
from hypothesis import HealthCheck, settings

from personal_agent import launchd
from personal_agent.app import App, build_app
from personal_agent.clock import ManualClock
from personal_agent.config import Config
from personal_agent.db import Database, open_database
from personal_agent.paths import Paths
from personal_agent.security.sandbox import SandboxProvider

# Property tests. Every profile has no per-example deadline: a slow machine must not fail a
# property because one example took long (the tests that care about speed time themselves).
#   default   random examples, a failing one is remembered in `.hypothesis/` and replayed
#   ci        the same examples every run, so a red run means the code changed (scripts/check.sh)
#   thorough  ten times as many; `quick` a fifth (a test that sets its own count keeps it, so
#             to explore harder run the suite under several `--hypothesis-seed=N`)
settings.register_profile("default", max_examples=100, deadline=None)
settings.register_profile("ci", max_examples=100, deadline=None, derandomize=True)
settings.register_profile("thorough", max_examples=1000, deadline=None)
settings.register_profile(
    "quick", max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))


# A database, file or socket nobody closed is reported (and, see `filterwarnings` in pyproject.toml,
# fails a test) when the garbage collector finds it, which can be many tests after the one that
# forgot. So every test ends with a collection, which makes the one that forgot the one that fails.
# Collecting the whole heap each time would triple the run; freezing what is alive between tests
# keeps each collection to what that test itself made.
def pytest_sessionstart(session: pytest.Session) -> None:
    gc.collect()
    gc.freeze()


@pytest.hookimpl(trylast=True)  # after the fixtures have been torn down
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    gc.collect()
    gc.freeze()


@pytest.fixture(autouse=True)
def _no_test_changes_the_real_launchd(monkeypatch, tmp_path):
    """A test may look at launchd but never change it, and never write into the real
    `~/Library/LaunchAgents`: whatever code a test reaches by mistake (a refactor, or a mutation
    that removes a guard) must not be able to install a job that starts at the next login.

    This exists because it happened: with a guard mutated away, a CLI test that was meant to stop
    at "run `pa init` first" installed and loaded a real job that ran for half an hour."""
    real = launchd.system_launchctl

    def only_looking(*args: str):
        if args and args[0] == "print":
            return real(*args)
        raise AssertionError(f"a test tried to change the real launchd: launchctl {' '.join(args)}")

    monkeypatch.setattr(launchd, "system_launchctl", only_looking)
    monkeypatch.setattr(launchd, "agents_dir", lambda: tmp_path / "guarded-launchagents")


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

    def make(
        model: ScriptedModel | None = None,
        *,
        config: Config | None = None,
        sandbox: SandboxProvider | None = None,
        mcp_factory=None,
        plugin_entry_points=None,
        transcriber=None,
    ) -> App:
        scripted = model if model is not None else ScriptedModel([])
        app = build_app(
            home=tmp_path / "pa-home",
            clock=clock,
            config=config,
            model_factory=lambda name: scripted,
            # Hermetic by default: no probing of the machine, so no shell unless a test wants one.
            sandbox=sandbox or SandboxProvider.fixed(None, "tests run without a sandbox"),
            read_env=False,
            mcp_factory=mcp_factory,
            plugin_entry_points=plugin_entry_points,
            transcriber=transcriber,
        )
        created.append(app)
        return app

    yield make
    for app in created:
        app.close()


@pytest.fixture
def use_app(make_app, monkeypatch):
    """`use_app(app)`: from now on `pa` commands (see `helpers.invoke`) run against `app`, which
    stays open between commands: a command closes the app it opened, and the test goes on using it.

    The app's `close` is switched off only until this fixture ends, which is before `make_app`
    closes it for real; patching it with `monkeypatch` would last until the end of the whole test
    and leave the database connection open."""
    from personal_agent.commands import common

    borrowed: list[App] = []

    def use(app: App) -> App:
        app.close = lambda: None  # type: ignore[method-assign]
        borrowed.append(app)
        monkeypatch.setattr(common, "build", lambda: app)
        return app

    yield use
    for app in borrowed:
        del app.close


@pytest.fixture
def wired(make_app, use_app):
    """Run CLI commands against a test app wired to a scripted model."""

    def wire(model: ScriptedModel | None = None, *, config: Config | None = None) -> App:
        return use_app(make_app(model, config=config))

    return wire


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fresh agent home for tests that exercise `pa init` and the real wiring."""
    monkeypatch.setenv("PA_HOME", str(tmp_path / "fresh-home"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    return tmp_path / "fresh-home"


@pytest.fixture
def site():
    """A web server on 127.0.0.1 (see tests/webserver.py)."""
    from webserver import Site

    running = Site().start()
    yield running
    running.stop()


@pytest.fixture
def other_site():
    """A second server, on another port, which counts as another site."""
    from webserver import Site

    running = Site().start()
    yield running
    running.stop()


@pytest.fixture(autouse=True)
def no_dns_probe(monkeypatch):
    """`pa doctor` looks up a public name to detect proxy fake-IP DNS; tests never touch the network."""
    monkeypatch.setattr("personal_agent.doctor._resolve_probe", lambda name, timeout=3.0: [])
