#!/usr/bin/env python3
"""Watch a GitHub repository for new pull requests and wake whoever listens to this terminal.

    .venv/bin/python scripts/pr_watch.py watch            # in a terminal whose output is monitored
    .venv/bin/python scripts/pr_watch.py status           # what it knows about each pull request
    .venv/bin/python scripts/pr_watch.py claim 7          # "I am on it": no reminders meanwhile
    .venv/bin/python scripts/pr_watch.py ack 7 --result integrated --note "landed as abc1234"
    .venv/bin/python scripts/pr_watch.py selftest         # one made-up event, to test the wake-up

An announcement is ONE line on standard output, `PR_WATCH_EVENT {json}`. Everything else this script
prints goes to standard error and starts with `pr-watch:`, so a monitor that waits for the first
word never fires on a log line. The announcement is a doorbell, not the truth: `status` (the state
on disk) says which pull requests are waiting, so a doorbell that was missed or merged with another
costs nothing.

Why it is built this way (docs/PR_PLAYBOOK.md says what the listener then does):

- It polls one REST endpoint with `gh api`: no public URL, no tunnel, no extra token scope. The
  reference project zeroclaw makes the same choice for its git channel (references/zeroclaw). One
  request a poll is 120 an hour at the default 30 s, a fortieth of what GitHub allows. The events
  feed is not used: it lags by minutes.
- What it knows is on disk and written atomically. A restart neither replays history (the first run
  only notes what is already there) nor misses a pull request opened while it was down. An
  announcement is printed before it is recorded, so a crash in between leaves one extra line, never
  a lost one; the listener's `claim` refuses a second time.
- The line carries only what is validated here: a number, a commit id, a login, a few flags. A pull
  request's title, text and branch names are written by whoever opened it, so they never travel
  through a channel that wakes an agent; the agent reads them afterwards, as data.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import IO, Any

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "PR_WATCH_EVENT"
PLAYBOOK = "docs/PR_PLAYBOOK.md"
RESULTS = ("integrated", "declined", "needs_user", "needs_changes")
FINAL_RESULTS = ("integrated", "declined")  # after these nothing re-arms the pull request
UNHEALTHY_AFTER = 5  # failed polls in a row before the listener is told
MAX_BACKOFF_S = 900.0
RATE_LIMIT_WAIT_S = 300.0

LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?")
SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

# What is asked of GitHub, and the only fields of the answer that are used.
JQ = (
    "[.[] | {number, state, draft: (.draft // false),"
    ' author: (.user.login // ""), head_sha: .head.sha,'
    " head_repo: (.head.repo.full_name // null), base_repo: .base.repo.full_name,"
    " base_ref: .base.ref}]"
)

Event = dict[str, Any]
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class StateError(Exception):
    """The state file cannot be used; starting over silently would hide pull requests."""


class FetchError(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


# ---- what GitHub says -----------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Pull:
    number: int
    open: bool
    draft: bool
    author: str
    head: str
    head_repo: str | None
    base_repo: str
    base_ref: str


def parse_pull(raw: object) -> Pull | None:
    """One entry of the answer, or None when it does not look like one."""
    if not isinstance(raw, dict):
        return None
    number, head = raw.get("number"), raw.get("head_sha")
    base_repo, base_ref = raw.get("base_repo"), raw.get("base_ref")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return None
    if not isinstance(head, str) or not SHA.fullmatch(head):
        return None
    if not isinstance(base_repo, str) or not REPO.fullmatch(base_repo):
        return None
    if not isinstance(base_ref, str) or not base_ref:
        return None
    author, head_repo = raw.get("author"), raw.get("head_repo")
    return Pull(
        number=number,
        open=raw.get("state") == "open",
        draft=raw.get("draft") is True,
        author=author if isinstance(author, str) and LOGIN.fullmatch(author) else "?",
        head=head,
        head_repo=head_repo if isinstance(head_repo, str) and REPO.fullmatch(head_repo) else None,
        base_repo=base_repo,
        base_ref=base_ref,
    )


def classify(stderr: str) -> str:
    """A short, fixed word for what went wrong: raw error text never reaches the listener."""
    text = stderr.lower()
    if "rate limit" in text or "abuse" in text:
        return "rate_limit"
    if "401" in text or "bad credentials" in text or "gh auth login" in text:
        return "auth"
    if "404" in text or "not found" in text:
        return "not_found"
    words = ("resolve", "timeout", "timed out", "connect", "network", "tls", "no such host")
    if any(w in text for w in words):
        return "network"
    return "error"


def gh_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", GH_SPINNER_DISABLED="1", NO_COLOR="1"
    )
    return env


def fetch_pulls(repo: str, run: Runner = subprocess.run) -> list[Pull]:
    """The 100 most recently opened pull requests, open or not (so one opened and closed between
    two polls is still seen, and not announced)."""
    command = [
        "gh", "api", "--method", "GET", f"repos/{repo}/pulls",
        "-f", "state=all", "-f", "sort=created", "-f", "direction=desc", "-f", "per_page=100",
        "--jq", JQ,
    ]  # fmt: skip
    try:
        proc = run(command, capture_output=True, text=True, timeout=45, check=False, env=gh_env())
    except subprocess.TimeoutExpired:
        raise FetchError("network") from None
    except FileNotFoundError:
        raise FetchError("gh_missing") from None
    if proc.returncode != 0:
        raise FetchError(classify(proc.stderr))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise FetchError("bad_response") from None
    if not isinstance(data, list):
        raise FetchError("bad_response")
    pulls = [p for p in map(parse_pull, data) if p is not None]
    if data and not pulls:  # every entry unreadable: the answer changed shape, say so
        raise FetchError("bad_response")
    return pulls


# ---- what it remembers ----------------------------------------------------------------------
@dataclasses.dataclass
class Tracked:
    """One pull request. status: baseline (there before the first run: never announced), draft,
    pending (announced, not answered), parked (answered "needs the user" or "needs changes": a
    new commit re-arms it), done (answered "integrated" or "declined"), closed (closed or merged
    by someone else)."""

    status: str
    head: str
    author: str
    seen_at: float
    trusted: bool = False
    reasons: list[str] = dataclasses.field(default_factory=list)
    notified_at: float | None = None
    reminders: int = 0
    claimed_until: float | None = None
    result: str | None = None
    note: str | None = None
    acked_at: float | None = None


@dataclasses.dataclass
class State:
    repo: str
    initialized: bool = False
    last_ok: float | None = None
    last_error: str | None = None
    prs: dict[int, Tracked] = dataclasses.field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "repo": self.repo,
            "initialized": self.initialized,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "prs": {str(n): dataclasses.asdict(t) for n, t in sorted(self.prs.items())},
        }

    @classmethod
    def from_json(cls, repo: str, raw: object) -> State:
        try:
            if not isinstance(raw, dict) or raw.get("repo") != repo:
                raise StateError(f"the state file is not for {repo}")
            fields = {f.name for f in dataclasses.fields(Tracked)}
            prs = {
                int(number): Tracked(**{k: v for k, v in item.items() if k in fields})
                for number, item in (raw.get("prs") or {}).items()
            }
            return cls(
                repo=repo,
                initialized=bool(raw.get("initialized")),
                last_ok=raw.get("last_ok"),
                last_error=raw.get("last_error"),
                prs=prs,
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise StateError(f"the state file cannot be read ({exc})") from None


class Store:
    """The state of one repository: one JSON file, replaced atomically, changed under a lock."""

    def __init__(self, directory: Path, repo: str) -> None:
        # An owner has no underscore, so a name made this way reads back one way only.
        slug = repo.replace("/", "__")
        self.repo = repo
        self.path = directory / f"{slug}.json"
        self.lock_path = directory / f"{slug}.lock"
        self.watch_lock_path = directory / f"{slug}.watch.lock"

    def load(self) -> State:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return State(repo=self.repo)
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StateError(
                f"{self.path} is damaged ({exc}); move it away to start over: every pull request "
                "that is open then counts as already known"
            ) from None
        return State.from_json(self.repo, raw)

    def save(self, state: State) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps(state.to_json(), indent=1, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    @contextlib.contextmanager
    def transaction(self) -> Iterator[State]:
        """Read the state, let the caller change it, write it back; nobody else changes it
        meanwhile. Nothing is written when the caller raises."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.load()
            yield state
            self.save(state)

    @contextlib.contextmanager
    def single_watcher(self) -> Iterator[None]:
        """Held for as long as a `watch` runs: a second one would announce everything twice."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.watch_lock_path.open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StateError("another `watch` is already running for this repository") from None
            handle.truncate(0)
            handle.write(str(os.getpid()))
            handle.flush()
            yield

    def watcher_pid(self) -> int | None:
        """The pid of the running `watch` (-1 when unknown), None when there is none."""
        try:
            with self.watch_lock_path.open("r+") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    text = handle.read().strip()
                    return int(text) if text.isdigit() else -1
                fcntl.flock(handle, fcntl.LOCK_UN)
                return None
        except FileNotFoundError:
            return None


# ---- what to announce -----------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Settings:
    repo: str
    trusted: frozenset[str]  # lower-case logins whose pull requests may be merged unasked
    base_branch: str = "main"
    interval_s: float = 30.0
    remind_after_s: float = 3 * 3600.0
    max_reminders: int = 3
    include_open: bool = False


def assess(pull: Pull, cfg: Settings) -> list[str]:
    """Why this pull request is not to be merged without asking (empty: it may be)."""
    reasons = []
    if pull.author.lower() not in cfg.trusted:
        reasons.append("author_not_trusted")
    if pull.head_repo != pull.base_repo:  # a fork, or a fork that was deleted
        reasons.append("from_fork")
    if pull.base_ref != cfg.base_branch:
        reasons.append("other_base_branch")
    return reasons


def event(kind: str, cfg: Settings, number: int, tracked: Tracked) -> Event:
    return {
        "event": kind,
        "repo": cfg.repo,
        "number": number,
        "head": tracked.head,
        "author": tracked.author,
        "trusted": tracked.trusted,
        "reasons": tracked.reasons,
        "attempt": tracked.reminders + 1,
        "url": f"https://github.com/{cfg.repo}/pull/{number}",
        "playbook": PLAYBOOK,
    }


def format_event(ev: Event) -> str:
    return f"{SENTINEL} {json.dumps(ev, separators=(',', ':'), sort_keys=True)}"


def claimed(tracked: Tracked, now: float) -> bool:
    return tracked.claimed_until is not None and now < tracked.claimed_until


def announce(tracked: Tracked, now: float) -> None:
    tracked.status, tracked.notified_at, tracked.reminders = "pending", now, 0
    tracked.claimed_until = tracked.acked_at = None


def plan(state: State, pulls: Sequence[Pull], now: float, cfg: Settings) -> list[Event]:
    """Bring the state up to date with `pulls` and return what is to be announced."""
    events: list[Event] = []
    first_run = not state.initialized
    for pull in sorted(pulls, key=lambda p: p.number):
        reasons = assess(pull, cfg)
        tracked = state.prs.get(pull.number)
        if tracked is None:
            tracked = Tracked(status="", head=pull.head, author=pull.author, seen_at=now)
            state.prs[pull.number] = tracked
            tracked.trusted, tracked.reasons = not reasons, reasons
            if first_run and not cfg.include_open:
                tracked.status = "baseline"
            elif not pull.open:
                tracked.status = "closed"  # opened and closed between two polls
            elif pull.draft:
                tracked.status = "draft"
            else:
                announce(tracked, now)
                events.append(event("new_pr", cfg, pull.number, tracked))
            continue
        head_changed = pull.head != tracked.head
        tracked.head, tracked.author = pull.head, pull.author
        tracked.trusted, tracked.reasons = not reasons, reasons
        if not pull.open:
            if tracked.status in ("pending", "draft", "parked"):
                tracked.status, tracked.claimed_until = "closed", None
        elif tracked.status == "draft" and not pull.draft:
            announce(tracked, now)
            events.append(event("new_pr", cfg, pull.number, tracked))
        elif not pull.draft and (
            (tracked.status == "parked" and head_changed)  # a new commit on one set aside
            or tracked.status == "closed"  # someone reopened it
        ):
            announce(tracked, now)
            events.append(event("pr_updated", cfg, pull.number, tracked))
        elif (
            tracked.status == "pending"
            and not claimed(tracked, now)
            and tracked.notified_at is not None
            and now - tracked.notified_at >= cfg.remind_after_s
            and tracked.reminders < cfg.max_reminders
        ):
            tracked.reminders += 1
            tracked.notified_at = now
            events.append(event("reminder", cfg, pull.number, tracked))
    state.initialized = True
    return events


def resume(state: State, now: float, cfg: Settings) -> list[Event]:
    """Pull requests announced earlier and not answered: announce them again after a restart (the
    terminal that heard the first time may be gone)."""
    events = []
    for number, tracked in sorted(state.prs.items()):
        if tracked.status == "pending" and not claimed(tracked, now):
            tracked.notified_at = now
            events.append(event("resume", cfg, number, tracked))
    return events


def claim(state: State, number: int, now: float, ttl_s: float, *, renew: bool = False) -> str:
    tracked = state.prs.get(number)
    if tracked is None or tracked.status != "pending":
        return "not_pending"
    if claimed(tracked, now) and not renew:
        return "claimed"
    tracked.claimed_until = now + ttl_s
    return "ok"


def ack(
    state: State, number: int, result: str, note: str, now: float, head: str | None = None
) -> str:
    """Record the outcome. With `head`, only when that is still the pull request's newest commit:
    what was reviewed must be what was merged, and the author may have pushed since."""
    tracked = state.prs.get(number)
    if tracked is None or tracked.status not in ("pending", "parked"):
        return "not_pending"
    if head is not None and head != tracked.head:
        return "head_changed"
    tracked.status = "done" if result in FINAL_RESULTS else "parked"
    tracked.result, tracked.acked_at, tracked.claimed_until = result, now, None
    tracked.note = re.sub(r"\s+", " ", note).strip()[:300] or None
    return "ok"


# ---- running ---------------------------------------------------------------------------------
def emit(out: IO[str], ev: Event) -> None:
    print(format_event(ev), file=out, flush=True)


def backoff(cfg: Settings, failures: int, kind: str) -> float:
    delay = min(cfg.interval_s * 2 ** (failures - 1), MAX_BACKOFF_S)
    return max(delay, RATE_LIMIT_WAIT_S) if kind == "rate_limit" else delay


def watch(
    cfg: Settings,
    store: Store,
    *,
    run: Runner = subprocess.run,
    out: IO[str],
    err: IO[str],
    clock: Callable[[], float] = time.time,
    stop: threading.Event | None = None,
    wait: Callable[[float], object] | None = None,
    once: bool = False,
) -> int:
    stop = stop or threading.Event()
    wait = wait or stop.wait  # a test passes its own: it never really sleeps

    def log(message: str) -> None:
        print(f"pr-watch: {message}", file=err, flush=True)

    known = store.load().prs
    waiting = sum(1 for t in known.values() if t.status == "pending")
    log(
        f"watching {cfg.repo} every {cfg.interval_s:g}s; may merge unasked: "
        f"{', '.join(sorted(cfg.trusted))}; {len(known)} known, {waiting} waiting for an answer"
    )
    failures, announced_waiting = 0, False
    while not stop.is_set():
        try:
            pulls = fetch_pulls(cfg.repo, run)
        except FetchError as exc:
            failures += 1
            log(f"poll failed ({exc.kind}); {failures} in a row")
            with store.transaction() as state:
                state.last_error = exc.kind
                if failures == UNHEALTHY_AFTER:
                    emit(
                        out,
                        {
                            "event": "watcher_unhealthy",
                            "repo": cfg.repo,
                            "kind": exc.kind,
                            "failures": failures,
                            "playbook": PLAYBOOK,
                        },
                    )
            if once:
                return 1
            delay = backoff(cfg, failures, exc.kind)
        else:
            if failures:
                log("polling works again")
            failures = 0
            now = clock()
            with store.transaction() as state:
                events = [] if announced_waiting else resume(state, now, cfg)
                events += plan(state, pulls, now, cfg)
                for ev in events:  # printed before the state is written: see the module text
                    emit(out, ev)
                state.last_ok, state.last_error = now, None
            announced_waiting = True
            if once:
                return 0
            delay = cfg.interval_s
        wait(delay)
    log("stopped")
    return 0


# ---- command line ----------------------------------------------------------------------------
def default_repo(run: Runner = subprocess.run) -> str | None:
    proc = run(
        ["git", "remote", "get-url", "github"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    if proc.returncode != 0:
        return None
    found = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", proc.stdout.strip())
    return found.group(1) if found else None


def default_state_dir() -> Path:
    return Path(os.environ.get("PR_WATCH_HOME") or Path.home() / ".local" / "state" / "pr-watch")


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def render_status(store: Store, state: State, now: float) -> str:
    pid = store.watcher_pid()
    watcher = "not running" if pid is None else f"running (pid {pid})" if pid > 0 else "running"
    last = "never" if state.last_ok is None else f"{ago(now - state.last_ok)} ago"
    lines = [
        f"{state.repo}: watcher {watcher}; last good poll {last}; last error {state.last_error or '-'}",
        f"{'#':>5}  {'status':<9} {'author':<18} {'head':<9} {'seen':<8} {'claim':<8} result",
    ]
    for number, t in sorted(state.prs.items()):
        claim_left = (
            f"{ago(t.claimed_until - now)} left" if claimed(t, now) and t.claimed_until else "-"
        )
        lines.append(
            f"{number:>5}  {t.status:<9} {t.author:<18} {t.head[:8]:<9} "
            f"{ago(now - t.seen_at) + ' ago':<8} {claim_left:<8} {t.result or '-'}"
        )
    return "\n".join(lines)


def commit_id(text: str) -> str:
    if not SHA.fullmatch(text):
        raise argparse.ArgumentTypeError("a commit id is 40 lower-case hex digits")
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pr_watch.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", help="OWNER/REPO (default: the `github` remote of this checkout)")
    parser.add_argument("--state-dir", type=Path, help="where the state is kept")
    commands = parser.add_subparsers(dest="command", required=True)
    watching = commands.add_parser("watch", help="poll and announce until stopped")
    watching.add_argument("--interval", type=float, default=30.0, help="seconds between polls")
    watching.add_argument(
        "--trusted",
        action="append",
        metavar="LOGIN",
        help="a login whose pull requests may be merged unasked (default: the owner; repeatable)",
    )
    watching.add_argument("--base", default="main", help="the branch pull requests must target")
    watching.add_argument("--remind-after-min", type=float, default=180.0)
    watching.add_argument(
        "--include-open",
        action="store_true",
        help="on the very first run, announce the pull requests that are open now",
    )
    watching.add_argument("--once", action="store_true", help="poll once and exit")
    status = commands.add_parser("status", help="what is known about each pull request")
    status.add_argument("--json", action="store_true")
    claiming = commands.add_parser("claim", help="say that a pull request is being worked on")
    claiming.add_argument("number", type=int)
    claiming.add_argument("--ttl-min", type=float, default=120.0)
    claiming.add_argument("--renew", action="store_true", help="extend a claim that is yours")
    acking = commands.add_parser("ack", help="record the outcome of a pull request")
    acking.add_argument("number", type=int)
    acking.add_argument("--result", choices=RESULTS, required=True)
    acking.add_argument("--note", default="", help="one line, kept with the result")
    acking.add_argument(
        "--head",
        type=commit_id,
        help="the commit that was reviewed: refused when the pull request has a newer one",
    )
    testing = commands.add_parser("selftest", help="print one made-up event")
    testing.add_argument("--delay", type=float, default=0.0, help="seconds to wait first")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Runner = subprocess.run,
    out: IO[str] | None = None,
    err: IO[str] | None = None,
    clock: Callable[[], float] = time.time,
) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    args = build_parser().parse_args(argv)
    repo = args.repo or default_repo(run)
    if not repo or not REPO.fullmatch(repo):
        print("pr-watch: say which repository with --repo OWNER/REPO", file=err)
        return 2
    store = Store(args.state_dir or default_state_dir(), repo)
    try:
        if args.command == "selftest":
            time.sleep(max(0.0, args.delay))
            emit(out, {"event": "selftest", "repo": repo, "playbook": PLAYBOOK})
            return 0
        if args.command == "status":
            state = store.load()
            if args.json:
                print(json.dumps(state.to_json(), indent=1, sort_keys=True), file=out)
            else:
                print(render_status(store, state, clock()), file=out)
            return 0
        if args.command == "claim":
            with store.transaction() as state:
                outcome = claim(state, args.number, clock(), args.ttl_min * 60, renew=args.renew)
                tracked = state.prs.get(args.number)
            messages = {
                "ok": f"claimed #{args.number} at {tracked.head if tracked else '?'} "
                f"for {args.ttl_min:g} minutes",
                "claimed": f"#{args.number} is already claimed (--renew if it is yours)",
                "not_pending": f"#{args.number} is not waiting for an answer",
            }
            print(messages[outcome], file=out if outcome == "ok" else err)
            return {"ok": 0, "claimed": 4, "not_pending": 5}[outcome]
        if args.command == "ack":
            with store.transaction() as state:
                outcome = ack(state, args.number, args.result, args.note, clock(), args.head)
                tracked = state.prs.get(args.number)
            if outcome == "head_changed" and tracked is not None:
                print(
                    f"#{args.number} has a newer commit, {tracked.head}: look at what changed, "
                    "then answer with --head of that",
                    file=err,
                )
                return 6
            if outcome != "ok":
                print(f"#{args.number} is not waiting for an answer", file=err)
                return 5
            print(f"#{args.number}: {args.result}", file=out)
            return 0
        owner = repo.split("/")[0].lower()
        if not all(LOGIN.fullmatch(login) for login in args.trusted or []):
            print("pr-watch: --trusted takes a GitHub login", file=err)
            return 2
        cfg = Settings(
            repo=repo,
            trusted=frozenset(t.lower() for t in args.trusted or [owner]),
            base_branch=args.base,
            interval_s=max(10.0, args.interval),
            remind_after_s=max(60.0, args.remind_after_min * 60),
            include_open=args.include_open,
        )
        stop = threading.Event()
        previous: dict[int, Any] = {}
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGINT, signal.SIGTERM):
                previous[number] = signal.signal(number, lambda *_: stop.set())
        try:
            with store.single_watcher():
                return watch(
                    cfg, store, run=run, out=out, err=err, clock=clock, stop=stop, once=args.once
                )
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
    except StateError as exc:
        print(f"pr-watch: {exc}", file=err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
