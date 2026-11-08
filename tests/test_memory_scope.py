"""The ``memory`` scope: the neuralizer lever.

A harness's durable memory is kept by every scope but its own. ``wipe
session`` leaves it; ``wipe memory`` (and ``wipe all``) takes it; restore
brings it back byte-for-byte. All of it drives the REAL CLI as a subprocess
over a fake $HOME holding Claude Code and Codex trees.

Every "survived" assertion here is non-vacuous by construction: a sibling in
the same wipe target is asserted gone first, so the wiper demonstrably walked
the tree and chose.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from neurailyzer.config import MEMORY_SCOPE, load

_SNAP_ID = re.compile(r"\b\d{8}T\d{6}Z-[0-9a-f]{4}-[\w.-]+")


def run_cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed argv, our own package
        [sys.executable, "-m", "neurailyzer", *args],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


@pytest.fixture
def home(tmp_path: Path) -> dict[str, Any]:
    h = tmp_path / "home"
    claude = h / ".claude"
    proj = claude / "projects" / "-Users-me-repo"
    (proj / "memory").mkdir(parents=True)
    (proj / "memory" / "MEMORY.md").write_text("# durable index\n")
    (proj / "memory" / "fact.md").write_text("a learned fact\n")
    (proj / "sess-1.jsonl").write_text('{"type":"user","text":"hello"}\n')
    (claude / "history.jsonl").write_text('{"display":"prompt"}\n')
    (claude / "settings.json").write_text('{"theme":"dark"}')
    (claude / ".credentials.json").write_text('{"token":"KEEP-ME"}')

    codex = h / ".codex"
    (codex / "sessions").mkdir(parents=True)
    (codex / "sessions" / "rollout.jsonl").write_text("{}\n")
    (codex / "memories_1.sqlite").write_bytes(b"SQLite format 3\x00memories")
    (codex / "memories_1.sqlite-wal").write_bytes(b"wal")
    (codex / "auth.json").write_text('{"token":"KEEP-ME"}')
    (codex / "config.toml").write_text("model = 'x'\n")

    config = tmp_path / "config.toml"
    config.write_text(
        '[presets]\nenabled = ["claude-code", "codex"]\n\n'
        f'[snapshots]\ndir = "{(tmp_path / "snaps").as_posix()}"\n'
    )
    env = {
        **os.environ,
        "HOME": str(h),
        "USERPROFILE": str(h),
        "NO_COLOR": "1",
        "TERM": "dumb",
        "COLUMNS": "220",
        "NEURAILYZER_CONFIG": str(config),
        "NEURAILYZER_DISABLE_PROCESS_SCAN": "1",
    }
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME", "GROK_HOME"):
        env.pop(var, None)
    return {"home": h, "claude": claude, "codex": codex, "proj": proj, "env": env, "config": config}


def _point_home(monkeypatch: Any, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows: ~ reads this one
    monkeypatch.setenv("NEURAILYZER_DISABLE_PROCESS_SCAN", "1")
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)


def test_memory_is_its_own_scope_in_config(home: dict[str, Any], monkeypatch: Any) -> None:
    _point_home(monkeypatch, home["home"])
    cfg = load(home["config"])
    mem_roots = {p.resolve() for p in cfg.roots_for(MEMORY_SCOPE)}
    assert (home["proj"] / "memory").resolve() in mem_roots
    assert (home["codex"] / "memories_1.sqlite").resolve() in mem_roots
    fact = home["proj"] / "memory" / "fact.md"
    # the session wiper's keep-list protects memory ...
    assert cfg.keep_for("session").protects(fact)
    assert cfg.keep_for("sandbox").protects(fact)
    # ... the memory wiper's does not, and neither protects credentials any less
    assert not cfg.keep_for(MEMORY_SCOPE).protects(fact)
    assert cfg.keep_for(MEMORY_SCOPE).protects(home["claude"] / ".credentials.json")
    assert cfg.keep_for(MEMORY_SCOPE).protects(home["codex"] / "auth.json")
    # an unknown scope fails closed to the full list
    assert cfg.keep_for("something-new").protects(fact)


@pytest.mark.e2e
def test_session_wipe_keeps_memory(home: dict[str, Any]) -> None:
    env, proj, codex = home["env"], home["proj"], home["codex"]
    r = run_cli(env, "wipe", "session", "--commit", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    assert not (proj / "sess-1.jsonl").exists()  # sibling gone: the walk reached here
    assert (proj / "memory" / "fact.md").read_text() == "a learned fact\n"
    assert not (codex / "sessions" / "rollout.jsonl").exists()
    assert (codex / "memories_1.sqlite").exists()
    assert "keep-list skip" in r.stdout and "memory" in r.stdout


@pytest.mark.e2e
def test_memory_wipe_takes_memory_and_nothing_else(home: dict[str, Any]) -> None:
    env, proj, claude, codex = home["env"], home["proj"], home["claude"], home["codex"]
    r = run_cli(env, "list-state", "-s", "memory")
    assert r.returncode == 0, r.stderr
    assert "memory" in r.stdout and "ready" in r.stdout

    r = run_cli(env, "wipe", "memory")  # dry-run
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout
    assert (proj / "memory" / "fact.md").exists()

    r = run_cli(env, "wipe", "memory", "--commit", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    assert not (proj / "memory" / "fact.md").exists()
    assert not (proj / "memory" / "MEMORY.md").exists()
    assert not (codex / "memories_1.sqlite").exists()
    assert not (codex / "memories_1.sqlite-wal").exists()
    # transcripts are NOT the memory scope's business
    assert (proj / "sess-1.jsonl").exists()
    assert (codex / "sessions" / "rollout.jsonl").exists()
    # and credentials/settings hold under every scope
    assert '"KEEP-ME"' in (claude / ".credentials.json").read_text()
    assert (claude / "settings.json").exists()
    assert "KEEP-ME" in (codex / "auth.json").read_text()
    assert (codex / "config.toml").exists()


@pytest.mark.e2e
def test_wipe_all_forgets_everything_and_restore_brings_it_back(home: dict[str, Any]) -> None:
    """The neuralizer: one command, every scope including memory, reversible."""
    env, proj, claude, codex = home["env"], home["proj"], home["claude"], home["codex"]
    golden = {"claude": tree_bytes(claude), "codex": tree_bytes(codex)}

    r = run_cli(env, "wipe", "all", "--commit", "--force")
    assert r.returncode == 2  # no --confirm all: refused, untouched
    assert (proj / "memory" / "fact.md").exists()

    r = run_cli(env, "wipe", "all", "--commit", "--confirm", "all", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    snap_id = _SNAP_ID.findall(r.stdout)[0]
    assert not (proj / "sess-1.jsonl").exists()
    assert not (proj / "memory" / "fact.md").exists()
    assert not (codex / "memories_1.sqlite").exists()
    assert not (codex / "sessions" / "rollout.jsonl").exists()
    assert '"KEEP-ME"' in (claude / ".credentials.json").read_text()
    assert "KEEP-ME" in (codex / "auth.json").read_text()

    r = run_cli(env, "restore", "--to", snap_id, "--commit", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    assert tree_bytes(claude) == golden["claude"], "memory did not come back"
    assert tree_bytes(codex) == golden["codex"]


def test_mcp_plans_memory_but_refuses_to_commit_it(home: dict[str, Any], monkeypatch: Any) -> None:
    pytest.importorskip("mcp", reason="mcp extra not installed")
    import asyncio

    from neurailyzer.mcp_server import build_server

    _point_home(monkeypatch, home["home"])
    server = build_server(str(home["config"]))

    def call(tool: str, args: dict[str, Any]) -> Any:
        result = asyncio.run(server.call_tool(tool, args))
        assert not result.is_error, result.content
        out = result.structured_content
        return out["result"] if isinstance(out, dict) and set(out) == {"result"} else out

    plan = call("nl_wipe", {"scopes": ["memory"]})
    assert plan["ok"] is True and plan["dry_run"] is True
    assert any(p["scope"] == "memory" and p["items"] > 0 for p in plan["plans"])

    refused = call("nl_wipe", {"scopes": ["memory"], "commit": True, "force": True})
    assert refused["ok"] is False
    assert "memory" in refused["reason"] and "CLI" in refused["reason"]
    fact = home["proj"] / "memory" / "fact.md"
    assert fact.exists()  # refused means untouched

    # the other direction: a human took a memory-scoped restore point at the
    # CLI, then memory changed; an agent restoring over MCP must not roll it back
    r = run_cli(home["env"], "wipe", "memory", "--commit", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    snap_id = _SNAP_ID.findall(r.stdout)[0]
    fact.parent.mkdir(exist_ok=True)
    fact.write_text("written after the snapshot\n")
    out = call("nl_restore", {"to": snap_id, "commit": True, "force": True})
    assert out["ok"] is True
    assert fact.read_text() == "written after the snapshot\n"
    assert str(fact) in out["keep_list_skipped"]


@pytest.mark.e2e
def test_session_restore_never_rolls_memory_back(home: dict[str, Any]) -> None:
    """A `wipe session` restore point holds memory bytes too (the snapshot
    covers the whole root). Restoring it must not undo memory the agent wrote
    since: the user asked for transcripts back, not for memory to time-travel."""
    env, proj = home["env"], home["proj"]
    fact = proj / "memory" / "fact.md"
    r = run_cli(env, "wipe", "session", "--commit", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    snap_id = _SNAP_ID.findall(r.stdout)[0]
    fact.write_text("learned after the wipe\n")
    (proj / "memory" / "newer.md").write_text("also after\n")

    r = run_cli(env, "restore", "--to", snap_id, "--commit", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    assert (proj / "sess-1.jsonl").exists()  # the transcript came back ...
    assert fact.read_text() == "learned after the wipe\n"  # ... memory did not move
    assert (proj / "memory" / "newer.md").exists()


@pytest.mark.e2e
def test_symlinked_memory_dir_is_skipped_not_followed(home: dict[str, Any]) -> None:
    """projects/p/memory -> ~/.claude would make `wipe memory` a wipe of the
    whole harness. A memory target that is a link below the harness root is
    skipped and reported; `wipe memory` then touches nothing it points at."""
    env, claude = home["env"], home["claude"]
    trap = claude / "projects" / "-trap"
    trap.mkdir()
    (trap / "memory").symlink_to(claude, target_is_directory=True)
    (trap / "sess.jsonl").write_text("transcript\n")

    r = run_cli(env, "wipe", "memory", "--commit", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "skipped" in r.stdout and "symlink" in r.stdout
    assert (trap / "sess.jsonl").exists()  # not that scope's business
    assert (claude / "history.jsonl").exists()  # nothing behind the link moved
    assert (trap / "memory").is_symlink()
    assert not (home["proj"] / "memory" / "fact.md").exists()  # real memory still wiped


def test_library_config_without_hard_keep_keeps_everything_for_memory(tmp_path: Path) -> None:
    """A hand-built Config (library use) that supplies `keep` but no
    `hard_keep` must hand the memory wiper the caller's keep-list, not an
    empty one -- a caller-protected credential inside a memory target holds."""
    from neurailyzer.config import Config, KeepList

    secret = tmp_path / "memory" / "secret"
    cfg = Config(keep=KeepList(paths=(secret,)), targets={"memory": (secret.parent,)})
    assert cfg.keep_for("memory").protects(secret)
    assert cfg.keep_for("session").protects(secret)


@pytest.mark.e2e
def test_stow_managed_root_does_not_false_skip_memory(home: dict[str, Any], tmp_path: Path) -> None:
    """`~/.claude -> $HOME/real` with CLAUDE_CONFIG_DIR=$HOME/real: both are
    the harness ROOT. The redirect guard must stop at the root through either
    spelling, not read the root's own link as a redirect below it."""
    if sys.platform == "win32":
        pytest.skip("directory symlinks need a privilege on Windows runners")
    real = home["home"] / "real"
    (home["claude"]).rename(real)
    home["claude"].symlink_to(real, target_is_directory=True)
    env = {**home["env"], "CLAUDE_CONFIG_DIR": str(real)}
    fact = real / "projects" / "-Users-me-repo" / "memory" / "fact.md"
    assert fact.exists()

    r = run_cli(env, "wipe", "memory", "--commit", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "memory dir" not in r.stdout  # no false "skipped: memory dir ... is a symlink"
    assert not fact.exists()
    assert (real / "projects" / "-Users-me-repo" / "sess-1.jsonl").exists()
    assert '"KEEP-ME"' in (real / ".credentials.json").read_text()


@pytest.mark.e2e
def test_home_with_glob_characters_never_selects_a_sibling_tree(tmp_path: Path) -> None:
    """HOME=/x/home[1]: the preset's `~/.claude/projects/*/memory` glob must
    expand `~` as a LITERAL, or `[1]` becomes pattern syntax and the wipe
    selects /x/home1 -- somebody else's memory."""
    victim = tmp_path / "home1"
    me = tmp_path / "home[1]"
    for h in (victim, me):
        (h / ".claude" / "projects" / "p" / "memory").mkdir(parents=True)
        (h / ".claude" / "projects" / "p" / "memory" / "fact.md").write_text(h.name)
    config = tmp_path / "c.toml"
    config.write_text(
        '[presets]\nenabled = ["claude-code"]\n\n'
        f'[snapshots]\ndir = "{(tmp_path / "snaps").as_posix()}"\n'
    )
    env = {
        **os.environ,
        "HOME": str(me),
        "USERPROFILE": str(me),
        "NO_COLOR": "1",
        "TERM": "dumb",
        "COLUMNS": "220",
        "NEURAILYZER_CONFIG": str(config),
        "NEURAILYZER_DISABLE_PROCESS_SCAN": "1",
    }
    env.pop("CLAUDE_CONFIG_DIR", None)
    r = run_cli(env, "wipe", "memory", "--commit", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    assert not (me / ".claude" / "projects" / "p" / "memory" / "fact.md").exists()
    assert (victim / ".claude" / "projects" / "p" / "memory" / "fact.md").read_text() == "home1"


@pytest.mark.e2e
def test_symlink_alias_does_not_shadow_the_real_memory_dir(home: dict[str, Any]) -> None:
    """projects/-a/memory -> ../-z/memory sorts before the real dir. If the
    alias wins deduplication, the redirect guard skips it and the real memory
    is silently never wiped. The non-symlink spelling must win."""
    if sys.platform == "win32":
        pytest.skip("directory symlinks need a privilege on Windows runners")
    claude = home["claude"]
    real = claude / "projects" / "-z" / "memory"
    real.mkdir(parents=True)
    (real / "fact.md").write_text("real\n")
    alias_parent = claude / "projects" / "-a"
    alias_parent.mkdir()
    (alias_parent / "memory").symlink_to(Path("..") / "-z" / "memory", target_is_directory=True)

    r = run_cli(home["env"], "wipe", "memory", "--commit", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    assert not (real / "fact.md").exists(), "the alias shadowed the real memory dir"
    assert (alias_parent / "memory").is_symlink()  # the link itself is untouched
