"""Partial unique index on approvals(action_id) for non-escalated decisions.

Revision ID: 0004_approval_action_unique
Revises: 0003_action_timed_out
Create Date: 2026-07-23

Defence-in-depth for the approval terminal-state guard (pre-Phase-2 audit #5).
`CRUDApproval.create_with_chain_link` / `create_with_action_status_update`
serialise concurrent resolvers with `SELECT ... FOR UPDATE` on the target
Action row. This index is the belt-and-braces: it makes a double-insert
surface as an IntegrityError instead of two silent Approval rows that
last-writer-wins on `Action.authorization_status`.

Scope: at most one *resolving* approval per action. Escalated approvals
(`decision='escalated'`) are excluded from the constraint because an action
may carry a chain of one escalated approval plus its resolving child; only
the terminal-causing decisions (approved/rejected) must be unique per action.

approvals is a regular table (only `actions` is a TimescaleDB hypertable),
so a unique index on `action_id` alone is permitted — no partition column
required. Forward-only per project policy.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0004_approval_action_unique"
down_revision: Union[str, None] = "0003_action_timed_out"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_approvals_action_resolution",
        "approvals",
        ["action_id"],
        unique=True,
        postgresql_where=sa.text("decision <> 'escalated'"),
    )


def downgrade() -> None:
    raise RuntimeError("Downgrade not permitted — forward-only migration policy")
