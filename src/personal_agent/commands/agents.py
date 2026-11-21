"""Connect an existing Grok/Muse agent to scoped work; the bridge never starts a model."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    help="Cloud-agent connection, claims, receipts and result reporting.", no_args_is_help=True
)
DEFAULT_TOKEN_ENV = "PA_AGENT_TOKEN"  # noqa: S105 — environment variable name, not a credential


@app.callback()
def options(
    ctx: typer.Context,
    home: Annotated[
        Path,
        typer.Option(
            "--home", envvar="PA_AGENT_HOME", help="Private worker state directory outside Git."
        ),
    ] = Path("~/.personal-agent-worker"),
) -> None:
    ctx.obj = home


def _run(ctx: typer.Context, arguments: list[str]) -> None:
    from personal_agent.agent_adapters.bridge import main

    code = main(["--home", str(ctx.obj), *arguments])
    if code:
        raise typer.Exit(code)


@app.command("setup")
def setup(
    ctx: typer.Context,
    url: Annotated[
        str, typer.Option(help="Control HTTPS origin; loopback HTTP is allowed for tests.")
    ],
    agent: Annotated[str, typer.Option(help="Agent ID registered in the control service.")],
    space: Annotated[str, typer.Option(help="Authorized space ID.")],
    token_env: Annotated[
        str, typer.Option(help="Name of the environment variable that holds the token.")
    ] = DEFAULT_TOKEN_ENV,
    token_stdin: Annotated[
        bool, typer.Option(help="Read one token from stdin and save it in a private 0600 file.")
    ] = False,
) -> None:
    """Configure this worker. Its token must come from the matching agent registration."""
    arguments = ["setup", "--url", url, "--agent", agent, "--space", space]
    arguments.extend(["--token-stdin"] if token_stdin else ["--token-env", token_env])
    _run(ctx, arguments)


@app.command("check")
def check(ctx: typer.Context) -> None:
    """Check the token and report what this bridge can do."""
    _run(ctx, ["check"])


@app.command("update-credentials")
def update_credentials(
    ctx: typer.Context,
    token_env: Annotated[
        str, typer.Option(help="Replacement token environment variable NAME.")
    ] = DEFAULT_TOKEN_ENV,
    token_stdin: Annotated[bool, typer.Option(help="Read the new token from stdin.")] = False,
) -> None:
    """Check and save a new token for the same server, agent and space."""
    _run(
        ctx,
        ["update-credentials", *(["--token-stdin"] if token_stdin else ["--token-env", token_env])],
    )


@app.command("begin")
def begin(ctx: typer.Context) -> None:
    """Save a new claim nonce once per native task invocation; keep it for retries."""
    _run(ctx, ["begin"])


@app.command("claim")
def claim(
    ctx: typer.Context,
    worker_id: Annotated[
        str, typer.Option(help="Nonce returned by begin; reuse it after a lost response.")
    ],
    task_id: Annotated[
        str | None, typer.Option(help="Optional task named by the wake notification.")
    ] = None,
) -> None:
    """Claim one task, saving its attempt identity before displaying work."""
    arguments = ["claim", "--worker-id", worker_id]
    if task_id is not None:
        arguments.extend(["--task-id", task_id])
    _run(ctx, arguments)


@app.command("status")
def status(ctx: typer.Context, attempt_id: str | None = None) -> None:
    """Read scoped connection or attempt state, cancellation requests and question answers."""
    _run(ctx, ["status", *([attempt_id] if attempt_id else [])])


@app.command("heartbeat")
def heartbeat(ctx: typer.Context, attempt_id: str) -> None:
    """Renew the saved claim and observe cancellation before doing more work."""
    _run(ctx, ["heartbeat", attempt_id])


def _text_flags(text: str | None, text_file: Path | None) -> list[str]:
    if (text is None) == (text_file is None):
        raise typer.BadParameter("Provide exactly one of --text or --text-file.")
    return ["--text-file", str(text_file)] if text_file is not None else ["--text", text or ""]


@app.command("progress")
def progress(
    ctx: typer.Context,
    attempt_id: str,
    text: Annotated[str | None, typer.Option()] = None,
    text_file: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Report progress under a persisted, monotonically increasing sequence number."""
    _run(ctx, ["progress", attempt_id, *_text_flags(text, text_file)])


@app.command("report")
def report(
    ctx: typer.Context,
    attempt_id: str,
    status: Annotated[str, typer.Option(help="succeeded, failed or cancelled.")],
    text: Annotated[str | None, typer.Option()] = None,
    text_file: Annotated[Path | None, typer.Option()] = None,
    file: Annotated[
        list[Path] | None,
        typer.Option("--file", help="A result file (up to 2 MiB)."),
    ] = None,
) -> None:
    """Submit a result and its files. The task owner accepts it separately."""
    arguments = ["report", attempt_id, "--status", status, *_text_flags(text, text_file)]
    for path in file or []:
        arguments.extend(["--file", str(path)])
    _run(ctx, arguments)


@app.command("ask")
def ask(
    ctx: typer.Context,
    attempt_id: str,
    text: Annotated[str | None, typer.Option()] = None,
    text_file: Annotated[Path | None, typer.Option()] = None,
    choice: Annotated[list[str] | None, typer.Option("--choice")] = None,
    request_id: Annotated[str | None, typer.Option(help="Optional stable question ID.")] = None,
) -> None:
    """Ask the task owner a question."""
    arguments = ["ask", attempt_id, *_text_flags(text, text_file)]
    for item in choice or []:
        arguments.extend(["--choice", item])
    if request_id is not None:
        arguments.extend(["--request-id", request_id])
    _run(ctx, arguments)


@app.command("retry")
def retry(ctx: typer.Context, attempt_id: str) -> None:
    """Resend the saved report after checking the status."""
    _run(ctx, ["retry", attempt_id])


@app.command("uncertain")
def uncertain(ctx: typer.Context, attempt_id: str, reason: Annotated[str, typer.Option()]) -> None:
    """Record that an attempt may have run, without marking it done or retrying it."""
    _run(ctx, ["uncertain", attempt_id, "--text", reason])


@app.command("files")
def files(
    ctx: typer.Context,
    attempt_id: str,
    out: Annotated[
        Path,
        typer.Option(help="Directory for the downloaded files."),
    ],
) -> None:
    """Download the files this task depends on and check their hashes."""
    _run(ctx, ["files", attempt_id, "--out", str(out)])


@app.command("export-bridge")
def export_bridge(
    output: Annotated[
        Path, typer.Option(help="New destination for the standalone standard-library script.")
    ],
) -> None:
    """Copy a standalone worker bridge for a cloud computer without installing Qixu."""
    from importlib.resources import files

    data = files("personal_agent.agent_adapters").joinpath("bridge.py").read_bytes()
    try:
        with output.open("xb") as stream:
            stream.write(data)
    except OSError:
        typer.echo("Cannot create bridge file; choose a new writable destination.", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps({"path": str(output), "sha256": hashlib.sha256(data).hexdigest()}))


@app.command("host")
def host(
    ctx: typer.Context,
    provider: Annotated[
        str, typer.Option(help="codex, cursor or personal_agent; uses an existing native account.")
    ],
    cwd: Annotated[Path, typer.Option(help="Local working directory.")],
    task_id: Annotated[str | None, typer.Option(help="Optional approved task ID.")] = None,
    executable: Annotated[str | None, typer.Option(help="Path to the executable.")] = None,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch/--once", help="Keep polling sequentially; default handles one claim."
        ),
    ] = False,
    allow_direct_workdir: Annotated[
        bool,
        typer.Option(help="Work directly in a directory that is not a Git repository."),
    ] = False,
    allow_shared_native_profile: Annotated[
        bool,
        typer.Option(help="Accept tasks from other members when the agent is marked as shared."),
    ] = False,
) -> None:
    """Run approved tasks on this computer (macOS or Linux)."""
    import asyncio

    from personal_agent.agent_adapters.bridge import BridgeError, WorkerBridge, public_result
    from personal_agent.agent_adapters.host import AgentHost, provider_for

    async def run() -> None:
        worker = WorkerBridge(ctx.obj)
        runner = AgentHost(
            worker,
            provider_for(provider, executable),
            provider_name=provider,
            cwd=cwd,
            allow_direct_workdir=allow_direct_workdir,
            allow_shared_native_profile=allow_shared_native_profile,
        )
        await asyncio.to_thread(worker.hello)
        while True:
            result = await runner.run_once(task_id=task_id)
            typer.echo(
                json.dumps(
                    public_result(result, worker.client.credentials.token), ensure_ascii=False
                )
            )
            if not watch:
                return
            await asyncio.sleep(10)

    try:
        asyncio.run(run())
    except BridgeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    except (OSError, ValueError):
        typer.echo(
            "Agent Host stopped; preserve its private receipts and inspect connection/native setup.",
            err=True,
        )
        raise typer.Exit(1) from None


@app.command("reconcile-local")
def reconcile_local(
    ctx: typer.Context,
    provider: Annotated[str, typer.Option(help="Original host provider.")],
    cwd: Annotated[Path, typer.Option(help="Original host working directory.")],
    stop: Annotated[
        bool,
        typer.Option(help="Stop the recorded process if it is still running."),
    ] = False,
) -> None:
    """Check that the earlier run has stopped before this host is used again."""
    import asyncio

    from personal_agent.agent_adapters.bridge import BridgeError, WorkerBridge
    from personal_agent.agent_adapters.host import AgentHost, provider_for

    try:
        runner = AgentHost(
            WorkerBridge(ctx.obj), provider_for(provider), provider_name=provider, cwd=cwd
        )
        typer.echo(json.dumps(asyncio.run(runner.reconcile_local(stop=stop))))
    except BridgeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    except (OSError, ValueError):
        typer.echo(
            "Cannot reconcile this profile. Keep its receipts and ask the task owner to verify the original execution.",
            err=True,
        )
        raise typer.Exit(1) from None
