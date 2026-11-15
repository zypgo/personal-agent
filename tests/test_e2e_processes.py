"""The real program, as real processes, stopped the hard way.

`test_crash_consistency.py` kills a simulated process after every durable write. These tests do
it for real: `pa serve` runs as a subprocess on a home directory, talks to a fake OpenAI endpoint
(`fake_openai.py`) that can be made to hold its answers, and is stopped with SIGKILL or SIGTERM
while it waits for the model, while it waits for the owner, or right after an action ran. A second
process then starts on the same files, as launchd would start it, and the tests check what the
owner saw and what was done.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from fake_openai import FakeOpenAI
from personal_agent.clock import SystemClock
from personal_agent.core.events import Kind, NewEvent
from personal_agent.core.inbox import Inbox
from personal_agent.db import open_database

CONFIG = """
[model]
main = "fake-model"
base_url = "{url}"

[tools]
shell = false

[runtime]
shutdown_grace_s = {grace}
"""
FAKE_KEY = "sk-" + "test-" + "not-a-real-key"
LEAKY = ("OPENAI_", "TELEGRAM_", "ANTHROPIC_", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


class Program:
    """`pa serve --console` as a process, with everything it prints kept."""

    def __init__(self, home: Home) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "personal_agent", "serve", "--console"],
            env=home.env(),
            cwd=home.path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.lines: list[str] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.append(line.rstrip("\n"))

    @property
    def output(self) -> str:
        return "\n".join(self.lines)

    @property
    def running(self) -> bool:
        return "is running" in self.output

    def kill(self) -> None:
        """SIGKILL: no handler runs, nothing is cleaned up."""
        self.process.kill()
        self.process.wait(timeout=10)
        self._reader.join(timeout=5)

    def terminate(self) -> None:
        """SIGTERM: what `launchctl stop` and a shutdown send."""
        self.process.send_signal(signal.SIGTERM)

    def wait(self, timeout: float = 30.0) -> int:
        code = self.process.wait(timeout=timeout)
        self._reader.join(timeout=5)
        return code


class Home:
    def __init__(self, root: Path, fake: FakeOpenAI, *, grace_s: float) -> None:
        self.path = root / "agent-home"
        self.path.mkdir()
        (self.path / "config.toml").write_text(CONFIG.format(url=fake.url, grace=grace_s))
        self.fake = fake
        self.programs: list[Program] = []

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(LEAKY)}
        env.pop("OPENAI_API_KEY", None)
        return {
            **env,
            "PA_HOME": str(self.path),
            "HOME": str(self.path.parent),  # nothing may fall back to the real home
            "OPENAI_API_KEY": FAKE_KEY,
            "PYTHONUNBUFFERED": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }

    # ---- processes -------------------------------------------------------------------------
    def start(self) -> Program:
        program = Program(self)
        self.programs.append(program)
        self.wait_for(lambda: program.running, "the program to say it is running")
        return program

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Another `pa` command, as the owner would type it in a second terminal."""
        return subprocess.run(
            [sys.executable, "-m", "personal_agent", *args],
            env=self.env(),
            cwd=self.path,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def stop_everything(self) -> None:
        for program in self.programs:
            if program.process.poll() is None:
                program.process.kill()
                program.process.wait(timeout=10)

    # ---- the owner and the database ---------------------------------------------------------
    def say(self, text: str) -> None:
        db = open_database(self.path / "state.db")
        try:
            Inbox(db, SystemClock()).add(
                NewEvent(
                    source="cli",
                    kind=Kind.MESSAGE,
                    session_key="main",
                    payload={"text": text},
                )
            )
        finally:
            db.close()

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.path / "state.db", timeout=10)) as db:
            db.row_factory = sqlite3.Row
            return db.execute(sql, params).fetchall()

    def count(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        return int(self.query(sql, params)[0][0])

    def sent(self, kind: str = "message") -> int:
        return self.count("SELECT COUNT(*) FROM outbox WHERE status = 'sent' AND kind = ?", (kind,))

    def printed(self, text: str) -> int:
        """How many times `text` reached the owner's terminal, over every process."""
        return sum(program.output.count(text) for program in self.programs)

    def wait_for(self, condition: Callable[[], object], what: str, timeout: float = 40.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.1)
        shown = "\n".join(f"--- process {i}\n{p.output}" for i, p in enumerate(self.programs))
        raise AssertionError(f"timed out waiting for {what}\n{shown}")


@pytest.fixture
def fake() -> Iterator[FakeOpenAI]:
    with FakeOpenAI() as server:
        yield server


@pytest.fixture
def home(tmp_path: Path, fake: FakeOpenAI) -> Iterator[Home]:
    house = Home(tmp_path, fake, grace_s=10)
    yield house
    house.stop_everything()


@pytest.fixture
def impatient_home(tmp_path: Path, fake: FakeOpenAI) -> Iterator[Home]:
    """Gives work in flight one second to finish when asked to stop."""
    house = Home(tmp_path, fake, grace_s=1)
    yield house
    house.stop_everything()


def events(home: Home) -> dict[str, int]:
    return {
        r["status"]: r["n"] for r in home.query("SELECT status, COUNT(*) n FROM events GROUP BY 1")
    }


# ---- stopped while the model is thinking -------------------------------------------------------
def test_kill_9_while_the_model_is_thinking_and_the_restart_answers_once(home, fake):
    fake.gate.clear()  # the model takes its time
    first = home.start()
    home.say("hi there")
    fake.wait_for_requests(1)

    first.kill()  # no handler runs
    second = home.start()
    fake.gate.set()
    home.wait_for(lambda: home.sent() == 1, "the answer to be delivered")
    second.terminate()

    assert second.wait() == 0
    assert "recovered after an unclean stop" in second.output
    assert events(home) == {"done": 1}
    assert home.printed("Hello.") == 1
    assert len(fake.requests) == 2  # asked again, after the restart


def test_sigterm_lets_the_run_in_flight_finish_and_nothing_is_repeated(home, fake):
    fake.gate.clear()
    first = home.start()
    home.say("hi there")
    fake.wait_for_requests(1)

    first.terminate()  # "stopping: letting running work finish"
    fake.gate.set()  # the model answers during the grace period

    assert first.wait() == 0
    assert events(home) == {"done": 1}
    second = home.start()  # whatever was not delivered before the stop goes out now
    home.wait_for(lambda: home.sent() == 1, "the answer to be delivered")
    second.terminate()
    assert second.wait() == 0
    assert home.printed("Hello.") == 1
    assert len(fake.requests) == 1  # the run was not done again


def test_sigterm_with_a_model_that_never_answers_puts_the_message_back_untouched(
    impatient_home, fake
):
    home = impatient_home
    fake.gate.clear()
    first = home.start()
    home.say("hi there")
    fake.wait_for_requests(1)

    first.terminate()
    assert first.wait() == 0  # after the one second of grace, the run is given up

    [event] = home.query("SELECT status, attempts FROM events")
    assert (event["status"], event["attempts"]) == ("pending", 0)  # no attempt was used up
    second = home.start()
    fake.gate.set()
    home.wait_for(lambda: home.sent() == 1, "the answer to be delivered")
    second.terminate()
    assert second.wait() == 0
    assert events(home) == {"done": 1}
    assert home.printed("Hello.") == 1


# ---- stopped while waiting for the owner, or right after an action ----------------------------
def approve_the_pending_request(home: Home) -> None:
    home.wait_for(
        lambda: (
            home.count("SELECT COUNT(*) FROM approvals WHERE status = 'pending'") == 1
            and home.sent("approval_request") == 1
        ),
        "the request for approval to reach the owner",
    )
    [approval] = home.query("SELECT id FROM approvals WHERE status = 'pending'")
    result = home.run("approve", str(approval["id"]))
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_request_for_approval_survives_kill_9_and_the_action_runs_once(home, fake):
    first = home.start()
    home.say("please write a note")
    home.wait_for(lambda: home.sent("approval_request") == 1, "the request to reach the owner")
    assert not (home.path / "workspace" / "note.txt").exists()  # nothing happens without a yes

    first.kill()
    second = home.start()
    approve_the_pending_request(home)
    home.wait_for(lambda: home.sent() == 1, "the final answer to be delivered")
    second.terminate()

    assert second.wait() == 0
    assert (home.path / "workspace" / "note.txt").read_text() == "hello"
    assert (
        home.count("SELECT COUNT(*) FROM audit WHERE tool = 'write_file' AND outcome = 'ok'") == 1
    )
    assert home.printed("Done.") == 1
    assert home.printed("/approve") == 1  # the owner was asked once, not once per process


def test_kill_9_right_after_the_action_ran_does_not_run_it_again(home, fake):
    first = home.start()
    home.say("please write a note")
    approve_the_pending_request(home)
    # The tool runs, then the model is asked what to say about it: stop the program there.
    fake.gate.clear()
    fake.wait_for_requests(2)
    assert (home.path / "workspace" / "note.txt").read_text() == "hello"

    first.kill()
    second = home.start()
    fake.gate.set()
    home.wait_for(lambda: home.sent() == 1, "the final answer to be delivered")
    second.terminate()

    assert second.wait() == 0
    assert (
        home.count("SELECT COUNT(*) FROM audit WHERE tool = 'write_file' AND outcome = 'ok'") == 1
    )
    assert home.printed("Done.") == 1
    assert events(home) == {"done": 2}  # the owner's message and the approval
