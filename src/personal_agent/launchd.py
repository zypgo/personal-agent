"""Keeping the agent running in the background on a Mac: one launchd LaunchAgent.

An always-on agent has to outlive the terminal it was started in, come back after a login and
after its own crash. On macOS that is what a LaunchAgent is for: launchd starts `pa serve` when the
user logs in, and again whenever it exits with an error. This module writes and manages that one
job and nothing else: no root, nothing system-wide, and no secret in the job's definition (the
daemon reads `PA_HOME/.env` itself, so the keys stay in one private file).

Every call to `launchctl` goes through a function that is handed in, so the logic is tested
without touching the machine's real launchd.
"""

from __future__ import annotations

import hashlib
import math
import os
import plistlib
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from personal_agent.paths import DEFAULT_HOME, Paths
from personal_agent.security.redact import find_secrets

LABEL = "local.personal-agent"
"""The job's name for the default home. Another `PA_HOME` gets a suffix (see `label_for`)."""

SAFE_PATH = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)
"""launchd starts a job with almost no PATH. These are where `ffmpeg` and friends usually live."""

PROTECTED_FOLDERS = (
    "Desktop",
    "Documents",
    "Downloads",
    "Library/Mobile Documents",
    "Library/CloudStorage",
)
"""Folders macOS keeps from background programs unless the user allows it for that program."""

THROTTLE_S = 30
"""launchd waits this long before starting a job again, so a failure that repeats does not spin."""

POLL_S = 0.5
"""How often to ask launchd whether a job has gone, while waiting for it to."""

STOP_WAIT_S = 50.0
"""How long a job may take to stop when its job file cannot say (the default shutdown grace of 30
seconds plus the 15 that `ExitTimeOut` adds)."""

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_PASSWORD_IN_ADDRESS = re.compile(r"://[^/\s@]*:[^/\s@]+@")
_SECRET_NAME = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASS|CRED|AUTH|COOKIE|SESSION)")
_NOT_LOADED = (3, 113)  # `launchctl` says "No such process" / "Could not find service"


class ServiceError(Exception):
    """Something the owner can fix; the message says what."""


# ---- the job's definition -----------------------------------------------------------------------
def label_for(paths: Paths) -> str:
    """One job per home. The default home gets the plain label; any other `PA_HOME` gets its own,
    so trying one out can never replace the job that runs the real agent."""
    default = Path(DEFAULT_HOME).expanduser().resolve()
    if paths.home == default:
        return LABEL
    return f"{LABEL}.{hashlib.sha256(str(paths.home).encode()).hexdigest()[:8]}"


def agents_dir() -> Path:
    return Path("~/Library/LaunchAgents").expanduser()


def pass_env(names: Sequence[str], environ: Mapping[str, str], env_file: Path) -> dict[str, str]:
    """Copy the named variables of this terminal into the job (a proxy, say: a job does not
    inherit the shell's environment). Secrets are refused, by name and by what the value looks
    like: they belong in `.env`, which the daemon reads by itself, and a job's definition is a
    file that gets copied and shown."""
    chosen: dict[str, str] = {}
    for raw in names:
        name = raw.strip()
        if not _ENV_NAME.match(name):
            raise ServiceError(f"{raw!r} is not the name of an environment variable")
        if _SECRET_NAME.search(name):
            raise ServiceError(
                f"{name} looks like a secret. Keys and tokens go in {env_file}, "
                "which the service reads by itself; nothing secret is put in the job file."
            )
        value = environ.get(name)
        if not value:
            raise ServiceError(f"{name} is not set in this terminal, so there is nothing to copy")
        if find_secrets(value) or _PASSWORD_IN_ADDRESS.search(value):
            raise ServiceError(
                f"The value of {name} holds a key or a password (a proxy address can). Put the "
                f"line in {env_file}, which the service reads by itself, instead of copying it "
                "into the job file."
            )
        chosen[name] = value
    return chosen


def protected_folder(path: str | os.PathLike[str], home: Path | None = None) -> str | None:
    """The protected folder (Desktop, Documents, ...) that `path` is in, or None.

    A program started by launchd is not allowed to read there unless the user has said so for that
    very program, and nothing asks them: it just fails with "Operation not permitted". The path is
    taken as written, not resolved: a virtualenv in the Desktop folder is in the Desktop folder
    whatever its `python` link points to."""
    base = home or Path.home()
    try:
        relative = Path(os.path.abspath(Path(path).expanduser())).relative_to(base)
    except ValueError:
        return None
    for folder in PROTECTED_FOLDERS:
        if relative == Path(folder) or Path(folder) in relative.parents:
            return folder
    return None


def build_plist(
    paths: Paths,
    *,
    python: str,
    label: str,
    grace_s: float,
    env: Mapping[str, str] | None = None,
    keep_awake: bool = False,
) -> dict[str, object]:
    """The job. It starts at login, and again when it exits with an error (a crash, a lock held by
    somebody else) but not when it was asked to stop; launchd gives running work as long to finish
    as the daemon itself does."""
    program = [python, "-m", "personal_agent", "serve"]
    if keep_awake:
        # `-s`: no sleep while the Mac is on mains power (the display may still go dark).
        program = ["/usr/bin/caffeinate", "-s", *program]
    search = [str(Path(python).parent), *SAFE_PATH]
    environment = {
        "PA_HOME": str(paths.home),
        "PATH": ":".join(dict.fromkeys(search)),
        "PYTHONUNBUFFERED": "1",
        "PYTHONUTF8": "1",
        **dict(env or {}),
    }
    return {
        "Label": label,
        "ProgramArguments": program,
        "WorkingDirectory": str(paths.home),
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_S,
        "ExitTimeOut": int(grace_s) + 15,
        "StandardOutPath": str(paths.logs_dir / "service.out.log"),
        "StandardErrorPath": str(paths.logs_dir / "service.err.log"),
    }


def render(plist: Mapping[str, object]) -> bytes:
    return plistlib.dumps(dict(plist), fmt=plistlib.FMT_XML, sort_keys=True)


def masked(plist: Mapping[str, object], hide: Sequence[str]) -> dict[str, object]:
    """The job as it may be shown on a screen: the copied variables only by name."""
    shown = dict(plist)
    environment = dict(shown.get("EnvironmentVariables") or {})  # type: ignore[call-overload]
    for name in hide:
        if name in environment:
            environment[name] = "(hidden)"
    shown["EnvironmentVariables"] = environment
    return shown


# ---- talking to launchd -------------------------------------------------------------------------
@dataclass(frozen=True)
class Done:
    code: int
    out: str = ""
    err: str = ""


class Launchctl(Protocol):
    def __call__(self, *args: str) -> Done: ...


def system_launchctl(*args: str) -> Done:
    try:
        result = subprocess.run(
            ["/bin/launchctl", *args], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Done(127, "", type(exc).__name__)
    return Done(result.returncode, result.stdout, result.stderr)


def lint(file: Path) -> str | None:
    """`plutil -lint`, the tool launchd's own parser agrees with. None when the file is fine (or
    the tool is not there)."""
    tool = Path("/usr/bin/plutil")
    if not tool.exists():
        return None
    try:
        result = subprocess.run(
            [str(tool), "-lint", str(file)], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return None
    return (result.stdout + result.stderr).strip() or "the job file is not a valid property list"


def can_import(python: str) -> str | None:
    """None if `python` can import this package, otherwise why not (a job that cannot even start
    must not be installed)."""
    try:
        result = subprocess.run(
            [python, "-c", "import personal_agent"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home())},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"it could not be run ({type(exc).__name__})"
    if result.returncode == 0:
        return None
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    return lines[-1] if lines else f"it exited with status {result.returncode}"


_FIELDS = {
    "state": re.compile(r"^\s*state = (.+?)\s*$", re.M),
    "pid": re.compile(r"^\s*pid = (\d+)\s*$", re.M),
    "runs": re.compile(r"^\s*runs = (\d+)\s*$", re.M),
    "last_exit": re.compile(r"^\s*last exit code = (.+?)\s*$", re.M),
}


@dataclass(frozen=True)
class LaunchStatus:
    installed: bool
    loaded: bool
    state: str | None = None
    pid: int | None = None
    runs: int | None = None
    last_exit: str | None = None


def parse_print(text: str) -> dict[str, str]:
    """The few facts worth showing from `launchctl print`. Its layout is not a promise, so a field
    that is not there is simply left out."""
    found: dict[str, str] = {}
    for key, pattern in _FIELDS.items():
        match = pattern.search(text)
        if match:
            found[key] = match.group(1)
    return found


@dataclass
class Installed:
    label: str
    plist_file: Path
    text: str
    shown_text: str
    written: bool
    loaded: bool = False
    program: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Restarted:
    status: LaunchStatus
    started: bool
    """False when launchd had not started the job again by the time we stopped looking."""


@dataclass(frozen=True)
class Removed:
    label: str
    was_loaded: bool
    removed_file: bool
    stopped: bool = True
    """False when the job was still finishing its work after the longest wait that makes sense."""


class ServiceManager:
    """Install, remove and look at the one LaunchAgent that runs `pa serve` for a home."""

    def __init__(
        self,
        paths: Paths,
        *,
        launchctl: Launchctl | None = None,
        agents: Path | None = None,
        uid: int | None = None,
        python: str | None = None,
        platform: str = sys.platform,
        probe: Callable[[str], str | None] = can_import,
        linter: Callable[[Path], str | None] = lint,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.paths = paths
        self.launchctl = launchctl if launchctl is not None else system_launchctl
        self.agents = agents if agents is not None else agents_dir()
        self.uid = os.getuid() if uid is None else uid
        self.python = python or sys.executable
        self.platform = platform
        self.probe = probe
        self.linter = linter
        self.sleep = sleep
        self.label = label_for(paths)

    # ---- where things are -------------------------------------------------------------------
    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{self.label}"

    @property
    def plist_file(self) -> Path:
        return self.agents / f"{self.label}.plist"

    @property
    def out_log(self) -> Path:
        return self.paths.logs_dir / "service.out.log"

    @property
    def err_log(self) -> Path:
        return self.paths.logs_dir / "service.err.log"

    def _require_mac(self) -> None:
        if self.platform != "darwin":
            raise ServiceError(
                "The background service is a macOS LaunchAgent. On Linux, run `pa serve` from a "
                "systemd user unit (ExecStart=… -m personal_agent serve, Restart=on-failure)."
            )

    # ---- installing ---------------------------------------------------------------------------
    def plan(
        self, *, grace_s: float, env: Mapping[str, str] | None = None, keep_awake: bool = False
    ) -> Installed:
        """What `install` would write, with every problem found so far. Writes nothing."""
        self._require_mac()
        python = os.path.abspath(self.python)
        if not (Path(python).is_file() and os.access(python, os.X_OK)):
            raise ServiceError(f"{python} is not a program that can be run")
        plist = build_plist(
            self.paths,
            python=python,
            label=self.label,
            grace_s=grace_s,
            env=env,
            keep_awake=keep_awake,
        )
        warnings: list[str] = []
        for what, where in (("The program", python), ("The agent's code", _package_dir())):
            folder = protected_folder(where)
            if folder:
                warnings.append(
                    f"{what} is in your {folder} folder ({where}). macOS does not let a background "
                    f"job read {folder} unless you allow that program, and nothing will ask: the "
                    "service would fail with 'Operation not permitted'. Move the project out of "
                    f"{folder} (for example to ~/Developer), or install it with "
                    "`uv tool install .` and run `pa service install` from that copy."
                )
                break
        return Installed(
            label=self.label,
            plist_file=self.plist_file,
            text=render(plist).decode(),
            shown_text=render(masked(plist, list(env or {}))).decode(),
            written=False,
            program=list(plist["ProgramArguments"]),  # type: ignore[call-overload]
            warnings=warnings,
        )

    def install(
        self,
        *,
        grace_s: float,
        env: Mapping[str, str] | None = None,
        keep_awake: bool = False,
        start: bool = True,
    ) -> Installed:
        result = self.plan(grace_s=grace_s, env=env, keep_awake=keep_awake)
        why_not = self.probe(os.path.abspath(self.python))
        if why_not:
            raise ServiceError(
                f"{self.python} cannot run the agent ({why_not}). Run `pa service install` from "
                "the environment where the agent is installed, or point --python at it."
            )
        self.paths.logs_dir.mkdir(parents=True, exist_ok=True)
        for log in (self.out_log, self.err_log):  # created private; launchd appends to what exists
            _touch_private(log)
        self._write(result.text.encode())
        result.written = True
        if start:
            self._start()
            result.loaded = True
        return result

    def _write(self, data: bytes) -> None:
        """Atomically, privately, and only a file that a property-list parser accepts."""
        self.agents.mkdir(parents=True, exist_ok=True)
        temporary = self.agents / f".{self.label}.plist.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            problem = self.linter(temporary)
            if problem:
                raise ServiceError(f"the job file was not accepted: {problem}")
            os.replace(temporary, self.plist_file)
        finally:
            temporary.unlink(missing_ok=True)

    def _start(self) -> None:
        self.launchctl("enable", self.target)  # in case it was once disabled; harmless otherwise
        if self.is_loaded():
            self.launchctl("bootout", self.target)  # a changed job file only counts once reloaded
            if not self._wait_until_gone():
                raise ServiceError(
                    "The running copy of the service has not finished stopping. Give it a minute "
                    "and run `pa service install` again."
                )
        failure = Done(1)
        for attempt in range(6):
            failure = self.launchctl("bootstrap", self.domain, str(self.plist_file))
            if failure.code == 0:
                return
            if attempt < 5:
                self.sleep(1.0)  # the old copy may still be going away
        raise ServiceError(
            "launchd refused the job: "
            + ((failure.err or failure.out).strip() or f"exit status {failure.code}")
        )

    # ---- removing, restarting, looking ----------------------------------------------------------
    def is_loaded(self) -> bool:
        return self.launchctl("print", self.target).code == 0

    def installed_exit_timeout(self) -> float | None:
        """How long launchd lets the job finish after asking it to stop, as the job file says."""
        try:
            data = plistlib.loads(self.plist_file.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None
        value = data.get("ExitTimeOut") if isinstance(data, dict) else None
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            return None
        return float(value)

    def _wait_until_gone(self) -> bool:
        """`bootout` comes back once the job has been told to stop, not once it has: a daemon that
        is finishing its work stays loaded for up to its exit timeout, and only then can the job be
        loaded again (or be said to be gone)."""
        budget = (self.installed_exit_timeout() or STOP_WAIT_S) + 5.0
        for _ in range(math.ceil(budget / POLL_S)):
            if not self.is_loaded():
                return True
            self.sleep(POLL_S)
        return not self.is_loaded()

    def uninstall(self) -> Removed:
        self._require_mac()
        loaded = self.is_loaded()
        stopped = True
        if loaded:
            asked = self.launchctl("bootout", self.target)
            if asked.code != 0 and asked.code not in _NOT_LOADED:
                raise ServiceError(
                    "launchd would not stop the job: "
                    + ((asked.err or asked.out).strip() or f"exit status {asked.code}")
                )
            stopped = self._wait_until_gone()
        existed = self.plist_file.exists()
        self.plist_file.unlink(missing_ok=True)
        return Removed(self.label, loaded, existed, stopped)

    def restart(self, *, wait_s: float | None = None) -> Restarted:
        """Stop the job (letting running work finish) and start it again, then say how it went.
        launchd starts a job at most once per `THROTTLE_S`, so a restart soon after a start waits."""
        self._require_mac()
        before = self.status()
        if not before.loaded:
            raise ServiceError("The service is not loaded. Install it with `pa service install`.")
        done = self.launchctl("kickstart", "-k", self.target)
        if done.code != 0:
            raise ServiceError(
                "launchd could not restart the job: "
                + ((done.err or done.out).strip() or f"exit status {done.code}")
            )
        if wait_s is None:
            wait_s = THROTTLE_S + (self.installed_exit_timeout() or STOP_WAIT_S) + 5.0
        now = before
        for _ in range(math.ceil(wait_s / 1.0)):
            self.sleep(1.0)
            now = self.status()
            if now.pid is not None and _is_a_new_run(before, now):
                return Restarted(now, True)
        return Restarted(now, False)

    def status(self) -> LaunchStatus:
        self._require_mac()
        installed = self.plist_file.exists()
        shown = self.launchctl("print", self.target)
        if shown.code != 0:
            return LaunchStatus(installed=installed, loaded=False)
        facts = parse_print(shown.out)
        return LaunchStatus(
            installed=installed,
            loaded=True,
            state=facts.get("state"),
            pid=int(facts["pid"]) if "pid" in facts else None,
            runs=int(facts["runs"]) if "runs" in facts else None,
            last_exit=facts.get("last_exit"),
        )

    def installed_program(self) -> list[str] | None:
        """The program the installed job file runs, if there is one that can be read."""
        try:
            data = plistlib.loads(self.plist_file.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None
        program = data.get("ProgramArguments") if isinstance(data, dict) else None
        if isinstance(program, list) and all(isinstance(p, str) for p in program):
            return program
        return None

    def installed_python(self) -> str | None:
        """The interpreter the installed job runs (the one right before `-m personal_agent`)."""
        program = self.installed_program() or []
        for index, part in enumerate(program[:-1]):
            if index > 0 and part == "-m" and program[index + 1] == "personal_agent":
                return program[index - 1]
        return None


def _is_a_new_run(before: LaunchStatus, now: LaunchStatus) -> bool:
    """`runs` counts every start launchd made; where it cannot be read, a different pid will do."""
    if before.runs is not None and now.runs is not None:
        return now.runs > before.runs
    return now.pid != before.pid


def _package_dir() -> str:
    import personal_agent

    return str(Path(personal_agent.__file__).parent)


def _touch_private(path: Path) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def tail(path: Path, lines: int) -> list[str]:
    """The last `lines` lines of a file that may be large, read from its end."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                step = min(block, size)
                size -= step
                handle.seek(size)
                data = handle.read(step) + data
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-lines:] if lines > 0 else []
