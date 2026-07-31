"""Is the harness still running?

Wiping a session store out from under a live writer is the one way this tool
can *corrupt* rather than reset: several harnesses keep history in WAL-mode
SQLite (Hermes ``state.db``, Codex's ``*.sqlite``), where deleting the ``-wal``
sidecar mid-write leaves a torn database rather than a clean slate.

There is no portable, reliable "is process X running" API, and none of these
harnesses publish a lock-file contract we can depend on. So this is an
explicit **best-effort advisory**, and it is honest about that:

- a positive result ("looks like it's running") is a strong signal,
- a negative result is NOT proof that nothing is running.

``wipe --commit`` warns on a positive; ``--force`` proceeds anyway. We never
silently skip the check, and we never claim certainty we don't have.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: process-name fragments per preset id. Matched against the full command
#: line, so a path like /usr/local/bin/claude counts.
PROCESS_HINTS: dict[str, tuple[str, ...]] = {
    "claude-code": ("claude",),
    "codex": ("codex",),
    "hermes": ("hermes",),
    "scion": ("scion",),
    "venice-web": ("Google Chrome", "chrome", "firefox", "Brave Browser", "msedge"),
}

#: WAL sidecars: if these exist, a writer either is live or died uncleanly.
_WAL_SUFFIXES = ("-wal", "-shm")


@dataclass(frozen=True)
class Liveness:
    """What we could tell about a harness still holding its state open."""

    preset_id: str
    likely_running: bool
    reasons: tuple[str, ...] = ()

    @property
    def advice(self) -> str:
        return (
            f"{self.preset_id} looks like it is RUNNING -- close it before "
            "wiping its session store, or pass --force"
        )


def _running_process_hits(fragments: tuple[str, ...]) -> list[str]:
    """Best-effort process scan. Empty list on any platform we can't ask."""
    hits: list[str] = []
    if os.name == "nt":
        exe = shutil.which("tasklist")
        cmd = [exe, "/fo", "csv", "/nh"] if exe else None
    else:
        exe = shutil.which("ps")
        cmd = [exe, "-eo", "command"] if exe else None
    if not cmd:
        return hits
    try:
        out = subprocess.run(  # noqa: S603 -- absolute path from shutil.which, fixed argv
            cmd, capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return hits
    own_pid = str(os.getpid())
    for line in out.splitlines():
        if own_pid in line and "neurailyzer" in line:
            continue  # never report ourselves
        for frag in fragments:
            if frag.lower() in line.lower() and "neurailyzer" not in line.lower():
                hits.append(frag)
                break
    return sorted(set(hits))


def _open_wal_sidecars(roots: tuple[Path, ...]) -> list[str]:
    """SQLite -wal/-shm files under the targets (live writer or unclean exit)."""
    found: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else list(root.rglob("*"))
        found.extend(str(p) for p in candidates if p.is_file() and p.name.endswith(_WAL_SUFFIXES))
    return sorted(found)[:10]


def check(preset_id: str, roots: tuple[Path, ...] = ()) -> Liveness:
    """Best-effort: does *preset_id* look like it is currently running?"""
    reasons: list[str] = []
    for hit in _running_process_hits(PROCESS_HINTS.get(preset_id, ())):
        reasons.append(f"a process matching {hit!r} is running")
    for sidecar in _open_wal_sidecars(roots):
        reasons.append(f"SQLite write-ahead file present: {sidecar}")
    return Liveness(preset_id=preset_id, likely_running=bool(reasons), reasons=tuple(reasons))


def check_enabled(preset_ids: list[str], roots: tuple[Path, ...] = ()) -> list[Liveness]:
    """Check every enabled preset; only positives come back."""
    out = [check(pid, roots) for pid in preset_ids]
    return [live for live in out if live.likely_running]
