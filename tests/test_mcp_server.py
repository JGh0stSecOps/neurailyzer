"""MCP surface: tools registered, gates identical to the CLI's."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp", reason="mcp extra not installed")

from neurailyzer.mcp_server import build_server  # noqa: E402


def _call(server: Any, tool: str, args: dict[str, Any]) -> Any:
    result = asyncio.run(server.call_tool(tool, args))
    assert not result.is_error, result.content
    out = result.structured_content
    # a non-dict tool return is wrapped as {"result": ...} by the SDK
    return out["result"] if isinstance(out, dict) and set(out) == {"result"} else out


def test_all_four_verbs_registered(state: dict[str, Path]) -> None:
    server = build_server(str(state["config"]))
    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert {"nl_list_state", "nl_snapshot", "nl_wipe", "nl_restore"} <= tools


def test_nl_list_state_reports_scopes(state: dict[str, Path]) -> None:
    server = build_server(str(state["config"]))
    rows = _call(server, "nl_list_state", {})
    by_scope = {r["scope"]: r for r in rows}
    assert by_scope["session"]["configured"] is True
    assert by_scope["rag"]["adapter_available"] is False


def test_nl_wipe_defaults_to_dry_run(state: dict[str, Path]) -> None:
    server = build_server(str(state["config"]))
    out = _call(server, "nl_wipe", {"scopes": ["sandbox"]})
    assert out["dry_run"] is True
    assert (state["sandbox"] / "notes.txt").exists()  # nothing changed


def test_nl_wipe_all_is_refused_over_mcp(state: dict[str, Path]) -> None:
    server = build_server(str(state["config"]))
    out = _call(server, "nl_wipe", {"scopes": ["all"], "commit": True})
    assert out["ok"] is False
    assert (state["sandbox"] / "notes.txt").exists()


def test_nl_wipe_commit_snapshots_then_wipes(state: dict[str, Path]) -> None:
    server = build_server(str(state["config"]))
    out = _call(server, "nl_wipe", {"scopes": ["sandbox"], "commit": True})
    assert out["ok"] is True
    assert out["snapshot"] is not None
    assert not (state["sandbox"] / "notes.txt").exists()
    assert (state["keep"] / "pinned.txt").exists()


def test_nl_snapshot_then_nl_restore_round_trip(state: dict[str, Path]) -> None:
    server = build_server(str(state["config"]))
    took = _call(server, "nl_snapshot", {"label": "mcp-golden"})
    assert took["ok"] is True
    (state["sandbox"] / "notes.txt").write_text("mutated")
    out = _call(server, "nl_restore", {"to": took["id"], "commit": True})
    assert out["ok"] is True
    assert (state["sandbox"] / "notes.txt").read_text() == "scratch note"


def test_nl_wipe_refuses_to_commit_remote_over_mcp(state: dict[str, Path]) -> None:
    """Local wipes are snapshot-protected; remote deletes are irreversible.

    An agent may look at the plan, but only a human commits a delete that no
    snapshot can undo.
    """
    cfg = state["config"]
    cfg.write_text(cfg.read_text() + '\n[remote.openai]\nsurfaces = ["files"]\n')
    server = build_server(str(cfg))
    out = _call(server, "nl_wipe", {"scopes": ["remote"], "commit": True})
    assert out["ok"] is False
    assert "irreversible" in out["reason"]


def test_nl_wipe_allows_planning_remote_over_mcp(state: dict[str, Path]) -> None:
    cfg = state["config"]
    cfg.write_text(cfg.read_text() + '\n[remote.openai]\nsurfaces = ["files"]\n')
    server = build_server(str(cfg))
    out = _call(server, "nl_wipe", {"scopes": ["remote"], "commit": False})
    assert out["ok"] is True
    assert out["dry_run"] is True


def test_nl_wipe_refuses_remote_hidden_inside_all(state: dict[str, Path]) -> None:
    """'all' expands to include remote -- the gate must catch that too."""
    server = build_server(str(state["config"]))
    out = _call(server, "nl_wipe", {"scopes": ["all"], "commit": True})
    assert out["ok"] is False


def test_nl_wipe_honors_the_liveness_guard(state: dict[str, Path]) -> None:
    """MCP is the surface where an agent wipes MID-SESSION -- the harness
    holding the store open is the one making the call, so this is the
    guaranteed-live case, not an edge case."""
    (state["sandbox"] / "state.db").write_bytes(b"SQLite format 3\x00")
    (state["sandbox"] / "state.db-wal").write_bytes(b"wal")
    cfg = state["config"]
    cfg.write_text(cfg.read_text() + '\n[presets]\nenabled = ["hermes"]\n')
    server = build_server(str(cfg))
    out = _call(server, "nl_wipe", {"scopes": ["sandbox"], "commit": True})
    assert out["ok"] is False
    assert "RUNNING" in out["reason"]
    assert out["liveness"]
    assert (state["sandbox"] / "notes.txt").exists()  # nothing was wiped


def test_nl_wipe_force_overrides_the_liveness_guard(state: dict[str, Path]) -> None:
    (state["sandbox"] / "state.db-wal").write_bytes(b"wal")
    cfg = state["config"]
    cfg.write_text(cfg.read_text() + '\n[presets]\nenabled = ["hermes"]\n')
    server = build_server(str(cfg))
    out = _call(server, "nl_wipe", {"scopes": ["sandbox"], "commit": True, "force": True})
    assert out["ok"] is True
    assert not (state["sandbox"] / "notes.txt").exists()
