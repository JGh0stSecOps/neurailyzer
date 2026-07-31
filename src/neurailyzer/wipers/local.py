"""Local adapters — file-tree state (``session`` and ``sandbox`` scopes).

``PathWiper`` resets the file trees a scope's config points at. Safety
properties, uniform across platforms:

- **keep-list**: protected entries are skipped and *reported*, never silently
  honored or ignored (DESIGN §8). Directories sheltering a kept entry survive.
- **symlinks**: a link is removed as a link. The wiper never follows one, so a
  sandbox symlink aimed at ``$HOME`` can't turn into a home wipe.
- **read-only files** (the Windows attribute) are cleared and removed.
- the target roots themselves survive — a wipe empties them, it doesn't
  delete the workspace.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Sequence
from pathlib import Path

from ..config import KeepList
from .base import WipePlan, Wiper


def _walk(root: Path) -> tuple[list[Path], list[Path]]:
    """(entries, dirs) under *root*: files+symlinks to unlink, dirs to prune.

    Never follows symlinks; a symlinked directory is returned as an entry (the
    link gets unlinked), its destination is never visited.
    """
    entries: list[Path] = []
    dirs: list[Path] = []
    if root.is_symlink() or not root.exists():
        return entries, dirs
    if root.is_file():
        return [root], dirs
    for cur, dnames, fnames in os.walk(root, followlinks=False):
        cur_p = Path(cur)
        for d in list(dnames):
            p = cur_p / d
            if p.is_symlink():
                dnames.remove(d)
                entries.append(p)
            else:
                dirs.append(p)
        entries.extend(cur_p / f for f in fnames)
    return entries, dirs


def _force_unlink(path: Path) -> None:
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        path.unlink()


class PathWiper(Wiper):
    """Wipes the contents of configured file trees, keep-list excepted."""

    def __init__(self, scope: str, roots: Sequence[Path], keep: KeepList) -> None:
        self.scope = scope
        self.roots = tuple(roots)
        self.keep = keep

    def _survey(self) -> tuple[list[Path], list[Path], list[Path]]:
        """(doomed entries, doomed dirs, keep-skipped paths) across all roots."""
        doomed: list[Path] = []
        doomed_dirs: list[Path] = []
        skipped: list[Path] = []
        for root in self.roots:
            entries, dirs = _walk(root)
            for p in entries:
                (skipped if self.keep.protects(p) else doomed).append(p)
            for d in dirs:
                if self.keep.protects(d) or self.keep.shelters(d):
                    skipped.append(d)
                else:
                    doomed_dirs.append(d)
        return doomed, doomed_dirs, skipped

    def plan(self) -> WipePlan:
        doomed, doomed_dirs, skipped = self._survey()
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
            notes=tuple(f"keep-list skip: {p}" for p in sorted(map(str, skipped))),
        )

    def commit(self) -> WipePlan:
        doomed, doomed_dirs, skipped = self._survey()
        for p in doomed:
            _force_unlink(p)
        # deepest first, so children fall before parents; kept content holds a dir up
        for d in sorted(doomed_dirs, key=lambda x: len(x.parts), reverse=True):
            with contextlib.suppress(OSError):
                d.rmdir()
        return WipePlan(
            scope=self.scope,
            description=f"removed {len(doomed)} file(s)/link(s) under: "
            + ", ".join(str(r) for r in self.roots),
            item_count=len(doomed),
            reversible=True,
            notes=tuple(f"keep-list skip: {p}" for p in sorted(map(str, skipped))),
        )

    def verify(self) -> bool:
        doomed, _dirs, _skipped = self._survey()
        return not doomed
