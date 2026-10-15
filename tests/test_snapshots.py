"""Snapshot store: take, resolve --to, restore rollback, retention."""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from neurailyzer import snapshots as snapshots_mod
from neurailyzer.config import KeepList, load
from neurailyzer.snapshots import Snapshot, SnapshotError, SnapshotStore, parse_point_in_time


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
    assert any(d.relpath == "deep/empty" for d in snap.dirs)  # empty dir recorded
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
        assert link.is_symlink()
        # compare by identity, not string: Windows readlink returns \\?\-prefixed paths
        assert os.path.samefile(link, state["victim"])


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


# -- resilience: the restore path IS the safety net --------------------------


def test_one_corrupt_manifest_does_not_hide_the_others(state: dict[str, Path]) -> None:
    """A single truncated file must not make every restore point unreachable."""
    store, targets = _store_and_targets(state)
    good = store.take(targets, "good")
    (store.manifest_dir / "broken.json").write_text("{ truncated", encoding="utf-8")

    snaps = store.list()
    assert [s.id for s in snaps] == [good.id]
    assert store.corrupt and "broken.json" in store.corrupt[0]
    # and it is still resolvable/restorable
    assert store.resolve(good.id) is not None


def test_corrupt_manifests_are_reported_not_swallowed(state: dict[str, Path]) -> None:
    """Silently losing a snapshot the user believes they have is just as bad
    as raising on it."""
    store, targets = _store_and_targets(state)
    store.take(targets, "good")
    (store.manifest_dir / "bad.json").write_text('{"id": "x"}', encoding="utf-8")
    store.list()
    assert any("bad.json" in c for c in store.corrupt)


def test_dry_run_restore_reports_a_missing_blob(state: dict[str, Path]) -> None:
    """A dry-run that lists a file it cannot actually restore is worse than
    useless -- it is a safety net promising a catch it will drop."""
    store, targets = _store_and_targets(state)
    snap = store.take(targets, "golden")
    (state["sandbox"] / "notes.txt").unlink()
    for blob in store.blob_dir.glob("*/*"):
        blob.unlink()

    plan = store.plan_restore(snap, KeepList())
    assert plan.errors, "dry-run stayed silent about unrestorable files"
    assert any("missing blob" in e for e in plan.errors)
    assert not plan.restored, "planned a restore it cannot perform"


@pytest.mark.parametrize("commit", [False, True])
def test_damaged_restore_preserves_all_current_state(
    state: dict[str, Path], damaged_restore: tuple[SnapshotStore, Snapshot, str], commit: bool
) -> None:
    store, snap, error = damaged_restore
    restore = store.restore if commit else store.plan_restore
    plan = restore(snap, KeepList())
    assert any(error in e for e in plan.errors)
    assert not plan.restored and not plan.removed
    assert (state["sandbox"] / "notes.txt").read_text() == "new notes"
    assert (state["sandbox"] / "deep" / "nested" / "artifact.bin").read_bytes() == b"new artifact"
    assert (state["sandbox"] / "new-dir" / "new.txt").read_text() == "new file"
    assert (state["sandbox"] / "new-empty-dir").is_dir()
    assert not list(store.root.glob(".restore-*"))


@pytest.mark.parametrize("kept", [False, True])
def test_restore_needs_no_blob_for_kept_or_unchanged_files(
    state: dict[str, Path], kept: bool
) -> None:
    store, targets = _store_and_targets(state)
    snap = store.take(targets, "golden")
    rec = next(f for f in snap.files if f.relpath == "notes.txt")
    (store.blob_dir / rec.sha256[:2] / rec.sha256).unlink()
    note = state["sandbox"] / "notes.txt"
    if kept:
        note.write_text("kept newer content")
    keep = KeepList(paths=(note,)) if kept else KeepList()
    extra = state["sandbox"] / "new.txt"
    extra.write_text("remove me")

    plan = store.restore(snap, keep)
    assert not plan.errors
    assert note.read_text() == ("kept newer content" if kept else "scratch note")
    assert not extra.exists()


def test_restore_uses_verified_bytes_if_store_changes_during_cleanup(
    state: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, targets = _store_and_targets(state)
    snap = store.take(targets, "golden")
    rec = next(f for f in snap.files if f.relpath == "notes.txt")
    blob = store.blob_dir / rec.sha256[:2] / rec.sha256
    note = state["sandbox"] / "notes.txt"
    note.write_text("new notes")
    extra = state["sandbox"] / "new.txt"
    extra.write_text("remove me")
    unlink = snapshots_mod._force_unlink

    def remove_and_damage_blob(path: Path) -> None:
        unlink(path)
        if path == extra:
            blob.write_bytes(b"corruption after validation")

    monkeypatch.setattr(snapshots_mod, "_force_unlink", remove_and_damage_blob)
    plan = store.restore(snap, KeepList())
    assert not plan.errors
    assert not extra.exists()
    assert note.read_text() == "scratch note"
    assert not list(store.root.glob(".restore-*"))


def test_restore_staging_failure_preserves_current_state(
    state: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, targets = _store_and_targets(state)
    snap = store.take(targets, "golden")
    note = state["sandbox"] / "notes.txt"
    note.write_text("new notes")
    extra = state["sandbox"] / "new.txt"
    extra.write_text("new file")
    original_open = Path.open

    def full_staging_disk(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        if path.parent.name.startswith(".restore-") and mode == "wb":
            if os.name != "nt":
                assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            raise OSError("No space left on device")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", full_staging_disk)
    plan = store.restore(snap, KeepList())
    assert any("No space left on device" in e for e in plan.errors)
    assert not plan.restored and not plan.removed
    assert note.read_text() == "new notes"
    assert extra.read_text() == "new file"
    assert not list(store.root.glob(".restore-*"))


def test_restore_does_not_skip_files_matching_through_a_directory_symlink(
    state: dict[str, Path], tmp_path: Path
) -> None:
    if not state["has_symlinks"]:
        pytest.skip("symlinks unavailable")
    store, targets = _store_and_targets(state)
    snap = store.take(targets, "golden")
    deep = state["sandbox"] / "deep"
    outside = tmp_path / "outside-tree"
    shutil.copytree(deep, outside)
    original = (outside / "nested" / "artifact.bin").read_bytes()
    shutil.rmtree(deep)
    deep.symlink_to(outside, target_is_directory=True)

    plan = store.restore(snap, KeepList())
    assert not plan.errors
    assert not deep.is_symlink()
    assert (deep / "nested" / "artifact.bin").read_bytes() == original
    assert (outside / "nested" / "artifact.bin").read_bytes() == original


@pytest.mark.parametrize("commit", [False, True])
def test_restore_refuses_a_target_root_replaced_by_a_symlink(
    state: dict[str, Path], tmp_path: Path, commit: bool
) -> None:
    if not state["has_symlinks"]:
        pytest.skip("symlinks unavailable")
    store, targets = _store_and_targets(state)
    snap = store.take(targets, "golden")
    root = state["sandbox"]
    saved = tmp_path / "moved-sandbox"
    root.rename(saved)
    outside = tmp_path / "outside-tree"
    outside.mkdir()
    (outside / "notes.txt").write_text("outside current state")
    root.symlink_to(outside, target_is_directory=True)

    plan = (store.restore if commit else store.plan_restore)(snap, KeepList())
    assert any("target root is a symlink" in e for e in plan.errors)
    assert not plan.restored and not plan.removed
    assert (outside / "notes.txt").read_text() == "outside current state"
    assert set(p.name for p in outside.iterdir()) == {"notes.txt"}


def test_force_unlink_does_not_chmod_through_a_symlink(
    state: dict[str, Path], tmp_path: Path
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX modes")
    if not state["has_symlinks"]:
        pytest.skip("symlinks unavailable")
    outside = tmp_path / "outside.txt"
    outside.write_text("data")
    outside.chmod(0o644)
    link = state["sandbox"] / "ln"
    link.symlink_to(outside)
    link.parent.chmod(0o500)  # make unlink fail -> exercise the recovery path
    try:
        with contextlib.suppress(PermissionError, OSError):
            snapshots_mod._force_unlink(link)
    finally:
        link.parent.chmod(0o700)
    assert outside.stat().st_mode & 0o777 == 0o644, "chmod leaked through the link"


def test_a_remote_only_snapshot_does_not_shadow_a_real_restore_point(
    state: dict[str, Path],
) -> None:
    """A remote-only wipe takes a snapshot purely to keep the id manifest.

    It records no file targets, so letting it answer "restore me to now"
    would silently roll a tree back to nothing while the real restore point
    sat one entry behind it.
    """
    store, targets = _store_and_targets(state)
    real = store.take(targets, "real-work")
    time.sleep(1.1)  # ids have 1s resolution
    manifest_only = store.take({}, "remote-only")

    latest = store.resolve((datetime.now(UTC) + timedelta(hours=1)).isoformat())
    assert latest is not None
    assert latest.id == real.id, "a content-free snapshot shadowed the real one"
    # ...but it is still reachable by exact id, since it records what was deleted
    assert store.resolve(manifest_only.id) is not None


def test_the_snapshot_store_is_owner_only(state: dict[str, Path]) -> None:
    """The store holds verbatim copies of whatever was in the wipe targets.

    A hygiene tool must not be the thing that discloses the secrets it was
    asked to clean up around: a 0600 credential copied into a 0644 blob under
    a 0755 tree is readable by every other user on the machine.
    """
    if os.name == "nt":
        pytest.skip("POSIX modes")
    secret = state["sandbox"] / "auth.json"
    secret.write_text('{"token":"s3cret"}')
    secret.chmod(0o600)

    store, targets = _store_and_targets(state)
    store.take(targets, "with-a-credential")

    assert store.root.stat().st_mode & 0o077 == 0, "store root is group/world readable"
    assert store.blob_dir.stat().st_mode & 0o077 == 0
    for blob in store.blob_dir.glob("*/*"):
        assert blob.stat().st_mode & 0o077 == 0, f"{blob} is readable by others"
    for manifest in store.manifest_dir.glob("*.json"):
        assert manifest.stat().st_mode & 0o077 == 0, "manifest lists every path"


def test_directory_modes_survive_a_restore(state: dict[str, Path]) -> None:
    """Recreating a 0700 tree at the umask default would turn a private
    directory world-readable as a side effect of rolling back."""
    if os.name == "nt":
        pytest.skip("POSIX modes")
    private = state["sandbox"] / "private"
    private.mkdir()
    private.chmod(0o700)
    (private / "note.txt").write_text("secret")

    store, targets = _store_and_targets(state)
    snap = store.take(targets, "with-a-private-dir")
    shutil.rmtree(private)

    store.restore(snap, KeepList())
    assert private.is_dir()
    assert private.stat().st_mode & 0o777 == 0o700, "restore widened the mode"
    assert (private / "note.txt").read_text() == "secret"


def test_restore_handles_a_file_that_became_a_directory(state: dict[str, Path]) -> None:
    """An audit believed this aborted restore mid-flight. It does not -- the
    removal pass clears the wrong-typed entry before the write pass runs.
    Locked in here so it stays that way."""
    store, targets = _store_and_targets(state)
    thing = state["sandbox"] / "thing"
    thing.write_text("i was a file")
    snap = store.take(targets, "before-the-swap")

    thing.unlink()
    thing.mkdir()
    (thing / "inner.txt").write_text("now a directory")

    plan = store.restore(snap, KeepList())
    assert not plan.errors, plan.errors
    assert thing.is_file(), "a file recorded as a file came back as something else"
    assert thing.read_text() == "i was a file"


def test_restore_handles_a_directory_that_became_a_file(state: dict[str, Path]) -> None:
    store, targets = _store_and_targets(state)
    memory = state["sandbox"] / "memory"
    memory.mkdir()
    (memory / "note.md").write_text("ORIGINAL")
    snap = store.take(targets, "before-the-swap")

    shutil.rmtree(memory)
    memory.write_text("now a file")

    plan = store.restore(snap, KeepList())
    assert not plan.errors, plan.errors
    assert memory.is_dir(), "a directory recorded as a directory did not come back"
    assert (memory / "note.md").read_text() == "ORIGINAL"
