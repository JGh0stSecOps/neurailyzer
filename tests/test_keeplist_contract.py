"""The keep-list contract, tested where it actually bites.

A "credentials survived" assertion proves nothing if the file was never
inside a wipe target -- it would survive with no keep-list at all. Every test
here first asserts the protected file IS under a target root (so the wiper
genuinely walks over it), then asserts it survives and that the skip is
reported. If the keep-list silently stopped working, these fail; the earlier
end-to-end assertions would not have.
"""

from __future__ import annotations

import os
import sys
import unicodedata
from pathlib import Path

import pytest

from neurailyzer.config import KeepList, load
from neurailyzer.wipers.local import PathWiper


def _under(path: Path, roots: tuple[Path, ...]) -> bool:
    """Is *path* genuinely inside one of the wipe targets?"""
    return any(path == root or root in path.parents for root in roots)


def _wipe(cfg_path: Path, scope: str = "session") -> tuple[PathWiper, tuple[Path, ...]]:
    cfg = load(cfg_path)
    roots = cfg.roots_for(scope)
    return PathWiper(scope, roots, cfg.keep), roots


def _config(tmp_path: Path, target: Path, keep: list[str]) -> Path:
    cfg = tmp_path / "c.toml"
    # forward slashes on every platform: a Windows path in TOML would treat
    # backslash sequences as escapes ("\U..." -> invalid hex value)
    entries = ", ".join(f'"{Path(k).as_posix()}"' for k in keep)
    cfg.write_text(
        f'[targets.session]\npaths = ["{target.as_posix()}"]\n'
        f"[keep]\npaths = [{entries}]\n"
        f'[snapshots]\ndir = "{(tmp_path / "snaps").as_posix()}"\n'
    )
    return cfg


def test_kept_file_inside_a_target_survives_and_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "state"
    (target / "keepme").mkdir(parents=True)
    secret = target / "keepme" / "creds.json"
    secret.write_text("SECRET")
    (target / "chat.jsonl").write_text("history")

    wiper, roots = _wipe(_config(tmp_path, target, [str(target / "keepme")]))
    assert _under(secret, roots), "test is vacuous: the file is not in a target"
    plan = wiper.plan()
    wiper.commit()

    assert secret.read_text() == "SECRET"
    assert not (target / "chat.jsonl").exists()
    assert any("creds.json" in n or "keepme" in n for n in plan.notes), (
        "a keep-list skip must be reported, never silent"
    )


def test_symlinked_keep_entry_is_protected(tmp_path: Path) -> None:
    """The walker sees the LINK; a keep entry stored only as its destination
    would never match it -- and the link would be unlinked."""
    target = tmp_path / "state"
    target.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    real = vault / "creds.json"
    real.write_text("SECRET")
    link = target / "creds.json"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")
    (target / "chat.jsonl").write_text("history")

    wiper, roots = _wipe(_config(tmp_path, target, [str(link)]))
    assert _under(link, roots)
    wiper.commit()

    assert link.is_symlink(), "a symlinked keep entry was unlinked"
    assert real.read_text() == "SECRET"
    assert not (target / "chat.jsonl").exists()


def test_keep_entry_written_as_destination_still_protects_the_link(
    tmp_path: Path,
) -> None:
    """The reverse spelling: config names the destination, disk holds a link."""
    target = tmp_path / "state"
    target.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    real = vault / "notes.md"
    real.write_text("KEEP")
    link = target / "notes.md"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")

    wiper, roots = _wipe(_config(tmp_path, target, [str(real)]))
    assert _under(link, roots)
    wiper.commit()
    assert link.is_symlink()
    assert real.read_text() == "KEEP"


def test_target_under_a_symlinked_root_still_honors_keep_patterns(
    tmp_path: Path,
) -> None:
    """stow/chezmoi put ~/.claude in a dotfiles repo. Targets resolve; a
    pattern stored unresolved could then never match."""
    real_home = tmp_path / "dotfiles" / "claude"
    (real_home / "projects" / "slug" / "memory").mkdir(parents=True)
    (real_home / "projects" / "slug" / "memory" / "MEMORY.md").write_text("DURABLE")
    (real_home / "projects" / "slug" / "sess.jsonl").write_text("history")
    linked = tmp_path / ".claude"
    try:
        linked.symlink_to(real_home, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[targets.session]\npaths = ["{(linked / "projects").as_posix()}"]\n'
        f'[keep]\npaths = ["{(linked / "projects" / "*" / "memory").as_posix()}"]\n'
        f'[snapshots]\ndir = "{(tmp_path / "snaps").as_posix()}"\n'
    )
    wiper, roots = _wipe(cfg)
    memory = real_home / "projects" / "slug" / "memory" / "MEMORY.md"
    assert _under(memory.resolve(), tuple(r.resolve() for r in roots))
    wiper.commit()
    assert memory.read_text() == "DURABLE", "pattern failed through a symlinked root"
    assert not (real_home / "projects" / "slug" / "sess.jsonl").exists()


def test_pattern_protects_a_directory_created_after_load(tmp_path: Path) -> None:
    target = tmp_path / "projects"
    (target / "old").mkdir(parents=True)
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[targets.session]\npaths = ["{target.as_posix()}"]\n'
        f'[keep]\npaths = ["{(target / "*" / "memory").as_posix()}"]\n'
        f'[snapshots]\ndir = "{(tmp_path / "snaps").as_posix()}"\n'
    )
    conf = load(cfg)  # loaded BEFORE the directory exists
    later = target / "brand-new" / "memory"
    later.mkdir(parents=True)
    (later / "note.md").write_text("LATE")
    (target / "brand-new" / "sess.jsonl").write_text("history")

    PathWiper("session", conf.roots_for("session"), conf.keep).commit()
    assert (later / "note.md").read_text() == "LATE"
    assert not (target / "brand-new" / "sess.jsonl").exists()


@pytest.mark.skipif(
    sys.platform not in ("darwin", "win32"),
    reason="case-insensitive filesystem behavior",
)
def test_case_difference_does_not_void_the_keep_list(tmp_path: Path) -> None:
    """On macOS/Windows `Path.resolve()` does not canonicalize case, so a
    keep entry spelled differently than the on-disk name must still match."""
    target = tmp_path / "State"
    (target / "Keep").mkdir(parents=True)
    secret = target / "Keep" / "creds.json"
    secret.write_text("SECRET")
    (target / "chat.jsonl").write_text("history")

    # config names the directory in a different case than disk
    wiper, roots = _wipe(_config(tmp_path, target, [str(tmp_path / "state" / "keep")]))
    assert _under(secret, roots)
    wiper.commit()
    assert secret.read_text() == "SECRET", "case difference voided the keep-list"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS hands back NFD")
def test_unicode_normalization_does_not_void_the_keep_list(tmp_path: Path) -> None:
    target = tmp_path / "state"
    name = "café"  # NFC in this source file
    (target / name).mkdir(parents=True)
    secret = target / name / "creds.json"
    secret.write_text("SECRET")
    (target / "chat.jsonl").write_text("history")

    nfd = unicodedata.normalize("NFD", name)
    wiper, roots = _wipe(_config(tmp_path, target, [str(target / nfd)]))
    assert _under(secret, roots)
    wiper.commit()
    assert secret.read_text() == "SECRET", "NFC/NFD divergence voided the keep-list"


def test_shelters_keeps_the_parent_directory_alive(tmp_path: Path) -> None:
    target = tmp_path / "state"
    deep = target / "a" / "b" / "keep"
    deep.mkdir(parents=True)
    (deep / "x.txt").write_text("KEEP")

    wiper, _roots = _wipe(_config(tmp_path, target, [str(deep)]))
    wiper.commit()
    assert (deep / "x.txt").read_text() == "KEEP"
    assert deep.parent.is_dir(), "a directory sheltering a keep entry was removed"


def test_keeplist_prefix_is_not_a_string_prefix(tmp_path: Path) -> None:
    """/a/b must protect /a/b/c but never /a/bc."""
    keep = KeepList(paths=(tmp_path / "a" / "b",))
    assert keep.protects(tmp_path / "a" / "b" / "c.txt")
    assert not keep.protects(tmp_path / "a" / "bc")
    assert not keep.protects(tmp_path / "a" / "b.txt")


def test_readonly_symlink_is_not_chmodded_through(tmp_path: Path) -> None:
    """os.chmod follows links: the recovery path must never rewrite the mode
    of a file outside the wipe target."""
    if os.name == "nt":
        pytest.skip("POSIX modes")
    target = tmp_path / "state"
    target.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("data")
    outside.chmod(0o644)
    try:
        (target / "link").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    wiper, _roots = _wipe(_config(tmp_path, target, []))
    wiper.commit()
    assert outside.stat().st_mode & 0o777 == 0o644, "chmod leaked through the symlink"
    assert outside.read_text() == "data"
