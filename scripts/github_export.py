#!/usr/bin/env python3
"""Publish `main` and its tags to the private GitHub copy of this repository.

    .venv/bin/python scripts/github_export.py            # check everything, then push
    .venv/bin/python scripts/github_export.py --dry-run  # check everything, push nothing

The full history lives on `origin`. GitHub's copy differs in one way, on purpose, and this script
is the only thing that makes it differ: the commits that touch `.github/workflows` are rewritten
without those files, because the token that pushes has no `workflow` scope and GitHub refuses a
push that carries one. The rewrite gives the same commit ids every time it is run on the same
history, so what GitHub has stays an ancestor of what is published next and each push is a
fast-forward. When it is not (somebody pushed to GitHub, or the rewrite changed), the script
stops: it never forces a push and never moves a tag.

Before anything leaves this machine, what is new there is read, and any one finding stops it all:

- every author and committer address is a GitHub noreply address;
- no `.env`, key, certificate or database file, and no file over 5 MiB;
- nothing the owner keeps private: the patterns in the private config, and the values in the
  `.env` files it names (a value shorter than 12 characters is not looked for);
- `gitleaks` finds no secret in the new commits.

The private config is `~/.config/pa-export/config.toml` (or `--config`, or `$PA_EXPORT_CONFIG`).
It lives outside the repository on purpose: it is made of exactly what must not be published.

    denylist  = ["a-regex", "another"]       # matched without regard to case; never empty
    env_files = ["~/.personal-agent/.env"]   # whose values must never appear (`<repo>/.env` too)
    gitleaks  = "~/.local/share/pa-export/gitleaks"   # default: `gitleaks` on the PATH

What was found is reported by place (a file, a commit) and by which rule, never by value.
Exit status: 0 pushed or nothing to push, 1 refused, 2 this machine cannot check (nothing pushed).
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import IO

try:
    import tomllib
except ModuleNotFoundError:  # Python before 3.11, e.g. the system one
    print(
        "export: cannot check: needs Python 3.11+ (.venv/bin/python scripts/github_export.py)",
        file=sys.stderr,
    )
    sys.exit(2)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ".github/workflows"
NOREPLY = re.compile(r"[0-9]+\+[A-Za-z0-9-]+@users\.noreply\.github\.com", re.IGNORECASE)
FORBIDDEN_PATH = re.compile(
    r"(^|/)\.env($|\.(?!example$|sample$|template$))|\.(pem|key|p12|pfx|sqlite3?|db)$",
    re.IGNORECASE,
)
MAX_BLOB_BYTES = 5 * 1024 * 1024
MIN_NEEDLE = 12  # a shorter value would be found everywhere
MAX_LISTED = 5  # places named per finding
REMOTE_REF = "refs/export/remote"


class Refusal(Exception):
    """Something would be published that must not be, or the two copies disagree."""


class Setup(Exception):
    """This machine cannot do the checks, so nothing is pushed."""


@dataclasses.dataclass(frozen=True)
class Config:
    denylist: tuple[re.Pattern[bytes], ...]
    env_files: tuple[Path, ...]
    gitleaks: Path | None


# ---- running git -----------------------------------------------------------------------------
def tidy(raw: bytes | str) -> str:
    """A short, one-line error text without credentials (a URL may carry them)."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    text = re.sub(r"://[^/@\s]+@", "://", text)
    return re.sub(r"\s+", " ", text).strip()[:300]


def run(
    args: Sequence[str | Path],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    return subprocess.run(
        [str(a) for a in args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        check=False,
        env=environment,
    )


def git(*args: str, cwd: Path, env: Mapping[str, str] | None = None) -> str:
    proc = run(["git", *args], cwd=cwd, env=env)
    if proc.returncode != 0:
        raise Setup(f"git {args[0]} failed: {tidy(proc.stderr)}")
    return proc.stdout.decode("utf-8", "replace")


def is_ancestor(older: str, newer: str, cwd: Path) -> bool:
    proc = run(["git", "merge-base", "--is-ancestor", older, newer], cwd=cwd)
    if proc.returncode not in (0, 1):
        raise Setup(f"git merge-base failed: {tidy(proc.stderr)}")
    return proc.returncode == 0


# ---- the private config -----------------------------------------------------------------------
def default_config_path() -> Path:
    given = os.environ.get("PA_EXPORT_CONFIG")
    return Path(given) if given else Path.home() / ".config" / "pa-export" / "config.toml"


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Setup(
            f"{path} is missing. It lists what must never be published (see `--help`); "
            "without it nothing can be checked."
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise Setup(f"{path} is not valid TOML: {exc}") from None
    patterns = raw.get("denylist")
    if not (
        isinstance(patterns, list) and patterns and all(isinstance(p, str) and p for p in patterns)
    ):
        raise Setup(
            f"{path}: `denylist` must be a non-empty list of patterns (an empty one checks nothing)"
        )
    compiled = []
    for number, pattern in enumerate(patterns, 1):
        try:
            compiled.append(re.compile(pattern.encode(), re.IGNORECASE))
        except re.error as exc:
            raise Setup(
                f"{path}: denylist entry {number} is not a regular expression ({exc})"
            ) from None
    files = raw.get("env_files", [])
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise Setup(f"{path}: `env_files` must be a list of paths")
    binary = raw.get("gitleaks")
    return Config(
        denylist=tuple(compiled),
        env_files=tuple(Path(f).expanduser() for f in files),
        gitleaks=Path(binary).expanduser() if isinstance(binary, str) and binary else None,
    )


def env_needles(path: Path) -> dict[str, list[bytes]]:
    """The values in an `.env` file that are long enough to look for, by variable name."""
    found: dict[str, list[bytes]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        # A value may be quoted or followed by a comment; look for each reading of it.
        readings = {value, value.strip("'\""), re.split(r"\s+#", value)[0].strip().strip("'\"")}
        picked = sorted(v.encode() for v in readings if len(v) >= MIN_NEEDLE)
        if picked:
            found[name.strip()] = picked
    return found


def find_gitleaks(config: Config) -> Path:
    candidates = [config.gitleaks] if config.gitleaks else []
    on_path = shutil.which("gitleaks")
    if on_path:
        candidates.append(Path(on_path))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise Setup(
        'gitleaks is not installed (set `gitleaks = "/path"` in the config, or put it on the PATH)'
    )


# ---- building the copy that is published ------------------------------------------------------
def filtered_clone(source: Path, work: Path, branch: str) -> bool:
    """Clone `source` into `work` and rewrite it as GitHub is to see it. Whether it was rewritten."""
    git(
        "clone",
        "--quiet",
        "--no-local",
        "--no-hardlinks",
        "--branch",
        branch,
        str(source),
        str(work),
        cwd=source,
    )
    touching = git("rev-list", branch, "--", WORKFLOWS, cwd=work).split()
    if not touching:
        return False
    parents = git("rev-list", "--parents", "-n", "1", touching[-1], cwd=work).split()[1:]
    git(
        "-c", "commit.gpgsign=false",
        "filter-branch", "-f", "--prune-empty", "--tag-name-filter", "cat",
        "--index-filter", f"git rm -r --cached --ignore-unmatch -q -- {WORKFLOWS}",
        "--", branch, *[f"^{p}" for p in parents],
        cwd=work,
        env={"FILTER_BRANCH_SQUELCH_WARNING": "1"},
    )  # fmt: skip
    left = git("rev-list", branch, "--tags", "--", WORKFLOWS, cwd=work).split()
    if left:
        raise Refusal("the rewrite left commits that still carry workflow files")
    return True


def remote_state(url: str, branch: str, cwd: Path) -> tuple[str | None, dict[str, str]]:
    """The branch tip and the tags GitHub has now."""
    listing = git("ls-remote", "--refs", url, f"refs/heads/{branch}", "refs/tags/*", cwd=cwd)
    tip, tags = None, {}
    for line in listing.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{branch}":
            tip = sha
        elif ref.startswith("refs/tags/"):
            tags[ref.removeprefix("refs/tags/")] = sha
    return tip, tags


# ---- what is checked --------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Rules:
    """What must not appear: the private patterns, and the values of the named `.env` files."""

    denylist: tuple[re.Pattern[bytes], ...]
    needles: Mapping[str, list[bytes]]

    def matching(self, data: bytes) -> list[str]:
        labels = [f"denylist entry {n}" for n, p in enumerate(self.denylist, 1) if p.search(data)]
        labels += [
            f"the value of {name}"
            for name, found in self.needles.items()
            if any(value in data for value in found)
        ]
        return labels

    def shown(self, name: str) -> str:
        """A file name for a report, unless the name is itself one of the things kept private."""
        return "(a name that is private itself)" if self.matching(name.encode()) else name


def range_args(exclude: bool) -> list[str]:
    """What is new on GitHub: everything `main` and the tags reach, less what GitHub has."""
    return ["--branches", "--tags", *([f"^{REMOTE_REF}"] if exclude else [])]


def objects_of(work: Path, exclude: bool) -> dict[str, str]:
    """The new objects: object id -> a path it was found at ("" when it has none)."""
    listing = git("rev-list", "--objects", *range_args(exclude), cwd=work)
    found: dict[str, str] = {}
    for line in listing.splitlines():
        oid, _, path = line.partition(" ")
        found.setdefault(oid, path)
    return found


def read_objects(work: Path, oids: Sequence[str]) -> Iterator[tuple[str, str, bytes]]:
    if not oids:
        return
    proc = run(["git", "cat-file", "--batch"], cwd=work, stdin=("\n".join(oids) + "\n").encode())
    if proc.returncode != 0:
        raise Setup(f"git cat-file failed: {tidy(proc.stderr)}")
    data, at = proc.stdout, 0
    while at < len(data):
        end = data.index(b"\n", at)
        oid, kind, size = data[at:end].decode().split()
        start = end + 1
        yield oid, kind, data[start : start + int(size)]
        at = start + int(size) + 1


def place(oid: str, kind: str, paths: Mapping[str, str], rules: Rules) -> str:
    path = paths.get(oid, "")
    if path and not rules.matching(path.encode()):
        return path
    return f"{kind} {oid[:7]}"


def check_identities(work: Path, exclude: bool) -> list[str]:
    log = git("log", "--format=%h\t%ae\t%ce", *range_args(exclude), cwd=work)
    problems = []
    for line in log.splitlines():
        short, author, committer = line.split("\t")
        for role, address in (("author", author), ("committer", committer)):
            if not NOREPLY.fullmatch(address):
                problems.append(f"commit {short}: the {role} address is not a GitHub noreply one")
    return problems


def check_paths_and_sizes(
    work: Path, paths: Mapping[str, str], rules: Rules, exclude: bool
) -> list[str]:
    names = git("log", "-m", "--name-only", "--format=", "-z", *range_args(exclude), cwd=work)
    forbidden = sorted({n for n in names.split("\0") if n and FORBIDDEN_PATH.search(n)})
    problems = [
        f"a file that must not be published: {rules.shown(n)}" for n in forbidden[:MAX_LISTED]
    ]
    if paths:
        sizes = run(
            ["git", "cat-file", "--batch-check=%(objecttype) %(objectname) %(objectsize)"],
            cwd=work,
            stdin=("\n".join(paths) + "\n").encode(),
        )
        for line in sizes.stdout.decode().splitlines():
            kind, oid, size = line.split()
            if kind == "blob" and int(size) > MAX_BLOB_BYTES:
                problems.append(
                    f"{place(oid, kind, paths, rules)} is {int(size) // 1024} KiB "
                    f"(the limit is {MAX_BLOB_BYTES // 1024})"
                )
    return problems


def check_contents(work: Path, paths: Mapping[str, str], rules: Rules) -> list[str]:
    hits: dict[str, list[str]] = {}
    for oid, kind, content in read_objects(work, list(paths)):
        for label in rules.matching(content):
            hits.setdefault(label, []).append(place(oid, kind, paths, rules))
    return [
        f"{label} is in {len(places)} place(s): {', '.join(sorted(set(places))[:MAX_LISTED])}"
        for label, places in hits.items()
    ]


def check_secrets(binary: Path, work: Path, exclude: bool) -> list[str]:
    proc = run(
        [binary, "git", "--no-banner", "--no-color", "--redact", "--exit-code", "1",
         f"--log-opts={' '.join(range_args(exclude))}", "--gitleaks-ignore-path", work, work],
        cwd=work,
    )  # fmt: skip
    if proc.returncode == 0:
        return []
    if proc.returncode != 1:
        raise Setup(f"gitleaks failed to run (exit {proc.returncode}): {tidy(proc.stderr)}")
    report = proc.stderr.decode("utf-8", "replace") + proc.stdout.decode("utf-8", "replace")
    count = re.search(r"leaks found: (\d+)", report)
    return [
        f"gitleaks found {count.group(1) if count else 'a'} possible secret(s): "
        "run it by hand to see where"
    ]


# ---- the whole thing --------------------------------------------------------------------------
def export(
    *,
    remote: str,
    branch: str,
    config_path: Path,
    dry_run: bool,
    out: IO[str],
    source: Path = ROOT,
) -> int:
    def say(message: str) -> None:
        print(f"export: {message}", file=out, flush=True)

    config = load_config(config_path)
    binary = find_gitleaks(config)
    env_files = [*(f for f in [source / ".env"] if f.is_file()), *config.env_files]
    missing = [str(f) for f in env_files if not f.is_file()]
    if missing:
        raise Setup(f"env_files lists a file that is not there: {', '.join(missing)}")
    env = {name: values for f in env_files for name, values in env_needles(f).items()}
    url = git("remote", "get-url", remote, cwd=source).strip()
    tip_there, tags_there = remote_state(url, branch, source)

    with tempfile.TemporaryDirectory(prefix="pa-export-") as scratch:
        work = Path(scratch) / "work"
        rewritten = filtered_clone(source, work, branch)
        new_tip = git("rev-parse", branch, cwd=work).strip()
        say(
            f"{branch} is {new_tip[:7]} here ({'workflow files left out' if rewritten else 'as it is'})"
        )

        exclude = tip_there is not None
        if tip_there:
            git("fetch", "--quiet", "--no-tags", url, f"{branch}:{REMOTE_REF}", cwd=work)
            if tip_there == new_tip:
                say(f"GitHub already has {new_tip[:7]}")
            elif is_ancestor(new_tip, tip_there, work):
                raise Refusal(
                    f"GitHub is ahead: {tip_there[:7]} has commits that are not here. Bring them in "
                    "first (docs/PR_PLAYBOOK.md), then export again."
                )
            elif not is_ancestor(tip_there, new_tip, work):
                raise Refusal(
                    f"GitHub's {branch} ({tip_there[:7]}) is not an ancestor of what would be pushed "
                    f"({new_tip[:7]}): pushed from somewhere else, or the history was rewritten. "
                    "Nothing was pushed; decide by hand."
                )
        local_tags = {}
        listing = git(
            "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/tags", cwd=work
        )
        for line in listing.splitlines():
            name, sha = line.split()
            local_tags[name] = sha
        moved = sorted(
            n for n, sha in local_tags.items() if n in tags_there and tags_there[n] != sha
        )
        if moved:
            raise Refusal(f"a tag is never moved, and these differ on GitHub: {', '.join(moved)}")
        new_tags = sorted(n for n in local_tags if n not in tags_there)
        if tip_there == new_tip and not new_tags:
            say("nothing to push")
            return 0

        paths = objects_of(work, exclude)
        rules = Rules(config.denylist, env)
        findings = [
            *check_identities(work, exclude),
            *check_paths_and_sizes(work, paths, rules, exclude),
            *check_contents(work, paths, rules),
            *check_secrets(binary, work, exclude),
        ]
        if findings:
            raise Refusal(
                f"{len(findings)} finding(s); nothing was pushed:\n  " + "\n  ".join(findings)
            )
        commits = git(
            "log",
            "--oneline",
            "--no-decorate",
            branch,
            *([f"^{REMOTE_REF}"] if exclude else []),
            cwd=work,
        )
        say(f"checked {len(paths)} new object(s): no finding")
        for line in commits.splitlines():
            say(f"  {line}")
        if new_tags:
            say(f"new tags: {', '.join(new_tags)}")
        if dry_run:
            say("dry run: nothing was pushed")
            return 0

        if tip_there != new_tip:
            pushed = run(
                ["git", "push", "--porcelain", url, f"{new_tip}:refs/heads/{branch}"], cwd=work
            )
            if pushed.returncode != 0:
                raise Refusal(
                    f"GitHub did not take the push: {tidy(pushed.stderr or pushed.stdout)}"
                )
        if new_tags:
            specs = [f"refs/tags/{n}:refs/tags/{n}" for n in new_tags]
            pushed = run(["git", "push", "--porcelain", url, *specs], cwd=work)
            if pushed.returncode != 0:
                raise Refusal(
                    f"the branch is pushed, the tags were not: {tidy(pushed.stderr or pushed.stdout)}"
                )

    fetched = run(
        [
            "git",
            "fetch",
            "--quiet",
            "--no-tags",
            remote,
            f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
        ],
        cwd=source,
    )
    if fetched.returncode != 0:
        say(f"pushed, but {remote}/{branch} here could not be updated: {tidy(fetched.stderr)}")
    say(f"pushed {new_tip[:7]} to {remote}/{branch}")
    return 0


def main(
    argv: Sequence[str] | None = None, *, out: IO[str] | None = None, err: IO[str] | None = None
) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="github_export.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--remote", default="github", help="the git remote that is GitHub's copy")
    parser.add_argument("--branch", default="main")
    parser.add_argument(
        "--config", type=Path, help="the private config (default: see --help of the script text)"
    )
    parser.add_argument("--dry-run", action="store_true", help="check everything, push nothing")
    parser.add_argument(
        "--source", type=Path, default=ROOT, help="the checkout to publish (default: this one)"
    )
    args = parser.parse_args(argv)
    try:
        return export(
            remote=args.remote,
            branch=args.branch,
            config_path=args.config or default_config_path(),
            dry_run=args.dry_run,
            out=out,
            source=args.source,
        )
    except Refusal as exc:
        print(f"export: refused: {exc}", file=err)
        return 1
    except Setup as exc:
        print(f"export: cannot check: {exc}", file=err)
        return 2


if __name__ == "__main__":
    sys.exit(main())
