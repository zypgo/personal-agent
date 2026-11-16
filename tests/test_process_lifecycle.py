"""A completed or cancelled tool must not leave its background work running."""

from __future__ import annotations

import asyncio
import sys

import pytest

from personal_agent.config import SandboxConfig
from personal_agent.security.sandbox import DockerSandbox, NoSandbox, run_process


async def _wait_for_file(path) -> None:
    async with asyncio.timeout(3):
        while not path.exists():  # noqa: ASYNC110 (a child process signals through a file)
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("redirect", ["", ">/dev/null 2>&1"], ids=["open-pipes", "closed-pipes"])
async def test_background_work_is_stopped_even_after_it_closes_both_pipes(paths, redirect):
    box = NoSandbox(paths, SandboxConfig(backend="none", i_understand_no_sandbox=True))
    command = f"sh -c 'sleep 0.3; echo leaked > child.marker' {redirect} & echo started"
    result = await box.execute(command, timeout_s=3)
    assert result.exit_code == 0 and result.stdout.strip() == "started"
    await asyncio.sleep(0.5)
    assert not (paths.workspace_dir / "child.marker").exists()


@pytest.mark.parametrize("ending", ["timeout", "cancel"])
async def test_interrupted_jobs_finish_cleanup_before_returning(tmp_path, ending):
    cleaned = []

    async def cleanup():
        await asyncio.sleep(0.01)
        cleaned.append(True)

    task = asyncio.create_task(
        run_process(
            [
                "/bin/sh",
                "-c",
                "sh -c 'sleep 0.3; echo leaked > child.marker' >/dev/null 2>&1 & "
                "echo ready > started; wait",
            ],
            env={"PATH": "/usr/bin:/bin"},
            cwd=tmp_path,
            timeout_s=0.1 if ending == "timeout" else 3,
            on_stop=cleanup,
        )
    )
    await _wait_for_file(tmp_path / "started")
    if ending == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result.timed_out and result.exit_code is None
    assert cleaned == [True]
    await asyncio.sleep(0.5)
    assert not (tmp_path / "child.marker").exists()


async def test_normal_exit_also_finishes_backend_cleanup(tmp_path):
    cleaned = []

    async def cleanup():
        cleaned.append(True)

    result = await run_process(
        ["/bin/sh", "-c", "echo complete"],
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        timeout_s=3,
        on_stop=cleanup,
    )
    assert result.stdout.strip() == "complete" and result.exit_code == 0
    assert cleaned == [True]


async def test_a_second_cancellation_does_not_abandon_backend_cleanup(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    cleaned = []

    async def cleanup():
        started.set()
        await release.wait()
        cleaned.append(True)

    task = asyncio.create_task(
        run_process(
            ["/bin/sh", "-c", "echo ready > started; sleep 30"],
            env={"PATH": "/usr/bin:/bin"},
            cwd=tmp_path,
            timeout_s=30,
            on_stop=cleanup,
        )
    )
    await _wait_for_file(tmp_path / "started")
    task.cancel()
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()  # e.g. the outer run is cancelled while the tool timeout is cleaning up
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned == [True]


async def test_cancelling_docker_removes_the_container_not_just_its_client(paths, monkeypatch):
    """Exercise real subprocess cancellation without touching an installed Docker daemon."""
    create = asyncio.create_subprocess_exec
    launched = asyncio.Event()
    asked = []

    async def fake_docker(*argv, **kwargs):
        assert argv[0] == "docker"
        asked.append(argv)
        if argv[1] == "run":
            process = await create(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
            launched.set()
            return process
        assert argv[1:3] == ("rm", "--force")
        return await create(sys.executable, "-c", "pass", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_docker)
    box = DockerSandbox(paths, SandboxConfig())
    task = asyncio.create_task(box.execute("sleep 30", timeout_s=30))
    await asyncio.wait_for(launched.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(asked) == 2
    container = asked[0][asked[0].index("--name") + 1]
    assert asked[1] == ("docker", "rm", "--force", container)
