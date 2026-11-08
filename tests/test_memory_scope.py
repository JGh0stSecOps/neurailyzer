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


def test_memory_is_its_own_scope_in_config(home: dict[str, Any], monkeypatch: Any) -> None:
    monkeypatch.setenv("HOME", str(home["home"]))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
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

    monkeypatch.setenv("HOME", str(home["home"]))
    monkeypatch.setenv("NEURAILYZER_DISABLE_PROCESS_SCAN", "1")
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    server = build_server(str(home["config"]))

    def call(args: dict[str, Any]) -> Any:
        result = asyncio.run(server.call_tool("nl_wipe", args))
        assert not result.is_error, result.content
        out = result.structured_content
        return out["result"] if isinstance(out, dict) and set(out) == {"result"} else out

    plan = call({"scopes": ["memory"]})
    assert plan["ok"] is True and plan["dry_run"] is True
    assert any(p["scope"] == "memory" and p["items"] > 0 for p in plan["plans"])

    refused = call({"scopes": ["memory"], "commit": True, "force": True})
    assert refused["ok"] is False
    assert "memory" in refused["reason"] and "CLI" in refused["reason"]
    assert (home["proj"] / "memory" / "fact.md").exists()  # refused means untouched
