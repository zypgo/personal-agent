"""scripts/github_export.py: what may leave this machine for GitHub, and how.

Real `git` works on synthetic repositories (a local one, and a bare one that plays GitHub), so
what is checked is what git does: the rewrite that leaves the workflow files out must be the same
every time, a push must always be a fast-forward, and a finding of any kind must leave the remote
as it was. Nothing here touches the network or the owner's real private list; `gitleaks` is a
stand-in that records how it was called (one test uses the real one when it is installed).
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOOD = "1+owner@users.noreply.github.com"
SECRET = "quokka-private-7"  # what the synthetic config keeps private
REAL_GITLEAKS = shutil.which("gitleaks") or str(
    Path.home() / ".local" / "share" / "pa-export" / "gitleaks"
)  # looked up now: the tests below change HOME


def _load():
    spec = importlib.util.spec_from_file_location(
        "github_export", ROOT / "scripts" / "github_export.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ge = _load()


def sh(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **(env or {})},
    )
    return done.stdout


class World:
    """A local repository, GitHub's copy of it (a bare repository), and a clock for commits."""

    def __init__(self, base: Path, stand_in: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.base, self.local, self.remote = base, base / "local", base / "github.git"
        self.stand_in, self.monkeypatch = stand_in, monkeypatch
        self.when = 1_700_000_000
        self.config = base / "config.toml"
        self.gitleaks_log = base / "gitleaks-args"
        base.mkdir(parents=True)
        sh("init", "-q", "-b", "main", str(self.local), cwd=base)
        sh("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=base)
        sh("remote", "add", "github", str(self.remote), cwd=self.local)
        self.configure(gitleaks_exit=0)

    # -- setting up
    def configure(
        self,
        *,
        denylist=("quokka-private-[0-9]+",),
        env_files=(),
        gitleaks_exit=0,
        binary=None,
        gitleaks_config=None,
    ):
        if binary is None:  # the stand-in is told by the environment what to do and where to write
            binary = self.stand_in
            self.monkeypatch.setenv("FAKE_GITLEAKS_EXIT", str(gitleaks_exit))
            self.monkeypatch.setenv("FAKE_GITLEAKS_LOG", str(self.gitleaks_log))
        lines = [
            f"denylist = {json.dumps(list(denylist))}",
            f"env_files = {json.dumps([str(f) for f in env_files])}",
            f"gitleaks = {json.dumps(str(binary))}",
        ]
        if gitleaks_config is not None:
            lines.append(f"gitleaks_config = {json.dumps(str(gitleaks_config))}")
        self.config.write_text("\n".join(lines) + "\n")

    def commit(self, message, files=None, *, author=GOOD, committer=None) -> str:
        for name, content in (files or {}).items():
            path = self.local / name
            if content is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content if isinstance(content, bytes) else content.encode())
        self.when += 60
        stamp = f"{self.when} +0000"
        env = {
            "GIT_AUTHOR_NAME": "Owner", "GIT_AUTHOR_EMAIL": author, "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_NAME": "Owner", "GIT_COMMITTER_EMAIL": committer or author,
            "GIT_COMMITTER_DATE": stamp,
        }  # fmt: skip
        sh("add", "-A", cwd=self.local)
        sh("commit", "-q", "--allow-empty", "-m", message, cwd=self.local, env=env)
        return sh("rev-parse", "HEAD", cwd=self.local).strip()

    def tag(self, name: str, *, annotated: bool = False) -> None:
        env = {"GIT_COMMITTER_NAME": "Owner", "GIT_COMMITTER_EMAIL": GOOD,
               "GIT_COMMITTER_DATE": f"{self.when} +0000"}  # fmt: skip
        args = ["tag", "-a", "-m", f"release {name}", name] if annotated else ["tag", name]
        sh(*args, cwd=self.local, env=env)

    def plain(self) -> None:
        """one (tagged t1), two (t2), three: nothing in it has to be rewritten."""
        self.commit("one", {"README.md": "one\n"})
        self.tag("t1", annotated=True)
        self.commit("two", {"src.py": "print(2)\n"})
        self.tag("t2")
        self.commit("three", {"src.py": "print(3)\n"})

    def standard(self) -> None:
        """one (tagged t1), a commit that only adds the workflow, two (t2), three."""
        self.commit("one", {"README.md": "one\n"})
        self.tag("t1", annotated=True)
        self.commit("add the workflow", {".github/workflows/check.yml": "on: push\n"})
        self.commit("two", {"src.py": "print(2)\n"})
        self.tag("t2")
        self.commit("three", {"src.py": "print(3)\n"})

    # -- running and looking
    def export(self, *extra: str):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--source", str(self.local), "--config", str(self.config), *extra]
        code = ge.main(argv, out=out, err=err)
        return SimpleNamespace(code=code, out=out.getvalue(), err=err.getvalue())

    def at_remote(self, *args: str) -> str:
        return sh(*args, cwd=self.remote)

    def remote_tip(self) -> str | None:
        done = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "refs/heads/main"],
            cwd=self.remote, capture_output=True, text=True, check=False,
        )  # fmt: skip
        return done.stdout.strip() or None

    def remote_subjects(self) -> list[str]:
        return self.at_remote("log", "--reverse", "--format=%s", "main").splitlines()

    def remote_tags(self) -> set[str]:
        return set(self.at_remote("tag", "-l").split())

    def gitleaks_args(self) -> list[str]:
        return self.gitleaks_log.read_text().splitlines()

    def gitleaks_config_seen(self) -> str:
        return Path(f"{self.gitleaks_log}.config").read_text()


@pytest.fixture(scope="session")
def stand_in_gitleaks(tmp_path_factory) -> Path:
    """One script for the whole run: a new executable costs the system a check each time it is
    first run, and this one is run in every test."""
    path = tmp_path_factory.mktemp("stand-in") / "gitleaks"
    path.write_text(
        "#!/bin/sh\n"
        # what it was asked, the config it was given (the scratch copy is gone afterwards), and
        # whether the directory it was run on (the last argument) has an ignore file
        'printf \'%s\\n\' "$@" > "$FAKE_GITLEAKS_LOG"\n'
        "prev=\n"
        "for a; do\n"
        '  [ "$prev" = --config ] && cp "$a" "$FAKE_GITLEAKS_LOG.config"\n'
        "  prev=$a; last=$a\n"
        "done\n"
        '[ -e "$last/.gitleaksignore" ] && echo "ignore file present" >> "$FAKE_GITLEAKS_LOG"\n'
        '[ "$FAKE_GITLEAKS_EXIT" = 1 ] && [ -z "$FAKE_GITLEAKS_QUIET" ]'
        ' && echo "WRN leaks found: 1" >&2\n'
        'exit "${FAKE_GITLEAKS_EXIT:-0}"\n'
    )
    path.chmod(0o755)
    return path


@pytest.fixture
def world(tmp_path, monkeypatch, stand_in_gitleaks):
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)  # a hook that runs the tests must not steer these repositories
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("PA_EXPORT_CONFIG", raising=False)
    return World(tmp_path / "world", stand_in_gitleaks, monkeypatch)


# ---- the rewrite and the push -----------------------------------------------------------------
def test_the_first_export_publishes_the_history_without_the_workflow_files(world):
    world.standard()

    result = world.export()

    assert result.code == 0, result.err
    assert world.remote_subjects() == [
        "one",
        "two",
        "three",
    ]  # the workflow-only commit left no trace
    assert world.at_remote("log", "main", "--format=%h", "--", ".github/workflows") == ""
    assert ".github" not in world.at_remote("ls-tree", "-r", "--name-only", "t2")
    assert world.remote_tags() == {"t1", "t2"}
    assert world.at_remote("log", "-1", "--format=%s", "t2").strip() == "two"
    assert world.at_remote("rev-parse", "t1") == sh("rev-parse", "t1", cwd=world.local)  # untouched
    assert (
        sh("rev-parse", "refs/remotes/github/main", cwd=world.local).strip() == world.remote_tip()
    )
    assert sh("status", "--porcelain", cwd=world.local) == ""  # what is here is as it was
    assert (world.local / ".github" / "workflows" / "check.yml").is_file()


def test_a_later_export_adds_only_what_is_new_and_nothing_old_changes(world):
    world.standard()
    assert world.export().code == 0
    before = world.remote_tip()
    changes = {"src.py": "print(4)\n", ".github/workflows/check.yml": "on: [push, pull_request]\n"}
    world.commit("four", changes)

    result = world.export()

    assert result.code == 0, result.err
    assert world.remote_subjects() == ["one", "two", "three", "four"]
    assert world.at_remote("rev-list", "--count", f"{before}..main").strip() == "1"
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", before, "main"], cwd=world.remote, check=True
    )
    assert "pushed" in result.out


def test_exporting_again_with_nothing_new_pushes_nothing(world):
    world.standard()
    world.export()
    tip = world.remote_tip()

    result = world.export()

    assert (result.code, "nothing to push" in result.out) == (0, True)
    assert world.remote_tip() == tip


def test_a_commit_that_only_touches_the_workflows_changes_nothing_on_github(world):
    world.standard()
    world.export()
    tip = world.remote_tip()
    world.commit("tune the workflow", {".github/workflows/check.yml": "on: [push]\n"})

    result = world.export()

    assert (result.code, "nothing to push" in result.out) == (0, True)
    assert world.remote_tip() == tip


def test_a_new_tag_alone_is_pushed(world):
    world.standard()
    world.export()
    tip = world.remote_tip()
    world.tag("t3")

    result = world.export()

    assert result.code == 0, result.err
    assert world.remote_tags() == {"t1", "t2", "t3"} and world.remote_tip() == tip


def test_a_history_without_workflow_files_is_published_as_it_is(world):
    world.commit("one", {"README.md": "one\n"})
    world.commit("two", {"a.txt": "a\n"})

    result = world.export()

    assert result.code == 0, result.err
    assert world.remote_tip() == sh("rev-parse", "main", cwd=world.local).strip()  # the same ids
    assert "as it is" in result.out


def test_a_dry_run_checks_everything_and_pushes_nothing(world):
    world.plain()

    result = world.export("--dry-run")

    assert (result.code, world.remote_tip()) == (0, None)
    assert "dry run" in result.out and "three" in result.out
    world.commit("leak", {"notes.txt": f"call {SECRET}\n"})
    assert world.export("--dry-run").code == 1  # and it refuses what a real run would refuse


# ---- what stops it ----------------------------------------------------------------------------
@pytest.mark.parametrize("where", ["content", "message", "name"])
def test_what_the_owner_keeps_private_is_found_wherever_it_is(world, where):
    world.plain()
    assert world.export().code == 0
    before = world.remote_tip()
    if where == "content":
        world.commit("add notes", {"notes.txt": f"call {SECRET}\n"})
    elif where == "message":
        world.commit(f"thanks to {SECRET}", {"notes.txt": "x\n"})
    else:
        world.commit("add notes", {f"{SECRET}.txt": "x\n"})

    result = world.export()

    assert result.code == 1
    assert "denylist entry 1" in result.err
    assert SECRET not in result.err + result.out  # a finding is told by place, never by value
    assert world.remote_tip() == before


def test_a_private_word_added_and_removed_again_within_what_is_new_is_still_found(world):
    world.plain()
    world.export()
    world.commit("oops", {"notes.txt": f"call {SECRET}\n"})
    world.commit("undo", {"notes.txt": None})

    assert world.export().code == 1


def test_the_place_of_a_finding_is_named_when_it_is_a_file(world):
    world.commit("one", {"README.md": "one\n"})
    world.commit("add notes", {"docs/notes.txt": f"call {SECRET}\n"})

    result = world.export()

    assert result.code == 1 and "docs/notes.txt" in result.err and SECRET not in result.err
    assert world.remote_tip() is None


@pytest.mark.parametrize("role", ["author", "committer"])
def test_a_commit_with_a_real_address_is_refused(world, role):
    world.plain()
    world.export()
    real = "someone@example.org"
    kwargs = {"author": real} if role == "author" else {"committer": real}
    world.commit("four", {"a.txt": "a\n"}, **kwargs)

    result = world.export()

    assert result.code == 1 and f"the {role} address" in result.err
    assert real not in result.err + result.out


def test_a_value_from_an_env_file_is_found_but_a_short_one_is_not(world, tmp_path):
    env_file = tmp_path / "keys.env"
    env_file.write_text("export API_KEY='sk-live-0123456789abcdef'\nPIN=1234\n")
    world.configure(env_files=[env_file])
    world.plain()
    assert world.export().code == 0
    world.commit("a pin", {"a.txt": "1234\n"})
    assert world.export().code == 0
    world.commit("a key", {"b.txt": "token sk-live-0123456789abcdef\n"})

    result = world.export()

    assert result.code == 1 and "the value of API_KEY" in result.err
    assert "sk-live-0123456789abcdef" not in result.err + result.out


def test_the_env_file_of_the_checkout_itself_is_always_looked_at(world):
    (world.local / ".env").write_text("TOKEN=checkout-token-0123456789\n")
    world.commit("one", {"README.md": "one\n"})
    world.commit("careless", {"notes.txt": "checkout-token-0123456789\n"})

    result = world.export()

    assert result.code == 1 and "the value of TOKEN" in result.err


@pytest.mark.parametrize(
    "name", [".env", "app/.env.local", "id.pem", "tls/server.key", "data/app.sqlite3", "cache.db"]
)
def test_files_that_must_not_be_published_are_refused(world, name):
    world.commit("one", {"README.md": "one\n"})
    sh("add", "-f", "-A", cwd=world.local)  # as if somebody forced past .gitignore
    world.commit("careless", {name: "nothing in it\n"})

    result = world.export()

    assert result.code == 1 and name in result.err
    assert world.remote_tip() is None


@pytest.mark.parametrize("name", [".env.example", ".env.sample", "docs/env.md", "keys.md"])
def test_an_example_of_an_env_file_is_fine(world, name):
    world.commit("one", {name: "KEY=\n"})

    assert world.export().code == 0


def test_a_big_file_is_refused(world, monkeypatch):
    monkeypatch.setattr(ge, "MAX_BLOB_BYTES", 1000)
    world.commit("one", {"README.md": "one\n"})
    world.commit("big", {"data/big.bin": b"x" * 5000})

    result = world.export()

    assert result.code == 1 and "data/big.bin" in result.err and "KiB" in result.err


# ---- gitleaks ---------------------------------------------------------------------------------
def test_gitleaks_is_asked_about_what_is_new_only(world):
    world.plain()
    world.export()
    world.commit("four", {"a.txt": "a\n"})

    assert world.export().code == 0

    args = world.gitleaks_args()
    assert args[0] == "git" and "--redact" in args and "--no-banner" in args
    assert "--log-opts=--branches --tags ^refs/export/remote" in args
    assert "pa-export-" in args[-1]  # the scratch copy of the repository is the last argument


def test_gitleaks_is_given_its_own_rules_unless_the_owner_gave_others(world):
    world.plain()

    assert world.export().code == 0

    args = world.gitleaks_args()
    given = Path(args[args.index("--config") + 1])
    assert given.parent == Path(args[-1]).parent  # beside the copy of the repository, not in it
    assert world.gitleaks_config_seen() == "[extend]\nuseDefault = true\n"


def test_gitleaks_is_given_the_owners_rules_when_there_are_some(world):
    mine = Path(os.environ["HOME"]) / "mine.toml"  # the test's own home (see the fixture)
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text("[extend]\nuseDefault = true\n# the owner's\n")
    world.configure(gitleaks_config="~/mine.toml")  # written the way the owner writes it
    world.plain()

    assert world.export().code == 0

    args = world.gitleaks_args()
    assert args[args.index("--config") + 1] == str(mine)
    assert world.gitleaks_config_seen().endswith("# the owner's\n")


def test_nothing_that_comes_with_the_commits_can_loosen_the_check(world):
    world.plain()
    world.commit("hide it", {".gitleaksignore": "a:b:c:1\n", ".gitleaks.toml": "x = 1\n"})

    assert world.export().code == 0

    args = world.gitleaks_args()
    assert "--ignore-gitleaks-allow" in args  # a comment in a file is not the owner's word
    assert "ignore file present" not in args  # the copy it is run in has no ignore file


def test_a_secret_that_gitleaks_finds_stops_everything(world):
    world.configure(gitleaks_exit=1)
    world.plain()

    result = world.export()

    assert result.code == 1 and "gitleaks found 1" in result.err
    assert world.remote_tip() is None


def test_gitleaks_that_cannot_run_means_nothing_is_pushed(world):
    world.configure(gitleaks_exit=3)
    world.plain()

    result = world.export()

    assert result.code == 2 and "gitleaks failed to run" in result.err
    assert world.remote_tip() is None


def test_an_exit_that_is_not_followed_by_a_report_is_not_taken_for_a_finding(world, monkeypatch):
    """gitleaks exits 1 for a finding, and also for a config it cannot read."""
    world.configure(gitleaks_exit=1)
    monkeypatch.setenv("FAKE_GITLEAKS_QUIET", "1")
    world.plain()

    result = world.export()

    assert result.code == 2 and "gitleaks failed to run (exit 1)" in result.err
    assert world.remote_tip() is None


def test_without_gitleaks_nothing_is_pushed(world, monkeypatch):
    world.configure(binary=world.base / "not-there")
    monkeypatch.setattr(ge.shutil, "which", lambda name: None)
    world.plain()

    result = world.export()

    assert result.code == 2 and "gitleaks is not installed" in result.err
    assert world.remote_tip() is None


@pytest.mark.skipif(not os.access(REAL_GITLEAKS, os.X_OK), reason="gitleaks is not installed")
def test_the_real_gitleaks_lets_clean_work_through_and_stops_a_token(world):
    world.configure(binary=Path(REAL_GITLEAKS))
    token = "ghp_" + hashlib.sha256(b"a made-up seed").hexdigest()[:36]
    world.plain()
    assert world.export().code == 0
    world.commit("clean", {"a.txt": "hello\n"})
    assert world.export().code == 0
    world.commit("leak", {"cfg.txt": f"token = {token}\n"})
    before = world.remote_tip()

    result = world.export()

    assert result.code == 1 and "gitleaks found 1" in result.err
    assert token not in result.err + result.out
    assert world.remote_tip() == before


def made_up_token(seed: bytes = b"a made-up seed") -> str:
    return "ghp_" + hashlib.sha256(seed).hexdigest()[:36]


needs_gitleaks = pytest.mark.skipif(
    not os.access(REAL_GITLEAKS, os.X_OK), reason="gitleaks is not installed"
)


# What a commit can bring with it to hide a token from the check that reads that commit. Each of
# these let the token through when gitleaks was left to find its own config and ignore file.
@needs_gitleaks
@pytest.mark.parametrize("how", ["config", "comment", "ignore file"])
def test_a_commit_cannot_hide_its_own_token_from_the_check(world, how):
    world.configure(binary=Path(REAL_GITLEAKS))
    token = made_up_token()
    line = f'token = "{token}"\n'
    world.plain()
    assert world.export().code == 0
    before = world.remote_tip()
    if how == "config":
        world.commit(
            "leak", {"cfg.txt": line, ".gitleaks.toml": '[allowlist]\npaths = ["cfg.txt"]\n'}
        )
    elif how == "comment":
        world.commit("leak", {"cfg.txt": f"{line.strip()}  # gitleaks:allow\n"})
    else:  # the commit is named by the id it has once pushed: no rewrite here, so the same one
        leak = world.commit("leak", {"cfg.txt": line})
        world.commit("hide it", {".gitleaksignore": f"{leak}:cfg.txt:github-pat:1\n"})

    result = world.export()

    assert result.code == 1 and "gitleaks found" in result.err, result.err
    assert token not in result.err + result.out
    assert world.remote_tip() == before


@needs_gitleaks
def test_the_owner_can_let_one_known_value_through_and_no_other(world, tmp_path):
    known, other = made_up_token(b"known"), made_up_token(b"other")
    mine = tmp_path / "mine.toml"
    mine.write_text(f"[extend]\nuseDefault = true\n[[allowlists]]\nregexes = ['''{known}''']\n")
    world.configure(binary=Path(REAL_GITLEAKS), gitleaks_config=mine)
    world.plain()
    assert world.export().code == 0
    world.commit("a value the owner vouched for", {"a.txt": f'token = "{known}"\n'})
    assert world.export().code == 0, "the owner's allowance was not honoured"
    world.commit("another one", {"b.txt": f'token = "{other}"\n'})

    result = world.export()

    assert result.code == 1 and "gitleaks found" in result.err


@needs_gitleaks
def test_a_gitleaks_config_that_cannot_be_read_is_not_taken_for_a_finding(world, tmp_path):
    broken = tmp_path / "broken.toml"
    broken.write_text("this is [ not toml\n")
    world.configure(binary=Path(REAL_GITLEAKS), gitleaks_config=broken)
    world.plain()

    result = world.export()

    assert result.code == 2 and "gitleaks failed to run" in result.err
    assert world.remote_tip() is None


# ---- when the two copies disagree -------------------------------------------------------------
def push_from_elsewhere(world, tmp_path, *, rewrite: bool) -> None:
    other = tmp_path / "elsewhere"
    if rewrite:  # a history that has nothing to do with this one
        sh("init", "-q", "-b", "main", str(other), cwd=tmp_path)
    else:
        sh("clone", "-q", str(world.remote), str(other), cwd=tmp_path)
    env = {"GIT_AUTHOR_NAME": "X", "GIT_AUTHOR_EMAIL": GOOD, "GIT_COMMITTER_NAME": "X",
           "GIT_COMMITTER_EMAIL": GOOD}  # fmt: skip
    (other / "x.txt").write_text("x\n")
    sh("add", "-A", cwd=other)
    sh("commit", "-q", "-m", "made on GitHub", cwd=other, env=env)
    sh("push", "-q", "--force", str(world.remote), "HEAD:refs/heads/main", cwd=other)


def test_github_being_ahead_stops_the_export(world, tmp_path):
    world.plain()
    world.export()
    push_from_elsewhere(world, tmp_path, rewrite=False)
    tip = world.remote_tip()

    result = world.export()

    assert result.code == 1 and "GitHub is ahead" in result.err
    assert world.remote_tip() == tip


def test_commits_on_both_sides_stop_the_export(world, tmp_path):
    world.plain()
    world.export()
    push_from_elsewhere(world, tmp_path, rewrite=False)
    world.commit("four", {"a.txt": "a\n"})
    tip = world.remote_tip()

    result = world.export()

    assert result.code == 1 and "not an ancestor" in result.err
    assert world.remote_tip() == tip


def test_a_history_that_is_not_ours_is_never_overwritten(world, tmp_path):
    world.plain()
    push_from_elsewhere(world, tmp_path, rewrite=True)
    tip = world.remote_tip()

    result = world.export()

    assert result.code == 1 and "not an ancestor" in result.err
    assert world.remote_tip() == tip


def test_a_tag_is_never_moved(world):
    world.plain()
    world.export()
    first = world.at_remote("rev-list", "--max-parents=0", "main").strip()
    world.at_remote("tag", "-f", "t2", first)
    world.commit("four", {"a.txt": "a\n"})
    tip = world.remote_tip()

    result = world.export()

    assert result.code == 1 and "never moved" in result.err and "t2" in result.err
    assert world.remote_tip() == tip


def test_a_push_is_never_forced_even_when_github_moved_after_the_check(
    world, tmp_path, monkeypatch
):
    world.plain()
    world.export()
    seen = world.remote_tip()
    world.commit("four", {"a.txt": "a\n"})
    push_from_elsewhere(world, tmp_path, rewrite=False)  # GitHub moves on ...
    moved = world.remote_tip()
    real = ge.remote_state
    # ... after the script has looked at it: only the server can still say no
    monkeypatch.setattr(ge, "remote_state", lambda *a, **k: (seen, real(*a, **k)[1]))

    result = world.export()

    assert result.code == 1 and "did not take the push" in result.err
    assert world.remote_tip() == moved


# ---- when GitHub makes commits of its own ------------------------------------------------------
SIGNATURE = "-----BEGIN PGP SIGNATURE-----\n\n{body}\n-----END PGP SIGNATURE-----"


def github_merges_a_pull_request(world, tmp_path, message="Merge pull request #4 from x/y") -> str:
    """A commit GitHub created and signed itself, pushed on top of what the export published."""
    web = tmp_path / "web"
    sh("clone", "-q", str(world.remote), str(web), cwd=tmp_path)
    env = {"GIT_AUTHOR_NAME": "Owner", "GIT_AUTHOR_EMAIL": GOOD, "GIT_COMMITTER_NAME": "GitHub",
           "GIT_COMMITTER_EMAIL": "noreply@github.com"}  # fmt: skip
    (web / "from-github.txt").write_text("made on the web\n")
    sh("add", "-A", cwd=web)
    sh("commit", "-q", "-m", message, cwd=web, env=env)
    raw = sh("cat-file", "commit", "HEAD", cwd=web)
    head, _, text = raw.partition("\n\n")
    armour = "\n ".join(SIGNATURE.format(body="not a real signature").splitlines())
    signed = f"{head}\ngpgsig {armour}\n\n{text}"
    done = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=web, input=signed, capture_output=True, text=True, check=True,
    )  # fmt: skip
    oid = done.stdout.strip()
    sh("update-ref", "refs/heads/main", oid, cwd=web)
    sh("push", "-q", str(world.remote), "HEAD:refs/heads/main", cwd=web)
    return oid


def owner_merges_github(world) -> None:
    """What the owner does on this side: fetch GitHub's main and merge it, keeping the workflow."""
    sh("fetch", "-q", "github", cwd=world.local)
    env = {"GIT_AUTHOR_NAME": "Owner", "GIT_AUTHOR_EMAIL": GOOD, "GIT_COMMITTER_NAME": "Owner",
           "GIT_COMMITTER_EMAIL": GOOD}  # fmt: skip
    sh("merge", "-q", "--no-edit", "--no-ff", "refs/remotes/github/main", cwd=world.local, env=env)


def test_commits_github_made_itself_stay_as_they_are_when_the_next_export_goes_out(world, tmp_path):
    world.standard()
    assert world.export().code == 0
    theirs = github_merges_a_pull_request(world, tmp_path)
    owner_merges_github(world)
    world.commit(
        "four", {"src.py": "print(4)\n", ".github/workflows/check.yml": "on: [push, pr]\n"}
    )

    result = world.export()

    assert result.code == 0, result.err
    assert world.at_remote("cat-file", "commit", theirs).count("gpgsig ") == 1  # signature kept
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", theirs, "main"], cwd=world.remote, check=True
    )
    assert world.at_remote("log", "-1", "--format=%s", "main").strip() == "four"
    assert world.at_remote("log", "main", "--format=%h", "--", ".github/workflows") == ""
    assert "Merge pull request" not in result.out  # GitHub's own commit is not published again


def test_a_tag_on_a_commit_github_made_is_not_taken_for_a_moved_tag(world, tmp_path):
    world.standard()
    assert world.export().code == 0
    theirs = github_merges_a_pull_request(world, tmp_path)
    world.at_remote("tag", "release", theirs)
    sh("fetch", "-q", "github", "refs/tags/release:refs/tags/release", cwd=world.local)
    owner_merges_github(world)
    world.commit("four", {"src.py": "print(4)\n"})

    result = world.export()

    assert result.code == 0, result.err
    assert world.at_remote("rev-parse", "release").strip() == theirs
    assert world.at_remote("log", "-1", "--format=%s", "main").strip() == "four"


def test_what_github_made_is_not_rewritten_even_when_nothing_of_ours_has_to_change(world, tmp_path):
    world.standard()
    assert world.export().code == 0
    theirs = github_merges_a_pull_request(world, tmp_path)
    owner_merges_github(world)  # the merge keeps the workflow file, so it has to be rewritten

    result = world.export()

    assert result.code == 0, result.err
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", theirs, "main"], cwd=world.remote, check=True
    )
    assert world.at_remote("cat-file", "-p", "main^{tree}") == world.at_remote(
        "cat-file", "-p", f"{theirs}^{{tree}}"
    )  # the same files as GitHub already had: only the way they got there is new


def test_a_rewrite_that_left_the_workflow_files_in_stops_the_export(world, monkeypatch):
    real = ge.git
    monkeypatch.setattr(ge, "git", lambda *a, **k: "" if "filter-branch" in a else real(*a, **k))
    world.standard()

    result = world.export()

    assert result.code == 1 and "workflow files" in result.err
    assert world.remote_tip() is None


# ---- what cannot be trusted -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("content", "why"),
    [
        (None, "is missing"),
        ("denylist = []", "non-empty"),
        ('denylist = ["("]', "not a regular expression"),
        ("this is [ not toml", "not valid TOML"),
        ('denylist = ["x"]\nenv_files = 3', "list of paths"),
        ('denylist = ["x"]\nenv_files = ["/nonexistent/keys.env"]', "not there"),
        ('denylist = ["x"]\ngitleaks_config = 3', "path of a gitleaks config file"),
        ('denylist = ["x"]\ngitleaks_config = ""', "path of a gitleaks config file"),
        ('denylist = ["x"]\ngitleaks_config = "/nonexistent/gl.toml"', "names a file that is not"),
    ],
)
def test_a_config_that_cannot_be_relied_on_means_nothing_is_pushed(world, content, why):
    if content is None:
        world.config.unlink()
    else:  # a good gitleaks everywhere, so that the fault under test is the only one there is
        world.config.write_text(f"{content}\ngitleaks = {json.dumps(str(world.stand_in))}\n")
    world.plain()

    result = world.export()

    assert result.code == 2 and why in result.err
    assert world.remote_tip() is None


def test_a_remote_that_does_not_exist_is_said_so(world):
    world.plain()

    result = world.export("--remote", "nowhere")

    assert result.code == 2 and "git remote failed" in result.err


# ---- the pieces -------------------------------------------------------------------------------
def test_env_files_are_read_in_each_way_a_value_may_be_written(tmp_path):
    path = tmp_path / "keys.env"
    path.write_text(
        "A=plain-value-long-enough\n"
        "export B='quoted value long enough'\n"
        'C="double-quoted-long-enough" # a comment\n'
        "# D=commented-out-long-enough\n"
        "E=short\n"
        "not a line\n"
        "F=value-then-comment-long # trailing\n"
    )

    found = ge.env_needles(path)

    assert set(found) == {"A", "B", "C", "F"}
    assert b"quoted value long enough" in found["B"]
    assert b"double-quoted-long-enough" in found["C"]
    assert b"value-then-comment-long" in found["F"]


def test_what_a_report_says_about_a_name_that_is_itself_private():
    rules = ge.Rules(denylist=(ge.re.compile(b"quokka", ge.re.IGNORECASE),), needles={})

    assert rules.shown("docs/notes.txt") == "docs/notes.txt"
    assert "quokka" not in rules.shown("docs/Quokka-notes.txt").lower()
    assert ge.place("a" * 40, "blob", {"a" * 40: "docs/Quokka.txt"}, rules) == "blob aaaaaaa"
    assert ge.place("b" * 40, "tree", {}, rules) == "tree bbbbbbb"


def test_an_error_text_carries_no_credentials():
    text = ge.tidy(b"fatal: unable to access 'https://user:tok3n@github.com/o/r.git/': boom\n")

    assert "tok3n" not in text and "user:" not in text and "\n" not in text


def test_the_config_comes_from_the_environment_or_the_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PA_EXPORT_CONFIG", raising=False)
    assert ge.default_config_path() == tmp_path / ".config" / "pa-export" / "config.toml"

    monkeypatch.setenv("PA_EXPORT_CONFIG", str(tmp_path / "elsewhere.toml"))
    assert ge.default_config_path() == tmp_path / "elsewhere.toml"
