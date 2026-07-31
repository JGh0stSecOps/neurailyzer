"""E2E smoke over a realistic multi-harness home, driving the REAL CLI.

Seeds a fake $HOME holding Claude Code, Codex, and Hermes trees complete with
their traps (auto-memory nested in the session tree, credential files, a
pairing allowlist, WAL sidecars), then runs the actual
``python -m neurailyzer`` binary end to end: detect -> enable -> list-state ->
dry-run -> wipe --commit -> restore --commit.

The assertions are the product promises: history dies, memory and credentials
live, and restore brings the tree back byte-for-byte.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


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
def harness_home(tmp_path: Path) -> dict[str, Any]:
    """A fake $HOME with three harnesses installed, traps included."""
    home = tmp_path / "home"

    claude = home / ".claude"
    proj = claude / "projects" / "-Users-me-repo"
    (proj / "memory").mkdir(parents=True)
    (proj / "memory" / "MEMORY.md").write_text("# durable index\n")
    (proj / "sess-1.jsonl").write_text('{"type":"user","text":"hello"}\n')
    (proj / "sess-1").mkdir()
    (proj / "sess-1" / "subagent.jsonl").write_text("{}\n")
    (claude / "history.jsonl").write_text('{"display":"prompt"}\n')
    (claude / "shell-snapshots").mkdir()
    (claude / "shell-snapshots" / "snap.sh").write_text("alias x=y\n")
    (claude / "settings.json").write_text('{"theme":"dark"}')
    (claude / ".credentials.json").write_text('{"token":"KEEP-ME"}')

    codex = home / ".codex"
    (codex / "sessions" / "2026" / "07" / "30").mkdir(parents=True)
    (codex / "sessions" / "2026" / "07" / "30" / "rollout-x.jsonl").write_text("{}\n")
    (codex / "history.jsonl").write_text('{"text":"hi"}\n')
    (codex / "state_5.sqlite").write_bytes(b"SQLite format 3\x00state")
    (codex / "memories_1.sqlite").write_bytes(b"SQLite format 3\x00memories")
    (codex / "auth.json").write_text('{"OPENAI_API_KEY":"KEEP-ME"}')
    (codex / "config.toml").write_text("model = 'gpt-5'\n")

    hermes = home / ".hermes"
    hermes.mkdir()
    (hermes / "state.db").write_bytes(b"SQLite format 3\x00sessions")
    (hermes / "sessions").mkdir()
    (hermes / "sessions" / "sessions.json").write_text("{}")
    (hermes / "memories").mkdir()
    (hermes / "memories" / "MEMORY.md").write_text("curated memory\n")
    (hermes / "pairing").mkdir()
    (hermes / "pairing" / "allowlist.json").write_text('["user-1"]')
    (hermes / ".env").write_text("ANTHROPIC_API_KEY=KEEP-ME\n")
    (hermes / "config.yaml").write_text("model: hermes\n")
    (hermes / "logs").mkdir()
    (hermes / "logs" / "run.log").write_text("log line\n")

    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "NO_COLOR": "1",
        "TERM": "dumb",
        "COLUMNS": "220",
        "NEURAILYZER_CONFIG": str(tmp_path / "config.toml"),
    }
    for var in ("CODEX_HOME", "HERMES_HOME", "GROK_HOME", "LOCALAPPDATA", "APPDATA"):
        env.pop(var, None)
    return {
        "home": home,
        "claude": claude,
        "codex": codex,
        "hermes": hermes,
        "proj": proj,
        "env": env,
        "config": tmp_path / "config.toml",
        "snapdir": tmp_path / "snaps",
    }


@pytest.mark.e2e
def test_detect_enable_wipe_restore(harness_home: dict[str, Any]) -> None:
    env, cfg = harness_home["env"], harness_home["config"]
    claude, codex, hermes = (
        harness_home["claude"],
        harness_home["codex"],
        harness_home["hermes"],
    )

    # 1. detect finds all three installed harnesses
    r = run_cli(env, "detect")
    assert r.returncode == 0, r.stderr
    for name in ("Claude Code", "Codex", "Hermes"):
        assert name in r.stdout

    # 2. --enable writes a config we can then use
    r = run_cli(env, "detect", "--enable")
    assert r.returncode == 0, r.stderr
    text = cfg.read_text()
    assert "claude-code" in text and "codex" in text and "hermes" in text

    # point snapshots somewhere outside the wipe targets
    cfg.write_text(
        text.rstrip("\n") + f'\n\n[snapshots]\ndir = "{harness_home["snapdir"].as_posix()}"\n'
    )

    golden = {
        "claude": tree_bytes(claude),
        "codex": tree_bytes(codex),
        "hermes": tree_bytes(hermes),
    }

    # 3. list-state sees real targets
    r = run_cli(env, "list-state")
    assert r.returncode == 0, r.stderr
    assert "ready" in r.stdout

    # 4. dry-run mutates nothing
    r = run_cli(env, "wipe", "session", "sandbox")
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout
    assert tree_bytes(claude) == golden["claude"]
    assert tree_bytes(hermes) == golden["hermes"]

    # 5. commit -- the guard fires on Hermes' WAL-less db only if sidecars
    #    exist; none here, but a `claude` process may be running in CI's
    #    parent, so --force keeps the test deterministic.
    r = run_cli(env, "wipe", "session", "sandbox", "--commit", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    snap_id = next(tok for tok in r.stdout.split() if tok.startswith("2026"))

    # history is gone ...
    assert not (harness_home["proj"] / "sess-1.jsonl").exists()
    assert not (harness_home["proj"] / "sess-1").exists()
    assert not (claude / "history.jsonl").exists()
    assert not (claude / "shell-snapshots" / "snap.sh").exists()
    assert not (codex / "history.jsonl").exists()
    assert not (codex / "state_5.sqlite").exists()
    assert not (codex / "sessions" / "2026" / "07" / "30" / "rollout-x.jsonl").exists()
    assert not (hermes / "state.db").exists()
    assert not (hermes / "logs" / "run.log").exists()

    # ... and everything that must survive, survived
    assert (harness_home["proj"] / "memory" / "MEMORY.md").read_text() == "# durable index\n"
    assert (claude / "settings.json").exists()
    assert '"KEEP-ME"' in (claude / ".credentials.json").read_text()
    assert "KEEP-ME" in (codex / "auth.json").read_text()
    assert (codex / "config.toml").exists()
    assert (codex / "memories_1.sqlite").exists()  # agent memory, not scratch
    assert (hermes / "memories" / "MEMORY.md").read_text() == "curated memory\n"
    assert (hermes / "pairing" / "allowlist.json").read_text() == '["user-1"]'
    assert "KEEP-ME" in (hermes / ".env").read_text()
    assert (hermes / "config.yaml").exists()

    # 6. restore brings the wiped state back byte-for-byte
    r = run_cli(env, "restore", "--to", snap_id, "--commit")
    assert r.returncode == 0, r.stderr + r.stdout
    for name, root in (("claude", claude), ("codex", codex), ("hermes", hermes)):
        assert tree_bytes(root) == golden[name], f"{name} not restored bit-identically"


@pytest.mark.e2e
def test_liveness_guard_blocks_a_live_store(harness_home: dict[str, Any]) -> None:
    env, cfg = harness_home["env"], harness_home["config"]
    assert run_cli(env, "detect", "--enable").returncode == 0
    cfg.write_text(
        cfg.read_text().rstrip("\n")
        + f'\n\n[snapshots]\ndir = "{harness_home["snapdir"].as_posix()}"\n'
    )
    # a WAL sidecar means a writer is live (or died mid-write)
    (harness_home["hermes"] / "state.db-wal").write_bytes(b"wal")

    r = run_cli(env, "wipe", "session", "--commit")
    assert r.returncode == 3
    assert "RUNNING" in (r.stdout + r.stderr)
    assert (harness_home["hermes"] / "state.db").exists()  # untouched


@pytest.mark.e2e
def test_remote_scope_without_tokens_is_a_reported_skip(
    harness_home: dict[str, Any],
) -> None:
    env, cfg = harness_home["env"], harness_home["config"]
    env = {k: v for k, v in env.items() if k != "OPENAI_API_KEY"}
    cfg.write_text(
        '[remote.openai]\nsurfaces = ["files"]\n'
        f'\n[snapshots]\ndir = "{harness_home["snapdir"].as_posix()}"\n'
    )
    r = run_cli(env, "wipe", "remote")
    assert r.returncode == 0, r.stderr
    assert "OPENAI_API_KEY not set" in r.stdout
    assert "NOT restorable" in r.stdout  # the irreversibility warning is loud


@pytest.mark.e2e
def test_mcp_tools_are_exposed_over_stdio(harness_home: dict[str, Any]) -> None:
    """The MCP surface answers a real initialize + tools/list handshake."""
    pytest.importorskip("mcp")
    script = (
        "import asyncio,sys,json\n"
        "from mcp import ClientSession, StdioServerParameters, stdio_client\n"
        "async def main():\n"
        "    p = StdioServerParameters(command=sys.executable,"
        " args=['-m','neurailyzer','mcp','serve'])\n"
        "    async with stdio_client(p) as (r,w):\n"
        "        async with ClientSession(r,w) as s:\n"
        "            await s.initialize()\n"
        "            t = await s.list_tools()\n"
        "            print(json.dumps(sorted(x.name for x in t.tools)))\n"
        "asyncio.run(main())\n"
    )
    r = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        env=harness_home["env"],
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == [
        "nl_list_state",
        "nl_restore",
        "nl_snapshot",
        "nl_wipe",
    ]
