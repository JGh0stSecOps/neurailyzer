"""Configuration: wipe targets, the keep-list, and snapshot settings.

Credentials are never stored here — remote wipers (when they land) read
least-privilege, per-provider tokens from the environment / OS keyring at call
time (see DESIGN §8).

Discovery order for the config file:

1. an explicit ``--config`` path,
2. the ``NEURAILYZER_CONFIG`` environment variable,
3. ``~/.neurailyzer/config.toml``.

A missing file is not an error — it yields an empty config (no targets), so
every command stays a safe no-op until the user opts state in.
"""

from __future__ import annotations

import fnmatch
import glob as globmod
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

#: scopes that have a working adapter in this release (file-tree state).
FILE_SCOPES: tuple[str, ...] = ("session", "sandbox")
#: provider-side stored state, configured via [remote.<provider>].
REMOTE_SCOPE = "remote"
#: scopes reserved by the design but without a shipped adapter yet.
PENDING_SCOPES: tuple[str, ...] = ("rag", "models")

DEFAULT_CONFIG_PATH = Path("~/.neurailyzer/config.toml")
DEFAULT_SNAPSHOT_DIR = Path("~/.neurailyzer/snapshots")
DEFAULT_RETENTION = 20

ENV_CONFIG = "NEURAILYZER_CONFIG"


class ConfigError(ValueError):
    """The config file exists but can't be used. The message says why."""


def _expand(raw: str) -> Path:
    """Expand ``~`` and environment variables; resolve to an absolute path."""
    return Path(os.path.expandvars(raw)).expanduser().resolve()


def _forbidden_target_reason(path: Path) -> str | None:
    """A target this broad is a config mistake, not a wipe request."""
    home = Path.home().resolve()
    if path == Path(path.anchor):
        return "is a filesystem root"
    if path == home:
        return "is your home directory"
    if path in home.parents:
        return "contains your home directory"
    return None


def _pattern_hits(path: Path, pattern: str) -> bool:
    """True if *path* or any ancestor matches the (absolute) glob *pattern*."""
    for candidate in (path, *path.parents):
        if fnmatch.fnmatchcase(candidate.as_posix(), pattern):
            return True
    return False


@dataclass(frozen=True)
class KeepList:
    """State that is NEVER wiped, no matter the scope."""

    paths: tuple[Path, ...] = ()
    #: absolute glob patterns (POSIX separators), e.g. ``~/.claude/projects/*/memory``
    #: after expansion. Patterns protect matches created at ANY time, not just
    #: ones that existed when the config was loaded.
    patterns: tuple[str, ...] = ()
    collections: tuple[str, ...] = ()
    memory_keys: tuple[str, ...] = ()

    def protects(self, path: Path) -> bool:
        """True if *path* is a keep-list entry, lives under one, or matches a pattern."""
        if any(path == p or p in path.parents for p in self.paths):
            return True
        return any(_pattern_hits(path, pat) for pat in self.patterns)

    def shelters(self, path: Path) -> bool:
        """True if *path* contains a keep-list entry (so it can't be removed)."""
        return any(path == p or path in p.parents for p in self.paths)


@dataclass(frozen=True)
class Config:
    """NeurAIlyzer configuration (see README Configuration)."""

    keep: KeepList = field(default_factory=KeepList)
    #: scope -> absolute target roots. Only FILE_SCOPES appear here in v0.1.
    targets: Mapping[str, tuple[Path, ...]] = field(default_factory=dict)
    #: provider id -> enabled remote surfaces (see wipers/remote.py).
    remote: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: ids of the harness presets in play (drives the liveness guard).
    presets: tuple[str, ...] = ()
    snapshot_dir: Path = field(default_factory=lambda: DEFAULT_SNAPSHOT_DIR.expanduser().resolve())
    retention: int = DEFAULT_RETENTION
    #: where this config was loaded from (None = defaults, no file found).
    source: Path | None = None

    def roots_for(self, scope: str) -> tuple[Path, ...]:
        return tuple(self.targets.get(scope, ()))


def _parse_paths(section: str, value: object) -> tuple[Path, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"[{section}] paths must be a list of strings")
    return tuple(_expand(v) for v in value)


def _is_glob(raw: str) -> bool:
    return any(c in raw for c in "*?[")


def _split_keep(section: str, value: object) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """Keep entries may be literal paths or glob patterns; sort them apart.

    Patterns are kept as expanded POSIX strings AND glob-resolved to real
    paths (existing matches also participate in shelter checks).
    """
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"[{section}] paths must be a list of strings")
    paths: list[Path] = []
    patterns: list[str] = []
    for raw in value:
        expanded = Path(os.path.expandvars(raw)).expanduser()
        if _is_glob(raw):
            patterns.append(expanded.as_posix())
            paths.extend(Path(m).resolve() for m in globmod.glob(str(expanded), recursive=True))
        else:
            paths.append(expanded.resolve())
    return tuple(paths), tuple(patterns)


def _parse(data: Mapping[str, object], source: Path) -> Config:
    keep_raw = data.get("keep", {})
    if not isinstance(keep_raw, Mapping):
        raise ConfigError("[keep] must be a table")
    keep_paths, keep_patterns = _split_keep("keep", keep_raw.get("paths", []))

    snap_raw = data.get("snapshots", {})
    if not isinstance(snap_raw, Mapping):
        raise ConfigError("[snapshots] must be a table")
    snap_dir_raw = snap_raw.get("dir", str(DEFAULT_SNAPSHOT_DIR))
    if not isinstance(snap_dir_raw, str):
        raise ConfigError("[snapshots] dir must be a string")
    snapshot_dir = _expand(snap_dir_raw)
    retention = snap_raw.get("retention", DEFAULT_RETENTION)
    if not isinstance(retention, int) or isinstance(retention, bool) or retention < 1:
        raise ConfigError("[snapshots] retention must be a positive integer")

    # -- presets: known-harness targets + keeps, defined in presets.py --------
    presets_raw = data.get("presets", {})
    if not isinstance(presets_raw, Mapping):
        raise ConfigError("[presets] must be a table")
    enabled = presets_raw.get("enabled", [])
    if not isinstance(enabled, list) or not all(isinstance(e, str) for e in enabled):
        raise ConfigError("[presets] enabled must be a list of strings")
    preset_targets: dict[str, list[Path]] = {"session": [], "sandbox": []}
    preset_keep_paths: list[Path] = []
    preset_keep_patterns: list[str] = []
    if enabled:
        from . import presets as presets_mod

        for pid in enabled:
            preset = presets_mod.REGISTRY.get(pid)
            if preset is None:
                raise ConfigError(
                    f"[presets] unknown preset {pid!r} "
                    f"(known: {', '.join(sorted(presets_mod.REGISTRY))})"
                )
            preset_targets["session"].extend(presets_mod.expand_existing(preset.session))
            preset_targets["sandbox"].extend(presets_mod.expand_existing(preset.sandbox))
            kp, kpat = _split_keep(f"presets.{pid}", list(preset.keep))
            preset_keep_paths.extend(kp)
            preset_keep_patterns.extend(kpat)

    # -- remote providers -----------------------------------------------------
    remote_raw = data.get("remote", {})
    if not isinstance(remote_raw, Mapping):
        raise ConfigError("[remote] must be a table of provider tables")
    remote: dict[str, tuple[str, ...]] = {}
    if remote_raw:
        from .wipers.remote import PROVIDERS

        for provider_id, body in remote_raw.items():
            provider = PROVIDERS.get(provider_id)
            if provider is None:
                raise ConfigError(
                    f"[remote.{provider_id}] is not a known provider "
                    f"(known: {', '.join(sorted(PROVIDERS))})"
                )
            if not isinstance(body, Mapping):
                raise ConfigError(f"[remote.{provider_id}] must be a table")
            surfaces = body.get("surfaces", [])
            if not isinstance(surfaces, list) or not all(isinstance(s, str) for s in surfaces):
                raise ConfigError(f"[remote.{provider_id}] surfaces must be a list")
            for s in surfaces:
                if s not in provider.surfaces:
                    known = ", ".join(sorted(provider.surfaces)) or (
                        "none -- this provider stores no wipeable server-side state"
                    )
                    raise ConfigError(
                        f"[remote.{provider_id}] unknown surface {s!r} (known: {known})"
                    )
            if not surfaces:
                raise ConfigError(
                    f"[remote.{provider_id}] lists no surfaces -- each wipeable "
                    "surface is an explicit opt-in"
                )
            remote[provider_id] = tuple(surfaces)

    targets_raw = data.get("targets", {})
    if not isinstance(targets_raw, Mapping):
        raise ConfigError("[targets] must be a table")
    targets: dict[str, tuple[Path, ...]] = {}
    for scope, body in targets_raw.items():
        if scope == REMOTE_SCOPE:
            raise ConfigError(
                "[targets.remote] is not a path scope -- configure providers "
                "via [remote.<provider>] instead"
            )
        if scope in PENDING_SCOPES:
            raise ConfigError(
                f"[targets.{scope}] has no adapter in this release -- "
                f"supported scopes today: {', '.join(FILE_SCOPES)}"
            )
        if scope not in FILE_SCOPES:
            raise ConfigError(
                f"[targets.{scope}] is not a known scope "
                f"(known: {', '.join((*FILE_SCOPES, *PENDING_SCOPES))})"
            )
        if not isinstance(body, Mapping):
            raise ConfigError(f"[targets.{scope}] must be a table")
        paths = _parse_paths(f"targets.{scope}", body.get("paths", []))
        for p in paths:
            reason = _forbidden_target_reason(p)
            if reason is not None:
                raise ConfigError(f"[targets.{scope}] refusing target {p}: {reason}")
            if p == snapshot_dir or p in snapshot_dir.parents:
                raise ConfigError(
                    f"[targets.{scope}] {p} contains the snapshot store {snapshot_dir} -- "
                    "snapshots must live outside every wipe target"
                )
        targets[scope] = paths

    # merge preset targets after explicit ones, with the same guards
    for scope, extra in preset_targets.items():
        if not extra:
            continue
        merged = list(targets.get(scope, ()))
        for p in extra:
            rp = p.resolve()
            reason = _forbidden_target_reason(rp)
            if reason is not None:
                raise ConfigError(f"[presets] refusing target {rp}: {reason}")
            if rp == snapshot_dir or rp in snapshot_dir.parents:
                raise ConfigError(
                    f"[presets] {rp} contains the snapshot store {snapshot_dir} -- "
                    "move the snapshot store outside every wipe target"
                )
            if rp not in merged:
                merged.append(rp)
        targets[scope] = tuple(merged)

    # The snapshot store protects itself: it is always on the keep-list.
    keep = KeepList(
        paths=(*keep_paths, *preset_keep_paths, snapshot_dir),
        patterns=(*keep_patterns, *preset_keep_patterns),
    )
    return Config(
        keep=keep,
        targets=targets,
        remote=remote,
        presets=tuple(enabled),
        snapshot_dir=snapshot_dir,
        retention=retention,
        source=source,
    )


def load(path: str | Path | None = None) -> Config:
    """Load configuration; a missing default file yields safe empty defaults."""
    explicit = path is not None or ENV_CONFIG in os.environ
    cfg_path = (
        Path(path)
        if path is not None
        else Path(os.environ.get(ENV_CONFIG, str(DEFAULT_CONFIG_PATH)))
    )
    cfg_path = cfg_path.expanduser()
    if not cfg_path.exists():
        if explicit:
            raise ConfigError(f"config file not found: {cfg_path}")
        return Config(keep=KeepList(paths=(DEFAULT_SNAPSHOT_DIR.expanduser().resolve(),)))
    try:
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{cfg_path}: invalid TOML: {exc}") from exc
    return _parse(data, cfg_path.resolve())
