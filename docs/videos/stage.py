"""Stage the working directory a docs video records in.

Each clip under docs/videos/ runs the commands its docs page shows, against
files lifted out of that page, in a fresh directory under /tmp/rootsign-videos.
Run it from the repo root with the project venv; render.sh calls it before each
tape:

    .venv/bin/python docs/videos/stage.py quickstart

The tape's hidden setup then sets HOME to <dir>/home, so `~/.rootsign` in the
recording is a fresh data folder and the developer's own is left alone.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # for tests.support

from tests.support.docs_pages import code_block  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
ROOT = Path("/tmp/rootsign-videos")


def _fresh(name: str) -> Path:
    workdir = ROOT / name
    shutil.rmtree(workdir, ignore_errors=True)
    (workdir / "home").mkdir(parents=True)
    return workdir


def _quickstart_agent(workdir: Path) -> Path:
    agent = workdir / "agent.py"
    agent.write_text(code_block("quickstart.mdx", "agent.py") + "\n", encoding="utf-8")
    return agent


def quickstart() -> None:
    workdir = _fresh("quickstart")
    _quickstart_agent(workdir)
    # No rootsign in this venv: the recording installs it from PyPI.
    python = shutil.which("python3.12") or shutil.which("python3.11")
    assert python, "the quickstart clip needs Python 3.11 or 3.12 on PATH"
    subprocess.run([python, "-m", "venv", str(workdir / ".venv")], check=True)


def evidence_bundle() -> None:
    """One quickstart session, recorded with the repo's rootsign, ready to export."""
    workdir = _fresh("evidence-bundle")
    agent = _quickstart_agent(workdir)
    # A clean environment: the default JSONL backend under the fresh HOME.
    env = {"HOME": str(workdir / "home"), "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"}
    subprocess.run([sys.executable, agent.name], cwd=workdir, env=env, check=True)


# Two panes, one above the other, each titled; the shells start without rc files
# so the exported PS1 holds.
TMUX_CONF = """\
set -g default-shell /bin/bash
set -g default-command "env PS1='$ ' bash --norc --noprofile"
set -g status off
set -g escape-time 0
set -g pane-border-status top
set -g pane-border-format " #{pane_title} "
set -g pane-border-style fg=#585b70
set -g pane-active-border-style fg=#89b4fa
"""


def human_approval() -> None:
    """transfer.py from the page, and a scratch PostgreSQL database for it.

    The database is `rootsign_docs` on the `rootsign-timescaledb` container that
    `rootsign-admin start-db` runs, recreated empty on every render. The
    connection settings go to db.env, which the tape sources out of shot.
    """
    workdir = _fresh("human-approval")
    page = "guides/human-approval.mdx"
    (workdir / "transfer.py").write_text(code_block(page, "transfer.py") + "\n", encoding="utf-8")
    (workdir / "tmux.conf").write_text(TMUX_CONF, encoding="utf-8")

    repo_env = dict(
        line.split("=", 1)
        for line in (REPO / ".env").read_text(encoding="utf-8").splitlines()
        if line.startswith("DATABASE_URL")
    )
    env = {key: value.replace("/rootsign_dev", "/rootsign_docs") for key, value in repo_env.items()}
    assert set(env) == {"DATABASE_URL", "DATABASE_URL_SYNC"} and "rootsign_dev" not in str(env)
    for sql in (
        "DROP DATABASE IF EXISTS rootsign_docs",
        "CREATE DATABASE rootsign_docs OWNER rootsign",
    ):
        psql = [
            "docker",
            "exec",
            "rootsign-timescaledb",
            "psql",
            "-q",
            "-U",
            "rootsign",
            "-d",
            "postgres",
        ]
        subprocess.run([*psql, "-c", sql], check=True)
    run_env = {
        "HOME": str(workdir / "home"),
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        **env,
    }
    subprocess.run(["rootsign-admin", "init"], cwd=workdir, env=run_env, check=True)

    db_env = workdir / "db.env"
    db_env.write_text(
        "ROOTSIGN_BACKEND=postgres\n" + "".join(f"{key}={value}\n" for key, value in env.items()),
        encoding="utf-8",
    )
    db_env.chmod(0o600)


CLIPS = {
    "quickstart": quickstart,
    "evidence-bundle": evidence_bundle,
    "human-approval": human_approval,
}

if __name__ == "__main__":
    CLIPS[sys.argv[1]]()
