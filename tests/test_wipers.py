"""PathWiper: plan/commit/verify, keep-list, symlink and read-only safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from neurailyzer.config import KeepList, load
from neurailyzer.wipers.local import PathWiper


def _wiper(state: dict[str, Path], scope: str = "sandbox") -> PathWiper:
    cfg = load(state["config"])
    return PathWiper(scope, cfg.roots_for(scope), cfg.keep)


def test_plan_never_mutates(state: dict[str, Path]) -> None:
    before = sorted(p for p in state["root"].rglob("*"))
    plan = _wiper(state).plan()
    assert plan.item_count > 0
    assert plan.bytes_total > 0
    assert sorted(p for p in state["root"].rglob("*")) == before


def test_plan_reports_keep_skips(state: dict[str, Path]) -> None:
    plan = _wiper(state).plan()
    assert any("keep-list skip" in n and "pinned.txt" in n for n in plan.notes)


def test_commit_wipes_and_keep_survives(state: dict[str, Path]) -> None:
    w = _wiper(state)
    w.commit()
    assert not (state["sandbox"] / "notes.txt").exists()
    assert not (state["sandbox"] / "deep").exists()
    assert not (state["sandbox"] / "readonly.lock").exists()  # read-only still dies
    assert (state["keep"] / "pinned.txt").read_text() == "NEVER wipe me"
    assert state["sandbox"].is_dir()  # the workspace itself survives
    assert w.verify()


def test_symlink_target_survives_wipe(state: dict[str, Path]) -> None:
    if not state["has_symlinks"]:
        pytest.skip("symlinks unavailable")
    _wiper(state).commit()
    assert not (state["sandbox"] / "sneaky-link").is_symlink()  # link removed
    assert state["victim"].read_text() == "outside the blast radius"  # target intact


def test_symlinked_dir_not_traversed(state: dict[str, Path]) -> None:
    if not state["has_symlinks"]:
        pytest.skip("symlinks unavailable")
    outside_dir = state["root"] / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "data.txt").write_text("do not enter")
    (state["sandbox"] / "dir-link").symlink_to(outside_dir, target_is_directory=True)
    _wiper(state).commit()
    assert not (state["sandbox"] / "dir-link").is_symlink()
    assert (outside_dir / "data.txt").read_text() == "do not enter"


def test_verify_false_when_state_remains(state: dict[str, Path]) -> None:
    w = _wiper(state)
    assert not w.verify()  # state exists, nothing wiped yet
    w.commit()
    assert w.verify()
    (state["sandbox"] / "new-junk.txt").write_text("regrown")
    assert not w.verify()


def test_missing_root_is_empty_plan(tmp_path: Path) -> None:
    w = PathWiper("sandbox", (tmp_path / "ghost",), KeepList())
    assert w.plan().item_count == 0
    w.commit()  # no error
    assert w.verify()


def test_single_file_root(tmp_path: Path) -> None:
    f = tmp_path / "history.jsonl"
    f.write_text("line\n")
    w = PathWiper("session", (f,), KeepList())
    assert w.plan().item_count == 1
    w.commit()
    assert not f.exists()
    assert w.verify()
