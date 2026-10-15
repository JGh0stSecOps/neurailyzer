"""Shared fixtures: a realistic fake agent-state tree + a config pointing at it."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from neurailyzer.config import load
from neurailyzer.snapshots import Snapshot, SnapshotStore


@pytest.fixture
def state(tmp_path: Path) -> dict[str, Any]:
    """A fake agent's on-disk state: session history, sandbox scratch, keep-list.

    Includes the traps the wiper must survive: a read-only file (the Windows
    attribute), a symlink pointing OUTSIDE the target at a victim file, an
    empty directory, and a keep-list entry nested inside a wipe target.
    """
    session = tmp_path / "agent" / "sessions"
    sandbox = tmp_path / "agent" / "scratch"
    keep = sandbox / "keep-me"
    victim = tmp_path / "outside" / "precious.txt"

    (session / "threads").mkdir(parents=True)
    (session / "threads" / "t1.jsonl").write_text('{"role":"user","content":"hi"}\n')
    (session / "chat.db").write_bytes(b"SQLite format 3\x00" + os.urandom(64))

    sandbox.mkdir(parents=True)
    (sandbox / "notes.txt").write_text("scratch note")
    (sandbox / "deep" / "nested").mkdir(parents=True)
    (sandbox / "deep" / "nested" / "artifact.bin").write_bytes(os.urandom(128))
    (sandbox / "deep" / "empty").mkdir()
    ro = sandbox / "readonly.lock"
    ro.write_text("locked")
    ro.chmod(0o444)

    keep.mkdir()
    (keep / "pinned.txt").write_text("NEVER wipe me")

    victim.parent.mkdir(parents=True)
    victim.write_text("outside the blast radius")
    try:
        (sandbox / "sneaky-link").symlink_to(victim)
        has_symlinks = True
    except OSError:  # e.g. Windows without the symlink privilege
        has_symlinks = False

    snapdir = tmp_path / "snapstore"
    cfg = tmp_path / "config.toml"
    # forward slashes on every platform: TOML needs no escaping, pathlib accepts them
    cfg.write_text(
        f"""
[keep]
paths = ["{keep.as_posix()}"]

[targets.session]
paths = ["{session.as_posix()}"]

[targets.sandbox]
paths = ["{sandbox.as_posix()}"]

[snapshots]
dir = "{snapdir.as_posix()}"
retention = 5
"""
    )
    return {
        "root": tmp_path,
        "session": session,
        "sandbox": sandbox,
        "keep": keep,
        "victim": victim,
        "config": cfg,
        "snapdir": snapdir,
        "has_symlinks": has_symlinks,
    }


@pytest.fixture(params=["missing", "corrupt", "unreadable"])
def damaged_restore(
    state: dict[str, Any], request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> tuple[SnapshotStore, Snapshot, str]:
    """A damaged restore point alongside newer state that must survive refusal."""
    cfg = load(state["config"])
    store = SnapshotStore(cfg.snapshot_dir)
    snap = store.take({s: cfg.roots_for(s) for s in ("session", "sandbox")}, "golden")
    record = next(f for f in snap.files if f.relpath == "notes.txt")
    blob = store.blob_dir / record.sha256[:2] / record.sha256
    (state["sandbox"] / "notes.txt").write_text("new notes")
    (state["sandbox"] / "deep" / "nested" / "artifact.bin").write_bytes(b"new artifact")
    (state["sandbox"] / "new-dir").mkdir()
    (state["sandbox"] / "new-dir" / "new.txt").write_text("new file")
    (state["sandbox"] / "new-empty-dir").mkdir()
    if request.param == "missing":
        blob.unlink()
        error = "missing blob"
    elif request.param == "corrupt":
        blob.write_bytes(b"damaged snapshot content")
        error = "corrupt blob"
    else:
        original_open = Path.open

        def deny_blob(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == blob:
                raise PermissionError("snapshot blob is unreadable")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", deny_blob)
        error = "could not read blob"
    return store, snap, error
