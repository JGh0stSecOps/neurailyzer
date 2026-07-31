"""Core orchestrator: snapshot-before-commit, scope expansion, reporting."""

from __future__ import annotations

from pathlib import Path

from neurailyzer import core
from neurailyzer.config import load
from neurailyzer.snapshots import SnapshotStore


def test_expand_scopes_all_and_dedupe() -> None:
    assert core.expand_scopes(["session", "session", "sandbox"]) == ["session", "sandbox"]
    assert set(core.expand_scopes(["all"])) == {
        "session",
        "sandbox",
        "rag",
        "models",
        "remote",
    }


def test_plan_wipe_skips_unconfigured_and_pending(state: dict[str, Path]) -> None:
    cfg = load(state["config"])
    plans = core.plan_wipe(cfg, ["session", "sandbox", "rag", "models", "remote"])
    assert {p.scope for p in plans} == {"session", "sandbox"}


def test_execute_wipe_snapshots_first(state: dict[str, Path]) -> None:
    cfg = load(state["config"])
    report = core.execute_wipe(cfg, ["session", "sandbox"])
    assert report.snapshot is not None
    assert report.verified == {"session": True, "sandbox": True}
    # the snapshot captured the state that was just destroyed
    store = SnapshotStore(cfg.snapshot_dir)
    snaps = store.list()
    assert len(snaps) == 1
    assert "threads/t1.jsonl" in {f.relpath for f in snaps[0].files}
    assert not (state["session"] / "threads").exists()


def test_execute_wipe_without_snapshot_when_waived(state: dict[str, Path]) -> None:
    cfg = load(state["config"])
    report = core.execute_wipe(cfg, ["sandbox"], take_snapshot=False)
    assert report.snapshot is None
    assert not SnapshotStore(cfg.snapshot_dir).list()


def test_execute_wipe_nothing_configured(tmp_path: Path) -> None:
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text("")
    report = core.execute_wipe(load(cfg_file), ["session"])
    assert report.snapshot is None
    assert report.plans == ()


def test_scope_status_shapes(state: dict[str, Path]) -> None:
    cfg = load(state["config"])
    ready = core.scope_status(cfg, "sandbox")
    assert ready.configured and ready.available and ready.file_count > 0
    pending = core.scope_status(cfg, "rag")
    assert not pending.available
