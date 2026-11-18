"""A real browser protocol, real serve processes and two real model turns across a restart.

Only synthetic data is used. Credentials and pairing tokens stay in memory; the subprocess
gets its model key through its environment and neither its output nor its cookies are printed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import socket
import sqlite3
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import tomlkit
from livehelp import EFFORT, LIVE_HOME, MODEL, record_spend

from live_credentials import live_key
from personal_agent.app import build_app
from personal_agent.config import Config

REPO = Path(__file__).resolve().parents[2]
REPLY_TIMEOUT_S = 180.0


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _prepare(home: Path, port: int) -> str:
    """Create an isolated configuration and a single-use browser invitation, without a model call."""
    home.mkdir(mode=0o700)
    sections = {
        "agent": {"name": "Web live test", "timezone": "UTC", "max_turns": 3},
        "model": {
            "main": MODEL,
            "fast": MODEL,
            "reasoning_effort": EFFORT,
            "base_url": "https://api.openai.com/v1",
            "timeout_s": 60.0,
            "max_retries": 0,
        },
        "budget": {"per_run_usd": 0.20, "daily_usd": 0.50},
        "tools": {
            "shell": False,
            "files": False,
            "web_fetch": False,
            "http_request": False,
            "web_search": "off",
        },
        "policy": {"mode": "read_only"},
        "skills": {"enabled": False},
        "media": {"images": False, "voice": False},
        "dreaming": {"enabled": False},
        "context": {"compact": False},
        "notify": {"channel": "web"},
        "runtime": {
            "concurrency": 1,
            "poll_s": 0.05,
            "shutdown_grace_s": 5.0,
            "run_timeout_s": 120.0,
        },
        "web": {"enabled": True, "host": "127.0.0.1", "port": port},
    }
    config = Config.model_validate(sections)
    path = home / "config.toml"
    path.write_text(tomlkit.dumps(sections), encoding="utf-8")
    path.chmod(0o600)
    app = build_app(home=home, config=config, read_env=False)
    try:
        return app.web_auth.invite("Live test browser").code
    finally:
        app.close()


def _environment(home: Path, key: str) -> dict[str, str]:
    # Preserve transport configuration where the real model needs a proxy or custom CA, but
    # carry no unrelated channel credentials, deployment settings or agent home into the child.
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    }
    environment = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    return {
        **environment,
        "PA_HOME": str(home),
        "OPENAI_API_KEY": key,
        "PYTHONPATH": str(REPO / "src"),
        "PYTHONUNBUFFERED": "1",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }


async def _start(
    home: Path,
    environment: dict[str, str],
    browser: httpx.AsyncClient,
    processes: list[asyncio.subprocess.Process],
) -> asyncio.subprocess.Process:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "personal_agent",
        "serve",
        cwd=home,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    processes.append(process)  # cleanup owns it before any readiness check can fail
    try:
        async with asyncio.timeout(30):
            while True:
                assert process.returncode is None, "serve exited before becoming ready"
                try:
                    response = await browser.get("/healthz", timeout=1.0)
                    if response.status_code == 200 and response.json() == {"ok": True}:
                        return process
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)  # poll readiness, never wait a guessed startup delay
    except TimeoutError:
        pytest.fail("serve did not become healthy within 30 seconds", pytrace=False)


async def _stop(process: asyncio.subprocess.Process) -> int:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            return await asyncio.wait_for(process.wait(), 15)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
    return await asyncio.wait_for(process.wait(), 10)


def _query(home: Path, sql: str) -> list[sqlite3.Row]:
    uri = (home / "state.db").as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5)) as database:
        database.row_factory = sqlite3.Row
        return database.execute(sql).fetchall()


def _assert_work_count(home: Path, count: int) -> None:
    events = _query(home, "SELECT id FROM events WHERE source='web' AND kind='message'")
    runs = _query(
        home,
        "SELECT status,requests,input_tokens,output_tokens,cost_usd,tool_calls FROM runs",
    )
    assert len(events) == len(runs) == count
    assert all(row["status"] == "completed" for row in runs)
    assert all(row["requests"] == 1 and row["tool_calls"] == 0 for row in runs)
    assert all(row["input_tokens"] > 0 and row["output_tokens"] > 0 for row in runs)
    assert all(0 < row["cost_usd"] < 0.20 for row in runs)


async def _pair(browser: httpx.AsyncClient, code: str) -> None:
    __tracebackhide__ = True
    response = await browser.post("/api/pair", json={"code": code})
    assert response.status_code == 200, "browser pairing failed"
    assert browser.cookies.get("pa_session") is not None, "pairing returned no session cookie"
    reused = await browser.post("/api/pair", json={"code": code})
    assert reused.status_code == 403, "a pairing invitation was usable twice"


async def _event(lines: AsyncIterator[str]) -> tuple[str, dict[str, Any], str | None]:
    name, data, event_id = "message", [], None
    async for line in lines:
        if not line and data:
            return name, json.loads("\n".join(data)), event_id
        if line.startswith("event:"):
            name = line.partition(":")[2].strip()
        elif line.startswith("data:"):
            data.append(line.partition(":")[2].lstrip())
        elif line.startswith("id:"):
            event_id = line.partition(":")[2].strip()
    raise AssertionError("the event stream ended before the reply")


async def _send(browser: httpx.AsyncClient, text: str, client_id: str) -> dict[str, Any]:
    response = await browser.post(
        "/api/messages",
        data={"text": text, "client_id": client_id},
        files={"form": (None, "")},  # the page uses multipart even without an attachment
    )
    assert response.status_code == 200
    return response.json()


async def _say(
    browser: httpx.AsyncClient, text: str, *, after: int = 0, duplicate: bool = False
) -> dict[str, Any]:
    client_id = secrets.token_hex(16)
    async with (
        asyncio.timeout(REPLY_TIMEOUT_S),
        browser.stream("GET", "/api/stream", params={"after": after}) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        async with contextlib.aclosing(response.aiter_lines()) as lines:
            name, _, _ = await _event(lines)
            assert name == "hello"
            accepted = await _send(browser, text, client_id)
            assert accepted["status"] == "queued"
            if duplicate:
                repeated = await _send(browser, text, client_id)
                assert repeated["status"] == "duplicate"
            while True:
                name, data, event_id = await _event(lines)
                if name == "turn" and data["direction"] == "out":
                    assert data["kind"] == "message", (
                        "the agent returned a failure or approval notice"
                    )
                    assert data.get("meta", {}).get("final_response") is True
                    assert data["channel"] == "web"
                    assert event_id == str(data["id"])
                    return data


async def test_web_pairing_reply_deduplication_and_restart_preserve_the_conversation(tmp_path):
    try:
        key = live_key(LIVE_HOME)
    except RuntimeError as exc:
        pytest.fail(str(exc), pytrace=False)
    home = tmp_path / "web-home"
    port = _free_port()
    code = _prepare(home, port)
    environment = _environment(home, key)
    del key
    url = f"http://127.0.0.1:{port}"
    processes: list[asyncio.subprocess.Process] = []
    try:
        async with httpx.AsyncClient(
            base_url=url,
            headers={"Origin": url, "X-PA": "1"},
            timeout=httpx.Timeout(30.0, connect=5.0),
            trust_env=False,
        ) as browser:
            first = await _start(home, environment, browser, processes)
            for path in (
                "/",
                "/workspace",
                "/static/query.js",
                "/static/markdown-parser.js",
                "/manifest.webmanifest",
            ):
                public = await browser.get(path)
                assert public.status_code == 200 and public.content, (
                    "public web shell is incomplete"
                )
            assert (await browser.get("/api/session")).status_code == 401
            assert (await browser.get("/api/v3/openapi.json")).status_code == 401
            await _pair(browser, code)
            del code
            session = await browser.get("/api/session")
            assert session.status_code == 200
            principal_id = session.json()["principal_id"]
            schema = await browser.get("/api/v3/openapi.json")
            assert schema.status_code == 200
            assert schema.headers["cache-control"] == "private, no-store"
            assert "/api/v3/tasks" in schema.json()["paths"]

            codeword = "word-" + secrets.token_hex(6)
            reply = await _say(
                browser,
                f"The codeword for this conversation is {codeword}. Reply only with this "
                "codeword. Do not use any tools or save it to memory.",
                duplicate=True,
            )
            if codeword not in reply["text"]:
                pytest.fail("the first real model reply omitted the codeword", pytrace=False)
            _assert_work_count(home, 1)

            # An empty second space must not show this conversation. No model call is needed.
            created = await browser.post("/api/v3/spaces", json={"name": "Live isolated space"})
            assert created.status_code == 201
            space_id = created.json()["space"]["id"]
            isolated = await browser.get("/api/history", params={"space": space_id})
            assert isolated.status_code == 200
            assert isolated.json()["turns"] == []
            history = await browser.get("/api/history")
            assert history.status_code == 200
            before_ids = [turn["id"] for turn in history.json()["turns"]]
            assert len(before_ids) == 2 and before_ids[-1] == reply["id"]

            assert await _stop(first) == 0, "serve did not shut down gracefully"
            # The same cookie jar and the same files survive a genuinely new server process.
            second = await _start(home, environment, browser, processes)
            session = await browser.get("/api/session")
            assert session.status_code == 200
            assert session.json()["principal_id"] == principal_id
            restored = await browser.get("/api/history")
            assert restored.status_code == 200
            assert [turn["id"] for turn in restored.json()["turns"]] == before_ids
            _assert_work_count(home, 1)
            recalled = await _say(
                browser,
                "What was the codeword I gave you earlier in this conversation? Reply only "
                "with that codeword. Do not use any tools.",
                after=reply["id"],
            )
            if codeword not in recalled["text"]:
                pytest.fail("the restarted agent did not recall the conversation", pytrace=False)
            _assert_work_count(home, 2)
            isolated = await browser.get("/api/history", params={"space": space_id})
            assert isolated.status_code == 200 and isolated.json()["turns"] == []
            assert (await browser.post("/api/logout")).status_code == 200
            assert (await browser.get("/api/session")).status_code == 401
            assert (await browser.get("/api/history")).status_code == 401
            assert (await browser.get("/api/v3/openapi.json")).status_code == 401
            assert await _stop(second) == 0, "restarted serve did not shut down gracefully"
    finally:
        # Stop every child before reading spend, including failed startup or assertion paths.
        try:
            results = await asyncio.gather(
                *(_stop(process) for process in processes), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        finally:
            [row] = _query(home, "SELECT COALESCE(SUM(cost_usd),0.0) AS total FROM runs")
            record_spend(row["total"])
