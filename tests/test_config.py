"""Config parsing, validation, and the keep-list contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from neurailyzer.config import ConfigError, KeepList, load


def test_missing_default_config_is_safe_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEURAILYZER_CONFIG", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/nonexistent-home")))
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
