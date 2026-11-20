"""audit #11b: `rootsign-admin init --reset` drops the entire public schema.
It must (a) require an explicit confirmation (or --yes for non-interactive
use) and (b) derive the schema owner role from the DSN rather than hardcoding
'rootsign'.

_drop_public_schema and alembic's command.upgrade are patched so the tests
never touch a real database.

The upgrade patch targets `alembic.command.upgrade` at its source rather than
a name re-exported by `rootsign.cli`. Alembic lives in the optional `postgres`
extra (ADR-011), so the operator CLI resolves it lazily via `_require()` and no
longer binds `command` at module scope — binding it there made
`rootsign-admin --help` crash on a bare install. `_require` returns the real
module object, so patching the attribute on `alembic.command` still applies.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from typer.testing import CliRunner

from rootsign.cli import _dsn_role, app

runner = CliRunner()


class TestInitResetConfirmation:
    def test_reset_aborts_without_confirmation(self):
        with (
            patch("rootsign.cli._drop_public_schema") as drop,
            patch("alembic.command.upgrade"),
        ):
            result = runner.invoke(app, ["init", "--reset"], input="n\n")
        assert result.exit_code != 0  # typer.confirm(abort=True) → Abort
        drop.assert_not_called()

    def test_reset_yes_skips_prompt_and_drops(self):
        with (
            patch("rootsign.cli._drop_public_schema") as drop,
            patch("alembic.command.upgrade"),
        ):
            result = runner.invoke(app, ["init", "--reset", "--yes"])
        assert result.exit_code == 0, result.output
        drop.assert_called_once()

    def test_reset_confirmed_interactively_drops(self):
        with (
            patch("rootsign.cli._drop_public_schema") as drop,
            patch("alembic.command.upgrade"),
        ):
            result = runner.invoke(app, ["init", "--reset"], input="y\n")
        assert result.exit_code == 0, result.output
        drop.assert_called_once()

    def test_init_without_reset_never_drops(self):
        with (
            patch("rootsign.cli._drop_public_schema") as drop,
            patch("alembic.command.upgrade"),
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


class TestStartDbVolume:
    """`stop-db --remove` promised the data volume was preserved, but
    `start-db` ran the container with no `-v`. The image declares a VOLUME,
    so Docker gave each new container a fresh anonymous volume: after
    `stop-db --remove` and `start-db`, the database came back empty.

    `start-db` now mounts the named volume `rootsign_pgdata`, which outlives
    the container and is reattached by the next `docker run`.

    The `docker` on PATH here is a stub script that logs its arguments, so the
    test sees the exact command line without a Docker daemon.
    """

    @pytest.fixture
    def docker_log(self, tmp_path, monkeypatch):
        log = tmp_path / "docker.log"
        stub = tmp_path / "bin" / "docker"
        stub.parent.mkdir()
        # `ps` lists nothing (no container yet); `run`, `start` and the
        # pg_isready probe all succeed.
        stub.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 0\n')
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{stub.parent}{os.pathsep}{os.environ['PATH']}")
        return log

    def test_new_container_mounts_the_named_volume(self, docker_log):
        result = runner.invoke(app, ["start-db"])

        assert result.exit_code == 0, result.output
        run = next(line for line in docker_log.read_text().splitlines() if line.startswith("run "))
        assert "-v rootsign_pgdata:/var/lib/postgresql/data" in run

    def test_stop_db_help_names_the_volume(self):
        result = runner.invoke(app, ["stop-db", "--help"])

        assert "rootsign_pgdata" in result.output
