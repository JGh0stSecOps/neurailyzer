"""The CLI safety gate holds."""

from typer.testing import CliRunner

from neurailyzer.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "neurailyzer" in result.stdout


def test_wipe_is_dry_run_without_commit() -> None:
    result = runner.invoke(app, ["wipe", "session"])
    assert result.exit_code == 0
    assert "DRY-RUN" in result.stdout
    assert "COMMIT" not in result.stdout


def test_wipe_all_commit_is_refused() -> None:
    # a full factory reset must not proceed without the confirmation token
    result = runner.invoke(app, ["wipe", "all", "--commit"])
    assert result.exit_code == 2


def test_list_state_runs() -> None:
    result = runner.invoke(app, ["list-state"])
    assert result.exit_code == 0


def test_restore_defaults_to_dry_run() -> None:
    result = runner.invoke(app, ["restore", "--to", "2026-07-09T04:00"])
    assert result.exit_code == 0
    assert "DRY-RUN" in result.stdout
