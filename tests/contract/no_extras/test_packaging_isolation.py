"""Packaging-isolation contract tests (ADR-011, Workstream 1).

The core install (`pip install rootsign`, no extras) must be dependency-light
and **DB-free**: `import rootsign` and the whole facade surface must work with
the SQLAlchemy/asyncpg/alembic stack absent, and selecting the Postgres
backend without the `postgres` extra must raise an actionable error.

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


def _run(script: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        [sys.executable, "-c", _BLOCKER + script],
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
