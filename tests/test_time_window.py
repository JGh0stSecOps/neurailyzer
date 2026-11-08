"""Time-windowed wipes: ``--after`` forgets everything since a moment,
``--before`` is the retention cut. Judged per file by ``lstat`` mtime.

The fixture dates files by hand (``os.utime``) so the window has something
on both sides of every boundary it is asked to respect.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from neurailyzer import core
from neurailyzer.config import KeepList, load
from neurailyzer.timewindow import TimeWindow, TimeWindowError, parse_when
from neurailyzer.wipers.local import PathWiper

NOW = datetime(2026, 11, 8, 0, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=30)
NEW = NOW - timedelta(hours=1)
CUT = NOW - timedelta(days=7)


def run_cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed argv, our own package
        [sys.executable, "-m", "neurailyzer", *args],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )


def _date(p: Path, when: datetime) -> None:
    ts = when.timestamp()
    if p.is_symlink():
        if os.utime not in os.supports_follow_symlinks:
            return  # Windows: a link keeps its creation time; the test tolerates it
        os.utime(p, (ts, ts), follow_symlinks=False)
    else:
        os.utime(p, (ts, ts))


# -- parse_when ---------------------------------------------------------------


def test_parse_when_reads_iso_and_spans() -> None:
    assert parse_when("2026-11-01T04:00+00:00") == datetime(2026, 11, 1, 4, tzinfo=UTC)
    local = parse_when("2026-11-01")
    assert local.tzinfo is not None  # naive input is the local clock, made aware
    assert parse_when("7d", now=NOW) == NOW - timedelta(days=7)
    assert parse_when("12h", now=NOW) == NOW - timedelta(hours=12)
    assert parse_when("30m", now=NOW) == NOW - timedelta(minutes=30)
    assert parse_when("2w", now=NOW) == NOW - timedelta(weeks=2)
    assert parse_when(" 3D ", now=NOW) == NOW - timedelta(days=3)


@pytest.mark.parametrize("bad", ["", "   ", "yesterday", "0d", "7x", "2026-13-40", "1.5d"])
def test_parse_when_refuses_what_it_cannot_read(bad: str) -> None:
    with pytest.raises(TimeWindowError):
        parse_when(bad, now=NOW)


def test_window_refuses_empty_and_naive_bounds() -> None:
    with pytest.raises(TimeWindowError):
        TimeWindow(after=NOW, before=NOW - timedelta(days=1))
    with pytest.raises(TimeWindowError):
        TimeWindow(after=datetime(2026, 1, 1))  # naive
    assert TimeWindow().is_open
    w = TimeWindow(after=CUT)
    assert w.contains(NEW.timestamp()) and not w.contains(OLD.timestamp())
    assert w.contains(CUT.timestamp())  # at/after: the bound itself is inside
    w = TimeWindow(before=CUT)
    assert w.contains(OLD.timestamp()) and not w.contains(NEW.timestamp())
    assert not w.contains(CUT.timestamp())  # before: the bound itself is outside


# -- PathWiper ----------------------------------------------------------------


@pytest.fixture
def dated(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "sessions"
    (root / "old-dir").mkdir(parents=True)
    (root / "new-dir").mkdir()
    (root / "old-empty").mkdir()  # untouched, dated long ago: not the window's business
    old = root / "old-dir" / "old.jsonl"
    new = root / "new-dir" / "new.jsonl"
    db = root / "state_5.sqlite"
    link = root / "new-link"
    old.write_text("old")
    new.write_text("new")
    db.write_bytes(b"SQLite format 3\x00")
    link.symlink_to(tmp_path / "elsewhere")  # dangling on purpose: lstat only
    _date(old, OLD)
    _date(new, NEW)
    _date(db, NEW)
    _date(link, NEW)
    _date(root / "old-dir", OLD)
    _date(root / "new-dir", NEW)
    _date(root / "old-empty", OLD)
    return {"root": root, "old": old, "new": new, "db": db, "link": link}


def test_after_takes_only_what_was_modified_since(dated: dict[str, Path]) -> None:
    w = PathWiper("session", [dated["root"]], KeepList(), window=TimeWindow(after=CUT))
    plan = w.plan()
    assert plan.item_count >= 3  # new.jsonl, the db, the link (+ prunable dirs)
    assert any("outside the window" in n and "1 file" in n for n in plan.notes)
    assert any("per FILE" in n and "state_5.sqlite" in n for n in plan.notes)
    assert dated["old"].exists()  # plan mutates nothing
    w.commit()
    assert not dated["new"].exists()
    assert not dated["db"].exists()
    assert not dated["link"].is_symlink()
    assert dated["old"].read_text() == "old"
    assert dated["old"].parent.exists()  # a dir holding out-of-window state stays
    assert not (dated["root"] / "new-dir").exists()  # an emptied dir is pruned
    assert (dated["root"] / "old-empty").is_dir()  # an untouched empty dir is not
    assert w.verify() is True  # nothing left INSIDE the window


def test_an_undatable_entry_is_an_error_not_a_skip(
    dated: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """lstat failing must fail the plan and the verify: an undated file may be
    exactly the one the user asked to remove. It is never a quiet 'outside'."""
    real_lstat = os.lstat

    def flaky(path: Any, *a: Any, **k: Any) -> Any:
        if Path(path) == dated["new"]:
            raise OSError(5, "Input/output error")
        return real_lstat(path, *a, **k)

    monkeypatch.setattr("neurailyzer.wipers.local.os.lstat", flaky)
    w = PathWiper("session", [dated["root"]], KeepList(), window=TimeWindow(after=CUT))
    plan = w.plan()
    assert plan.complete is False
    assert any("could not date" in n and "new.jsonl" in n for n in plan.notes)
    assert w.verify() is False
    assert dated["new"].exists()  # never acted on a guess


def test_before_is_the_retention_cut(dated: dict[str, Path]) -> None:
    w = PathWiper("session", [dated["root"]], KeepList(), window=TimeWindow(before=CUT))
    w.commit()
    assert not dated["old"].exists()
    assert dated["new"].read_text() == "new"
    assert dated["db"].exists()
    assert w.verify() is True


def test_keep_list_wins_over_the_window(dated: dict[str, Path]) -> None:
    keep = KeepList(paths=(dated["new"],))
    w = PathWiper("session", [dated["root"]], keep, window=TimeWindow(after=CUT))
    plan = w.commit()
    assert dated["new"].read_text() == "new"
    assert any("keep-list skip" in n for n in plan.notes)


def test_open_window_is_no_window(dated: dict[str, Path]) -> None:
    w = PathWiper("session", [dated["root"]], KeepList(), window=TimeWindow())
    assert w.window is None
    w.commit()
    assert not dated["old"].exists() and not dated["new"].exists()


# -- core ---------------------------------------------------------------------


def test_core_refuses_a_window_on_the_remote_scope(state: dict[str, Any]) -> None:
    cfg = load(state["config"])
    with pytest.raises(core.WipeRefused):
        core.plan_wipe(cfg, ["remote"], TimeWindow(after=CUT))
    # an OPEN window on remote is fine -- it is no window
    core.plan_wipe(cfg, ["session"], TimeWindow())


# -- CLI ----------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, dated: dict[str, Path]) -> dict[str, Any]:
    config = tmp_path / "config.toml"
    config.write_text(
        f'[targets.session]\npaths = ["{dated["root"].as_posix()}"]\n\n'
        f'[snapshots]\ndir = "{(tmp_path / "snaps").as_posix()}"\n'
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "NO_COLOR": "1",
        "TERM": "dumb",
        "COLUMNS": "220",
        "NEURAILYZER_CONFIG": str(config),
        "NEURAILYZER_DISABLE_PROCESS_SCAN": "1",
    }
    return {"env": env, "config": config, **dated}


@pytest.mark.e2e
def test_cli_after_forgets_everything_since(home: dict[str, Any]) -> None:
    env = home["env"]
    cut = CUT.isoformat()
    r = run_cli(env, "list-state", "-s", "session", "--after", cut)
    assert r.returncode == 0, r.stderr
    assert "modified at/after" in r.stdout

    r = run_cli(env, "wipe", "session", "--after", cut)  # dry-run
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout and "time window" in r.stdout
    assert "outside the window" in r.stdout
    assert home["new"].exists()

    r = run_cli(env, "wipe", "session", "--after", cut, "--commit", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "verified" in r.stdout
    assert not home["new"].exists()
    assert home["old"].read_text() == "old"


@pytest.mark.e2e
def test_cli_before_is_the_retention_cut_and_restore_undoes_it(home: dict[str, Any]) -> None:
    env = home["env"]
    r = run_cli(env, "wipe", "session", "--before", CUT.isoformat(), "--commit", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    assert not home["old"].exists()
    assert home["new"].exists()
    # a windowed wipe is restored like any other: the snapshot covered the root
    r = run_cli(env, "restore", "--to", "2999-01-01T00:00+00:00", "--commit", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    assert home["old"].read_text() == "old"


@pytest.mark.e2e
def test_cli_span_form_and_refusals(home: dict[str, Any]) -> None:
    env = home["env"]
    r = run_cli(env, "wipe", "session", "--after", "7d", "--commit", "--force")
    assert r.returncode == 0, r.stderr + r.stdout
    assert not home["new"].exists() and home["old"].exists()

    r = run_cli(env, "wipe", "session", "--after", "yesterday", "--commit", "--force")
    assert r.returncode == 2
    assert "refusing" in r.stderr
    assert home["old"].exists()

    r = run_cli(env, "wipe", "remote", "--after", "7d", "--commit", "--force")
    assert r.returncode == 2
    assert "remote" in r.stderr

    r = run_cli(env, "list-state", "-s", "remote", "--after", "7d")
    assert r.returncode == 2
    assert "remote" in r.stderr


def test_mcp_wipe_accepts_a_window_and_refuses_it_on_remote(home: dict[str, Any]) -> None:
    pytest.importorskip("mcp", reason="mcp extra not installed")
    import asyncio

    from neurailyzer.mcp_server import build_server

    server = build_server(str(home["config"]))

    def call(args: dict[str, Any]) -> Any:
        result = asyncio.run(server.call_tool("nl_wipe", args))
        assert not result.is_error, result.content
        out = result.structured_content
        return out["result"] if isinstance(out, dict) and set(out) == {"result"} else out

    plan = call({"scopes": ["session"], "after": CUT.isoformat()})
    assert plan["ok"] is True and plan["window"] and "at/after" in plan["window"]
    assert any("outside the window" in n for p in plan["plans"] for n in p["notes"])
    assert call({"scopes": ["session"], "after": "nonsense"})["ok"] is False
    assert call({"scopes": ["remote"], "after": "7d"})["ok"] is False


def test_mcp_empty_bound_is_refused_not_unwindowed(home: dict[str, Any]) -> None:
    """after="" is an explicit (bad) bound, not "no bound": it must refuse,
    never fall through to a commit of the whole target."""
    pytest.importorskip("mcp", reason="mcp extra not installed")
    import asyncio

    from neurailyzer.mcp_server import build_server

    server = build_server(str(home["config"]))

    def call(args: dict[str, Any]) -> Any:
        result = asyncio.run(server.call_tool("nl_wipe", args))
        assert not result.is_error, result.content
        out = result.structured_content
        return out["result"] if isinstance(out, dict) and set(out) == {"result"} else out

    out = call({"scopes": ["session"], "after": "", "commit": True, "force": True})
    assert out["ok"] is False and "time window" in out["reason"]
    assert home["old"].exists() and home["new"].exists()  # nothing moved


def test_scope_status_counts_keep_skips_only_and_carries_completeness(
    dated: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from neurailyzer.config import Config

    cfg = Config(targets={"session": (dated["root"],)})
    st = core.scope_status(cfg, "session", TimeWindow(after=CUT))
    assert st.kept_count == 0  # the window note is not a keep skip
    assert st.complete is True

    real_lstat = os.lstat

    def flaky(path: Any, *a: Any, **k: Any) -> Any:
        if Path(path) == dated["new"]:
            raise OSError(5, "Input/output error")
        return real_lstat(path, *a, **k)

    monkeypatch.setattr("neurailyzer.wipers.local.os.lstat", flaky)
    st = core.scope_status(cfg, "session", TimeWindow(after=CUT))
    assert st.complete is False


@pytest.mark.e2e
def test_cli_list_state_shows_incomplete_when_a_file_cannot_be_read(home: dict[str, Any]) -> None:
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("needs an unreadable directory, which root and Windows ACLs do not give")
    locked = home["root"] / "locked"
    locked.mkdir()
    (locked / "x.jsonl").write_text("x")
    locked.chmod(0o000)
    try:
        r = run_cli(home["env"], "list-state", "-s", "session")
        assert r.returncode == 0, r.stderr
        assert "incomplete" in r.stdout and "ready" not in r.stdout
    finally:
        locked.chmod(0o700)
