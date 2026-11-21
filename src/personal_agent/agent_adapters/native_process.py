"""macOS/Linux native-process ownership, descendant tracking and bounded cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import psutil

from personal_agent.agent_adapters.bridge import BridgeError
from personal_agent.agent_adapters.host_protocol import NativeCallbacks, NativeRequest


def process_identity(pid: int) -> dict[str, Any] | None:
    """Creation time + boot identity prevent signalling a recycled PID."""
    try:
        process = psutil.Process(pid)
        status = process.status()
        pgid: int | None
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            # macOS refuses to name the group of a process that has exited but not yet been
            # collected by its parent. It is still a known process, and it has stopped: leaving
            # it unnamed made every such descendant look untraceable, so a clean exit of the
            # Codex turn could never be confirmed.
            if status != psutil.STATUS_ZOMBIE:
                raise
            pgid = None
        result: dict[str, Any] = {
            "pid": pid,
            "create_time": process.create_time(),
            "boot_time": psutil.boot_time(),
            "pgid": pgid,
            "status": status,
        }
        if sys.platform == "linux":
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
            result["start_ticks"] = int(fields[19])
            result["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return result
    except (psutil.Error, OSError, ValueError, IndexError):
        return None


def process_matches(record: Mapping[str, Any]) -> bool:
    pid = record.get("pid")
    if type(pid) is not int or pid < 1:
        return False
    current = process_identity(pid)
    if current is None or current["status"] == psutil.STATUS_ZOMBIE:
        return False
    keys: tuple[str, ...] = ("pid", "create_time", "boot_time")
    if "start_ticks" in record:
        keys += ("start_ticks", "boot_id")
    # pgid can change when an owned child calls setsid; it is not process identity.
    return all(current.get(key) == record.get(key) for key in keys)


def process_resolved(record: Mapping[str, Any]) -> bool:
    """Absence/reuse is evidence of stopping; permission failure is not."""
    pid = record.get("pid")
    if type(pid) is not int or pid < 1:
        return False
    current = process_identity(pid)
    if current is None:
        return not psutil.pid_exists(pid)
    return current["status"] == psutil.STATUS_ZOMBIE or not process_matches(record)


def _group_members(group: int) -> list[dict[str, Any]]:
    members = []
    for process in psutil.process_iter(["pid"]):
        try:
            if os.getpgid(process.pid) == group:
                identity = process_identity(process.pid)
                if identity and identity["status"] != psutil.STATUS_ZOMBIE:
                    members.append(identity)
        except (psutil.Error, OSError):
            continue
    return members


def _signal_owned(records: Sequence[Mapping[str, Any]], sig: signal.Signals) -> None:
    owned_pids = {record["pid"] for record in records if process_matches(record)}
    signalled: set[int] = set()
    for record in records:
        if record["pid"] not in owned_pids or not process_matches(record):
            continue
        try:
            group = os.getpgid(record["pid"])
            # Never signal an unrelated host's process group.
            if group in owned_pids:
                if group not in signalled:
                    os.killpg(group, sig)
                    signalled.add(group)
            else:
                os.kill(record["pid"], sig)
        except ProcessLookupError:
            continue


async def stop_recorded(records: Sequence[Mapping[str, Any]], *, grace_s: float = 2) -> bool:
    """Explicit recovery: terminate only saved identities whose birth time still matches."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        await asyncio.to_thread(_signal_owned, records, sig)
        deadline = asyncio.get_running_loop().time() + grace_s
        while asyncio.get_running_loop().time() < deadline:
            if all(process_resolved(record) for record in records):
                return True
            await asyncio.sleep(0.05)
    return all(process_resolved(record) for record in records)


class NativeProcess:
    def __init__(self, process: asyncio.subprocess.Process, callbacks: NativeCallbacks) -> None:
        self.process, self.callbacks = process, callbacks
        self.identity = process_identity(process.pid)
        self.descendants: dict[tuple[int, float], dict[str, Any]] = {}
        self.tracking_ok = True
        self.capture_lock = threading.Lock()
        self.forced_kill = False
        self.closed = False
        self.stderr_task = asyncio.create_task(self._discard_stderr())
        self.tracker: asyncio.Task[None] | None = None

    @classmethod
    async def start(
        cls,
        command: Sequence[str],
        request: NativeRequest,
        callbacks: NativeCallbacks,
        *,
        env: Mapping[str, str] | None = None,
    ) -> NativeProcess:
        if sys.platform not in {"linux", "darwin"}:
            raise BridgeError("Agent Host currently supports macOS and Linux.")
        environment = dict(os.environ if env is None else env)
        for name in {"PA_AGENT_TOKEN", request.worker_secret_env}:
            if name:
                environment.pop(name, None)
        await callbacks.state({"phase": "launching"})
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=request.cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=1_048_576,
        )
        instance = cls(process, callbacks)
        try:
            if instance.identity is None or instance.identity["pgid"] != process.pid:
                raise BridgeError("Cannot establish native process ownership.")
            await callbacks.state({"phase": "running", "process": instance.identity})
            instance.tracker = asyncio.create_task(instance._track())
        except BaseException:
            await instance.close()
            raise
        return instance

    async def _discard_stderr(self) -> None:
        if self.process.stderr is not None:
            while await self.process.stderr.read(65_536):
                pass

    def _capture(self) -> bool:
        with self.capture_lock:
            return self._capture_locked()

    def _capture_locked(self) -> bool:
        changed = False
        records = [self.identity, *list(self.descendants.values())]
        discovered = _group_members(self.process.pid)
        for record in records:
            if record is None:
                continue
            if not process_matches(record):
                if not process_resolved(record):
                    self.tracking_ok = False
                continue
            try:
                children = psutil.Process(record["pid"]).children(recursive=True)
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                self.tracking_ok = False
                continue
            discovered.extend(
                identity
                for child in children
                if (identity := process_identity(child.pid)) is not None
            )
        for identity in discovered:
            if identity["pid"] == self.process.pid:
                continue
            key = (identity["pid"], identity["create_time"])
            if key not in self.descendants:
                if len(self.descendants) >= 512:
                    self.tracking_ok = False
                    break
                self.descendants[key] = identity
                changed = True
        return changed

    async def _track(self) -> None:
        try:
            while True:
                if await asyncio.to_thread(self._capture):
                    await self.callbacks.state({"descendants": list(self.descendants.values())})
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.tracking_ok = False

    async def close(self, *, grace_s: float = 2.0) -> bool:
        """Confirm the owned group and every observed descendant stopped.

        This is not a native terminal receipt or proof that remote work ended. Providers
        retain unknown if their protocol/cleanup acknowledgement is missing.
        """
        if self.closed:
            return True
        if self.tracker is not None:
            self.tracker.cancel()
            await asyncio.gather(self.tracker, return_exceptions=True)
        await asyncio.to_thread(self._capture)
        process = self.process
        if process.stdin is not None:
            process.stdin.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=min(grace_s, 0.15))
        records: list[dict[str, Any]] = []
        for sig in (signal.SIGTERM, signal.SIGKILL):
            await asyncio.to_thread(self._capture)
            records = ([self.identity] if self.identity else []) + list(self.descendants.values())
            if any(process_matches(record) for record in records):
                if sig == signal.SIGKILL:
                    self.forced_kill = True
                await asyncio.to_thread(_signal_owned, records, sig)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(process.wait()), timeout=grace_s)
            deadline = asyncio.get_running_loop().time() + grace_s
            while asyncio.get_running_loop().time() < deadline:
                if all(process_resolved(record) for record in records):
                    break
                await asyncio.sleep(0.05)
            if process.returncode is not None and all(
                process_resolved(record) for record in records
            ):
                break
        stopped = (
            process.returncode is not None
            and self.tracking_ok
            and all(process_resolved(record) for record in records)
            and not await asyncio.to_thread(_group_members, process.pid)
        )
        if not self.stderr_task.done():
            self.stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, OSError):
            await self.stderr_task
        self.closed = stopped
        await self.callbacks.state(
            {
                "process_stopped": stopped,
                "exit_code": process.returncode,
                "forced_kill": self.forced_kill,
                "descendants": list(self.descendants.values()),
                "descendant_coverage": "observed" if self.tracking_ok else "incomplete",
            }
        )
        return stopped
