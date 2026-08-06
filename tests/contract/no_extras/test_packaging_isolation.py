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
DB_DRIVERS = ("sqlalchemy", "asyncpg", "psycopg2", "greenlet", "alembic")

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


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _BLOCKER + script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
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
    naming the install command, not a bare ModuleNotFoundError."""
    result = _run(
        "from rootsign.errors import RootSignPostgresExtraRequired\n"
        "from rootsign.sdk.client import get_ingest_client\n"
        "class _StubDB: pass\n"
        "try:\n"
        "    get_ingest_client(db=_StubDB())\n"
        "    print('NO_ERROR')\n"
        "except RootSignPostgresExtraRequired as e:\n"
        "    assert 'rootsign[postgres]' in str(e), str(e)\n"
        "    print('RAISED_OK')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "RAISED_OK" in result.stdout, result.stdout + result.stderr
