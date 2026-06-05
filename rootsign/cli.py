"""rootsign-admin CLI — minimal operator surface for Phase 0.

Commands:
  rootsign-admin init           — alembic upgrade head
  rootsign-admin init --reset   — drop the schema, then upgrade head
  rootsign-admin status         — table row counts
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import psycopg2
import typer

from rootsign.config import settings

app = typer.Typer(
    name="rootsign-admin",
    help="RootSign Phase 0 — schema and storage operations (powered by Providex AI).",
    add_completion=False,
    no_args_is_help=True,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _strip_driver(sqla_url: str) -> str:
    return re.sub(r"^postgresql\+[^:]+://", "postgresql://", sqla_url)


def _run_alembic_upgrade_head() -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        check=True,
    )


def _drop_public_schema() -> None:
    dsn = _strip_driver(settings.DATABASE_URL_SYNC)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cur.execute("CREATE SCHEMA public AUTHORIZATION rootsign")
            cur.execute("GRANT ALL ON SCHEMA public TO public")
            cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    finally:
        conn.close()


@app.command()
def init(
    reset: bool = typer.Option(
        False, "--reset", help="Drop and recreate the schema before upgrading."
    ),
) -> None:
    """Apply migrations to the dev database. Use --reset to start from scratch."""
    start = time.perf_counter()
    if reset:
        typer.echo("Dropping public schema...")
        _drop_public_schema()
    typer.echo("Running alembic upgrade head...")
    _run_alembic_upgrade_head()
    elapsed = time.perf_counter() - start
    typer.echo(f"Done in {elapsed:.2f}s")


@app.command()
def status() -> None:
    """Print row counts for every RootSign table."""
    dsn = _strip_driver(settings.DATABASE_URL_SYNC)
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            tables = [
                "agents",
                "sessions",
                "decisions",
                "actions",
                "approvals",
                "policies",
                "incidents",
            ]
            typer.echo(f"{'table':<14} {'rows':>10}")
            typer.echo("-" * 26)
            for t in tables:
                try:
                    cur.execute(f"SELECT count(*) FROM {t}")
                    n = cur.fetchone()[0]
                    typer.echo(f"{t:<14} {n:>10}")
                except psycopg2.Error:
                    conn.rollback()
                    typer.echo(f"{t:<14} {'(missing)':>10}")
    finally:
        conn.close()


if __name__ == "__main__":
    app()
