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

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

#: scopes that have a working adapter in this release (file-tree state).
FILE_SCOPES: tuple[str, ...] = ("session", "sandbox")
#: scopes reserved by the design but without a shipped adapter yet.
PENDING_SCOPES: tuple[str, ...] = ("rag", "models", "remote")

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


@dataclass(frozen=True)
class KeepList:
    """State that is NEVER wiped, no matter the scope."""

    paths: tuple[Path, ...] = ()
    collections: tuple[str, ...] = ()
    memory_keys: tuple[str, ...] = ()

    def protects(self, path: Path) -> bool:
        """True if *path* is a keep-list entry or lives under one."""
        return any(path == p or p in path.parents for p in self.paths)

    def shelters(self, path: Path) -> bool:
        """True if *path* contains a keep-list entry (so it can't be removed)."""
        return any(path == p or path in p.parents for p in self.paths)


@dataclass(frozen=True)
class Config:
    """NeurAIlyzer configuration (see README Configuration)."""

    keep: KeepList = field(default_factory=KeepList)
    #: scope -> absolute target roots. Only FILE_SCOPES appear here in v0.1.
    targets: Mapping[str, tuple[Path, ...]] = field(default_factory=dict)
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


def _parse(data: Mapping[str, object], source: Path) -> Config:
    keep_raw = data.get("keep", {})
    if not isinstance(keep_raw, Mapping):
        raise ConfigError("[keep] must be a table")
    keep_paths = _parse_paths("keep", keep_raw.get("paths", []))

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

    targets_raw = data.get("targets", {})
    if not isinstance(targets_raw, Mapping):
        raise ConfigError("[targets] must be a table")
    targets: dict[str, tuple[Path, ...]] = {}
    for scope, body in targets_raw.items():
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

    # The snapshot store protects itself: it is always on the keep-list.
    keep = KeepList(paths=(*keep_paths, snapshot_dir))
    return Config(
        keep=keep,
        targets=targets,
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
