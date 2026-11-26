from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragcentre.cli import app as cli

PASSWORD = "correct-horse-battery-staple"


def test_account_init_and_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGCENTRE_DATA_DIR", str(tmp_path / "data"))
    runner = CliRunner()
    result = runner.invoke(cli, ["account", "init"], input=f"alice\n\n{PASSWORD}\n{PASSWORD}\n")
    assert result.exit_code == 0, result.output
    assert "Created account alice" in result.output

    dup = runner.invoke(cli, ["account", "init"], input=f"bob\n\n{PASSWORD}\n{PASSWORD}\n")
    assert dup.exit_code == 1
    assert "already exists" in dup.output


def test_account_reset_password_requires_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGCENTRE_DATA_DIR", str(tmp_path / "data"))
    result = CliRunner().invoke(cli, ["account", "reset-password"], input=f"{PASSWORD}\n{PASSWORD}\n")
    assert result.exit_code == 1
    assert "No account exists" in result.output
