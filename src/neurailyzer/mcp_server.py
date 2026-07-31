"""MCP server: expose NeurAIlyzer's verbs as tools for agents.

Tools: ``nl_list_state``, ``nl_snapshot``, ``nl_wipe``, ``nl_restore`` — thin
wrappers over :mod:`neurailyzer.core`, so every safety gate (dry-run default,
snapshot-before-commit, keep-list) holds identically here. ``scope=all`` is
refused entirely on this surface: a factory reset requires a human at the CLI.

Built for the ``mcp`` 2.x SDK. Stdio is the default transport;
``streamable-http`` (the stateless-capable transport of the current spec) is
available via ``neurailyzer mcp serve --transport streamable-http``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import __version__, core
from .config import FILE_SCOPES, PENDING_SCOPES, load
from .snapshots import SnapshotError, SnapshotStore


class McpUnavailable(RuntimeError):
    """The optional ``mcp`` dependency is not installed."""


def build_server(config_path: str | None = None) -> Any:
    """Construct the MCPServer with NeurAIlyzer's tools registered."""
    try:
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - exercised only without extras
        raise McpUnavailable(
            "MCP support is not installed. Install with: pip install 'neurailyzer[mcp]'"
        ) from exc

    server = MCPServer(
        name="neurailyzer",
        version=__version__,
        instructions=(
            "State hygiene for AI agents. nl_wipe and nl_restore are dry-run "
            "unless commit=true; a snapshot is always taken before a commit."
        ),
    )

    @server.tool(description="What state exists per scope, and what a wipe would affect.")
    def nl_list_state() -> list[dict[str, Any]]:
        cfg = load(config_path)
        out = []
        for scope in (*FILE_SCOPES, *PENDING_SCOPES):
            st = core.scope_status(cfg, scope)
            out.append(
                {
                    "scope": st.scope,
                    "configured": st.configured,
                    "adapter_available": st.available,
                    "targets": list(st.roots),
                    "file_count": st.file_count,
                    "total_bytes": st.total_bytes,
                    "keep_list_protected": st.kept_count,
                }
            )
        return out

    @server.tool(description="Take a restore point of all configured state.")
    def nl_snapshot(label: str = "mcp") -> dict[str, Any]:
        cfg = load(config_path)
        targets = {s: cfg.roots_for(s) for s in FILE_SCOPES if cfg.roots_for(s)}
        if not targets:
            return {"ok": False, "reason": "no targets configured"}
        store = SnapshotStore(cfg.snapshot_dir)
        snap = store.take(targets, label)
        store.prune(cfg.retention)
        return {
            "ok": True,
            "id": snap.id,
            "taken_at": snap.taken_at.isoformat(),
            "files": snap.file_count,
            "bytes": snap.total_bytes,
        }

    @server.tool(
        description=(
            "Reset state at the given scopes (session, sandbox, ...). Dry-run "
            "unless commit=true; a snapshot is taken before any commit. "
            "'all' is CLI-only."
        )
    )
    def nl_wipe(scopes: list[str], commit: bool = False) -> dict[str, Any]:
        if "all" in scopes:
            return {
                "ok": False,
                "reason": "scope 'all' is refused over MCP -- a factory reset "
                "requires a human at the CLI (--confirm all).",
            }
        cfg = load(config_path)
        scopes = core.expand_scopes(scopes)
        if not commit:
            plans = core.plan_wipe(cfg, scopes)
            return {
                "ok": True,
                "dry_run": True,
                "plans": [_plan_dict(p) for p in plans],
            }
        report = core.execute_wipe(cfg, scopes, take_snapshot=True, label="mcp-wipe")
        return {
            "ok": all(report.verified.values()),
            "dry_run": False,
            "snapshot": report.snapshot.id if report.snapshot else None,
            "plans": [_plan_dict(p) for p in report.plans],
            "verified": report.verified,
        }

    @server.tool(
        description=(
            "Roll state back to a point in time (ISO-8601) or snapshot id. "
            "Dry-run unless commit=true; commits snapshot current state first."
        )
    )
    def nl_restore(to: str, commit: bool = False) -> dict[str, Any]:
        cfg = load(config_path)
        store = SnapshotStore(cfg.snapshot_dir)
        try:
            snap = store.resolve(to)
        except SnapshotError as exc:
            return {"ok": False, "reason": str(exc)}
        if snap is None:
            return {"ok": False, "reason": f"no snapshot at or before {to!r}"}
        if commit:
            store.take(
                {s: tuple(Path(p) for p in roots) for s, roots in snap.targets.items()},
                label="pre-restore",
            )
            plan = store.restore(snap, cfg.keep)
        else:
            plan = store.plan_restore(snap, cfg.keep)
        return {
            "ok": not plan.errors,
            "dry_run": not commit,
            "snapshot": snap.id,
            "restored": plan.restored,
            "removed": plan.removed,
            "keep_list_skipped": plan.skipped_keep,
            "errors": plan.errors,
        }

    return server


def _plan_dict(p: Any) -> dict[str, Any]:
    return {
        "scope": p.scope,
        "description": p.description,
        "items": p.item_count,
        "bytes": p.bytes_total,
        "reversible": p.reversible,
        "notes": list(p.notes),
    }


def serve(config_path: str | None = None, transport: str = "stdio") -> None:
    """Run the MCP server on the chosen transport."""
    server = build_server(config_path)
    if transport not in ("stdio", "streamable-http"):
        raise ValueError(f"unsupported transport {transport!r} (stdio, streamable-http)")
    server.run(transport)
