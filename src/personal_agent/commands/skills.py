"""`pa skills`: see, check, install and remove the skills the agent can load."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from personal_agent.app import App
from personal_agent.skills.install import (
    InstallError,
    Review,
    check_git_url,
    fetched,
    inspect_folder,
    install_folder,
    remove_skill,
)
from personal_agent.skills.parse import SkillFormatError, parse_skill
from personal_agent.skills.store import SkillStore, read_skill_file


def build(open_app: Callable[[], App], console: Console) -> typer.Typer:
    skills_app = typer.Typer(
        help="Skills: folders of instructions the agent loads when a task matches.",
        invoke_without_command=True,
    )

    def show_review(review: Review) -> None:
        console.print(f"[bold]{escape(review.name or review.folder.name)}[/bold]")
        if review.description:
            console.print(escape(review.description))
        console.print(f"{len(review.files)} file(s), {review.total_bytes:,} bytes")
        if review.scripts:
            console.print(
                "[yellow]Contains scripts[/yellow] (they can only run inside the sandbox, and each "
                "run asks for your approval):"
            )
            for script in review.scripts[:15]:
                console.print(f"  {escape(script)}")
            if len(review.scripts) > 15:
                console.print(f"  ... and {len(review.scripts) - 15} more")
        for warning in review.warnings:
            console.print(f"[yellow]warning:[/yellow] {escape(warning)}")
        for error in review.errors:
            console.print(f"[red]error:[/red] {escape(error)}")

    def list_skills() -> None:
        opened = open_app()
        try:
            scan = opened.skills.scan()
            if not scan.skills and not scan.problems:
                console.print(
                    "[dim]No skills installed. `pa init` adds the starter skills; "
                    "`pa skills install <folder or https address>` adds more.[/dim]"
                )
                return
            if scan.skills:
                table = Table(box=None, pad_edge=False)
                for column in ("name", "model may load", "description"):
                    table.add_column(column)
                for skill in scan.skills:
                    brief = (
                        skill.description
                        if len(skill.description) <= 90
                        else skill.description[:89] + "…"
                    )
                    table.add_row(
                        escape(skill.name),
                        "no (owner only: $name)" if skill.disable_model_invocation else "yes",
                        escape(brief),
                    )
                console.print(table)
            for problem in scan.problems:
                colour, label = ("red", "skipped") if problem.fatal else ("yellow", "warning")
                console.print(
                    f"[{colour}]{label}[/{colour}] {escape(problem.folder)}: {escape(problem.message)}"
                )
        finally:
            opened.close()

    @skills_app.callback()
    def _list(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is None:
            list_skills()

    @skills_app.command("list")
    def list_() -> None:
        """Installed skills, and any that could not be used."""
        list_skills()

    @skills_app.command("show")
    def show(name: str = typer.Argument(..., help="The skill's name.")) -> None:
        """Print a skill's instructions and the files that belong to it."""
        opened = open_app()
        try:
            skill = opened.skills.scan().by_name().get(name)
            if skill is None:
                console.print(f"[red]No installed skill named {escape(name)}.[/red]")
                raise typer.Exit(1)
            parsed = parse_skill(read_skill_file(skill.location), folder_name=skill.directory.name)
            console.print(f"[bold]{escape(skill.name)}[/bold]  {escape(str(skill.directory))}")
            console.print(escape(skill.description))
            for label, value in (
                ("license", skill.license),
                ("compatibility", skill.compatibility),
            ):
                if value:
                    console.print(f"{label}: {escape(value)}")
            for warning in skill.warnings:
                console.print(f"[yellow]warning:[/yellow] {escape(warning)}")
            files = SkillStore.resources(skill)
            if files:
                console.print("files: " + ", ".join(escape(f) for f in files))
            console.print()
            console.print(escape(parsed.body))
        finally:
            opened.close()

    @skills_app.command("validate")
    def validate(folder: Annotated[Path, typer.Argument(help="A skill folder to check.")]) -> None:
        """Check a folder without installing it."""
        review = inspect_folder(folder.expanduser())
        show_review(review)
        if not review.ok:
            raise typer.Exit(1)
        console.print("[green]This folder is a valid skill.[/green]")

    @skills_app.command("install")
    def install(
        source: str = typer.Argument(
            ..., help="A skill folder on disk, or an https:// git repository."
        ),
        path: str | None = typer.Option(
            None, "--path", help="For a repository: the folder inside it that holds the skill."
        ),
        force: bool = typer.Option(False, "--force", help="Replace a skill of the same name."),
        yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    ) -> None:
        """Copy a skill into the agent's skills folder after showing what is in it."""
        opened = open_app()
        try:
            try:
                if source.startswith(("http://", "https://")):
                    check_git_url(source)
                    console.print(f"Fetching {escape(source)} ...")
                    with fetched(source, path) as folder:
                        _review_and_install(opened, folder, force=force, yes=yes)
                else:
                    _review_and_install(opened, Path(source).expanduser(), force=force, yes=yes)
            except InstallError as exc:
                console.print(f"[red]{escape(str(exc))}[/red]")
                raise typer.Exit(1) from exc
        finally:
            opened.close()

    def _review_and_install(opened: App, folder: Path, *, force: bool, yes: bool) -> None:
        review = inspect_folder(folder)
        show_review(review)
        if not review.ok:
            raise InstallError("this folder cannot be installed")
        console.print(
            "\nA skill is instructions the agent will follow in later runs. Install only skills "
            "whose text you have read or whose author you trust."
        )
        if not yes and not typer.confirm("Install it?", default=False):
            console.print("Not installed.")
            raise typer.Exit(1)
        target = install_folder(opened.paths, folder, force=force)
        console.print(f"[green]Installed[/green] {escape(review.name)} → {escape(str(target))}")

    @skills_app.command("remove")
    def remove(
        name: str = typer.Argument(..., help="The skill's name."),
        yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    ) -> None:
        """Delete an installed skill."""
        opened = open_app()
        try:
            if not yes and not typer.confirm(f"Delete the skill {name}?", default=False):
                console.print("Kept.")
                raise typer.Exit(1)
            try:
                folder = remove_skill(opened.paths, name)
            except InstallError as exc:
                console.print(f"[red]{escape(str(exc))}[/red]")
                raise typer.Exit(1) from exc
            console.print(f"Removed {escape(str(folder))}")
        finally:
            opened.close()

    return skills_app


__all__ = ["SkillFormatError", "build"]
