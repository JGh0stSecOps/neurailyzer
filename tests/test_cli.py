"""The CLI safety gate holds."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from neurailyzer.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "neurailyzer" in result.stdout


def test_wipe_is_dry_run_without_commit(state: dict[str, Path]) -> None:
    result = runner.invoke(app, ["--config", str(state["config"]), "wipe", "session"])
    assert result.exit_code == 0
    assert "DRY-RUN" in result.stdout
    assert "COMMIT" not in result.stdout
    assert (state["session"] / "chat.db").exists()  # nothing changed


def test_wipe_all_commit_is_refused() -> None:
    # a full factory reset must not proceed without the confirmation token
    result = runner.invoke(app, ["wipe", "all", "--commit"])
    assert result.exit_code == 2


def test_wipe_all_commit_with_token_proceeds(state: dict[str, Path]) -> None:
    result = runner.invoke(
        app,
        ["--config", str(state["config"]), "wipe", "all", "--commit", "--confirm", "all"],
    )
    assert result.exit_code == 0
    assert not (state["session"] / "chat.db").exists()


def test_wipe_commit_takes_snapshot_and_verifies(state: dict[str, Path]) -> None:
    result = runner.invoke(
        app, ["--config", str(state["config"]), "wipe", "session", "sandbox", "--commit"]
    )
    assert result.exit_code == 0
    assert "snapshot" in result.stdout
    assert "verified" in result.stdout
    assert (state["snapdir"] / "manifests").is_dir()


def test_wipe_no_snapshot_shouts(state: dict[str, Path]) -> None:
    result = runner.invoke(
        app,
        ["--config", str(state["config"]), "wipe", "sandbox", "--commit", "--no-snapshot"],
    )
    assert result.exit_code == 0
    assert "NO restore point" in result.stdout


def test_list_state_runs(state: dict[str, Path]) -> None:
    result = runner.invoke(app, ["--config", str(state["config"]), "list-state"])
    assert result.exit_code == 0
    assert "session" in result.stdout


def test_restore_defaults_to_dry_run(state: dict[str, Path]) -> None:
    take = runner.invoke(app, ["--config", str(state["config"]), "snapshot", "-l", "t"])
    assert take.exit_code == 0
    (state["sandbox"] / "junk.txt").write_text("junk")
    result = runner.invoke(
        app, ["--config", str(state["config"]), "restore", "--to", "9999-01-01T00:00"]
    )
    assert result.exit_code == 0
    assert "DRY-RUN" in result.stdout
    assert (state["sandbox"] / "junk.txt").exists()  # dry-run touched nothing


def test_restore_with_no_snapshot_fails_clearly(state: dict[str, Path]) -> None:
    result = runner.invoke(
        app, ["--config", str(state["config"]), "restore", "--to", "2020-01-01T00:00"]
    )
    assert result.exit_code == 1


def test_bad_config_is_exit_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text('[targets.rag]\npaths = ["/x"]\n')
    result = runner.invoke(app, ["--config", str(bad), "list-state"])
    assert result.exit_code == 2


def test_snapshot_list_empty(state: dict[str, Path]) -> None:
    result = runner.invoke(app, ["--config", str(state["config"]), "snapshot", "--list"])
    assert result.exit_code == 0
    assert "no snapshots yet" in result.stdout
