"""End-to-end smoke: drive the REAL CLI (subprocess) through the whole flow.

Not CliRunner — an actual ``python -m neurailyzer`` process, the same entry a
user hits, on a realistic state tree:

    list-state -> wipe (dry-run) -> wipe --commit -> restore --commit

Asserts the three product guarantees: the right files are wiped (and ONLY
those), the keep-list survives everything, and restore brings state back
bit-identically.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def run_cli(cfg: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed argv, our own package
        [sys.executable, "-m", "neurailyzer", "--config", str(cfg), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"},
    )


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


@pytest.mark.e2e
def test_full_flow(state: dict[str, Path]) -> None:
    cfg = state["config"]
    session, sandbox, keep = state["session"], state["sandbox"], state["keep"]
    golden_session = tree_bytes(session)
    golden_sandbox = tree_bytes(sandbox)
    assert golden_session and golden_sandbox, "fixture must seed real state"

    # 1. list-state sees the targets
    r = run_cli(cfg, "list-state")
    assert r.returncode == 0, r.stderr
    assert "ready" in r.stdout

    # 2. dry-run changes nothing
    r = run_cli(cfg, "wipe", "session", "sandbox")
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout
    assert tree_bytes(session) == golden_session
    assert tree_bytes(sandbox) == golden_sandbox

    # 3. commit wipes — snapshot first, keep-list survives, symlink victim intact
    r = run_cli(cfg, "wipe", "session", "sandbox", "--commit")
    assert r.returncode == 0, r.stderr
    assert "snapshot" in r.stdout and "verified" in r.stdout
    snap_id = next(
        tok for tok in r.stdout.split() if tok.startswith(datetime.now(UTC).strftime("%Y"))
    )
    assert not (session / "chat.db").exists()
    assert not (sandbox / "notes.txt").exists()
    assert not (sandbox / "deep").exists()
    assert (keep / "pinned.txt").read_text() == "NEVER wipe me"  # keep-list held
    if state["has_symlinks"]:
        assert state["victim"].read_text() == "outside the blast radius"  # link not followed
    assert session.is_dir() and sandbox.is_dir()  # workspaces survive

    # 4. state regrows; restore rolls back to the pre-wipe point in time
    (sandbox / "regrown-junk.txt").write_text("post-wipe drift")
    r = run_cli(cfg, "restore", "--to", snap_id)  # dry-run first
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout
    assert (sandbox / "regrown-junk.txt").exists()

    r = run_cli(cfg, "restore", "--to", snap_id, "--commit")
    assert r.returncode == 0, r.stderr + r.stdout
    restored_session = tree_bytes(session)
    restored_sandbox = tree_bytes(sandbox)
    assert restored_session == golden_session, "session not bit-identical after restore"
    assert restored_sandbox == golden_sandbox, "sandbox not bit-identical after restore"
    assert not (sandbox / "regrown-junk.txt").exists()

    # 5. restore by TIME (not id): a future timestamp resolves to the latest point
    (sandbox / "junk2.txt").write_text("more drift")
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    r = run_cli(cfg, "restore", "--to", future, "--commit")
    assert r.returncode == 0, r.stderr + r.stdout
    assert not (sandbox / "junk2.txt").exists()

    # 6. snapshots exist and are listed
    r = run_cli(cfg, "snapshot", "--list")
    assert r.returncode == 0
    assert snap_id in r.stdout


@pytest.mark.e2e
def test_factory_reset_gate_end_to_end(state: dict[str, Path]) -> None:
    cfg = state["config"]
    r = run_cli(cfg, "wipe", "all", "--commit")
    assert r.returncode == 2
    assert (state["session"] / "chat.db").exists()  # refused, untouched
    r = run_cli(cfg, "wipe", "all", "--commit", "--confirm", "all")
    assert r.returncode == 0
    assert not (state["session"] / "chat.db").exists()
    assert (state["keep"] / "pinned.txt").exists()  # keep-list survives even 'all'


@pytest.mark.e2e
def test_console_script_entrypoint() -> None:
    """The installed `neurailyzer` script itself answers (not just -m)."""
    exe = Path(sys.executable).parent / ("neurailyzer.exe" if os.name == "nt" else "neurailyzer")
    if not exe.exists():
        pytest.skip("console script not on this interpreter's path")
    r = subprocess.run(  # noqa: S603
        [str(exe), "--version"], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0
    assert "neurailyzer" in r.stdout
