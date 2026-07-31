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

from . import __version__, core, liveness
from .config import FILE_SCOPES, PENDING_SCOPES, REMOTE_SCOPE, Config, ConfigError, load
from .snapshots import SnapshotError, SnapshotStore


class McpUnavailable(RuntimeError):
    """The optional ``mcp`` dependency is not installed."""


def _load_or_reason(config_path: str | None) -> tuple[Config | None, dict[str, Any] | None]:
    """(config, refusal). A bad config is a structured answer, not a traceback.

    An agent can act on ``{"ok": false, "reason": ...}``; it cannot act on an
    exception surfaced as a tool error.
    """
    try:
        return load(config_path), None
    except ConfigError as exc:
        return None, {
            "ok": False,
            "reason": f"configuration problem: {exc}",
            "hint": "run `neurailyzer detect --enable` to create a config, "
            "or point NEURAILYZER_CONFIG at an existing one",
        }


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
    def nl_list_state() -> dict[str, Any]:
        cfg, refusal = _load_or_reason(config_path)
        if cfg is None:
            return refusal or {"ok": False, "reason": "unknown configuration error"}
        out = []
        # REMOTE_SCOPE must appear here: an agent that cannot SEE the
        # scope can still name it, and it is the irreversible one.
        for scope in (*FILE_SCOPES, REMOTE_SCOPE, *PENDING_SCOPES):
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
                    "counted": st.counted,
                    "irreversible": scope == REMOTE_SCOPE,
                }
            )
        return {"ok": True, "scopes": out}

    @server.tool(description="Take a restore point of all configured state.")
    def nl_snapshot(label: str = "mcp") -> dict[str, Any]:
        cfg, refusal = _load_or_reason(config_path)
        if cfg is None:
            return refusal or {"ok": False, "reason": "unknown configuration error"}
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
            "Scopes 'all' and 'remote' can only be COMMITTED by a human at "
            "the CLI -- ask over MCP with commit=false to see their plan."
        )
    )
    def nl_wipe(scopes: list[str], commit: bool = False, force: bool = False) -> dict[str, Any]:
        if "all" in scopes:
            return {
                "ok": False,
                "reason": "scope 'all' is refused over MCP -- a factory reset "
                "requires a human at the CLI (--confirm all).",
            }
        # Local wipes are snapshot-protected and therefore reversible; remote
        # deletes are NOT. An agent may plan one, but only a human commits it.
        if commit and REMOTE_SCOPE in core.expand_scopes(scopes):
            return {
                "ok": False,
                "reason": "scope 'remote' cannot be committed over MCP: provider "
                "deletions are irreversible, so no snapshot can undo them. Run "
                "`neurailyzer wipe remote --commit` yourself. (commit=false to "
                "see the plan.)",
            }
        cfg, refusal = _load_or_reason(config_path)
        if cfg is None:
            return refusal or {"ok": False, "reason": "unknown configuration error"}
        scopes = core.expand_scopes(scopes)
        if not commit:
            plans = core.plan_wipe(cfg, scopes)
            return {
                "ok": True,
                "dry_run": True,
                "plans": [_plan_dict(p) for p in plans],
            }
        # An agent calling this mid-session is the case the liveness guard
        # exists for: the harness holding the store open is the very one
        # making the call.
        from . import presets as presets_mod

        scope_roots = tuple(r for s in scopes for r in cfg.roots_for(s))
        by_preset = {
            pid: tuple(
                r
                for r in presets_mod.expand_existing(
                    (*presets_mod.REGISTRY[pid].session, *presets_mod.REGISTRY[pid].sandbox)
                )
                if r in scope_roots
            )
            for pid in cfg.presets
            if pid in presets_mod.REGISTRY
        }
        live = liveness.check_enabled(list(cfg.presets), scope_roots, by_preset)
        if live and not force:
            return {
                "ok": False,
                "reason": "a targeted harness looks like it is RUNNING -- wiping a "
                "live session store can corrupt it rather than reset it. Close it, "
                "or call again with force=true if you are certain.",
                "liveness": [
                    {"preset": entry.preset_id, "reasons": list(entry.reasons)} for entry in live
                ],
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
        cfg, refusal = _load_or_reason(config_path)
        if cfg is None:
            return refusal or {"ok": False, "reason": "unknown configuration error"}
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
        "complete": p.complete,
        "item_ids": list(p.item_ids),
        "notes": list(p.notes),
    }


def serve(config_path: str | None = None, transport: str = "stdio") -> None:
    """Run the MCP server on the chosen transport."""
    server = build_server(config_path)
    if transport not in ("stdio", "streamable-http"):
        raise ValueError(f"unsupported transport {transport!r} (stdio, streamable-http)")
    server.run(transport)
