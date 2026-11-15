"""Who is running the agent right now.

Exactly one process runs the agent at a time: the background daemon, or a lone `pa chat` or `pa ask`.
It says so by holding a lock, and the daemon also writes a heartbeat. Everything that needs to know
(a command deciding whether to run the agent itself or hand over to the daemon, `pa status`,
`pa service`) reads them here. This module is small and imports nothing heavy, so a command that
only wants to know does not load the model SDK to find out.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from personal_agent.app import App

LOCK_NAME = "engine.lock"
HEARTBEAT_KEY = "daemon:heartbeat"
HOUSEKEEPING_S = 15.0


class AlreadyRunning(RuntimeError):
    def __init__(self, pid: int | None) -> None:
        super().__init__(f"another process is already running the agent (pid {pid or 'unknown'})")
        self.pid = pid


class EngineLock:
    """Exclusive "I run the agent" lock. Held by the daemon, or by a lone CLI session.

    An advisory `flock`: the kernel releases it when the process dies, so it can never go stale.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def holder_pid(self) -> int | None:
        """The pid written by whoever holds the lock (only meaningful while it is held)."""
        try:
            return int(self.path.read_text().strip())
        except (OSError, ValueError):
            return None

    @classmethod
    def is_taken(cls, path: Path) -> bool:
        """True if some process currently holds the lock."""
        probe = cls(path)
        if probe.acquire():
            probe.release()
            return False
        return True


@dataclass(frozen=True)
class DaemonState:
    kind: str
    """"daemon" (the background service), "terminal" (a chat or command is running the agent
    itself) or "stopped"."""
    pid: int | None = None
    heartbeat_age_s: float | None = None


def daemon_state(app: App) -> DaemonState:
    lock_path = app.paths.run_dir / LOCK_NAME
    if not EngineLock.is_taken(lock_path):
        return DaemonState("stopped")
    pid = EngineLock(lock_path).holder_pid()
    beat = (app.db.kv_get(HEARTBEAT_KEY) or "").split()
    if len(beat) == 2 and beat[0] == str(pid):
        age = max(0.0, app.clock.now() - float(beat[1]))
        if age < 4 * HOUSEKEEPING_S:
            return DaemonState("daemon", pid, age)
    return DaemonState("terminal", pid)
