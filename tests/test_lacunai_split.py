"""The model-side migration refuses execution without affecting state hygiene."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from neurailyzer.cli import Scope, app

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--help"],
        ["run", "--enable-experimental", "--apply", "--in-place"],
        ["verify", "--model", "private-invocation-marker"],
    ],
)
def test_experimental_shim_refuses_without_forwarding(
    arguments: list[str], state: dict[str, Path]
) -> None:
    before = {
        p.relative_to(state["root"]): p.read_bytes()
        for p in state["root"].rglob("*")
        if p.is_file() and not p.is_symlink()
    }
    result = runner.invoke(app, ["--config", str(state["config"]), "experimental", *arguments])
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    notice = json.loads(result.stderr)
    assert notice["ok"] is False
    assert notice["status"] == "deprecated"
    assert notice["error"] == "weight_editing_moved"
    assert notice["replacement"] == "lacunai"
    assert notice["help_command"] == "lacunai --help"
    assert notice["removed_in"] == "0.2.0"
    assert notice["forwarded"] is False
    assert "private-invocation-marker" not in result.output
    after = {
        p.relative_to(state["root"]): p.read_bytes()
        for p in state["root"].rglob("*")
        if p.is_file() and not p.is_symlink()
    }
    assert after == before
    assert not state["snapdir"].exists()


BLOCK_OPTIONAL_IMPORTS = """
import importlib.abc
import sys

class BlockModelDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'lacunai', 'torch', 'transformers'}:
            raise AssertionError('forbidden model-side import: ' + fullname)
        return None

sys.meta_path.insert(0, BlockModelDependencies())
import neurailyzer.core
import neurailyzer.config
from neurailyzer.cli import app
app()
"""


@pytest.mark.parametrize(
    ("arguments", "expected_code", "expected_output"),
    [
        (["list-state", "--scope", "session"], 0, "session"),
        (["wipe", "session"], 0, "DRY-RUN"),
        (["experimental", "run", "--apply"], 2, '"weight_editing_moved"'),
    ],
)
def test_state_cli_works_with_model_imports_blocked(
    arguments: list[str], expected_code: int, expected_output: str, state: dict[str, Path]
) -> None:
    source = state["session"] / "chat.db"
    before = source.read_bytes()
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            BLOCK_OPTIONAL_IMPORTS,
            "--config",
            str(state["config"]),
            *arguments,
        ],
        cwd=state["root"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == expected_code, result.stdout + result.stderr
    assert expected_output in result.stdout + result.stderr
    assert "forbidden model-side import" not in result.stderr
    assert source.read_bytes() == before
    assert not state["snapdir"].exists()


@pytest.mark.parametrize("scope", ["trace", "weights"])
def test_model_side_scopes_are_rejected(scope: str, state: dict[str, Path]) -> None:
    assert scope not in {value.value for value in Scope}
    result = runner.invoke(app, ["--config", str(state["config"]), "wipe", scope])
    assert result.exit_code == 2
    assert "Invalid value" in result.output
    assert (state["session"] / "chat.db").exists()


def test_package_metadata_has_no_lacunai_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    dependencies = list(project["dependencies"])
    for requirements in project["optional-dependencies"].values():
        dependencies.extend(requirements)
    assert all("lacunai" not in dependency.casefold() for dependency in dependencies)
    assert "distill" not in project["description"].casefold()
    assert project["authors"] == [{"name": "Pixelated"}]
