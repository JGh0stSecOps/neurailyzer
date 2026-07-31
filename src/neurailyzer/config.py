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

import contextlib
import fnmatch
import glob as globmod
import os
import sys
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

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


#: Directories nobody means to hand to a wiper. A harness path that RESOLVES
#: into one of these is almost always an accident -- `~/.claude/downloads ->
#: ~/Downloads` is a natural thing to set up and a catastrophic thing to wipe.
_PRECIOUS_HOME_DIRS: tuple[str, ...] = (
    "Downloads",
    "Documents",
    "Desktop",
    "Pictures",
    "Music",
    "Movies",
    "Videos",
    "Public",
)


def _same(a: Path, b: Path) -> bool:
    """Path equality that respects the filesystem's case rules."""
    return _norm(a) == _norm(b)


def _within(path: Path, ancestor: Path) -> bool:
    return _same(path, ancestor) or any(_same(p, ancestor) for p in path.parents)


def _forbidden_target_reason(path: Path) -> str | None:
    """A target this broad is a config mistake, not a wipe request.

    Comparisons are case-insensitive where the filesystem is: on macOS
    ``~/downloads`` and ``~/Downloads`` are the same directory, so a
    byte-exact check would wave the dangerous spelling straight through.
    """
    home = Path.home().resolve()
    if _same(path, Path(path.anchor)):
        return "is a filesystem root"
    if _same(path, home):
        return "is your home directory"
    if any(_same(home, p) or _same(p, home) for p in [home, *home.parents]) and _within(home, path):
        return "contains your home directory"
    for name in _PRECIOUS_HOME_DIRS:
        candidate = home / name
        if _within(path, candidate):
            return (
                f"resolves inside {candidate} -- if a harness path links there, "
                "wipe the harness's own directory instead"
            )
    return None


#: Windows and (by default) macOS match filenames case-insensitively. NOTE:
#: os.path.normcase is a NO-OP on POSIX -- including macOS -- so relying on it
#: alone would leave macOS keep-lists case-sensitive and silently voidable.
_FOLD_CASE = sys.platform in ("win32", "darwin")


def _norm(path: Path | str) -> str:
    """Comparison key: case-folded where the filesystem is, NFC-normalized.

    `Path.resolve()` does NOT canonicalize case, so a keep entry spelled
    `~/projects` must still protect on-disk `~/Projects`. macOS also hands
    back NFD from the filesystem while a config file usually carries NFC.
    """
    text = unicodedata.normalize("NFC", str(path))
    text = os.path.normcase(text)  # also flips \\ to / on Windows
    return text.casefold() if _FOLD_CASE else text


def _spellings(path: Path) -> tuple[str, ...]:
    """Every way this path can legitimately be written: as given, and resolved.

    A keep entry may be a symlink whose destination is what the walker sees,
    or the reverse -- comparing only one spelling silently voids protection.
    """
    keys = {_norm(path)}
    with contextlib.suppress(OSError, RuntimeError):  # broken link / resolve loop
        keys.add(_norm(path.resolve()))
    return tuple(keys)


def _resolve_pattern(pattern: str) -> tuple[str, ...]:
    """A glob pattern, plus the same pattern with its literal prefix resolved.

    Targets are stored resolved, so an unresolved pattern can never match
    when the harness dir is a symlink (stow/chezmoi put ``~/.claude`` in a
    dotfiles repo). Only the non-glob prefix is resolvable.
    """
    out = {pattern}
    parts = PurePosixPath(pattern).parts
    literal = []
    for part in parts:
        if any(c in part for c in "*?["):
            break
        literal.append(part)
    if literal and len(literal) < len(parts):
        prefix = Path(*literal)
        try:
            resolved = prefix.resolve()
        except (OSError, RuntimeError):
            return tuple(out)
        rest = parts[len(literal) :]
        out.add((resolved.joinpath(*rest)).as_posix())
    return tuple(out)


def _pattern_hits(path: Path, pattern: str) -> bool:
    """True if *path* or any ancestor matches the (absolute) glob *pattern*."""
    pat = _norm(pattern)
    for candidate in (path, *path.parents):
        for spelling in _spellings(candidate):
            if fnmatch.fnmatch(spelling, pat):
                return True
    return False


@dataclass(frozen=True)
class KeepList:
    """State that is NEVER wiped, no matter the scope.

    Matching is deliberately generous: a path is protected if EITHER its
    literal spelling or its resolved spelling matches a keep entry in either
    of ITS spellings, compared case-insensitively where the filesystem is.
    Every near-miss here is silent data loss, so the bias is toward keeping.
    """

    paths: tuple[Path, ...] = ()
    #: absolute glob patterns (POSIX separators), e.g. ``~/.claude/projects/*/memory``
    #: after expansion. Patterns protect matches created at ANY time, not just
    #: ones that existed when the config was loaded.
    patterns: tuple[str, ...] = ()
    collections: tuple[str, ...] = ()
    memory_keys: tuple[str, ...] = ()

    def _keys(self) -> set[str]:
        return {k for p in self.paths for k in _spellings(p)}

    def protects(self, path: Path) -> bool:
        """True if *path* is a keep entry, lives under one, or matches a pattern."""
        keys = self._keys()
        for candidate in (path, *path.parents):
            if keys.intersection(_spellings(candidate)):
                return True
        return any(_pattern_hits(path, pat) for pat in self.patterns)

    def shelters(self, path: Path) -> bool:
        """True if *path* contains a keep entry (so it can't be removed)."""
        here = set(_spellings(path))
        for kept in self.paths:
            for spelling in _spellings(kept):
                if spelling in here:
                    return True
                for parent in Path(spelling).parents:
                    if _norm(parent) in here:
                        return True
        return False


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
    #: (as written, where it actually resolves) for targets that are symlinks
    #: -- surfaced in every plan so a redirected wipe is never a surprise.
    symlinked_targets: tuple[tuple[str, str], ...] = ()
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
            patterns.extend(_resolve_pattern(expanded.as_posix()))
            for match in globmod.glob(str(expanded), recursive=True):
                paths.append(Path(match))  # both spellings kept by _spellings()
        else:
            # store the path AS WRITTEN: _spellings() supplies the resolved
            # form too, so a symlinked keep entry still matches the link
            # itself, which is what the walker actually sees.
            paths.append(expanded)
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
    symlinked_targets: list[tuple[str, str]] = []
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
            if rp != p:
                # following a link is usually right (stow/chezmoi dotfiles),
                # but the user must SEE where the wipe actually lands
                symlinked_targets.append((str(p), str(rp)))
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
        symlinked_targets=tuple(symlinked_targets),
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
