"""What the two CLIs show their users: help text and the --reset prompt.

The help text is the first reference most people read, and the docs site's
CLI pages quote it. It used to carry internal planning language ("RootSign
Phase 0"), design-record numbers, "the dev database" for whatever
DATABASE_URL_SYNC points at, and Markdown that a terminal prints literally.
Developer notes now sit after `\\f` in the docstrings, which Click leaves out
of `--help`.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from rootsign.cli import app as admin_app
from rootsign.sdk.cli import app as user_app

runner = CliRunner()

HELP_PAGES = [
    pytest.param(user_app, [], id="rootsign"),
    *[
        pytest.param(user_app, [c], id=f"rootsign {c}")
        for c in ("version", "verify", "export", "approve")
    ],
    pytest.param(admin_app, [], id="rootsign-admin"),
    *[
        pytest.param(admin_app, [c], id=f"rootsign-admin {c}")
        for c in ("start-db", "stop-db", "init", "sync", "status")
    ],
]


@pytest.mark.parametrize(("app", "command"), HELP_PAGES)
def test_help_has_no_internal_language(app, command):
    result = runner.invoke(app, [*command, "--help"])

    assert result.exit_code == 0, result.output
    for internal in ("Phase 0", "ADR-", "dev database", "HiTL", "**", "my_session"):
        assert internal not in result.output, internal


def test_reset_prompt_hides_the_database_password(monkeypatch):
    monkeypatch.setattr(
        "rootsign.cli.settings.DATABASE_URL_SYNC",
        "postgresql+psycopg2://rootsign:s3cret-pw@db.internal:5432/rootsign",
    )
    with (
        patch("rootsign.cli._drop_public_schema") as drop,
        patch("alembic.command.upgrade"),
    ):
        result = runner.invoke(admin_app, ["init", "--reset"], input="n\n")

    drop.assert_not_called()
    assert "postgresql://rootsign:***@db.internal:5432/rootsign" in result.output
    assert "s3cret-pw" not in result.output
