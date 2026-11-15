"""`pa doctor`: find setup problems before they surface as confusing runtime failures.

`static_checks` never calls the network and never creates anything. `live_checks` makes two
real model calls (a few cents) to prove the parts that cannot be unit-tested: the API key, the
tool schemas, and replaying stored history (including encrypted reasoning) back to the API.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from personal_agent.clock import get_tz
from personal_agent.config import ConfigError, api_key, load_config
from personal_agent.db import SCHEMA_VERSION
from personal_agent.harness.pricing import is_known
from personal_agent.paths import Paths

if TYPE_CHECKING:
    from personal_agent.app import App

Status = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str = ""
    fix: str = ""


def _fts5_available() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return False
    return True


def static_checks(paths: Paths) -> list[Check]:
    checks: list[Check] = []

    def add(name: str, status: Status, detail: str = "", fix: str = "") -> None:
        checks.append(Check(name, status, detail, fix))

    add("python", "ok", sys.version.split()[0])  # pyproject.toml enforces the minimum version

    if not paths.home.is_dir():
        add("home", "fail", f"{paths.home} does not exist", "Run: pa init")
        return checks
    mode = stat.S_IMODE(paths.home.stat().st_mode)
    if mode & 0o077:
        add(
            "home",
            "warn",
            f"{paths.home} is accessible by other users ({mode:04o})",
            f"chmod 700 {paths.home}",
        )
    else:
        add("home", "ok", str(paths.home))

    if not paths.env_file.exists():
        add("env file", "warn", "no .env file", "Run: pa init")
    elif problems := paths.env_file_warnings():
        add("env file", "warn", problems[0])
    else:
        add("env file", "ok", "private (owner only)")

    try:
        cfg = load_config(paths)
    except ConfigError as exc:
        add("config", "fail", str(exc), f"Fix {paths.config_file}")
        return checks
    add(
        "config",
        "ok",
        str(paths.config_file) if paths.config_file.exists() else "defaults (no config.toml)",
    )

    if api_key():
        add("OpenAI key", "ok", "OPENAI_API_KEY is set")
    elif cfg.model.base_url or os.environ.get("OPENAI_BASE_URL"):
        add("OpenAI key", "warn", "no key, but a custom endpoint is configured")
    else:
        add(
            "OpenAI key",
            "fail",
            "OPENAI_API_KEY is not set",
            f"Put OPENAI_API_KEY=... in {paths.env_file}",
        )

    for role, name in (("main model", cfg.model.main), ("fast model", cfg.model.fast)):
        if is_known(name):
            add(role, "ok", name)
        else:
            add(
                role, "warn", f"{name}: no price on file, so cost estimates assume the highest rate"
            )

    if cfg.agent.timezone:
        try:
            ZoneInfo(cfg.agent.timezone)
            add("timezone", "ok", cfg.agent.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            add("timezone", "warn", f"unknown timezone {cfg.agent.timezone!r}; using the system's")
    else:
        add("timezone", "ok", f"detected: {get_tz(None)}")

    if _fts5_available():
        add("sqlite", "ok", f"{sqlite3.sqlite_version}, FTS5 available")
    else:
        add(
            "sqlite",
            "warn",
            f"{sqlite3.sqlite_version} without FTS5",
            "Memory search will be slower and less precise.",
        )

    if paths.db_file.exists():
        try:
            conn = sqlite3.connect(f"file:{paths.db_file}?mode=ro", uri=True)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as exc:
            add("database", "fail", f"cannot open {paths.db_file.name}: {exc}")
        else:
            if version > SCHEMA_VERSION:
                add(
                    "database",
                    "fail",
                    f"schema v{version} is newer than this program (v{SCHEMA_VERSION})",
                    "Upgrade personal-agent.",
                )
            else:
                add("database", "ok", f"schema v{version}")
    else:
        add("database", "warn", "not created yet", "Run: pa init")

    unreadable = []
    for path in sorted(paths.preferences_dir.glob("*")) if paths.preferences_dir.is_dir() else []:
        try:
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append(f"{path.name}: {exc}")
    if unreadable:
        add(
            "preferences",
            "fail",
            "; ".join(unreadable),
            "The agent refuses to run without being able to read your rules.",
        )
    else:
        add("preferences", "ok", str(paths.preferences_dir))
    return checks


LIVE_PROMPTS = (
    "This is a connectivity self-test. Use the memory tool to view /memories, then reply with the "
    "single word READY.",
    "Second self-test turn. In one short sentence, say what I asked you to do in my previous message.",
)


async def live_checks(app: App) -> list[Check]:
    """Two real turns on a throwaway session. Costs a few cents.

    Turn 1 proves the key, the model name and the tool schemas. Turn 2 replays the stored
    history, including reasoning items, back to the API, which is the part a stateless
    (`store = false`) setup can get wrong.
    """
    from personal_agent.core.events import Kind, NewEvent
    from personal_agent.core.worker import Worker

    checks: list[Check] = []
    session = f"doctor-{int(app.clock.now())}"
    worker = Worker(app)
    total = 0.0
    for number, prompt in enumerate(LIVE_PROMPTS, start=1):
        app.inbox.add(
            NewEvent(source="cli", kind=Kind.MESSAGE, session_key=session, payload={"text": prompt})
        )
        result = await worker.run_once(session_key=session, on_failure="drop")
        name = f"live turn {number}"
        if result is None or result.failure is not None or result.outcome is None:
            detail = result.failure.summary if result and result.failure else "no run happened"
            hint = (result.failure.user_hint if result and result.failure else None) or ""
            checks.append(Check(name, "fail", detail, hint))
            break
        run = app.runs.get(result.outcome.run_id)
        total += run.cost_usd
        detail = (
            f"{run.requests} model calls, {run.tool_calls} tool calls, about ${run.cost_usd:.4f}"
        )
        if result.outcome.status != "completed":
            checks.append(Check(name, "fail", f"run ended as {result.outcome.status}: {detail}"))
            break
        if number == 1 and run.tool_calls == 0:
            checks.append(Check(name, "warn", f"the model did not call the memory tool; {detail}"))
        else:
            checks.append(Check(name, "ok", detail))
    else:
        checks.append(
            Check(
                "history replay",
                "ok",
                f"stored history was accepted by the API; total about ${total:.4f}",
            )
        )

    app.sessions.clear(session)
    for message in app.outbox.list(status="pending"):
        if message.session_key == session:
            app.outbox.cancel(message.id)
    return checks
