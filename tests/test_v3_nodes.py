from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest

from personal_agent.clock import ManualClock
from personal_agent.db import open_database
from personal_agent.nodes.agent import Runner
from personal_agent.nodes.backend import BackendError, DockerBackend, Observation
from personal_agent.nodes.client import (
    AuthorizationError,
    Credentials,
    HttpTransport,
    TransportError,
    control_url,
    load_credentials,
    save_credentials,
)
from personal_agent.nodes.registry import DEFAULT_CAPABILITIES, NodeRegistry
from personal_agent.nodes.store import NodeStore
from personal_agent.nodes.transfers import collect_outputs, materialize, validate_envelope


def envelope(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "attempt_id": "attempt_123",
        "job_id": "job_123",
        "task_id": "task_123",
        "space_id": "personal-owner",
        "generation": 1,
        "plan_hash": "a" * 64,
        "command": "printf hello",
        "timeout_s": 60,
        "network": "none",
        "deadline_at": 1060.0,
        "input_manifest": [],
        "output_paths": [],
    }
    value.update(changes)
    return value


def file_entry(path: str, content: bytes = b"hello") -> dict[str, Any]:
    return {
        "path": path,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_base64": base64.b64encode(content).decode(),
    }


def test_node_pairing_is_hash_only_single_use_and_scoped(tmp_path):
    clock = ManualClock(1000)
    with closing(open_database(tmp_path / "control.db")) as db:
        registry = NodeRegistry(db, clock)
        invitation = registry.create_invite("owner", ["project_a"], "computer")
        registration = registry.redeem(invitation.code, [*DEFAULT_CAPABILITIES, "host_root"])
        assert registration
        assert registration.token not in repr(registration)
        assert invitation.code not in repr(invitation)
        assert registry.redeem(invitation.code, DEFAULT_CAPABILITIES) is None
        assert registration.token not in str(db.query("SELECT * FROM nodes"))
        assert invitation.code not in str(db.query("SELECT * FROM node_pairings"))
        node = registry.authenticate(registration.token)
        assert node and "host_root" not in node.effective_capabilities
        assert registry.authorize(node.id, "project_a")
        assert not registry.authorize(node.id, "project_b")
        assert not registry.revoke(node.id, "another_user")
        assert registry.revoke(node.id, "owner")
        assert registry.authenticate(registration.token) is None
        assert not registry.authorize(node.id, "project_a")


def test_node_invite_expiry_and_capability_intersection(tmp_path):
    clock = ManualClock(1000)
    with closing(open_database(tmp_path / "control.db")) as db:
        registry = NodeRegistry(db, clock)
        code = registry.create_invite("owner", ["personal-owner"], ttl_s=30).code
        clock.advance(31)
        assert registry.redeem(code, DEFAULT_CAPABILITIES) is None
        code = registry.create_invite("owner", ["personal-owner"]).code
        registration = registry.redeem(code, ["network_none"])
        assert registration and registration.node.effective_capabilities == ("network_none",)
        assert registry.heartbeat(registration.node.id, list(DEFAULT_CAPABILITIES))
        with pytest.raises(PermissionError):
            registry.set_grants(registration.node.id, "other", ["other"], DEFAULT_CAPABILITIES)
        registry.set_grants(registration.node.id, "owner", ["other"], ["network_none"])
        assert not registry.authorize(registration.node.id, "personal-owner")


@pytest.mark.parametrize("path", ["../escape", "/escape", "a/../b", "a//b", "./a", "a\\b", "a/"])
def test_input_paths_cannot_escape_or_alias(path, tmp_path):
    with pytest.raises(ValueError):
        materialize(tmp_path / "attempt", envelope(input_manifest=[file_entry(path)]))
    assert not (tmp_path / "escape").exists()


def test_manifest_hash_size_and_duplicate_validation(tmp_path):
    bad = file_entry("file.txt")
    bad["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="manifest"):
        materialize(tmp_path / "attempt", envelope(input_manifest=[bad]))
    with pytest.raises(ValueError, match="duplicate"):
        validate_envelope(envelope(input_manifest=[file_entry("a"), file_entry("a")]))
    with pytest.raises(ValueError, match="network"):
        validate_envelope(envelope(network="internet"))


def test_workspace_snapshot_not_rewritten_on_recovery_and_outputs_reject_links(tmp_path):
    payload = envelope(input_manifest=[file_entry("a.txt")])
    workspace = materialize(tmp_path / "attempt", payload)
    (workspace / "a.txt").write_text("result")
    assert materialize(tmp_path / "attempt", payload) == workspace
    assert (workspace / "a.txt").read_text() == "result"
    (tmp_path / "private.txt").write_text("private")
    (workspace / "link").symlink_to(tmp_path / "private.txt")
    with pytest.raises(OSError):
        collect_outputs(workspace, ["link"])
    files = collect_outputs(workspace, ["a.txt"])
    assert base64.b64decode(files[0]["content_base64"]) == b"result"
    os.link(workspace / "a.txt", workspace / "hard-link")
    with pytest.raises(ValueError, match="regular"):
        collect_outputs(workspace, ["hard-link"])


def test_local_receipt_survives_restart_and_rejects_changed_plan(tmp_path):
    payload = envelope()
    with NodeStore(tmp_path) as store:
        receipt = store.accept(payload, now=1000)
        store.update(receipt.attempt_id, state="running", admitted=True)
    with NodeStore(tmp_path) as store:
        receipt = store.accept(payload, now=1050)
        assert receipt.state == "running" and receipt.deadline_at == 1060
        with pytest.raises(ValueError, match="another plan"):
            store.accept(envelope(command="different"), now=1050)
        with pytest.raises(ValueError, match="another plan"):
            store.accept(envelope(generation=2), now=1050)


class FakeBackend:
    def __init__(self, *, finish: bool = True):
        self.state = "missing"
        self.finish = finish
        self.creates = self.starts = self.stops = self.cleanups = 0
        self.cleanup_fails = False
        self.stdout = "finished"
        self.workspace: Path | None = None

    async def inspect(self, receipt):
        return Observation(self.state, 0 if self.state == "exited" else None)

    async def create(self, receipt, workspace):
        assert self.state == "missing"
        self.creates += 1
        self.workspace = workspace
        self.state = "created"

    async def start(self, receipt):
        assert self.state == "created"
        self.starts += 1
        self.state = "exited" if self.finish else "running"

    async def stop(self, receipt):
        self.stops += 1
        if self.state == "running":
            self.state = "exited"

    async def logs(self, receipt):
        return self.stdout, ""

    async def cleanup(self, receipt):
        if self.cleanup_fails:
            raise BackendError("cleanup failed")
        self.cleanups += 1
        self.state = "missing"


class FakeTransport:
    def __init__(self, payload=None):
        self.payload = payload or envelope()
        self.completed: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []
        self.lost_completion = False
        self.lost_admission = False
        self.lost_observation = False
        self.offline = False
        self.revoked = False
        self.cancel = False
        self.admissions = 0

    async def request(self, method, path, payload=None):
        if self.revoked:
            raise AuthorizationError("revoked")
        if self.offline:
            raise TransportError("offline")
        if path.endswith("/claim"):
            return {"attempt": self.payload if not self.completed else None}
        if path.endswith("/status"):
            return {"authorized": True, "desired_state": "cancel" if self.cancel else "run"}
        if path.endswith("/admit"):
            self.admissions += 1
            if self.lost_admission:
                self.lost_admission = False
                raise TransportError("admission reply lost")
            return {"accepted": True}
        if path.endswith("/observe"):
            self.observations.append(payload)
            if self.lost_observation:
                self.lost_observation = False
                raise TransportError("observation reply lost")
            return {"accepted": True}
        if path.endswith("/complete"):
            self.completed.append(payload)
            if self.lost_completion:
                self.lost_completion = False
                raise TransportError("completion reply lost")
            return {"accepted": True}
        if path.endswith("/cleanup"):
            return {"accepted": True}
        raise AssertionError(path)


async def test_lost_completion_reply_retries_result_not_execution(tmp_path):
    backend, transport = FakeBackend(), FakeTransport()
    transport.lost_completion = True
    with NodeStore(tmp_path) as store:
        worker = Runner(store, backend, transport, clock=lambda: 1000, min_free_bytes=0)
        with pytest.raises(TransportError):
            await worker.tick()
        assert store.get("attempt_123").result
        assert backend.creates == backend.starts == 1 and backend.cleanups == 0
    with NodeStore(tmp_path) as store:
        worker = Runner(store, backend, transport, clock=lambda: 1001, min_free_bytes=0)
        await worker.tick()
        assert len(transport.completed) == 2
        assert transport.completed[0] == transport.completed[1]
        assert backend.creates == backend.starts == 1
        assert not store.pending()
        assert "command" not in store.get("attempt_123").payload


async def test_lost_admission_reply_does_not_start_until_confirmed(tmp_path):
    backend, transport = FakeBackend(), FakeTransport()
    transport.lost_admission = True
    with NodeStore(tmp_path) as store:
        worker = Runner(store, backend, transport, clock=lambda: 1000, min_free_bytes=0)
        with pytest.raises(TransportError):
            await worker.tick()
        assert backend.starts == backend.creates == 0
        await worker.tick()
        assert transport.admissions == 2
        assert backend.starts == 1


async def test_restarted_runner_never_recreates_missing_started_container(tmp_path):
    backend, transport = FakeBackend(), FakeTransport()
    with NodeStore(tmp_path) as store:
        receipt = store.accept(envelope(), now=1000)
        store.update(receipt.attempt_id, state="running", admitted=True)
        worker = Runner(store, backend, transport, clock=lambda: 1001, min_free_bytes=0)
        await worker.tick()
        assert backend.creates == backend.starts == 0
        assert store.get(receipt.attempt_id).state == "uncertain"
        assert transport.observations[-1]["state"] == "uncertain"


@pytest.mark.parametrize("state", ["starting", "running", "uncertain"])
async def test_restarted_runner_never_repeats_an_ambiguous_start(tmp_path, state):
    backend, transport = FakeBackend(), FakeTransport()
    backend.state = "created"
    with NodeStore(tmp_path) as store:
        receipt = store.accept(envelope(), now=1000)
        store.update(receipt.attempt_id, state=state, admitted=True)
    with NodeStore(tmp_path) as store:
        worker = Runner(store, backend, transport, clock=lambda: 1001, min_free_bytes=0)
        await worker.tick()
        await worker.tick()
        pending = store.get(receipt.attempt_id)
        assert pending.state == "uncertain" and pending.result is None
        assert len(store.pending()) == 1
        assert backend.starts == backend.creates == 0
        assert not transport.completed
        assert transport.observations[-1]["state"] == "uncertain"


async def test_lost_start_reply_can_be_reconciled_but_never_replayed(tmp_path):
    class LateStart(FakeBackend):
        async def start(self, receipt):
            self.starts += 1
            raise BackendError("start response lost; daemon outcome unknown")

    backend, transport = LateStart(finish=False), FakeTransport()
    with NodeStore(tmp_path) as store:
        worker = Runner(store, backend, transport, clock=lambda: 1000, min_free_bytes=0)
        with pytest.raises(BackendError):
            await worker.tick()
        assert store.get("attempt_123").state == "starting"
        await worker.tick()
        assert backend.starts == 1 and not transport.completed
        assert store.get("attempt_123").state == "uncertain"
        # The delayed original start is discovered; it is not submitted a second time.
        backend.state = "running"
        await worker.tick()
        assert backend.starts == 1 and store.get("attempt_123").state == "running"


async def test_cancel_ambiguous_created_resource_requires_removal_before_success(tmp_path):
    backend, transport = FakeBackend(), FakeTransport()
    backend.state, backend.cleanup_fails, transport.cancel = "created", True, True
    with NodeStore(tmp_path) as store:
        receipt = store.accept(envelope(), now=1000)
        store.update(receipt.attempt_id, state="starting", admitted=True)
        worker = Runner(store, backend, transport, clock=lambda: 1001, min_free_bytes=0)
        await worker.tick()
        assert not transport.completed
        assert store.get(receipt.attempt_id).state == "uncertain"
        backend.cleanup_fails = False
        await worker.tick()
        assert transport.completed[-1]["status"] == "cancelled"
        assert backend.state == "missing" and backend.starts == 0
        assert not store.pending()


async def test_cancellation_and_offline_deadline_stop_running_container(tmp_path):
    backend, transport = FakeBackend(finish=False), FakeTransport()
    now = [1000.0]
    with NodeStore(tmp_path) as store:
        worker = Runner(store, backend, transport, clock=lambda: now[0], min_free_bytes=0)
        await worker.tick()
        assert backend.state == "running"
        transport.offline, now[0] = True, 1061
        await worker.tick()
        assert backend.state == "exited" and backend.stops == 1
        assert store.get("attempt_123").desired_cancel
        transport.offline = False
        await worker.tick()
        assert transport.completed[-1]["status"] == "cancelled"


async def test_revoked_node_stops_without_cleaning_unacknowledged_result(tmp_path):
    backend, transport = FakeBackend(finish=False), FakeTransport()
    with NodeStore(tmp_path) as store:
        worker = Runner(store, backend, transport, clock=lambda: 1000, min_free_bytes=0)
        await worker.tick()
        transport.revoked = True
        with pytest.raises(AuthorizationError):
            await worker.tick()
        assert backend.state == "exited"
        assert store.pending() and not store.pending()[0].acknowledged
        assert not backend.cleanups


async def test_failed_cleanup_is_retained_and_retried_without_execution(tmp_path):
    backend, transport = FakeBackend(), FakeTransport()
    backend.cleanup_fails = True
    with NodeStore(tmp_path) as store:
        worker = Runner(store, backend, transport, clock=lambda: 1000, min_free_bytes=0)
        with pytest.raises(BackendError):
            await worker.tick()
        assert store.get("attempt_123").acknowledged
        assert store.get("attempt_123").cleanup_state == "cleanup_pending"
        backend.cleanup_fails = False
        await worker.tick()
        assert backend.starts == 1 and not store.pending()


async def test_observation_sequence_persists_before_reply_and_heartbeat_stays_active(tmp_path):
    backend, transport = FakeBackend(finish=False), FakeTransport()
    transport.lost_observation = True
    with NodeStore(tmp_path) as store:
        worker = Runner(store, backend, transport, clock=lambda: 1000, min_free_bytes=0)
        with pytest.raises(TransportError):
            await worker.tick()
        backend.stdout = "new output"
        await worker.tick()
        assert transport.observations[-1]["sequence"] == 2
        await worker.tick()
        assert transport.observations[-1]["state"] == "running"
        assert "sequence" not in transport.observations[-1]


def test_docker_argv_is_bounded_no_network_and_deadline_survives_runner(tmp_path):
    with NodeStore(tmp_path) as store:
        receipt = store.accept(envelope(), now=1000)
        backend = DockerBackend()
        args = backend.create_argv(receipt, tmp_path / "workspace")
        assert "--rm" not in args and "--pull=never" in args
        assert args[args.index("--network") + 1] == "none"
        assert "--read-only" in args and "no-new-privileges" in args
        assert args[args.index("--log-driver") + 1] == "local"
        assert "max-size=5m" in args and "max-file=2" in args
        assert any("/usr/bin/timeout" in arg and "--kill-after=5s" in arg for arg in args)
        assert args[-2] == "1060"  # fixed absolute deadline; recovery does not reset it
        assert any("date +%s" in arg for arg in args)
        assert "--user" in args and args[args.index("--user") + 1] != "0:0"
        assert not any("docker.sock" in arg for arg in args)
        assert sum(arg == "--mount" for arg in args) == 1


def test_container_deadline_script_refuses_an_expired_start(tmp_path):
    if not Path("/usr/bin/timeout").exists():
        pytest.skip("GNU timeout is not installed on this test host")
    with NodeStore(tmp_path) as store:
        receipt = store.accept(envelope(command="printf should-not-run"), now=1000)
        args = DockerBackend().create_argv(receipt, tmp_path / "workspace")
        script = args[args.index("python:3.12-slim") + 1 :]
        stopped = subprocess.run(
            script, capture_output=True, timeout=2, check=False, env={"PATH": "/usr/bin:/bin"}
        )
        assert stopped.returncode == 124 and not stopped.stdout
        script[-2], script[-1] = str(int(time.time() + 10)), "printf 'literal $text'"
        ran = subprocess.run(
            script, capture_output=True, timeout=2, check=False, env={"PATH": "/usr/bin:/bin"}
        )
        assert ran.returncode == 0 and ran.stdout == b"literal $text"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test",
        "https://user:secret@example.test",
        "https://example.test/?token=secret",
        "https://example.test/#secret",
        "https://example.test/path",
        "file:///tmp/control",
    ],
)
def test_node_url_rejects_plaintext_and_credential_routing(url):
    with pytest.raises(ValueError):
        control_url(url)


def test_pairing_cannot_replace_an_existing_identity(tmp_path):
    credentials = Credentials("https://control.test", "node_a", "a" * 64)
    save_credentials(tmp_path, credentials)
    with pytest.raises(FileExistsError):
        save_credentials(tmp_path, Credentials("https://other.test", "node_b", "b" * 64))
    assert load_credentials(tmp_path) == credentials


async def test_actual_http_protocol_runs_full_quota_snapshot_and_persists_cleanup(
    tmp_path, make_app
):
    from web_fake import Web

    web = Web(make_app())
    try:
        web.pair()
        names = [f"input-{number}.txt" for number in range(4)]
        content = b"x" * (2 * 1024 * 1024)
        for name in names:
            (web.app.paths.workspace_dir / name).write_bytes(content)
        created = web.client.post(
            "/api/v3/tasks",
            headers=web.write,
            json={
                "client_request_id": "node-http-roundtrip",
                "title": "Snapshot roundtrip",
                "command": "printf checked",
                "input_paths": names,
                "output_paths": names,
            },
        )
        assert created.status_code == 201, created.text
        job = created.json()["task"]["jobs"][0]
        approved = web.client.post(
            f"/api/v3/jobs/{job['id']}/approve",
            headers=web.write,
            json={"allowed": True, "plan_hash": job["plan_hash"]},
        )
        assert approved.status_code == 200, approved.text
        invitation = web.client.post(
            "/api/v3/node-invites",
            headers=web.write,
            json={"space_ids": ["personal-owner"]},
        )
        assert invitation.status_code == 201, invitation.text
        asgi = httpx.ASGITransport(app=web.client.app)
        async with HttpTransport(web.origin, transport=asgi) as unpaired:
            registration = await unpaired.request(
                "POST",
                "/api/v3/nodes/pair",
                {"code": invitation.json()["code"], "capabilities": list(DEFAULT_CAPABILITIES)},
            )
        async with HttpTransport(web.origin, registration["token"], transport=asgi) as client:
            backend = FakeBackend(finish=False)
            with NodeStore(tmp_path / "runner") as store:
                worker = Runner(store, backend, client, clock=web.app.clock.now, min_free_bytes=0)
                await worker.tick()
                receipt = store.pending()[0]
                assert receipt.state == "running" and backend.starts == 1
                assert backend.workspace is not None
                assert all((backend.workspace / name).read_bytes() == content for name in names)
                backend.state = "exited"
                await worker.tick()
                assert not store.pending() and backend.cleanups == 1
                completed = web.app.jobs.get_attempt(receipt.attempt_id)
                assert completed["state"] == "succeeded"
                assert completed["cleanup_state"] == "released"
                assert len(completed["result"]["artifacts"]) == 4
                assert web.app.db.scalar("SELECT COUNT(*) FROM execution_inputs") == 0
                assert (
                    web.app.db.scalar("SELECT COUNT(*) FROM events WHERE source='execution'") == 1
                )
    finally:
        web.client.close()


async def test_http_transport_keeps_secret_at_one_origin_and_no_redirect():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://unrelated.test/"})

    async with HttpTransport(
        "https://control.test",
        "private-node-token",
        transport=httpx.MockTransport(respond),
    ) as client:
        with pytest.raises(TransportError, match="302"):
            await client.request("POST", "/api/v3/nodes/claim", {})
    assert len(requests) == 1
    assert requests[0].headers["Origin"] == "https://control.test"
    assert requests[0].headers["X-PA"] == "1"
    assert requests[0].headers["Authorization"] == "Bearer private-node-token"


def test_credentials_private_and_status_representation_has_no_token(tmp_path):
    credentials = Credentials("https://control.test", "node_123", "x" * 64)
    save_credentials(tmp_path, credentials)
    assert load_credentials(tmp_path) == credentials
    assert (tmp_path / "node.json").stat().st_mode & 0o777 == 0o600
    assert credentials.token not in repr(credentials)
    assert json.loads((tmp_path / "node.json").read_text())["node_id"] == "node_123"
