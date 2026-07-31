"""NeurAIlyzer CLI -- state hygiene for AI agents.

Safety-first by construction: ``wipe`` and ``restore`` are **dry-run by
default**; ``--commit`` is required to change anything, and a snapshot is taken
before every commit-wipe (and before every commit-restore, so a restore is
itself reversible). ``--scope all --commit`` additionally demands
``--confirm all``.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__, core
from .config import (
    DEFAULT_CONFIG_PATH,
    ENV_CONFIG,
    FILE_SCOPES,
    REMOTE_SCOPE,
    ConfigError,
    load,
)
from .config import Config as Config  # noqa: PLC0414 — re-exported for library use
from .snapshots import RestorePlan, SnapshotError, SnapshotStore
from .wipers.base import WipePlan

app = typer.Typer(
    name="neurailyzer",
    help=(
        "Wipe the drift, restore to a point in time.\n\n"
        "Exit codes: 0 ok · 1 the operation ran but did not fully succeed "
        "(nothing to restore, verify failed) · 2 refused before doing "
        "anything (bad config, missing confirmation) · 3 refused because a "
        "targeted harness looks like it is running (--force overrides)."
    ),
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
        err_console.print(f"[red]config error:[/red] {escape(str(exc))}")
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
        shown = st.configured and st.available and st.counted
        table.add_row(
            s.value,
            status,
            "\n".join(st.roots) or "--",
            str(st.file_count) if shown else "?" if st.configured else "--",
            str(st.total_bytes) if shown else "?" if st.configured else "--",
            str(st.kept_count) if shown else "--",
        )
    console.print(table)
    if cfg.remote:
        console.print(
            "[dim]remote: '?' means not counted -- list-state makes no network "
            "call. Run `neurailyzer wipe remote` for a real plan.[/dim]"
        )
    if cfg.source is None:
        console.print("[dim]no config file found -- nothing is targeted. see README Config.[/dim]")


@app.command()
def detect(
    enable: bool = typer.Option(
        False, "--enable", help="Write/merge a [presets] block enabling everything found."
    ),
) -> None:
    """Find known harnesses installed on this machine and what they accumulate."""
    from . import presets as presets_mod

    found = presets_mod.detect_installed()
    if not found:
        console.print(
            "[dim]no known harnesses found. "
            f"known presets: {', '.join(sorted(presets_mod.REGISTRY))}[/dim]"
        )
        return
    for det in found:
        p = det.preset
        console.print(f"[bold]{p.name}[/bold] ({p.id}) -- {p.vendor}")
        for scope, templates in (("session", p.session), ("sandbox", p.sandbox)):
            for path in presets_mod.expand_existing(templates):
                console.print(f"  {scope}: {escape(str(path))}")
        for raw in p.keep:
            console.print(f"  [cyan]keep:[/cyan] {escape(raw)}")
        if p.notes:
            console.print(f"  [dim]{p.notes}[/dim]")
    if not enable:
        console.print(
            "[dim]run `neurailyzer detect --enable` to enable these presets in config, "
            "then `neurailyzer list-state` / `wipe`.[/dim]"
        )
        return

    ids = sorted(det.preset.id for det in found)
    cfg_path = (
        Path(_state["config"])
        if _state["config"]
        else Path(os.environ.get(ENV_CONFIG, str(DEFAULT_CONFIG_PATH))).expanduser()
    )
    _merge_presets_into_config(cfg_path, ids)
    console.print(f"[green]enabled[/green] {', '.join(ids)} in {cfg_path}")
    console.print("[dim]close a harness before wiping its live session store.[/dim]")


def _merge_presets_into_config(cfg_path: Path, ids: list[str]) -> None:
    """Create or minimally edit the TOML file so [presets] enabled covers *ids*."""
    import re

    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        body = "[presets]\nenabled = [" + ", ".join(f'"{i}"' for i in ids) + "]\n"
        cfg_path.write_text(body, encoding="utf-8")
        return
    text = cfg_path.read_text(encoding="utf-8")
    existing: list[str] = []
    m = re.search(r"(?ms)^\[presets\]\s*?$.*?^enabled\s*=\s*\[(?P<items>[^\]]*)\]", text)
    if m:
        existing = re.findall(r'"([^"]+)"', m.group("items"))
        merged = existing + [i for i in ids if i not in existing]
        new_line = "enabled = [" + ", ".join(f'"{i}"' for i in merged) + "]"
        start, end = m.span()
        block = text[start:end]
        block = re.sub(r"enabled\s*=\s*\[[^\]]*\]", new_line, block)
        text = text[:start] + block + text[end:]
    else:
        text = text.rstrip("\n") + "\n\n[presets]\nenabled = ["
        text += ", ".join(f'"{i}"' for i in ids) + "]\n"
    cfg_path.write_text(text, encoding="utf-8")


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
        for bad in store.corrupt:
            err_console.print(f"[red]unreadable snapshot[/red] {escape(bad)}")
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
    force: bool = typer.Option(
        False, "--force", help="Wipe even if a targeted harness looks like it is running."
    ),
) -> None:
    """Reset state at the given scope(s). Dry-run unless --commit."""
    cfg = _load_config()
    scopes = core.expand_scopes([s.value for s in scope])
    label = ", ".join(s.value for s in scope)

    for written, actual in cfg.symlinked_targets:
        console.print(
            f"[yellow]note:[/yellow] {escape(written)} is a symlink -- the wipe lands "
            f"on [bold]{escape(actual)}[/bold]"
        )
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

    if not _liveness_ok(cfg, scopes, force=force):
        raise typer.Exit(code=3)

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
    incomplete = [p.scope for p in report.plans if not p.complete]
    if incomplete:
        err_console.print(
            f"[red]INCOMPLETE[/red] for: {', '.join(sorted(set(incomplete)))} -- "
            "part of the target could not be read or enumerated, so state you "
            "asked to remove may still exist. See the notes above."
        )
    if bad:
        err_console.print(f"[red]verify FAILED[/red] for: {', '.join(bad)}")
        raise typer.Exit(code=1)
    if incomplete:
        raise typer.Exit(code=1)
    console.print("[green]verified[/green] -- state matches the plan.")


def _liveness_ok(
    cfg: Config,
    scopes: list[str],
    *,
    force: bool,
    extra_roots: tuple[Path, ...] = (),
) -> bool:
    """Refuse (best-effort) to touch a live harness's session store."""
    from . import liveness
    from . import presets as presets_mod

    roots = tuple(r for s in scopes for r in cfg.roots_for(s)) + extra_roots
    # blame a WAL sidecar on the harness that owns it, not on every preset
    by_preset: dict[str, tuple[Path, ...]] = {}
    for pid in cfg.presets:
        preset = presets_mod.REGISTRY.get(pid)
        if preset is None:
            continue
        owned = presets_mod.expand_existing((*preset.session, *preset.sandbox))
        by_preset[pid] = tuple(r for r in owned if r in roots) or (owned if not roots else ())
    live = liveness.check_enabled(list(cfg.presets), roots, by_preset)
    if not live:
        return True
    for entry in live:
        err_console.print(f"[yellow]{entry.advice}[/yellow]")
        for reason in entry.reasons:
            err_console.print(f"    [dim]{escape(reason)}[/dim]")
    if force:
        console.print("[red]--force:[/red] wiping anyway.")
        return True
    err_console.print(
        "[red]refusing[/red] -- wiping a live session store can corrupt it "
        "rather than reset it. Close the harness, or re-run with --force."
    )
    return False


def _print_plans(plans: list[WipePlan], scopes: list[str]) -> None:
    covered = set()
    for p in plans:
        covered.add(p.scope)
        console.print(
            f"  - [bold]{p.scope}[/bold]: {escape(p.description)} "
            f"[dim]({p.item_count} item(s), {p.bytes_total} bytes)[/dim]"
        )
        for note in p.notes:
            console.print(f"      [cyan]{escape(note)}[/cyan]")
    for s in scopes:
        if s in covered:
            continue
        if s in FILE_SCOPES:
            console.print(f"  - {s}: [yellow]not configured -- skipped[/yellow]")
        elif s == REMOTE_SCOPE:
            console.print(
                f"  - {s}: [yellow]no providers configured -- skipped[/yellow] "
                "[dim](see \\[remote.<provider>] in the README)[/dim]"
            )
        else:
            console.print(f"  - {s}: [dim]no adapter yet -- skipped[/dim]")


@app.command()
def restore(
    to: str = typer.Option(..., "--to", help="Point in time (ISO-8601) or snapshot id."),
    commit: bool = typer.Option(False, "--commit", help="Actually restore. Dry-run without it."),
    force: bool = typer.Option(
        False, "--force", help="Restore even if a targeted harness looks like it is running."
    ),
) -> None:
    """Roll state back to a point in time (nearest snapshot at/before it)."""
    cfg = _load_config()
    store = SnapshotStore(cfg.snapshot_dir)
    try:
        snap = store.resolve(to)
        for bad in store.corrupt:
            err_console.print(
                f"[yellow]warning:[/yellow] unreadable snapshot {bad} -- "
                "it was skipped when resolving"
            )
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
    if snap.remote_manifest:
        # Remote deletes cannot be undone, so the ids are all this snapshot
        # can offer. Reporting them is the whole reason it records them.
        console.print(
            "[yellow]note:[/yellow] this restore point also recorded remote "
            "objects that were deleted. They CANNOT be restored -- listed "
            "here only so you know what is gone:"
        )
        for provider, entry in sorted(snap.remote_manifest.items()):
            ids = entry.get("item_ids") or []
            console.print(f"  [bold]{provider}[/bold]: {len(ids)} object(s)")
            for item in ids[:20]:
                console.print(f"    [dim]{escape(str(item))}[/dim]")
            if len(ids) > 20:
                console.print(f"    [dim]... and {len(ids) - 20} more[/dim]")
    if commit:
        # a restore rewrites and deletes files, so the live-harness hazard
        # applies here too -- and a rewritten SQLite db under a live -wal is
        # worse than either alone.
        snap_roots = tuple(Path(p) for rs in snap.targets.values() for p in rs)
        if not _liveness_ok(cfg, [], force=force, extra_roots=snap_roots):
            raise typer.Exit(code=3)
        # a restore is destructive too -- snapshot current state first
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
        console.print(f"  [green]restore[/green] {escape(p)}")
    for p in plan.removed:
        console.print(f"  [red]remove[/red]  {escape(p)} [dim](did not exist at that time)[/dim]")
    for p in plan.skipped_keep:
        console.print(f"  [cyan]keep[/cyan]    {escape(p)}")
    for e in plan.errors:
        err_console.print(f"  [red]error[/red]   {escape(e)}")
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
