"""Is the harness still running?

Wiping a session store out from under a live writer is the one way this tool
can *corrupt* rather than reset: several harnesses keep history in WAL-mode
SQLite (Hermes ``state.db``, Codex's ``*.sqlite``), where deleting the ``-wal``
sidecar mid-write leaves a torn database rather than a clean slate.

Detection is two-tier. Where a harness publishes its own pid (Claude Code's
``sessions/<pid>.json``, Grok Build's ``leader.lock``) we read it and check
that the process is actually alive -- exact, and immune to stale files from a
crashed run. Everywhere else we fall back to matching command lines, which is
fuzzy. So this is an explicit **best-effort advisory**, and it is honest
about that:

- a positive result ("looks like it's running") is a strong signal,
- a negative result is NOT proof that nothing is running.

``wipe --commit`` warns on a positive; ``--force`` proceeds anyway. We never
silently skip the check, and we never claim certainty we don't have.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Harnesses that publish a pid: a glob of files whose name (or JSON body)
#: identifies a live process. This is the STRONG signal -- a live pid means
#: the harness is running, no guessing. Verified against the upstream
#: layouts (Claude Code's sessions/<pid>.json, Grok Build's leader.lock).
PID_SOURCES: dict[str, tuple[str, ...]] = {
    "claude-code": (
        "$CLAUDE_CONFIG_DIR/sessions/*.json",
        "~/.claude/sessions/*.json",
        "~/.claude/ide/*.lock",
    ),
    "grok-build": (
        "$GROK_HOME/leader*.lock",
        "~/.grok/leader*.lock",
    ),
    "opencode": (
        "$XDG_STATE_HOME/opencode/server.json",
        "~/.local/state/opencode/server.json",
        "$XDG_STATE_HOME/opencode/locks/*.lock/meta.json",
        "~/.local/state/opencode/locks/*.lock/meta.json",
    ),
}

#: process-name fragments per preset id -- the WEAK fallback, used only where
#: no pid file exists. Matched against the full command line.
PROCESS_HINTS: dict[str, tuple[str, ...]] = {
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


def _pid_is_alive(pid: int) -> bool:
    """Does a process with this pid exist? (Not whether we may signal it.)"""
    if pid <= 0:
        return False
    if os.name == "nt":
        exe = shutil.which("tasklist")
        if not exe:
            return False
        try:
            out = subprocess.run(  # noqa: S603 -- absolute path, fixed argv
                [exe, "/fi", f"PID eq {pid}", "/nh"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _pids_from(templates: tuple[str, ...]) -> list[tuple[int, str]]:
    """(pid, source) pairs from a harness's pid files -- live ones only.

    The pid may be the filename stem (Claude Code's ``sessions/<pid>.json``)
    or a ``pid`` field in a JSON body (its ``ide/<port>.lock``). Stale files
    from a crashed run are common, so every pid is checked for liveness --
    a leftover file must not block a wipe forever.
    """
    out: list[tuple[int, str]] = []
    for template in templates:
        expanded = Path(os.path.expandvars(template)).expanduser()
        if "$" in str(expanded):  # unset env var -- skip, don't glob a literal '$'
            continue
        for path in sorted(_glob(expanded)):
            pids: set[int] = set()
            if path.stem.isdigit():
                pids.add(int(path.stem))
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(body, dict) and isinstance(body.get("pid"), int):
                    pids.add(body["pid"])
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                pass  # a lock file need not be JSON
            for pid in pids:
                if pid != os.getpid() and _pid_is_alive(pid):
                    out.append((pid, str(path)))
    return out


def _glob(pattern: Path) -> list[Path]:
    import glob as globmod

    return [Path(m) for m in globmod.glob(str(pattern))]


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
    """Best-effort: does *preset_id* look like it is currently running?

    Prefers the harness's own pid files where it publishes them (exact), and
    falls back to command-line matching (fuzzy) only where it doesn't.
    """
    reasons: list[str] = []
    pid_hits = _pids_from(PID_SOURCES.get(preset_id, ()))
    for pid, source in pid_hits:
        reasons.append(f"live process {pid} recorded in {source}")
    if not pid_hits and preset_id not in PID_SOURCES:
        for hit in _running_process_hits(PROCESS_HINTS.get(preset_id, ())):
            reasons.append(f"a process matching {hit!r} is running")
    for sidecar in _open_wal_sidecars(roots):
        reasons.append(f"SQLite write-ahead file present: {sidecar}")
    return Liveness(preset_id=preset_id, likely_running=bool(reasons), reasons=tuple(reasons))


def check_enabled(preset_ids: list[str], roots: tuple[Path, ...] = ()) -> list[Liveness]:
    """Check every enabled preset; only positives come back."""
    out = [check(pid, roots) for pid in preset_ids]
    return [live for live in out if live.likely_running]
