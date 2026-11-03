"""Refuse the retired experimental command without executing or forwarding arguments."""

from __future__ import annotations

import json

import typer


def moved_to_lacunai() -> None:
    """Emit a machine-readable migration notice, with no model or state side effects."""
    typer.echo(
        json.dumps(
            {
                "ok": False,
                "status": "deprecated",
                "error": "weight_editing_moved",
                "replacement": "lacunai",
                "help_command": "lacunai --help",
                "removed_in": "0.2.0",
                "forwarded": False,
                "message": (
                    "Weight editing moved to LacunAI. No model or state operation ran. "
                    "Use lacunai --help to select the corresponding model-side command."
                ),
            },
            sort_keys=True,
        ),
        err=True,
    )
    raise typer.Exit(code=2)
