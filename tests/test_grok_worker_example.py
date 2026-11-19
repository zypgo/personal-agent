"""Grok's optional env CLI against the real worker API; no Grok login or cloud requests."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from personal_agent.agent_adapters import bridge
from test_agent_orchestration_http import accept, approve, create_task
from test_v4_agent_bridge import TOKEN, worker_server
from web_fake import Web

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "examples" / "grok" / "grok_worker.py"
SPEC = importlib.util.spec_from_file_location("grok_worker_example_test", EXAMPLE)
grok = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = grok
SPEC.loader.exec_module(grok)
grok.core = bridge


@pytest.fixture
def connected(make_app, tmp_path, monkeypatch):
    site = Web(make_app())
    site.pair()
    registration = site.client.post(
        "/api/v4/agents", headers=site.write, json={"name": "Grok fixture", "kind": "grok"}
    ).json()
    agent_id = registration["agent"]["id"]
    token = registration["worker_token"]
    monkeypatch.setenv("PA_GROK_AGENT_TOKEN", token)
    state = {"calls": [], "drop_result": False, "bad_reply": None}

    def http(self, method, action, payload=None, *, binary=False):
        state["calls"].append((method, action, copy.deepcopy(payload), type(self).__name__))
        response = site.client.request(
            method,
            f"/api/v4/agents/{self.agent_id}/worker/{action}?"
            + urlencode({"space": self.space_id}),
            headers={
                "Authorization": "Bearer " + self.credentials.token,
                "Origin": self.origin,
                "X-PA": "1",
            },
            json=payload,
        )
        if response.status_code != 200:
            raise bridge.RequestError(
                "Fixture worker authorization or state rejected.", status=response.status_code
            )
        if action.endswith("/result") and state["drop_result"]:
            state["drop_result"] = False
            raise bridge.RequestError("Synthetic lost result acknowledgement.")
        if state["bad_reply"] is not None and method == "POST" and action.startswith("runs/"):
            reply = response.json()
            state["bad_reply"](reply)
            return json.dumps(reply).encode()
        return response.content

    monkeypatch.setattr(bridge.WorkerClient, "_read_response", http)
    monkeypatch.setattr(bridge.SystemProxyWorkerClient, "_read_response", http)
    home = tmp_path / "grok-state"

    def args(*command):
        return grok.parser().parse_args(["--home", str(home), *command])

    configured = grok.execute(
        args(
            "setup",
            "--url",
            site.origin,
            "--agent",
            agent_id,
            "--space",
            "personal-owner",
            "--token-env",
            "PA_GROK_AGENT_TOKEN",
        )
    )
    assert configured["connected"] is False and not configured["token_stored"]
    assert token not in (home / "agent.json").read_text()
    try:
        yield SimpleNamespace(
            site=site,
            agent_id=agent_id,
            token=token,
            state=state,
            home=home,
            args=args,
            run=lambda *command: grok.execute(args(*command)),
        )
    finally:
        site.client.close()


@pytest.mark.parametrize("transport", ["direct", "system-proxy"])
def test_wakes_share_nonce_across_restart_and_retry_original_result(connected, tmp_path, transport):
    env = connected

    def run(*command):
        return env.run("--transport", transport, *command)

    assert run("claim")["idle"]
    saved_nonce = bridge._read_private(env.home / "grok-dispatch.json")["worker_id"]
    assert run("claim")["idle"]
    claims = [call for call in env.state["calls"] if call[1] == "claim"]
    assert claims[0][2] == claims[1][2] == {"worker_id": saved_nonce}
    task = create_task(env.site, [env.agent_id])
    approve(env.site, task)
    first = run("claim")
    assert first["claimed"] and first["execution"] == "native_grok"
    attempt_id = first["attempt_id"]
    assert run("claim")["busy"] and run("claim")["busy"]
    assert len([call for call in env.state["calls"] if call[1] == "claim"]) == 3
    assert env.token not in json.dumps(first) and "lease_token" not in first
    run("progress", attempt_id, "--text", "Preparing the approved result.")
    run("heartbeat", attempt_id)
    output = tmp_path / "result.md"
    original = b"Original immutable result.\n"
    output.write_bytes(original)
    env.state["drop_result"] = True
    with pytest.raises(bridge.RequestError):
        run("result", attempt_id, "--text", "Summary", "--file", str(output))
    pending = bridge._read_private(env.home / "attempts" / f"{attempt_id}.json")["pending"]
    output.write_bytes(b"Later modification must not change the saved request.\n")
    assert run("claim")["next"] == "status_then_retry"
    assert run("retry", attempt_id)["state"] == "result_reported"
    result_calls = [call for call in env.state["calls"] if call[1].endswith("/result")]
    assert result_calls[-1][2] == result_calls[-2][2] == pending["payload"]
    accepted = accept(env.site, task, "step0", {"attempt_id": attempt_id})
    artifact = accepted["steps"][0]["attempt"]["result"]["artifacts"][0]
    assert env.site.client.get(f"/api/v4/artifacts/{artifact['id']}").content == original
    assert run("claim")["idle"]
    assert bridge._read_private(env.home / "grok-dispatch.json")["worker_id"] != saved_nonce
    expected = "WorkerClient" if transport == "direct" else "SystemProxyWorkerClient"
    assert all(call[3] == expected for call in env.state["calls"])


def test_lost_accepted_result_after_generation_change_needs_owner_stop_proof(connected):
    env = connected
    task = create_task(env.site, [env.agent_id])
    approve(env.site, task)
    attempt_id = env.run("claim")["attempt_id"]
    env.state["drop_result"] = True
    with pytest.raises(bridge.RequestError):
        env.run("result", attempt_id, "--text", "Accepted candidate.")
    path = env.home / "attempts" / f"{attempt_id}.json"
    pending = bridge._read_private(path)["pending"]
    accepted = accept(env.site, task, "step0", {"attempt_id": attempt_id})
    assert (
        env.site.client.patch(
            f"/api/v4/agents/{env.agent_id}",
            headers=env.site.write,
            json={"name": "Grok updated", "kind": "grok"},
        ).status_code
        == 200
    )
    with pytest.raises(bridge.RequestError) as fenced:
        env.run("retry", attempt_id)
    assert fenced.value.status == 409
    with pytest.raises(bridge.BridgeError, match="owner stop"):
        env.run("reconcile")
    stopped = env.site.client.post(
        f"/api/v4/tasks/{task['id']}/stopped",
        headers=env.site.write,
        json={
            "step_id": "step0",
            "attempt_id": attempt_id,
            "note": "Native execution is confirmed ended.",
        },
    )
    assert stopped.status_code == 200
    assert stopped.json()["task"]["state"] == "succeeded"
    assert env.run("reconcile")["pending_preserved"]
    old = bridge._read_private(path)
    assert old["pending"] == pending and "result_receipt" not in old
    following = create_task(env.site, [env.agent_id], key="next")
    approve(env.site, following)
    assert env.run("claim")["task_id"] == following["id"]
    prior = env.site.client.get(f"/api/v4/tasks/{task['id']}").json()["task"]
    assert prior["state"] == "succeeded"
    assert prior["steps"][0]["attempt"]["result"] == accepted["steps"][0]["attempt"]["result"]


@pytest.mark.parametrize(
    "operation, mutation",
    [
        ("result", "object"),
        ("result", "generation"),
        ("result", "lease"),
        ("result", "attempt"),
        ("result", "outcome"),
        ("events", "sequence"),
        ("question", "missing"),
        ("uncertain", "state"),
    ],
)
def test_success_status_with_wrong_operation_reply_keeps_outbox(connected, operation, mutation):
    env = connected
    task = create_task(env.site, [env.agent_id])
    approve(env.site, task)
    attempt_id = env.run("claim")["attempt_id"]

    def corrupt(reply):
        if operation == "result":
            if mutation == "object":
                reply.clear()
                reply["error"] = "An unrelated JSON success body."
            elif mutation == "generation":
                reply["generation"] += 1
            elif mutation == "lease":
                reply["lease_token"] = "wrong-fixture-lease"
            elif mutation == "attempt":
                reply["attempt_id"] = "attempt_wrong"
            else:
                reply["result"]["outcome"] = "failed"
        elif operation == "events":
            reply["last_sequence"] = 99
        elif operation == "question":
            reply["questions"] = []
        else:
            reply.update(state="running", desired_state="run", desired_cancel=False)

    env.state["bad_reply"] = corrupt
    command = {
        "result": ("result", attempt_id, "--text", "Candidate"),
        "events": ("progress", attempt_id, "--text", "Preparing"),
        "question": ("ask", attempt_id, "--request-id", "question_one", "--text", "Continue?"),
        "uncertain": ("uncertain", attempt_id, "--text", "Native execution outcome is unknown."),
    }[operation]
    with pytest.raises(bridge.RequestError):
        env.run(*command)
    path = env.home / "attempts" / f"{attempt_id}.json"
    assert bridge._read_private(path)["pending"]["action"] == operation
    assert env.run("claim")["next"] == "status_then_retry"
    env.state["bad_reply"] = None
    assert env.run("retry", attempt_id)["recorded"]
    assert bridge._read_private(path)["pending"] is None


def test_question_redaction_is_a_valid_stable_id_acknowledgement(connected):
    env = connected
    task = create_task(env.site, [env.agent_id])
    approve(env.site, task)
    attempt_id = env.run("claim")["attempt_id"]
    result = env.run(
        "ask",
        attempt_id,
        "--request-id",
        "sensitive_question",
        "--text",
        "Verify " + env.token,
        "--choice",
        "Use " + env.token,
        "--choice",
        "Stop",
    )
    assert result["recorded"] and result["questions"][0]["id"] == "sensitive_question"
    assert env.token not in json.dumps(result)
    assert bridge._read_private(env.home / "attempts" / f"{attempt_id}.json")["pending"] is None


def test_probe_does_not_claim_and_rejects_placeholder_credentials(connected, monkeypatch, capsys):
    env = connected
    assert env.run("probe")["connected"]
    assert not [call for call in env.state["calls"] if call[1] == "claim"]
    placeholder = "opaque-vault-placeholder-" + "x" * 40
    monkeypatch.setenv("PA_GROK_AGENT_TOKEN", placeholder)
    assert grok.main(["--home", str(env.home), "probe"]) == 2
    output = capsys.readouterr()
    assert placeholder not in output.err + output.out
    assert not [call for call in env.state["calls"] if call[1] == "claim"]


def test_local_status_does_not_resolve_secret_or_touch_ledger(connected, monkeypatch):
    env = connected
    env.run("claim")
    before = {path.relative_to(env.home): path.read_bytes() for path in env.home.rglob("*.json")}

    def forbidden(*args, **kwargs):
        pytest.fail(
            "Offline status must not load credentials, environment secrets or HTTP clients."
        )

    monkeypatch.setattr(bridge, "load_credentials", forbidden)
    monkeypatch.setattr(bridge.WorkerClient, "__init__", forbidden)
    monkeypatch.setattr(bridge.SystemProxyWorkerClient, "__init__", forbidden)
    assert env.run("local-status") == {
        "initialized": True,
        "attempt_id": None,
        "delivered": False,
        "released": False,
    }
    assert {
        path.relative_to(env.home): path.read_bytes() for path in env.home.rglob("*.json")
    } == before


@pytest.mark.parametrize("value", ["pa4_" + "x" * 40, "sk_secret_value", "vault-placeholder-value"])
def test_setup_never_echoes_or_saves_secret_as_environment_name(tmp_path, value, capsys):
    home = tmp_path / "private-state"
    result = grok.main(
        [
            "--home",
            str(home),
            "setup",
            "--url",
            "https://control.example.test",
            "--agent",
            "agent_grok",
            "--space",
            "space_test",
            "--token-env",
            value,
        ]
    )
    assert result == 2 and not (home / "agent.json").exists()
    output = capsys.readouterr()
    assert value not in output.out + output.err


def test_probe_requires_integer_version_and_grok_provider(connected, monkeypatch):
    env = connected
    raw = bridge.SystemProxyWorkerClient.request

    def wrong_version(self, method, action, payload=None):
        reply = raw(self, method, action, payload)
        if action == "hello":
            reply["protocol_version"] = True
        return reply

    monkeypatch.setattr(bridge.SystemProxyWorkerClient, "request", wrong_version)
    with pytest.raises(bridge.RequestError):
        env.run("probe")
    monkeypatch.setattr(bridge.SystemProxyWorkerClient, "request", raw)
    assert (
        env.site.client.patch(
            f"/api/v4/agents/{env.agent_id}",
            headers=env.site.write,
            json={"name": "Muse fixture", "kind": "muse"},
        ).status_code
        == 200
    )
    with pytest.raises(bridge.RequestError, match="not registered as a Grok"):
        env.run("claim")
    assert not [call for call in env.state["calls"] if call[1] == "claim"]


@pytest.mark.parametrize("provider", ["grok", "muse"])
def test_updated_examples_run_with_only_verified_standalone_bridge(tmp_path, provider):
    exported = tmp_path / "pa-agent-bridge.py"
    raw = Path(bridge.__file__).read_bytes()
    exported.write_bytes(raw)
    script = tmp_path / "worker.py"
    source = EXAMPLE if provider == "grok" else ROOT / "examples/muse/muse_vault_worker.py"
    script.write_bytes(source.read_bytes())
    home = tmp_path / "worker-state"
    bridge.setup_credentials(
        home,
        url="https://control.example.test",
        agent_id="agent_test",
        space_id="space_test",
        token_env="PA_GROK_AGENT_TOKEN",
    )
    # This is the exact pre-extraction v1 idle ledger shape; its nonce must remain intact.
    ledger = home / ("grok-dispatch.json" if provider == "grok" else "muse-dispatch.json")
    bridge._write_private(
        ledger,
        {
            "version": 1,
            "binding": {
                "url": "https://control.example.test",
                "agent_id": "agent_test",
                "space_id": "space_test",
            },
            "worker_id": "worker_existing",
            "task_id": None,
            "attempt_id": None,
            "delivered": False,
        },
    )
    saved = ledger.read_bytes()
    command = [
        sys.executable,
        "-I",
        str(script),
        "--home",
        str(home),
        "--bridge",
        str(exported),
        "--bridge-sha256",
        hashlib.sha256(raw).hexdigest(),
    ]
    if provider == "muse":
        command += [
            "--url",
            "https://control.example.test",
            "--credential",
            "custom.worker",
            "--agent",
            "agent_test",
            "--space",
            "space_test",
        ]
    result = subprocess.run(
        [*command, "local-status"], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["initialized"]
    assert ledger.read_bytes() == saved


@pytest.mark.parametrize("status", [301, 307])
def test_system_proxy_transport_does_not_follow_redirects_or_echo_token(monkeypatch, status):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    with worker_server() as (url, server):
        server["error"] = status
        client = bridge.SystemProxyWorkerClient(
            bridge.Credentials(url, "agent_a", "space_a", TOKEN)
        )
        with pytest.raises(bridge.RequestError) as caught:
            client.request("GET", "status")
        assert caught.value.status == status and TOKEN not in str(caught.value)
        assert len(server["seen"]) == 1


def test_system_proxy_https_denial_never_falls_back_to_direct(monkeypatch):
    seen = []

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_CONNECT(self):
            seen.append((self.path, dict(self.headers)))
            self.send_response(403)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("https_proxy", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setattr(
        bridge.BoundedConnection, "connect", lambda _: pytest.fail("No direct fallback")
    )
    try:
        client = bridge.SystemProxyWorkerClient(
            bridge.Credentials("https://control.example.test", "agent_a", "space_a", TOKEN),
            timeout_s=2,
        )
        with pytest.raises(bridge.RequestError) as caught:
            client.request("GET", "status")
        assert TOKEN not in str(caught.value)
        assert len(seen) == 1 and seen[0][0] == "control.example.test:443"
        assert "Authorization" not in seen[0][1]  # Bearer is not exposed before the TLS tunnel.
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
