"""The SDK adapter preserves receipt ownership, isolation and uncertain outcomes."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import cast

import docker
import pytest
import requests
from docker.errors import APIError, NotFound
from docker.types import HostConfig

from personal_agent.nodes.backend import BackendError, Observation
from personal_agent.nodes.docker_sdk import DockerSDKBackend
from personal_agent.nodes.store import NodeStore
from personal_agent.nodes.transfers import MAX_LOG_BYTES


@pytest.fixture
def receipt(tmp_path):
    with NodeStore(tmp_path / "node") as store:
        return store.accept(
            {
                "attempt_id": "attempt_1",
                "job_id": "job_1",
                "task_id": "task_1",
                "space_id": "space_1",
                "generation": 3,
                "plan_hash": "a" * 64,
                "command": "true",
                "timeout_s": 60,
                "network": "none",
                "deadline_at": 1060.0,
                "input_manifest": [],
                "output_paths": [],
            },
            now=1000,
        )


class Stream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.chunks)

    def close(self):
        self.closed = True


class FakeDocker:
    """Only the SDK operations allowed by our adapter exist on this fake."""

    base_url = "http+docker://localhost"
    trust_env = True

    def __init__(self, receipt, state=None):
        self.api = self
        self.images = SimpleNamespace(get=self.inspect_image)
        self.receipt = receipt
        self.state = state
        self.labels = {
            "pa.attempt": receipt.attempt_id,
            "pa.plan": receipt.plan_hash,
            "pa.generation": str(receipt.generation),
        }
        self.calls = []
        self.streams = []
        self.closed = False

    def ping(self):
        self.calls.append(("ping",))

    def inspect_image(self, image):
        self.calls.append(("image", image))

    def inspect_container(self, resource):
        self.calls.append(("inspect", resource))
        if self.state is None:
            raise NotFound("missing")
        return {
            "Id": "b" * 64,
            "Config": {"Labels": self.labels},
            "State": {"Status": self.state, "ExitCode": 0},
        }

    def create_host_config(self, **kwargs):
        # Exercise the installed SDK's actual serialization without contacting Docker.
        return HostConfig(version="1.47", **kwargs)

    def create_container(self, **kwargs):
        self.calls.append(("create", kwargs))
        self.state = "created"
        return {"Id": "b" * 64}

    def start(self, identifier):
        self.calls.append(("start", identifier))
        self.state = "running"

    def kill(self, identifier):
        self.calls.append(("kill", identifier))
        self.state = "exited"

    def remove_container(self, identifier, **kwargs):
        self.calls.append(("remove", identifier, kwargs))
        self.state = None

    def logs(self, identifier, **kwargs):
        self.calls.append(("logs", identifier, kwargs))
        stream = Stream([b"a" * (MAX_LOG_BYTES + 10)])
        self.streams.append(stream)
        return stream

    def close(self):
        self.closed = True


def adapter(client, **kwargs):
    return DockerSDKBackend(client_factory=lambda: cast(docker.DockerClient, client), **kwargs)


async def test_sdk_preserves_isolation_deadline_and_creates_without_start_or_pull(
    receipt, tmp_path
):
    client = FakeDocker(receipt)
    backend = adapter(client, memory_mb=256, cpus=0.5)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        await backend.probe()
        await backend.create(receipt, workspace)
        await backend.create(receipt, workspace)
        created = [call[1] for call in client.calls if call[0] == "create"]
        assert len(created) == 1
        config = created[0]
        assert config["name"] == receipt.resource_id
        assert config["labels"] == client.labels
        assert config["user"] not in {"0", "0:0"}
        assert config["environment"] == {"HOME": "/tmp", "LANG": "C.UTF-8"}
        assert config["network_disabled"] and not config["use_config_proxy"]
        assert config["command"] == backend.create_argv(receipt, workspace)[-6:]
        assert config["command"][-2:] == ["1060", "true"]
        host = config["host_config"]
        assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"]
        assert host["CapDrop"] == ["ALL"]
        assert host["SecurityOpt"] == ["no-new-privileges"]
        assert host["Memory"] == host["MemorySwap"] == 256 * 1024 * 1024
        assert host["NanoCpus"] == 500_000_000
        assert host["PidsLimit"] == 128 and host["Init"]
        assert not host.get("AutoRemove", False) and not host.get("Privileged", False)
        assert host["LogConfig"] == {"Type": "local", "Config": {"max-size": "5m", "max-file": "2"}}
        assert host["Tmpfs"] == {"/tmp": "rw,nosuid,size=128m"}
        assert host["Ulimits"] == [
            {"Name": "fsize", "Soft": 67108864, "Hard": 67108864},
            {"Name": "nofile", "Soft": 256, "Hard": 256},
        ]
        assert host["Mounts"] == [
            {
                "Target": "/workspace",
                "Source": str(workspace.resolve()),
                "Type": "bind",
                "ReadOnly": False,
            }
        ]
        assert not any(call[0] in {"start", "pull"} for call in client.calls)
        assert not client.trust_env
    finally:
        await backend.aclose()
    assert client.closed


@pytest.mark.parametrize("label", ["pa.attempt", "pa.plan", "pa.generation"])
async def test_sdk_never_touches_a_resource_with_other_ownership(receipt, tmp_path, label):
    client = FakeDocker(receipt, "exited")
    client.labels[label] = "different"
    backend = adapter(client)
    try:
        for operation in (
            backend.inspect,
            backend.start,
            backend.stop,
            backend.logs,
            backend.cleanup,
            lambda r: backend.create(r, tmp_path),
        ):
            with pytest.raises(BackendError, match="ownership"):
                await operation(receipt)
        assert {call[0] for call in client.calls} == {"inspect"}
    finally:
        await backend.aclose()


async def test_sdk_uses_immutable_id_and_never_restarts_finished_work(receipt):
    client = FakeDocker(receipt, "created")
    backend = adapter(client)
    try:
        await backend.start(receipt)
        await backend.start(receipt)
        await backend.stop(receipt)
        await backend.start(receipt)
        assert await backend.inspect(receipt) == Observation("exited", 0)
        stdout, stderr = await backend.logs(receipt)
        assert len(stdout) == len(stderr) == MAX_LOG_BYTES
        assert all(stream.closed for stream in client.streams)
        await backend.cleanup(receipt)
        await backend.cleanup(receipt)
        operations = [call for call in client.calls if call[0] in {"start", "kill", "remove"}]
        assert [call[0] for call in operations] == ["start", "kill", "remove"]
        assert all(call[1] == "b" * 64 for call in operations)
        assert operations[-1][2] == {"force": False}
    finally:
        await backend.aclose()


async def test_sdk_lost_create_response_adopts_the_same_name(receipt, tmp_path):
    client = FakeDocker(receipt)
    original = client.create_container

    def lost(**kwargs):
        original(**kwargs)
        raise APIError("private response")

    client.create_container = lost
    backend = adapter(client)
    try:
        await backend.create(receipt, tmp_path)
        assert await backend.inspect(receipt) == Observation("created")
        assert len([call for call in client.calls if call[0] == "create"]) == 1
    finally:
        await backend.aclose()


async def test_sdk_cancelled_call_keeps_worker_slot_until_request_finishes(receipt):
    entered, release = threading.Event(), threading.Event()
    client = FakeDocker(receipt, "created")
    original = client.inspect_container

    def blocking(resource):
        entered.set()
        assert release.wait(3)
        return original(resource)

    client.inspect_container = blocking
    backend = adapter(client)
    first = asyncio.create_task(backend.start(receipt))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(backend.inspect(receipt))
        await asyncio.sleep(0.02)
        assert not client.calls and not second.done()
        release.set()
        assert await second == Observation("created")
        assert {call[0] for call in client.calls} == {"inspect"}
    finally:
        release.set()
        await backend.aclose()


@pytest.mark.parametrize("cancel", [True, False])
async def test_sdk_interrupted_close_still_closes_client_after_the_worker_finishes(receipt, cancel):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    client = FakeDocker(receipt, "running")
    original = client.inspect_container
    original_close = client.close

    def blocking(resource):
        entered.set()
        assert release.wait(5)
        return original(resource)

    def close():
        original_close()
        closed.set()

    client.inspect_container = blocking
    client.close = close
    backend = adapter(client, timeout_s=0.01)
    pending = asyncio.create_task(backend.inspect(receipt))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(backend.aclose())
        if cancel:
            await asyncio.sleep(0)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
        else:
            with pytest.raises(TimeoutError):
                await closing
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        assert await asyncio.to_thread(closed.wait, 1)
        await backend.aclose()
        assert client.closed
        with pytest.raises(BackendError, match="closed"):
            await backend.inspect(receipt)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await backend.aclose()


@pytest.mark.parametrize("cancel", [True, False])
async def test_sdk_interrupts_a_blocked_log_stream_without_killing_container(receipt, cancel):
    entered, closed = threading.Event(), threading.Event()
    client = FakeDocker(receipt, "running")

    class Blocked(Stream):
        def __next__(self):
            entered.set()
            assert closed.wait(3)
            raise StopIteration

        def close(self):
            super().close()
            closed.set()

    stream = Blocked([])
    client.logs = lambda *args, **kwargs: stream
    backend = adapter(client, timeout_s=0.01)
    pending = asyncio.create_task(backend.logs(receipt))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if cancel:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            with pytest.raises(BackendError, match="timed out"):
                await pending
        assert closed.is_set()
        assert await backend.inspect(receipt) == Observation("running")
        assert {call[0] for call in client.calls} == {"inspect"}
    finally:
        closed.set()
        await backend.aclose()


async def test_sdk_errors_do_not_expose_daemon_details_or_use_automatic_fallback(receipt):
    client = FakeDocker(receipt)
    client.inspect_container = lambda _: (_ for _ in ()).throw(APIError("secret@example.test"))
    backend = adapter(client)
    try:
        with pytest.raises(BackendError) as caught:
            await backend.inspect(receipt)
        assert "secret" not in str(caught.value)
        assert "reconcile" in str(caught.value)
    finally:
        await backend.aclose()


async def test_sdk_requires_explicit_cli_for_ssh_context(receipt):
    client = FakeDocker(receipt)
    client.base_url = "http+docker://ssh"
    backend = adapter(client)
    try:
        for _ in range(2):
            with pytest.raises(BackendError, match="--backend cli"):
                await backend.probe()
        assert not client.calls
    finally:
        await backend.aclose()


async def test_sdk_version_discovery_never_inherits_proxy_or_netrc_credentials(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
    for key in ("DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_CONTEXT", "DOCKER_CONFIG"):
        monkeypatch.delenv(key, raising=False)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(key, "http://proxy-user:proxy-secret@127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    calls = []

    def send(session, request, **kwargs):
        calls.append((request, kwargs))
        response = requests.Response()
        response.status_code = 200
        response.request = request
        response.url = request.url
        if request.url.endswith("/version"):
            response._content = json.dumps({"ApiVersion": "1.47"}).encode()
        elif request.url.endswith("/_ping"):
            response._content = b"OK"
        else:
            response._content = b"{}"
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    backend = DockerSDKBackend()
    try:
        await backend.probe()
        assert calls[0][0].url == "http://127.0.0.1:2375/version"
        assert len(calls) == 3
        assert all(not kwargs.get("proxies") for _, kwargs in calls)
        assert all("Authorization" not in request.headers for request, _ in calls)
    finally:
        await backend.aclose()
