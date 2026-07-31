"""Core orchestration: snapshot -> wipe -> verify -> report.

Enforces the invariants individual wipers don't: snapshot-before-commit and the
keep-list. The CLI and MCP server are thin surfaces over these functions.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import FILE_SCOPES, PENDING_SCOPES, REMOTE_SCOPE, Config
from .snapshots import Snapshot, SnapshotStore
from .wipers.base import WipePlan, Wiper
from .wipers.local import PathWiper
from .wipers.remote import PROVIDERS, RemoteWiper


class WipeRefused(RuntimeError):
    """The requested wipe violates a safety invariant."""


@dataclass(frozen=True)
class ScopeStatus:
    """What one scope looks like right now (for ``list-state``)."""

    scope: str
    configured: bool
    available: bool  # an adapter exists in this release
    roots: tuple[str, ...]
    file_count: int
    total_bytes: int
    kept_count: int


@dataclass(frozen=True)
class WipeReport:
    """The outcome of an executed (or planned) wipe."""

    snapshot: Snapshot | None
    plans: tuple[WipePlan, ...]
    verified: dict[str, bool]


def expand_scopes(scopes: list[str]) -> list[str]:
    """``all`` means every scope that could have an adapter."""
    if "all" in scopes:
        return [*FILE_SCOPES, REMOTE_SCOPE, *PENDING_SCOPES]
    return list(dict.fromkeys(scopes))  # dedupe, keep order


def build_wipers(config: Config, scopes: list[str]) -> list[Wiper]:
    """Wipers for every requested scope that is configured and available."""
    wipers: list[Wiper] = []
    for scope in scopes:
        roots = config.roots_for(scope)
        if scope in FILE_SCOPES and roots:
            wipers.append(PathWiper(scope, roots, config.keep))
        elif scope == REMOTE_SCOPE:
            for provider_id, surfaces in config.remote.items():
                wipers.append(RemoteWiper(PROVIDERS[provider_id], surfaces))
    return wipers


def scope_status(config: Config, scope: str) -> ScopeStatus:
    if scope == REMOTE_SCOPE:
        # no network from list-state: show configured providers, not live counts
        providers = tuple(
            f"{pid}: {', '.join(surfaces)}" for pid, surfaces in config.remote.items()
        )
        return ScopeStatus(scope, bool(providers), True, providers, 0, 0, 0)
    roots = config.roots_for(scope)
    available = scope in FILE_SCOPES
    if not (available and roots):
        return ScopeStatus(scope, bool(roots), available, tuple(map(str, roots)), 0, 0, 0)
    plan = PathWiper(scope, roots, config.keep).plan()
    return ScopeStatus(
        scope=scope,
        configured=True,
        available=True,
        roots=tuple(map(str, roots)),
        file_count=plan.item_count,
        total_bytes=plan.bytes_total,
        kept_count=len(plan.notes),
    )


def plan_wipe(config: Config, scopes: list[str]) -> list[WipePlan]:
    """Dry-run: what each configured scope would lose. Mutates nothing."""
    return [w.plan() for w in build_wipers(config, scopes)]


def execute_wipe(
    config: Config,
    scopes: list[str],
    *,
    take_snapshot: bool = True,
    label: str = "pre-wipe",
) -> WipeReport:
    """Snapshot (unless waived), then commit each wiper, then verify.

    The snapshot-before-wipe invariant lives HERE, not in the CLI, so every
    surface (CLI, MCP, library) inherits it.
    """
    wipers = build_wipers(config, scopes)
    if not wipers:
        return WipeReport(snapshot=None, plans=(), verified={})

    snapshot: Snapshot | None = None
    if take_snapshot:
        store = SnapshotStore(config.snapshot_dir)
        affected = {
            w.scope: tuple(config.roots_for(w.scope)) for w in wipers if w.scope != REMOTE_SCOPE
        }
        # Remote deletes can't be restored, so the restore point records a
        # MANIFEST of the ids that existed (DESIGN 7) -- the only honest
        # "before" a provider API allows.
        manifests = {f"{w.provider.id}": w.plan() for w in wipers if isinstance(w, RemoteWiper)}
        snapshot = store.take(
            affected,
            label,
            remote_manifest={
                pid: {"description": p.description, "notes": list(p.notes)}
                for pid, p in manifests.items()
            }
            or None,
        )
        store.prune(config.retention)

    plans = tuple(w.commit() for w in wipers)
    verified: dict[str, bool] = {}
    for w in wipers:
        key = f"{w.scope}:{w.provider.id}" if isinstance(w, RemoteWiper) else w.scope
        verified[key] = w.verify()
    return WipeReport(snapshot=snapshot, plans=plans, verified=verified)
