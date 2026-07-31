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
