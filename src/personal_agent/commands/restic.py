"""Explicit encrypted whole-instance backup operations; imports stay light for CLI help."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from personal_agent.commands.common import echo, fail

if TYPE_CHECKING:
    from personal_agent.collaboration.restic import ResticArchive

app = typer.Typer(help="Encrypted storage for checked backup-all snapshots, using restic.")


@app.callback()
def configure(
    ctx: typer.Context,
    repository: Annotated[
        str | None,
        typer.Option(
            "--repository",
            "-r",
            help="Local path or sftp://user@host/path; default RESTIC_REPOSITORY.",
        ),
    ] = None,
    namespace: Annotated[
        str, typer.Option(help="A distinct name for this agent instance in the repository.")
    ] = "personal-agent",
    password_file: Annotated[
        Path | None,
        typer.Option(
            help="Private password file; defaults to RESTIC_PASSWORD_FILE or RESTIC_PASSWORD."
        ),
    ] = None,
    timeout_s: Annotated[
        float, typer.Option(min=1, max=3600, help="Maximum time for each restic operation.")
    ] = 300,
) -> None:
    ctx.obj = (repository, namespace, password_file, timeout_s)


def _archive(ctx: typer.Context) -> ResticArchive:
    from personal_agent.collaboration.restic import ResticArchive, ResticError

    repository, namespace, password_file, timeout_s = ctx.obj
    try:
        return ResticArchive(
            repository, namespace=namespace, password_file=password_file, timeout_s=timeout_s
        )
    except ResticError as exc:
        fail(str(exc), code=2)


@app.command("init")
def initialize(ctx: typer.Context) -> None:
    """Create a new encrypted repository."""
    from personal_agent.collaboration.backup import SnapshotError

    try:
        _archive(ctx).initialize()
    except SnapshotError as exc:
        fail(str(exc))
    except (OSError, sqlite3.Error):
        fail("The local snapshot or storage could not be accessed or verified.")
    echo("Encrypted backup repository initialized.")


@app.command("backup")
def backup(
    ctx: typer.Context,
    snapshot: Annotated[
        Path, typer.Argument(help="An existing, checked pa db backup-all snapshot.")
    ],
) -> None:
    """Verify a complete snapshot, then upload only its manifest-listed files."""
    from personal_agent.collaboration.backup import SnapshotError

    try:
        identity = _archive(ctx).backup(snapshot)
    except SnapshotError as exc:
        fail(str(exc))
    except (OSError, sqlite3.Error):
        fail("The local snapshot or storage could not be accessed or verified.")
    echo("Encrypted snapshot: " + identity)


@app.command("snapshots")
def snapshots(ctx: typer.Context) -> None:
    """List this instance's snapshot IDs and times."""
    from personal_agent.collaboration.backup import SnapshotError

    try:
        items = _archive(ctx).snapshots()
    except SnapshotError as exc:
        fail(str(exc))
    except (OSError, sqlite3.Error):
        fail("The local snapshot or storage could not be accessed or verified.")
    for item in items:
        echo(f"{item.id}  {item.time}")
    if not items:
        echo("No snapshots in this backup namespace.")


@app.command("restore")
def restore(
    ctx: typer.Context,
    snapshot_id: str,
    to: Annotated[
        Path,
        typer.Option("--to", help="New agent home (it must not exist yet)."),
    ],
) -> None:
    """Download a snapshot, check it and restore it into a new home. Old device, node and channel logins stop working."""
    from personal_agent.collaboration.backup import SnapshotError

    try:
        _archive(ctx).restore(snapshot_id, to)
    except SnapshotError as exc:
        fail(str(exc))
    except (OSError, sqlite3.Error):
        fail("The local snapshot or storage could not be accessed or verified.")
    echo("Restored into the new home. Old device, node and channel credentials are revoked.")
    echo(
        "Pending work and timers are held. Pair devices again before starting one control process."
    )


@app.command("check")
def check(ctx: typer.Context) -> None:
    """Verify the encrypted repository and all stored data."""
    from personal_agent.collaboration.backup import SnapshotError

    try:
        _archive(ctx).check()
    except SnapshotError as exc:
        fail(str(exc))
    except (OSError, sqlite3.Error):
        fail("The local snapshot or storage could not be accessed or verified.")
    echo("Encrypted backup data passed verification.")


@app.command("forget")
def forget(
    ctx: typer.Context,
    keep_last: Annotated[
        int, typer.Option(min=1, max=10000, help="Snapshots to retain in this instance namespace.")
    ],
    apply: Annotated[
        bool,
        typer.Option(
            "--apply", help="Actually remove old snapshot references; default is a preview."
        ),
    ] = False,
) -> None:
    """Preview or apply the snapshot retention for this instance."""
    from personal_agent.collaboration.backup import SnapshotError

    try:
        count = _archive(ctx).forget(keep_last=keep_last, apply=apply)
    except SnapshotError as exc:
        fail(str(exc))
    except (OSError, sqlite3.Error):
        fail("The local snapshot or storage could not be accessed or verified.")
    echo(
        f"{'Removed' if apply else 'Would remove'} {count} snapshot reference(s) in this namespace."
    )
