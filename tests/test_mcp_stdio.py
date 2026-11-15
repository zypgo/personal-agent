"""MCP against real server programs: a stdio child process and a streamable-HTTP server.

These are the tests that prove the parts the fakes cannot: the child gets a minimal environment,
stderr goes to a private log, a call that takes too long is given up on, nothing is left running,
and a server that changes its tools after being reviewed is caught.
"""

from __future__ import annotations

import asyncio
import json
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from personal_agent.config import Config

SCRIPT = str(Path(__file__).parent / "mcp_fixture_server.py")


def entry(marker: str, **extra) -> dict:
    return {
        "name": "fixture",
        "command": sys.executable,
        "args": [SCRIPT, "--marker", marker],
        "pin": "auto",
        "connect_timeout_s": 30,
        "call_timeout_s": 20,
        **extra,
    }


def config(*servers: dict, **mcp) -> Config:
    return Config.model_validate({"mcp": {"servers": list(servers), **mcp}})


def running(marker: str) -> list[int]:
    out = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True, check=False)
    return [int(p) for p in out.stdout.split()]


@pytest.fixture
async def real(make_app, monkeypatch):
    """`real(config)` -> an app whose MCP servers are real programs; everything is stopped after."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "test-" + "a1B2c3D4" * 4)
    monkeypatch.setenv("FIXTURE_TOKEN", "token-for-the-fixture")
    apps = []

    def make(cfg: Config):
        app = make_app(config=cfg)
        apps.append(app)
        return app

    yield make
    for app in apps:
        await app.stop_services()


async def call(app, tool: str, **args) -> str:
    return await app.mcp._call("fixture", tool, json.dumps(args))


def names(app) -> list[str]:
    return sorted(n for n in app.tools.names() if n.startswith("mcp__"))


# ---- stdio ----------------------------------------------------------------------------------------------
async def test_a_real_server_is_started_listed_and_answers(real):
    marker = uuid.uuid4().hex
    app = real(config(entry(marker)))
    await app.mcp.prepare()

    assert app.mcp.notes() == []
    assert {"mcp__fixture__echo", "mcp__fixture__add", "mcp__fixture__get-weather"} <= set(
        names(app)
    )
    assert (await call(app, "echo", text="hello")).endswith("hello\n</untrusted>")
    assert "5" in await call(app, "add", a=2, b=3)
    assert running(marker)  # a program is running


async def test_the_program_is_gone_after_stop(real):
    marker = uuid.uuid4().hex
    app = real(config(entry(marker)))
    await app.mcp.prepare()
    assert running(marker)
    await app.stop_services()
    for _ in range(50):
        if not running(marker):
            break
        await asyncio.sleep(0.05)
    assert running(marker) == []


async def test_the_program_only_sees_a_minimal_environment_and_what_was_configured(real):
    marker = uuid.uuid4().hex
    app = real(config(entry(marker, env={"FIXTURE_TOKEN": "${FIXTURE_TOKEN}"})))
    await app.mcp.prepare()

    assert "<unset>" in await call(app, "env_probe", name="OPENAI_API_KEY")
    assert "token-for-the-fixture" in await call(app, "env_probe", name="FIXTURE_TOKEN")
    assert "<unset>" not in await call(app, "env_probe", name="PATH")


async def test_text_that_tries_to_give_orders_stays_inside_the_fence(real):
    marker = uuid.uuid4().hex
    app = real(config(entry(marker)))
    await app.mcp.prepare()
    out = await call(app, "injection")
    assert out.startswith('<untrusted source="mcp:fixture">')
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in out
    assert out.count("</untrusted>") == 1  # the text's own closing tag was defused


async def test_images_are_left_out_and_the_model_is_told(real):
    marker = uuid.uuid4().hex
    app = real(config(entry(marker)))
    await app.mcp.prepare()
    out = await call(app, "image")
    assert "here is the picture" in out and "iVBORw0KGgo" not in out
    assert "an image was returned and left out" in out


async def test_a_failure_the_server_reports_is_data_and_not_a_failed_call(real):
    marker = uuid.uuid4().hex
    app = real(config(entry(marker)))
    await app.mcp.prepare()
    out = await call(app, "fail")
    assert out.startswith("The tool fixture/fail reported a failure:")
    assert "Error executing tool fail" in out


async def test_a_huge_answer_is_cut(real):
    marker = uuid.uuid4().hex
    app = real(config(entry(marker)))
    await app.mcp.prepare()
    out = await call(app, "big", n=200_000)
    assert len(out) <= app.config.runtime.tool_output_chars and "omitted" in out


async def test_a_call_that_takes_too_long_is_given_up_on_and_the_server_recovers(real):
    marker = uuid.uuid4().hex
    app = real(config(entry(marker, call_timeout_s=1)))
    await app.mcp.prepare()
    out = await call(app, "slow", seconds=4)
    assert out.startswith("Error: fixture/slow did not answer within 1 seconds")
    assert "may have partly run" in out

    await app.mcp.prepare()  # notices that the last call went wrong, and checks the server
    assert (await call(app, "echo", text="again")).rstrip().endswith("again\n</untrusted>")


async def test_a_program_that_does_not_exist_is_a_note_and_not_a_crash(real):
    app = real(config({"name": "fixture", "command": "no-such-program-anywhere", "pin": "auto"}))
    await app.mcp.prepare()
    [note] = app.mcp.notes()
    assert "MCP server 'fixture' is not available" in note and names(app) == []


async def test_what_the_program_writes_to_stderr_goes_to_a_private_log(real):
    marker = uuid.uuid4().hex
    app = real(config(entry(marker)))
    await app.mcp.prepare()
    log = app.mcp.log_path("fixture")
    assert log.exists() and stat.S_IMODE(log.stat().st_mode) == 0o600

    await call(app, "shout", line=f"hello-from-stderr-{marker}")
    for _ in range(50):
        if marker in app.mcp.log_tail("fixture"):
            break
        await asyncio.sleep(0.05)
    assert f"hello-from-stderr-{marker}" in app.mcp.log_tail("fixture")
    out = await call(app, "echo", text="x")
    assert "hello-from-stderr" not in out  # stderr never reaches the model


# ---- a server that changes after the owner reviewed it ---------------------------------------------------
async def test_a_server_that_changes_its_tools_after_review_is_caught(real):
    marker = uuid.uuid4().hex
    first = real(config(entry(marker)))
    await first.mcp.prepare()
    assert "mcp__fixture__echo" in names(first)
    await first.stop_services()

    # The same program, "updated": a changed description for echo, and a new tool.
    second = real(config(entry(marker, env={"FIXTURE_VARIANT": "2"})))
    await second.mcp.prepare()
    assert "mcp__fixture__echo" not in names(second)  # changed: withheld until reviewed
    assert "mcp__fixture__extra" not in names(second)  # new: withheld until reviewed
    assert "mcp__fixture__add" in names(second)  # unchanged tools keep working
    [note] = second.mcp.notes()
    assert "new or changed" in note


# ---- streamable HTTP --------------------------------------------------------------------------------------
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server():
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, SCRIPT, "--http", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(150):
            with socket.socket() as s:
                s.settimeout(0.2)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        else:
            raise AssertionError("the HTTP fixture server did not start")
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def test_a_streamable_http_server_works_like_a_stdio_one(real, http_server, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    app = real(
        config(
            {
                "name": "fixture",
                "transport": "http",
                "url": f"http://127.0.0.1:{http_server}/mcp",
                "pin": "auto",
                "connect_timeout_s": 20,
            }
        )
    )
    await app.mcp.prepare()
    assert app.mcp.notes() == []
    assert "mcp__fixture__add" in names(app)
    assert "5" in await call(app, "add", a=2, b=3)
