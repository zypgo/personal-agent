"""Sub-commands of `pa` that live outside cli.py. Each module builds a typer app from the pieces
cli.py hands it (how to open the app, where to print), so the commands can be tested in-process
against the same wiring as the rest of the CLI."""
