"""The live-harness guard: advisory, honest, and never silently skipped."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from neurailyzer import liveness
from neurailyzer.cli import app

runner = CliRunner()


def test_wal_sidecar_is_a_liveness_signal(tmp_path: Path) -> None:
    store = tmp_path / "state.db"
    store.write_bytes(b"SQLite format 3\x00")
    (tmp_path / "state.db-wal").write_bytes(b"wal")
    result = liveness.check("hermes", (tmp_path,))
    assert result.likely_running
    assert any("write-ahead" in r for r in result.reasons)


def test_clean_tree_is_not_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(liveness, "_running_process_hits", lambda _f: [])
    (tmp_path / "sessions.jsonl").write_text("{}\n")
    assert not liveness.check("hermes", (tmp_path,)).likely_running


def test_process_scan_failure_is_not_a_false_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # if we can't ask the OS, we must not claim something is running
    monkeypatch.setattr(liveness.shutil, "which", lambda _name: None)
    assert liveness._running_process_hits(("hermes",)) == []


def test_unknown_preset_has_no_hints(tmp_path: Path) -> None:
    assert not liveness.check("not-a-harness", (tmp_path,)).likely_running


def test_check_enabled_returns_only_positives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(liveness, "_running_process_hits", lambda _f: [])
    (tmp_path / "state.db-shm").write_bytes(b"shm")
    hits = liveness.check_enabled(["hermes", "codex"], (tmp_path,))
    assert {h.preset_id for h in hits} == {"hermes", "codex"}  # both see the sidecar
    monkeypatch.setattr(liveness, "_open_wal_sidecars", lambda _r: [])
    assert liveness.check_enabled(["hermes"], (tmp_path,)) == []


# -- CLI wiring -------------------------------------------------------------


@pytest.fixture
def live_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    home = tmp_path / "home"
    claude = home / ".claude"
    (claude / "projects" / "-p").mkdir(parents=True)
    (claude / "projects" / "-p" / "s.jsonl").write_text("{}\n")
    (claude / "projects" / "-p" / "state.db-wal").write_bytes(b"live")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        '[presets]\nenabled = ["claude-code"]\n'
        f'\n[snapshots]\ndir = "{(tmp_path / "snaps").as_posix()}"\n'
    )
    return {"config": cfg, "claude": claude}


def test_wipe_refuses_when_harness_looks_live(live_config: dict[str, Any]) -> None:
    result = runner.invoke(
        app, ["--config", str(live_config["config"]), "wipe", "session", "--commit"]
    )
    assert result.exit_code == 3
    # nothing was touched
    assert (live_config["claude"] / "projects" / "-p" / "s.jsonl").exists()


def test_force_overrides_the_guard(live_config: dict[str, Any]) -> None:
    result = runner.invoke(
        app,
        [
            "--config",
            str(live_config["config"]),
            "wipe",
            "session",
            "--commit",
            "--force",
        ],
    )
    assert result.exit_code == 0
    assert not (live_config["claude"] / "projects" / "-p" / "s.jsonl").exists()


def test_dry_run_is_never_blocked_by_the_guard(live_config: dict[str, Any]) -> None:
    # a plan mutates nothing, so liveness is irrelevant to it
    result = runner.invoke(app, ["--config", str(live_config["config"]), "wipe", "session"])
    assert result.exit_code == 0
    assert "DRY-RUN" in result.stdout


# -- pid-file detection (the strong signal) ---------------------------------


def test_live_pid_file_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude Code names its session files after the pid; a live one counts."""
    sessions = tmp_path / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{os.getppid()}.json").write_text("{}")
    monkeypatch.setattr(liveness, "PID_SOURCES", {"claude-code": (str(sessions / "*.json"),)})
    result = liveness.check("claude-code", ())
    assert result.likely_running
    assert any("live process" in r for r in result.reasons)


def test_stale_pid_file_does_not_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crashed run leaves its file behind -- that must not block forever."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "999999.json").write_text("{}")  # no such process
    monkeypatch.setattr(liveness, "PID_SOURCES", {"claude-code": (str(sessions / "*.json"),)})
    assert not liveness.check("claude-code", ()).likely_running


def test_pid_read_from_json_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ide/<port>.lock form carries the pid in the body, not the name."""
    locks = tmp_path / "ide"
    locks.mkdir()
    (locks / "56736.lock").write_text(json.dumps({"pid": os.getppid()}))
    monkeypatch.setattr(liveness, "PID_SOURCES", {"claude-code": (str(locks / "*.lock"),)})
    assert liveness.check("claude-code", ()).likely_running


def test_unset_env_template_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROK_HOME", raising=False)
    # an unexpanded $VAR must not be globbed as a literal path
    assert liveness._pids_from(("$GROK_HOME/leader.lock",)) == []


def test_pid_harness_does_not_fall_back_to_fuzzy_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A harness with a pid contract must not be flagged by name-grepping."""
    called = False

    def _spy(_frags: tuple[str, ...]) -> list[str]:
        nonlocal called
        called = True
        return ["claude"]

    monkeypatch.setattr(liveness, "_running_process_hits", _spy)
    monkeypatch.setattr(liveness, "PID_SOURCES", {"claude-code": ()})
    assert not liveness.check("claude-code", ()).likely_running
    assert not called, "pid-backed harness must not use command-line matching"


def test_sidecar_is_blamed_on_the_owning_preset_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One sidecar under Codex's tree must not report Hermes as running --
    naming a harness the user may not even have installed."""
    monkeypatch.setattr(liveness, "_running_process_hits", lambda _f: [])
    codex = tmp_path / "codex"
    hermes = tmp_path / "hermes"
    codex.mkdir()
    hermes.mkdir()
    (codex / "state_5.sqlite-wal").write_bytes(b"wal")

    hits = liveness.check_enabled(
        ["codex", "hermes"],
        (codex, hermes),
        {"codex": (codex,), "hermes": (hermes,)},
    )
    assert [h.preset_id for h in hits] == ["codex"]


def test_hand_configured_targets_are_still_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scoping per preset must not leave [targets] paths unguarded."""
    monkeypatch.setattr(liveness, "_running_process_hits", lambda _f: [])
    mine = tmp_path / "my-agent"
    mine.mkdir()
    (mine / "chat.db-wal").write_bytes(b"wal")

    hits = liveness.check_enabled(["codex"], (mine,), {"codex": ()})
    assert [h.preset_id for h in hits] == [liveness.TARGETS_PSEUDO_PRESET]


def test_sidecar_search_stops_at_the_limit(tmp_path: Path) -> None:
    """A real ~/.claude is gigabytes; the walk must not stat all of it."""
    root = tmp_path / "big"
    root.mkdir()
    for i in range(25):
        (root / f"db{i}.sqlite-wal").write_bytes(b"wal")
    assert len(liveness._open_wal_sidecars((root,), limit=5)) == 5
