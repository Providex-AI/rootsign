"""Agent identity becomes (name, environment).

Revision ID: 0005_agents_name_env_unique
Revises: 0004_approval_action_unique
Create Date: 2026-08-07

(The revision id is abbreviated because `alembic_version.version_num` is
varchar(32).)

ADR-012 keys agent get-or-create on `(name, environment)` — the same logical
agent may exist independently per environment (`invoice-agent` in
`development` and in `production`). The shipped schema's `UNIQUE(name)`
(`uq_agents_name`) both forbids that and gives
`INSERT ... ON CONFLICT (name, environment)` the wrong conflict target, so it
is replaced here with `uq_agents_name_environment`.

Safe with no backfill: `name` is globally unique today, so every existing row
already satisfies the strictly weaker composite key. This is the only schema
change in the Pre-Phase-2 Sprint A. Forward-only per project policy.

`agents` is a regular table (only `actions` is a TimescaleDB hypertable), so a
unique constraint that doesn't include a partition column is permitted.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0005_agents_name_env_unique"
down_revision: Union[str, None] = "0004_approval_action_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_agents_name_environment", "agents", ["name", "environment"]
    )
    op.drop_constraint("uq_agents_name", "agents", type_="unique")


def downgrade() -> None:
    raise RuntimeError("Downgrade not permitted — forward-only migration policy")
