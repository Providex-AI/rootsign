"""Packaged Alembic migrations.

Shipped inside the wheel so `rootsign-admin init` works for users who
`pip install rootsign` without cloning the repo. `rootsign/cli.py` builds
an `alembic.config.Config` programmatically and resolves this directory
via `importlib.resources` — no `alembic.ini` lookup, no cwd assumption.
"""
