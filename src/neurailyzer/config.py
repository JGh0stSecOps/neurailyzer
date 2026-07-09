"""Configuration: wipe targets, the keep-list, and credential *references*.

Credentials themselves are never stored here — remote wipers read least-privilege,
per-provider tokens from the environment / OS keyring at call time (see DESIGN §8).
Targets are populated as wipers land.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class KeepList:
    """State that is NEVER wiped, no matter the scope."""

    paths: tuple[str, ...] = ()
    collections: tuple[str, ...] = ()
    memory_keys: tuple[str, ...] = ()

    def protects_path(self, path: str) -> bool:
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in self.paths)


@dataclass(frozen=True)
class Config:
    """NeurAIlyzer configuration."""

    keep_list: KeepList = field(default_factory=KeepList)
    snapshot_dir: str = "~/.neurailyzer/snapshots"
    dry_run_default: bool = True
    # Adapter target references: chat-store path, vector-store URL,
    # sandbox handle, runtime endpoint (populated per configured adapter)
    # Remote provider capability flags.


def load(path: str | None = None) -> Config:
    """Load configuration from *path* (or discover env/defaults).

    Returns safe defaults for now. File/env parsing lands with the wipers so that
    every target is introduced alongside the code that resets it.
    """

    return Config()
