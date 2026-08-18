"""rootsign-admin CLI — minimal operator surface for Phase 0.

Commands:
  rootsign-admin start-db       — launch a local TimescaleDB container
  rootsign-admin stop-db        — stop + remove the local container
  rootsign-admin init           — alembic upgrade head
  rootsign-admin init --reset   — drop the schema, then upgrade head
  rootsign-admin status         — table row counts

Alembic migrations ship inside the `rootsign` package (`rootsign/_migrations/`)
and are resolved via `importlib.resources`, so this CLI works identically when
installed from PyPI as it does from the source tree.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from importlib import import_module, resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from rootsign.config import settings

if TYPE_CHECKING:
    from alembic.config import Config

# psycopg2 and alembic live in the optional `postgres` extra (ADR-011), so they
# are imported inside the commands that need them via `_require()`. At module
# level they made `rootsign-admin` unusable on a bare install — even
# `rootsign-admin --help` and the docker-only `start-db` / `stop-db` died with
# ModuleNotFoundError before Typer could dispatch. Same defect class as the
# `rootsign` console script (rootsign/sdk/cli.py).

app = typer.Typer(
    name="rootsign-admin",
    help="RootSign Phase 0 — schema and storage operations (powered by Providex AI).",
    add_completion=False,
    no_args_is_help=True,
)


def _require(module: str) -> Any:
    """Import an optional DB-stack module, or exit with the install hint.

    Mirrors `rootsign.sdk.cli._session_factory`: a missing `postgres` extra
    becomes a one-line actionable error and exit 1, never a raw traceback.
    """
    try:
        return import_module(module)
    except ModuleNotFoundError as exc:
        from rootsign.errors import RootSignPostgresExtraRequired

        typer.echo(
            f"Error: {RootSignPostgresExtraRequired(f'missing module: {exc.name}')}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

# Default container name + image. Mirrors docker-compose.yml so devs can swap
# freely between `rootsign-admin start-db` and `docker-compose up -d db`.
_DB_CONTAINER = "rootsign-timescaledb"
_DB_IMAGE = "timescale/timescaledb:latest-pg16"


def _strip_driver(sqla_url: str) -> str:
    return re.sub(r"^postgresql\+[^:]+://", "postgresql://", sqla_url)


def _migrations_dir() -> Path:
    """Resolve the packaged migrations directory.

    Uses `importlib.resources.files` so the path is correct both in a wheel
    install (inside site-packages) and in an editable / source-tree checkout.
    """
    return Path(str(resources.files("rootsign") / "_migrations"))


def _alembic_config(sync_url: str | None = None) -> Config:
    """Build an Alembic Config pointing at the packaged migrations.

    Never reads `alembic.ini` from cwd — script_location and sqlalchemy.url
    are set directly, so PyPI users (who have no alembic.ini) and devs in the
    repo root behave identically.
    """
    cfg = _require("alembic.config").Config()
    cfg.set_main_option("script_location", str(_migrations_dir()))
    cfg.set_main_option("sqlalchemy.url", sync_url or settings.DATABASE_URL_SYNC)
    return cfg


def _dsn_role() -> str:
    """Owner role for the recreated schema — derived from the configured DSN
    rather than hardcoded, so a non-`rootsign` DB user (e.g. `ci_user`) works
    (audit #11b)."""
    from sqlalchemy.engine import make_url

    return make_url(settings.DATABASE_URL_SYNC).username or "rootsign"


def _drop_public_schema() -> None:
    psycopg2 = _require("psycopg2")
    sql = _require("psycopg2.sql")

    dsn = _strip_driver(settings.DATABASE_URL_SYNC)
    role = sql.Identifier(_dsn_role())
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cur.execute(sql.SQL("CREATE SCHEMA public AUTHORIZATION {}").format(role))
            cur.execute("GRANT ALL ON SCHEMA public TO public")
            cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    finally:
        conn.close()


def _docker_or_die() -> str:
    docker = shutil.which("docker")
    if docker is None:
        typer.echo(
            "docker not found on PATH. Install Docker Desktop "
            "(https://docker.com/products/docker-desktop) and re-run.",
            err=True,
        )
        raise typer.Exit(code=1)
    return docker


def _container_exists() -> bool:
    docker = _docker_or_die()
    res = subprocess.run(
        [docker, "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=^{_DB_CONTAINER}$"],
        capture_output=True,
        text=True,
        check=False,
    )
    return _DB_CONTAINER in res.stdout.split()


def _container_running() -> bool:
    docker = _docker_or_die()
    res = subprocess.run(
        [docker, "ps", "--format", "{{.Names}}", "--filter", f"name=^{_DB_CONTAINER}$"],
        capture_output=True,
        text=True,
        check=False,
    )
    return _DB_CONTAINER in res.stdout.split()


@app.command("start-db")
def start_db(
    port: int = typer.Option(5432, "--port", "-p", help="Host port to bind (default 5432)."),
) -> None:
    """Launch a local TimescaleDB container (PostgreSQL 16 + TimescaleDB 2.x).

    Equivalent to `docker-compose up -d db` but doesn't require the repo's
    docker-compose.yml — useful for users who installed rootsign from PyPI.
    """
    docker = _docker_or_die()
    if _container_running():
        typer.echo(f"{_DB_CONTAINER} already running.")
        return
    if _container_exists():
        typer.echo(f"Starting existing {_DB_CONTAINER}...")
        subprocess.run([docker, "start", _DB_CONTAINER], check=True)
    else:
        typer.echo(f"Pulling {_DB_IMAGE} and starting {_DB_CONTAINER} on port {port}...")
        subprocess.run(
            [
                docker,
                "run",
                "-d",
                "--name",
                _DB_CONTAINER,
                "--restart",
                "unless-stopped",
                "-e",
                "POSTGRES_USER=rootsign",
                "-e",
                "POSTGRES_PASSWORD=rootsign",
                "-e",
                "POSTGRES_DB=rootsign_dev",
                "-p",
                f"{port}:5432",
                _DB_IMAGE,
            ],
            check=True,
        )
    typer.echo("Waiting for Postgres to accept connections...")
    for _ in range(30):
        check = subprocess.run(
            [docker, "exec", _DB_CONTAINER, "pg_isready", "-U", "rootsign", "-d", "rootsign_dev"],
            capture_output=True,
            check=False,
        )
        if check.returncode == 0:
            typer.echo("Ready. Next: `rootsign-admin init`")
            return
        time.sleep(1)
    typer.echo("Container started but pg_isready never returned ready.", err=True)
    raise typer.Exit(code=1)


@app.command("stop-db")
def stop_db(
    remove: bool = typer.Option(
        False, "--remove", "-r", help="Also remove the container (data volume is preserved)."
    ),
) -> None:
    """Stop the local TimescaleDB container."""
    docker = _docker_or_die()
    if not _container_exists():
        typer.echo(f"{_DB_CONTAINER} not found — nothing to stop.")
        return
    if _container_running():
        subprocess.run([docker, "stop", _DB_CONTAINER], check=True)
    if remove:
        subprocess.run([docker, "rm", _DB_CONTAINER], check=True)


@app.command()
def init(
    reset: bool = typer.Option(
        False, "--reset", help="Drop and recreate the schema before upgrading."
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt for --reset (for non-interactive use).",
    ),
) -> None:
    """Apply migrations to the dev database. Use --reset to start from scratch."""
    # Resolve the DB stack before any output or the destructive --reset prompt,
    # so a missing extra fails cleanly instead of half-way through.
    alembic_command = _require("alembic.command")
    start = time.perf_counter()
    if reset:
        # audit #11b: --reset drops the ENTIRE public schema. Require an
        # explicit confirmation (or --yes for CI) so it can't run by accident.
        if not yes:
            typer.confirm(
                "This will DROP the entire public schema "
                f"({_strip_driver(settings.DATABASE_URL_SYNC)}) and destroy all "
                "data. Continue?",
                abort=True,
            )
        typer.echo("Dropping public schema...")
        _drop_public_schema()
    typer.echo("Running alembic upgrade head...")
    alembic_command.upgrade(_alembic_config(), "head")
    elapsed = time.perf_counter() - start
    typer.echo(f"Done in {elapsed:.2f}s")


@app.command()
def status() -> None:
    """Print row counts for every RootSign table."""
    psycopg2 = _require("psycopg2")
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
