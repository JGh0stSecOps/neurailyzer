"""Config parsing, validation, and the keep-list contract."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from neurailyzer.config import ConfigError, KeepList, load


def test_missing_default_config_is_safe_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("NEURAILYZER_CONFIG", raising=False)
    # `~` expands through $HOME / %USERPROFILE%, not Path.home(): patch both,
    # or a developer with a real ~/.neurailyzer/config.toml fails this test
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cfg = load()
    assert cfg.targets == {}
    assert cfg.source is None


def test_missing_explicit_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "nope.toml")


def test_load_full_config(state: dict[str, Path]) -> None:
    cfg = load(state["config"])
    assert cfg.roots_for("session") == (state["session"],)
    assert cfg.roots_for("sandbox") == (state["sandbox"],)
    assert cfg.retention == 5
    assert cfg.snapshot_dir == state["snapdir"]
    assert cfg.source == state["config"].resolve()


def test_snapshot_dir_always_on_keep_list(state: dict[str, Path]) -> None:
    cfg = load(state["config"])
    assert cfg.keep.protects(state["snapdir"])
    assert cfg.keep.protects(state["snapdir"] / "manifests" / "x.json")


def test_invalid_toml_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("this is [not toml")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load(bad)


def test_unknown_scope_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('[targets.everything]\npaths = ["/tmp/x"]\n')
    with pytest.raises(ConfigError, match="not a known scope"):
        load(cfg)


def test_pending_scope_rejected_with_honest_message(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('[targets.rag]\npaths = ["/tmp/x"]\n')
    with pytest.raises(ConfigError, match="no adapter in this release"):
        load(cfg)


@pytest.mark.parametrize("target", ["/", "~"])
def test_dangerous_targets_refused(tmp_path: Path, target: str) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(f"[targets.sandbox]\npaths = [{target!r}]\n")
    with pytest.raises(ConfigError, match="refusing target"):
        load(cfg)


def test_target_containing_snapshot_store_refused(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f"[targets.sandbox]\npaths = [{str(tmp_path)!r}]\n"
        f"[snapshots]\ndir = {str(tmp_path / 'snaps')!r}\n"
    )
    with pytest.raises(ConfigError, match="snapshot"):
        load(cfg)


def test_retention_must_be_positive_int(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text("[snapshots]\nretention = 0\n")
    with pytest.raises(ConfigError, match="retention"):
        load(cfg)


def test_keep_list_prefix_is_path_aware(tmp_path: Path) -> None:
    # /a/b protects /a/b/c but NOT /a/bc — string prefixes would get this wrong
    keep = KeepList(paths=(tmp_path / "a" / "b",))
    assert keep.protects(tmp_path / "a" / "b")
    assert keep.protects(tmp_path / "a" / "b" / "c.txt")
    assert not keep.protects(tmp_path / "a" / "bc")
    assert not keep.protects(tmp_path / "a")
    # shelters: an ancestor of a kept path can't be removed
    assert keep.shelters(tmp_path / "a")
    assert not keep.shelters(tmp_path / "z")


def test_env_var_discovery(state: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEURAILYZER_CONFIG", str(state["config"]))
    cfg = load()
    assert cfg.roots_for("sandbox") == (state["sandbox"],)


def test_explicit_target_linking_to_its_own_ancestor_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`[targets.X] paths = [".../projects/p/memory"]` where that path is a
    symlink to `.../.claude` would make the wipe root the whole harness dir,
    credentials included. A link to an ancestor is never a dotfiles layout;
    it is "wipe my parent tree", so config load refuses it for every scope."""
    if sys.platform == "win32":
        pytest.skip("directory symlinks need a privilege on Windows runners")
    harness = tmp_path / ".claude"
    (harness / "projects" / "p").mkdir(parents=True)
    (harness / ".credentials.json").write_text("secret")
    link = harness / "projects" / "p" / "memory"
    link.symlink_to(harness, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    spellings = (
        link.as_posix(),  # absolute
        ".claude/projects/p/memory",  # relative to cwd
        ".claude/projects/p/../p/memory",  # with a `..` segment
    )
    for scope in ("memory", "session"):
        for spelled in spellings:
            cfg = tmp_path / f"{scope}.toml"
            cfg.write_text(
                f'[targets.{scope}]\npaths = ["{spelled}"]\n\n'
                f'[snapshots]\ndir = "{(tmp_path / "snaps").as_posix()}"\n'
            )
            with pytest.raises(ConfigError, match="ancestor"):
                load(cfg)
    assert (harness / ".credentials.json").exists()
