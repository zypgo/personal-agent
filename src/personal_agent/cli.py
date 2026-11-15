"""`pa` command line entry point."""

from __future__ import annotations

import typer

from personal_agent import __version__

app = typer.Typer(
    name="pa",
    help="A local, always-on personal agent (OpenAI models, Anthropic engineering practices).",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"personal-agent {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version."
    ),
) -> None:
    """personal-agent."""


def main() -> None:
    app()


if __name__ == "__main__":
    main()
