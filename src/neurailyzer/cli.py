"""NeurAIlyzer CLI -- state hygiene for AI agents.

Safety-first by construction: ``wipe`` and ``restore`` are **dry-run by
default**; ``--commit`` is required to change anything, and a snapshot is taken
before every commit-wipe (and before every commit-restore, so a restore is
itself reversible). ``--scope all --commit`` additionally demands
``--confirm all``.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__, core
from .config import FILE_SCOPES, ConfigError, load
from .config import Config as Config  # noqa: PLC0414 — re-exported for library use
from .snapshots import RestorePlan, SnapshotError, SnapshotStore
from .wipers.base import WipePlan

app = typer.Typer(
    name="neurailyzer",
    help="Wipe the drift, restore to a point in time.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)

_state: dict[str, str | None] = {"config": None}


class Scope(StrEnum):
    """Wipe scopes, least-blast-radius first. See docs/DESIGN.md#scopes."""

    session = "session"
    rag = "rag"
    sandbox = "sandbox"
    models = "models"
    remote = "remote"
    all = "all"


def _load_config() -> Config:
    try:
        return load(_state["config"])
    except ConfigError as exc:
        err_console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def _version_cb(value: bool) -> None:
    if value:
        console.print(f"neurailyzer {__version__}")
        raise typer.Exit


@app.callback()
def main(
    _version: bool = typer.Option(
        None, "--version", callback=_version_cb, is_eager=True, help="Show version and exit."
    ),
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Config file (default: ~/.neurailyzer/config.toml)."
    ),
) -> None:
    """NeurAIlyzer root."""
    _state["config"] = str(config) if config is not None else None


@app.command("list-state")
def list_state(
    scope: list[Scope] = typer.Option(
        [s for s in Scope if s is not Scope.all], "--scope", "-s", help="Scopes to inspect."
    ),
) -> None:
    """Show what state exists and what a wipe at these scopes *would* affect."""
    cfg = _load_config()
    table = Table(title="neurailyzer list-state")
    for col in ("scope", "status", "targets", "files", "bytes", "kept"):
        table.add_column(col)
    for s in scope:
        if s is Scope.all:
            continue
        st = core.scope_status(cfg, s.value)
        if not st.available:
            status = "[dim]no adapter yet (planned)[/dim]"
        elif not st.configured:
            status = "[yellow]not configured[/yellow]"
        else:
            status = "[green]ready[/green]"
        table.add_row(
            s.value,
            status,
            "\n".join(st.roots) or "--",
            str(st.file_count) if st.configured and st.available else "--",
            str(st.total_bytes) if st.configured and st.available else "--",
            str(st.kept_count) if st.configured and st.available else "--",
        )
    console.print(table)
    if cfg.source is None:
        console.print("[dim]no config file found -- nothing is targeted. see README Config.[/dim]")


@app.command()
def snapshot(
    label: str = typer.Option("manual", "--label", "-l", help="Human label for the restore point."),
    list_: bool = typer.Option(False, "--list", help="List existing restore points and exit."),
) -> None:
    """Take a restore point of all configured state (or list existing ones)."""
    cfg = _load_config()
    store = SnapshotStore(cfg.snapshot_dir)
    if list_:
        snaps = store.list()
        if not snaps:
            console.print("[dim]no snapshots yet[/dim]")
            return
        table = Table(title="snapshots")
        for col in ("id", "taken at (UTC)", "label", "scopes", "files", "bytes"):
            table.add_column(col)
        for s in snaps:
            table.add_row(
                s.id,
                s.taken_at.strftime("%Y-%m-%d %H:%M:%SZ"),
                s.label,
                ",".join(s.scopes),
                str(s.file_count),
                str(s.total_bytes),
            )
        console.print(table)
        return
    targets = {s: cfg.roots_for(s) for s in FILE_SCOPES if cfg.roots_for(s)}
    if not targets:
        console.print("[yellow]nothing to snapshot[/yellow] -- no targets configured.")
        raise typer.Exit(code=1)
    snap = store.take(targets, label)
    store.prune(cfg.retention)
    console.print(
        f"[green]snapshot[/green] {snap.id} ({snap.file_count} file(s), {snap.total_bytes} bytes)"
    )


@app.command()
def wipe(
    scope: list[Scope] = typer.Argument(..., help="One or more scopes to reset."),
    commit: bool = typer.Option(
        False, "--commit", help="Actually wipe. Without this flag, dry-run only."
    ),
    no_snapshot: bool = typer.Option(
        False, "--no-snapshot", help="Skip the pre-wipe snapshot (dangerous)."
    ),
    confirm: str = typer.Option(
        "", "--confirm", help="Required for --scope all --commit: type 'all'."
    ),
) -> None:
    """Reset state at the given scope(s). Dry-run unless --commit."""
    cfg = _load_config()
    scopes = core.expand_scopes([s.value for s in scope])
    label = ", ".join(s.value for s in scope)

    if not commit:
        plans = core.plan_wipe(cfg, scopes)
        console.print(f"[yellow]DRY-RUN[/yellow] would wipe: [bold]{label}[/bold]")
        _print_plans(plans, scopes)
        console.print("[dim]nothing changed. re-run with --commit to act.[/dim]")
        return

    # --- the safety gate that tests assert on ---
    if Scope.all in scope and confirm != "all":
        err_console.print(
            "[red]refusing[/red] --scope all --commit without a confirmation token. "
            "Re-run with [bold]--confirm all[/bold] to factory-reset every scope."
        )
        raise typer.Exit(code=2)

    report = core.execute_wipe(
        cfg, scopes, take_snapshot=not no_snapshot, label=f"pre-wipe-{label.replace(', ', '-')}"
    )
    if not report.plans:
        console.print("[yellow]nothing to wipe[/yellow] -- no configured targets in scope.")
        return
    if no_snapshot:
        console.print("[red bold]--no-snapshot: this wipe has NO restore point.[/red bold]")
    elif report.snapshot is not None:
        console.print(f"[dim]snapshot {report.snapshot.id} taken before wiping[/dim]")
    console.print(f"[red]COMMIT[/red] wipe: [bold]{label}[/bold]")
    _print_plans(list(report.plans), scopes)
    bad = [s for s, ok in report.verified.items() if not ok]
    if bad:
        err_console.print(f"[red]verify FAILED[/red] for: {', '.join(bad)}")
        raise typer.Exit(code=1)
    console.print("[green]verified[/green] -- state matches the plan.")


def _print_plans(plans: list[WipePlan], scopes: list[str]) -> None:
    covered = set()
    for p in plans:
        covered.add(p.scope)
        console.print(
            f"  - [bold]{p.scope}[/bold]: {p.description} "
            f"[dim]({p.item_count} item(s), {p.bytes_total} bytes)[/dim]"
        )
        for note in p.notes:
            console.print(f"      [cyan]{note}[/cyan]")
    for s in scopes:
        if s in covered:
            continue
        if s in FILE_SCOPES:
            console.print(f"  - {s}: [yellow]not configured -- skipped[/yellow]")
        else:
            console.print(f"  - {s}: [dim]no adapter yet -- skipped[/dim]")


@app.command()
def restore(
    to: str = typer.Option(..., "--to", help="Point in time (ISO-8601) or snapshot id."),
    commit: bool = typer.Option(False, "--commit", help="Actually restore. Dry-run without it."),
) -> None:
    """Roll state back to a point in time (nearest snapshot at/before it)."""
    cfg = _load_config()
    store = SnapshotStore(cfg.snapshot_dir)
    try:
        snap = store.resolve(to)
    except SnapshotError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    if snap is None:
        err_console.print(f"[red]no snapshot at or before[/red] {to!r} -- see snapshot --list")
        raise typer.Exit(code=1)

    mode = "COMMIT" if commit else "DRY-RUN"
    console.print(
        f"[bold]{mode}[/bold] restore --to {to!r} -> snapshot [bold]{snap.id}[/bold] "
        f"(taken {snap.taken_at.isoformat()})"
    )
    if commit:
        # a restore is destructive too — snapshot current state first
        pre = store.take(
            {s: tuple(Path(p) for p in roots) for s, roots in snap.targets.items()},
            label="pre-restore",
        )
        console.print(f"[dim]snapshot {pre.id} taken before restoring[/dim]")
        plan = store.restore(snap, cfg.keep)
    else:
        plan = store.plan_restore(snap, cfg.keep)
    _print_restore(plan)
    if not commit:
        console.print("[dim]nothing changed. re-run with --commit to act.[/dim]")
    elif plan.errors:
        raise typer.Exit(code=1)
    else:
        console.print("[green]restored.[/green]")


def _print_restore(plan: RestorePlan) -> None:
    for p in plan.restored:
        console.print(f"  [green]restore[/green] {p}")
    for p in plan.removed:
        console.print(f"  [red]remove[/red]  {p} [dim](did not exist at that time)[/dim]")
    for p in plan.skipped_keep:
        console.print(f"  [cyan]keep[/cyan]    {p}")
    for e in plan.errors:
        err_console.print(f"  [red]error[/red]   {e}")
    if plan.change_count == 0:
        console.print("  [dim]state already matches this snapshot[/dim]")


mcp_app = typer.Typer(help="MCP server.")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("serve")
def mcp_serve(
    transport: str = typer.Option(
        "stdio", "--transport", "-t", help="stdio (default) or streamable-http."
    ),
) -> None:
    """Serve NeurAIlyzer's verbs as MCP tools (nl_list_state, nl_snapshot, ...)."""
    from .mcp_server import serve

    serve(config_path=_state["config"], transport=transport)


if __name__ == "__main__":  # pragma: no cover
    app()
