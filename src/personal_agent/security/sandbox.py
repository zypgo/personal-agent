"""Where commands run.

A policy decides *whether* a command may run; a sandbox limits what it can do when it does.
Both are needed and neither replaces the other: a guard that reads the command text is trivially
bypassed by a command that builds another command, but a process that is not allowed to read
your keys, write outside its workspace or open a network connection cannot do those things however
it was talked into running.

Backends, all behind `Sandbox.execute(command) -> ExecResult`:

- `seatbelt` (macOS): `sandbox-exec` with a `(deny default)` profile. Reads are limited to system
  and tool directories plus the workspace, writes to the workspace and two scratch directories,
  and there is no network at all.
- `docker`: a throwaway container with no network, a read-only root, no capabilities and only the
  workspace mounted. You provide the image (we never pull one for you).
- `none`: no isolation. Only with `i_understand_no_sandbox = true`.

The child gets a tiny fixed environment. API keys and the rest of your environment are never
passed on.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from personal_agent.config import SandboxConfig
from personal_agent.paths import Paths

DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
_CS_DARWIN_USER_TEMP_DIR = 65537  # not exported by Python's os module
STREAM_CAP = 256_000
"""Bytes kept per stream; the rest is read and thrown away so the process never blocks on a full pipe."""
SHELL = "/bin/sh"
# ulimit -f counts 512-byte blocks: refuse to create files bigger than 256 MB; no core dumps.
LIMITS = "ulimit -c 0; ulimit -f 524288 2>/dev/null; "


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None
    """None when the command was killed (timeout) or could not start."""
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    truncated: bool = False
    duration_s: float = 0.0
    error: str | None = None
    """The sandbox itself failed (not the command): nothing ran."""


@dataclass(frozen=True)
class Probe:
    ok: bool
    detail: str


class SandboxUnavailable(RuntimeError):
    pass


class Sandbox(Protocol):
    name: str

    async def execute(self, command: str, *, timeout_s: float) -> ExecResult: ...

    def probe(self) -> Probe:
        """Run a trivial command inside the sandbox: does isolation actually work here?"""
        ...

    def describe(self) -> str:
        """One line for `pa doctor`: what this sandbox allows."""
        ...


# ---- shared plumbing ---------------------------------------------------------------------
def child_env(home: Path, tmp: Path) -> dict[str, str]:
    """The only environment a sandboxed command sees. Never a copy of ours."""
    return {
        "PATH": DEFAULT_PATH,
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TERM": "dumb",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PAGER": "cat",
        "NO_COLOR": "1",
    }


class _Sink:
    """Collects a stream up to a cap. What was read stays available even if we stop waiting."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.data = bytearray()
        self.truncated = False

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(65_536):
            room = self.cap - len(self.data)
            if room > 0:
                self.data += chunk[:room]
            if len(chunk) > room:
                self.truncated = True

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", "replace")


PIPE_GRACE_S = 1.0
"""After the command itself has finished, how long its leftover background processes may keep the
output pipes open before the whole process group is killed. Runs are bounded, so a command
cannot leave a daemon behind."""


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


async def run_process(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout_s: float,
    on_timeout=None,
) -> ExecResult:
    """Run `argv`, cap its output, and kill its whole process group on timeout or cancellation."""
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        return ExecResult(None, error=f"could not start: {exc}")
    assert proc.stdout is not None and proc.stderr is not None
    out, err = _Sink(STREAM_CAP), _Sink(STREAM_CAP)
    readers = [
        asyncio.create_task(out.drain(proc.stdout)),
        asyncio.create_task(err.drain(proc.stderr)),
    ]
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout_s)
    except TimeoutError:
        timed_out = True
        if on_timeout is not None:
            await on_timeout()
        _kill_group(proc)
        await proc.wait()
    except BaseException:
        _kill_group(proc)
        for reader in readers:
            reader.cancel()
        raise
    _, still_open = await asyncio.wait(readers, timeout=PIPE_GRACE_S)
    if still_open:  # background processes are holding the pipes: end them, keep what we have
        _kill_group(proc)
        _, still_open = await asyncio.wait(still_open, timeout=2)
        for reader in still_open:
            reader.cancel()
    return ExecResult(
        exit_code=None if timed_out else proc.returncode,
        stdout=out.text(),
        stderr=err.text(),
        timed_out=timed_out,
        truncated=out.truncated or err.truncated,
        duration_s=time.monotonic() - started,
    )


def _real(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _sbpl(path: Path | str) -> str:
    """A path as an SBPL string literal."""
    text = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


# ---- seatbelt (macOS) ------------------------------------------------------------------------
SYSTEM_READ = (
    "/usr",
    "/bin",
    "/sbin",
    "/System",
    "/Library/Developer/CommandLineTools",
    "/Library/Apple",
    "/Library/Frameworks",
    "/private/etc",
    "/private/var/select",
    "/private/var/db/dyld",
    "/private/var/db/timezone",
    "/private/var/db/xcode_select_link",
    "/dev",
)
DENIED_SERVICES = (
    "com.apple.coreservices.appleevents",
    "com.apple.pasteboard.1",
    "com.apple.SecurityServer",
    "com.apple.securityd",
    "com.apple.secd",
    "com.apple.windowserver.active",
)
TOOL_ROOTS = ("bin", "lib", "opt", "Cellar", "share", "include", "Frameworks")
"""Under /opt/homebrew and /usr/local: where programs live. `etc` and `var` are left out."""


def developer_dir() -> Path | None:
    """The active Xcode / Command Line Tools directory (git and python are shims into it)."""
    try:
        out = subprocess.run(
            ["/usr/bin/xcode-select", "-p"], capture_output=True, text=True, timeout=5, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return _real(out) if out and Path(out).exists() else None


class SeatbeltSandbox:
    name = "seatbelt"

    def __init__(
        self,
        paths: Paths,
        cfg: SandboxConfig,
        *,
        extra_read: list[Path] | None = None,
        extra_write: list[Path] | None = None,
    ) -> None:
        self.workspace = _real(paths.workspace_dir)
        self.skills = _real(paths.skills_dir)
        self.home = _real(paths.run_dir) / "sandbox-home"
        self.tmp = _real(paths.run_dir) / "sandbox-tmp"
        self.cfg = cfg
        self.extra_read = [_real(p) for p in (extra_read or [])]
        self.extra_write = [_real(p) for p in (extra_write or [])]
        for directory in (self.workspace, self.home, self.tmp):
            directory.mkdir(parents=True, exist_ok=True)
        self.profile = self.build_profile()

    # The profile -------------------------------------------------------------------------
    def read_roots(self) -> list[Path]:
        roots = [Path(p) for p in SYSTEM_READ]
        for prefix in ("/opt/homebrew", "/usr/local"):
            roots += [Path(prefix) / name for name in TOOL_ROOTS]
        dev = developer_dir()
        if dev is not None:
            roots.append(dev)
        roots += [self.skills, *self.extra_read]
        return [_real(p) for p in roots if p.exists()]

    def write_roots(self) -> list[Path]:
        return [self.workspace, self.home, self.tmp, *self.extra_write]

    def build_profile(self) -> str:
        writable = self.write_roots()
        readable = [*self.read_roots(), *writable]
        ancestors: set[Path] = set()
        for root in readable:
            ancestors.update(p for p in root.parents)
        xcrun = self.xcrun_cache_rule()
        lines = [
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            "(allow process-exec)",
            "(allow process-fork)",
            "(allow signal (target self))",
            "(allow sysctl-read)",
            "(allow file-read*",
            *(f"  (subpath {_sbpl(p)})" for p in readable),
            ")",
            "(allow file-read-metadata",
            *(f"  (literal {_sbpl(p)})" for p in sorted(ancestors)),
            ")",
            "(allow file-write*",
            *(f"  (subpath {_sbpl(p)})" for p in writable),
            '  (literal "/dev/null") (literal "/dev/dtracehelper")',
            '  (literal "/dev/tty") (regex #"^/dev/fd/[0-9]+$")',
            ")",
            *xcrun,
            "(deny network*)",
            # Defense in depth: system services through which a process could reach beyond its
            # files, network and environment (other apps via Apple events, the clipboard, the
            # keychain). Later rules win in a seatbelt profile, so these override anything above.
            "(deny mach-lookup",
            *(f"  (global-name {_sbpl(name)})" for name in DENIED_SERVICES),
            ")",
        ]
        return "\n".join(lines) + "\n"

    @staticmethod
    def xcrun_cache_rule() -> list[str]:
        """`/usr/bin/python3` and `/usr/bin/git` are shims that run `xcrun`, which wants to write
        a small cache file into the per-user temp directory. Allow exactly those files (and
        nothing else there) so the tools work without printing errors."""
        try:
            user_tmp = _real(os.confstr(_CS_DARWIN_USER_TEMP_DIR))
        except (AttributeError, ValueError, OSError):
            return []
        text = str(user_tmp)
        if not re.fullmatch(r"[A-Za-z0-9/_-]+", text):
            return []
        return [
            f'(allow file-read* file-write* (regex #"^{text}/xcrun_db"))',
            f"(allow file-read-metadata (literal {_sbpl(user_tmp)}))",
        ]

    # Running ------------------------------------------------------------------------------
    def argv(self, command: str) -> list[str]:
        return ["/usr/bin/sandbox-exec", "-p", self.profile, SHELL, "-c", LIMITS + command]

    async def execute(self, command: str, *, timeout_s: float) -> ExecResult:
        return await run_process(
            self.argv(command),
            env=child_env(self.home, self.tmp),
            cwd=self.workspace,
            timeout_s=timeout_s,
        )

    def probe(self) -> Probe:
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
            return Probe(False, "sandbox-exec is not available on this system")
        try:
            done = subprocess.run(
                self.argv("echo ok; test ! -r ~/.ssh/id_rsa"),
                capture_output=True,
                text=True,
                timeout=15,
                env=child_env(self.home, self.tmp),
                cwd=self.workspace,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return Probe(False, f"sandbox-exec failed to start: {exc}")
        if done.returncode != 0 or "ok" not in done.stdout:
            tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["no output"]
            return Probe(False, f"sandbox-exec could not run a trivial command: {tail[0]}")
        return Probe(True, "sandbox-exec works")

    def describe(self) -> str:
        return (
            f"seatbelt: no network; writes only in {self.workspace}; reads system and tool "
            "directories and the workspace"
        )


# ---- docker ------------------------------------------------------------------------------------
class DockerSandbox:
    name = "docker"

    def __init__(self, paths: Paths, cfg: SandboxConfig) -> None:
        self.workspace = _real(paths.workspace_dir)
        self.skills = _real(paths.skills_dir)
        self.cfg = cfg
        self.workspace.mkdir(parents=True, exist_ok=True)

    def argv(self, command: str, container: str) -> list[str]:
        cfg = self.cfg
        uid, gid = os.getuid(), os.getgid()
        return [
            "docker", "run", "--rm", "--name", container,
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "256",
            "--memory", f"{cfg.memory_mb}m",
            "--cpus", "1",
            "--user", f"{uid}:{gid}",
            "--tmpfs", "/tmp:rw,nosuid,size=256m",  # noqa: S108 (inside the container)
            "-e", "HOME=/tmp",
            "-e", "LANG=C.UTF-8",
            "-e", "TERM=dumb",
            "-v", f"{self.workspace}:/workspace:rw",
            "-v", f"{self.skills}:/skills:ro",
            "-w", "/workspace",
            cfg.image, SHELL, "-c", LIMITS + command,
        ]  # fmt: skip

    async def execute(self, command: str, *, timeout_s: float) -> ExecResult:
        container = f"pa-{uuid.uuid4().hex[:12]}"

        async def stop_container() -> None:
            killer = await asyncio.create_subprocess_exec(
                "docker", "kill", container,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )  # fmt: skip
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(killer.wait(), 10)

        env = {"PATH": os.environ.get("PATH", DEFAULT_PATH), "HOME": os.environ.get("HOME", "/")}
        # The Docker client needs a few variables (DOCKER_HOST, DOCKER_CONFIG) to find the daemon.
        env.update({k: v for k, v in os.environ.items() if k.startswith("DOCKER_")})
        return await run_process(
            self.argv(command, container),
            env=env,
            cwd=self.workspace,
            timeout_s=timeout_s,
            on_timeout=stop_container,
        )

    def probe(self) -> Probe:
        if shutil.which("docker") is None:
            return Probe(False, "the `docker` command was not found")
        try:
            info = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, timeout=15, check=False,
            )  # fmt: skip
        except (OSError, subprocess.SubprocessError) as exc:
            return Probe(False, f"docker could not be run: {exc}")
        if info.returncode != 0:
            return Probe(False, "the Docker daemon is not running")
        try:
            image = subprocess.run(
                ["docker", "image", "inspect", self.cfg.image],
                capture_output=True, timeout=15, check=False,
            )  # fmt: skip
        except (OSError, subprocess.SubprocessError) as exc:
            return Probe(False, f"docker could not be run: {exc}")
        if image.returncode != 0:
            return Probe(
                False, f"image {self.cfg.image} is not present: run `docker pull {self.cfg.image}`"
            )
        return Probe(True, f"docker {info.stdout.strip()} with image {self.cfg.image}")

    def describe(self) -> str:
        return (
            f"docker ({self.cfg.image}): no network, read-only root, no capabilities, only "
            f"{self.workspace} is mounted"
        )


# ---- none ------------------------------------------------------------------------------------------
class NoSandbox:
    """Commands run directly as you. Only available when the owner explicitly asks for it."""

    name = "none"

    def __init__(self, paths: Paths, cfg: SandboxConfig) -> None:
        self.workspace = _real(paths.workspace_dir)
        self.home = _real(paths.run_dir) / "sandbox-home"
        self.tmp = _real(paths.run_dir) / "sandbox-tmp"
        for directory in (self.workspace, self.home, self.tmp):
            directory.mkdir(parents=True, exist_ok=True)

    def argv(self, command: str) -> list[str]:
        return [SHELL, "-c", LIMITS + command]

    async def execute(self, command: str, *, timeout_s: float) -> ExecResult:
        return await run_process(
            self.argv(command),
            env=child_env(self.home, self.tmp),
            cwd=self.workspace,
            timeout_s=timeout_s,
        )

    def probe(self) -> Probe:
        return Probe(True, "no isolation")

    def describe(self) -> str:
        return "NONE: commands run as you with full access to your files and network"


# ---- choosing -----------------------------------------------------------------------------------------
def select_sandbox(paths: Paths, cfg: SandboxConfig) -> tuple[Sandbox | None, str]:
    """The sandbox to use, or None with the reason there is none (the shell tool is then not
    offered: we do not quietly fall back to running commands unprotected)."""
    backend = cfg.backend
    if backend == "none":
        if not cfg.i_understand_no_sandbox:
            return None, "backend = none needs i_understand_no_sandbox = true"
        return NoSandbox(paths, cfg), "no isolation (by your choice)"
    candidates: list[Sandbox] = []
    if backend in ("auto", "seatbelt") and sys.platform == "darwin":
        candidates.append(
            SeatbeltSandbox(
                paths,
                cfg,
                extra_read=[Path(p) for p in cfg.extra_read],
                extra_write=[Path(p) for p in cfg.extra_write],
            )
        )
    if backend in ("auto", "docker"):
        candidates.append(DockerSandbox(paths, cfg))
    problems: list[str] = []
    for candidate in candidates:
        probe = candidate.probe()
        if probe.ok:
            return candidate, probe.detail
        problems.append(f"{candidate.name}: {probe.detail}")
    if not candidates:
        problems.append(f"no {backend} sandbox exists on {sys.platform}")
    return None, "; ".join(problems)


class SandboxProvider:
    """Picks the sandbox the first time somebody needs one. Choosing means probing (starting a
    process, asking Docker), which is not something every `pa status` should pay for."""

    def __init__(self, paths: Paths, cfg: SandboxConfig, *, chooser=select_sandbox) -> None:
        self._paths = paths
        self._cfg = cfg
        self._chooser = chooser
        self._chosen: tuple[Sandbox | None, str] | None = None

    @classmethod
    def fixed(cls, sandbox: Sandbox | None, reason: str = "fixed") -> SandboxProvider:
        """A provider that always answers the same (tests, embedding)."""
        provider = cls.__new__(cls)
        provider._chosen = (sandbox, reason)
        return provider

    def resolve(self) -> tuple[Sandbox | None, str]:
        if self._chosen is None:
            self._chosen = self._chooser(self._paths, self._cfg)
        return self._chosen

    @property
    def sandbox(self) -> Sandbox | None:
        return self.resolve()[0]

    @property
    def reason(self) -> str:
        return self.resolve()[1]
