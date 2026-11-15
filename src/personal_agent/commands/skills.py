"""`pa skills`: see, check, install and remove the skills the agent can load."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape

from personal_agent.commands.common import console, fail, opened, say, table
from personal_agent.skills.install import (
    InstallError,
    Review,
    check_git_url,
    fetched,
    inspect_folder,
    install_folder,
    remove_skill,
)
from personal_agent.skills.parse import parse_skill
from personal_agent.skills.store import SkillStore, read_skill_file

if TYPE_CHECKING:
    from personal_agent.app import App

app = typer.Typer(
    help="Skills: folders of instructions the agent loads when a task matches.",
    invoke_without_command=True,
)

NameArg = Annotated[str, typer.Argument(help="The skill's name.")]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")]


def _show_review(review: Review) -> None:
    say(f"[bold]{escape(review.name or review.folder.name)}[/bold]")
    if review.description:
        say(escape(review.description))
    say(f"{len(review.files)} file(s), {review.total_bytes:,} bytes")
    if review.scripts:
        say(
            "[yellow]Contains scripts[/yellow] (they can only run inside the sandbox, and each "
            "run asks for your approval):"
        )
        for script in review.scripts[:15]:
            say(f"  {escape(script)}")
        if len(review.scripts) > 15:
            say(f"  ... and {len(review.scripts) - 15} more")
    for warning in review.warnings:
        say(f"[yellow]warning:[/yellow] {escape(warning)}")
    for error in review.errors:
        say(f"[red]error:[/red] {escape(error)}")


def _list_skills() -> None:
    with opened() as agent:
        scan = agent.skills.scan()
        if not scan.skills and not scan.problems:
            say(
                "[dim]No skills installed. `pa init` adds the starter skills; "
                "`pa skills install <folder or https address>` adds more.[/dim]"
            )
            return
        if scan.skills:
            rows = table("name", "model may load", "description")
            for skill in scan.skills:
                brief = (
                    skill.description
                    if len(skill.description) <= 90
                    else skill.description[:89] + "…"
                )
                rows.add_row(
                    escape(skill.name),
                    "no (owner only: $name)" if skill.disable_model_invocation else "yes",
                    escape(brief),
                )
            console.print(rows)
        for problem in scan.problems:
            colour, label = ("red", "skipped") if problem.fatal else ("yellow", "warning")
            say(f"[{colour}]{label}[/{colour}] {escape(problem.folder)}: {escape(problem.message)}")


@app.callback()
def _default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _list_skills()


@app.command("list")
def list_() -> None:
    """Installed skills, and any that could not be used."""
    _list_skills()


@app.command("show")
def show(name: NameArg) -> None:
    """Print a skill's instructions and the files that belong to it."""
    with opened() as agent:
        skill = agent.skills.scan().by_name().get(name)
        if skill is None:
            fail(f"No installed skill named {name}.")
        parsed = parse_skill(read_skill_file(skill.location), folder_name=skill.directory.name)
        say(f"[bold]{escape(skill.name)}[/bold]  {escape(str(skill.directory))}")
        say(escape(skill.description))
        for label, value in (("license", skill.license), ("compatibility", skill.compatibility)):
            if value:
                say(f"{label}: {escape(value)}")
        for warning in skill.warnings:
            say(f"[yellow]warning:[/yellow] {escape(warning)}")
        files = SkillStore.resources(skill)
        if files:
            say("files: " + ", ".join(escape(f) for f in files))
        say()
        say(escape(parsed.body))


@app.command("validate")
def validate(folder: Annotated[Path, typer.Argument(help="A skill folder to check.")]) -> None:
    """Check a folder without installing it."""
    review = inspect_folder(folder.expanduser())
    _show_review(review)
    if not review.ok:
        raise typer.Exit(1)
    say("[green]This folder is a valid skill.[/green]")


@app.command("install")
def install(
    source: Annotated[
        str, typer.Argument(help="A skill folder on disk, or an https:// git repository.")
    ],
    path: Annotated[
        str | None,
        typer.Option("--path", help="For a repository: the folder inside it that holds the skill."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Replace a skill of the same name.")
    ] = False,
    yes: Yes = False,
) -> None:
    """Copy a skill into the agent's skills folder after showing what is in it."""
    with opened() as agent:
        try:
            if source.startswith(("http://", "https://")):
                check_git_url(source)
                say(f"Fetching {escape(source)} ...")
                with fetched(source, path) as folder:
                    _review_and_install(agent, folder, force=force, yes=yes)
            else:
                _review_and_install(agent, Path(source).expanduser(), force=force, yes=yes)
        except InstallError as exc:
            fail(str(exc))


def _review_and_install(agent: App, folder: Path, *, force: bool, yes: bool) -> None:
    review = inspect_folder(folder)
    _show_review(review)
    if not review.ok:
        raise InstallError("this folder cannot be installed")
    say(
        "\nA skill is instructions the agent will follow in later runs. Install only skills "
        "whose text you have read or whose author you trust."
    )
    if not yes and not typer.confirm("Install it?", default=False):
        say("Not installed.")
        raise typer.Exit(1)
    target = install_folder(agent.paths, folder, force=force)
    say(f"[green]Installed[/green] {escape(review.name)} → {escape(str(target))}")


@app.command("remove")
def remove(name: NameArg, yes: Yes = False) -> None:
    """Delete an installed skill."""
    with opened() as agent:
        if not yes and not typer.confirm(f"Delete the skill {name}?", default=False):
            say("Kept.")
            raise typer.Exit(1)
        try:
            folder = remove_skill(agent.paths, name)
        except InstallError as exc:
            fail(str(exc))
        say(f"Removed {escape(str(folder))}")
