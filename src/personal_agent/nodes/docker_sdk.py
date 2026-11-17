"""Docker's supported Python API behind the node's existing receipt protocol.

One worker owns the SDK client. Cancelling an await never releases that worker early or
turns an unknown remote result into a successful cancellation. The next pass must inspect
the same persisted resource. No method pulls an image or starts a stopped container.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

import docker
from docker.errors import APIError, NotFound
from docker.types import LogConfig, Mount, Ulimit

from personal_agent.nodes.backend import BackendError, DockerBackend, Observation
from personal_agent.nodes.store import Receipt
from personal_agent.nodes.transfers import MAX_LOG_BYTES

T = TypeVar("T")


class _Operation:
    """Close SDK log streams on cancellation; the receipt still requires reconciliation."""

    def __init__(self) -> None:
        self.stopped = threading.Event()
        self._lock = threading.Lock()
        self._stream: Any = None

    def check(self) -> None:
        if self.stopped.is_set():
            raise BackendError("Docker operation was interrupted; reconcile the same container")

    def stream(self, stream: Any) -> None:
        with self._lock:
            self._stream = stream
            stopped = self.stopped.is_set()
        if stopped:
            self.cancel()
            self.check()

    def clear(self) -> None:
        with self._lock:
            self._stream = None

    def cancel(self) -> None:
        self.stopped.set()
        with self._lock:
            stream = self._stream
        if stream is not None:
            # This is Docker's public streaming cancellation API. It shuts down the socket
            # even while the worker is blocked reading (the SDK removes streaming timeouts).
            with contextlib.suppress(Exception):
                stream.close()


class DockerSDKBackend(DockerBackend):
    """Structured Docker operations, with explicit CLI fallback selected by the owner."""

    def __init__(
        self,
        image: str = "python:3.12-slim",
        *,
        memory_mb: int = 512,
        cpus: float = 1,
        timeout_s: float = 20,
        client_factory: Callable[[], docker.DockerClient] | None = None,
    ) -> None:
        super().__init__(image, memory_mb=memory_mb, cpus=cpus)
        if not 0 < timeout_s <= 120:
            raise ValueError("invalid Docker API timeout")
        self.timeout_s = timeout_s
        self._client_factory = client_factory
        self._client: docker.DockerClient | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pa-docker")
        self._slot = asyncio.Semaphore(1)
        self._pending: set[asyncio.Future[Any]] = set()
        self._operation: _Operation | None = None
        self._closed = False

    def _connect(self) -> docker.DockerClient:
        if self._client is None:
            if self._client_factory is not None:
                client = self._client_factory()
            else:

                def configured(version: str) -> docker.DockerClient:
                    client = docker.from_env(
                        timeout=math.ceil(self.timeout_s),
                        version=version,
                        environment=self._env(),
                        use_context=True,
                        use_ssh_client=True,
                        max_pool_size=1,
                    )
                    client.api.trust_env = False
                    return client

                # version="auto" performs a network request inside the constructor, before
                # we can disable inherited proxy/netrc credentials. Construct without I/O,
                # negotiate through the public API, then build the real client explicitly.
                discovery = configured("1.44")
                try:
                    self._check_transport(discovery)
                    version = discovery.version(api_version=False).get("ApiVersion")
                    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", version):
                        raise BackendError("Docker returned an invalid API version")
                finally:
                    discovery.close()
                client = configured(version)
            client.api.trust_env = False
            self._client = client
        self._check_transport(self._client)
        return self._client

    @staticmethod
    def _check_transport(client: docker.DockerClient) -> None:
        if client.api.base_url.startswith(("ssh://", "http+docker://ssh")):
            # Docker's CancellableStream explicitly cannot interrupt SSH streams.
            # The bounded CLI backend supports the owner's SSH Docker context instead.
            raise BackendError("Use --backend cli for an SSH Docker context")

    def _require_open(self) -> None:
        if self._closed:
            raise BackendError("Docker backend is closed")

    async def _call(self, action: Callable[[docker.DockerClient, _Operation], T]) -> T:
        self._require_open()
        operation: _Operation | None = None
        try:
            async with asyncio.timeout(self.timeout_s + 1):
                await self._slot.acquire()
                try:
                    self._require_open()
                except BackendError:
                    self._slot.release()
                    raise
                operation = _Operation()
                self._operation = operation
                loop = asyncio.get_running_loop()

                def invoke() -> T:
                    assert operation is not None
                    operation.check()
                    client = self._connect()
                    operation.check()
                    return action(client, operation)

                try:
                    future = loop.run_in_executor(self._executor, invoke)
                except Exception:
                    self._operation = None
                    self._slot.release()
                    raise
                self._pending.add(future)

                def finished(completed: asyncio.Future[T]) -> None:
                    self._pending.discard(completed)
                    self._operation = None
                    self._slot.release()
                    # A cancelled caller must not leave unobserved SDK exceptions/logs.
                    if not completed.cancelled():
                        completed.exception()

                future.add_done_callback(finished)
                return await asyncio.shield(future)
        except asyncio.CancelledError:
            if operation is not None:
                operation.cancel()
            # Keep the slot until the worker actually exits: otherwise cancellation could
            # submit another start/create while the first request is still in flight.
            raise
        except TimeoutError:
            if operation is not None:
                operation.cancel()
            raise BackendError("Docker operation timed out; reconcile the same container") from None
        except BackendError:
            raise
        except Exception:
            # Docker errors may include commands, paths or remote credentials.
            raise BackendError("Docker operation failed; reconcile the same container") from None

    @staticmethod
    def _owned(client: docker.DockerClient, receipt: Receipt) -> tuple[str, Observation]:
        try:
            item = client.api.inspect_container(receipt.resource_id)
        except NotFound:
            return "", Observation("missing")
        try:
            labels = item["Config"].get("Labels") or {}
            if (
                labels.get("pa.attempt") != receipt.attempt_id
                or labels.get("pa.plan") != receipt.plan_hash
                or labels.get("pa.generation") != str(receipt.generation)
            ):
                raise BackendError("execution container ownership does not match its receipt")
            identifier = item["Id"]
            if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
                raise ValueError("invalid ID")
            state = item["State"]
            status = state["Status"]
            if status in {"exited", "dead"}:
                code = state["ExitCode"]
                if type(code) is not int:
                    raise ValueError("invalid exit status")
                return identifier, Observation("exited", code)
            return identifier, Observation(
                status if status in {"created", "running"} else "unknown"
            )
        except (TypeError, KeyError, ValueError, AttributeError):
            raise BackendError("Docker returned an invalid execution state") from None

    async def inspect(self, receipt: Receipt) -> Observation:
        return await self._call(lambda client, _operation: self._owned(client, receipt)[1])

    async def probe(self) -> None:
        def check(client: docker.DockerClient, operation: _Operation) -> None:
            client.ping()
            operation.check()
            try:
                client.images.get(self.image)
            except NotFound:
                raise BackendError(
                    "Preinstall the configured Docker image before starting this Runner"
                ) from None

        await self._call(check)

    async def create(self, receipt: Receipt, workspace: Path) -> None:
        def create(client: docker.DockerClient, operation: _Operation) -> None:
            if self._owned(client, receipt)[1].state != "missing":
                return
            if "," in str(workspace):
                raise BackendError("Docker bind mount path cannot contain a comma")
            if os.getuid() == 0:
                for directory, _, files in os.walk(workspace):
                    os.chown(directory, 65532, 65532)
                    for name in files:
                        os.chown(Path(directory) / name, 65532, 65532, follow_symlinks=False)
            uid, gid = os.getuid() or 65532, os.getgid() or 65532
            host = client.api.create_host_config(
                auto_remove=False,
                init=True,
                network_mode="none",
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                pids_limit=128,
                mem_limit=f"{self.memory_mb}m",
                memswap_limit=f"{self.memory_mb}m",
                nano_cpus=int(self.cpus * 1_000_000_000),
                ulimits=[
                    Ulimit(name="fsize", soft=67108864, hard=67108864),
                    Ulimit(name="nofile", soft=256, hard=256),
                ],
                log_config=LogConfig(type="local", config={"max-size": "5m", "max-file": "2"}),
                tmpfs={"/tmp": "rw,nosuid,size=128m"},  # noqa: S108
                mounts=[
                    Mount(
                        target="/workspace",
                        source=str(workspace.resolve()),
                        type="bind",
                        read_only=False,
                    )
                ],
            )
            # Share the fixed-deadline shell with the legacy backend. Container policy is
            # expressed through SDK fields, not parsed back from Docker command-line flags.
            command = self.create_argv(receipt, workspace)[-6:]
            operation.check()
            try:
                client.api.create_container(
                    image=self.image,
                    command=command,
                    name=receipt.resource_id,
                    labels={
                        "pa.attempt": receipt.attempt_id,
                        "pa.plan": receipt.plan_hash,
                        "pa.generation": str(receipt.generation),
                    },
                    user=f"{uid}:{gid}",
                    environment={"HOME": "/tmp", "LANG": "C.UTF-8"},  # noqa: S108
                    working_dir="/workspace",
                    host_config=host,
                    network_disabled=True,
                    use_config_proxy=False,
                )
            except APIError:
                # A conflict or lost creation response can still name our original container.
                # Never call containers.run(), which implicitly pulls missing images.
                operation.check()
                if self._owned(client, receipt)[1].state == "missing":
                    raise BackendError(
                        "Docker could not create the container; preinstall its image"
                    ) from None

        await self._call(create)

    async def start(self, receipt: Receipt) -> None:
        def start(client: docker.DockerClient, operation: _Operation) -> None:
            identifier, observed = self._owned(client, receipt)
            if observed.state == "created":
                operation.check()
                client.api.start(identifier)

        await self._call(start)

    async def stop(self, receipt: Receipt) -> None:
        def stop(client: docker.DockerClient, operation: _Operation) -> None:
            identifier, observed = self._owned(client, receipt)
            if observed.state in {"missing", "created", "exited"}:
                return
            if observed.state != "running":
                raise BackendError("execution cancellation could not be confirmed")
            operation.check()
            try:
                client.api.kill(identifier)
            except APIError:
                operation.check()
                if self._owned(client, receipt)[1].state != "exited":
                    raise BackendError("execution cancellation could not be confirmed") from None

        await self._call(stop)

    async def logs(self, receipt: Receipt) -> tuple[str, str]:
        def logs(client: docker.DockerClient, operation: _Operation) -> tuple[str, str]:
            identifier, observed = self._owned(client, receipt)
            if observed.state == "missing":
                raise BackendError("Docker could not confirm execution logs")
            result = []
            for stdout in (True, False):
                operation.check()
                stream = client.api.logs(
                    identifier,
                    stdout=stdout,
                    stderr=not stdout,
                    stream=True,
                    follow=False,
                    tail=1000,
                )
                operation.stream(stream)
                data = bytearray()
                try:
                    for chunk in stream:
                        operation.check()
                        data.extend(chunk[: max(0, MAX_LOG_BYTES - len(data))])
                        if len(data) >= MAX_LOG_BYTES:
                            break
                finally:
                    with contextlib.suppress(Exception):
                        stream.close()
                    operation.clear()
                operation.check()
                result.append(bytes(data).decode(errors="replace"))
            return result[0], result[1]

        return await self._call(logs)

    async def cleanup(self, receipt: Receipt) -> None:
        def cleanup(client: docker.DockerClient, operation: _Operation) -> None:
            identifier, observed = self._owned(client, receipt)
            if observed.state == "missing":
                return
            if observed.state not in {"created", "exited"}:
                raise BackendError("refusing to clean a running or uncertain execution")
            operation.check()
            try:
                client.api.remove_container(identifier, force=False)
            except APIError:
                operation.check()
                if self._owned(client, receipt)[1].state != "missing":
                    raise BackendError("execution cleanup is pending") from None

        await self._call(cleanup)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._operation is not None:
            self._operation.cancel()
        try:
            if self._pending:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.gather(*self._pending, return_exceptions=True)),
                    self.timeout_s + 2,
                )
            if self._client is not None:
                await asyncio.get_running_loop().run_in_executor(self._executor, self._client.close)
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)
