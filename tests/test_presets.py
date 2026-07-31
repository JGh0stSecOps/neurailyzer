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


def test_grok_worktrees_are_never_wiped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent worktrees can hold uncommitted work -- the same rule as Scion."""
    home = tmp_path / "home"
    grok = home / ".grok"
    (grok / "sessions" / "s").mkdir(parents=True)
    (grok / "sessions" / "s" / "chat_history.jsonl").write_text("{}\n")
    (grok / "worktrees" / "repo" / "sess").mkdir(parents=True)
    (grok / "worktrees" / "repo" / "sess" / "unmerged.py").write_text("work in progress")
    (grok / "worktrees.db").write_bytes(b"SQLite format 3\x00")
    (grok / "upload_queue").mkdir()
    (grok / "upload_queue" / "pending.json").write_text("{}")
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

    assert not (grok / "sessions" / "s" / "chat_history.jsonl").exists()
    assert (grok / "worktrees" / "repo" / "sess" / "unmerged.py").read_text() == "work in progress"
    assert (grok / "worktrees.db").exists()
    assert (grok / "upload_queue" / "pending.json").exists()


def test_opencode_credential_bearing_db_is_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opencode.db is BOTH the session store and a credential store.

    File-level deletion would clear chats and take provider credentials with
    them, so this release protects the DB and wipes only what it can wipe
    safely. If this test ever fails, the wiper has become a credential
    shredder.
    """
    home = tmp_path / "home"
    data = home / ".local" / "share" / "opencode"
    state = home / ".local" / "state" / "opencode"
    (data / "storage" / "session_x").mkdir(parents=True)
    (data / "storage" / "session_x" / "msg.json").write_text("{}")
    (data / "project").mkdir()
    (data / "project" / "legacy.json").write_text("{}")
    (data / "opencode.db").write_bytes(b"SQLite format 3\x00credentials+sessions")
    (data / "opencode.db-wal").write_bytes(b"wal")
    (data / "opencode-nightly.db").write_bytes(b"SQLite format 3\x00channel build")
    (data / "auth.json").write_text('{"anthropic":"KEEP"}')
    (data / "mcp-auth.json").write_text('{"server":"KEEP"}')
    (data / "worktree" / "proj").mkdir(parents=True)
    (data / "worktree" / "proj" / "wip.py").write_text("uncommitted")
    state.mkdir(parents=True)
    (state / "password").write_text("daemon-secret")
    (home / ".cache" / "opencode").mkdir(parents=True)
    (home / ".cache" / "opencode" / "blob").write_text("cached")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    cfg = tmp_path / "c.toml"
    cfg.write_text(
        '[presets]\nenabled = ["opencode"]\n'
        f'\n[snapshots]\ndir = "{(tmp_path / "snaps").as_posix()}"\n'
    )
    conf = load(cfg)
    for scope in ("session", "sandbox"):
        PathWiper(scope, conf.roots_for(scope), conf.keep).commit()

    # legacy JSON session trees and caches go
    assert not (data / "storage" / "session_x" / "msg.json").exists()
    assert not (data / "project" / "legacy.json").exists()
    assert not (home / ".cache" / "opencode" / "blob").exists()
    # every credential-bearing or work-bearing path survives
    assert (data / "opencode.db").exists(), "credential-bearing DB was deleted"
    assert (data / "opencode.db-wal").exists()
    assert (data / "opencode-nightly.db").exists(), "channel DB glob failed"
    assert (data / "auth.json").read_text() == '{"anthropic":"KEEP"}'
    assert (data / "mcp-auth.json").exists()
    assert (data / "worktree" / "proj" / "wip.py").read_text() == "uncommitted"
    assert (state / "password").read_text() == "daemon-secret"


def test_claude_config_dir_relocation_is_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE_CONFIG_DIR moves the whole tree; a preset that only knew
    ~/.claude would silently see -- and protect -- nothing."""
    home = tmp_path / "home"
    home.mkdir()
    relocated = tmp_path / "elsewhere" / "claude"
    proj = relocated / "projects" / "-p"
    (proj / "memory").mkdir(parents=True)
    (proj / "memory" / "MEMORY.md").write_text("DURABLE")
    (proj / "sess.jsonl").write_text("history")
    (relocated / "settings.json").write_text("{}")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(relocated))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(
        '[presets]\nenabled = ["claude-code"]\n'
        f'\n[snapshots]\ndir = "{(tmp_path / "snaps").as_posix()}"\n'
    )
    conf = load(cfg_file)
    roots = {p.resolve() for p in conf.roots_for("session")}
    assert (relocated / "projects").resolve() in roots, "relocated tree not detected"

    PathWiper("session", conf.roots_for("session"), conf.keep).commit()
    assert (proj / "memory" / "MEMORY.md").read_text() == "DURABLE"
    assert not (proj / "sess.jsonl").exists()
    assert (relocated / "settings.json").exists()


def test_a_harness_dir_linked_elsewhere_is_skipped_not_emptied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~/.claude/downloads -> ~/src/myrepo` must not empty the repo.

    A pre-release verifier destroyed a real git checkout this way: the link
    lives inside the harness dir, so it looked like the harness's own scratch.
    """
    home = tmp_path / "home"
    claude = home / ".claude"
    (claude / "shell-snapshots").mkdir(parents=True)
    (claude / "shell-snapshots" / "snap.sh").write_text("real scratch")
    repo = home / "src" / "myrepo"
    (repo / ".git").mkdir(parents=True)
    (repo / "main.py").write_text("real work")
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main")
    try:
        (claude / "downloads").symlink_to(repo, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(
        '[presets]\nenabled = ["claude-code"]\n'
        f'\n[snapshots]\ndir = "{(tmp_path / "snaps").as_posix()}"\n'
    )
    conf = load(cfg_file)
    assert conf.escaped_targets, "the redirected target was not flagged"

    PathWiper("sandbox", conf.roots_for("sandbox"), conf.keep).commit()
    assert (repo / "main.py").read_text() == "real work"
    assert (repo / ".git" / "HEAD").exists()
    # and the harness's genuine scratch was still wiped, so this is not a
    # test of the wiper simply doing nothing
    assert not (claude / "shell-snapshots" / "snap.sh").exists()


def test_a_symlinked_harness_root_is_still_wiped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stow/chezmoi keep ~/.claude in a dotfiles repo. That redirection is
    legitimate and must NOT be mistaken for an escaped target."""
    home = tmp_path / "home"
    home.mkdir()
    real = tmp_path / "dotfiles" / "claude"
    (real / "shell-snapshots").mkdir(parents=True)
    (real / "shell-snapshots" / "snap.sh").write_text("scratch")
    try:
        (home / ".claude").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(
        '[presets]\nenabled = ["claude-code"]\n'
        f'\n[snapshots]\ndir = "{(tmp_path / "snaps").as_posix()}"\n'
    )
    conf = load(cfg_file)
    assert not conf.escaped_targets, "a stow-managed harness root was refused"
    PathWiper("sandbox", conf.roots_for("sandbox"), conf.keep).commit()
    assert not (real / "shell-snapshots" / "snap.sh").exists()
