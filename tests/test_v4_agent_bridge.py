from __future__ import annotations

import base64
import hashlib
import json
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from typer.testing import CliRunner

from personal_agent.agent_adapters import bridge
from personal_agent.agent_adapters.bridge import (
    BridgeError,
    Credentials,
    RequestError,
    WorkerBridge,
    WorkerClient,
    load_credentials,
    setup_credentials,
)
from personal_agent.commands.agents import app as agents_cli

TOKEN = "worker-test-token-" + "x" * 40


@contextmanager
def worker_server():
    state = {
        "seen": [],
        "claims": {},
        "result": None,
        "drop_result": False,
        "cancel": False,
        "error": None,
        "artifact": None,
        "slow": False,
        "stopped": False,
        "kind": "codex",
        "requester_id": "owner",
        "token": TOKEN,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            parsed = urlsplit(self.path)
            action = parsed.path.split("/worker/", 1)[-1]
            body = json.loads(
                self.rfile.read(int(self.headers.get("content-length", "0"))) or b"{}"
            )
            state["seen"].append((self.command, self.path, dict(self.headers), body))
            assert self.headers["Authorization"] == f"Bearer {state['token']}"
            assert self.headers["Origin"] == f"http://127.0.0.1:{self.server.server_port}"
            assert self.headers["X-PA"] == "1"
            assert parse_qs(parsed.query) == {"space": ["space_a"]}
            if state["error"] is not None:
                self.send_response(state["error"])
                self.send_header("Location", "/leak")
                self.end_headers()
                self.wfile.write(TOKEN.encode())
                return
            if state["slow"]:
                self.send_response(200)
                self.end_headers()
                try:
                    for _ in range(40):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.01)
                except OSError:
                    pass
                return
            if "/artifacts/" in action:
                self.send_response(200)
                self.send_header("Content-Length", str(len(state["artifact"])))
                self.end_headers()
                self.wfile.write(state["artifact"])
                return
            if action == "hello":
                answer = {"agent_id": "agent_a", "connected": True}
            elif action == "status":
                answer = {"agent": {"id": "agent_a", "kind": state["kind"]}, "attempts": []}
            elif action == "claim":
                attempt = state["claims"].setdefault(
                    body["worker_id"],
                    {
                        "attempt_id": "attempt_a",
                        "task_id": "task_a",
                        "generation": 1,
                        "lease_token": "opaque-claim-identity",
                        "lease_expires_at": 9999999999,
                        "state": "running",
                        "desired_state": "run",
                        "payload": {
                            "prompt": "Write a small report",
                            "dependencies": [],
                            "requester_id": state["requester_id"],
                            "agent_owner_id": "owner",
                            "visibility": "private",
                            "shared_environment": False,
                        },
                    },
                )
                answer = {"attempt": attempt}
            elif action.endswith("/result"):
                if state["result"] is not None:
                    assert state["result"] == body
                state["result"] = body
                if state["drop_result"]:
                    state["drop_result"] = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                answer = {"attempt_id": "attempt_a", "state": "result_reported"}
            elif action.endswith("/status") or action.endswith("/stop-receipt"):
                answer = {
                    "attempt_id": "attempt_a",
                    "state": "result_reported" if state["result"] else "running",
                    "desired_state": "cancel" if state["cancel"] else "run",
                    "questions": [{"request_id": "question_a", "answer": "Proceed"}],
                }
                if state["stopped"]:
                    answer.update({"stopped_by": "owner", "stopped_at": 1234})
            else:
                answer = {
                    "attempt_id": "attempt_a",
                    "state": "running",
                    "desired_state": "cancel" if state["cancel"] else "run",
                }
            encoded = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = do_POST = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def configured(tmp_path, url):
    home = tmp_path / "worker"
    setup_credentials(home, url=url, agent_id="agent_a", space_id="space_a", token_value=TOKEN)
    return home


def test_actual_http_claim_file_report_and_cancel_observation_survive_restart(tmp_path):
    with worker_server() as (url, server):
        home = configured(tmp_path, url)
        first = WorkerBridge(home)
        assert first.hello()["connected"]
        nonce = first.begin()["worker_id"]
        attempt = first.claim(nonce)["attempt"]
        assert first.claim(nonce)["attempt"]["attempt_id"] == attempt["attempt_id"]
        resumed = WorkerBridge(home)
        resumed.progress("attempt_a", "Prepared the file")
        resumed.ask("attempt_a", "Continue?", ["Proceed", "Stop"], request_id="question_a")
        server["cancel"] = True
        assert resumed.heartbeat("attempt_a")["desired_state"] == "cancel"
        assert resumed.status("attempt_a")["questions"][0]["answer"] == "Proceed"
        result = tmp_path / "answer.txt"
        result.write_bytes(b"small, verified result")
        assert (
            resumed.report(
                "attempt_a", status="succeeded", text="Candidate answer", files=[result]
            )["state"]
            == "result_reported"
        )
        artifact = server["result"]["artifacts"][0]
        assert (
            hashlib.sha256(base64.b64decode(artifact["content_base64"])).hexdigest()
            == artifact["sha256"]
        )
        assert server["result"]["generation"] == 1
        assert server["result"]["lease_token"] == "opaque-claim-identity"
        assert all(path.stat().st_mode & 0o077 == 0 for path in home.rglob("*.json"))
        assert TOKEN not in repr(load_credentials(home))


def test_lost_result_ack_keeps_outbox_and_retries_only_identical_report(tmp_path):
    with worker_server() as (url, server):
        home = configured(tmp_path, url)
        worker = WorkerBridge(home)
        worker.claim(worker.begin()["worker_id"])
        server["drop_result"] = True
        with pytest.raises(RequestError):
            worker.report("attempt_a", status="succeeded", text="Result once")
        restored = WorkerBridge(home)
        assert restored.status("attempt_a")["state"] == "result_reported"
        with pytest.raises(BridgeError, match="unconfirmed"):
            restored.report("attempt_a", status="succeeded", text="Changed result")
        assert restored.retry("attempt_a")["state"] == "result_reported"
        reports = [entry[3] for entry in server["seen"] if "/result?" in entry[1]]
        assert reports[0] == reports[1]
        assert len(server["claims"]) == 1


@pytest.mark.parametrize("status", [301, 307, 401, 403, 409, 500])
def test_http_errors_do_not_echo_bearer_or_follow_redirects(tmp_path, status):
    with worker_server() as (url, server):
        client = WorkerClient(Credentials(url, "agent_a", "space_a", TOKEN))
        server["error"] = status
        with pytest.raises(RequestError) as caught:
            client.request("GET", "status")
        assert TOKEN not in str(caught.value)
        assert caught.value.status == status
        assert len(server["seen"]) == 1


def test_standalone_script_needs_no_package_and_never_echoes_stdin_token(tmp_path):
    standalone = tmp_path / "pa-agent-bridge.py"
    standalone.write_bytes(Path(bridge.__file__).read_bytes())
    with worker_server() as (url, server):
        home = tmp_path / "private-state"
        common = [sys.executable, "-I", str(standalone), "--home", str(home)]
        setup = subprocess.run(
            [
                *common,
                "setup",
                "--url",
                url,
                "--agent",
                "agent_a",
                "--space",
                "space_a",
                "--token-stdin",
            ],
            input=TOKEN + "\n",
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        assert setup.returncode == 0, setup.stderr
        check = subprocess.run(
            [*common, "check"], text=True, capture_output=True, check=False, timeout=5
        )
        assert check.returncode == 0, check.stderr
        assert json.loads(check.stdout)["connected"]
        assert TOKEN not in setup.stdout + setup.stderr + check.stdout + check.stderr
        assert len(server["seen"]) == 1


def test_typer_commands_share_the_standalone_contract(tmp_path):
    runner = CliRunner()
    with worker_server() as (url, _):
        home = tmp_path / "cli-state"
        result = runner.invoke(
            agents_cli,
            [
                "--home",
                str(home),
                "setup",
                "--url",
                url,
                "--agent",
                "agent_a",
                "--space",
                "space_a",
                "--token-stdin",
            ],
            input=TOKEN + "\n",
        )
        assert result.exit_code == 0, result.output
        nonce = json.loads(runner.invoke(agents_cli, ["--home", str(home), "begin"]).output)[
            "worker_id"
        ]
        claimed = runner.invoke(agents_cli, ["--home", str(home), "claim", "--worker-id", nonce])
        assert claimed.exit_code == 0, claimed.output
        assert "opaque-claim-identity" not in claimed.output
        assert TOKEN not in claimed.output
        assert "Write a small report" in claimed.output


def test_secret_environment_reference_and_private_state_validation(tmp_path, monkeypatch):
    home = tmp_path / "private"
    setup_credentials(
        home,
        url="https://control.example.test",
        agent_id="agent_a",
        space_id="space_a",
        token_env="MY_WORKER_AUTH",
    )
    assert TOKEN not in (home / "agent.json").read_text()
    monkeypatch.setenv("MY_WORKER_AUTH", TOKEN)
    assert load_credentials(home).token == TOKEN
    (home / "agent.json").chmod(0o644)
    with pytest.raises(BridgeError, match="0600"):
        load_credentials(home)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(BridgeError, match="Git"):
        setup_credentials(
            repo / "state",
            url="https://control.example.test",
            agent_id="agent_a",
            space_id="space_a",
            token_value=TOKEN,
        )


def test_input_hash_and_output_links_are_checked(tmp_path):
    with worker_server() as (url, _):
        home = configured(tmp_path, url)
        worker = WorkerBridge(home)
        worker.claim(worker.begin()["worker_id"])
        actual = tmp_path / "file.txt"
        actual.write_text("permitted")
        link = tmp_path / "link.txt"
        link.symlink_to(actual)
        with pytest.raises(OSError):
            worker.report("attempt_a", status="succeeded", text="answer", files=[link])
        path = home / "attempts" / "attempt_a.json"
        record = json.loads(path.read_text())
        record["attempt"]["payload"]["dependencies"] = [
            {"artifacts": [{"name": "input.txt", "content_base64": "aGVsbG8=", "sha256": "0" * 64}]}
        ]
        path.write_text(json.dumps(record))
        with pytest.raises(BridgeError, match="hash"):
            worker.files("attempt_a", tmp_path / "inputs")


def test_artifact_metadata_download_is_scoped_and_hash_checked(tmp_path):
    with worker_server() as (url, server):
        home = configured(tmp_path, url)
        worker = WorkerBridge(home)
        worker.claim(worker.begin()["worker_id"])
        server["artifact"] = b"dependency evidence"
        path = home / "attempts" / "attempt_a.json"
        record = json.loads(path.read_text())
        record["attempt"]["payload"]["dependencies"] = [
            {
                "artifacts": [
                    {
                        "id": "artifact_a",
                        "name": "evidence.txt",
                        "size": len(server["artifact"]),
                        "sha256": hashlib.sha256(server["artifact"]).hexdigest(),
                    }
                ]
            }
        ]
        path.write_text(json.dumps(record))
        files = worker.files("attempt_a", tmp_path / "inputs")
        assert Path(files["files"][0]["path"]).read_bytes() == server["artifact"]
        assert "/runs/attempt_a/artifacts/artifact_a?" in server["seen"][-1][1]


def test_slow_response_is_bounded_by_total_deadline(tmp_path):
    with worker_server() as (url, server):
        server["slow"] = True
        client = WorkerClient(Credentials(url, "agent_a", "space_a", TOKEN), timeout_s=0.08)
        started = time.monotonic()
        with pytest.raises(RequestError):
            client.request("GET", "status")
        assert time.monotonic() - started < 0.3


def test_dns_resolution_is_bounded_without_sending_credentials(monkeypatch):
    release = threading.Event()

    def slow_dns(*args, **kwargs):
        release.wait(2)
        raise OSError("test resolver unavailable")

    monkeypatch.setattr(socket, "getaddrinfo", slow_dns)
    client = WorkerClient(
        Credentials("http://127.0.0.1:9", "agent_a", "space_a", TOKEN), timeout_s=0.04
    )
    started = time.monotonic()
    try:
        with pytest.raises(RequestError):
            client.request("GET", "status")
        assert time.monotonic() - started < 0.3
    finally:
        release.set()
