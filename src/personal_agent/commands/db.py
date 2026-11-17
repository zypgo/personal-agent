"""`pa db`: how big the database is, what is removed from it and when, and looking after the file."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape

from personal_agent.commands.common import (
    USAGE,
    ago,
    console,
    fail,
    home,
    opened,
    say,
    table,
)
from personal_agent.maintenance import RULES, Footprint, footprint, problems, size
from personal_agent.presence import LOCK_NAME, EngineLock

if TYPE_CHECKING:
    from personal_agent.app import App

app = typer.Typer(
    help="The database: its size, what is removed from it and when, checks and backups.",
    invoke_without_command=True,
)

BACKUP_NOTE = (
    "That is the database only. Notes and preferences are files in the memory folder, and "
    "config.toml and .env are separate: copy the whole home folder for a backup of everything."
)


# ---- pa db ----------------------------------------------------------------------------------
@app.callback()
def _show(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    with opened() as agent:
        found = footprint(agent.db, agent.paths.db_file)
        due = agent.janitor.eligible()
        say(_headline(agent.paths.db_file.name, found))
        by_table = {rule.table: rule for rule in RULES}
        rows = table("table", "rows", "can be removed now")
        for name, count in sorted(found.rows.items(), key=lambda item: (-item[1], item[0])):
            rule = by_table.get(name)
            waiting = due.get(rule.name, 0) if rule else 0
            rows.add_row(escape(name), f"{count:,}", f"{waiting:,}" if waiting else "")
        console.print(rows)
        _say_windows(agent)


def _headline(name: str, found: Footprint) -> str:
    log = f" (+ {size(found.wal_bytes)} log)" if found.wal_bytes else ""
    back = (
        f", {size(found.reclaimable_bytes)} could be given back by `pa db vacuum`"
        if found.reclaimable_bytes >= max(1_000_000, found.db_bytes // 4)
        else ""
    )
    return f"[bold]{escape(name)}[/bold] {size(found.db_bytes)}{log}, schema v{found.schema}{back}"


def _say_windows(agent: App) -> None:
    keep = agent.config.retention
    lasting = {
        "records": keep.records_days,
        "runs": keep.runs_days,
        "audit": keep.audit_days,
        "memory history": keep.memory_history_days,
    }
    shown = ", ".join(f"{k} {'for ever' if v == 0 else f'{v} days'}" for k, v in lasting.items())
    note = escape("(config.toml [retention]; what was said and remembered is never removed)")
    say(f"kept: {shown}  [dim]{note}[/dim]")
    last = agent.janitor.last()
    if not keep.auto:
        say("daily upkeep: off (`pa db prune` does it on request)")
    elif last is None:
        say("daily upkeep: on, has not run yet (it runs while the agent is running)")
    else:
        what = f"removed {last.total:,}" if not last.error else f"stopped: {escape(last.error)}"
        say(f"daily upkeep: on, last {ago(last.at, agent.clock.now())}, {what}")


# ---- pa db prune ----------------------------------------------------------------------------
@app.command("prune")
def prune(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only say what would be removed.")
    ] = False,
) -> None:
    """Remove finished records that are older than the retention in config.toml."""
    with opened() as agent:
        if all(agent.config.retention.days(rule.window) == 0 for rule in RULES):
            say("Every retention window is 0 (keep for ever), so nothing is removed.")
            return
        if dry_run:
            counts = agent.janitor.eligible()
            verb = "Would remove"
        else:
            tidy = agent.janitor.maintain()
            if tidy.error:
                _say_counts(tidy.removed, "Removed")
                fail(f"Stopped before it was done: {tidy.error}.", "Run it again.")
            counts = tidy.removed
            verb = "Removed"
        _say_counts(counts, verb)


def _say_counts(counts: dict[str, int], verb: str) -> None:
    total = sum(counts.values())
    if not total:
        say("Nothing is old enough to remove.")
        return
    say(f"{verb} {total:,} record(s):")
    what = {rule.name: rule.what for rule in RULES}
    for name, count in counts.items():
        if count:
            say(f"  {count:>8,}  {name}: {what[name]}")


# ---- pa db check ----------------------------------------------------------------------------
@app.command("check")
def check(
    full: Annotated[
        bool, typer.Option("--full", help="Read everything and check the indexes (slower).")
    ] = False,
) -> None:
    """Look for damage in the file. It works even where the agent itself cannot open the database."""
    path = home().db_file
    if not path.exists():
        fail(f"{path} does not exist.", "Run `pa init` first.", code=USAGE)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            found = problems(conn, full=full)
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        fail(f"{path.name} cannot be read: {exc}.", "Restore a copy made by `pa db backup`.")
    if found:
        for line in found[:20]:
            say(f"[red]{escape(line)}[/red]")
        fail(f"{len(found)} problem(s) in {path.name}.", "Restore a copy made by `pa db backup`.")
    say(
        f"[green]ok[/green]: {'every page and index' if full else 'the structure'} of {path.name} is sound"
    )


# ---- pa db backup ---------------------------------------------------------------------------
@app.command("backup")
def backup(
    where: Annotated[
        Path | None,
        typer.Argument(help="A new file, or a folder. Default: the backups folder of the home."),
    ] = None,
) -> None:
    """Copy the database while the agent runs. The copy can be read by you only."""
    with opened() as agent:
        stamp = f"state-{datetime.fromtimestamp(agent.clock.now(), agent.tz):%Y%m%d-%H%M%S}.db"
        if where is None:
            agent.paths.backups_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = agent.paths.backups_dir / stamp
        else:
            where = where.expanduser()
            target = where / stamp if where.is_dir() else where
        if target.exists():
            fail(f"{target} already exists.", "Choose a name that is free; nothing is overwritten.")
        if not target.parent.is_dir():
            fail(f"{target.parent} is not a folder.", code=USAGE)
        try:
            agent.db.backup(target)
            copy = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
            try:
                found = problems(copy)
            finally:
                copy.close()
        except (OSError, sqlite3.Error) as exc:  # a copy that failed half way is already removed
            fail(f"The backup failed: {exc}.")
        if found:
            target.unlink(missing_ok=True)
            fail("The copy did not pass its check, so it was removed.", "; ".join(found[:3]))
        say(f"Copied the database to {escape(str(target))} ({size(target.stat().st_size)}).")
        say(f"[dim]{BACKUP_NOTE}[/dim]")


# ---- pa db vacuum ---------------------------------------------------------------------------
@app.command("vacuum")
def vacuum() -> None:
    """Rewrite the file without the space deleted rows left behind (the agent must not be running)."""
    with opened() as agent:
        lock = EngineLock(agent.paths.run_dir / LOCK_NAME)
        if not lock.acquire():
            fail(
                f"The agent is running (process {lock.holder_pid()}).",
                "Stop it first: close the terminal that runs `pa serve`, or `pa service uninstall` "
                "if it runs as a service (`pa service install` starts it again).",
            )
        try:
            before = footprint(agent.db, agent.paths.db_file)
            free = shutil.disk_usage(agent.paths.home).free
            if free < before.total_bytes * 1.2:
                fail(
                    f"Rewriting needs room for a second copy ({size(before.total_bytes)}) "
                    f"and {size(free)} is free.",
                    "Free some space first.",
                )
            try:
                agent.db.vacuum()
                agent.db.checkpoint("TRUNCATE")
            except sqlite3.OperationalError as exc:
                fail(f"Could not rewrite the database: {exc}.", "Another program is using it.")
            after = footprint(agent.db, agent.paths.db_file)
        finally:
            lock.release()
        say(f"{size(before.total_bytes)} -> {size(after.total_bytes)}")


@app.command("backup-all")
def backup_all(
    where: Annotated[Path, typer.Argument(help="A new directory outside PA_HOME.")],
) -> None:
    """Snapshot all spaces, data files and configuration while every engine is stopped."""
    from personal_agent.collaboration.backup import SnapshotError, create_snapshot

    with opened() as agent:
        try:
            result = create_snapshot(agent, where)
        except (SnapshotError, OSError, sqlite3.Error) as exc:
            fail(str(exc))
        say(
            f"Saved {len(result['databases'])} databases and {len(result['files'])} files to {escape(str(where))}."
        )
        say(
            "This private backup includes credentials stored under PA_HOME. Keep it on trusted storage."
        )
        say("Process environment and Railway Variables are not included; retain them separately.")


@app.command("restore-all")
def restore_all(
    snapshot: Annotated[Path, typer.Argument(help="A complete backup-all snapshot.")],
    to: Annotated[
        Path, typer.Option("--to", help="A new home directory; never overwrites an existing home.")
    ],
) -> None:
    """Check every checksum/database and restore into a separate new home."""
    from personal_agent.collaboration.backup import SnapshotError, restore_snapshot

    try:
        result = restore_snapshot(snapshot, to)
    except (SnapshotError, OSError, sqlite3.Error) as exc:
        fail(str(exc))
    say(f"Restored {len(result['databases'])} databases into {escape(str(to))}.")
    say(
        "Old device, node and channel credentials are revoked; pending work and timers are held for review."
    )
    say("Pair devices again with pa web pair (or --principal ID for an existing member).")
    say(
        "Start exactly one control process using the restored PA_HOME; retain the old home until verified."
    )
