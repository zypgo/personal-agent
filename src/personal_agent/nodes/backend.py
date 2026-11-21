"""Runs jobs in Docker.

Finished containers are kept, each with a fixed name, so a container can be checked to really
belong to its job.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from personal_agent.nodes.store import Receipt
from personal_agent.nodes.transfers import MAX_LOG_BYTES


class BackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class Observation:
    state: str
    exit_code: int | None = None


class Backend(Protocol):
    async def inspect(self, receipt: Receipt) -> Observation: ...
    async def create(self, receipt: Receipt, workspace: Path) -> None: ...
    async def start(self, receipt: Receipt) -> None: ...
    async def stop(self, receipt: Receipt) -> None: ...
    async def logs(self, receipt: Receipt) -> tuple[str, str]: ...
    async def cleanup(self, receipt: Receipt) -> None: ...


class DockerBackend:
    def __init__(
        self, image: str = "python:3.12-slim", *, memory_mb: int = 512, cpus: float = 1
    ) -> None:
        if not image or image.startswith("-") or len(image) > 256:
            raise ValueError("invalid Docker image")
        if not 128 <= memory_mb <= 4096 or not 0.25 <= cpus <= 4:
            raise ValueError("invalid node resource limits")
        self.image, self.memory_mb, self.cpus = image, memory_mb, cpus

    async def aclose(self) -> None:
        """The CLI backend keeps no persistent client or background worker."""

    @staticmethod
    def _env() -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/"),
        }
        for key in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        ):
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    async def _run(self, args: list[str], *, cap: int = 128 * 1024) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                *args,
                env=self._env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise BackendError("Docker is unavailable") from exc

        async def drain(stream: asyncio.StreamReader | None) -> bytes:
            assert stream is not None
            chunks: list[bytes] = []
            kept = 0
            while chunk := await stream.read(65536):
                take = chunk[: max(0, cap - kept)]
                if take:
                    chunks.append(take)
                    kept += len(take)
            return b"".join(chunks)

        out_task, err_task = (
            asyncio.create_task(drain(proc.stdout)),
            asyncio.create_task(drain(proc.stderr)),
        )
        try:
            async with asyncio.timeout(20):
                await proc.wait()
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            await asyncio.gather(out_task, err_task, return_exceptions=True)
            raise
        stdout, stderr = await asyncio.gather(out_task, err_task)
        return (
            proc.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    async def inspect(self, receipt: Receipt) -> Observation:
        code, stdout, stderr = await self._run(
            ["inspect", "--format", "{{json .}}", receipt.resource_id]
        )
        if code:
            if "No such object" in stderr or "No such container" in stderr:
                return Observation("missing")
            raise BackendError("Docker could not confirm execution state")
        try:
            item = json.loads(stdout)
            labels = item.get("Config", {}).get("Labels", {}) or {}
            if (
                labels.get("pa.attempt") != receipt.attempt_id
                or labels.get("pa.plan") != receipt.plan_hash
                or labels.get("pa.generation") != str(receipt.generation)
            ):
                raise BackendError("execution container ownership does not match its receipt")
            state = item["State"]
            status = state["Status"]
            if status in {"exited", "dead"}:
                return Observation("exited", int(state["ExitCode"]))
            if status == "created":
                return Observation("created")
            if status == "running":
                return Observation("running")
            return Observation("unknown")
        except (TypeError, KeyError, ValueError) as exc:
            raise BackendError("Docker returned an invalid execution state") from exc

    async def probe(self) -> None:
        code, _, _ = await self._run(["info", "--format", "{{.ServerVersion}}"])
        if code:
            raise BackendError("Docker daemon is not running")
        code, _, _ = await self._run(["image", "inspect", self.image])
        if code:
            raise BackendError("Preinstall the configured Docker image before starting this Runner")

    def create_argv(self, receipt: Receipt, workspace: Path) -> list[str]:
        uid, gid = os.getuid() or 65532, os.getgid() or 65532
        return [
            "create",
            "--pull=never",
            "--name",
            receipt.resource_id,
            "--label",
            f"pa.attempt={receipt.attempt_id}",
            "--label",
            f"pa.plan={receipt.plan_hash}",
            "--label",
            f"pa.generation={receipt.generation}",
            "--init",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=5m",
            "--log-opt",
            "max-file=2",
            "--memory",
            f"{self.memory_mb}m",
            "--memory-swap",
            f"{self.memory_mb}m",
            "--ulimit",
            "fsize=67108864:67108864",
            "--ulimit",
            "nofile=256:256",
            "--cpus",
            str(self.cpus),
            "--user",
            f"{uid}:{gid}",
            "--tmpfs",
            "/tmp:rw,nosuid,size=128m",  # noqa: S108
            "-e",
            "HOME=/tmp",
            "-e",
            "LANG=C.UTF-8",
            "--mount",
            f"type=bind,src={workspace.resolve()},dst=/workspace",
            # A deadline inside the container survives a killed/offline Runner process.
            "-w",
            "/workspace",
            self.image,
            "/bin/sh",
            "-c",
            # A created container may start after Runner recovery. Recompute the remaining
            # budget at start from the fixed receipt deadline, rather than resetting a timeout.
            'remaining=$(( $1 - $(date +%s) )); [ "$remaining" -gt 0 ] || exit 124; '
            'exec /usr/bin/timeout --signal=TERM --kill-after=5s "${remaining}s" '
            '/bin/sh -c "$2"',
            "pa-exec",
            str(int(receipt.deadline_at)),
            receipt.payload["command"],
        ]

    async def create(self, receipt: Receipt, workspace: Path) -> None:
        if (await self.inspect(receipt)).state != "missing":
            return
        if "," in str(workspace):
            raise BackendError("Docker bind mount path cannot contain a comma")
        if os.getuid() == 0:
            for directory, _, files in os.walk(workspace):
                os.chown(directory, 65532, 65532)
                for name in files:
                    os.chown(Path(directory) / name, 65532, 65532, follow_symlinks=False)
        code, _, _ = await self._run(self.create_argv(receipt, workspace))
        if code and (await self.inspect(receipt)).state == "missing":
            raise BackendError(
                "Docker could not create the execution container; pre-pull its image"
            )

    async def start(self, receipt: Receipt) -> None:
        # docker start on a stopped container would run it again. Only a proven never-started
        # container is eligible; exited/running is reconciliation, not a start request.
        observed = await self.inspect(receipt)
        if observed.state != "created":
            return
        code, _, _ = await self._run(["start", receipt.resource_id])
        if code:
            raise BackendError("Docker start result is unknown; reconcile the same container")

    async def stop(self, receipt: Receipt) -> None:
        observed = await self.inspect(receipt)
        if observed.state in {"missing", "created", "exited"}:
            return
        if observed.state != "running":
            raise BackendError("execution cancellation could not be confirmed")
        code, _, _ = await self._run(["kill", receipt.resource_id])
        if code and (await self.inspect(receipt)).state != "exited":
            raise BackendError("execution cancellation could not be confirmed")

    async def logs(self, receipt: Receipt) -> tuple[str, str]:
        await self.inspect(receipt)  # ownership check before reading
        _, stdout, stderr = await self._run(
            ["logs", "--tail", "1000", receipt.resource_id], cap=MAX_LOG_BYTES
        )
        return stdout, stderr

    async def cleanup(self, receipt: Receipt) -> None:
        observed = await self.inspect(receipt)
        if observed.state == "missing":
            return
        if observed.state not in {"exited", "created"}:
            raise BackendError("refusing to clean a running or uncertain execution")
        code, _, _ = await self._run(["rm", receipt.resource_id])
        if code and (await self.inspect(receipt)).state != "missing":
            raise BackendError("execution cleanup is pending")
