"""audit #11b: `rootsign-admin init --reset` drops the entire public schema.
It must (a) require an explicit confirmation (or --yes for non-interactive
use) and (b) derive the schema owner role from the DSN rather than hardcoding
'rootsign'.

_drop_public_schema and alembic's command.upgrade are patched so the tests
never touch a real database.
"""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from rootsign.cli import _dsn_role, app

runner = CliRunner()


class TestInitResetConfirmation:
    def test_reset_aborts_without_confirmation(self):
        with (
            patch("rootsign.cli._drop_public_schema") as drop,
            patch("rootsign.cli.command.upgrade"),
        ):
            result = runner.invoke(app, ["init", "--reset"], input="n\n")
        assert result.exit_code != 0  # typer.confirm(abort=True) → Abort
        drop.assert_not_called()

    def test_reset_yes_skips_prompt_and_drops(self):
        with (
            patch("rootsign.cli._drop_public_schema") as drop,
            patch("rootsign.cli.command.upgrade"),
        ):
            result = runner.invoke(app, ["init", "--reset", "--yes"])
        assert result.exit_code == 0, result.output
        drop.assert_called_once()

    def test_reset_confirmed_interactively_drops(self):
        with (
            patch("rootsign.cli._drop_public_schema") as drop,
            patch("rootsign.cli.command.upgrade"),
        ):
            result = runner.invoke(app, ["init", "--reset"], input="y\n")
        assert result.exit_code == 0, result.output
        drop.assert_called_once()

    def test_init_without_reset_never_drops(self):
        with (
            patch("rootsign.cli._drop_public_schema") as drop,
            patch("rootsign.cli.command.upgrade"),
        ):
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 0, result.output
        drop.assert_not_called()


class TestDsnRole:
    def test_role_derived_from_dsn_username(self, monkeypatch):
        monkeypatch.setattr(
            "rootsign.cli.settings.DATABASE_URL_SYNC",
            "postgresql+psycopg2://ci_user:pw@host:5432/db",
        )
        assert _dsn_role() == "ci_user"

    def test_role_falls_back_when_no_username(self, monkeypatch):
        monkeypatch.setattr(
            "rootsign.cli.settings.DATABASE_URL_SYNC",
            "postgresql+psycopg2://host:5432/db",
        )
        assert _dsn_role() == "rootsign"
