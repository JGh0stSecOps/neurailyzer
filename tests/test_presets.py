"""Preset layer: registry expansion, keep patterns, detect CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from neurailyzer.cli import app
from neurailyzer.config import ConfigError, KeepList, load
from neurailyzer.wipers.local import PathWiper

runner = CliRunner()


@pytest.fixture
def fake_claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A fake $HOME with a realistic ~/.claude tree (mirrors a live install)."""
    home = tmp_path / "home"
    claude = home / ".claude"
    proj = claude / "projects" / "-Users-me"
    (proj / "memory").mkdir(parents=True)
    (proj / "memory" / "MEMORY.md").write_text("# index\n")
    (proj / "memory" / "fact.md").write_text("durable fact")
    (proj / "abc123.jsonl").write_text('{"type":"user"}\n')
    (proj / "abc123").mkdir()  # per-session subagent dir
    (proj / "abc123" / "sub.jsonl").write_text("{}\n")
    (claude / "history.jsonl").write_text('{"display":"hi"}\n')
    (claude / "shell-snapshots").mkdir()
    (claude / "shell-snapshots" / "snap.sh").write_text("export X=1\n")
    (claude / "settings.json").write_text("{}")
    (claude / "plugins").mkdir()
    (claude / "plugins" / "p.json").write_text("{}")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows expanduser
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("NEURAILYZER_CONFIG", raising=False)

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[presets]\nenabled = ["claude-code"]\n\n'
        f'[snapshots]\ndir = "{(tmp_path / "snaps").as_posix()}"\n'
    )
    return {"home": home, "claude": claude, "proj": proj, "config": cfg}


def test_keep_pattern_protects_future_matches(tmp_path: Path) -> None:
    keep = KeepList(patterns=((tmp_path / "projects" / "*" / "memory").as_posix(),))
    target = tmp_path / "projects" / "new-slug" / "memory" / "later.md"
    assert keep.protects(target)  # doesn't exist yet -- pattern still holds
    assert keep.protects(tmp_path / "projects" / "x" / "memory")
    assert not keep.protects(tmp_path / "projects" / "x" / "session.jsonl")


def test_preset_expands_targets_and_keeps(fake_claude_home: dict[str, Any]) -> None:
    cfg = load(fake_claude_home["config"])
    session_roots = set(cfg.roots_for("session"))
    claude = fake_claude_home["claude"]
    assert (claude / "projects").resolve() in {p.resolve() for p in session_roots}
    assert (claude / "history.jsonl").resolve() in {p.resolve() for p in session_roots}
    sandbox_roots = {p.resolve() for p in cfg.roots_for("sandbox")}
    assert (claude / "shell-snapshots").resolve() in sandbox_roots
    # keep rules protect settings, plugins, and every memory dir
    assert cfg.keep.protects(claude / "settings.json")
    assert cfg.keep.protects(claude / "plugins" / "p.json")
    assert cfg.keep.protects(fake_claude_home["proj"] / "memory" / "fact.md")


def test_preset_wipe_spares_memory_and_settings(fake_claude_home: dict[str, Any]) -> None:
    cfg = load(fake_claude_home["config"])
    proj = fake_claude_home["proj"]
    for scope in ("session", "sandbox"):
        PathWiper(scope, cfg.roots_for(scope), cfg.keep).commit()
    assert not (proj / "abc123.jsonl").exists()  # transcript gone
    assert not (proj / "abc123").exists()  # subagent dir gone
    assert not (fake_claude_home["claude"] / "history.jsonl").exists()
    assert (proj / "memory" / "fact.md").read_text() == "durable fact"  # memory held
    assert (fake_claude_home["claude"] / "settings.json").exists()
    assert (fake_claude_home["claude"] / "plugins" / "p.json").exists()


def test_memory_of_new_project_survives(fake_claude_home: dict[str, Any]) -> None:
    # a project slug created AFTER config load must still be protected
    cfg = load(fake_claude_home["config"])
    newer = fake_claude_home["claude"] / "projects" / "-Users-me-newproj"
    (newer / "memory").mkdir(parents=True)
    (newer / "memory" / "note.md").write_text("late-created memory")
    (newer / "sess.jsonl").write_text("{}\n")
    PathWiper("session", cfg.roots_for("session"), cfg.keep).commit()
    assert (newer / "memory" / "note.md").exists()
    assert not (newer / "sess.jsonl").exists()


def test_unknown_preset_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('[presets]\nenabled = ["not-a-thing"]\n')
    with pytest.raises(ConfigError, match="unknown preset"):
        load(cfg)


def test_detect_finds_claude_code(fake_claude_home: dict[str, Any]) -> None:
    result = runner.invoke(app, ["detect"])
    assert result.exit_code == 0
    assert "Claude Code" in result.stdout


def test_detect_enable_writes_and_merges(fake_claude_home: dict[str, Any], tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.toml"
    result = runner.invoke(app, ["--config", str(fresh), "detect", "--enable"])
    # --config on a missing file errors for other commands, but detect --enable creates it
    assert result.exit_code == 0, result.output
    assert 'enabled = ["claude-code"]' in fresh.read_text()
    # idempotent: enabling again doesn't duplicate
    result = runner.invoke(app, ["--config", str(fresh), "detect", "--enable"])
    assert result.exit_code == 0
    assert fresh.read_text().count("claude-code") == 1


def test_detect_enable_appends_to_existing_config(
    fake_claude_home: dict[str, Any], tmp_path: Path
) -> None:
    cfg = tmp_path / "existing.toml"
    cfg.write_text('[keep]\npaths = ["~/important"]\n')
    result = runner.invoke(app, ["--config", str(cfg), "detect", "--enable"])
    assert result.exit_code == 0, result.output
    text = cfg.read_text()
    assert "[keep]" in text and "[presets]" in text and "claude-code" in text
    load(cfg)  # still valid TOML that parses into a working config


# -- preset invariants: the rules that keep a wiper from being a footgun -----


def test_every_registered_preset_is_verified() -> None:
    from neurailyzer.presets import REGISTRY

    unverified = [p.id for p in REGISTRY.values() if not p.verified]
    assert not unverified, f"unverified preset(s) shipped: {unverified}"


def test_no_preset_targets_a_bare_home_root() -> None:
    """A preset must name subtrees, never the harness root -- those roots mix
    state with credentials and, for Grok Build, the binary itself."""
    from neurailyzer.presets import REGISTRY

    roots = {"~", "~/.claude", "~/.codex", "~/.hermes", "~/.grok", "~/.scion"}
    for preset in REGISTRY.values():
        for template in (*preset.session, *preset.sandbox):
            assert template.rstrip("/") not in roots, (
                f"{preset.id} targets the bare root {template}"
            )


def test_grok_install_paths_are_protected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """bin/ is a symlink to downloads/<binary>: wiping either uninstalls Grok."""
    home = tmp_path / "home"
    grok = home / ".grok"
    (grok / "sessions" / "enc-cwd" / "sid").mkdir(parents=True)
    (grok / "sessions" / "enc-cwd" / "sid" / "chat_history.jsonl").write_text("{}\n")
    (grok / "logs").mkdir()
    (grok / "logs" / "unified.jsonl").write_text("{}\n")
    (grok / "downloads").mkdir()
    (grok / "downloads" / "grok-1.2.3").write_text("#!/bin/sh\n")
    (grok / "bin").mkdir()
    (grok / "auth.json").write_text('{"token":"KEEP"}')
    (grok / "memory").mkdir()
    (grok / "memory" / "MEMORY.md").write_text("curated\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    cfg = tmp_path / "c.toml"
    cfg.write_text(
        '[presets]\nenabled = ["grok-build"]\n'
        f'\n[snapshots]\ndir = "{(tmp_path / "snaps").as_posix()}"\n'
    )
    conf = load(cfg)
    for scope in ("session", "sandbox"):
        PathWiper(scope, conf.roots_for(scope), conf.keep).commit()

    assert not (grok / "sessions" / "enc-cwd" / "sid" / "chat_history.jsonl").exists()
    assert not (grok / "logs" / "unified.jsonl").exists()
    # the install and the credentials survive
    assert (grok / "downloads" / "grok-1.2.3").exists()
    assert (grok / "bin").is_dir()
    assert (grok / "auth.json").read_text() == '{"token":"KEEP"}'
    assert (grok / "memory" / "MEMORY.md").exists()
