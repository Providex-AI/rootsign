"""Packaging-isolation contract tests (ADR-011, Workstream 1).

The core install (`pip install rootsign`, no extras) must be dependency-light,
**DB-free**, and **httpx-free**: `import rootsign` and the whole facade surface
must work with the SQLAlchemy/asyncpg/alembic stack absent, and selecting a
backend whose extra is missing — `postgres` (ADR-011) or `cloud` (ADR-013) —
must raise an actionable error naming the install command.

Two layers here:

  * A **static guard-rail** — no file under `rootsign/sdk/` or `rootsign/mcp/`
    may import a DB driver at module level (the tripwire the spec's W1 asks
    for). This is the fast check that catches a stray top-level import.
  * **Subprocess** import tests — the only reliable in-process way to prove
    DB-free import, since this test process already has SQLAlchemy loaded
    (a `sys.modules` cache hit would mask a regression). A fresh interpreter
    installs a meta-path blocker that makes the DB stack un-importable, then
    imports rootsign. The authoritative end-to-end check is the separate
    `no-extras` CI job that does a real bare `pip install .`.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SDK_DIRS = [REPO_ROOT / "rootsign" / "sdk", REPO_ROOT / "rootsign" / "mcp"]
# Both console-script entry points (pyproject [project.scripts]). Neither may
# pull the DB stack at import time, or the script dies before Typer dispatches
# — including on `--help`. `rootsign/cli.py` had a module-level `import
# psycopg2`; `rootsign/sdk/cli.py` had `from rootsign.database import ...`.
ENTRY_POINT_MODULES = ("rootsign.sdk.cli", "rootsign.cli")
DB_DRIVERS = ("sqlalchemy", "asyncpg", "psycopg2", "greenlet", "alembic")

# rootsign's own DB-stack modules. Importing one of these at module level is
# just as fatal as importing SQLAlchemy directly — they import it themselves —
# but it is invisible to a driver-name check. `rootsign/sdk/cli.py` carried a
# module-level `from rootsign.database import AsyncSessionLocal` that broke
# every console-script invocation on a bare install while the driver guard-rail
# above stayed green.
DB_STACK_MODULES = ("rootsign.database", "rootsign.crud", "rootsign.models")

# The cloud transport's dependency (ADR-013). Same discipline as the DB stack:
# lazy imports only, and an actionable error when the extra is missing.
CLOUD_DEPS = ("httpx",)

# Installed at the top of a fresh interpreter to make the DB stack look absent.
_BLOCKER = """
import sys, importlib.abc
class _Blocker(importlib.abc.MetaPathFinder):
    _BLOCKED = {"sqlalchemy", "asyncpg", "psycopg2", "greenlet", "alembic"}
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in self._BLOCKED:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None
sys.meta_path.insert(0, _Blocker())
"""


# Hides the DB stack *and* httpx — a true bare install. Kept separate from
# `_BLOCKER` so the existing DB-free tests are unaffected: several of them
# import modules (the MCP server) that may legitimately reach for httpx.
_BLOCKER_BARE = _BLOCKER.replace(
    '{"sqlalchemy", "asyncpg", "psycopg2", "greenlet", "alembic"}',
    '{"sqlalchemy", "asyncpg", "psycopg2", "greenlet", "alembic", "httpx"}',
)


def _run(
    script: str,
    extra_env: dict[str, str] | None = None,
    blocker: str = _BLOCKER,
) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        [sys.executable, "-c", blocker + script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


def test_no_module_level_db_driver_imports_in_sdk_or_mcp():
    """Guard-rail: DB drivers must be imported lazily inside functions."""
    offenders = []
    for base in SDK_DIRS:
        for py in base.rglob("*.py"):
            for lineno, line in enumerate(py.read_text().splitlines(), start=1):
                stripped = line.lstrip()
                if stripped != line:
                    continue  # indented → inside a function/class → allowed
                for drv in DB_DRIVERS:
                    if stripped.startswith((f"import {drv}", f"from {drv} ")):
                        offenders.append(f"{py.relative_to(REPO_ROOT)}:{lineno}: {stripped}")
    assert not offenders, "module-level DB-driver imports found:\n" + "\n".join(offenders)


def test_no_module_level_internal_db_stack_imports_in_sdk_or_mcp():
    """Guard-rail: rootsign's own DB modules are equally off-limits at module level.

    `from rootsign.database import X` pulls SQLAlchemy transitively, so it
    breaks a bare install exactly like a direct driver import — but a
    driver-name check can't see it. This is the check that would have caught
    the `rootsign/sdk/cli.py` console-script regression.
    """
    offenders = []
    for base in SDK_DIRS:
        for py in base.rglob("*.py"):
            for lineno, line in enumerate(py.read_text().splitlines(), start=1):
                stripped = line.lstrip()
                if stripped != line:
                    continue  # indented → deferred inside a function → allowed
                for mod in DB_STACK_MODULES:
                    if stripped.startswith((f"import {mod}", f"from {mod} ", f"from {mod}.")):
                        offenders.append(f"{py.relative_to(REPO_ROOT)}:{lineno}: {stripped}")
    assert not offenders, "module-level rootsign DB-stack imports found:\n" + "\n".join(offenders)


def test_no_module_level_db_imports_in_entry_point_modules():
    """Guard-rail over the console-script modules themselves.

    `rootsign/cli.py` sits outside SDK_DIRS, so the sweeps above never saw its
    module-level `import psycopg2` / `from alembic import command`.
    """
    offenders = []
    files = [REPO_ROOT / (m.replace(".", "/") + ".py") for m in ENTRY_POINT_MODULES]
    for py in files:
        for lineno, line in enumerate(py.read_text().splitlines(), start=1):
            stripped = line.lstrip()
            if stripped != line:
                continue  # indented → deferred inside a function → allowed
            for mod in DB_DRIVERS + DB_STACK_MODULES:
                if stripped.startswith((f"import {mod}", f"from {mod} ", f"from {mod}.")):
                    offenders.append(f"{py.relative_to(REPO_ROOT)}:{lineno}: {stripped}")
    assert not offenders, "console-script modules import the DB stack eagerly:\n" + "\n".join(
        offenders
    )


def test_import_entry_point_modules_without_db_stack():
    """Both console-script modules must import with the DB stack absent."""
    for mod in ENTRY_POINT_MODULES:
        result = _run(f"import {mod}; print('OK')")
        assert result.returncode == 0, f"{mod}: {result.stderr}"
        assert "OK" in result.stdout


def test_admin_cli_help_works_without_db_stack():
    """`rootsign-admin --help` must be usable on a bare install."""
    result = _run(
        "import rootsign.cli as admin\n"
        "from typer.testing import CliRunner\n"
        "r = CliRunner().invoke(admin.app, ['--help'])\n"
        "assert r.exit_code == 0, r.output\n"
        "print('OK')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_import_sdk_cli_without_db_stack():
    """The `rootsign` console script's module must import DB-free.

    The entry point is `rootsign.sdk.cli:app`, a different import graph from
    `import rootsign` — so a green `test_import_rootsign_without_db_stack`
    proves nothing about the CLI. Without this, a module-level DB import in
    cli.py crashes `rootsign version` and `rootsign verify --local` on a bare
    install while every other check passes.
    """
    result = _run("import rootsign.sdk.cli; print('OK')")
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_cli_app_builds_and_dbfree_commands_are_reachable():
    """Typer must be able to build the command tree with the DB stack absent."""
    result = _run(
        "import rootsign.sdk.cli as cli\n"
        "from typer.testing import CliRunner\n"
        "r = CliRunner().invoke(cli.app, ['version'])\n"
        "assert r.exit_code == 0, r.output\n"
        "assert 'rootsign' in r.output, r.output\n"
        "print('OK')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_import_rootsign_without_db_stack():
    """`import rootsign` + the public surface must work with the DB stack gone."""
    result = _run(
        "import sys\n"
        "import rootsign\n"
        "from rootsign import (trace, session, wrap_tools, wrap_crewai_tools,\n"
        "    BufferedIngestClient, IngestClient, get_ingest_client,\n"
        "    verify_session_local, SessionContext, StandardPIIConfig,\n"
        "    register_agent, LangGraphTracer, CrewAITracer)\n"
        "assert 'sqlalchemy' not in sys.modules, 'sqlalchemy leaked into core import'\n"
        "print('OK')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_import_mcp_server_without_db_stack():
    """The MCP audit server module must import DB-free too (lazy queries)."""
    result = _run("import rootsign.mcp.server; print('OK')")
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_postgres_backend_without_extra_raises_actionable_error():
    """Selecting postgres without the extra → RootSignPostgresExtraRequired
    naming the install command, not a bare ModuleNotFoundError. Explicitly
    pin postgres — the default backend is now jsonl (ADR-011), which needs no
    DB and would return a JsonlIngestClient instead."""
    result = _run(
        "from rootsign.errors import RootSignPostgresExtraRequired\n"
        "from rootsign.sdk.client import get_ingest_client\n"
        "class _StubDB: pass\n"
        "try:\n"
        "    get_ingest_client(db=_StubDB())\n"
        "    print('NO_ERROR')\n"
        "except RootSignPostgresExtraRequired as e:\n"
        "    assert 'rootsign[postgres]' in str(e), str(e)\n"
        "    print('RAISED_OK')\n",
        extra_env={"ROOTSIGN_BACKEND": "postgres"},
    )
    assert result.returncode == 0, result.stderr
    assert "RAISED_OK" in result.stdout, result.stdout + result.stderr


def test_db_backed_sdk_entry_points_raise_actionable_error():
    """Every DB-backed SDK path must name the extra, not leak ModuleNotFoundError.

    Deferring the DB imports (ADR-011) only moves *where* the failure happens.
    `get_ingest_client` translated the error from the start, but the SDK entry
    points did not — so the same mistake produced a helpful install hint or a
    bare traceback depending on which door you came through. A user hitting
    `rootsign.init()` on a bare install saw `ModuleNotFoundError: No module
    named 'sqlalchemy'` from inside `get_or_register_agent`, with nothing
    pointing at `pip install 'rootsign[postgres]'`.
    """
    result = _run(
        "import asyncio\n"
        "from uuid import uuid4\n"
        "from rootsign.errors import RootSignPostgresExtraRequired\n"
        "from rootsign.sdk.registration import get_or_register_agent, register_agent\n"
        "from rootsign.sdk.chain import verify_session\n"
        "from rootsign.mcp import server as mcp_server\n"
        "\n"
        "async def check(label, fn):\n"
        "    try:\n"
        "        await fn()\n"
        "    except RootSignPostgresExtraRequired as e:\n"
        "        assert 'rootsign[postgres]' in str(e), (label, str(e))\n"
        "        return\n"
        "    except ModuleNotFoundError as e:\n"
        "        raise AssertionError(f'{label} leaked ModuleNotFoundError: {e}')\n"
        "    raise AssertionError(f'{label} did not raise')\n"
        "\n"
        "async def main():\n"
        "    await check('get_or_register_agent', lambda: get_or_register_agent(\n"
        "        name='agent-x', owner='team-o', environment='development',\n"
        "        risk_tier='medium', framework='custom', description=None,\n"
        "        model_version=None, permitted_tools=None, regulatory_categories=None))\n"
        "    await check('register_agent', lambda: register_agent(\n"
        "        name='agent-y', owner='team-o', environment='development',\n"
        "        risk_tier='medium', framework='custom'))\n"
        "    await check('verify_session', lambda: verify_session(uuid4(), db=None))\n"
        "\n"
        "asyncio.run(main())\n"
        "\n"
        "try:\n"
        "    mcp_server._default_session_factory()\n"
        "except RootSignPostgresExtraRequired as e:\n"
        "    assert 'rootsign[postgres]' in str(e)\n"
        "except ModuleNotFoundError as e:\n"
        "    raise AssertionError(f'mcp session factory leaked: {e}')\n"
        "else:\n"
        "    raise AssertionError('mcp session factory did not raise')\n"
        "print('OK')\n",
        extra_env={"ROOTSIGN_BACKEND": "postgres"},
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# Cloud extra (ADR-013 / Sprint B T2.1)
# ---------------------------------------------------------------------------


def test_no_module_level_httpx_imports_in_sdk_or_mcp():
    """Guard-rail: httpx is the `cloud` extra — import it inside functions only.

    `rootsign/sdk/client.py` defines `HttpIngestClient`, and the class must
    stay importable on a bare install: `import rootsign` walks this module,
    so a module-level `import httpx` would break every bare-install path the
    way `import sqlalchemy` once did.
    """
    offenders = []
    for base in SDK_DIRS:
        for py in base.rglob("*.py"):
            for lineno, line in enumerate(py.read_text().splitlines(), start=1):
                stripped = line.lstrip()
                if stripped != line:
                    continue  # indented → deferred inside a function → allowed
                for dep in CLOUD_DEPS:
                    if stripped.startswith((f"import {dep}", f"from {dep} ")):
                        offenders.append(f"{py.relative_to(REPO_ROOT)}:{lineno}: {stripped}")
    assert not offenders, "module-level cloud-extra imports found:\n" + "\n".join(offenders)


def test_import_client_module_without_httpx():
    """`HttpIngestClient` must be importable with the cloud extra absent."""
    result = _run(
        "from rootsign.sdk.client import HttpIngestClient\n"
        "assert HttpIngestClient.owns_retry is True\n"
        "print('OK')\n",
        blocker=_BLOCKER_BARE,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_cloud_backend_without_extra_raises_actionable_error():
    """Selecting cloud without the extra → RootSignCloudExtraRequired.

    The twin of the postgres check above: the failure must name
    `pip install 'rootsign[cloud]'`, not surface a ModuleNotFoundError from
    inside httpx's import graph.
    """
    result = _run(
        "from rootsign.errors import RootSignCloudExtraRequired\n"
        "from rootsign.sdk.client import get_ingest_client\n"
        "try:\n"
        "    get_ingest_client()\n"
        "    print('NO_ERROR')\n"
        "except RootSignCloudExtraRequired as e:\n"
        "    assert 'rootsign[cloud]' in str(e), str(e)\n"
        "    print('RAISED_OK')\n",
        extra_env={"ROOTSIGN_BACKEND": "cloud"},
        blocker=_BLOCKER_BARE,
    )
    assert result.returncode == 0, result.stderr
    assert "RAISED_OK" in result.stdout, result.stdout + result.stderr


def test_export_local_works_without_the_db_stack(tmp_path):
    """`rootsign export --local` is the bare-install evidence path.

    It is also the only way a cloud-mode user turns a spooled session into
    something they can hand over, so a module-level DB import anywhere in the
    export graph would strand exactly the users who need it most. Runs the real
    command against a real session file with sqlalchemy hidden.
    """
    import json
    from uuid import uuid4

    session_id = str(uuid4())
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True)
    # Store-shaped ACTION_RECORD line: flat canonical fields, as ADR-011 writes
    # them. Hand-built here because the writer cannot run in the blocked env.
    (sessions / f"{session_id}.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "sdk_version": "0.3.0",
                "event_type": "SESSION_OPEN",
                "event_id": str(uuid4()),
                "emitted_at": "2026-08-21T09:00:00+00:00",
                "agent_id": str(uuid4()),
                "session_id": session_id,
                "payload": {"objective": "bare install"},
            }
        )
        + "\n"
    )

    result = _run(
        "import rootsign.sdk.cli as cli\n"
        "from typer.testing import CliRunner\n"
        f"r = CliRunner().invoke(cli.app, ['export', '--local', {str(sessions / (session_id + '.jsonl'))!r},"
        f" '--out', {str(tmp_path / 'out')!r}])\n"
        "assert r.exit_code == 0, r.output\n"
        "assert 'manifest.json SHA-256' in r.output, r.output\n"
        "print('OK')\n",
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
    assert (tmp_path / "out" / f"evidence-{session_id}" / "manifest.json").is_file()


def test_export_check_works_without_the_db_stack(tmp_path):
    """Checking a bundle someone sent must not require anything but Python."""
    import json
    from uuid import uuid4

    session_id = str(uuid4())
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{session_id}.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "sdk_version": "0.3.0",
                "event_type": "SESSION_OPEN",
                "event_id": str(uuid4()),
                "emitted_at": "2026-08-21T09:00:00+00:00",
                "agent_id": str(uuid4()),
                "session_id": session_id,
                "payload": {},
            }
        )
        + "\n"
    )
    out = tmp_path / "out"

    result = _run(
        "import rootsign.sdk.cli as cli\n"
        "from typer.testing import CliRunner\n"
        "runner = CliRunner()\n"
        f"e = runner.invoke(cli.app, ['export', '--local', {str(sessions / (session_id + '.jsonl'))!r},"
        f" '--out', {str(out)!r}])\n"
        "assert e.exit_code == 0, e.output\n"
        f"c = runner.invoke(cli.app, ['export', '--check', {str(out / ('evidence-' + session_id))!r}])\n"
        "assert c.exit_code == 0, c.output\n"
        "assert 'INTACT' in c.output, c.output\n"
        "print('OK')\n",
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_export_from_postgres_without_the_extra_names_the_extra():
    """`export <session_id>` legitimately needs the extra — and must say so in
    one line rather than dying inside sqlalchemy's import graph."""
    result = _run(
        "import rootsign.sdk.cli as cli\n"
        "from typer.testing import CliRunner\n"
        "r = CliRunner().invoke(cli.app, ['export', '550e8400-e29b-41d4-a716-446655440001'])\n"
        "assert r.exit_code == 1, r.output\n"
        "assert 'rootsign[postgres]' in r.output, r.output\n"
        "assert 'Traceback' not in r.output, r.output\n"
        "print('OK')\n",
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_admin_sync_is_reachable_without_the_cloud_extra():
    """`rootsign-admin sync --help` and `--dry-run` must work on a bare install.

    `sync` is the one operator command that needs the `cloud` extra to do its
    job — but not to be *found*, and not to inspect the spool. An operator
    deciding whether to install anything reads the help and runs the dry run
    first, and Typer builds the whole command tree on any invocation, so a
    module-level httpx import in the sync path would break `rootsign-admin`
    entirely (the ADR-011 console-script defect, one extra over).
    """
    result = _run(
        "import rootsign.cli as admin\n"
        "from typer.testing import CliRunner\n"
        "runner = CliRunner()\n"
        "assert runner.invoke(admin.app, ['sync', '--help']).exit_code == 0\n"
        "r = runner.invoke(admin.app, ['sync', '--dry-run', '--spool-dir', '/nonexistent'])\n"
        "assert r.exit_code == 0, r.output\n"
        "assert 'Nothing to sync' in r.output, r.output\n"
        "print('OK')\n",
        blocker=_BLOCKER_BARE,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_admin_sync_without_the_cloud_extra_names_the_extra(tmp_path):
    """Uploading without the extra must print the install command, not a traceback.

    Reaching the transport requires something to upload, so this writes one
    spooled session file first — the same shape the JSONL writer produces,
    since the spool *is* that writer (ADR-013 Decision 4).
    """
    import json
    from uuid import uuid4

    session_id = str(uuid4())
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{session_id}.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "sdk_version": "0.3.0",
                "event_type": "SESSION_OPEN",
                "event_id": str(uuid4()),
                "emitted_at": "2026-08-21T09:00:00+00:00",
                "agent_id": str(uuid4()),
                "session_id": session_id,
                "payload": {"objective": "bare install"},
            }
        )
        + "\n"
    )

    result = _run(
        "import rootsign.cli as admin\n"
        "from typer.testing import CliRunner\n"
        f"r = CliRunner().invoke(admin.app, ['sync', '--spool-dir', {str(tmp_path)!r}])\n"
        "assert r.exit_code == 1, r.output\n"
        "assert 'rootsign[cloud]' in r.output, r.output\n"
        "assert 'Traceback' not in r.output, r.output\n"
        "print('OK')\n",
        extra_env={"ROOTSIGN_API_KEY": "sk-bare-install"},
        blocker=_BLOCKER_BARE,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_jsonl_default_still_works_without_httpx():
    """The bare install's default path must not have acquired a cloud dependency."""
    result = _run(
        "from rootsign.sdk.client import get_ingest_client\n"
        "from rootsign.sdk.jsonl_client import JsonlIngestClient\n"
        "assert isinstance(get_ingest_client(), JsonlIngestClient)\n"
        "print('OK')\n",
        extra_env={"ROOTSIGN_BACKEND": "jsonl"},
        blocker=_BLOCKER_BARE,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
