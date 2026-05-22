"""Add parent_approval_id to approvals for escalation chain-of-custody.

Revision ID: 0002_approval_parent_id
Revises: 0001_initial_schema
Create Date: 2026-05-21

Adds the self-referential parent_approval_id FK on approvals so a follow-up
APPROVAL_RECORD that resolves a prior escalation can link back to it.

Per founder decision (feedback_req03_decisions): no CHECK constraint enforcing
escalation depth here — depth validation lives in CRUDApproval. This leaves
the DB shape ready for a future relaxation to chained escalations without
requiring a migration.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_approval_parent_id"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column(
            "parent_approval_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_approvals_parent_approval_id",
        "approvals",
        "approvals",
        ["parent_approval_id"],
        ["approval_id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_approvals_parent_approval_id",
        "approvals",
        ["parent_approval_id"],
    )


def downgrade() -> None:
    raise RuntimeError("Downgrade not permitted — forward-only migration policy")
