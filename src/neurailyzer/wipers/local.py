"""Local adapters -- file-tree state (``session``, ``sandbox`` and ``memory`` scopes).

``PathWiper`` resets the file trees a scope's config points at. Safety
properties, uniform across platforms:

- **keep-list**: protected entries are skipped and *reported*, never silently
  honored or ignored (DESIGN §8). Directories sheltering a kept entry survive.
- **symlinks**: a link is removed as a link. The wiper never follows one, so a
  sandbox symlink aimed at ``$HOME`` can't turn into a home wipe.
- **read-only files** (the Windows attribute) are cleared and removed.
- the target roots themselves survive -- a wipe empties them, it doesn't
  delete the workspace.
- **time window**: with a ``TimeWindow`` only entries whose own mtime
  (``lstat``: the link, never its destination) fall inside it are touched;
  the rest are skipped and *counted* in the plan. Boundaries are per file,
  so a database file goes whole or stays whole -- the plan says so.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Sequence
from pathlib import Path

from ..config import KeepList
from ..timewindow import TimeWindow
from .base import WipePlan, Wiper

#: a file that holds many records under one mtime: a time window is coarse here
_DB_MARKERS = (".sqlite", ".db")


def _walk(root: Path) -> tuple[list[Path], list[Path], list[str]]:
    """(entries, dirs, errors) under *root*.

    Entries are files+symlinks to unlink, dirs are candidates to prune.
    Never follows symlinks; a symlinked directory is returned as an entry (the
    link gets unlinked), its destination is never visited.

    ``os.walk`` swallows permission errors by default, which would let an
    unreadable subtree vanish from both the plan and the verification -- so a
    file that survived the wipe would still be reported as gone. Errors are
    collected and surfaced instead.
    """
    entries: list[Path] = []
    dirs: list[Path] = []
    errors: list[str] = []

    def _record(exc: OSError) -> None:
        errors.append(f"could not read {exc.filename}: {exc.strerror}")

    if root.is_symlink() or not root.exists():
        return entries, dirs, errors
    if root.is_file():
        return [root], dirs, errors
    for cur, dnames, fnames in os.walk(root, followlinks=False, onerror=_record):
        cur_p = Path(cur)
        for d in list(dnames):
            p = cur_p / d
            if p.is_symlink():
                dnames.remove(d)
                entries.append(p)
            else:
                dirs.append(p)
        entries.extend(cur_p / f for f in fnames)
    return entries, dirs, errors


def _force_unlink(path: Path) -> None:
    """Unlink, clearing a read-only attribute if that is what blocks us.

    The chmod recovery NEVER runs on a symlink: os.chmod follows links, so it
    would rewrite the mode of the destination -- a file outside the wipe
    target that we were never authorized to touch.
    """
    try:
        path.unlink()
    except PermissionError:
        if path.is_symlink():
            raise
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        path.unlink()


def _looks_like_db(p: Path) -> bool:
    name = p.name.lower()
    return any(m in name for m in _DB_MARKERS)


class PathWiper(Wiper):
    """Wipes the contents of configured file trees, keep-list excepted."""

    def __init__(
        self,
        scope: str,
        roots: Sequence[Path],
        keep: KeepList,
        *,
        window: TimeWindow | None = None,
    ) -> None:
        self.scope = scope
        self.roots = tuple(roots)
        self.keep = keep
        self.window = window if window is not None and not window.is_open else None

    def _in_window(self, p: Path) -> bool | str:
        """True/False for a dated entry; the error text when it cannot be dated.

        An undated file is neither in nor out of the window. It stays, and it
        is an ERROR (incomplete plan, failed verify), never a quiet skip: a
        file the wiper could not read may be exactly the one asked for.
        """
        if self.window is None:
            return True
        try:
            return self.window.contains(os.lstat(p).st_mtime)
        except OSError as exc:
            return f"could not date {p}: {exc.strerror or exc}"

    def _survey(self) -> tuple[list[Path], list[Path], list[Path], list[str], list[Path]]:
        """(doomed entries, doomed dirs, keep-skipped, errors, out-of-window)."""
        doomed: list[Path] = []
        doomed_dirs: list[Path] = []
        skipped: list[Path] = []
        errors: list[str] = []
        outside: list[Path] = []
        for root in self.roots:
            entries, dirs, errs = _walk(root)
            errors.extend(errs)
            for p in entries:
                if self.keep.protects(p):
                    skipped.append(p)
                    continue
                dated = self._in_window(p)
                if dated is True:
                    doomed.append(p)
                elif dated is False:
                    outside.append(p)
                else:
                    errors.append(dated)
            # With a window, a directory is pruned only if the wipe emptied
            # it: an ancestor of something doomed. An untouched empty dir
            # dated 2020 is state outside the window and stays.
            prunable: set[Path] | None = None
            if self.window is not None:
                prunable = set()
                for p in doomed:
                    for anc in p.parents:
                        if anc == root or root not in anc.parents:
                            break
                        prunable.add(anc)
            for d in dirs:
                if self.keep.protects(d) or self.keep.shelters(d):
                    skipped.append(d)
                elif prunable is not None and d not in prunable:
                    continue
                else:
                    # a dir still holding out-of-window files simply fails rmdir
                    doomed_dirs.append(d)
        return doomed, doomed_dirs, skipped, errors, outside

    def _window_notes(self, doomed: list[Path], outside: list[Path]) -> tuple[str, ...]:
        if self.window is None:
            return ()
        notes = [f"time window: {self.window.describe()}"]
        if outside:
            notes.append(
                f"time-window skip: {len(outside)} file(s)/link(s) outside the window left alone"
            )
        dbs = sorted(str(p) for p in (*doomed, *outside) if _looks_like_db(p))
        if dbs:
            notes.append(
                "time window is per FILE: a database file carries one mtime for "
                "every row, so it goes whole or stays whole: " + ", ".join(dbs)
            )
        return tuple(notes)

    def plan(self) -> WipePlan:
        doomed, doomed_dirs, skipped, errors, outside = self._survey()
        size = sum(p.stat().st_size for p in doomed if p.is_file() and not p.is_symlink())
        return WipePlan(
            scope=self.scope,
            description=(
                f"remove {len(doomed)} file(s)/link(s) and {len(doomed_dirs)} dir(s) "
                f"({size} bytes) under: " + ", ".join(str(r) for r in self.roots)
            ),
            item_count=len(doomed) + len(doomed_dirs),
            bytes_total=size,
            reversible=True,  # core snapshots before committing
            complete=not errors,
            notes=(
                *self._window_notes(doomed, outside),
                *(f"keep-list skip: {p}" for p in sorted(map(str, skipped))),
                *(f"UNREADABLE: {e}" for e in errors),
            ),
        )

    def commit(self) -> WipePlan:
        doomed, doomed_dirs, skipped, errors, outside = self._survey()
        removed = 0
        for p in doomed:
            try:
                _force_unlink(p)
                removed += 1
            except OSError as exc:
                # one stubborn file must not abort the rest of the wipe --
                # and must not be counted as removed either
                errors.append(f"could not remove {p}: {exc.strerror or exc}")
        # deepest first, so children fall before parents; kept content holds a dir up
        for d in sorted(doomed_dirs, key=lambda x: len(x.parts), reverse=True):
            with contextlib.suppress(OSError):
                d.rmdir()
        return WipePlan(
            scope=self.scope,
            description=f"removed {removed} of {len(doomed)} file(s)/link(s) under: "
            + ", ".join(str(r) for r in self.roots),
            item_count=removed,
            reversible=True,
            complete=not errors,
            notes=(
                *self._window_notes(doomed, outside),
                *(f"keep-list skip: {p}" for p in sorted(map(str, skipped))),
                *(f"UNREADABLE: {e}" for e in errors),
            ),
        )

    def verify(self) -> bool:
        """True only if the tree was fully READABLE and nothing is left.

        A subtree we could not read may still hold the very files the user
        asked to destroy; reporting "verified" there is a false assurance of
        deletion, which for a privacy tool is the worst possible lie. With a
        time window, "nothing left" means nothing left INSIDE the window.
        """
        doomed, _dirs, _skipped, errors, _outside = self._survey()
        return not doomed and not errors
