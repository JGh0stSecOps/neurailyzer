"""Point-in-time snapshots: take / list / restore ``--to T``.

Layout (all under the configured snapshot dir, which the keep-list always
protects):

    blobs/<aa>/<sha256>      content-addressed file bodies (deduplicated)
    manifests/<id>.json      one manifest per snapshot

A manifest records, per target root: every file (relpath, sha256, size, mode,
mtime), every directory (so empty ones come back), and every symlink (the link
itself, never its destination). Snapshot ids are filesystem-safe on every
platform (UTC compact timestamp — no colons, which Windows forbids).

``restore`` is a true rollback: it rewrites recorded files, recreates dirs and
links, and removes anything present now that the snapshot doesn't know --
except keep-list entries, which are skipped and reported.
"""

from __future__ import annotations

import builtins  # the SnapshotStore.list method shadows the builtin in annotations
import contextlib
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .config import KeepList

_BLOBS = "blobs"
_MANIFESTS = "manifests"


class SnapshotError(RuntimeError):
    """A snapshot operation failed. The message says why."""


@dataclass(frozen=True)
class FileRecord:
    """One regular file inside a snapshot."""

    target: str  # absolute target root (native form)
    relpath: str  # POSIX-style path relative to the target root
    sha256: str
    size: int
    mode: int
    mtime: float


@dataclass(frozen=True)
class LinkRecord:
    """One symlink inside a snapshot -- the link itself, never what it points at."""

    target: str
    relpath: str
    link_to: str


@dataclass(frozen=True)
class Snapshot:
    """A restore point."""

    id: str
    label: str
    taken_at: datetime  # aware, UTC
    scopes: tuple[str, ...]
    targets: dict[str, tuple[str, ...]]  # scope -> absolute roots
    dir_roots: tuple[str, ...]  # roots that were directories when taken
    files: tuple[FileRecord, ...]
    dirs: tuple[tuple[str, str], ...]  # (target root, relpath)
    links: tuple[LinkRecord, ...]

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


@dataclass
class RestorePlan:
    """What a restore would do (dry-run) or did (commit)."""

    snapshot_id: str
    restored: list[str] = field(default_factory=list)  # written back from blobs
    removed: list[str] = field(default_factory=list)  # present now, absent at T
    skipped_keep: list[str] = field(default_factory=list)  # keep-list, untouched
    errors: list[str] = field(default_factory=list)

    @property
    def change_count(self) -> int:
        return len(self.restored) + len(self.removed)


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk_tree(root: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """(files, dirs, symlinks) under *root*, never following symlinks."""
    files: list[Path] = []
    dirs: list[Path] = []
    links: list[Path] = []
    if root.is_symlink() or not root.exists():
        return files, dirs, links
    if root.is_file():
        return [root], dirs, links
    for cur, dnames, fnames in os.walk(root, followlinks=False):
        cur_p = Path(cur)
        for d in list(dnames):
            p = cur_p / d
            if p.is_symlink():
                dnames.remove(d)  # never descend through a link
                links.append(p)
            else:
                dirs.append(p)
        for f in fnames:
            p = cur_p / f
            if p.is_symlink():
                links.append(p)
            else:
                files.append(p)
    return files, dirs, links


def _parse_manifest(data: dict[str, Any]) -> Snapshot:
    return Snapshot(
        id=data["id"],
        label=data["label"],
        taken_at=datetime.fromisoformat(data["taken_at"]),
        scopes=tuple(data["scopes"]),
        targets={k: tuple(v) for k, v in data["targets"].items()},
        dir_roots=tuple(data["dir_roots"]),
        files=tuple(FileRecord(**f) for f in data["files"]),
        dirs=tuple((d[0], d[1]) for d in data["dirs"]),
        links=tuple(LinkRecord(**ln) for ln in data["links"]),
    )


def parse_point_in_time(raw: str) -> datetime:
    """Parse ``--to``: ISO-8601, naive times taken as local time."""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SnapshotError(
            f"--to {raw!r} is neither a snapshot id nor an ISO-8601 time (e.g. 2026-07-09T04:00)"
        ) from exc
    if dt.tzinfo is None:
        try:
            dt = dt.astimezone()  # interpret as local time
        except (OSError, OverflowError, ValueError):
            # Windows localtime() can't map far-future/past dates -- outside
            # any plausible DST question, so plain UTC is the right reading.
            dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class SnapshotStore:
    """Content-addressed snapshot store rooted at *root*."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.blob_dir = root / _BLOBS
        self.manifest_dir = root / _MANIFESTS

    # -- take -----------------------------------------------------------------

    def take(self, targets: dict[str, tuple[Path, ...]], label: str) -> Snapshot:
        """Record every file/dir/symlink under the target roots."""
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(UTC)
        slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40] or "snap"
        snap_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(2)}-{slug}"

        files: list[FileRecord] = []
        dirs: list[tuple[str, str]] = []
        links: list[LinkRecord] = []
        dir_roots: set[str] = set()
        for _scope, roots in sorted(targets.items()):
            for root in roots:
                froms, ds, lns = _walk_tree(root)
                if root.is_dir() and not root.is_symlink():
                    dir_roots.add(str(root))
                base = root if root.is_dir() else root.parent
                for f in froms:
                    digest = self._store_blob(f)
                    st = f.stat()
                    files.append(
                        FileRecord(
                            target=str(root),
                            relpath=f.relative_to(base).as_posix(),
                            sha256=digest,
                            size=st.st_size,
                            mode=st.st_mode,
                            mtime=st.st_mtime,
                        )
                    )
                for d in ds:
                    dirs.append((str(root), d.relative_to(base).as_posix()))
                for ln in lns:
                    links.append(
                        LinkRecord(
                            target=str(root),
                            relpath=ln.relative_to(base).as_posix(),
                            link_to=os.readlink(ln),
                        )
                    )

        snap = Snapshot(
            id=snap_id,
            label=label,
            taken_at=now,
            scopes=tuple(sorted(targets)),
            targets={scope: tuple(str(r) for r in roots) for scope, roots in targets.items()},
            dir_roots=tuple(sorted(dir_roots)),
            files=tuple(files),
            dirs=tuple(dirs),
            links=tuple(links),
        )
        manifest = {
            "id": snap.id,
            "label": snap.label,
            "taken_at": snap.taken_at.isoformat(),
            "scopes": list(snap.scopes),
            "targets": {k: list(v) for k, v in snap.targets.items()},
            "dir_roots": list(snap.dir_roots),
            "files": [vars(f) for f in snap.files],
            "dirs": [list(d) for d in snap.dirs],
            "links": [vars(ln) for ln in snap.links],
        }
        path = self.manifest_dir / f"{snap_id}.json"
        path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        return snap

    def _store_blob(self, src: Path) -> str:
        digest = _hash_file(src)
        dest = self.blob_dir / digest[:2] / digest
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".tmp")
            tmp.write_bytes(src.read_bytes())
            tmp.replace(dest)
        return digest

    # -- list / resolve -------------------------------------------------------

    def list(self) -> builtins.list[Snapshot]:
        """All snapshots, oldest first."""
        if not self.manifest_dir.is_dir():
            return []
        snaps: builtins.list[Snapshot] = []
        for mf in sorted(self.manifest_dir.glob("*.json")):
            try:
                snaps.append(_parse_manifest(json.loads(mf.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise SnapshotError(f"corrupt manifest {mf}: {exc}") from exc
        snaps.sort(key=lambda s: s.taken_at)
        return snaps

    def resolve(self, to: str) -> Snapshot | None:
        """*to* is a snapshot id, or a time -- nearest snapshot at/before it."""
        snaps = self.list()
        for s in snaps:
            if s.id == to:
                return s
        point = parse_point_in_time(to)
        eligible = [s for s in snaps if s.taken_at <= point]
        return eligible[-1] if eligible else None

    # -- restore --------------------------------------------------------------

    def plan_restore(self, snap: Snapshot, keep: KeepList) -> RestorePlan:
        return self._restore(snap, keep, commit=False)

    def restore(self, snap: Snapshot, keep: KeepList) -> RestorePlan:
        return self._restore(snap, keep, commit=True)

    def _restore(self, snap: Snapshot, keep: KeepList, *, commit: bool) -> RestorePlan:
        plan = RestorePlan(snapshot_id=snap.id)
        roots = [Path(r) for rs in snap.targets.values() for r in rs]

        wanted_files = {(f.target, f.relpath): f for f in snap.files}
        wanted_dirs = set(snap.dirs)
        wanted_links = {(ln.target, ln.relpath): ln for ln in snap.links}

        # 1. remove what exists now but didn't at T (keep-list excepted)
        for root in roots:
            base = root if root.is_dir() else root.parent
            files, dirs, links = _walk_tree(root)
            for p in files + links:
                key = (str(root), p.relative_to(base).as_posix())
                known = key in wanted_files or key in wanted_links
                if known:
                    continue
                if keep.protects(p):
                    plan.skipped_keep.append(str(p))
                    continue
                plan.removed.append(str(p))
                if commit:
                    _force_unlink(p)
            for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):
                key = (str(root), d.relative_to(base).as_posix())
                if key in wanted_dirs:
                    continue
                if keep.protects(d) or keep.shelters(d):
                    continue
                if commit:
                    with contextlib.suppress(OSError):
                        d.rmdir()  # only empties fall; kept content holds the dir

        # 2. put back what the snapshot recorded
        dir_roots = set(snap.dir_roots)
        for (target, relpath), rec in sorted(wanted_files.items()):
            dest = _dest(target, relpath, dir_roots)
            blob = self.blob_dir / rec.sha256[:2] / rec.sha256
            differs = not (
                dest.is_file() and not dest.is_symlink() and _hash_file(dest) == rec.sha256
            )
            if not differs:
                continue
            plan.restored.append(str(dest))
            if not commit:
                continue
            if not blob.exists():
                plan.errors.append(f"missing blob for {dest} ({rec.sha256[:12]}...)")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() or dest.is_symlink():
                _force_unlink(dest)
            dest.write_bytes(blob.read_bytes())
            os.chmod(dest, stat.S_IMODE(rec.mode))
            os.utime(dest, (rec.mtime, rec.mtime))
        for target, relpath in sorted(wanted_dirs):
            dest = _dest(target, relpath, dir_roots)
            if commit and not dest.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
        for (target, relpath), lrec in sorted(wanted_links.items()):
            dest = _dest(target, relpath, dir_roots)
            if dest.is_symlink() and os.readlink(dest) == lrec.link_to:
                continue
            plan.restored.append(str(dest))
            if not commit:
                continue
            try:
                if dest.exists() or dest.is_symlink():
                    _force_unlink(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(lrec.link_to, dest)
            except OSError as exc:  # e.g. Windows without symlink privilege
                plan.errors.append(f"could not recreate symlink {dest}: {exc}")
        return plan

    # -- retention ------------------------------------------------------------

    def prune(self, retention: int) -> builtins.list[str]:
        """Drop the oldest snapshots beyond *retention*; GC unreferenced blobs."""
        snaps = self.list()
        doomed = snaps[:-retention] if retention and len(snaps) > retention else []
        if not doomed:
            return []
        for s in doomed:
            (self.manifest_dir / f"{s.id}.json").unlink(missing_ok=True)
        alive = {f.sha256 for s in self.list() for f in s.files}
        if self.blob_dir.is_dir():
            for blob in self.blob_dir.glob("*/*"):
                if blob.name not in alive:
                    blob.unlink(missing_ok=True)
        return [s.id for s in doomed]


def _dest(target: str, relpath: str, dir_roots: set[str]) -> Path:
    """Absolute destination for a manifest entry.

    A directory root records children relative to itself; a single-file root
    records itself relative to its parent (so *relpath* is just its name).
    """
    root = Path(target)
    base = root if target in dir_roots else root.parent
    return base / Path(*PurePosixPath(relpath).parts)


def _force_unlink(path: Path) -> None:
    """Unlink even Windows read-only files. Never follows symlinks."""
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        path.unlink()
