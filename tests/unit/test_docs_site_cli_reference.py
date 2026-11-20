"""The CLI reference pages stay true to the two CLIs.

`reference/cli-rootsign.mdx` and `reference/cli-rootsign-admin.mdx` list every
option, show real output, and tabulate every error message. These tests hold
each part to the code:

- the options documented for a command are exactly the options it has;
- each output block is reproduced by a fresh run, with captured IDs, hashes
  and paths swapped for this run's;
- each row of an error table is matched by the message a real run prints, with
  the exit code the page states;
- defaults the page quotes match the settings classes.

Paths that need PostgreSQL (an unknown session ID, an uninitialised database,
`init`, `status`) are in tests/integration/test_docs_site_cli_reference.py.
"""

from __future__ import annotations

import json
import os
import re
from importlib.metadata import version
from pathlib import Path

import pytest
import typer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from rootsign.cli import app as admin_app
from rootsign.config import Settings
from rootsign.sdk.cli import app as user_app
from rootsign.sdk.config import SDKSettings
from tests.support.docs_pages import (
    assert_lines_match,
    code_block,
    output_after,
    page_text,
    run_script,
)
from tests.support.spool_tree import Endpoint, bind_transport, spool_a_session

USER = "reference/cli-rootsign.mdx"
ADMIN = "reference/cli-rootsign-admin.mdx"
# Values the pages' sample output was captured with.
PAGE_SESSION = "fe478124-ff91-4519-ab3e-726184fa92b4"
PAGE_MANIFEST = "e9b0202780e6e236659051acf4839ba62a74595be5240cb8474cece671975576"
PAGE_SPOOL_SESSION = "88c2ab59-4dc3-437f-8d46-1df2b7308220"
PAGE_RECEIPT = "8480957bf2cde213890f1f6d574120cd9785788350e805f35e725e2f0ce88bb1"

runner = CliRunner()


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------


def _section(page: str, heading: str) -> str:
    """The text under `## <heading>`, up to the next `## `."""
    text = page_text(page)
    start = text.index(f"\n## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else None]


def _documented_options(page: str, heading: str) -> set[str]:
    return set(re.findall(r'<ResponseField name="([^"]+)"', _section(page, heading)))


def _error_rows(page: str, heading: str) -> list[str]:
    """First-column messages of the "### Errors" table in a command's section."""
    section = _section(page, heading)
    table = section[section.index("### Errors") :]
    rows = re.findall(r"^\| (``?)(Error: .+?)\1 \|", table, re.MULTILINE)
    return [message for _, message in rows]


def _pattern(message: str) -> re.Pattern[str]:
    """A page message as a regex: `<placeholder>` matches text, `...` matches anything."""
    parts = re.split(r"(<[^>]+>|\.\.\.)", message)
    regex = "".join(
        ".+?" if p.startswith("<") else ".*" if p == "..." else re.escape(p) for p in parts
    )
    return re.compile(regex)


def _row(page: str, heading: str, startswith: str) -> re.Pattern[str]:
    (message,) = [m for m in _error_rows(page, heading) if m.startswith(startswith)]
    return _pattern(message)


def _assert_error(result, page: str, heading: str, startswith: str, code: int) -> None:
    assert result.exit_code == code, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert _row(page, heading, startswith).fullmatch(result.output.strip()), result.output


def _cli_options(app: typer.Typer, command: str) -> set[str]:
    params = typer.main.get_command(app).commands[command].params
    names = set()
    for param in params:
        if param.param_type_name == "argument":
            names.add(param.name.upper())
        elif "--help" not in param.opts:
            names.add(", ".join(sorted(param.opts, key=lambda o: not o.startswith("--"))))
    return names


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path: Path) -> Path:
    """A two-action session recorded by the quickstart's agent.py."""
    run = run_script(code_block("quickstart.mdx", "agent.py"), tmp_path, name="agent.py")
    return run.only_session()


@pytest.fixture
def unreachable(monkeypatch):
    engine = create_async_engine(
        "postgresql+asyncpg://rootsign:rootsign@localhost:1/rootsign_dev", poolclass=NullPool
    )
    monkeypatch.setattr("rootsign.sdk.cli.AsyncSessionLocal", async_sessionmaker(bind=engine))


# ---------------------------------------------------------------------------
# Both pages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("app", "page", "command"),
    [
        *[(user_app, USER, c) for c in ("verify", "export", "approve")],
        *[(admin_app, ADMIN, c) for c in ("start-db", "stop-db", "init", "sync")],
    ],
)
def test_each_command_documents_exactly_its_options(app, page, command):
    prefix = "rootsign" if app is user_app else "rootsign-admin"
    assert _documented_options(page, f"{prefix} {command}") == _cli_options(app, command)


class TestUsageErrors:
    def test_rootsign_usage_errors_exit_3(self):
        assert "exits `3`" in _section(USER, "Exit codes")
        assert runner.invoke(user_app, []).exit_code == 3
        assert runner.invoke(user_app, ["verify", "--lcal", "x"]).exit_code == 3
        assert runner.invoke(user_app, ["verfy"]).exit_code == 3
        assert runner.invoke(user_app, ["--help"]).exit_code == 0

    def test_rootsign_admin_usage_errors_exit_2(self):
        assert "such as an unknown option, exits `2`" in page_text(ADMIN)
        assert runner.invoke(admin_app, ["sync", "--bogus"]).exit_code == 2
        assert runner.invoke(admin_app, ["--help"]).exit_code == 0


# ---------------------------------------------------------------------------
# rootsign
# ---------------------------------------------------------------------------


def test_version_prints_the_installed_version():
    result = runner.invoke(user_app, ["version"])

    assert result.exit_code == 0
    assert output_after(USER, "rootsign version\n```") == [result.output.strip()]
    assert result.output.strip() == f"rootsign {version('rootsign')}"


class TestVerify:
    def test_the_valid_output(self, session):
        result = runner.invoke(user_app, ["verify", "--local", str(session)])

        assert result.exit_code == 0
        expected = output_after(USER, "rootsign verify --local ~/.rootsign/sessions/")
        assert_lines_match(result.output, expected, {PAGE_SESSION: session.stem})

    def test_local_wins_over_a_session_id(self, session):
        result = runner.invoke(
            user_app, ["verify", "00000000-0000-0000-0000-000000000000", "--local", str(session)]
        )
        assert result.exit_code == 0

    def test_the_spool_reminder(self, tmp_path, monkeypatch):
        spool = tmp_path / "spool"
        session_id = spool_a_session(spool, actions=1)
        monkeypatch.setenv("ROOTSIGN_SPOOL_DIR", str(spool))

        path = spool / "sessions" / f"{session_id}.jsonl"
        result = runner.invoke(user_app, ["verify", "--local", str(path)])

        assert "rootsign-admin sync" in result.output

    def test_nothing_given(self):
        _assert_error(
            runner.invoke(user_app, ["verify"]), USER, "rootsign verify", "Error: provide", 3
        )

    def test_not_a_uuid(self):
        result = runner.invoke(user_app, ["verify", "nope"])
        _assert_error(result, USER, "rootsign verify", "Error: '<value>' is not", 3)

    def test_missing_file_and_folder(self, tmp_path):
        for path in (tmp_path / "missing.jsonl", tmp_path):
            result = runner.invoke(user_app, ["verify", "--local", str(path)])
            _assert_error(result, USER, "rootsign verify", "Error: no session file", 3)

    def test_unreadable_file(self, tmp_path):
        path = tmp_path / "binary.jsonl"
        path.write_bytes(b"\xff\xfe\x00not text")
        result = runner.invoke(user_app, ["verify", "--local", str(path)])
        _assert_error(result, USER, "rootsign verify", "Error: cannot read", 3)

    def test_empty_file(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("\n")
        result = runner.invoke(user_app, ["verify", "--local", str(path)])
        _assert_error(result, USER, "rootsign verify", "Error: <path> is empty", 3)

    def test_unreachable_database(self, unreachable):
        result = runner.invoke(user_app, ["verify", "550e8400-e29b-41d4-a716-446655440001"])
        _assert_error(result, USER, "rootsign verify", "Error: cannot use the database", 3)
        assert "rootsign:***@" in result.output


class TestExport:
    def test_the_export_and_check_output(self, session, tmp_path):
        out = tmp_path / "bundles"

        result = runner.invoke(user_app, ["export", "--local", str(session), "--out", str(out)])

        assert result.exit_code == 0, result.output
        manifest = re.search(r"manifest.json SHA-256:\s+(\w+)", result.output).group(1)
        swaps = {
            PAGE_SESSION: session.stem,
            PAGE_MANIFEST: manifest,
            "bundles/evidence-": f"{out}/evidence-",
        }
        expected = output_after(USER, "rootsign export --local ~/.rootsign/sessions/")
        assert_lines_match(result.output, expected, swaps)

        bundle = out / f"evidence-{session.stem}"
        result = runner.invoke(user_app, ["export", "--check", str(bundle)])
        assert result.exit_code == 0
        assert_lines_match(result.output, output_after(USER, "rootsign export --check"), swaps)

    @pytest.mark.parametrize(
        ("fmt", "reports"),
        [
            ("all", {"report.md", "report.html"}),
            ("json", set()),
            ("md", {"report.md"}),
            ("html", {"report.html"}),
        ],
    )
    def test_each_format_writes_the_reports_the_table_lists(self, session, tmp_path, fmt, reports):
        row = re.search(rf"^  \| `{fmt}` \| (.+) \|$", _section(USER, "rootsign export"), re.M)
        assert set(re.findall(r"`([^`]+)`", row.group(1))) == reports

        result = runner.invoke(
            user_app, ["export", "--local", str(session), "--out", str(tmp_path), "-f", fmt]
        )

        assert result.exit_code == 0, result.output
        written = {p.name for p in (tmp_path / f"evidence-{session.stem}").iterdir()}
        json_documents = {"redaction.json", "timeline.json", "verification.json", "manifest.json"}
        assert written == json_documents | reports

    def test_redact_previews_keeps_hashes_ids_and_timings(self, session, tmp_path):
        runner.invoke(
            user_app,
            ["export", "--local", str(session), "--out", str(tmp_path), "--redact-previews"],
        )

        timeline = json.loads((tmp_path / f"evidence-{session.stem}" / "timeline.json").read_text())
        action = next(e for e in timeline["events"] if e["type"] == "ACTION")
        assert "input_preview" not in action
        for kept in ("input_hash", "output_hash", "action_id", "timestamp"):
            assert action[kept]

    def test_a_tampered_session_still_exports_with_exit_0(self, session, tmp_path):
        lines = session.read_text().splitlines(keepends=True)
        lines[1] = lines[1].replace("1500.0", "15000.0")
        tampered = tmp_path / "tampered.jsonl"
        tampered.write_text("".join(lines))

        result = runner.invoke(
            user_app, ["export", "--local", str(tampered), "--out", str(tmp_path)]
        )

        assert result.exit_code == 0
        assert result.output.startswith("TAMPERED")

    def test_check_ignores_every_other_option(self, session, tmp_path):
        runner.invoke(user_app, ["export", "--local", str(session), "--out", str(tmp_path)])
        bundle = tmp_path / f"evidence-{session.stem}"

        result = runner.invoke(
            user_app, ["export", "--check", str(bundle), "-f", "pdf", "--out", "/nonexistent"]
        )
        assert result.exit_code == 0

    def test_check_fails_on_an_unexpected_file(self, session, tmp_path):
        runner.invoke(user_app, ["export", "--local", str(session), "--out", str(tmp_path)])
        bundle = tmp_path / f"evidence-{session.stem}"
        (bundle / "extra.txt").write_text("x")

        assert runner.invoke(user_app, ["export", "--check", str(bundle)]).exit_code == 1

    def test_errors(self, session, tmp_path, unreachable):
        h = "rootsign export"
        _assert_error(runner.invoke(user_app, ["export"]), USER, h, "Error: provide", 1)
        _assert_error(
            runner.invoke(user_app, ["export", "nope"]), USER, h, "Error: '<value>' is not", 1
        )
        _assert_error(
            runner.invoke(user_app, ["export", "--local", str(session), "-f", "pdf"]),
            USER,
            h,
            "Error: --format",
            1,
        )
        _assert_error(
            runner.invoke(user_app, ["export", "--local", str(tmp_path / "missing.jsonl")]),
            USER,
            h,
            "Error: Session file not found",
            1,
        )
        args = ["export", "--local", str(session), "--out", str(tmp_path)]
        assert runner.invoke(user_app, args).exit_code == 0
        _assert_error(runner.invoke(user_app, args), USER, h, "Error: <dir> already exists", 1)
        _assert_error(
            runner.invoke(user_app, ["export", "550e8400-e29b-41d4-a716-446655440001"]),
            USER,
            h,
            "Error: cannot use the database",
            1,
        )


class TestApprove:
    def test_the_sample_output_is_the_approval_guide_s(self):
        """The reference reuses the guide's captured run, which
        tests/integration/test_docs_site_approval.py reproduces."""
        guide = "guides/human-approval.mdx"
        for marker in ("rootsign approve --list", "rootsign approve 06f72d12"):
            assert output_after(USER, marker) == output_after(guide, marker)

    def test_errors_without_a_database(self, unreachable):
        h = "rootsign approve"
        _assert_error(runner.invoke(user_app, ["approve"]), USER, h, "Error: provide", 1)
        _assert_error(
            runner.invoke(user_app, ["approve", "nope"]), USER, h, "Error: '<value>' is not", 1
        )
        _assert_error(
            runner.invoke(user_app, ["approve", "--list"]),
            USER,
            h,
            "Error: cannot use the database",
            1,
        )


# ---------------------------------------------------------------------------
# rootsign-admin
# ---------------------------------------------------------------------------


def test_the_stated_database_default_is_the_real_one():
    default = Settings.model_fields["DATABASE_URL_SYNC"].default
    assert f"Its default, `{default}`, is the database `start-db` creates." in page_text(ADMIN)


class TestStartAndStopDb:
    """A stub `docker` on PATH answers `ps` from $STUB_PS / $STUB_PS_ALL and
    succeeds at everything else, so these run without a Docker daemon."""

    @pytest.fixture
    def docker(self, tmp_path, monkeypatch):
        stub = tmp_path / "bin" / "docker"
        stub.parent.mkdir()
        stub.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = ps ]; then\n'
            '  case "$*" in *" -a "*) echo "$STUB_PS_ALL" ;; *) echo "$STUB_PS" ;; esac\n'
            "fi\nexit 0\n"
        )
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{stub.parent}{os.pathsep}{os.environ['PATH']}")

        def state(*, exists: bool, running: bool) -> None:
            monkeypatch.setenv("STUB_PS_ALL", "rootsign-timescaledb" if exists else "")
            monkeypatch.setenv("STUB_PS", "rootsign-timescaledb" if running else "")

        return state

    def test_a_new_container(self, docker):
        docker(exists=False, running=False)
        result = runner.invoke(admin_app, ["start-db"])

        assert result.exit_code == 0
        assert output_after(ADMIN, "rootsign-admin start-db\n```") == result.output.splitlines()

    def test_an_existing_and_a_running_container(self, docker):
        docker(exists=True, running=False)
        result = runner.invoke(admin_app, ["start-db"])
        assert result.output.splitlines()[0] == "Starting existing rootsign-timescaledb..."
        assert "the first line is `Starting existing rootsign-timescaledb...`" in page_text(ADMIN)

        docker(exists=True, running=True)
        result = runner.invoke(admin_app, ["start-db"])
        assert result.output.strip() == "rootsign-timescaledb already running."
        assert "prints `rootsign-timescaledb already running.`" in page_text(ADMIN)

    def test_stop_db_with_no_container(self, docker):
        docker(exists=False, running=False)
        result = runner.invoke(admin_app, ["stop-db"])

        assert result.exit_code == 0
        assert f"prints `{result.output.strip()}` and exits `0`" in page_text(ADMIN)

    def test_no_docker_exits_1(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))
        assert runner.invoke(admin_app, ["start-db"]).exit_code == 1
        assert runner.invoke(admin_app, ["stop-db"]).exit_code == 1


def test_the_reset_prompt(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setattr(
        "rootsign.cli.settings.DATABASE_URL_SYNC",
        Settings.model_fields["DATABASE_URL_SYNC"].default,
    )
    with patch("rootsign.cli._drop_public_schema") as drop, patch("alembic.command.upgrade"):
        result = runner.invoke(admin_app, ["init", "--reset"], input="\n")

    drop.assert_not_called()
    assert result.exit_code == 1
    # CI forces colour, and Typer prints "Aborted." in red there.
    lines = re.sub(r"\x1b\[[0-9;]*m", "", result.output).splitlines()
    expected = output_after(ADMIN, "With `--reset`, it asks")
    assert lines[0].startswith(expected[0])
    assert lines[-1] == "Aborted."


class TestSync:
    @pytest.fixture
    def spool(self, tmp_path, monkeypatch) -> tuple[Path, str]:
        spool = tmp_path / "spool"
        session_id = spool_a_session(spool, actions=1)
        monkeypatch.setenv("ROOTSIGN_SPOOL_DIR", str(spool))
        return spool, session_id

    def _swaps(self, spool: Path, session_id: str) -> dict[str, str]:
        return {"<data_dir>/spool": str(spool), PAGE_SPOOL_SESSION: session_id}

    def test_dry_run(self, spool):
        root, session_id = spool
        result = runner.invoke(admin_app, ["sync", "--dry-run"])

        assert result.exit_code == 0
        expected = output_after(ADMIN, "rootsign-admin sync --dry-run\n```")
        assert_lines_match(result.output, expected, self._swaps(root, session_id))

    def test_nothing_to_sync(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROOTSIGN_SPOOL_DIR", str(tmp_path / "empty"))
        result = runner.invoke(admin_app, ["sync"])

        assert result.exit_code == 0
        printed = result.output.strip().replace(str(tmp_path / "empty"), "<spool>")
        assert f"it prints `{printed}` and exits `0`" in page_text(ADMIN)

    def test_no_api_key_exits_1_before_uploading(self, spool, monkeypatch):
        monkeypatch.delenv("ROOTSIGN_API_KEY", raising=False)
        monkeypatch.chdir(spool[0].parent)  # keep the repo's .env out of reach
        result = runner.invoke(admin_app, ["sync"])

        assert result.exit_code == 1
        assert "ROOTSIGN_API_KEY is not set" in result.output

    def test_an_unreachable_endpoint(self, spool, monkeypatch):
        root, session_id = spool
        monkeypatch.setenv("ROOTSIGN_API_KEY", "sk-docs")
        monkeypatch.setenv("ROOTSIGN_CLOUD_URL", "http://localhost:1/v1")
        monkeypatch.setenv("ROOTSIGN_HTTP_MAX_RETRIES", "1")

        result = runner.invoke(admin_app, ["sync"])

        assert result.exit_code == 1
        receipt = re.search(r"receipt SHA-256 (\w+)", result.output).group(1)
        swaps = {**self._swaps(root, session_id), PAGE_RECEIPT: receipt}
        expected = output_after(ADMIN, "For example, against an unreachable endpoint")
        assert_lines_match(result.output, expected, swaps)

    @pytest.mark.parametrize(
        ("endpoint", "word"), [(Endpoint(), "synced"), (Endpoint(reject_at_sequence=1), "REJECTED")]
    )
    def test_the_synced_and_rejected_lines(self, spool, monkeypatch, endpoint, word):
        monkeypatch.setenv("ROOTSIGN_API_KEY", "sk-docs")
        bind_transport(monkeypatch, endpoint.transport())

        result = runner.invoke(admin_app, ["sync"])

        (row,) = re.findall(rf"^\| `({word} .+?)` \|", page_text(ADMIN), re.M)
        line = next(x.strip() for x in result.output.splitlines() if x.strip().startswith(word))
        assert _pattern(row).fullmatch(re.sub(r"\s+", " ", line)), line

    def test_the_settings_table_defaults(self, monkeypatch):
        for name in ("API_KEY", "CLOUD_URL", "HTTP_TIMEOUT_SECONDS", "HTTP_MAX_RETRIES"):
            monkeypatch.delenv(f"ROOTSIGN_{name}", raising=False)
        defaults = SDKSettings(_env_file=None)
        section = _section(ADMIN, "rootsign-admin sync")

        assert f"| `ROOTSIGN_CLOUD_URL` | `{defaults.CLOUD_URL}` |" in section
        assert f"| `ROOTSIGN_HTTP_TIMEOUT_SECONDS` | `{defaults.HTTP_TIMEOUT_SECONDS}` |" in section
        assert f"| `ROOTSIGN_HTTP_MAX_RETRIES` | `{defaults.HTTP_MAX_RETRIES}` |" in section
        assert defaults.API_KEY == ""
