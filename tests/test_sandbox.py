"""The sandbox, for real: these tests start actual processes under `sandbox-exec` (macOS) and check
what they can and cannot do. Skipped where seatbelt is not available."""

from __future__ import annotations

import os
import shutil
import socket
import sys
from pathlib import Path

import pytest

from personal_agent.config import SandboxConfig
from personal_agent.paths import Paths
from personal_agent.security import sandbox as sandbox_module
from personal_agent.security.sandbox import (
    DockerSandbox,
    NoSandbox,
    SeatbeltSandbox,
    child_env,
    select_sandbox,
)

needs_seatbelt = pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists(),
    reason="needs macOS sandbox-exec",
)
has_python = pytest.mark.skipif(shutil.which("python3") is None, reason="needs python3")


@pytest.fixture
def paths(tmp_path) -> Paths:
    p = Paths.resolve(tmp_path / "home")
    p.ensure()
    return p


@pytest.fixture
def box(paths) -> SeatbeltSandbox:
    return SeatbeltSandbox(paths, SandboxConfig())


async def sh(box, command: str, timeout_s: float = 20):
    return await box.execute(command, timeout_s=timeout_s)


# ---- what a command can do ---------------------------------------------------------------------
@needs_seatbelt
def test_the_probe_confirms_isolation_works(box):
    probe = box.probe()
    assert probe.ok, probe.detail


@needs_seatbelt
async def test_commands_run_in_the_workspace_and_their_files_stay_there(box, paths):
    result = await sh(
        box, "pwd; echo data > note.txt; mkdir -p a/b; cp note.txt a/b/; ls -R | wc -l"
    )
    assert result.exit_code == 0 and result.error is None
    assert result.stdout.splitlines()[0] == str(paths.workspace_dir.resolve())
    assert (paths.workspace_dir / "a/b/note.txt").read_text() == "data\n"


@needs_seatbelt
async def test_exit_codes_and_stderr_come_back(box):
    result = await sh(box, "echo out; echo err >&2; exit 3")
    assert (result.exit_code, result.stdout.strip(), result.stderr.strip()) == (3, "out", "err")


@needs_seatbelt
async def test_it_cannot_read_the_agents_secrets_or_files_outside_the_workspace(
    box, paths, tmp_path
):
    (paths.home / ".env").write_text("OPENAI_API_KEY=" + "sk-" + "x" * 30)
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    for target in (paths.home / ".env", paths.db_file, outside):
        result = await sh(box, f"cat '{target}'")
        assert result.exit_code != 0 and "private" not in result.stdout
        assert "x" * 30 not in result.stdout + result.stderr


@needs_seatbelt
async def test_it_cannot_write_outside_the_workspace_scratch_areas(box, paths, tmp_path):
    for target in (tmp_path / "evil.txt", paths.home / "pwned.txt", paths.memory_dir / "x.md"):
        result = await sh(box, f"echo x > '{target}'")
        assert result.exit_code != 0
        assert not target.exists()


@needs_seatbelt
async def test_it_sees_a_tiny_environment_without_our_secrets(box, paths, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "p" * 30)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "q" * 30)
    result = await sh(box, "env")
    assert result.exit_code == 0
    assert "p" * 30 not in result.stdout and "q" * 30 not in result.stdout
    env = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert Path(env["HOME"]).is_relative_to(paths.run_dir.resolve())
    assert Path(env["TMPDIR"]).is_relative_to(paths.run_dir.resolve())


@needs_seatbelt
@has_python
async def test_there_is_no_network_not_even_to_this_machine(box):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        script = (
            "import socket; s = socket.socket(); s.settimeout(3); "
            f"s.connect(('127.0.0.1', {port})); print('CONNECTED')"
        )
        result = await sh(box, f'python3 -c "{script}"')
    finally:
        server.close()
    assert "CONNECTED" not in result.stdout
    assert "Operation not permitted" in result.stderr


@needs_seatbelt
async def test_it_cannot_signal_other_processes(box):
    result = await sh(box, f"kill -9 {os.getpid()}; echo survived")
    assert "Operation not permitted" in result.stderr  # and the test process is still here


@needs_seatbelt
async def test_the_clipboard_is_out_of_reach(box):
    result = await sh(box, "pbpaste")
    assert result.exit_code != 0 or result.stdout == ""


@needs_seatbelt
async def test_skills_are_readable_but_not_writable(box, paths):
    (paths.skills_dir / "demo").mkdir()
    (paths.skills_dir / "demo" / "SKILL.md").write_text("hello")
    read = await sh(box, f"cat '{paths.skills_dir}/demo/SKILL.md'")
    write = await sh(box, f"echo x > '{paths.skills_dir}/demo/SKILL.md'")
    assert read.stdout == "hello" and write.exit_code != 0
    assert (paths.skills_dir / "demo" / "SKILL.md").read_text() == "hello"


@needs_seatbelt
async def test_extra_read_and_write_directories_are_opt_in(paths, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "in.txt").write_text("readable")
    drop = tmp_path / "drop"
    drop.mkdir()
    box = SeatbeltSandbox(paths, SandboxConfig(), extra_read=[shared], extra_write=[drop])
    assert (await sh(box, f"cat '{shared}/in.txt'")).stdout == "readable"
    assert (await sh(box, f"echo x > '{shared}/new.txt'")).exit_code != 0
    assert (await sh(box, f"echo kept > '{drop}/out.txt'")).exit_code == 0
    assert (drop / "out.txt").read_text() == "kept\n"


# ---- bounds ---------------------------------------------------------------------------------------
@needs_seatbelt
async def test_a_command_that_runs_too_long_is_killed_with_its_children(box, paths):
    pid_file = paths.workspace_dir / "child.pid"
    result = await sh(box, "sleep 60 & echo $! > child.pid; wait", timeout_s=1)
    assert result.timed_out and result.exit_code is None and result.duration_s < 10
    child = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


@needs_seatbelt
async def test_huge_output_is_capped_without_stalling_the_command(box, monkeypatch):
    monkeypatch.setattr(sandbox_module, "STREAM_CAP", 10_000)
    result = await sh(box, "yes | head -c 3000000; echo done >&2")
    assert len(result.stdout) == 10_000 and result.truncated
    assert result.exit_code == 0 and "done" in result.stderr


@needs_seatbelt
async def test_background_processes_do_not_outlive_the_command_or_swallow_its_output(
    box, monkeypatch
):
    monkeypatch.setattr(sandbox_module, "PIPE_GRACE_S", 0.3)
    result = await sh(box, "(sleep 60 &); echo started")
    assert result.stdout.strip() == "started" and result.exit_code == 0
    assert result.duration_s < 10


# ---- other backends and selection ---------------------------------------------------------------
def test_docker_runs_with_no_network_a_read_only_root_and_only_the_workspace(paths):
    box = DockerSandbox(paths, SandboxConfig(image="python:3.13-slim", memory_mb=256))
    argv = box.argv("echo hi", "pa-test")
    joined = " ".join(argv)
    assert argv[:2] == ["docker", "run"]
    for expected in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "no-new-privileges",
        "--memory 256m",
    ):
        assert expected in joined
    mounts = [argv[i + 1] for i, item in enumerate(argv) if item == "-v"]
    assert mounts == [
        f"{paths.workspace_dir.resolve()}:/workspace:rw",
        f"{paths.skills_dir.resolve()}:/skills:ro",
    ]  # nothing else of ours is visible inside
    assert argv[-4] == "python:3.13-slim"


def test_child_env_never_carries_our_environment(paths, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "z" * 30)
    env = child_env(paths.home / "h", paths.home / "t")
    assert "OPENAI_API_KEY" not in env and "z" * 30 not in " ".join(env.values())
    assert set(env) >= {"PATH", "HOME", "TMPDIR"}


def user_temp(monkeypatch, value):
    """Make the per-user temp directory look like `value` (or like an unanswerable question)."""

    def confstr(name):
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(os, "confstr", confstr)


def test_the_xcrun_cache_rule_allows_exactly_its_own_files(monkeypatch, tmp_path):
    folder = tmp_path / "T"
    folder.mkdir()
    user_temp(monkeypatch, str(folder) + "/")

    rules = SeatbeltSandbox.xcrun_cache_rule()

    assert len(rules) == 2
    assert f"{folder.resolve()}/xcrun_db" in rules[0] and "file-write" in rules[0]
    assert "file-read-metadata" in rules[1] and "file-write" not in rules[1]


@pytest.mark.parametrize(
    "answer",
    [None, "", ValueError("no such name"), OSError("unavailable"), AttributeError("not macOS")],
    ids=["none", "empty", "value-error", "os-error", "no-confstr"],
)
def test_without_a_user_temp_directory_there_is_no_xcrun_rule(monkeypatch, answer):
    user_temp(monkeypatch, answer)
    assert SeatbeltSandbox.xcrun_cache_rule() == []


@pytest.mark.parametrize("name", ["with space", 'a"quote', "back\\slash", "paren(thesis)"])
def test_an_odd_user_temp_directory_is_never_pasted_into_the_profile(monkeypatch, tmp_path, name):
    odd = tmp_path / name
    odd.mkdir()
    user_temp(monkeypatch, str(odd))
    assert SeatbeltSandbox.xcrun_cache_rule() == []


def test_no_sandbox_needs_an_explicit_acknowledgement(paths):
    # The setting itself is refused (see test_config); a config that got here some other way is
    # still not given an unprotected shell.
    unacknowledged = SandboxConfig.model_construct(backend="none", i_understand_no_sandbox=False)
    refused, why = select_sandbox(paths, unacknowledged)
    assert refused is None and "i_understand_no_sandbox" in why
    chosen, _ = select_sandbox(paths, SandboxConfig(backend="none", i_understand_no_sandbox=True))
    assert isinstance(chosen, NoSandbox) and "NONE" in chosen.describe()


def test_without_a_working_sandbox_there_is_no_shell_not_an_unprotected_one(paths, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    chosen, why = select_sandbox(paths, SandboxConfig(backend="auto"))
    assert chosen is None and "docker" in why

    chosen, why = select_sandbox(paths, SandboxConfig(backend="seatbelt"))
    assert chosen is None and "seatbelt" in why


@needs_seatbelt
def test_auto_prefers_seatbelt_on_macos(paths):
    chosen, _ = select_sandbox(paths, SandboxConfig(backend="auto"))
    assert chosen is not None and chosen.name == "seatbelt"
    assert "no network" in chosen.describe()


async def test_no_sandbox_backend_runs_commands_in_the_workspace(paths):
    box = NoSandbox(paths, SandboxConfig(backend="none", i_understand_no_sandbox=True))
    result = await box.execute("pwd; echo hi", timeout_s=10)
    assert result.exit_code == 0 and result.stdout.splitlines()[1] == "hi"
