"""Typer CLI entry point for the GearMeshing-AI proof of concept."""

from __future__ import annotations

import typer

from gearmeshing_ai import __version__

app = typer.Typer(name="gmai", help="Governed autonomous engineering teams powered by Agent Assembly.")


@app.callback()
def main() -> None:
    """Governed autonomous engineering teams powered by Agent Assembly."""


@app.command()
def version() -> None:
    """Print the installed GearMeshing-AI version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
