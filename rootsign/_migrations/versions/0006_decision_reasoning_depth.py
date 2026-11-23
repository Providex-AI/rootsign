"""Store the reasoning depth on each decision.

Revision ID: 0006_decision_reasoning_depth
Revises: 0005_agents_name_env_unique
Create Date: 2026-11-22

ADR-008 records the `ROOTSIGN_REASONING_DEPTH` in force when a decision was
captured, so a reader can tell a `reasoning_summary` dropped by `minimal`, or
cut to 500 characters by `summary`, from one the developer never supplied.
Every DECISION_RECORD carries `reasoning_depth`, and the JSONL backend keeps
it, but the `decisions` table had no column for it, so PostgreSQL discarded it.

Additive and nullable: rows written before this revision have no depth
recorded, and NULL says exactly that. Forward-only per project policy.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_decision_reasoning_depth"
down_revision: Union[str, None] = "0005_agents_name_env_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("decisions", sa.Column("reasoning_depth", sa.String(20), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Downgrade not permitted — forward-only migration policy")
