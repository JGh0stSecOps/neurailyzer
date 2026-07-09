"""NeurAIlyzer CLI — state hygiene for AI agents.

Safety-first by construction: ``wipe`` and ``restore`` are **dry-run by default**;
``--commit`` is required to change anything, and a snapshot is taken before every
commit-wipe. This is a skeleton:
``plan()`` paths describe what *would* happen; ``commit()`` paths are guarded
no-ops until the wipers land.
"""

from __future__ import annotations

from enum import StrEnum

import typer
from rich.console import Console

from . import __version__

app = typer.Typer(
    name="neurailyzer",
    help="Wipe the drift, restore to a point in time.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


class Scope(StrEnum):
    """Wipe scopes, least-blast-radius first. See docs/DESIGN.md#scopes."""

    session = "session"
    rag = "rag"
    sandbox = "sandbox"
    models = "models"
    remote = "remote"
    all = "all"


def _version_cb(value: bool) -> None:
    if value:
        console.print(f"neurailyzer {__version__}")
        raise typer.Exit


@app.callback()
def main(
    _version: bool = typer.Option(
        None, "--version", callback=_version_cb, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """NeurAIlyzer root."""


@app.command("list-state")
def list_state(
    scope: list[Scope] = typer.Option(list(Scope), "--scope", "-s", help="Scopes to inspect."),
) -> None:
    """Show what state exists and what a wipe at these scopes *would* affect."""
    console.print("[bold]neurailyzer list-state[/bold] (skeleton)")
    for s in scope:
        console.print(f"  • {s.value}: [dim]no wiper wired yet[/dim]")


@app.command()
def snapshot(
    label: str = typer.Option("manual", "--label", "-l", help="Human label for the restore point."),
) -> None:
    """Take a restore point of current state (not yet implemented)."""
    console.print(f"[bold]snapshot[/bold] label={label!r} [dim](not yet implemented)[/dim]")


@app.command()
def wipe(
    scope: list[Scope] = typer.Argument(..., help="One or more scopes to reset."),
    commit: bool = typer.Option(
        False, "--commit", help="Actually wipe. Without this flag, dry-run only."
    ),
    no_snapshot: bool = typer.Option(
        False, "--no-snapshot", help="Skip the pre-wipe snapshot (dangerous)."
    ),
) -> None:
    """Reset state at the given scope(s). Dry-run unless --commit."""
    scopes = ", ".join(s.value for s in scope)
    if not commit:
        console.print(f"[yellow]DRY-RUN[/yellow] would wipe: [bold]{scopes}[/bold]")
        console.print("[dim]nothing changed. re-run with --commit to act.[/dim]")
        return
    # --- the safety gate that tests assert on ---
    if Scope.all in scope:
        # A full factory reset requires an extra confirmation.
        console.print("[red]refusing[/red] --scope all --commit without a confirmation token")
        raise typer.Exit(code=2)
    if not no_snapshot:
        console.print("[dim](would snapshot before wiping)[/dim]")
    console.print(f"[red]COMMIT[/red] wipe: [bold]{scopes}[/bold] [dim](wipers land later)[/dim]")


@app.command()
def restore(
    to: str = typer.Option(..., "--to", help="Point in time (ISO-8601) or snapshot id."),
    commit: bool = typer.Option(False, "--commit", help="Actually restore. Dry-run without it."),
) -> None:
    """Roll state back to a point in time (not yet implemented)."""
    mode = "COMMIT" if commit else "DRY-RUN"
    console.print(f"[bold]{mode}[/bold] restore --to {to!r} [dim](not yet implemented)[/dim]")


mcp_app = typer.Typer(help="MCP server.")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Serve NeurAIlyzer's verbs as MCP tools (not yet implemented)."""
    console.print("[dim]mcp serve — not yet implemented[/dim]")


if __name__ == "__main__":  # pragma: no cover
    app()
