"""`pa vault`: the knowledge base, from the terminal.

The knowledge base is a folder of Markdown notes that the assistant, the connected agents and you
all add to, and that Obsidian opens as it is. This is for looking at it without the browser:
`status` says where the folder is and what is in it, `search` and `lint` look through it, `open`
hands a note (or the whole folder) to Obsidian, `log` lists the latest changes, and `undo` and
`restore` take changes back. Notes themselves are written in the page, in Obsidian, or by the
assistant; nothing here edits a note.
"""

from __future__ import annotations

import webbrowser
from collections import defaultdict
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape

from personal_agent.clock import format_local
from personal_agent.commands.common import USAGE, console, fail, opened, say, table

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.vault.store import Vault

app = typer.Typer(
    help="The knowledge base: notes you can also open in Obsidian.", invoke_without_command=True
)

WHO = {"user": "you", "agent": "assistant", "dream": "night tidy-up", "system": "system"}


def _vault(agent: App) -> Vault:
    if agent.vault is None:
        fail("The knowledge base is switched off.", "Set [vault] enabled = true.", code=USAGE)
    return agent.vault


@app.callback()
def _status(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    with opened() as agent:
        vault = _vault(agent)
        say(f"folder     {escape(str(vault.root))}")
        if not vault.exists():
            say("[dim]It does not exist yet; it is made when the first note is written.[/dim]")
            return
        stats = vault.stats()
        areas = ", ".join(f"{name} {count}" for name, count in stats["areas"].items() if count)
        say(f"notes      {stats['notes']}" + (f"  ({areas})" if areas else ""))
        if waiting := len(vault.backlog(limit=100)):
            say(f"waiting    {waiting} captured note(s) that nothing in wiki/ points to yet")
        say(f"to look at {len(vault.lint())} thing(s)  (`pa vault lint`)")
        say("Obsidian   Manage vaults > Open folder as vault, or `pa vault open`")


@app.command("search")
def search(
    query: Annotated[
        str, typer.Argument(help="Words to look for. Without any, the newest notes.")
    ] = "",
    folder: Annotated[str, typer.Option("--folder", "-f", help="Only in this folder.")] = "",
    tag: Annotated[str, typer.Option("--tag", "-t", help="Only notes with this tag.")] = "",
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many notes.")] = 10,
) -> None:
    """Find notes by their words, title, tags and links."""
    from personal_agent.memory.store import Actor

    with opened() as agent:
        vault = _vault(agent)
        hits = vault.search(
            query.strip(),
            folder=folder.strip().strip("/"),
            tag=tag.strip().lstrip("#"),
            limit=max(1, min(limit, 50)),
            actor=Actor.USER,
        )
        if not hits:
            say("[dim]Nothing found.[/dim]")
            return
        found = table("note", "title", "updated")
        for hit in hits:
            found.add_row(
                escape(hit.path), escape(hit.title), format_local(hit.updated, agent.tz)[:10]
            )
        console.print(found)


@app.command("lint")
def lint() -> None:
    """Things in the notes worth a look: broken links, notes nothing links to, material not yet
    written up. It only reports."""
    with opened() as agent:
        issues = _vault(agent).lint()
        if not issues:
            say("[dim]Nothing to look at.[/dim]")
            return
        kinds: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for issue in issues:
            kinds[issue.kind].append((issue.path, issue.detail))
        for kind, rows in sorted(kinds.items()):
            say(f"[bold]{escape(kind)}[/bold] ({len(rows)})")
            for path, detail in rows[:20]:
                say(f"  {escape(path)}  {escape(detail)}".rstrip())
            if len(rows) > 20:
                say(f"  [dim]... and {len(rows) - 20} more[/dim]")


@app.command("open")
def open_note(
    path: Annotated[
        str, typer.Argument(help="A note, such as wiki/张三.md. Without one, the whole folder.")
    ] = "",
    print_only: Annotated[
        bool, typer.Option("--print", help="Only print the obsidian:// address.")
    ] = False,
) -> None:
    """Open a note, or the folder, in Obsidian."""
    from personal_agent.channels.web.vault_views import obsidian_link
    from personal_agent.vault.store import VaultError, clean_path

    with opened() as agent:
        vault = _vault(agent)
        try:
            rel = clean_path(path) if path.strip() else ""
        except VaultError as exc:
            fail(str(exc), code=USAGE)
        address = obsidian_link(vault, rel)
    say(escape(address))
    if print_only:
        return
    if not webbrowser.open(address):
        say("[dim]Nothing opened it. Is Obsidian installed? The address works once it is.[/dim]")


@app.command("index")
def index() -> None:
    """Write the contents note (wiki/index.md) again right now."""
    from personal_agent.vault.generated import refresh_index

    with opened() as agent:
        vault = _vault(agent)
        if not vault.exists():
            fail("There is no knowledge base folder yet.", "Write a note first.")
        refresh_index(vault, agent.clock.now(), agent.tz)
        say("Wrote wiki/index.md.")


@app.command("log")
def log(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many changes.")] = 20,
) -> None:
    """The latest changes to notes, with the version and run to undo them by."""
    with opened() as agent:
        rows = _vault(agent).recent(limit)
        if not rows:
            say("[dim]No changes yet.[/dim]")
            return
        changes = table("version", "when", "who", "what", "note", "run")
        for row in rows:
            changes.add_row(
                str(row["id"]),
                format_local(row["created_at"], agent.tz)[:16],
                WHO.get(row["actor"], row["actor"]),
                row["action"],
                escape(row["path"]),
                escape(row["run_id"] or "-"),
            )
        console.print(changes)
        say(
            "[dim]`pa vault restore VERSION` puts one note back; `pa vault undo RUN` a whole run.[/dim]"
        )


@app.command("restore")
def restore(
    version: Annotated[int, typer.Argument(help="A version number from `pa vault log`.")],
) -> None:
    """Put a note back the way it was before that change."""
    from personal_agent.vault.store import VaultError

    with opened() as agent:
        try:
            path = _vault(agent).restore(version)
        except VaultError as exc:
            fail(str(exc))
        say(f"Put {escape(path)} back.")


@app.command("undo")
def undo(run: Annotated[str, typer.Argument(help="A run from `pa vault log`.")]) -> None:
    """Take back everything one run wrote. A note edited since is left as it is."""
    with opened() as agent:
        undone, left = _vault(agent).revert_run(run)
        if not undone and not left:
            fail(f"That run changed no notes: {run}.")
        for path in undone:
            say(f"Took back {escape(path)}")
        for path in left:
            say(f"[yellow]Left {escape(path)}: it was changed after that run.[/yellow]")
