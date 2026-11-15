"""`pa service`: keep the agent running in the background, started at login (macOS).

`install` writes one LaunchAgent for this home and loads it; `uninstall` stops and removes it;
`status` says what launchd and the agent itself report; `restart` starts it afresh; `logs` shows
what it has been saying. Nothing here needs root, and nothing secret goes into the job file: the
daemon reads `PA_HOME/.env` itself.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.markup import escape

from personal_agent.config import ConfigError, api_key, load_config, load_env
from personal_agent.daemon import LOCK_NAME, EngineLock, daemon_state
from personal_agent.launchd import (
    THROTTLE_S,
    ServiceError,
    ServiceManager,
    pass_env,
    protected_folder,
    tail,
)
from personal_agent.paths import Paths

if TYPE_CHECKING:
    from personal_agent.app import App


def manager_for(paths: Paths, *, python: str | None = None) -> ServiceManager:
    """Where tests put a manager that does not touch the machine's launchd."""
    return ServiceManager(paths, python=python)


def pause(seconds: float) -> None:
    """Where tests skip the waiting."""
    time.sleep(seconds)


AGENT_WAIT_S = 30.0
"""How long to wait, after launchd has started the program, for the agent itself to report in:
Python needs a few seconds to load before the agent takes its lock."""


def wait_for_agent(paths: Paths, *, seconds: float = AGENT_WAIT_S, step_s: float = 0.5) -> bool:
    lock = paths.run_dir / LOCK_NAME
    for _ in range(math.ceil(seconds / step_s)):
        if EngineLock.is_taken(lock):
            return True
        pause(step_s)
    return EngineLock.is_taken(lock)


def follow(
    path: Path,
    emit: Callable[[str], None],
    *,
    poll_s: float = 0.5,
    stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Print what is added to `path` from now on, until `stop()` says so: a log that is rotated
    (a new file under the same name) is followed into its new file, one that is cut short is read
    again from its start, and a line is only shown once it is complete."""
    handle = None
    inode = None
    skip_existing = path.exists()  # a log that is not there yet is read from its first line
    try:
        while not stop():
            try:
                info = path.stat()
            except OSError:
                sleep(poll_s)
                continue
            if handle is None:
                handle = open(path, "rb")  # noqa: SIM115 (closed below, across iterations)
                inode = info.st_ino
                if skip_existing:
                    handle.seek(0, os.SEEK_END)
                skip_existing = False
            elif info.st_ino != inode:
                handle.close()
                handle = open(path, "rb")  # noqa: SIM115
                inode = info.st_ino
            elif info.st_size < handle.tell():
                handle.seek(0)
            line = handle.readline()
            if line.endswith(b"\n"):
                emit(line.decode("utf-8", "replace").rstrip("\n"))
                continue
            if line:
                handle.seek(-len(line), os.SEEK_CUR)  # half a line: wait for the rest
            sleep(poll_s)
    finally:
        if handle is not None:
            handle.close()


def build(open_app: Callable[[], App], console: Console) -> typer.Typer:
    service_app = typer.Typer(
        help="Run the agent in the background, started at login (macOS).",
        invoke_without_command=True,
    )

    def say(text: str) -> None:
        console.print(text, highlight=False, soft_wrap=True)  # a path is never broken in two

    def fail(text: str, code: int = 1) -> typer.Exit:
        console.print(f"[red]{escape(text)}[/red]")
        return typer.Exit(code)

    def home() -> Paths:
        paths = Paths.resolve()
        if not paths.home.is_dir():
            raise fail(f"{paths.home} does not exist. Run `pa init` first.")
        try:
            load_env(paths)
        except ConfigError as exc:
            raise fail(str(exc), 2) from exc
        return paths

    # ---- status -------------------------------------------------------------------------------
    @service_app.callback()
    def _bare(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is None:  # `pa service` alone is the same as `pa service status`
            show_status()

    @service_app.command("status")
    def status() -> None:
        """How the background service is doing."""
        show_status()

    def show_status() -> None:
        paths = home()
        manager = manager_for(paths)
        try:
            launch = manager.status()
        except ServiceError as exc:
            raise fail(str(exc)) from exc
        say(f"job        {manager.label}")
        if not launch.installed and not launch.loaded:
            say("service    [dim]not installed[/dim] (`pa service install`)")
        elif not launch.loaded:
            say(
                "service    [yellow]installed but not loaded[/yellow]: it loads at your next "
                "login, or now with `pa service install`"
            )
        else:
            running = launch.pid is not None
            say(
                "service    "
                + (
                    f"[green]running[/green] (pid {launch.pid})"
                    if running
                    else f"[yellow]loaded, not running[/yellow] ({launch.state or 'unknown'})"
                )
                + (f" · started {launch.runs} time(s)" if launch.runs is not None else "")
                + (f" · last exit: {launch.last_exit}" if launch.last_exit else "")
            )
            if not running:
                say("           `pa service logs --launchd` says why it stopped")
        if launch.installed:
            say(f"job file   {manager.plist_file}")
            program = manager.installed_program()
            if program:
                say(f"program    {' '.join(program)}")
                target = manager.installed_python()
                if target and not os.path.exists(target):
                    say(
                        f"           [yellow]{escape(target)} no longer exists[/yellow]: "
                        "run `pa service install` again"
                    )
                elif target and (folder := protected_folder(target)):
                    say(
                        f"           [yellow]it is in your {folder} folder, which macOS keeps from "
                        "background jobs[/yellow] (see `pa service install --help`)"
                    )
        try:
            opened = open_app()
        except typer.Exit:
            return
        try:
            state = daemon_state(opened)
        finally:
            opened.close()
        if state.kind == "daemon":
            say(
                f"agent      [green]alive[/green] (pid {state.pid}, heartbeat {state.heartbeat_age_s:.0f}s ago)"
            )
        elif state.kind == "terminal":
            say(f"agent      run from a terminal (pid {state.pid}), not by the service")
        else:
            say("agent      [dim]not running[/dim]")

    # ---- install ------------------------------------------------------------------------------
    @service_app.command("install")
    def install(
        dry_run: bool = typer.Option(False, "--dry-run", help="Show the job file, change nothing."),
        no_start: bool = typer.Option(
            False, "--no-start", help="Write the job file but do not load it yet."
        ),
        keep_awake: bool = typer.Option(
            False,
            "--keep-awake",
            help="Do not let the Mac sleep while it is on mains power (the display may still).",
        ),
        copy_env: Annotated[
            list[str] | None,
            typer.Option(
                "--pass-env",
                help="Copy this variable of the terminal into the job, e.g. HTTPS_PROXY (a job "
                "does not inherit your shell's environment). Repeat for more. Secrets are refused.",
            ),
        ] = None,
        python: str | None = typer.Option(
            None,
            "--python",
            help="The interpreter that runs the agent. Default: the one running this command.",
        ),
    ) -> None:
        """Start the agent at login and again if it stops by accident.

        Run this from the environment the agent is installed in. macOS does not let a background job read your Desktop, Documents or Downloads folder (nothing asks, it just refuses), so keep the project and its virtualenv elsewhere, or install a copy with `uv tool install .`.
        """
        paths = home()
        try:
            cfg = load_config(paths)
        except ConfigError as exc:
            raise fail(str(exc), 2) from exc
        manager = manager_for(paths, python=python)
        try:
            chosen = pass_env(copy_env or [], os.environ, paths.env_file)
            planned = manager.plan(
                grace_s=cfg.runtime.shutdown_grace_s, env=chosen, keep_awake=keep_awake
            )
        except ServiceError as exc:
            raise fail(str(exc)) from exc
        if dry_run:
            say(f"[dim]# {planned.plist_file}[/dim]")
            console.print(planned.shown_text, markup=False, highlight=False, soft_wrap=True)
            for warning in planned.warnings:
                say(f"[yellow]{escape(warning)}[/yellow]")
            return

        start = not no_start
        held = EngineLock.is_taken(paths.run_dir / LOCK_NAME)
        if start and held and not manager.is_loaded():
            start = False
            say(
                "[yellow]The agent is already running in a terminal[/yellow] (`pa serve` or "
                "`pa chat`). The job file is written but not loaded: stop it there, then run "
                "`pa service install` again."
            )
        try:
            with console.status("Loading the job (an older copy is stopped first)..."):
                done = manager.install(
                    grace_s=cfg.runtime.shutdown_grace_s,
                    env=chosen,
                    keep_awake=keep_awake,
                    start=start,
                )
        except ServiceError as exc:
            raise fail(str(exc)) from exc
        say(f"[green]Installed[/green] the background service ({done.label}).")
        say(f"  job file   {done.plist_file}")
        say(f"  program    {' '.join(done.program)}")
        say(f"  logs       {manager.out_log} and {manager.err_log} (the agent's own: pa.log)")
        if chosen:
            say(f"  copied     {', '.join(sorted(chosen))} (the values are in the job file)")
        for warning in done.warnings:
            say(f"[yellow]{escape(warning)}[/yellow]")
        if not api_key() and not (cfg.model.base_url or os.environ.get("OPENAI_BASE_URL")):
            say(
                f"[yellow]OPENAI_API_KEY is not set in {paths.env_file} yet[/yellow]: the service "
                "will run, but it cannot answer until it is."
            )
        if done.loaded:
            with console.status("Waiting for the agent to come up..."):
                up = wait_for_agent(paths)
            if up:
                say(
                    "[green]The agent is up.[/green] It starts at every login and is restarted if "
                    "it stops by accident."
                )
            else:
                say(
                    f"[yellow]launchd started the program, but the agent has not reported in after "
                    f"{AGENT_WAIT_S:.0f} seconds.[/yellow] `pa service logs --launchd` shows why; "
                    "launchd keeps trying."
                )
        else:
            say("It is not running yet (see above); `pa service install` loads it.")
        say("`pa service status` shows how it is doing, `pa service uninstall` removes it.")

    # ---- uninstall, restart, logs -----------------------------------------------------------
    @service_app.command("uninstall")
    def uninstall() -> None:
        """Stop the background service and remove its job file. Your data and logs stay."""
        paths = home()
        try:
            with console.status("Stopping: the agent finishes what it is doing first..."):
                removed = manager_for(paths).uninstall()
        except ServiceError as exc:
            raise fail(str(exc)) from exc
        if not removed.was_loaded and not removed.removed_file:
            say("The service was not installed; nothing to remove.")
            return
        say(
            f"Removed the background service ({removed.label}). Memory, history and logs in "
            f"{paths.home} are untouched; `pa serve` still runs the agent in a terminal."
        )
        if not removed.stopped:
            say(
                "[yellow]The agent is still finishing its work.[/yellow] launchd ends it for good "
                "when its time is up; `pa service status` says when it is gone."
            )

    @service_app.command("restart")
    def restart() -> None:
        """Stop the agent (letting running work finish) and start it again."""
        paths = home()
        manager = manager_for(paths)
        say(
            "Restarting: the agent finishes what it is doing, then launchd starts it again "
            f"(never sooner than {THROTTLE_S} seconds after its last start, so this can take a "
            "minute)."
        )
        try:
            with console.status("Waiting for launchd..."):
                again = manager.restart()
        except ServiceError as exc:
            raise fail(str(exc)) from exc
        if not again.started:
            say(
                "[yellow]launchd has not started it again yet.[/yellow] It will; "
                "`pa service status` shows when."
            )
            return
        with console.status("Waiting for the agent to come up..."):
            up = wait_for_agent(paths)
        if up:
            say(f"[green]The agent is running again[/green] (pid {again.status.pid}).")
        else:
            say(
                f"[yellow]launchd started it again (pid {again.status.pid}), but the agent has "
                f"not reported in after {AGENT_WAIT_S:.0f} seconds.[/yellow] "
                "`pa service logs --launchd` shows why."
            )

    @service_app.command("logs")
    def logs(
        lines: int = typer.Option(40, "--lines", "-n", min=1, help="How many lines to show."),
        launchd: bool = typer.Option(
            False,
            "--launchd",
            help="Show what the job printed to its terminal (crashes before the log opens) "
            "instead of the agent's own log.",
        ),
        follow_on: bool = typer.Option(False, "--follow", "-f", help="Keep showing new lines."),
    ) -> None:
        """What the agent has been saying (its own log; secrets are scrubbed from every line)."""
        paths = home()
        manager = manager_for(paths)
        files: Sequence[Path] = (
            (manager.err_log, manager.out_log) if launchd else (paths.logs_dir / "pa.log",)
        )
        shown = False
        for file in files:
            if not file.exists():
                continue
            body = tail(file, lines)
            if launchd:
                say(f"[dim]== {file.name}[/dim]")
            for line in body:
                console.print(line, markup=False, highlight=False, soft_wrap=True)
            shown = shown or bool(body)
        if not any(f.exists() for f in files):
            raise fail("There is no log yet: the agent has not run in the background.")
        if not shown:
            say("[dim](nothing logged yet)[/dim]")
        if follow_on:
            try:
                follow(
                    files[0],
                    lambda line: console.print(line, markup=False, highlight=False, soft_wrap=True),
                )
            except KeyboardInterrupt:
                pass

    return service_app
