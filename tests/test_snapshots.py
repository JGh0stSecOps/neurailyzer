"""Snapshot store: take, resolve --to, restore rollback, retention."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from neurailyzer.config import KeepList, load
from neurailyzer.snapshots import SnapshotError, SnapshotStore, parse_point_in_time


def _store_and_targets(state: dict[str, Path]) -> tuple[SnapshotStore, dict[str, tuple[Path, ...]]]:
    cfg = load(state["config"])
    targets = {s: cfg.roots_for(s) for s in ("session", "sandbox")}
    return SnapshotStore(cfg.snapshot_dir), targets


def test_take_records_everything(state: dict[str, Path]) -> None:
    store, targets = _store_and_targets(state)
    snap = store.take(targets, "pre-task")
    names = {f.relpath for f in snap.files}
    assert "threads/t1.jsonl" in names
    assert "chat.db" in names
    assert "deep/nested/artifact.bin" in names
    assert any(d[1] == "deep/empty" for d in snap.dirs)  # empty dir recorded
    if state["has_symlinks"]:
        assert any(ln.relpath == "sneaky-link" for ln in snap.links)  # link, not its target
    assert snap.total_bytes > 0
    # blobs are content-addressed and present
    for f in snap.files:
        assert (store.blob_dir / f.sha256[:2] / f.sha256).is_file()


def test_snapshot_id_is_windows_safe(state: dict[str, Path]) -> None:
    store, targets = _store_and_targets(state)
    snap = store.take(targets, "label with spaces & symbols!")
    assert not any(c in snap.id for c in ':<>"|?*')


def test_resolve_by_id_and_by_time(state: dict[str, Path]) -> None:
    store, targets = _store_and_targets(state)
    snap = store.take(targets, "first")
    assert store.resolve(snap.id) is not None
    future = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    got = store.resolve(future)
    assert got is not None and got.id == snap.id
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assert store.resolve(past) is None


def test_resolve_picks_nearest_at_or_before(state: dict[str, Path]) -> None:
    store, targets = _store_and_targets(state)
    s1 = store.take(targets, "one")
    time.sleep(1.1)  # ids have 1s resolution
    cut = datetime.now(UTC)
    time.sleep(0.2)
    (state["sandbox"] / "later.txt").write_text("after the cut")
    s2 = store.take(targets, "two")
    got = store.resolve(cut.isoformat())
    assert got is not None and got.id == s1.id
    got2 = store.resolve((datetime.now(UTC) + timedelta(seconds=5)).isoformat())
    assert got2 is not None and got2.id == s2.id


def test_bad_point_in_time_errors() -> None:
    with pytest.raises(SnapshotError, match="ISO-8601"):
        parse_point_in_time("not-a-time")


def test_naive_time_is_local(state: dict[str, Path]) -> None:
    aware = parse_point_in_time("2026-07-09T04:00")
    assert aware.tzinfo is UTC


def test_restore_round_trip_bit_identical(state: dict[str, Path]) -> None:
    store, targets = _store_and_targets(state)
    originals = {
        p: p.read_bytes() for p in state["sandbox"].rglob("*") if p.is_file() and not p.is_symlink()
    }
    originals.update({p: p.read_bytes() for p in state["session"].rglob("*") if p.is_file()})
    snap = store.take(targets, "golden")

    # wreck the state: delete, modify, add
    (state["sandbox"] / "notes.txt").unlink()
    (state["session"] / "chat.db").write_bytes(b"corrupted")
    (state["sandbox"] / "junk-after.txt").write_text("appeared later")

    plan = store.restore(snap, KeepList())
    assert not plan.errors
    for p, body in originals.items():
        assert p.read_bytes() == body, f"{p} not restored bit-identically"
    assert not (state["sandbox"] / "junk-after.txt").exists()  # rollback removes it
    assert (state["sandbox"] / "deep" / "empty").is_dir()  # empty dir came back
    if state["has_symlinks"]:
        link = state["sandbox"] / "sneaky-link"
        assert link.is_symlink() and os.readlink(link) == str(state["victim"])


def test_restore_dry_run_mutates_nothing(state: dict[str, Path]) -> None:
    store, targets = _store_and_targets(state)
    snap = store.take(targets, "golden")
    (state["sandbox"] / "junk.txt").write_text("junk")
    (state["session"] / "chat.db").write_bytes(b"corrupted")
    plan = store.plan_restore(snap, KeepList())
    assert plan.change_count >= 2
    assert (state["sandbox"] / "junk.txt").exists()
    assert (state["session"] / "chat.db").read_bytes() == b"corrupted"


def test_restore_honors_keep_list(state: dict[str, Path]) -> None:
    store, targets = _store_and_targets(state)
    snap = store.take(targets, "golden")
    kept_new = state["keep"] / "written-after-snapshot.txt"
    kept_new.write_text("added after T, but protected")
    cfg = load(state["config"])
    plan = store.restore(snap, cfg.keep)
    assert kept_new.read_text() == "added after T, but protected"
    assert str(kept_new) in plan.skipped_keep


def test_restore_preserves_mode(state: dict[str, Path]) -> None:
    if os.name == "nt":
        pytest.skip("POSIX modes")
    store, targets = _store_and_targets(state)
    script = state["sandbox"] / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    snap = store.take(targets, "with-exec")
    script.unlink()
    store.restore(snap, KeepList())
    assert script.stat().st_mode & 0o777 == 0o755


def test_retention_prunes_manifests_and_blobs(state: dict[str, Path]) -> None:
    store, targets = _store_and_targets(state)
    unique = state["sandbox"] / "unique.bin"
    ids = []
    for i in range(4):
        unique.write_bytes(os.urandom(64) + bytes([i]))
        ids.append(store.take(targets, f"s{i}").id)
    removed = store.prune(2)
    assert set(removed) == set(ids[:2])
    left = {s.id for s in store.list()}
    assert left == set(ids[2:])
    # blobs referenced only by pruned snapshots are gone; live ones remain
    live_hashes = {f.sha256 for s in store.list() for f in s.files}
    on_disk = {b.name for b in store.blob_dir.glob("*/*")}
    assert on_disk == live_hashes


def test_snapshot_store_dedupes_blobs(state: dict[str, Path]) -> None:
    store, targets = _store_and_targets(state)
    store.take(targets, "a")
    n1 = len(list(store.blob_dir.glob("*/*")))
    store.take(targets, "b")  # identical content -> no new blobs
    n2 = len(list(store.blob_dir.glob("*/*")))
    assert n1 == n2
